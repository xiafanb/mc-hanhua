"""Read-only purpose index for macro parameters (hardening plan W3, phase 1).

Answers "what is this value actually used for?" from the package itself instead
of guessing from the text: every ``$(param)`` occurrence in a macro function
body is classified by **position** —

``display``
    inside a display leaf the extractor already recognises
    (``extract_command_texts`` returns exactly those values);
``mechanism``
    inside a command name / function target / selector / resource identifier;
``forward``
    handed on as another function's argument; resolved recursively with a
    bound, because a display value may travel through several macros;
``unknown``
    none of the above.

A call-site argument value is display-translatable only when its parameter is a
display sink along **every** link of the call chain — that is the condition W3
will enforce once the index has been reviewed on real packages.

Nothing here changes behaviour: the pipeline does not import this module. It is
the evidence stage of W3, so the classification can be spot-checked before any
policy flips.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .models import TextUnit
from .text_components import (
    BOSSBAR_NAME_COMMAND_RE,
    ITEM_MATCH_KEYS,
    SNBT_MECHANISM_KEYS,
    SNBT_NONTEXT_KEYS,
    SNBT_PARENTED_TEXT_KEYS,
    SNBT_TEXT_KEYS,
    TEXT_COMPONENT_KEYS,
    TITLE_TEXT_COMMAND_RE,
    _find_execute_run,
    extract_command_texts,
)

MACRO_RE = re.compile(r"\$\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)")
#: Values under these keys are displayed (a ``with:`` entry substitutes into a
#: translated component, ``body``/``label``/``text`` are shown as-is).
#: ``body``/``label`` are the 1.21.6 dialog display fields; once W3 flips policy
#: they belong in ``text_components``' shared vocabulary rather than here.
DISPLAY_ARGUMENT_KEYS = frozenset(
    TEXT_COMPONENT_KEYS | SNBT_TEXT_KEYS | SNBT_PARENTED_TEXT_KEYS
    | {"with", "contents", "tooltip", "description", "subtitle", "actionbar", "body", "label"}
)
#: Values under these keys name, address or drive something else — a command
#: template, an input key, a numeric field — and are never display text.
MECHANISM_ARGUMENT_KEYS = frozenset(
    SNBT_MECHANISM_KEYS | SNBT_NONTEXT_KEYS | ITEM_MATCH_KEYS
    | {"command", "value", "template", "run_command", "suggest_command", "key", "id", "initial", "step", "width", "columns", "macro"}
)
#: A quoted literal starting with one of these is a command, so a macro inside
#: it is an argument rather than display text.
COMMAND_HEADS = frozenset(
    {
        "function", "execute", "schedule", "data", "scoreboard", "summon", "item", "give", "setblock",
        "playsound", "tellraw", "title", "dialog", "team", "bossbar", "tag", "advancement", "loot",
    }
)
# The audit's ``function_call_arguments`` requires literal SNBT values (a macro
# in an argument position is not something it may rewrite). The index must see
# exactly those macro-valued calls, so it parses the trailing argument block
# itself — quoted or not.
_CALL_RE = re.compile(r"\bfunction\s+([a-z0-9_\-:./]+)\s*\{([^{}]*)\}\s*$", re.IGNORECASE)
_ARGUMENT_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*:\s*([^,}]*)")
_FUNCTION_PATH_RE = re.compile(r"data/(?P<ns>[a-z0-9_.-]+)/functions?/(?P<path>.+)\.mcfunction$")
_IDENTIFIER_HINT_RE = re.compile(r"[A-Za-z0-9_.:/-]*:")
SELECTOR_HINT_RE = re.compile(r"^@[a-zA-Z]")
DEFAULT_MAX_DEPTH = 4
EVIDENCE_SNIPPET = 60


def function_id_from_path(rel_path: str) -> str | None:
    """``…/data/<ns>/function[s]/<path>.mcfunction`` -> ``<ns>:<path>``."""

    match = _FUNCTION_PATH_RE.search(rel_path.replace("\\", "/"))
    if match is None:
        return None
    return f"{match.group('ns')}:{match.group('path')}"


@dataclass(frozen=True)
class SinkOccurrence:
    """One observed use of a parameter, with the rule that classified it."""

    kind: str  # display | mechanism | forward | unknown
    detail: str
    file: str
    line: int
    snippet: str


@dataclass
class ParameterUsage:
    function: str
    parameter: str
    occurrences: list[SinkOccurrence] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        """``display`` only when *every* occurrence is a display sink."""

        if not self.occurrences:
            return "unused"
        kinds = {occurrence.kind for occurrence in self.occurrences}
        if "mechanism" in kinds:
            return "mechanism"
        if kinds == {"display"}:
            return "display"
        return "unknown"


@dataclass
class PurposeIndex:
    parameters: dict[tuple[str, str], ParameterUsage] = field(default_factory=dict)
    values: dict[str, list[SinkOccurrence]] = field(default_factory=dict)
    missing_functions: dict[str, int] = field(default_factory=dict)

    def verdict_for(self, function: str, parameter: str) -> str:
        usage = self.parameters.get((function, parameter))
        return usage.verdict if usage is not None else "unknown"


def function_bodies(root: Path) -> dict[str, list[tuple[str, int, str]]]:
    """Every command line of every ``.mcfunction`` in the package, keyed by id."""

    bodies: dict[str, list[tuple[str, int, str]]] = {}
    for path in sorted(root.rglob("*.mcfunction")):
        rel = path.relative_to(root).as_posix()
        function_id = function_id_from_path(rel)
        if function_id is None:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        lines: list[tuple[str, int, str]] = []
        for number, raw in enumerate(text.splitlines(), 1):
            command = raw[1:] if raw.startswith("$") else raw
            if command.strip():
                lines.append((rel, number, command))
        bodies.setdefault(function_id, []).extend(lines)
    return bodies


def classify_line(line: str, parameter: str) -> list[SinkOccurrence]:
    """Classify every occurrence of ``$(parameter)`` in one command line.

    Rules, in order (the first that matches wins, so a display payload can never
    hide an identifier use):

    1. the macro sits inside a value the extractor returns as display text;
    2. it sits in the command head, a selector, or a ``ns:path`` shaped token —
       an identifier, i.e. mechanism;
    3. the command itself is a display command (tellraw / title / bossbar name /
       dialog show) and the macro sits in its payload — display, which covers
       compound arguments such as ``body:$(body)`` that are not string leaves;
    4. the macro is handed on as another function's argument — forwarded, and
       resolved through the callee (see :func:`build_index`).
    """

    marker = f"$({parameter})"
    occurrences: list[SinkOccurrence] = []
    if marker not in line:
        return occurrences
    try:
        texts = [text for _locator, text in extract_command_texts(line)]
    except Exception:  # pragma: no cover - extractor guards its own parsing
        texts = []
    if any(marker in text for text in texts):
        return [
            SinkOccurrence(
                "display",
                "显示叶子（extract_command_texts 取出）",
                "",
                0,
                _snippet(line, line.find(marker)),
            )
        ]
    inner_start = _find_execute_run(line)
    inner = line[inner_start:] if inner_start is not None else line
    stripped = inner.lstrip()
    head = stripped.split(None, 1)[0] if stripped else ""
    token_start = 0
    for token in stripped.split(" "):
        token_end = token_start + len(token)
        if marker in token:
            if token_start < len(head) or token.startswith("@") or _IDENTIFIER_HINT_RE.match(token):
                return [
                    SinkOccurrence(
                        "mechanism",
                        f"标识符/目标位置（{head}）",
                        "",
                        0,
                        _snippet(line, line.find(marker)),
                    )
                ]
            if SELECTOR_HINT_RE.match(token):
                return [SinkOccurrence("mechanism", f"选择器（{head}）", "", 0, _snippet(line, line.find(marker)))]
        token_start = token_end + 1
    index_in_line = line.find(marker)
    quoted = _enclosing_string(line, index_in_line)
    key = _preceding_key(line, index_in_line)
    if quoted is not None and _looks_like_command(quoted):
        # Embedded command text (click events, dialog action templates): the
        # value names a function / carries arguments, whatever command wraps it.
        return [SinkOccurrence("mechanism", f"命令模板（{head}）", "", 0, _snippet(line, index_in_line))]
    if key is not None and key in MECHANISM_ARGUMENT_KEYS:
        return [SinkOccurrence("mechanism", f"机制键 `{key}`（{head}）", "", 0, _snippet(line, index_in_line))]
    if _display_command_head(line) and (quoted is not None or key in DISPLAY_ARGUMENT_KEYS):
        return [SinkOccurrence("display", f"显示载荷 {f'`{key}`' if key else '字符串'}（{head}）", "", 0, _snippet(line, index_in_line))]
    forwarded = [
        (function_id, argument)
        for (function_id, argument), value in call_arguments(line).items()
        if marker in value
    ]
    if forwarded:
        function_id, argument = forwarded[0]
        return [
            SinkOccurrence(
                "forward",
                f"转交 {function_id} 的 {argument}",
                "",
                0,
                _snippet(line, line.find(marker)),
            )
        ]
    return [SinkOccurrence("unknown", f"未分类位置（{head}）", "", 0, _snippet(line, line.find(marker)))]


def call_arguments(line: str) -> dict[tuple[str, str], str]:
    """``(function id, argument) -> decoded value`` for a trailing ``function … {…}`` call.

    Literal and macro-valued arguments both count: the index needs to follow
    ``function demo:writer {dialog:$(text)}`` to learn what ``text`` feeds.
    """

    return {key: value for key, _span, value in _call_argument_spans(line)}


def argument_at(line: str, offset: int) -> tuple[str, str] | None:
    """The ``(function id, argument)`` whose value span covers ``offset``.

    This is how a scanned unit is tied back to the call that feeds it: the
    extractor records the value's offsets, so containment identifies the
    parameter without threading the callee through the locator.
    """

    for (function_id, argument), (start, end), _value in _call_argument_spans(line):
        if start <= offset <= end:
            return function_id, argument
    return None


def _call_argument_spans(line: str) -> list[tuple[tuple[str, str], tuple[int, int], str]]:
    """Argument key, raw value span inside ``line``, and the decoded value."""

    match = _CALL_RE.search(line.strip())
    if match is None:
        return []
    lead = len(line) - len(line.lstrip())
    block_start = match.start(2)
    block = match.group(2)
    function_id = match.group(1)
    spans: list[tuple[tuple[str, str], tuple[int, int], str]] = []
    for argument in _ARGUMENT_RE.finditer(block):
        key = argument.group(1)
        value = argument.group(2).strip()
        start = block_start + argument.start(2) + (len(argument.group(2)) - len(argument.group(2).lstrip()))
        end = start + len(value)
        decoded = value
        if len(decoded) >= 2 and decoded[0] == decoded[-1] and decoded[0] in {"'", '"'}:
            decoded = decoded[1:-1].replace("\\'", "'").replace('\\"', '"')
        spans.append(((function_id, key), (start + lead, end + lead), decoded))
    return spans


def _display_command_head(line: str) -> bool:
    """Whether this command renders its payload to the player."""

    stripped = line.lstrip().lstrip("/")
    head = stripped.lower()
    if head.startswith("tellraw ") or head.startswith("dialog show "):
        return True
    return bool(TITLE_TEXT_COMMAND_RE.match(head) or BOSSBAR_NAME_COMMAND_RE.match(head))


def _enclosing_string(line: str, index: int) -> str | None:
    """The quoted literal containing ``index``, if the position is inside one."""

    quote: str | None = None
    start = 0
    position = 0
    while position < len(line) and position <= index:
        char = line[position]
        if quote is None:
            if char in {'"', "'"}:
                quote = char
                start = position + 1
        elif char == "\\":
            position += 1
        elif char == quote:
            if position >= index:
                return line[start:position]
            quote = None
        position += 1
    if quote is not None and index >= start:
        return line[start:]
    return None


def _preceding_key(line: str, index: int) -> str | None:
    """The compound key whose value ``index`` sits in, e.g. ``text`` / ``template``."""

    prefix = line[:index].rstrip().rstrip("'\"")
    colon = prefix.rfind(":")
    if colon <= 0:
        return None
    match = re.search(r"([A-Za-z_][A-Za-z0-9_]*)\s*$", prefix[:colon])
    return match.group(1) if match else None


def _looks_like_command(text: str) -> bool:
    """Whether a quoted literal is a command (so its macros are arguments)."""

    stripped = text.strip().lstrip("/").lower()
    head = stripped.split(None, 1)[0] if stripped else ""
    if head in COMMAND_HEADS:
        return True
    return bool(re.match(r"^(?:execute|function|data|scoreboard|tellraw|title|summon|item|give|setblock|playsound)\b", stripped))


def _snippet(line: str, index: int, width: int = EVIDENCE_SNIPPET) -> str:
    start = max(0, index - width // 2)
    return line[start : start + width]


def build_index(root: Path, *, max_depth: int = DEFAULT_MAX_DEPTH) -> PurposeIndex:
    """Build the purpose index for one extracted package (read-only)."""

    bodies = function_bodies(root)
    index = PurposeIndex()
    for function_id, lines in bodies.items():
        for rel, number, command in lines:
            for parameter in dict.fromkeys(MACRO_RE.findall(command)):
                for occurrence in classify_line(command, parameter):
                    located = SinkOccurrence(
                        occurrence.kind,
                        occurrence.detail,
                        rel,
                        number,
                        occurrence.snippet,
                    )
                    index.parameters.setdefault((function_id, parameter), ParameterUsage(function_id, parameter)).occurrences.append(
                        located
                    )
    # Forwarded parameters are resolved through the callee's own usage, bounded
    # so a cyclic chain cannot spin: a chain that never reaches a display sink
    # stays unknown, which is the protecting verdict.
    for usage in index.parameters.values():
        resolved: list[SinkOccurrence] = []
        for occurrence in usage.occurrences:
            if occurrence.kind != "forward":
                resolved.append(occurrence)
                continue
            mode = occurrence.detail.removeprefix("转交 ")
            callee, _, argument = mode.partition(" 的 ")
            kind, detail = _resolve_forward(index, bodies, callee, argument, depth=max_depth, seen={(usage.function, usage.parameter)})
            resolved.append(SinkOccurrence(kind, f"{occurrence.detail} → {detail}", occurrence.file, occurrence.line, occurrence.snippet))
        usage.occurrences = resolved
    for function_id, lines in bodies.items():
        for rel, number, command in lines:
            for (callee, argument), value in call_arguments(command).items():
                if callee not in bodies:
                    index.missing_functions[callee] = index.missing_functions.get(callee, 0) + 1
                    kind, detail = "unknown", f"{callee} 的函数体缺失"
                else:
                    usage = index.parameters.setdefault((callee, argument), ParameterUsage(callee, argument))
                    kind, detail = usage.verdict, f"{callee} 的 {argument}"
                index.values.setdefault(value, []).append(SinkOccurrence(kind, detail, rel, number, _snippet(command, command.find("{"))))
    return index


def _resolve_forward(
    index: PurposeIndex,
    bodies: dict[str, list[tuple[str, int, str]]],
    callee: str,
    argument: str,
    *,
    depth: int,
    seen: set[tuple[str, str]],
) -> tuple[str, str]:
    if depth <= 0 or (callee, argument) in seen or callee not in bodies:
        return "unknown", "递归上限或函数体缺失"
    seen = seen | {(callee, argument)}
    usage = index.parameters.get((callee, argument))
    if usage is None:
        return "unknown", f"{callee} 不使用 {argument}"
    kinds = set()
    for occurrence in usage.occurrences:
        if occurrence.kind != "forward":
            kinds.add(occurrence.kind)
            continue
        nested_callee, _, nested_argument = occurrence.detail.removeprefix("转交 ").partition(" 的 ")
        nested_kind, _detail = _resolve_forward(index, bodies, nested_callee, nested_argument, depth=depth - 1, seen=seen)
        kinds.add(nested_kind)
    if "mechanism" in kinds:
        return "mechanism", f"{callee} 的 {argument}"
    if kinds == {"display"}:
        return "display", f"{callee} 的 {argument}"
    return "unknown", f"{callee} 的 {argument}"


ALLOWLIST_ASSET = Path("assets") / "display-argument-allowlist.json"
ALLOWLIST_ENV = "MC_HANHUA_DISPLAY_ARGUMENT_ALLOWLIST"


def bundled_allowlist_path() -> Path:
    """Where the reviewed display-argument table lives (frozen builds included)."""

    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2])) / ALLOWLIST_ASSET


@dataclass(frozen=True)
class DisplayArgumentAllowlist:
    """Reviewed exceptions that promote an unproven argument to display.

    Only ``unknown`` / ``unused`` verdicts can be promoted: the index proves
    mechanism positions from the package itself, and a table entry must never
    override that proof. Entries carry the package fingerprint, so an edited or
    repacked map expires them instead of inheriting the decision.
    """

    entries: frozenset[tuple[str, str, str]]  # (fingerprint, function, argument)

    def allows(self, fingerprint: str, function_id: str, argument: str) -> bool:
        if not fingerprint:
            return False
        return (fingerprint, function_id, argument) in self.entries


def load_display_argument_allowlist(path: Path | str | None = None) -> DisplayArgumentAllowlist:
    """A missing or broken source grants nothing (fail-closed)."""

    override = str(path) if path is not None else (os.getenv(ALLOWLIST_ENV) or "")
    candidates = [Path(override)] if override else [bundled_allowlist_path()]
    for candidate in candidates:
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        entries = payload.get("entries") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            continue
        collected = set()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            fingerprint = str(entry.get("package_fingerprint") or "")
            function_id = str(entry.get("function") or "")
            argument = str(entry.get("argument") or "")
            if fingerprint and function_id and argument:
                collected.add((fingerprint, function_id, argument))
        return DisplayArgumentAllowlist(frozenset(collected))
    return DisplayArgumentAllowlist(frozenset())


@dataclass(frozen=True)
class ArgumentUse:
    """What a scanned unit's value feeds, and whether it may be translated."""

    function: str
    argument: str
    verdict: str
    source: str  # "index" | "allowlist" | "index+allowlist"

    @property
    def translatable(self) -> bool:
        return self.verdict == "display"


def argument_for_unit(
    unit: TextUnit,
    line: str,
    index: PurposeIndex,
    *,
    allowlist: DisplayArgumentAllowlist | None = None,
    fingerprint: str = "",
) -> ArgumentUse | None:
    """Tie a scanned unit back to the ``function …{k:"v"}`` argument that feeds it.

    Only the extracted value's own offsets are used, so a unit inside a payload
    that a command merely prints is never mistaken for a call argument.
    """

    try:
        locator = json.loads(unit.locator)
    except (TypeError, ValueError):
        return None
    command = locator.get("command") if isinstance(locator, dict) else None
    if not isinstance(command, dict) or command.get("kind") not in {"snbt", "plain"}:
        return None
    try:
        offset = int(command["start"])
    except (KeyError, TypeError, ValueError):
        return None
    found = argument_at(line, offset)
    if found is None:
        return None
    function_id, argument = found
    verdict = index.verdict_for(function_id, argument)
    if verdict == "display":
        return ArgumentUse(function_id, argument, verdict, "index")
    # A reviewed entry may rescue what the index cannot prove. It must never
    # override a proven mechanism verdict: the package itself showed that the
    # value ends up in an identifier, selector or command position.
    if verdict != "mechanism" and allowlist is not None and allowlist.allows(fingerprint, function_id, argument):
        return ArgumentUse(function_id, argument, "display", "allowlist")
    return ArgumentUse(function_id, argument, verdict, "index")


def render_markdown(index: PurposeIndex, *, evidence: int = 2, limit: int = 200) -> str:
    """Human-reviewable report: verdicts plus the evidence line for each."""

    lines = ["# 宏参数用途索引（只读）", ""]
    lines.append(f"- 参数条目：{len(index.parameters)}；取值条目：{len(index.values)}")
    counts: dict[str, int] = {}
    for usage in index.parameters.values():
        counts[usage.verdict] = counts.get(usage.verdict, 0) + 1
    lines.append("- 判定分布：" + "、".join(f"{kind} {count}" for kind, count in sorted(counts.items())))
    if index.missing_functions:
        lines.append("- 函数体缺失：" + "、".join(f"{name}({count})" for name, count in sorted(index.missing_functions.items())[:10]))
    lines.append("")
    lines.append("## 参数表 `(function_id, param) → sinks`")
    lines.append("")
    lines.append("| 函数 | 参数 | 判定 | 证据 |")
    lines.append("| --- | --- | --- | --- |")
    for (function_id, parameter), usage in sorted(index.parameters.items(), key=lambda item: (item[1].verdict, item[0]))[:limit]:
        evidence_cells = []
        for occurrence in usage.occurrences[:evidence]:
            evidence_cells.append(f"`{occurrence.file}:{occurrence.line}` {occurrence.detail}：`{occurrence.snippet}`")
        lines.append(f"| `{function_id}` | `{parameter}` | **{usage.verdict}** | {'<br>'.join(evidence_cells)} |")
    lines.append("")
    lines.append("## 取值表 `value → sinks`")
    lines.append("")
    lines.append("| 取值 | 判定 | 调用点 |")
    lines.append("| --- | --- | --- |")
    for value, occurrences in sorted(index.values.items(), key=lambda item: item[0])[:limit]:
        kinds = {occurrence.kind for occurrence in occurrences}
        verdict = "mechanism" if "mechanism" in kinds else ("display" if kinds == {"display"} else "unknown")
        sites = "<br>".join(f"`{occurrence.file}:{occurrence.line}` {occurrence.detail}" for occurrence in occurrences[:evidence])
        lines.append(f"| `{value}` | **{verdict}** | {sites} |")
    return "\n".join(lines) + "\n"
