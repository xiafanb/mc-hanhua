from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import zipfile
from collections import Counter, defaultdict
from collections.abc import Callable
from pathlib import Path
from threading import Event

from .archives import ARCHIVE_SUFFIXES
from .audit import AuditBlockedError, audit_output, take_baseline
from .diagnostics import attach_diagnostic_path, collect_input_diagnostics, write_failure_log
from .diffdiag import compare_nbt_text, diagnose_structure_conflicts
from .events import (
    EventSink,
    emit_event,
    filter_detail_limit,
    output_event_log_path,
    reset_current_sink,
    set_current_sink,
    user_logs_disabled,
)
from .extractors import GenericJsonTextPlugin, JsonLangPlugin, LegacyLangPlugin, McfunctionPlugin, NbtTextPlugin
from .glossary import Glossary
from .memory import TranslationMemory
from .mod_assets import ModAssetLibrary, default_mod_assets_root, sha256_file
from .models import ArchiveResult, ModCoverageEntry, ProgressEvent, RiskLevel, ScanResult, TextUnit, TranslationReport
from .official_terms import OfficialTermIndex
from .plugins import PluginRegistry
from .policy import POLICY_IMMUTABLE_ID, POLICY_IMMUTABLE_MATCH, POLICY_TRANSLATABLE, POLICY_UNKNOWN, apply_unit_policy, should_send_to_ai
from .quality import (
    build_translation_groups,
    estimate_group_requests,
    is_multiline_layout_unit,
    quality_memory_context,
    unit_outcome_status,
    validate_contextual_target,
)
from .reporting import write_html_report, write_json_report
from .task_records import archive_fingerprint, atomic_write_json
from .translator import STOPPED_MESSAGE, TranslationPaused, TranslatorProvider, translator_from_env
from .utils import looks_like_chinese_translation, normalize_layout_newlines

ProgressCallback = Callable[[ProgressEvent], None]
SeedTranslations = dict[tuple[str, str, str, str], str]


def _merge_counts(target: dict[str, int], source: dict[str, int]) -> None:
    for reason, count in source.items():
        target[reason] = target.get(reason, 0) + count


def _emit(
    callback: ProgressCallback | None,
    stage: str,
    message: str,
    percent: int,
    **stats: object,
) -> None:
    if callback:
        callback(ProgressEvent(stage=stage, message=message, percent=max(0, min(100, percent)), **stats))


def _safe_archive_id(archive_rel: str) -> str:
    sanitized = archive_rel.replace("\\", "/").replace("!/", "__").replace("/", "__").replace(":", "_")
    # The plain substitution collides (a/b.zip vs a__b.zip, A03); a chain hash
    # keeps every archive's staging directory distinct.
    digest = hashlib.sha256(archive_rel.encode("utf-8")).hexdigest()[:12]
    return f"{sanitized}-{digest}"


def _prefix_units(units: list[TextUnit], archive_rel: str) -> None:
    prefix = f"{archive_rel}!/"
    for unit in units:
        if not unit.file_path.startswith(prefix):
            unit.file_path = prefix + unit.file_path
            unit.metadata["archive_id"] = archive_rel
            unit.metadata["archive_chain"] = [archive_rel]


def _prefix_filtered_details(details: list[dict[str, object]], archive_rel: str) -> list[dict[str, object]]:
    prefix = f"{archive_rel}!/"
    prefixed: list[dict[str, object]] = []
    for item in details:
        copied = dict(item)
        file_name = str(copied.get("file") or "")
        if file_name and not file_name.startswith(prefix):
            copied["file"] = prefix + file_name
        copied["archive_id"] = archive_rel
        prefixed.append(copied)
    return prefixed


def _persist_archive_workspace(
    task_root: Path | None,
    archive_rel: str,
    archive_root: Path,
    scan_result: ScanResult,
    audit: object | None,
    *,
    status: str,
    publication_status: str,
    translated: int,
    reused: int,
    skipped: int,
    original_fingerprint: str = "",
    error_code: str = "",
    phase: str = "",
    groups_completed: int = 0,
) -> ArchiveResult:
    archive_id = archive_rel or "(root)"
    staging_path = ""
    report_path = ""
    if task_root is not None:
        dest = task_root / "archives" / _safe_archive_id(archive_id)
        dest.mkdir(parents=True, exist_ok=True)
        staging = dest / "output"
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        shutil.copytree(archive_root, staging)
        staging_path = str(staging)
        issues = []
        if audit is not None and hasattr(audit, "to_dict"):
            payload = audit.to_dict()
            atomic_write_json(dest / "audit.json", payload)
            issues = list(payload.get("errors") or []) + list(payload.get("warnings") or [])
        else:
            payload = {}
            issues = []
        result = ArchiveResult(
            archive_id=archive_id,
            archive_chain=[archive_id] if archive_id != "(root)" else [],
            status=status,
            scanned=len(scan_result.text_units),
            groups_completed=groups_completed,
            candidate_count=sum(1 for unit in scan_result.text_units if unit.final_target not in (None, unit.source_text)),
            unit_ids=[unit.id for unit in scan_result.text_units],
            filtered_details=list(scan_result.filtered_details),
            warnings=list(scan_result.warnings),
            audit_issues=issues,
            staging_path=staging_path,
            publication_status=publication_status,
            translated=translated,
            reused=reused,
            skipped=skipped,
            original_fingerprint=original_fingerprint,
            error_code=error_code,
            phase=phase,
        )
        report_path = str(dest / "report.json")
        result.report_path = report_path
        atomic_write_json(dest / "report.json", result.to_dict())
        atomic_write_json(
            dest / "staging-manifest.json",
            {
                "archive_id": archive_id,
                "original_fingerprint": original_fingerprint,
                "publication_status": publication_status,
                "unit_count": len(scan_result.text_units),
            },
        )
        return result
    issues = []
    if audit is not None and hasattr(audit, "to_dict"):
        payload = audit.to_dict()
        issues = list(payload.get("errors") or []) + list(payload.get("warnings") or [])
    return ArchiveResult(
        archive_id=archive_id,
        archive_chain=[archive_id] if archive_id != "(root)" else [],
        status=status,
        scanned=len(scan_result.text_units),
        groups_completed=groups_completed,
        candidate_count=sum(1 for unit in scan_result.text_units if unit.final_target not in (None, unit.source_text)),
        unit_ids=[unit.id for unit in scan_result.text_units],
        filtered_details=list(scan_result.filtered_details),
        warnings=list(scan_result.warnings),
        audit_issues=issues,
        staging_path=staging_path,
        report_path=report_path,
        publication_status=publication_status,
        translated=translated,
        reused=reused,
        skipped=skipped,
        original_fingerprint=original_fingerprint,
        error_code=error_code,
        phase=phase,
    )


def reject_output_inside_input(input_path: Path, output_path: Path) -> None:
    """Reject an output location that lives inside a directory input (A04).

    The workspace is created next to the output; an output inside the input
    tree would fold the workspace back into the copied input, growing it on
    every run. Symlinks/junctions are resolved first.
    """

    if not input_path.is_dir():
        return
    try:
        input_resolved = input_path.resolve()
        output_resolved = output_path.resolve()
    except OSError:
        return
    try:
        output_resolved.relative_to(input_resolved)
    except ValueError:
        return
    raise ValueError("输出路径不能位于输入目录内部；请选择输入目录之外的位置。")


def _preflight_output(input_path: Path, output_path: Path) -> None:
    if input_path.resolve() == output_path.resolve():
        raise ValueError("输出路径不能与输入路径相同")
    reject_output_inside_input(input_path, output_path)
    if output_path.exists():
        raise FileExistsError(f"输出路径已存在：{output_path}")

    parent = output_path.parent
    parent.mkdir(parents=True, exist_ok=True)
    if not parent.is_dir():
        raise NotADirectoryError(f"输出目录不可用：{parent}")

    temp_name = ""
    try:
        handle, temp_name = tempfile.mkstemp(prefix=".mc-hanhua-write-", dir=parent)
        os.close(handle)
    except OSError as exc:
        raise PermissionError(f"输出目录不可写：{parent} ({exc})") from exc
    finally:
        if temp_name:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass


def default_registry() -> PluginRegistry:
    registry = PluginRegistry()
    json_lang = JsonLangPlugin()
    legacy_lang = LegacyLangPlugin()
    generic_json = GenericJsonTextPlugin()
    mcfunction = McfunctionPlugin()
    nbt_text = NbtTextPlugin()
    for plugin in (json_lang, legacy_lang, generic_json, mcfunction, nbt_text):
        registry.add_extractor(plugin)
    for plugin in (json_lang, legacy_lang, generic_json, mcfunction, nbt_text):
        registry.add_writer(plugin)
    return registry


def iter_files(root: Path) -> list[Path]:
    return [path for path in root.rglob("*") if path.is_file()]


def iter_dirs(root: Path) -> list[Path]:
    return [path for path in root.rglob("*") if path.is_dir()]


def _check_cancel(cancel_event: Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise TranslationPaused(STOPPED_MESSAGE)


def scan(root: Path, registry: PluginRegistry | None = None, cancel_event: Event | None = None, *, allow_new_names: bool = True) -> ScanResult:
    registry = registry or default_registry()
    result = ScanResult(root=root)
    selector_refs: set[str] = set()
    player_names: set[str] = set()
    custom_names: set[str] = set()
    mechanism_refs: set[str] = set()
    for path in iter_files(root):
        _check_cancel(cancel_event)
        plugin = registry.extractor_for(path)
        if not plugin:
            continue
        units = plugin.extract(root, path)
        result.text_units.extend(units)
        # extract() resets last_* per file, so accumulate here.
        selector_refs.update(getattr(plugin, "last_selector_refs", set()))
        player_names.update(getattr(plugin, "last_player_names", set()))
        custom_names.update(getattr(plugin, "last_custom_names", set()))
        mechanism_refs.update(getattr(plugin, "last_mechanism_refs", set()))
        result.commands.extend(getattr(plugin, "last_commands", []))
        result.warnings.extend(getattr(plugin, "last_warnings", []))
        per_file = getattr(plugin, "last_filtered_counts", {})
        if per_file:
            result.file_filtered[path.relative_to(root).as_posix()] = dict(per_file)
        for reason, count in per_file.items():
            result.filtered_counts[reason] = result.filtered_counts.get(reason, 0) + count
        details = getattr(plugin, "last_filtered_details", [])
        if details:
            remaining = filter_detail_limit() - len(result.filtered_details)
            if remaining > 0:
                result.filtered_details.extend(details[:remaining])
    result.mechanism_refs = mechanism_refs
    for unit in result.text_units:
        apply_unit_policy(unit)
    identity_hits = _identity_hits(result, selector_refs, player_names, custom_names)
    from .references import apply_index_to_units, build_reference_index

    if not allow_new_names:
        result.filtered_counts["reference_coverage_incomplete"] = 1
    nested_selector_refs, nested_mechanism_refs = _collect_nested_archive_refs(root, result)
    selector_refs.update(nested_selector_refs)
    mechanism_refs.update(nested_mechanism_refs)
    result.mechanism_refs.update(nested_mechanism_refs)
    index = build_reference_index(result, identity_hits)
    apply_index_to_units(result, index)
    _lock_selector_referenced_names(result, selector_refs, custom_names)
    _lock_player_name_texts(result, player_names)
    _lock_mechanism_referenced_texts(result, mechanism_refs, index)
    _finalize_entity_display_names(result, selector_refs, mechanism_refs, index)
    apply_index_to_units(result, index)
    _sync_immutable_mechanism_refs(result)
    return result


def _sync_immutable_mechanism_refs(result: ScanResult) -> None:
    """Keep consumer match values in mechanism_refs; do not add display Command text.

    Extractor collect_mechanism_refs already records clear / if-items / CustomName
    consumers. Command-block give/summon book pages are display text and must not
    enter the match set, or a later translation would false-fail the audit.
    """

    return


def _identity_hits(
    result: ScanResult,
    selector_refs: set[str],
    player_names: set[str],
    custom_names: set[str],
) -> list[dict[str, object]]:
    """Turn extractor identity sets into typed index hits."""

    hits: list[dict[str, object]] = []
    # Command selectors are already parsed into the index. Extractor
    # last_selector_refs also contains mechanism match values, so dumping
    # the mixed set here would mis-label lore as selector_name. Natural-language
    # CustomName values are display candidates, not automatic match definitions.
    _ = selector_refs
    _ = custom_names
    for name in player_names:
        hits.append(
            {
                "value": name,
                "kind": "match_value",
                "detail": "player_name",
                "object_key": f"match:player_name:{name}",
                "role": "definition",
                "policy": POLICY_IMMUTABLE_MATCH,
            }
        )
    for unit in result.text_units:
        if unit.policy == POLICY_IMMUTABLE_MATCH and unit.source_text:
            hits.append(
                {
                    "value": unit.source_text,
                    "kind": "match_value",
                    "detail": "extracted_match",
                    "object_key": unit.object_key or f"match:unit:{unit.source_text}",
                    "role": "definition",
                    "file": unit.file_path,
                    "locator": unit.locator,
                    "context": unit.context,
                    "unit_id": unit.id,
                    "policy": unit.policy,
                }
            )
    return hits


def _lock_mechanism_referenced_texts(result: ScanResult, refs: set[str], index=None) -> None:
    """Keep values consumed by match clauses untranslated everywhere.

    clear / execute if|unless items|data predicates match exact strings; the
    same normalized value in a producer (give command, NBT item data, item
    definition JSON) is locked so the pair cannot drift apart. Unreferenced
    display lore still translates normally. reference_sources records the
    concrete consuming file/locator, not a generic 'selector' label.
    """

    if not refs:
        return
    kept: list[TextUnit] = []
    locked = 0
    for unit in result.text_units:
        if unit.source_text in refs:
            locked += 1
            unit.policy = POLICY_IMMUTABLE_MATCH
            unit.policy_reason = "value consumed by a match clause"
            sources = _sources_for_value(index, unit.source_text, fallback="mechanism_ref")
            unit.reference_sources = sources
            if index is not None:
                keys = [key for key in index.object_keys_for_value(unit.source_text) if key in index.by_object]
                match_keys = [key for key in keys if index.by_object[key].kind == "match_value"]
                if len(match_keys) == 1:
                    unit.object_key = match_keys[0]
            _append_lock_detail(result, unit, "mechanism_locked", "value consumed by a match clause")
            if _is_custom_name_unit(unit):
                kept.append(unit)
            continue
        kept.append(unit)
    if locked:
        result.text_units = kept
        result.filtered_counts["mechanism_locked"] = result.filtered_counts.get("mechanism_locked", 0) + locked


def _lock_selector_referenced_names(result: ScanResult, refs: set[str], custom_names: set[str]) -> None:
    """Keep entity names referenced by @e[name=...] selectors untranslated.

    Selector arguments are never extracted from commands, so a translated
    CustomName would silently break kill/tp/execute matching. Plain CustomName
    values are preserved at extraction already; this also defends future
    extraction paths and reports every preserved name that a selector or NBT
    condition references as ``reference_locked``.
    """

    if not refs:
        return
    locked = 0
    for unit in result.text_units:
        if unit.format == "nbt-text" and unit.source_text in refs and _is_custom_name_unit(unit):
            locked += 1
            unit.policy = POLICY_IMMUTABLE_MATCH
            unit.policy_reason = "selector or NBT condition references this name"
            unit.reference_sources = _sources_for_value(result.reference_index, unit.source_text, fallback="selector_name")
            unit.object_key = unit.object_key or f"match:selector_name:{unit.source_text}"
            _append_lock_detail(result, unit, "reference_locked", "selector or NBT condition references this name")
    if locked:
        result.filtered_counts["reference_locked"] = result.filtered_counts.get("reference_locked", 0) + locked
    leftover = refs & custom_names
    leftover -= {unit.source_text for unit in result.text_units if _is_custom_name_unit(unit)}
    if leftover:
        result.filtered_counts["reference_locked"] = result.filtered_counts.get("reference_locked", 0) + len(leftover)


def _lock_player_name_texts(result: ScanResult, names: set[str]) -> None:
    """Sign lines and dialog fragments that exactly repeat a filtered
    player-shaped entity name are name plates, not prose."""

    if not names:
        return
    kept: list[TextUnit] = []
    locked = 0
    for unit in result.text_units:
        if unit.source_text.strip() in names:
            locked += 1
            unit.policy = POLICY_IMMUTABLE_MATCH
            unit.policy_reason = "matches a filtered player-shaped entity name"
            unit.reference_sources = _sources_for_value(result.reference_index, unit.source_text, fallback="player_name")
            unit.object_key = unit.object_key or f"match:player_name:{unit.source_text}"
            _append_lock_detail(result, unit, "player_name", "matches a filtered player-shaped entity name")
            continue
        kept.append(unit)
    if locked:
        result.text_units = kept
        result.filtered_counts["player_name"] = result.filtered_counts.get("player_name", 0) + locked


def _sources_for_value(index, value: str, *, fallback: str) -> list[str]:
    if index is None:
        return [fallback]
    labels: list[str] = []
    for key in index.object_keys_for_value(value):
        entry = index.by_object.get(key)
        if entry is None:
            continue
        for occ in (*entry.uses, *entry.definitions):
            label = occ.source_label()
            if label not in labels:
                labels.append(label)
    return labels[:20] or [fallback]


def _append_lock_detail(result: ScanResult, unit: TextUnit, reason: str, extra_reason: str) -> None:
    from .events import filter_detail_limit, redact_text

    if len(result.filtered_details) < filter_detail_limit():
        result.filtered_details.append(
            {
                "reason": reason,
                "file": unit.file_path,
                "format": unit.format,
                "locator": redact_text(unit.locator),
                "source": redact_text(unit.source_text),
                "policy": unit.policy,
                "policy_reason": extra_reason,
                "reference_sources": list(unit.reference_sources),
                "object_key": unit.object_key,
            }
        )
    emit_event(
        stage="filter",
        file=unit.file_path,
        format=unit.format,
        locator=unit.locator,
        context=unit.context,
        unit_id=unit.id,
        source=unit.source_text,
        policy=unit.policy,
        policy_reason=extra_reason,
        extra={"reason": reason, "reference_sources": list(unit.reference_sources), "object_key": unit.object_key},
    )
    if result.filtered_details:
        result.filtered_details[-1]["reference_sources"] = list(unit.reference_sources)
        result.filtered_details[-1]["object_key"] = unit.object_key


def _is_custom_name_unit(unit: TextUnit) -> bool:
    if unit.metadata.get("display_kind") == "entity_display_name":
        return True
    try:
        locator = json.loads(unit.locator)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(locator, dict):
        return False
    path = locator.get("path")
    if not isinstance(path, list) or not path:
        return False
    return str(path[-1]).lower() == "customname"


def _reference_coverage_complete(result: ScanResult) -> bool:
    if result.filtered_counts.get("unparsed_mechanism") or result.filtered_counts.get("nbt_parse_error") or result.filtered_counts.get("reference_coverage_incomplete"):
        return False
    return result.reference_index is not None


def _collect_nested_archive_refs(root: Path, result: ScanResult | None = None) -> tuple[set[str], set[str]]:
    """Harvest selector / match refs from nested zip/jar before unlocking names."""

    from .text_components import collect_mechanism_refs, collect_selector_refs

    selector_refs: set[str] = set()
    mechanism_refs: set[str] = set()
    def incomplete() -> None:
        if result is not None:
            result.filtered_counts["reference_coverage_incomplete"] = 1

    for path in iter_files(root):
        if path.suffix.lower() not in {".jar", ".zip"} or not zipfile.is_zipfile(path):
            continue
        try:
            with zipfile.ZipFile(path) as zf:
                for info in zf.infolist():
                    name = info.filename.replace("\\", "/").lower()
                    if info.is_dir():
                        continue
                    if name.endswith((".dat", ".nbt", ".mca", ".zip", ".jar", ".mrpack")):
                        incomplete()
                    if info.file_size > 1_000_000:
                        incomplete()
                        continue
                    if not name.endswith((".mcfunction", ".dat", ".nbt")):
                        continue
                    try:
                        payload = zf.read(info)
                    except (OSError, KeyError, RuntimeError):
                        incomplete()
                        continue
                    if name.endswith(".mcfunction"):
                        try:
                            text = payload.decode("utf-8")
                        except UnicodeDecodeError:
                            incomplete()
                            continue
                        for line in text.splitlines():
                            from .references import _command_unparsed
                            if _command_unparsed(line):
                                incomplete()
                            selector_refs.update(collect_selector_refs(line))
                            mechanism_refs.update(collect_mechanism_refs(line))
        except (OSError, zipfile.BadZipFile):
            incomplete()
            continue
    return selector_refs, mechanism_refs


def _finalize_entity_display_names(
    result: ScanResult,
    selector_refs: set[str],
    mechanism_refs: set[str],
    index=None,
) -> None:
    """Promote unreferenced natural-language entity names after a full scan."""

    consumed = set(selector_refs) | set(mechanism_refs) | set(result.mechanism_refs)
    if index is not None:
        consumed.update(getattr(index, "locked_values", set()) or [])
    coverage_ok = _reference_coverage_complete(result)
    kept: list[TextUnit] = []
    locked = 0
    unknown = 0
    display = 0
    for unit in result.text_units:
        if not _is_custom_name_unit(unit):
            kept.append(unit)
            continue
        if unit.policy == POLICY_IMMUTABLE_MATCH or unit.source_text in consumed:
            locked += 1
            unit.policy = POLICY_IMMUTABLE_MATCH
            unit.policy_reason = unit.policy_reason or "selector or NBT condition references this name"
            unit.reference_sources = unit.reference_sources or _sources_for_value(index, unit.source_text, fallback="selector_name")
            unit.object_key = unit.object_key or f"match:selector_name:{unit.source_text}"
            _append_lock_detail(result, unit, "reference_locked", unit.policy_reason)
            continue
        if not coverage_ok:
            unknown += 1
            unit.policy = POLICY_UNKNOWN
            unit.policy_reason = "reference coverage incomplete; entity display name kept for review"
            _append_lock_detail(result, unit, "unknown_identity", unit.policy_reason)
            continue
        unit.policy = POLICY_TRANSLATABLE
        unit.policy_reason = "unreferenced natural-language entity display name"
        unit.metadata["policy"] = unit.policy
        unit.metadata["policy_reason"] = unit.policy_reason
        display += 1
        kept.append(unit)
    result.text_units = kept
    if locked:
        result.filtered_counts["entity_name_preserved"] = result.filtered_counts.get("entity_name_preserved", 0) + locked
    if unknown:
        result.filtered_counts["unknown_identity"] = result.filtered_counts.get("unknown_identity", 0) + unknown
    if display:
        result.filtered_counts["entity_display_name"] = result.filtered_counts.get("entity_display_name", 0) + display


def copy_input(input_path: Path, output_path: Path, cancel_event: Event | None = None) -> Path:
    if output_path.exists():
        raise FileExistsError(f"Output path already exists: {output_path}")
    if input_path.is_dir():

        def _copy_file(src: str, dst: str) -> None:
            _check_cancel(cancel_event)
            shutil.copy2(src, dst)

        shutil.copytree(input_path, output_path, copy_function=_copy_file)
        return output_path
    if input_path.suffix.lower() in ARCHIVE_SUFFIXES:
        output_path.mkdir(parents=True, exist_ok=False)
        with zipfile.ZipFile(input_path) as zf:
            for member in zf.infolist():
                _check_cancel(cancel_event)
                zf.extract(member, output_path)
        return output_path
    raise ValueError(f"Unsupported input path: {input_path}")


def _is_tool_metadata_path(source_dir: Path, path: Path) -> bool:
    try:
        rel = path.relative_to(source_dir)
    except ValueError:
        return False
    return bool(rel.parts) and rel.parts[0] == ".mc-hanhua"


def _is_publish_residue_path(source_dir: Path, path: Path) -> bool:
    try:
        rel = path.relative_to(source_dir)
    except ValueError:
        return False
    return path.name == "session.lock" or path.suffix.lower() == ".backup" or any(part == ".mc-hanhua" for part in rel.parts)


def repack_directory(source_dir: Path, archive_path: Path, exclude_tool_metadata: bool = False, cancel_event: Event | None = None) -> None:
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in iter_dirs(source_dir):
            if exclude_tool_metadata and _is_publish_residue_path(source_dir, path):
                continue
            rel = path.relative_to(source_dir).as_posix().rstrip("/") + "/"
            zf.write(path, rel)
        for path in iter_files(source_dir):
            _check_cancel(cancel_event)
            if exclude_tool_metadata and _is_publish_residue_path(source_dir, path):
                continue
            zf.write(path, path.relative_to(source_dir).as_posix())


def load_existing_zh_cn_glossary(root: Path) -> Glossary:
    translations: dict[str, dict[str, str]] = {}
    for path in root.rglob("*.json"):
        if "assets" not in path.parts:
            continue
        if path.parent.name != "lang":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and path.stem in {"en_us", "zh_cn"}:
            pack_key = path.parent.parent.as_posix()
            translations.setdefault(pack_key, {})
            for key, value in data.items():
                if isinstance(value, str):
                    translations[pack_key][f"{path.stem}:{key}"] = value
    from .models import GlossaryEntry

    entries = []
    for values in translations.values():
        keys = {item.split(":", 1)[1] for item in values}
        for key in keys:
            source = values.get(f"en_us:{key}")
            target = values.get(f"zh_cn:{key}")
            if source and target:
                entries.append(GlossaryEntry(source=source, target=target, priority=30))
    return Glossary(entries)


def _seed_key(file_path: str, format_name: str, locator: str, source_text: str) -> tuple[str, str, str, str]:
    return file_path, format_name, locator, source_text


def _book_locator_alias_context(unit: TextUnit, memory_context: str) -> str | None:
    """Map extra/N/\"\" book contexts onto extra/N/text for a narrow memory hit."""

    if unit.format != "nbt-text" or unit.policy != POLICY_TRANSLATABLE:
        return None
    haystack = f"{unit.locator} {unit.context}".lower()
    if not any(marker in haystack for marker in ("written_book", "writable_book", "/pages", "pages/")):
        return None
    aliased = memory_context
    if aliased.endswith("|"):
        return f"{aliased}text"
    if aliased.endswith("/"):
        return f"{aliased}text"
    return None


def _lookup_book_locator_alias(memory: TranslationMemory, unit: TextUnit, memory_context: str) -> str | None:
    alias = _book_locator_alias_context(unit, memory_context)
    if not alias or alias == memory_context:
        return None
    return memory.get(unit.source_text, alias)


def _normalize_layout_target(unit: TextUnit, target: str) -> str:
    """Repair escaped breaks only for proven multiline display containers."""

    if is_multiline_layout_unit(unit):
        return normalize_layout_newlines(unit.source_text, target)
    return target


def _validated_candidate(
    unit: TextUnit,
    candidate: object,
    *,
    origin: str,
    official_terms: OfficialTermIndex | None = None,
) -> str | None:
    """Normalize and validate every reusable/model candidate at one gate."""

    if not isinstance(candidate, str):
        reason = "invalid_candidate_type"
        normalized = None
    else:
        from .quality import is_book_page_unit

        if is_book_page_unit(unit) and not re.match(r"^\s*\[page=", unit.source_text):
            # Prompt page labels are context, never part of the displayed page.
            candidate = re.sub(r"^\s*\[page=(?:\d+|未知)\][ \t]*", "", candidate)
        normalized = _normalize_layout_target(unit, candidate)
        # A proven TextDisplay renders real line breaks, but the map may author
        # them as literal "\n" sequences that the game only shows as backslash-n
        # and no model reproduces reliably. Convert both sides for proven
        # text-display containers only; commands and language files keep their
        # literal tokens (README: 布局容器限定).
        if unit.metadata.get("layout_container") == "text-display" and "\\n" in unit.source_text:
            normalized = normalized.replace("\\n", "\n")
            source_for_validation = unit.source_text.replace("\\n", "\n")
        else:
            source_for_validation = unit.source_text
        reason = validate_contextual_target(source_for_validation, normalized)
        if reason is None and _reuse_conflicts_official(unit, normalized, official_terms):
            reason = "official_term_conflict"
        layout_slot = unit.metadata.get("layout_container") in {"sign", "text-display"}
        if reason is None and not normalized.strip() and unit.source_text.strip() and not layout_slot:
            reason = "empty_candidate"
    if reason is not None:
        unit.metadata["candidate_validation"] = {"origin": origin, "reason": reason}
        emit_event(
            stage="candidate_validation",
            file=unit.file_path,
            format=unit.format,
            locator=unit.locator,
            context=unit.context,
            unit_id=unit.id,
            source=unit.source_text,
            policy=unit.policy,
            validation_status="rejected",
            candidate=str(candidate) if isinstance(candidate, str) else "",
            extra={"origin": origin, "reason": reason},
        )
        return None
    # A stale rejection from an earlier attempt must not outlive a success
    # and pollute the final outcome stats (A14).
    unit.metadata.pop("candidate_validation", None)
    return normalized


def load_prior_translation_seeds(
    report_dir: Path,
    input_path: Path,
    *,
    fallback_dirs: tuple[Path, ...] = (),
) -> SeedTranslations:
    """Load exact-input drafts from the current report store and legacy stores."""

    candidates: list[tuple[float, Path, dict[str, object]]] = []
    seen_dirs: set[Path] = set()
    for directory in (report_dir, *fallback_dirs):
        directory = directory.resolve()
        if directory in seen_dirs:
            continue
        seen_dirs.add(directory)
        for report_path in directory.glob("*-translation-report.json"):
            try:
                payload = json.loads(report_path.read_text(encoding="utf-8"))
                report = payload.get("report", {})
                if not isinstance(report, dict) or Path(str(report.get("input_path", ""))).resolve() != input_path.resolve():
                    continue
                units = payload.get("units", [])
                if not isinstance(units, list) or not any(
                    isinstance(item, dict) and isinstance(item.get("final_target"), str) and item["final_target"]
                    for item in units
                ):
                    # A failed or deliberately AI-free run must not hide the
                    # last report that contains reusable historical drafts.
                    continue
                candidates.append((report_path.stat().st_mtime, report_path, payload))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
    if not candidates:
        return {}

    _mtime, _path, payload = max(candidates, key=lambda item: item[0])
    seeds: SeedTranslations = {}
    for item in payload.get("units", []):
        if not isinstance(item, dict):
            continue
        target = item.get("final_target")
        source = item.get("source_text")
        if not isinstance(target, str) or not isinstance(source, str):
            continue
        if target == source:
            # Preserved originals from a failed run are not translations
            # (A07); seeding them would skip the retry on the next run.
            continue
        file_path = str(item.get("file_path", ""))
        format_name = str(item.get("format", ""))
        locator = str(item.get("locator", ""))
        if file_path and format_name and locator:
            seeds[_seed_key(file_path, format_name, locator, source)] = target
    return seeds


def _resolve_mod_assets(mod_assets: ModAssetLibrary | Path | None) -> ModAssetLibrary | None:
    if isinstance(mod_assets, ModAssetLibrary):
        library = mod_assets
    else:
        root = Path(mod_assets) if mod_assets is not None else default_mod_assets_root()
        if root is None or not root.is_dir():
            return None
        library = ModAssetLibrary(root)
    library.ensure_index()
    return library


def _load_language_entries(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _language_value_status(source: str, target: object) -> str:
    if not isinstance(target, str) or not target.strip():
        return "empty"
    if validate_contextual_target(source, target) is not None:
        return "invalid"
    if looks_like_chinese_translation(target):
        return "valid"
    if target == source:
        return "same_as_english"
    return "unverified"


def _merge_existing_language_file(en_path: Path, en_us: dict[str, object], zh_path: Path) -> tuple[SeedTranslations, int, int]:
    """Keep valid Chinese keys and expose missing/invalid English keys for translation."""

    rel = en_path.as_posix()
    zh_cn = _load_language_entries(zh_path) or {}
    seeds: SeedTranslations = {}
    covered = 0
    issues = 0
    for key, source in en_us.items():
        if not isinstance(source, str):
            continue
        status = _language_value_status(source, zh_cn.get(key))
        if status == "valid":
            seeds[_seed_key(rel, "json-lang", key, source)] = str(zh_cn[key])
            covered += 1
        elif status in {"empty", "invalid"}:
            issues += 1
    return seeds, covered, issues


def _verify_copied_language_file(en_us: dict[str, object], zh_path: Path) -> tuple[int, int]:
    zh_cn = _load_language_entries(zh_path) or {}
    covered = 0
    issues = 0
    for key, source in en_us.items():
        if not isinstance(source, str):
            continue
        status = _language_value_status(source, zh_cn.get(key))
        if status == "valid":
            covered += 1
        else:
            issues += 1
    return covered, issues


def _apply_mod_assets(
    root: Path,
    library: ModAssetLibrary | None,
    *,
    archive: str = "",
) -> tuple[set[str], SeedTranslations, list[ModCoverageEntry]]:
    """Reuse audited or existing Chinese language keys before scanning.

    Existing zh_cn files are merged key-by-key: valid Chinese is kept, missing
    or structurally invalid English keys still enter translation. Full library
    copies are verified against the English keys rather than trusted by hash.
    """

    excluded: set[str] = set()
    seeds: SeedTranslations = {}
    coverage: list[ModCoverageEntry] = []
    for en_path in root.rglob("en_us.json"):
        parts = en_path.parts
        try:
            assets_index = parts.index("assets")
        except ValueError:
            continue
        if len(parts) != assets_index + 4 or parts[-2] != "lang":
            continue
        modid = parts[assets_index + 1]
        rel = en_path.relative_to(root).as_posix()
        en_us = _load_language_entries(en_path)
        if en_us is None:
            continue
        zh_path = en_path.with_name("zh_cn.json")
        keys_total = sum(1 for value in en_us.values() if isinstance(value, str))
        if zh_path.is_file():
            existing_seeds, covered, issues = _merge_existing_language_file(en_path.relative_to(root), en_us, zh_path)
            seeds.update(existing_seeds)
            if covered == keys_total and issues == 0:
                excluded.add(rel)
            coverage.append(
                ModCoverageEntry(
                    archive=archive,
                    modid=modid,
                    source="",
                    match_type="existing",
                    keys_total=keys_total,
                    keys_covered=covered,
                    keys_ai=max(0, keys_total - covered),
                    ai_failures=issues,
                )
            )
            continue
        if library is None:
            continue
        entry = library.find_full_entry(modid, sha256_file(en_path))
        if entry is not None:
            zh_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(entry.zh_path, zh_path)
            covered, issues = _verify_copied_language_file(en_us, zh_path)
            if covered == keys_total and issues == 0:
                excluded.add(rel)
            else:
                copied_seeds, copied_covered, copied_issues = _merge_existing_language_file(en_path.relative_to(root), en_us, zh_path)
                seeds.update(copied_seeds)
                covered, issues = copied_covered, copied_issues
            coverage.append(
                ModCoverageEntry(
                    archive=archive,
                    modid=modid,
                    source=entry.source,
                    match_type="full",
                    keys_total=keys_total,
                    keys_covered=covered,
                    keys_ai=max(0, keys_total - covered),
                    ai_failures=issues,
                )
            )
            continue
        partial = library.find_partial(modid, en_us)
        source = ""
        entries = library._ordered_entries(modid)
        if entries:
            source = entries[0].source
        for key, target in partial.items():
            source_text = en_us.get(key)
            if isinstance(source_text, str) and _language_value_status(source_text, target) == "valid":
                seeds[_seed_key(rel, "json-lang", key, source_text)] = target
        covered = len([key for key, target in partial.items() if isinstance(en_us.get(key), str) and _language_value_status(str(en_us.get(key)), target) == "valid"])
        coverage.append(
            ModCoverageEntry(
                archive=archive,
                modid=modid,
                source=source,
                match_type="partial" if entries else "none",
                keys_total=keys_total,
                keys_covered=covered,
                keys_ai=keys_total - covered,
            )
        )
    return excluded, seeds, coverage


def _filter_excluded_units(units: list[TextUnit], excluded_paths: set[str]) -> list[TextUnit]:
    return [unit for unit in units if unit.file_path not in excluded_paths]


def _apply_seed_targets(units: list[TextUnit], seeds: SeedTranslations) -> None:
    for unit in units:
        target = seeds.get(_seed_key(unit.file_path, unit.format, unit.locator, unit.source_text))
        if target is not None:
            unit.final_target = _validated_candidate(unit, target, origin="seed")


def _apply_desktop_choices(units, skip_keys, manual_targets, *, prefix="", official_terms=None, translate_npc: bool = True):
    from .task_records import unit_key
    for unit in units:
        key = unit_key([], prefix + unit.file_path, unit.format, unit.locator, unit.source_text)
        npc_skip = (not translate_npc) and unit.metadata.get("display_kind") == "entity_display_name"
        if key in skip_keys or npc_skip:
            unit.final_target = unit.source_text
            unit.metadata["desktop_skip"] = True
        elif key in manual_targets:
            target = manual_targets[key]
            if not should_send_to_ai(unit.policy):
                raise ValueError("修订对象已被机制保护，不能应用")
            reason = validate_contextual_target(unit.source_text, target)
            if not target.strip() or reason or _reuse_conflicts_official(unit, target, official_terms):
                raise ValueError(f"修订校验失败：{reason or '术语冲突或空译文'}")
            unit.final_target = target


def _resolve_official_terms(
    official_terms: OfficialTermIndex | None,
    official_terms_dir: Path | str | None,
    official_terms_version: str | None,
) -> OfficialTermIndex | None:
    if official_terms is not None:
        return official_terms
    if official_terms_dir:
        return OfficialTermIndex.from_directory(official_terms_dir, official_terms_version or "")
    from .official_terms import bundled_official_terms
    return bundled_official_terms()


def _official_role_for_unit(unit: TextUnit) -> str | None:
    if unit.format in {"json-lang", "legacy-lang"} and (".minecraft." in unit.locator or "assets/minecraft/lang/" in unit.file_path.replace("\\", "/")):
        return unit.locator.split(".", 1)[0]
    return None


def _reuse_conflicts_official(unit: TextUnit, candidate: str, official_terms: OfficialTermIndex | None) -> bool:
    if official_terms is None or not official_terms.available:
        return False
    key = unit.locator if unit.format in {"json-lang", "legacy-lang"} else None
    return official_terms.validate_candidate(unit.source_text, candidate, key=key, role=_official_role_for_unit(unit)) is not None


def translate_units(
    units: list[TextUnit],
    glossary: Glossary,
    memory: TranslationMemory,
    translator: TranslatorProvider,
    max_ai: int | None = None,
    progress_callback: ProgressCallback | None = None,
    progress_start: int = 20,
    progress_end: int = 75,
    scanned: int = 0,
    seed_translations: SeedTranslations | None = None,
    cancel_event: Event | None = None,
    official_terms: OfficialTermIndex | None = None,
) -> tuple[int, int, int]:
    translated = 0
    reused = 0
    skipped = 0
    glossary_only = translator.name == "glossary-only"
    pending: list[TextUnit] = []

    from .references import (
        apply_leader_targets,
        assign_canonical_keys,
        audit_canonical_consistency,
        keep_original_target,
        policy_blocks_canonical_share,
        select_canonical_leaders,
        share_existing_canonical_targets,
    )

    if official_terms is not None and hasattr(translator, "official_terms"):
        translator.official_terms = official_terms

    assign_canonical_keys(units)
    for unit in units:
        _check_cancel(cancel_event)
        if policy_blocks_canonical_share(unit):
            keep_original_target(unit)
            skipped += 1
            continue
        if unit.final_target is not None:
            candidate = _validated_candidate(unit, unit.final_target, origin="preset", official_terms=official_terms)
            if candidate is not None:
                unit.final_target = candidate
                reused += 1
                continue
            unit.final_target = None
        if unit.policy and not should_send_to_ai(unit.policy) and unit.policy != POLICY_TRANSLATABLE:
            keep_original_target(unit)
            skipped += 1
            continue
        memory_context = quality_memory_context(unit)
        if official_terms is not None and official_terms.available and unit.format in {"json-lang", "legacy-lang"}:
            official = official_terms.lookup_exact(unit.locator, unit.source_text, official_terms.game_version or None)
            if official is not None:
                normalized = _validated_candidate(unit, official.target, origin="official", official_terms=official_terms)
                if normalized is not None:
                    unit.final_target = normalized
                    memory.put(unit.source_text, normalized, memory_context, "official")
                    reused += 1
                    continue
        remembered = memory.get(unit.source_text, memory_context)
        if remembered is None:
            remembered = _lookup_book_locator_alias(memory, unit, memory_context)
        if remembered is not None and remembered == unit.source_text:
            # A failure fallback can persist "English -> English" rows (A07);
            # reusing them would make a re-run skip the retry forever.
            remembered = None
        if remembered is not None:
            normalized = _validated_candidate(unit, remembered, origin="memory", official_terms=official_terms)
            if normalized is not None:
                unit.final_target = normalized
                if normalized != remembered:
                    memory.put(unit.source_text, normalized, memory_context, "layout-newline-normalized")
                reused += 1
                continue

        exact = glossary.exact_match(unit.source_text)
        if exact:
            normalized = _validated_candidate(unit, exact, origin="glossary", official_terms=official_terms)
            if normalized is not None:
                unit.final_target = normalized
                memory.put(unit.source_text, normalized, memory_context, "glossary")
                reused += 1
                continue
        if seed_translations:
            seed = seed_translations.get(_seed_key(unit.file_path, unit.format, unit.locator, unit.source_text))
            unit.suggested_target = _validated_candidate(unit, seed, origin="seed", official_terms=official_terms) if seed is not None else None
        pending.append(unit)

    reused += share_existing_canonical_targets(pending)
    pending = [unit for unit in pending if unit.final_target is None]
    leaders = select_canonical_leaders(pending)
    followers = [unit for unit in pending if unit.metadata.get("canonical_role") == "follower"]

    if leaders:
        groups = build_translation_groups(leaders)
        ai_groups = groups if max_ai is None else groups[:max_ai]
        scheduled_ids = {unit.id for group in ai_groups for unit in group.units}
        ai_total = len(ai_groups)
        planned_draft_calls, planned_review_calls = estimate_group_requests(ai_groups)
        checkpointed_groups = 0
        seeded_drafts = sum(1 for unit in leaders if unit.suggested_target is not None)
        if hasattr(translator, "seeded_drafts"):
            translator.seeded_drafts += seeded_drafts

        _emit(
            progress_callback,
            "plan",
            f"已生成 {ai_total} 个上下文文本组",
            progress_start,
            scanned=scanned,
            reused=reused,
            groups_total=ai_total,
            planned_draft_calls=planned_draft_calls,
            planned_review_calls=planned_review_calls,
            seeded_drafts=seeded_drafts,
        )

        def _log_ai_result(group, targets: dict[str, str], done: int) -> None:
            """Expose the exact model-returned text in the runtime log.

            Keep this deliberately bounded: the GUI/runtime log is diagnostic,
            not a second copy of the whole input archive.  Newlines are escaped
            so one result remains one searchable log line. Structured JSONL
            events are emitted separately by the translator / write-back path.
            """
            import html as _html

            ok = 0
            empty = 0
            failed = 0
            kept = 0
            rows: list[str] = []
            plain_rows: list[str] = []
            for unit in group.units:
                source = unit.source_text.replace("\\n", "\\\\n")
                target = targets.get(unit.id)
                emit_event(
                    stage="group_result",
                    file=unit.file_path,
                    format=unit.format,
                    locator=unit.locator,
                    context=unit.context,
                    unit_id=unit.id,
                    source=unit.source_text,
                    policy=unit.policy,
                    group_id=group.id,
                    request_type="group",
                    candidate=target or "",
                    validation_status="ok" if target is not None else "missing",
                    final=target if target is not None else unit.source_text,
                    extra={"original_preserved": target is None or target == unit.source_text},
                )
                if target is None:
                    failed += 1
                    plain_rows.append(f"    ✗ {source}  →  未返回译文 · 保留原文")
                    rows.append(
                        f"    <span style='color:#c0392b'>✗</span> "
                        f"<span style='color:#435774'>{_html.escape(source)}</span> "
                        f"<span style='color:#0097e6'>→</span> "
                        f"<span style='color:#c0392b'>未返回译文 · 保留原文</span>"
                    )
                elif target == "":
                    empty += 1
                    plain_rows.append(f"    ~ {source}  →  （空槽位 · 布局保留）")
                    rows.append(
                        f"    <span style='color:#8a8a93'>~</span> "
                        f"<span style='color:#435774'>{_html.escape(source)}</span> "
                        f"<span style='color:#0097e6'>→</span> "
                        f"<span style='color:#8a8a93'>（空槽位 · 布局保留）</span>"
                    )
                elif target == unit.source_text:
                    kept += 1
                    plain_rows.append(f"    = {source}  →  （保留原文）")
                    rows.append(
                        f"    <span style='color:#8a8a93'>=</span> "
                        f"<span style='color:#435774'>{_html.escape(source)}</span> "
                        f"<span style='color:#0097e6'>→</span> "
                        f"<span style='color:#8a8a93'>（保留原文）</span>"
                    )
                else:
                    ok += 1
                    rendered = str(target).replace("\\n", "\\\\n")
                    plain_rows.append(f"    ✓ {source}  →  {rendered}")
                    rows.append(
                        f"    <span style='color:#2e9e5b'>✓</span> "
                        f"<span style='color:#435774'>{_html.escape(source)}</span> "
                        f"<span style='color:#0097e6'>→</span> "
                        f"<span style='color:#2e9e5b'>{_html.escape(rendered)}</span>"
                    )
            summary = f"{len(group.units)} 段 · {ok}✓ {failed}✗ {empty}~ {kept}="
            percent = (
                progress_end
                if ai_total == 0
                else progress_start + (progress_end - progress_start) * min(done, ai_total) // ai_total
            )
            file_line = f"<span style='color:#1d70b8'><b>▸</b> {_html.escape(group.file_path)}</span>"
            stats_line = f"<span style='color:#8a8a93'>（{summary}）</span>"
            plain_block = f"· {group.file_path}（{summary}）" + "\n" + "\n".join(plain_rows)
            html_block = (
                f"<div style='margin:4px 0'>"
                f"{file_line}&nbsp;&nbsp;{stats_line}"
                f"<div style='margin-left:0'>" + "<br>".join(rows) + "</div>"
                "</div>"
            )
            _emit(
                progress_callback,
                "ai-result",
                plain_block,
                percent,
                current_file=group.file_path,
                message_html=html_block,
            )

        def checkpoint_group(group, targets: dict[str, str]) -> None:
            _log_ai_result(group, targets, int(getattr(translator, "groups_completed", 0)) + 1)
            nonlocal translated, reused, checkpointed_groups
            history_reuse = group.review_only and not group.requires_review
            valid_completed = 0
            for unit in group.units:
                if policy_blocks_canonical_share(unit):
                    keep_original_target(unit)
                    continue
                target = targets.get(unit.id)
                if target is None or unit.final_target is not None:
                    continue
                if target == unit.source_text:
                    # A fallback-preserved original is not a translation (A07):
                    # caching it would block the retry on every later run.
                    continue
                normalized = _validated_candidate(unit, target, origin="model-checkpoint", official_terms=official_terms)
                if normalized is None:
                    continue
                unit.final_target = normalized
                valid_completed += 1
                provider = "history-seed" if history_reuse else translator.name
                memory.put(unit.source_text, normalized, quality_memory_context(unit), provider)
                if history_reuse or glossary_only:
                    reused += 1
                else:
                    translated += 1
            if valid_completed == len(group.units):
                checkpointed_groups += 1

        def on_ai_progress(done: int, total: int, failed: int, last_error: str, group) -> None:
            percent = progress_end if total == 0 else progress_start + int((progress_end - progress_start) * done / total)
            _emit(
                progress_callback,
                "ai",
                "正在翻译并审校文本组",
                percent,
                scanned=scanned,
                translated=translated,
                reused=reused,
                skipped=skipped,
                ai_total=total,
                ai_done=done,
                ai_failed=failed,
                current_file=group.file_path,
                last_error=last_error,
                failure_counts=dict(getattr(translator, "failure_counts", {})),
                retry_counts=dict(getattr(translator, "retry_counts", {})),
                units_total=len(units),
                units_translated=translated,
                units_original_preserved=int(getattr(translator, "units_original_preserved", 0)),
                units_validation_failed=int(getattr(translator, "units_validation_failed", 0)),
                groups_total=total,
                groups_done=done,
                reviewed_groups=int(getattr(translator, "reviewed_groups", 0)),
                review_corrected=int(getattr(translator, "review_corrected", 0)),
                planned_draft_calls=int(getattr(translator, "planned_draft_calls", planned_draft_calls)),
                planned_review_calls=int(getattr(translator, "planned_review_calls", planned_review_calls)),
                api_requests=int(getattr(translator, "api_requests", 0)),
                in_flight=int(getattr(translator, "in_flight_requests", 0)),
                token_usage=dict(getattr(translator, "token_usage", {})),
                usage_reported_requests=int(getattr(translator, "usage_reported_requests", 0)),
                rate_limit_cooldowns=int(getattr(translator, "rate_limit_cooldowns", 0)),
                checkpointed_groups=checkpointed_groups,
                seeded_drafts=int(getattr(translator, "seeded_drafts", seeded_drafts)),
            )

        try:
            targets = translator.translate_groups(ai_groups, glossary, on_progress=on_ai_progress, on_result=checkpoint_group)
        except TypeError as exc:
            # External providers built against the original extension point can
            # still participate; they simply checkpoint after the batch returns.
            if "on_result" not in str(exc):
                raise
            targets = translator.translate_groups(ai_groups, glossary, on_progress=on_ai_progress)
        for unit in leaders:
            if policy_blocks_canonical_share(unit):
                keep_original_target(unit)
                skipped += 1
                continue
            if unit.id not in scheduled_ids:
                skipped += 1
                continue
            target = targets.get(unit.id)
            if target is not None and unit.final_target is None:
                if target == unit.source_text:
                    # Fallback-preserved original: never apply or cache (A07).
                    skipped += 1
                    continue
                normalized = _validated_candidate(unit, target, origin="model", official_terms=official_terms)
                if normalized is not None:
                    unit.final_target = normalized
                    memory.put(unit.source_text, normalized, quality_memory_context(unit), translator.name)
                    translated += 0 if glossary_only else 1
                    reused += 1 if glossary_only else 0
                else:
                    skipped += 1
            elif unit.final_target is None:
                skipped += 1
    applied, leader_conflicts = apply_leader_targets(units)
    reused += applied
    for unit in followers:
        if unit.final_target is not None:
            candidate = _validated_candidate(unit, unit.final_target, origin="canonical-shared", official_terms=official_terms)
            if candidate is not None:
                unit.final_target = candidate
                memory.put(unit.source_text, candidate, quality_memory_context(unit), "canonical-shared")
            else:
                unit.final_target = unit.source_text
                reused = max(0, reused - 1)
                skipped += 1
        elif unit.id not in {leader.id for leader in leaders}:
            skipped += 1
    unified, conflicts = _unify_same_source_targets(units, memory, glossary)
    canonical_conflicts = audit_canonical_consistency(units)
    translator.consistency_unified = int(getattr(translator, "consistency_unified", 0)) + unified + applied
    merged_conflicts = _merge_conflict_lists(conflicts, leader_conflicts, canonical_conflicts)
    translator.consistency_conflicts = merged_conflicts
    translator.canonical_conflicts = canonical_conflicts
    return translated, reused, skipped


# A single lowercase common word (fire, stone, name) is exactly the
# polysemous case: its translations legitimately vary by context, so it is
# never mechanically unified unless a mechanism command or a forced glossary
# term demands consistency.
_POLYSEMOUS_WORD_RE = re.compile(r"^[a-z]{2,16}$")
_MAX_CONFLICT_SAMPLES = 3
_MAX_CONFLICT_TARGETS = 8


def _unify_same_source_targets(
    units: list[TextUnit],
    memory: TranslationMemory,
    glossary: Glossary,
) -> tuple[int, list[dict[str, object]]]:
    """Unify translations that belong to the same mechanism object.

    Canonical keys prefer object/reference identity. Exact same source plus a
    shared display role may still unify (give/clear pairs, lectern books).
    Layout containers stay exempt. Ordinary polysemous words are never forced
    together just because the source string matches.
    """

    from .references import assign_canonical_keys, is_shareable_canonical, keep_original_target, policy_blocks_canonical_share

    assign_canonical_keys(units)
    by_key: dict[str, list[TextUnit]] = defaultdict(list)
    by_source: dict[str, list[TextUnit]] = defaultdict(list)
    for unit in units:
        if policy_blocks_canonical_share(unit):
            keep_original_target(unit)
            continue
        if not unit.final_target or unit.metadata.get("layout_container"):
            continue
        if is_shareable_canonical(unit) or glossary.has_entry(unit.source_text):
            by_key[unit.canonical_key].append(unit)
        by_source[unit.source_text].append(unit)
    unified = 0
    conflicts: dict[str, dict[str, object]] = {}
    seen_ids: set[str] = set()
    work_groups: list[list[TextUnit]] = list(by_key.values())
    for source, members in by_source.items():
        eligible = [member for member in members if not policy_blocks_canonical_share(member)]
        mechanism = any(member.format == "mcfunction" or member.object_key for member in eligible)
        forced_term = glossary.has_entry(source)
        if not mechanism and not forced_term:
            continue
        if len({member.final_target for member in eligible}) > 1:
            work_groups.append(eligible)
    for members in work_groups:
        members = [member for member in members if not policy_blocks_canonical_share(member)]
        distinct = {member.final_target for member in members}
        if len(distinct) <= 1:
            continue
        source = members[0].source_text
        mechanism = any(member.format == "mcfunction" or member.object_key for member in members)
        forced_term = glossary.has_entry(source)
        if not mechanism and not forced_term and _POLYSEMOUS_WORD_RE.fullmatch(source):
            continue
        canonical = Counter(member.final_target for member in members).most_common(1)[0][0]
        for member in members:
            if policy_blocks_canonical_share(member):
                keep_original_target(member)
                continue
            if member.final_target != canonical and member.id not in seen_ids:
                candidate = _validated_candidate(member, canonical, origin="consistency-unified")
                if candidate is not None:
                    member.final_target = candidate
                    memory.put(member.source_text, candidate, quality_memory_context(member), "consistency-unified")
                    unified += 1
                    seen_ids.add(member.id)
    # Residual divergences, also across case/whitespace variants of the same
    # source word, are what the conflict report must explain.
    normalized_groups: dict[str, list[TextUnit]] = defaultdict(list)
    for unit in units:
        if unit.final_target and not unit.metadata.get("layout_container"):
            normalized_groups[unit.source_text.strip().lower()].append(unit)
    for normalized, members in normalized_groups.items():
        distinct = {member.final_target for member in members}
        if len(distinct) <= 1:
            continue
        _record_consistency_conflict(
            conflicts,
            normalized,
            " | ".join(sorted({member.source_text for member in members})),
            members,
        )
    return unified, sorted(conflicts.values(), key=lambda entry: entry["normalized"])


def _merge_conflict_lists(*groups: list[dict[str, object]]) -> list[dict[str, object]]:
    merged: dict[str, dict[str, object]] = {}
    for group in groups:
        for entry in group:
            key = str(entry.get("normalized") or entry.get("canonical_key") or entry.get("source") or "")
            if key not in merged:
                merged[key] = entry
                continue
            existing_targets = {item.get("target") for item in merged[key].get("targets", [])}
            for item in entry.get("targets", []):
                if item.get("target") not in existing_targets:
                    merged[key].setdefault("targets", []).append(item)
    return sorted(merged.values(), key=lambda entry: str(entry.get("normalized") or entry.get("canonical_key") or ""))


def _record_consistency_conflict(
    conflicts: dict[str, dict[str, object]],
    normalized: str,
    source: str,
    members: list[TextUnit],
) -> None:
    """Group same-source multi-translations by case/whitespace normalization."""

    entry = conflicts.get(normalized)
    if entry is None:
        entry = {"source": source, "normalized": normalized, "targets": []}
        conflicts[normalized] = entry
    elif source not in entry["source"]:
        entry["source"] = f"{entry['source']} | {source}"
    by_target: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for member in members:
        by_target[member.final_target or ""].append((member.file_path, member.context))
    for target, samples in sorted(by_target.items(), key=lambda item: (-len(item[1]), item[0])):
        existing = next((item for item in entry["targets"] if item["target"] == target), None)
        if existing is None:
            if len(entry["targets"]) >= _MAX_CONFLICT_TARGETS:
                continue
            existing = {"target": target, "count": 0, "samples": []}
            entry["targets"].append(existing)
        existing["count"] += len(samples)
        for file_path, context in samples[: _MAX_CONFLICT_SAMPLES]:
            sample = {"file": file_path, "context": context}
            if sample not in existing["samples"]:
                existing["samples"].append(sample)


def write_translations(
    root: Path,
    units: list[TextUnit],
    registry: PluginRegistry | None = None,
    warnings: list[str] | None = None,
    cancel_event: Event | None = None,
) -> int:
    registry = registry or default_registry()
    grouped: dict[str, list[TextUnit]] = defaultdict(list)
    from .references import unit_policy_name
    for unit in units:
        if unit.final_target is not None:
            # Writers are an independent extension point; enforce the same
            # policy and source-token gate immediately before any plugin sees
            # a candidate.
            if unit_policy_name(unit) in {POLICY_IMMUTABLE_ID, POLICY_IMMUTABLE_MATCH, POLICY_UNKNOWN} and unit.final_target != unit.source_text:
                reason = "protected_policy"
                unit.metadata["writeback_status"] = "blocked"
                unit.metadata["candidate_validation"] = {"origin": "writeback", "reason": reason}
                if warnings is not None:
                    warnings.append(f"Skipped protected translation {unit.file_path}:{unit.locator} ({reason})")
                emit_event(stage="candidate_validation", file=unit.file_path, format=unit.format,
                           locator=unit.locator, context=unit.context, unit_id=unit.id,
                           source=unit.source_text, policy=unit.policy,
                           validation_status="rejected", candidate=unit.final_target,
                           extra={"origin": "writeback", "reason": reason})
                unit.final_target = unit.source_text
                continue
            elif unit.final_target != unit.source_text:
                candidate = _validated_candidate(unit, unit.final_target, origin="writeback")
                if candidate is None:
                    unit.metadata["writeback_status"] = "blocked"
                    if warnings is not None:
                        reason = unit.metadata.get("candidate_validation", {}).get("reason", "invalid_candidate")
                        warnings.append(f"Skipped invalid translation {unit.file_path}:{unit.locator} ({reason})")
                    unit.final_target = unit.source_text
                    continue
                else:
                    unit.final_target = candidate
            grouped[unit.file_path].append(unit)

    changed = 0
    for rel_path, file_units in grouped.items():
        _check_cancel(cancel_event)
        path = root / rel_path
        if not path.is_file():
            if warnings is not None:
                warnings.append(f"Skipped missing source file: {rel_path}")
            for unit in file_units:
                unit.metadata["writeback_status"] = "failed"
                emit_event(
                    stage="writeback",
                    file=rel_path,
                    format=unit.format,
                    locator=unit.locator,
                    unit_id=unit.id,
                    source=unit.source_text,
                    policy=unit.policy,
                    writeback_status="failed",
                    writeback_target=unit.final_target or "",
                    extra={"reason": "missing_source_file"},
                )
            continue
        plugin = registry.writer_for(path)
        if not plugin:
            if warnings is not None:
                warnings.append(f"Skipped unsupported output file: {rel_path}")
            for unit in file_units:
                unit.metadata["writeback_status"] = "failed"
                emit_event(
                    stage="writeback",
                    file=rel_path,
                    format=unit.format,
                    locator=unit.locator,
                    unit_id=unit.id,
                    source=unit.source_text,
                    policy=unit.policy,
                    writeback_status="failed",
                    writeback_target=unit.final_target or "",
                    extra={"reason": "unsupported_output"},
                )
            continue
        try:
            file_changed = plugin.write(root, path, file_units)
            changed += file_changed
            for unit in file_units:
                if unit.metadata.get("writeback_status"):
                    continue
                status = "changed" if file_changed else "unchanged"
                unit.metadata["writeback_status"] = status
                emit_event(
                    stage="writeback",
                    file=rel_path,
                    format=unit.format,
                    locator=unit.locator,
                    context=unit.context,
                    unit_id=unit.id,
                    source=unit.source_text,
                    policy=unit.policy,
                    writeback_status=status,
                    writeback_target=unit.final_target or "",
                    writeback_changed=status == "changed",
                    final=unit.final_target or "",
                )
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            if warnings is not None:
                warnings.append(f"Failed to write {rel_path}: {exc}")
            for unit in file_units:
                unit.metadata["writeback_status"] = "failed"
                emit_event(
                    stage="writeback",
                    file=rel_path,
                    format=unit.format,
                    locator=unit.locator,
                    unit_id=unit.id,
                    source=unit.source_text,
                    policy=unit.policy,
                    writeback_status="failed",
                    writeback_target=unit.final_target or "",
                    extra={"reason": str(exc)},
                )
            continue
    return changed


def _process_nested_archives(
    root: Path,
    glossary: Glossary,
    memory: TranslationMemory,
    translator: TranslatorProvider,
    registry: PluginRegistry,
    max_ai: int | None,
    mod_assets: ModAssetLibrary | None = None,
    seed_translations: SeedTranslations | None = None,
    progress_callback: ProgressCallback | None = None,
    cancel_event: Event | None = None,
    official_terms: OfficialTermIndex | None = None,
    skip_unit_keys: set[str] | None = None,
    manual_targets: dict[str, str] | None = None,
    translate_npc: bool = True,
    task_root: Path | None = None,
) -> tuple[list[TextUnit], int, int, int, list[str], dict[str, int], list[ModCoverageEntry], list[ArchiveResult], list[dict[str, object]], bool]:
    all_units: list[TextUnit] = []
    translated_total = 0
    reused_total = 0
    skipped_total = 0
    warnings: list[str] = []
    filtered_counts: dict[str, int] = {}
    filtered_details: list[dict[str, object]] = []
    mod_coverage: list[ModCoverageEntry] = []
    archive_results: list[ArchiveResult] = []
    any_blocked = False
    remaining_ai = max_ai

    archives = [
        path
        for path in iter_files(root)
        if path.suffix.lower() in {".jar", ".zip"} and zipfile.is_zipfile(path)
    ]
    for index, archive_path in enumerate(archives, start=1):
        _check_cancel(cancel_event)
        archive_rel = archive_path.relative_to(root).as_posix()
        original_fingerprint = archive_fingerprint(archive_path) if archive_path.is_file() else ""
        _emit(
            progress_callback,
            "nested",
            f"处理内嵌压缩包 {index} / {len(archives)}",
            78 + int(10 * (index - 1) / max(1, len(archives))),
            current_file=archive_rel,
            archive_id=archive_rel,
            archive_index=index,
            archives_discovered=len(archives),
        )
        with tempfile.TemporaryDirectory(prefix="mc-hanhua-nested-") as tmp:
            archive_root = Path(tmp) / "archive"
            archive_root.mkdir()
            try:
                with zipfile.ZipFile(archive_path) as zf:
                    zf.extractall(archive_root)
            except (OSError, zipfile.BadZipFile) as exc:
                warning = f"Failed to extract nested archive {archive_rel}: {exc}"
                warnings.append(warning)
                archive_results.append(
                    ArchiveResult(
                        archive_id=archive_rel,
                        archive_chain=[archive_rel],
                        status="failed",
                        warnings=[warning],
                        publication_status="withheld",
                        original_fingerprint=original_fingerprint,
                        error_code="extract_failed",
                        phase="prepare",
                    )
                )
                any_blocked = True
                continue

            excluded_paths, asset_seeds, archive_coverage = _apply_mod_assets(archive_root, mod_assets, archive=archive_rel)
            scan_result = scan(archive_root, registry, cancel_event=cancel_event, allow_new_names=False)
            scan_result.text_units = _filter_excluded_units(scan_result.text_units, excluded_paths)
            nested_seeds: SeedTranslations = dict(asset_seeds)
            if seed_translations:
                prefix = f"{archive_rel}!/"
                for (file_path, format_name, locator, source_text), target in seed_translations.items():
                    if file_path.startswith(prefix):
                        nested_seeds[_seed_key(file_path[len(prefix) :], format_name, locator, source_text)] = target
            _apply_seed_targets(scan_result.text_units, nested_seeds)
            _apply_desktop_choices(scan_result.text_units, skip_unit_keys or set(), manual_targets or {}, prefix=f"{archive_rel}!/", official_terms=official_terms, translate_npc=translate_npc)
            groups_before = int(getattr(translator, "groups_completed", 0))
            translated, reused, skipped = translate_units(
                [u for u in scan_result.text_units if not u.metadata.get("desktop_skip")],
                glossary,
                memory,
                translator,
                remaining_ai,
                progress_callback=progress_callback,
                progress_start=78,
                progress_end=88,
                scanned=len(scan_result.text_units),
                seed_translations=nested_seeds,
                cancel_event=cancel_event,
                official_terms=official_terms,
            )
            consumed_groups = int(getattr(translator, "groups_completed", 0)) - groups_before
            if remaining_ai is not None:
                remaining_ai = max(0, remaining_ai - consumed_groups)
            skipped += sum(bool(u.metadata.get("desktop_skip")) for u in scan_result.text_units)
            reused += sum(entry.keys_covered for entry in archive_coverage if entry.match_type == "full")
            nested_baseline = take_baseline(archive_root, scan_result)
            try:
                write_translations(archive_root, scan_result.text_units, registry, warnings=scan_result.warnings, cancel_event=cancel_event)
                nested_audit = audit_output(
                    archive_root,
                    baseline=nested_baseline,
                    baseline_scan=scan_result,
                    units=scan_result.text_units,
                    registry=registry,
                    cancel_event=cancel_event,
                )
            finally:
                del nested_baseline
            blocked = bool(nested_audit.blocked)
            prefixed_details = _prefix_filtered_details(list(scan_result.filtered_details), archive_rel)
            _prefix_units(scan_result.text_units, archive_rel)
            all_units.extend(scan_result.text_units)
            filtered_details.extend(prefixed_details)
            _merge_counts(filtered_counts, scan_result.filtered_counts)
            mod_coverage.extend(archive_coverage)
            translated_total += translated
            reused_total += reused
            skipped_total += skipped
            prefixed_warnings = [f"{archive_rel}!/{warning}" for warning in scan_result.warnings]
            if blocked:
                any_blocked = True
                issue_lines = [
                    f"{archive_rel}!/{issue.file}: [{issue.category}] {issue.message}"
                    for issue in nested_audit.errors[:20]
                ]
                warnings.append(f"{archive_rel}!/nested audit blocked publication of this archive")
                warnings.extend(issue_lines)
                warnings.extend(prefixed_warnings)
                archive_results.append(
                    _persist_archive_workspace(
                        task_root,
                        archive_rel,
                        archive_root,
                        scan_result,
                        nested_audit,
                        status="blocked",
                        publication_status="withheld",
                        translated=translated,
                        reused=reused,
                        skipped=skipped,
                        original_fingerprint=original_fingerprint,
                        error_code="nested_audit_blocked",
                        phase="audit",
                        groups_completed=consumed_groups,
                    )
                )
                continue
            warnings.extend(prefixed_warnings)
            published = False
            if any(unit.final_target for unit in scan_result.text_units) or any(entry.match_type == "full" for entry in archive_coverage):
                tmp_archive = Path(tmp) / "archive.zip"
                try:
                    repack_directory(archive_root, tmp_archive, exclude_tool_metadata=True, cancel_event=cancel_event)
                    shutil.move(str(tmp_archive), archive_path)
                    published = True
                except TranslationPaused:
                    raise
                except Exception as exc:
                    any_blocked = True
                    warning = f"{archive_rel}!/failed to repack nested archive: {exc}"
                    warnings.append(warning)
                    archive_results.append(
                        _persist_archive_workspace(
                            task_root,
                            archive_rel,
                            archive_root,
                            scan_result,
                            nested_audit,
                            status="failed",
                            publication_status="withheld",
                            translated=translated,
                            reused=reused,
                            skipped=skipped,
                            original_fingerprint=original_fingerprint,
                            error_code="repack_failed",
                            phase="pack",
                            groups_completed=consumed_groups,
                        )
                    )
                    continue
            archive_results.append(
                _persist_archive_workspace(
                    task_root,
                    archive_rel,
                    archive_root,
                    scan_result,
                    nested_audit,
                    status="published" if published else "ready",
                    publication_status="published" if published else "staged",
                    translated=translated,
                    reused=reused,
                    skipped=skipped,
                    original_fingerprint=original_fingerprint,
                    phase="audit",
                    groups_completed=consumed_groups,
                )
            )

    return all_units, translated_total, reused_total, skipped_total, warnings, filtered_counts, mod_coverage, archive_results, filtered_details, any_blocked


def _build_translation_report(
    input_path: Path,
    output_path: Path,
    translator: TranslatorProvider,
    all_units: list[TextUnit],
    translated: int,
    reused: int,
    skipped: int,
    warnings: list[str],
    filtered_counts: dict[str, int],
    mod_coverage: list[ModCoverageEntry],
    audit: dict[str, object] | None = None,
    audit_blocked: bool = False,
    nbt_text_diff: dict[str, object] | None = None,
    structure_conflicts: list[dict[str, object]] | None = None,
    event_log_path: str = "",
    filtered_details: list[dict[str, object]] | None = None,
    reference_index: dict[str, object] | None = None,
    archive_results: list[ArchiveResult] | None = None,
    task_id: str = "",
    publication_status: str = "unknown",
    workspace_path: str = "",
    report_path: str = "",
    recoverable: bool = False,
) -> TranslationReport:
    layout_groups = build_translation_groups(all_units)
    units_translated = sum(1 for unit in all_units if unit.final_target not in (None, unit.source_text))
    units_original = sum(
        1
        for unit in all_units
        if unit.final_target is None or unit.final_target == unit.source_text
    )
    units_writeback_failed = sum(1 for unit in all_units if unit.metadata.get("writeback_status") == "failed")
    units_validation_failed = int(getattr(translator, "units_validation_failed", 0))
    unit_outcomes: dict[str, int] = {}
    for unit in all_units:
        status = unit_outcome_status(unit)
        unit_outcomes[status] = unit_outcomes.get(status, 0) + 1
    return TranslationReport(
        input_path=input_path,
        output_path=output_path,
        scanned=len(all_units),
        translated=translated,
        reused=reused,
        skipped=skipped,
        high_risk=sum(1 for unit in all_units if unit.risk_level == RiskLevel.HIGH),
        warnings=warnings,
        failure_counts=dict(getattr(translator, "failure_counts", {})),
        failure_samples=dict(getattr(translator, "failure_samples", {})),
        retry_counts=dict(getattr(translator, "retry_counts", {})),
        groups_total=int(getattr(translator, "groups_total", 0)),
        groups_completed=int(getattr(translator, "groups_completed", 0)),
        failed_groups=int(getattr(translator, "failed_groups", 0)),
        reviewed_groups=int(getattr(translator, "reviewed_groups", 0)),
        review_corrected=int(getattr(translator, "review_corrected", 0)),
        review_pending_groups=int(getattr(translator, "review_pending_groups", 0)),
        planned_draft_calls=int(getattr(translator, "planned_draft_calls", 0)),
        planned_review_calls=int(getattr(translator, "planned_review_calls", 0)),
        api_requests=int(getattr(translator, "api_requests", 0)),
        token_usage=dict(getattr(translator, "token_usage", {})),
        usage_reported_requests=int(getattr(translator, "usage_reported_requests", 0)),
        rate_limit_cooldowns=int(getattr(translator, "rate_limit_cooldowns", 0)),
        checkpointed_groups=int(getattr(translator, "checkpointed_groups", 0)),
        seeded_drafts=int(getattr(translator, "seeded_drafts", 0)),
        consistency_unified=int(getattr(translator, "consistency_unified", 0)),
        consistency_conflicts=list(getattr(translator, "consistency_conflicts", [])),
        reference_index=dict(reference_index or {}),
        canonical_conflicts=list(getattr(translator, "canonical_conflicts", [])),
        filtered_counts=filtered_counts,
        layout_reflow_groups=sum(1 for group in layout_groups if group.layout_reflow),
        mod_coverage=mod_coverage,
        spatial_sign_clusters=sum(1 for group in layout_groups if group.spatial_sign_cluster),
        units_total=len(all_units),
        units_translated=units_translated,
        units_original_preserved=units_original,
        units_validation_failed=units_validation_failed,
        units_writeback_failed=units_writeback_failed,
        unit_outcomes=unit_outcomes,
        filtered_details=list(filtered_details or []),
        event_log_path=event_log_path,
        nbt_text_diff=nbt_text_diff,
        structure_conflicts=list(structure_conflicts or []),
        audit=audit,
        audit_blocked=audit_blocked,
        entity_display_name=int(filtered_counts.get("entity_display_name", 0)),
        unknown_identity=int(filtered_counts.get("unknown_identity", 0)),
        task_id=task_id,
        publication_status=publication_status,
        archive_results=list(archive_results or []),
        workspace_path=workspace_path,
        report_path=report_path,
        recoverable=recoverable,
    )


def run_translation(
    input_path: Path,
    output_path: Path,
    glossary_path: Path | None = None,
    memory_path: Path | None = None,
    translator: TranslatorProvider | None = None,
    max_ai: int | None = None,
    display_output_path: Path | None = None,
    progress_callback: ProgressCallback | None = None,
    seed_translations: SeedTranslations | None = None,
    mod_assets: ModAssetLibrary | Path | None = None,
    cancel_event: Event | None = None,
    official_terms: OfficialTermIndex | None = None,
    official_terms_dir: Path | str | None = None,
    official_terms_version: str | None = None,
    skip_unit_keys: set[str] | None = None,
    manual_targets: dict[str, str] | None = None,
    translate_npc: bool = True,
    _preflight: bool = True,
    _write_failure_log: bool = True,
    task_root: Path | None = None,
    task_id: str = "",
) -> TranslationReport:
    input_path = input_path.resolve()
    output_path = output_path.resolve()
    memory: TranslationMemory | None = None
    event_sink: EventSink | None = None
    sink_token = None
    try:
        if _preflight:
            _preflight_output(input_path, output_path)
        event_sink = None if user_logs_disabled() else EventSink.create_default()
        if event_sink is not None:
            sink_token = set_current_sink(event_sink)
        _emit(progress_callback, "prepare", "正在准备输入资源", 3, current_file=str(input_path))
        registry = default_registry()
        library = _resolve_mod_assets(mod_assets)
        working_root = copy_input(input_path, output_path, cancel_event=cancel_event)
        if event_sink is not None:
            event_sink.add_mirror(output_event_log_path(working_root))
        _emit(progress_callback, "scan", "正在扫描可翻译文本", 12, current_file=str(working_root))

        _check_cancel(cancel_event)
        glossary = Glossary.builtin().merge(load_existing_zh_cn_glossary(working_root))
        if glossary_path:
            glossary = glossary.merge(Glossary.from_file(glossary_path))

        memory_path = memory_path or (working_root / ".mc-hanhua" / "translation-memory-context-v3.sqlite")
        memory = TranslationMemory(memory_path)
        translator = translator or translator_from_env()
        official_index = _resolve_official_terms(official_terms, official_terms_dir, official_terms_version)
        if official_index is not None and hasattr(translator, "official_terms"):
            translator.official_terms = official_index
        excluded_paths, asset_seeds, mod_coverage = _apply_mod_assets(working_root, library)
        combined_seeds = dict(asset_seeds)
        if seed_translations:
            combined_seeds.update(seed_translations)
        scan_result = scan(working_root, registry, cancel_event=cancel_event)
        scan_result.text_units = _filter_excluded_units(scan_result.text_units, excluded_paths)
        _apply_seed_targets(scan_result.text_units, combined_seeds)
        _apply_desktop_choices(scan_result.text_units, skip_unit_keys or set(), manual_targets or {}, official_terms=official_index, translate_npc=translate_npc)
        _emit(
            progress_callback,
            "scan",
            f"扫描到 {len(scan_result.text_units)} 条文本，已过滤 {sum(scan_result.filtered_counts.values())} 条非翻译内容",
            18,
            scanned=len(scan_result.text_units),
            filtered_counts=dict(scan_result.filtered_counts),
        )
        translated, reused, skipped = translate_units(
            [u for u in scan_result.text_units if not u.metadata.get("desktop_skip")],
            glossary,
            memory,
            translator,
            max_ai,
            progress_callback=progress_callback,
            scanned=len(scan_result.text_units),
            seed_translations=combined_seeds,
            cancel_event=cancel_event,
            official_terms=official_index,
        )
        skipped += sum(bool(u.metadata.get("desktop_skip")) for u in scan_result.text_units)
        reused += sum(entry.keys_covered for entry in mod_coverage if entry.match_type == "full")
        _emit(progress_callback, "write", "正在写入汉化文本", 76, scanned=len(scan_result.text_units), translated=translated, reused=reused, skipped=skipped)
        # Audit root files before entering nested AI work. Each archive has its
        # own pre-write baseline and publication gate below.
        baseline_snapshot = take_baseline(working_root, scan_result)
        try:
            write_translations(working_root, scan_result.text_units, registry, warnings=scan_result.warnings, cancel_event=cancel_event)
            _emit(progress_callback, "audit", "正在审计根图写回结果", 77, scanned=len(scan_result.text_units))
            audit_result = audit_output(
                working_root, baseline=baseline_snapshot, baseline_scan=scan_result,
                units=scan_result.text_units, registry=registry, cancel_event=cancel_event,
            )
        finally:
            del baseline_snapshot
        completed_groups = int(getattr(translator, "groups_completed", 0))
        remaining_ai = None if max_ai is None else max(0, max_ai - completed_groups)
        nested_units, nested_translated, nested_reused, nested_skipped, nested_warnings, nested_filtered_counts, nested_mod_coverage, nested_archive_results, nested_filtered_details, nested_blocked = _process_nested_archives(
            working_root,
            glossary,
            memory,
            translator,
            registry,
            remaining_ai,
            mod_assets=library,
            seed_translations=combined_seeds,
            progress_callback=progress_callback,
            cancel_event=cancel_event,
            official_terms=official_index,
            skip_unit_keys=skip_unit_keys,
            manual_targets=manual_targets,
            translate_npc=translate_npc,
            task_root=task_root or (working_root / ".mc-hanhua"),
        )
        all_units = [*scan_result.text_units, *nested_units]
        if manual_targets:
            from .task_records import unit_key
            applied = {unit_key([], u.file_path, u.format, u.locator, u.source_text): u.final_target for u in all_units}
            if any(applied.get(key) != target for key, target in manual_targets.items()):
                raise ValueError("部分修订无法按原始定位应用；已中止发布，原归档未改变。")
        translated += nested_translated
        reused += nested_reused
        skipped += nested_skipped
        warnings = [*scan_result.warnings, *nested_warnings]
        if official_index is None or not official_index.available:
            warnings.append("official_terms_unavailable")
        if not all_units:
            warnings.extend(collect_input_diagnostics(working_root))
        filtered_counts = dict(scan_result.filtered_counts)
        _merge_counts(filtered_counts, nested_filtered_counts)
        combined_filtered_details = [*scan_result.filtered_details, *nested_filtered_details]
        _emit(
            progress_callback,
            "audit",
            f"发布前审计完成：{len(audit_result.errors)} 个阻断问题，{len(audit_result.warnings)} 个警告",
            90,
            scanned=len(all_units),
            audit_errors=len(audit_result.errors),
            audit_warnings=len(audit_result.warnings),
        )
        nbt_text_diff = compare_nbt_text(scan_result, all_units, working_root)
        structure_conflicts = diagnose_structure_conflicts(working_root)
        from .references import record_display_candidates

        record_display_candidates(scan_result.reference_index, all_units)
        reference_payload = scan_result.reference_index.to_report() if scan_result.reference_index is not None else {}
        event_log = ""
        if event_sink is not None:
            mirror = output_event_log_path(working_root)
            event_log = str(event_sink.primary or mirror)
        blocked = bool(audit_result.blocked or nested_blocked)
        publication_status = "withheld" if blocked else "published"
        report_dir = working_root / ".mc-hanhua"
        json_report_path = report_dir / "translation-report.json"
        html_report_path = report_dir / "translation-report.html"
        report = _build_translation_report(
            input_path,
            (display_output_path.resolve() if display_output_path else output_path),
            translator,
            all_units,
            translated,
            reused,
            skipped,
            warnings,
            filtered_counts,
            [*mod_coverage, *nested_mod_coverage],
            audit=audit_result.to_dict(),
            audit_blocked=blocked,
            nbt_text_diff=nbt_text_diff,
            structure_conflicts=structure_conflicts,
            event_log_path=event_log,
            filtered_details=combined_filtered_details,
            reference_index=reference_payload,
            archive_results=nested_archive_results,
            task_id=task_id,
            publication_status=publication_status,
            workspace_path=str(task_root or working_root),
            report_path=str(json_report_path),
            recoverable=blocked,
        )
        write_json_report(json_report_path, report, all_units)
        write_html_report(html_report_path, report, all_units)
        if task_root is not None:
            shutil.copy2(json_report_path, task_root / "translation-report.json")
            shutil.copy2(html_report_path, task_root / "translation-report.html")
        if blocked:
            first = audit_result.errors[0] if audit_result.errors else None
            blocked_archives = [item.archive_id for item in nested_archive_results if item.publication_status == "withheld"]
            if first is not None:
                category = first.category or "audit"
                message = f"发布前审计阻断：{len(audit_result.errors)} 处{category}问题（例如 {first.file or '(重新扫描)'}：{first.message}）"
                archive_id = first.file
            elif blocked_archives:
                message = f"内嵌包审计阻断：{len(blocked_archives)} 个数据包未发布（例如 {blocked_archives[0]}）"
                archive_id = blocked_archives[0]
            else:
                message = "发布前审计阻断：存在未通过的数据包"
                archive_id = ""
            raise AuditBlockedError(
                message,
                task_id=task_id,
                error_code="audit_blocked",
                phase="audit",
                archive_id=archive_id,
                report_path=str(json_report_path),
                workspace_path=str(task_root or working_root),
                recoverable=True,
            )
        _emit(
            progress_callback,
            "complete",
            f"资源处理完成；布局重排 {report.layout_reflow_groups} 组，跨牌告示 {report.spatial_sign_clusters} 组",
            92,
            scanned=report.scanned,
            translated=report.translated,
            reused=report.reused,
            skipped=report.skipped,
            units_total=report.units_total,
            units_translated=report.units_translated,
            units_original_preserved=report.units_original_preserved,
            units_validation_failed=report.units_validation_failed,
            units_writeback_failed=report.units_writeback_failed,
            ai_failed=report.failed_groups,
            failure_counts=report.failure_counts,
            retry_counts=report.retry_counts,
            groups_total=report.groups_total,
            groups_done=report.groups_completed,
            reviewed_groups=report.reviewed_groups,
            review_corrected=report.review_corrected,
            planned_draft_calls=report.planned_draft_calls,
            planned_review_calls=report.planned_review_calls,
            api_requests=report.api_requests,
            token_usage=report.token_usage,
            usage_reported_requests=report.usage_reported_requests,
            rate_limit_cooldowns=report.rate_limit_cooldowns,
            checkpointed_groups=report.checkpointed_groups,
            seeded_drafts=report.seeded_drafts,
            filtered_counts=report.filtered_counts,
        )
        return report
    except TranslationPaused as exc:
        if _write_failure_log:
            displayed_output = display_output_path.resolve() if display_output_path else output_path
            log_path = write_failure_log(
                exc,
                operation="translation-paused",
                input_path=input_path,
                output_path=displayed_output,
            )
            attach_diagnostic_path(exc, log_path)
        _emit(progress_callback, "paused", str(exc), 0, last_error=str(exc))
        raise
    except Exception as exc:
        if _write_failure_log:
            displayed_output = display_output_path.resolve() if display_output_path else output_path
            log_path = write_failure_log(
                exc,
                operation="translation",
                input_path=input_path,
                output_path=displayed_output,
            )
            attach_diagnostic_path(exc, log_path)
            _emit(progress_callback, "failed", f"处理失败：{exc}", 0, last_error=str(exc))
        raise
    finally:
        if sink_token is not None:
            reset_current_sink(sink_token)
        if memory is not None:
            memory.close()


def translate_archive_to_file(
    input_path: Path,
    output_archive: Path,
    *,
    progress_callback: ProgressCallback | None = None,
    **kwargs: object,
) -> TranslationReport:
    input_path = input_path.resolve()
    output_archive = output_archive.resolve()
    cancel_event = kwargs.get("cancel_event")
    job_dir: Path | None = None
    try:
        _preflight_output(input_path, output_archive)
        _emit(progress_callback, "prepare", "正在检查输出位置", 2, current_file=str(output_archive))
        runtime_dir = output_archive.parent / ".mc-hanhua"
        cache_dir = runtime_dir / "cache"
        reports_dir = runtime_dir / "reports"
        work_dir = runtime_dir / "work"
        for directory in (cache_dir, reports_dir, work_dir):
            directory.mkdir(parents=True, exist_ok=True)
        legacy_cache_dir = output_archive.parent / ".mc-hanhua-cache"
        memory_path = cache_dir / "translation-memory-context-v3.sqlite"
        legacy_memory_path = legacy_cache_dir / "translation-memory-context-v3.sqlite"
        if not memory_path.exists() and legacy_memory_path.is_file():
            memory_path = legacy_memory_path
        run_kwargs = dict(kwargs)
        # The CLI explicitly passes ``memory_path=None`` when its optional flag
        # is absent. Treat that the same as omitting it so archive tasks still
        # use their persistent cache beside the selected output file.
        if not run_kwargs.get("memory_path"):
            run_kwargs["memory_path"] = memory_path
        run_kwargs.setdefault(
            "seed_translations",
            load_prior_translation_seeds(reports_dir, input_path, fallback_dirs=(legacy_cache_dir,)),
        )
        # The working directory deliberately lives beside the final archive. This avoids
        # cross-drive temp permissions and makes the final move an atomic same-volume rename.
        job_dir = Path(tempfile.mkdtemp(prefix="job-", dir=work_dir))
        tmp_output = job_dir / "output"
        task_id = str(run_kwargs.get("task_id") or job_dir.name)
        run_kwargs["task_id"] = task_id
        run_kwargs["task_root"] = job_dir
        report = run_translation(
            input_path,
            tmp_output,
            display_output_path=output_archive,
            progress_callback=progress_callback,
            _preflight=False,
            _write_failure_log=False,
            **run_kwargs,
        )
        _copy_long_term_reports(tmp_output, job_dir, reports_dir, output_archive.stem)
        temp_archive = job_dir / f"{output_archive.stem}.tmp{output_archive.suffix}"
        _emit(progress_callback, "pack", "正在压缩输出文件", 94, scanned=report.scanned, translated=report.translated, reused=report.reused, skipped=report.skipped, failure_counts=report.failure_counts, retry_counts=report.retry_counts)
        repack_directory(tmp_output, temp_archive, exclude_tool_metadata=True, cancel_event=cancel_event)
        _emit(progress_callback, "save", "正在保存输出文件", 98, scanned=report.scanned, translated=report.translated, reused=report.reused, skipped=report.skipped, failure_counts=report.failure_counts, retry_counts=report.retry_counts)
        temp_archive.replace(output_archive)
        report.output_path = output_archive
        report.publication_status = "published"
        # job_dir is removed below; the surviving report copies live in the
        # long-term store beside the output (A11).
        report.report_path = str(reports_dir / f"{output_archive.stem}-translation-report.json")
        report.workspace_path = str(output_archive.parent / ".mc-hanhua")
        _emit(progress_callback, "complete", "汉化完成", 100, scanned=report.scanned, translated=report.translated, reused=report.reused, skipped=report.skipped, failure_counts=report.failure_counts, retry_counts=report.retry_counts)
        shutil.rmtree(job_dir, ignore_errors=True)
        return report
    except TranslationPaused as exc:
        _copy_long_term_reports(job_dir / "output" if job_dir is not None else None, job_dir, reports_dir if "reports_dir" in locals() else None, output_archive.stem)
        log_path = write_failure_log(
            exc,
            operation="archive-translation-paused",
            input_path=input_path,
            output_path=output_archive,
        )
        attach_diagnostic_path(exc, log_path)
        _attach_recovery_paths(exc, job_dir)
        _emit(progress_callback, "paused", str(exc), 0, last_error=str(exc))
        raise
    except Exception as cop:
        _copy_long_term_reports(job_dir / "output" if job_dir is not None else None, job_dir, reports_dir if "reports_dir" in locals() else None, output_archive.stem)
        log_path = write_failure_log(
            cop,
            operation="archive-translation",
            input_path=input_path,
            output_path=output_archive,
        )
        attach_diagnostic_path(cop, log_path)
        _attach_recovery_paths(cop, job_dir)
        _emit(progress_callback, "failed", f"保存或处理失败：{cop}", 0, last_error=str(cop))
        raise


def _attach_recovery_paths(error: BaseException, job_dir: Path | None) -> None:
    if job_dir is None:
        return
    setattr(error, "workspace_path", str(job_dir))
    candidate = job_dir / "translation-report.json"
    nested = job_dir / "output" / ".mc-hanhua" / "translation-report.json"
    if not candidate.is_file() and nested.is_file():
        candidate = nested
    setattr(error, "report_path", str(candidate))
    setattr(error, "recoverable", True)
    if not getattr(error, "task_id", ""):
        setattr(error, "task_id", job_dir.name)


def _copy_long_term_reports(output_root: Path | None, job_dir: Path | None, reports_dir: Path | None, stem: str) -> None:
    sources: list[Path] = []
    if output_root is not None:
        sources.append(output_root / ".mc-hanhua")
    if job_dir is not None:
        sources.append(job_dir)
    for report_name in ("translation-report.json", "translation-report.html"):
        source = next((directory / report_name for directory in sources if (directory / report_name).is_file()), None)
        if source is None:
            continue
        if job_dir is not None and source.parent != job_dir:
            shutil.copy2(source, job_dir / report_name)
        if reports_dir is not None:
            reports_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, reports_dir / f"{stem}-{report_name}")


class OfflineRecoveryError(RuntimeError):
    def __init__(self, message: str, *, missing: str = "") -> None:
        super().__init__(message)
        self.missing = missing
        self.recoverable = False


def _unit_from_report(item: dict[str, object]) -> TextUnit:
    target = item.get("final_target")
    return TextUnit(
        id=str(item.get("id") or ""),
        source_text=str(item.get("source_text") or ""),
        file_path=str(item.get("file_path") or ""),
        format=str(item.get("format") or ""),
        locator=str(item.get("locator") or ""),
        context=str(item.get("context") or ""),
        final_target=target if isinstance(target, str) else None,
        policy=str(item.get("policy") or ""),
    )


def _save_recovery_report(report_path: Path, payload: dict, output_archive: Path) -> TranslationReport:
    """Keep the saved audit, HTML and desktop result on the same terminal state."""
    from dataclasses import fields

    data = payload["report"]
    values = {field.name: data[field.name] for field in fields(TranslationReport) if field.name in data}
    values["input_path"] = Path(data["input_path"])
    values["output_path"] = output_archive
    values["archive_results"] = [ArchiveResult.from_dict(item) for item in data.get("archive_results", [])]
    values["mod_coverage"] = [ModCoverageEntry(**item) for item in data.get("mod_coverage", [])]
    report = TranslationReport(**values)
    units = [_unit_from_report(item) for item in payload.get("units", [])]
    atomic_write_json(report_path, payload)
    html_path = report_path.with_suffix(".html")
    temporary = html_path.with_suffix(".html.tmp")
    write_html_report(temporary, report, units)
    temporary.replace(html_path)
    reports_dir = output_archive.parent / ".mc-hanhua" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    for source in (report_path, html_path):
        target = reports_dir / f"{output_archive.stem}-translation-report{source.suffix}"
        if source.resolve() != target.resolve():
            shutil.copy2(source, target)
    return report


def republish_from_workspace(
    workspace: Path,
    output_archive: Path,
    *,
    progress_callback: ProgressCallback | None = None,
    cancel_event: Event | None = None,
) -> TranslationReport:
    """Re-audit staged output against the ORIGINAL input and publish without AI.

    The baseline always comes from a fresh scan of the original input (A01):
    auditing the staged output against itself would treat anything the
    translation broke as pre-existing damage. Saved report units are replayed
    against the staged output so lost translations are caught too. When the
    original input is gone there is no trustworthy baseline and the zero-AI
    publish is refused.
    """

    workspace = workspace.resolve()
    output_archive = output_archive.resolve()
    report_path = workspace / "translation-report.json"
    if not report_path.is_file():
        nested = workspace / "output" / ".mc-hanhua" / "translation-report.json"
        if nested.is_file():
            report_path = nested
    if not report_path.is_file():
        raise OfflineRecoveryError("暂存译文缺失，需要恢复翻译", missing="translation-report.json")
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    report_data = payload.get("report") or {}
    input_path = Path(str(report_data.get("input_path") or ""))
    if not input_path.exists():
        raise OfflineRecoveryError(
            "原始输入缺失，无法建立审计基线；请重新运行翻译", missing=str(input_path)
        )
    saved_units = payload.get("units") if isinstance(payload.get("units"), list) else []
    report_units = [_unit_from_report(item) for item in saved_units if isinstance(item, dict)]
    # Root units must be captured BEFORE the nested loop strips the "!/"
    # prefix from nested units (they are disjoint by construction).
    root_units = [unit for unit in report_units if "!/" not in unit.file_path]
    archive_results = [ArchiveResult.from_dict(item) for item in report_data.get("archive_results") or [] if isinstance(item, dict)]
    registry = default_registry()
    root_blocked = False
    with tempfile.TemporaryDirectory(prefix="mc-hanhua-reaudit-") as tmp:
        base_root = Path(tmp) / "input"
        copy_input(input_path, base_root, cancel_event=cancel_event)
        input_scan = scan(base_root, registry, cancel_event=cancel_event, allow_new_names=False)
        for result in archive_results:
            staging = Path(result.staging_path) if result.staging_path else workspace / "archives" / _safe_archive_id(result.archive_id) / "output"
            if not staging.is_dir():
                raise OfflineRecoveryError(f"暂存译文缺失，需要恢复翻译：{result.archive_id}", missing=str(staging))
            input_side = base_root / result.archive_id
            if not input_side.is_file():
                raise OfflineRecoveryError(
                    f"原始输入中找不到内嵌包，无法建立审计基线：{result.archive_id}",
                    missing=str(input_side),
                )
            _emit(progress_callback, "audit", f"正在重新审计 {result.archive_id}", 80, archive_id=result.archive_id, current_file=result.archive_id)
            nested_root = Path(tmp) / "nested" / _safe_archive_id(result.archive_id)
            copy_input(input_side, nested_root, cancel_event=cancel_event)
            nested_scan = scan(nested_root, registry, cancel_event=cancel_event, allow_new_names=False)
            nested_baseline = take_baseline(nested_root, nested_scan)
            prefix = f"{result.archive_id}!/"
            nested_units = []
            for unit in report_units:
                if unit.file_path.startswith(prefix):
                    unit.file_path = unit.file_path[len(prefix):]
                    nested_units.append(unit)
            try:
                audit = audit_output(staging, baseline=nested_baseline, baseline_scan=nested_scan, units=nested_units, registry=registry, cancel_event=cancel_event)
            finally:
                del nested_baseline
            result.audit_issues = list(audit.to_dict().get("errors") or []) + list(audit.to_dict().get("warnings") or [])
            if audit.blocked:
                result.status = "blocked"
                result.publication_status = "withheld"
            else:
                result.status = "ready"
                result.publication_status = "staged"
        output_root = workspace / "output"
        if not output_root.is_dir():
            raise OfflineRecoveryError("暂存译文缺失，需要恢复翻译", missing=str(output_root))
        _emit(progress_callback, "audit", "正在重新审计根输出", 82)
        input_baseline = take_baseline(base_root, input_scan)
        try:
            root_audit = audit_output(output_root, baseline=input_baseline, baseline_scan=input_scan, units=root_units, registry=registry, cancel_event=cancel_event)
        finally:
            del input_baseline
        root_blocked = root_audit.blocked
        report_data["recovery_audit"] = root_audit.to_dict()
        report_data["recovery_baseline"] = str(input_path)
        report_data["audit"] = root_audit.to_dict()
        report_data["archive_results"] = [item.to_dict() for item in archive_results]
        report_data["report_path"] = str(report_path)
        report_data["workspace_path"] = str(workspace)
    if root_blocked or any(item.publication_status == "withheld" for item in archive_results):
        report_data["audit_blocked"] = True
        report_data["publication_status"] = "withheld"
        report_data["recoverable"] = True
        payload["report"] = report_data
        _save_recovery_report(report_path, payload, output_archive)
        raise AuditBlockedError(
            "重新审计阻断，未发布最终输出",
            error_code="audit_blocked",
            phase="audit",
            report_path=str(report_path),
            workspace_path=str(workspace),
            recoverable=True,
        )
    for result in archive_results:
        if not result.archive_id or result.archive_id == "(root)":
            continue
        staging = Path(result.staging_path) if result.staging_path else workspace / "archives" / _safe_archive_id(result.archive_id) / "output"
        target = output_root / result.archive_id
        tmp_archive = workspace / f"{_safe_archive_id(result.archive_id)}.zip"
        _emit(progress_callback, "pack", f"正在重打 {result.archive_id}", 90, archive_id=result.archive_id)
        repack_directory(staging, tmp_archive, exclude_tool_metadata=True, cancel_event=cancel_event)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp_archive), target)
        result.status = "published"
        result.publication_status = "published"
    _preflight_output(output_root, output_archive)
    temp_archive = workspace / f"{output_archive.stem}.tmp{output_archive.suffix}"
    _emit(progress_callback, "pack", "正在压缩输出文件", 94)
    repack_directory(output_root, temp_archive, exclude_tool_metadata=True, cancel_event=cancel_event)
    _emit(progress_callback, "save", "正在保存输出文件", 98)
    temp_archive.replace(output_archive)
    report_data["archive_results"] = [item.to_dict() for item in archive_results]
    report_data["audit_blocked"] = False
    report_data["publication_status"] = "published"
    report_data["output_path"] = str(output_archive)
    report_data["recoverable"] = False
    payload["report"] = report_data
    report = _save_recovery_report(report_path, payload, output_archive)
    _emit(progress_callback, "complete", "汉化完成", 100)
    return report
