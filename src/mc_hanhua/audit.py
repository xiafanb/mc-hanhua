"""Read-only pre-publication safety audit.

After translation is written back, the output copy is re-scanned with the same
unified scan() and audited before the archive is repacked:

1. structure: every JSON / NBT / SNBT structure that parsed in the input must
   still parse in the output (clear damage blocks publication);
2. mechanism: the cross-file reference index (selector names, CustomName NBT
   matches, clear / execute if|unless items|data match values) must be
   unchanged between input and output;
3. resources: function / schedule / scoreboard / team / bossbar / storage
   identifiers are validated read-only; unknown syntax, dynamic Mod-registered
   namespaces and cross-pack references only warn;
4. books: page boundaries, consecutive blank lines, leading/trailing newlines,
   raw/filtered/extra component structure and event attributes are checked;
   overlong pages only warn.

Only clear structural damage (JSON/NBT/SNBT that can no longer be parsed) and
changed mechanism match values raise error-level issues that block
publication; everything else is warning or info.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Any

from .models import ScanResult, TextUnit
from .nbt import (
    TAG_BYTE,
    TAG_BYTE_ARRAY,
    TAG_COMPOUND,
    TAG_DOUBLE,
    TAG_FLOAT,
    TAG_INT,
    TAG_INT_ARRAY,
    TAG_LIST,
    TAG_LONG,
    TAG_LONG_ARRAY,
    TAG_SHORT,
    TAG_STRING,
    NbtTag,
    get_tag,
    read_nbt_file,
    read_region_chunks,
)
from .quality import clue_token_drift, container_display_risk
from .text_components import escape_pointer_part

NBT_WARNING_PREFIX = "无法解析 NBT 文件，已跳过："
BOOK_PAGE_VISIBLE_WARNING_CHARS = 400
# Book container structure checks are only performed for standalone .dat/.nbt
# files (the most complex writer path); region files are skipped to keep the
# audit bounded on large worlds.
BOOK_CONTAINER_SUFFIXES = {".dat", ".nbt"}

FUNCTION_ID_RE = re.compile(r"^[a-z0-9_.-]+:[a-z0-9_./-]+$")
TAG_FUNCTION_ID_RE = re.compile(r"^#[a-z0-9_.-]+:[a-z0-9_./-]+$")
BOSSBAR_ID_RE = re.compile(r"^[a-z0-9_.-]+:[a-z0-9_.-]+$")
STORAGE_ID_RE = re.compile(r"^[a-z0-9_.-]+:[a-z0-9_./-]+$")
NAMESPACE_RE = re.compile(r"^[a-z0-9_.-]+$")
_BOOK_PATH_MARKERS = ("pages", "filtered_pages", "written_book_content", "writable_book_content", "raw", "filtered")


class AuditBlockedError(Exception):
    """Raised when the pre-publication audit found clear structural damage."""

    def __init__(
        self,
        message: str,
        *,
        task_id: str = "",
        error_code: str = "audit_blocked",
        phase: str = "audit",
        archive_id: str = "",
        report_path: str = "",
        workspace_path: str = "",
        recoverable: bool = True,
    ) -> None:
        super().__init__(message)
        self.task_id = task_id
        self.error_code = error_code
        self.phase = phase
        self.archive_id = archive_id
        self.report_path = report_path
        self.workspace_path = workspace_path
        self.recoverable = recoverable


@dataclass(slots=True)
class AuditIssue:
    level: str  # error | warning | info
    category: str  # structure | mechanism | function | schedule | scoreboard | team | bossbar | storage | book | writeback | selector | numeric | enum | json | nbt
    file: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {"level": self.level, "category": self.category, "file": self.file, "message": self.message, "details": dict(self.details)}
        locator = self.details.get("locator")
        if locator:
            payload["locator"] = locator
        return payload


@dataclass(slots=True)
class AuditResult:
    issues: list[AuditIssue] = field(default_factory=list)
    rescanned_units: int = 0
    rescanned_files: int = 0
    rescan_failed: bool = False
    # Mechanism reference index summary: how many values were locked and
    # sample values, so the report can explain every preserved match value.
    locked_ref_count: int = 0
    locked_ref_samples: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(issue.level == "error" for issue in self.issues)

    @property
    def errors(self) -> list[AuditIssue]:
        return [issue for issue in self.issues if issue.level == "error"]

    @property
    def warnings(self) -> list[AuditIssue]:
        return [issue for issue in self.issues if issue.level == "warning"]

    @property
    def infos(self) -> list[AuditIssue]:
        return [issue for issue in self.issues if issue.level == "info"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "blocked": self.blocked,
            "rescan_failed": self.rescan_failed,
            "rescanned_units": self.rescanned_units,
            "rescanned_files": self.rescanned_files,
            "locked_ref_count": self.locked_ref_count,
            "locked_ref_samples": list(self.locked_ref_samples),
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "info_count": len(self.infos),
            "errors": [issue.to_dict() for issue in self.errors],
            "warnings": [issue.to_dict() for issue in self.warnings],
            "infos": [issue.to_dict() for issue in self.infos],
        }

    def issue(self, level: str, category: str, file: str, message: str, **details: Any) -> None:
        self.issues.append(AuditIssue(level, category, file, message, dict(details)))


@dataclass(slots=True)
class AuditBaseline:
    """Pre-write state of the working copy, captured after the input scan."""

    json_parseable: set[str] = field(default_factory=set)
    nbt_broken: set[str] = field(default_factory=set)
    # (rel_path, unit.locator) -> structural skeleton of the JSON container.
    book_containers: dict[tuple[str, str], object] = field(default_factory=dict)
    json_documents: dict[str, Any] = field(default_factory=dict)
    nbt_documents: dict[str, NbtTag] = field(default_factory=dict)
    mca_chunk_raw_sha: dict[tuple[str, int], str] = field(default_factory=dict)
    mca_chunk_docs: dict[tuple[str, int], NbtTag] = field(default_factory=dict)


@dataclass(slots=True)
class PackResources:
    namespaces: set[str] = field(default_factory=lambda: {"minecraft"})
    functions: set[str] = field(default_factory=set)
    function_tags: set[str] = field(default_factory=set)


def take_baseline(root: Path, scan_result: ScanResult) -> AuditBaseline:
    """Record parseability and book container structure before any write-back."""

    baseline = AuditBaseline()
    for path in _iter_files(root):
        rel = path.relative_to(root).as_posix()
        suffix = path.suffix.lower()
        if suffix == ".json":
            try:
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                continue
            baseline.json_parseable.add(rel)
            baseline.json_documents[rel] = payload
        elif suffix in {".dat", ".nbt"}:
            try:
                baseline.nbt_documents[rel] = read_nbt_file(path).root
            except Exception:
                continue
        elif suffix == ".mca":
            try:
                for chunk in read_region_chunks(path):
                    index = int(chunk["index"])
                    baseline.mca_chunk_raw_sha[(rel, index)] = hashlib.sha256(bytes(chunk["raw"])).hexdigest()
                    baseline.mca_chunk_docs[(rel, index)] = chunk["doc"].root
            except Exception:
                continue
    for warning in scan_result.warnings:
        if warning.startswith(NBT_WARNING_PREFIX):
            baseline.nbt_broken.add(warning[len(NBT_WARNING_PREFIX) :].split("（", 1)[0])

    for unit in scan_result.text_units:
        if _book_unit_kind(unit) != "nbt":
            continue
        path = _unit_nbt_path(unit)
        if path is None:
            continue
        tag = _baseline_nbt_tag(baseline, unit, path)
        if tag is None or tag.type_id != TAG_STRING:
            continue
        try:
            parsed = json.loads(tag.value)
        except json.JSONDecodeError:
            continue
        baseline.book_containers[(unit.file_path, unit.locator)] = _json_skeleton(parsed)
    return baseline


def _baseline_nbt_tag(baseline: AuditBaseline, unit: TextUnit, path: list[str | int]) -> NbtTag | None:
    rel = unit.file_path
    try:
        if rel.lower().endswith(".mca"):
            locator = json.loads(unit.locator)
            chunk_id = int(str(locator.get("chunk")))
            root = baseline.mca_chunk_docs.get((rel, chunk_id))
            if root is None:
                return None
            return get_tag(root, path)
        root = baseline.nbt_documents.get(rel)
        if root is None:
            return None
        return get_tag(root, path)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def audit_output(
    root: Path,
    baseline: AuditBaseline | None = None,
    baseline_scan: ScanResult | None = None,
    units: list[TextUnit] | None = None,
    registry: Any = None,
    cancel_event: Event | None = None,
) -> AuditResult:
    """Re-scan the output copy and run every read-only pre-publication check."""

    from .pipeline import scan  # lazy: pipeline imports audit
    from .translator import TranslationPaused

    result = AuditResult()
    baseline = baseline or AuditBaseline()
    baseline_scan = baseline_scan or ScanResult(root=root)
    units = units or []
    try:
        rescan = scan(root, registry, cancel_event=cancel_event)
    except TranslationPaused:
        # A cancel must surface as a pause, never as a structural audit error (A12).
        raise
    except Exception as exc:
        result.rescan_failed = True
        result.issue("error", "structure", "", f"输出重新扫描失败：{exc}", exc=str(exc))
        return result
    result.rescanned_units = len(rescan.text_units)
    result.rescanned_files = len({unit.file_path for unit in rescan.text_units})
    result.locked_ref_count = len(baseline_scan.mechanism_refs)
    result.locked_ref_samples = sorted(baseline_scan.mechanism_refs)[:10]

    _audit_json_structures(result, root, baseline.json_parseable)
    _audit_nbt_structures(result, rescan, baseline.nbt_broken)
    _audit_mechanism_refs(result, baseline_scan.mechanism_refs, rescan.mechanism_refs)
    _audit_unparsed_commands(result, baseline_scan.file_filtered)
    _audit_immutable_units(result, units)
    resources = _collect_pack_resources(root)
    result.issues.extend(audit_resources(rescan.commands, resources))
    result.issues.extend(audit_books(units))
    result.issues.extend(_audit_book_containers(result, root, baseline, units))
    result.issues.extend(_audit_located_writeback(result, root, units))
    result.issues.extend(_audit_non_text_fields(result, root, baseline, units))
    return result


def _iter_files(root: Path) -> list[Path]:
    return [path for path in root.rglob("*") if path.is_file()]


def _audit_json_structures(result: AuditResult, root: Path, baseline_parseable: set[str]) -> None:
    for path in sorted(_iter_files(root)):
        if path.suffix.lower() != ".json":
            continue
        rel = path.relative_to(root).as_posix()
        try:
            json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            if rel in baseline_parseable:
                result.issue("error", "structure", rel, f"JSON 结构在写回后损坏：{exc}", exc=str(exc))
            else:
                result.issue("warning", "structure", rel, f"JSON 无法解析（输入同样损坏，跳过）：{exc}", exc=str(exc))


def _audit_nbt_structures(result: AuditResult, rescan: ScanResult, baseline_broken: set[str]) -> None:
    for warning in rescan.warnings:
        if not warning.startswith(NBT_WARNING_PREFIX):
            continue
        rel = warning[len(NBT_WARNING_PREFIX) :].split("（", 1)[0]
        if rel in baseline_broken:
            result.issue("warning", "structure", rel, "NBT 无法解析（输入同样损坏，跳过）")
        else:
            result.issue("error", "structure", rel, f"NBT 结构在写回后损坏：{warning}")


def _audit_unparsed_commands(result: AuditResult, file_filtered: dict[str, dict[str, int]]) -> None:
    """Surface files whose commands could not be fully parsed (input side)."""

    for rel, counts in sorted(file_filtered.items()):
        count = counts.get("unparsed_mechanism", 0)
        if count:
            result.issue("warning", "mechanism", rel, f"{count} 条命令的 SNBT 未能解析（已保留原文）", unparsed=count)


def _audit_immutable_units(result: AuditResult, units: list[TextUnit]) -> None:
    """Block publication if an identity / match / numeric / selector value changed."""

    from .policy import POLICY_IMMUTABLE_ID, POLICY_IMMUTABLE_MATCH, POLICY_UNKNOWN
    from .utils import ENTITY_SELECTOR_RE

    for unit in units:
        if unit.final_target is None or unit.final_target == unit.source_text:
            continue
        policy = unit.policy or (unit.metadata or {}).get("policy") or ""
        if policy in {POLICY_IMMUTABLE_ID, POLICY_IMMUTABLE_MATCH, POLICY_UNKNOWN}:
            result.issue(
                "error",
                "mechanism",
                unit.file_path,
                f"不可翻译字段被改写：{unit.source_text!r} -> {unit.final_target!r}",
                locator=unit.locator,
                policy=policy,
                category_hint=policy,
            )
            continue
        if ENTITY_SELECTOR_RE.fullmatch(unit.source_text.strip()):
            result.issue(
                "error",
                "selector",
                unit.file_path,
                f"选择器被改写：{unit.source_text!r} -> {unit.final_target!r}",
                locator=unit.locator,
            )
            continue
        if _looks_numeric_or_enum(unit.source_text) and unit.final_target != unit.source_text:
            result.issue(
                "error",
                "numeric",
                unit.file_path,
                f"数值/布尔/枚举被改写：{unit.source_text!r} -> {unit.final_target!r}",
                locator=unit.locator,
            )


def _looks_numeric_or_enum(value: str) -> bool:
    stripped = value.strip().lower()
    if stripped in {"true", "false", "null", "none"}:
        return True
    if re.fullmatch(r"-?\d+(\.\d+)?[bslfd]?", stripped):
        return True
    return False


def _audit_mechanism_refs(result: AuditResult, baseline_refs: set[str], output_refs: set[str]) -> None:
    missing = sorted(baseline_refs - output_refs)
    for value in missing[:20]:
        result.issue(
            "error",
            "mechanism",
            "",
            f"机制锁定值在输出中改变：{value}",
            value=value,
            locator="",
        )
    if len(missing) > 20:
        result.issue("error", "mechanism", "", f"机制锁定值在输出中改变：另有 {len(missing) - 20} 个值", remaining=len(missing) - 20)
    added = sorted(output_refs - baseline_refs)
    for value in added[:10]:
        result.issue(
            "warning",
            "mechanism",
            "",
            f"输出中出现了新的机制引用值（可能为动态拼接或书写差异）：{value}",
            value=value,
        )


# ---------------------------------------------------------------------------
# Resource ID audit (read-only, warning level)
# ---------------------------------------------------------------------------


def _collect_pack_resources(root: Path) -> PackResources:
    resources = PackResources()
    for path in root.rglob("*"):
        if path.is_dir() and path.name == "data":
            for child in path.iterdir():
                if child.is_dir() and NAMESPACE_RE.match(child.name):
                    resources.namespaces.add(child.name)
    for func_dir in root.rglob("functions"):
        if not func_dir.is_dir():
            continue
        parts = func_dir.parts
        try:
            data_index = parts.index("data")
        except ValueError:
            continue
        if data_index + 2 >= len(parts):
            continue
        ns = parts[data_index + 1]
        under_tags = parts[data_index + 2] == "tags"
        if under_tags:
            for tag_path in func_dir.rglob("*.json"):
                rel = tag_path.relative_to(func_dir).as_posix()
                resources.function_tags.add(f"#{ns}:{rel[: -len('.json')]}")
        else:
            for func_path in func_dir.rglob("*.mcfunction"):
                rel = func_path.relative_to(func_dir).as_posix()
                resources.functions.add(f"{ns}:{rel[: -len('.mcfunction')]}")
    return resources


def _split_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    current: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(text):
        ch = text[index]
        if quote is not None:
            if ch == "\\":
                current.append(ch)
                if index + 1 < len(text):
                    current.append(text[index + 1])
                    index += 1
            elif ch == quote:
                quote = None
            else:
                current.append(ch)
        elif ch in "'\"":
            quote = ch
        elif ch.isspace():
            if current:
                tokens.append("".join(current))
                current = []
        else:
            current.append(ch)
        index += 1
    if current:
        tokens.append("".join(current))
    return tokens


def _command_starts(line: str) -> list[str]:
    """Line start plus every nested command after an execute ... run boundary."""

    starts = [line]
    current = line
    while True:
        match = re.search(r"\srun\s", current)
        if not match:
            break
        prefix = current[: match.start()]
        if not prefix.strip().lstrip("/").lower().startswith("execute "):
            break
        current = current[match.end() :]
        starts.append(current)
    return starts


def audit_resources(commands: list[tuple[str, str]], resources: PackResources | None = None) -> list[AuditIssue]:
    """Validate function/schedule/scoreboard/team/bossbar/storage identifiers.

    Definitions are collected in command order; usages of undefined or
    format-invalid identifiers warn. Unknown namespaces (Mod injection or
    cross-pack references) only warn.
    """

    resources = resources or PackResources()
    issues: list[AuditIssue] = []
    objectives: set[str] = set()
    teams: set[str] = set()
    bossbars: set[str] = set()

    def objective_use(name: str, file: str, line: str) -> None:
        if not name or name.startswith("@") or name == "*" or name.isdigit():
            return
        if name not in objectives:
            issues.append(AuditIssue("warning", "scoreboard", file, f"引用了未定义的 scoreboard objective：{name}", {"line": line}))

    def team_use(name: str, file: str, line: str) -> None:
        if name and name not in teams:
            issues.append(AuditIssue("warning", "team", file, f"引用了未定义的 team：{name}", {"line": line}))

    def bossbar_use(bid: str, file: str, line: str) -> None:
        if bid and bid not in bossbars:
            issues.append(AuditIssue("warning", "bossbar", file, f"引用了未定义的 bossbar：{bid}", {"line": line}))

    def function_id_check(fid: str, file: str, line: str) -> None:
        fid = fid.strip()
        if fid.startswith("#"):
            if not TAG_FUNCTION_ID_RE.match(fid):
                issues.append(AuditIssue("warning", "function", file, f"函数标签 ID 格式非法：{fid}", {"line": line}))
                return
            namespace = fid[1:].split(":", 1)[0]
            if namespace not in resources.namespaces:
                issues.append(AuditIssue("warning", "function", file, f"未知命名空间（跨包或 Mod 注入）：{fid}", {"line": line}))
            elif fid not in resources.function_tags:
                issues.append(AuditIssue("warning", "function", file, f"函数标签引用缺失：{fid}", {"line": line}))
            return
        if not FUNCTION_ID_RE.match(fid):
            issues.append(AuditIssue("warning", "function", file, f"函数 ID 格式非法（缺少命名空间或含非法字符）：{fid}", {"line": line}))
            return
        namespace = fid.split(":", 1)[0]
        if namespace not in resources.namespaces:
            issues.append(AuditIssue("warning", "function", file, f"未知命名空间（跨包或 Mod 注入）：{fid}", {"line": line}))
        elif fid not in resources.functions:
            issues.append(AuditIssue("warning", "function", file, f"函数引用缺失（可能由其他数据包提供）：{fid}", {"line": line}))

    for file, line in commands:
        for start in _command_starts(line):
            tokens = _split_tokens(start)
            if not tokens:
                continue
            cmd = tokens[0].lstrip("/").lower()
            if cmd == "function":
                if len(tokens) >= 2:
                    function_id_check(tokens[1], file, line)
            elif cmd == "schedule":
                if len(tokens) >= 3 and tokens[1].lower() == "function":
                    function_id_check(tokens[2], file, line)
            elif cmd == "scoreboard":
                if len(tokens) >= 4 and tokens[1].lower() == "objectives":
                    op = tokens[2].lower()
                    name = tokens[3]
                    if op == "add":
                        objectives.add(name)
                    elif op == "remove":
                        objectives.discard(name)
                    elif op == "modify":
                        objective_use(name, file, line)
                    elif op == "setdisplay" and len(tokens) >= 5:
                        objective_use(tokens[4], file, line)
                elif len(tokens) >= 5 and tokens[1].lower() == "players":
                    op = tokens[2].lower()
                    if op in {"add", "set", "remove", "get", "enable", "random"}:
                        objective_use(tokens[4], file, line)
                    elif op == "reset":
                        # scoreboard players reset <target> [objective]
                        objective_use(tokens[4], file, line)
                    elif op == "operation" and len(tokens) >= 7:
                        objective_use(tokens[4], file, line)
                        objective_use(tokens[6], file, line)
            elif cmd == "team":
                if len(tokens) >= 3:
                    op = tokens[1].lower()
                    name = tokens[2]
                    if op == "add":
                        teams.add(name)
                    elif op in {"join", "leave", "empty", "modify", "msg"}:
                        team_use(name, file, line)
            elif cmd == "bossbar":
                if len(tokens) >= 3:
                    op = tokens[1].lower()
                    bid = tokens[2]
                    if not BOSSBAR_ID_RE.match(bid):
                        issues.append(AuditIssue("warning", "bossbar", file, f"bossbar ID 格式非法：{bid}", {"line": line}))
                    elif op == "add":
                        bossbars.add(bid)
                    elif op in {"set", "get", "remove"}:
                        bossbar_use(bid, file, line)
            elif cmd == "storage":
                if len(tokens) >= 2 and not STORAGE_ID_RE.match(tokens[1]):
                    issues.append(AuditIssue("warning", "storage", file, f"storage ID 格式非法（需要 namespace:path）：{tokens[1]}", {"line": line}))
            elif cmd == "data":
                if (
                    len(tokens) >= 4
                    and tokens[1].lower() in {"get", "merge", "modify", "remove"}
                    and tokens[2].lower() == "storage"
                    and not STORAGE_ID_RE.match(tokens[3])
                ):
                    issues.append(AuditIssue("warning", "storage", file, f"storage ID 格式非法（需要 namespace:path）：{tokens[3]}", {"line": line}))

            # execute sub-clauses and selectors that reference the same pools.
            for match in re.finditer(r"\b(?:if|unless)\s+score\s+(\S+)\s+(\S+)", start, re.IGNORECASE):
                objective_use(match.group(2), file, line)
            for match in re.finditer(r"\bstore\s+(?:result|success)\s+score\s+(\S+)\s+(\S+)", start, re.IGNORECASE):
                objective_use(match.group(2), file, line)
            for match in re.finditer(r"@[a-z]\[[^\]]*team\s*=\s*([^,\]\s]+)", start, re.IGNORECASE):
                team_use(match.group(1).strip("\"'"), file, line)
            for match in re.finditer(r"\b(?:if|unless)\s+data\s+storage\s+(\S+)", start, re.IGNORECASE):
                if not STORAGE_ID_RE.match(match.group(1)):
                    issues.append(AuditIssue("warning", "storage", file, f"storage ID 格式非法（需要 namespace:path）：{match.group(1)}", {"line": line}))
            for match in re.finditer(r"\bwith\s+storage\s+(\S+)", start, re.IGNORECASE):
                if not STORAGE_ID_RE.match(match.group(1)):
                    issues.append(AuditIssue("warning", "storage", file, f"storage ID 格式非法（需要 namespace:path）：{match.group(1)}", {"line": line}))
    return issues


# ---------------------------------------------------------------------------
# Book quality checks (warning level only)
# ---------------------------------------------------------------------------


def _book_unit_kind(unit: TextUnit) -> str | None:
    """Classify a unit as a book page: nbt | command | json | None."""

    try:
        locator = json.loads(unit.locator)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if unit.format == "nbt-text":
        path = locator.get("path")
        if isinstance(path, list):
            lowered = [str(part).lower() for part in path]
            if any(marker in lowered for marker in _BOOK_PATH_MARKERS):
                return "nbt"
        return None
    if unit.format == "mcfunction":
        command = locator.get("command")
        if isinstance(command, dict) and command.get("kind") == "snbt" and "\n" in unit.source_text:
            return "command"
        return None
    if unit.format == "generic-json" and "pages" in unit.locator.lower():
        return "json"
    return None


def _blank_line_indices(text: str) -> tuple[int, ...]:
    return tuple(index for index, line in enumerate(text.split("\n")) if not line)


def audit_books(units: list[TextUnit]) -> list[AuditIssue]:
    """Check page boundaries, blank lines, boundary newlines and page length."""

    issues: list[AuditIssue] = []
    for unit in units:
        if unit.final_target is None or _book_unit_kind(unit) is None:
            continue
        source, target = unit.source_text, unit.final_target
        file_path = unit.file_path
        context = unit.context
        if source.count("\n") != target.count("\n"):
            issues.append(
                AuditIssue(
                    "warning",
                    "book",
                    file_path,
                    f"书页换行数量变化：{source.count(chr(10))} -> {target.count(chr(10))}",
                    {"context": context, "source": source, "target": target},
                )
            )
        elif "\n" in source and _blank_line_indices(source) != _blank_line_indices(target):
            issues.append(
                AuditIssue("warning", "book", file_path, "书页连续空行位置变化", {"context": context, "source": source, "target": target})
            )
        source_lead = len(source) - len(source.lstrip("\n"))
        target_lead = len(target) - len(target.lstrip("\n"))
        source_trail = len(source) - len(source.rstrip("\n"))
        target_trail = len(target) - len(target.rstrip("\n"))
        if source_lead != target_lead or source_trail != target_trail:
            issues.append(
                AuditIssue(
                    "warning",
                    "book",
                    file_path,
                    "书页行首/行尾换行变化",
                    {"context": context, "source": source, "target": target, "source_lead": source_lead, "target_lead": target_lead},
                )
            )
        for reason in clue_token_drift(source, target):
            issues.append(
                AuditIssue(
                    "warning",
                    "book",
                    file_path,
                    f"关键线索标记变化：{reason}",
                    {"context": context, "source": source, "target": target, "reason": reason},
                )
            )
    by_page: dict[tuple[str, str], list[TextUnit]] = {}
    for unit in units:
        if unit.final_target is None or _book_unit_kind(unit) is None:
            continue
        owner = unit.metadata.get("layout_owner") or unit.context.rsplit("/", 1)[0]
        by_page.setdefault((unit.file_path, str(owner)), []).append(unit)
    for (file_path, owner), members in by_page.items():
        if container_display_risk(members, limit=BOOK_PAGE_VISIBLE_WARNING_CHARS):
            issues.append(
                AuditIssue(
                    "warning",
                    "book",
                    file_path,
                    f"拼接后书页疑似超出可视范围（{sum(len(unit.final_target or '') for unit in members)} 字符）",
                    {"context": owner, "unit_count": len(members)},
                )
            )
    return issues


def _json_skeleton(value: Any) -> Any:
    """Structural skeleton: keys/containers/event attributes, text values folded."""

    if isinstance(value, dict):
        return ("dict", sorted((key, _json_skeleton(child)) for key, child in value.items()))
    if isinstance(value, list):
        return ("list", [_json_skeleton(child) for child in value])
    return ("leaf",)


def _unit_nbt_path(unit: TextUnit) -> list[str | int] | None:
    try:
        locator = json.loads(unit.locator)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    path = locator.get("path")
    return path if isinstance(path, list) else None


def _audit_book_containers(result: AuditResult, root: Path, baseline: AuditBaseline, units: list[TextUnit]) -> list[AuditIssue]:
    """Compare JSON container structure (extra/click/hover/style) before/after."""

    issues: list[AuditIssue] = []
    read_cache: dict[str, Any] = {}
    unit_by_locator = {(unit.file_path, unit.locator): unit for unit in units}
    current_file = None
    for (rel, locator_json), expected in sorted(baseline.book_containers.items()):
        if rel != current_file:
            read_cache.clear()
            current_file = rel
        unit = unit_by_locator.get((rel, locator_json))
        path = _unit_nbt_path(unit) if unit is not None else None
        if path is None:
            continue
        tag = _output_nbt_tag(root, unit, path, read_cache)
        if tag is None:
            continue
        if tag.type_id != TAG_STRING:
            continue
        try:
            actual = _json_skeleton(json.loads(tag.value))
        except json.JSONDecodeError:
            result.issue("warning", "structure", rel, "书页 JSON 容器在写回后无法解析")
            continue
        if actual != expected:
            result.issue(
                "error",
                "book",
                rel,
                "书页组件结构变化（extra/clickEvent/hoverEvent/style 等非文本属性被改写）",
                locator=locator_json,
            )
    return issues


def _output_nbt_tag(root: Path, unit: TextUnit, path: list[str | int], cache: dict[str, Any]) -> NbtTag | None:
    file_path = root / unit.file_path
    try:
        if unit.file_path.lower().endswith(".mca"):
            chunks = cache.get(unit.file_path)
            if chunks is None:
                chunks = read_region_chunks(file_path)
                cache[unit.file_path] = chunks
            locator = json.loads(unit.locator)
            chunk_id = str(locator.get("chunk", ""))
            for chunk in chunks:
                if str(chunk["index"]) == chunk_id:
                    return get_tag(chunk["doc"].root, path)
            return None
        doc = cache.get(unit.file_path)
        if doc is None:
            doc = read_nbt_file(file_path).root
            cache[unit.file_path] = doc
        return get_tag(doc, path)
    except Exception:
        return None


def _should_verify_located_writeback(unit: TextUnit) -> bool:
    if unit.final_target is None:
        return False
    if "!/" in unit.file_path:
        return False
    if unit.format not in {"json-lang", "legacy-lang", "generic-json", "mcfunction", "nbt-text"}:
        return False
    status = unit.metadata.get("writeback_status")
    if status == "blocked":
        return False
    if unit.final_target == unit.source_text and status not in {"changed", "unchanged", "failed"}:
        return False
    return True


def _layout_empty_slot(unit: TextUnit) -> bool:
    # Only an explicit empty string is a layout blank. ``None`` means the unit
    # was never translated and the original must stay on disk; treating it as
    # "expect ''" blocked every unfinished sign / TextDisplay (Realm: 343).
    return (
        unit.final_target == ""
        and unit.metadata.get("layout_container") in {"sign", "text-display"}
        and "!/" not in unit.file_path
    )


def _audit_located_writeback(result: AuditResult, root: Path, units: list[TextUnit]) -> list[AuditIssue]:
    """Read written fields by locator, independent of looks_translatable."""

    from .extractors import LocatedTextReadError, read_located_text

    issues: list[AuditIssue] = []
    read_cache: dict[str, Any] = {}
    current_file = None
    for unit in sorted(units, key=lambda item: item.file_path):
        if unit.file_path != current_file:
            read_cache.clear()
            current_file = unit.file_path
        if unit.metadata.get("writeback_status") == "failed" and "!/" not in unit.file_path:
            issues.append(
                AuditIssue(
                    "error",
                    "writeback",
                    unit.file_path,
                    "译文未落盘：写回失败",
                    {"context": unit.context, "locator": unit.locator, "expected": unit.final_target or ""},
                )
            )
            continue
        if not _should_verify_located_writeback(unit) and not _layout_empty_slot(unit):
            continue
        expected = unit.final_target if unit.final_target is not None else ""
        try:
            actual = read_located_text(root, unit, cache=read_cache)
        except LocatedTextReadError as exc:
            level = "warning" if exc.reason == "source_corrupt" else "error"
            category = "structure" if exc.reason == "source_corrupt" else "writeback"
            message = {
                "missing_file": "译文未落盘：目标文件缺失",
                "locator_missing": "写回定位丢失",
                "source_corrupt": "源文件本来损坏，无法核对写回",
                "unsupported_format": f"不支持的写回格式：{unit.format}",
            }.get(exc.reason, str(exc))
            issues.append(
                AuditIssue(
                    level,
                    category,
                    unit.file_path,
                    message,
                    {
                        "context": unit.context,
                        "locator": unit.locator,
                        "expected": expected,
                        "reason": exc.reason,
                    },
                )
            )
            continue
        if actual != expected:
            issues.append(
                AuditIssue(
                    "error",
                    "writeback",
                    unit.file_path,
                    f"写回后文本不一致：期望 {expected!r}，输出 {actual!r}",
                    {
                        "context": unit.context,
                        "locator": unit.locator,
                        "expected": expected,
                        "actual": actual,
                    },
                )
            )
    return issues


def _writable_leaf_keys(units: list[TextUnit]) -> dict[str, set[str]]:
    allowed: dict[str, set[str]] = {}
    for unit in units:
        if unit.final_target is None or "!/" in unit.file_path:
            continue
        if unit.format == "generic-json":
            allowed.setdefault(unit.file_path, set()).add(unit.locator)
        elif unit.format == "nbt-text":
            try:
                locator = json.loads(unit.locator)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            path = locator.get("path")
            if not isinstance(path, list):
                continue
            pointer = _nbt_path_pointer(path)
            json_pointer = locator.get("json")
            if isinstance(json_pointer, str) and json_pointer:
                pointer = f"{pointer}{json_pointer}" if json_pointer.startswith("/") else f"{pointer}/{json_pointer}"
            allowed.setdefault(unit.file_path, set()).add(pointer)
    return allowed


def _nbt_path_pointer(path: list[Any]) -> str:
    return "/" + "/".join(escape_pointer_part(str(part)) for part in path)


def _json_non_text_skeleton(value: Any, pointer: str, allowed: set[str]) -> Any:
    if pointer in allowed:
        return ("writable",)
    if isinstance(value, dict):
        return ("dict", sorted((key, _json_non_text_skeleton(child, f"{pointer}/{escape_pointer_part(str(key))}", allowed)) for key, child in value.items()))
    if isinstance(value, list):
        return ("list", [_json_non_text_skeleton(child, f"{pointer}/{index}", allowed) for index, child in enumerate(value)])
    return ("leaf", value)


def _nbt_non_text_skeleton(tag: NbtTag, pointer: str, allowed: set[str]) -> Any:
    if pointer in allowed and tag.type_id == TAG_STRING:
        return ("writable", TAG_STRING)
    if tag.type_id == TAG_COMPOUND:
        return (
            "compound",
            sorted(
                (key, _nbt_non_text_skeleton(child, f"{pointer}/{escape_pointer_part(str(key))}", allowed))
                for key, child in tag.value.items()
            ),
        )
    if tag.type_id == TAG_LIST:
        child_type, children = tag.value
        return (
            "list",
            child_type,
            [_nbt_non_text_skeleton(child, f"{pointer}/{index}", allowed) for index, child in enumerate(children)],
        )
    if tag.type_id in {TAG_BYTE, TAG_SHORT, TAG_INT, TAG_LONG, TAG_FLOAT, TAG_DOUBLE}:
        return ("number", tag.type_id, tag.value)
    if tag.type_id in {TAG_BYTE_ARRAY, TAG_INT_ARRAY, TAG_LONG_ARRAY}:
        return ("array", tag.type_id, list(tag.value))
    if tag.type_id == TAG_STRING:
        try:
            parsed = json.loads(tag.value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return ("string", tag.value)
        if isinstance(parsed, (dict, list)):
            return ("json-string", _json_non_text_skeleton(parsed, pointer, allowed))
        return ("string", tag.value)
    return ("other", tag.type_id)


def _audit_non_text_fields(result: AuditResult, root: Path, baseline: AuditBaseline, units: list[TextUnit]) -> list[AuditIssue]:
    """Compare non-writable JSON/NBT structure; only extractor leaves may change."""

    issues: list[AuditIssue] = []
    allowed = _writable_leaf_keys(units)
    changed_files = {unit.file_path for unit in units if unit.metadata.get("writeback_status") == "changed" and "!/" not in unit.file_path}
    for rel, expected in baseline.json_documents.items():
        if rel not in changed_files and rel not in allowed:
            continue
        path = root / rel
        try:
            actual = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        if _json_non_text_skeleton(expected, "", allowed.get(rel, set())) != _json_non_text_skeleton(actual, "", allowed.get(rel, set())):
            issues.append(
                AuditIssue(
                    "error",
                    "json",
                    rel,
                    "非文本 JSON 字段、数组长度或事件框架被改写",
                    {"file": rel},
                )
            )
    for rel, expected in baseline.nbt_documents.items():
        if rel not in changed_files and rel not in allowed:
            continue
        path = root / rel
        try:
            actual = read_nbt_file(path).root
        except Exception:
            continue
        if _nbt_non_text_skeleton(expected, "", allowed.get(rel, set())) != _nbt_non_text_skeleton(actual, "", allowed.get(rel, set())):
            issues.append(
                AuditIssue(
                    "error",
                    "nbt",
                    rel,
                    "非文本 NBT 字段、数组长度或事件框架被改写",
                    {"file": rel},
                )
            )
    issues.extend(_audit_mca_non_text(root, baseline, allowed, changed_files))
    return issues


def _audit_mca_non_text(
    root: Path,
    baseline: AuditBaseline,
    allowed: dict[str, set[str]],
    changed_files: set[str],
) -> list[AuditIssue]:
    issues: list[AuditIssue] = []
    mca_files = {rel for rel, _index in baseline.mca_chunk_raw_sha}
    for rel in sorted(mca_files):
        if rel not in changed_files and rel not in allowed:
            continue
        path = root / rel
        try:
            chunks = read_region_chunks(path)
        except Exception as exc:
            issues.append(AuditIssue("error", "structure", rel, f"MCA 结构在写回后损坏：{exc}", {"exc": str(exc)}))
            continue
        actual_raw = {int(chunk["index"]): hashlib.sha256(bytes(chunk["raw"])).hexdigest() for chunk in chunks}
        actual_docs = {int(chunk["index"]): chunk["doc"].root for chunk in chunks}
        expected_indexes = {index for file_rel, index in baseline.mca_chunk_raw_sha if file_rel == rel}
        if set(actual_raw) != expected_indexes:
            issues.append(AuditIssue("error", "nbt", rel, "MCA chunk 集合变化", {"file": rel}))
            continue
        for index in expected_indexes:
            expected_sha = baseline.mca_chunk_raw_sha[(rel, index)]
            if actual_raw.get(index) == expected_sha:
                continue
            expected_doc = baseline.mca_chunk_docs.get((rel, index))
            actual_doc = actual_docs.get(index)
            if expected_doc is None or actual_doc is None:
                issues.append(AuditIssue("error", "nbt", rel, f"MCA chunk {index} 无法比较非文本语义", {"chunk": index}))
                continue
            if _nbt_non_text_skeleton(expected_doc, "", allowed.get(rel, set())) != _nbt_non_text_skeleton(actual_doc, "", allowed.get(rel, set())):
                issues.append(
                    AuditIssue(
                        "error",
                        "nbt",
                        rel,
                        f"MCA chunk {index} 的非文本 NBT 语义被改写",
                        {"chunk": index},
                    )
                )
    return issues
