from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from .events import emit_event, filter_detail_limit, redact_text
from .models import RiskLevel, TextUnit
from .nbt import (
    TAG_COMPOUND,
    TAG_LIST,
    TAG_STRING,
    NbtTag,
    get_tag,
    iter_children,
    read_nbt_file,
    read_region_chunks,
    set_string,
    write_nbt_file,
    write_region_chunks,
)
from .plugins import ExtractorPlugin, WriterPlugin
from .policy import (
    GENERIC_JSON_TEXT_KEYS,
    POLICY_TRANSLATABLE,
    POLICY_UNKNOWN,
    apply_unit_policy,
    classify_json_pointer,
    classify_nbt_path,
)
from .text_components import (
    PLAYER_NAME_RE,
    apply_command_text,
    apply_json_text,
    collect_mechanism_refs,
    collect_selector_refs,
    command_locator_is_valid,
    command_locator_offset,
    command_targets_text_display,
    escape_pointer_part,
    extract_command_texts,
    extract_json_texts,
    get_json_pointer,
    read_command_text,
    set_json_pointer,
)
from .utils import detect_risk, dump_json, is_visual_glyph_art, load_json, looks_translatable, read_text, stable_id, write_text

LANG_PATTERN = re.compile(r"assets[/\\][^/\\]+[/\\]lang[/\\].+\.(json|lang)$", re.IGNORECASE)
SOURCE_LANGS = {"en_us"}
KEEP_ORIGINAL_KEY_PREFIXES = ("argument.id.", "jukebox_song.")
TARGET_LANG = "zh_cn"
NBT_SUFFIXES = {".dat", ".mca", ".mcr", ".nbt"}
NON_SOURCE_LOCALE_RE = re.compile(r"/(?!en_us)[a-z]{2}_[a-z]{2}/")
DATAPACK_NAMESPACE_RE = re.compile(r"(?:^|/)data/[^/]+/")


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


class JsonLangPlugin(ExtractorPlugin, WriterPlugin):
    name = "json-lang"

    def supports(self, path: Path) -> bool:
        return (
            path.suffix.lower() == ".json"
            and path.stem.lower() in SOURCE_LANGS
            and bool(LANG_PATTERN.search(path.as_posix()))
        )

    def extract(self, root: Path, path: Path) -> list[TextUnit]:
        self.last_filtered_counts: dict[str, int] = {}
        self.last_filtered_details: list[dict[str, object]] = []
        try:
            data = load_json(path)
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(data, dict):
            return []
        rel = _rel(root, path)
        units: list[TextUnit] = []
        for key, value in data.items():
            if key.startswith(KEEP_ORIGINAL_KEY_PREFIXES):
                _record_filtered(
                    self.last_filtered_counts,
                    self.last_filtered_details,
                    reason="keep_original_prefix",
                    file_path=rel,
                    format_name=self.name,
                    locator=key,
                    source=value if isinstance(value, str) else "",
                    extra_reason="language-file technical key prefix",
                )
                continue
            if isinstance(value, str) and looks_translatable(value):
                risk = RiskLevel(detect_risk(value, key))
                units.append(
                    apply_unit_policy(
                        TextUnit(
                            id=stable_id(rel, key, value),
                            source_text=value,
                            file_path=rel,
                            format=self.name,
                            locator=key,
                            context=key,
                            risk_level=risk,
                        )
                    )
                )
        return units

    def write(self, root: Path, path: Path, units: list[TextUnit]) -> int:
        target_path = path.with_name(f"{TARGET_LANG}.json")
        data = {}
        if target_path.exists():
            loaded = load_json(target_path)
            data = loaded if isinstance(loaded, dict) else {}
        changed = 0
        by_key = {unit.locator: unit for unit in units if unit.final_target is not None}
        for key, unit in by_key.items():
            if data.get(key) != unit.final_target:
                data[key] = unit.final_target
                changed += 1
        if changed:
            dump_json(target_path, data)
        return changed


class LegacyLangPlugin(ExtractorPlugin, WriterPlugin):
    name = "legacy-lang"

    def supports(self, path: Path) -> bool:
        return (
            path.suffix.lower() == ".lang"
            and path.stem.lower() in SOURCE_LANGS
            and bool(LANG_PATTERN.search(path.as_posix()))
        )

    def extract(self, root: Path, path: Path) -> list[TextUnit]:
        rel = _rel(root, path)
        self.last_filtered_counts: dict[str, int] = {}
        self.last_filtered_details: list[dict[str, object]] = []
        units: list[TextUnit] = []
        for index, line in enumerate(read_text(path).splitlines()):
            if not line or line.lstrip().startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if looks_translatable(value):
                risk = RiskLevel(detect_risk(value, key))
                units.append(
                    apply_unit_policy(
                        TextUnit(
                            id=stable_id(rel, str(index), key, value),
                            source_text=value,
                            file_path=rel,
                            format=self.name,
                            locator=str(index),
                            context=key,
                            risk_level=risk,
                            metadata={"key": key},
                        )
                    )
                )
        return units

    def write(self, root: Path, path: Path, units: list[TextUnit]) -> int:
        target_path = path.with_name(f"{TARGET_LANG}.lang")
        existing: dict[str, str] = {}
        if target_path.exists():
            for line in read_text(target_path).splitlines():
                if line and not line.lstrip().startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    existing[key] = value
        changed = 0
        for unit in units:
            key = str(unit.metadata.get("key", ""))
            if key and unit.final_target is not None and existing.get(key) != unit.final_target:
                existing[key] = unit.final_target
                changed += 1
        if changed:
            write_text(target_path, "\n".join(f"{key}={value}" for key, value in existing.items()) + "\n")
        return changed


class GenericJsonTextPlugin(ExtractorPlugin, WriterPlugin):
    name = "generic-json"

    # Keep extraction and policy vocabulary synchronized.  ``texts`` remains
    # an extractor-only compatibility key because policy must classify its
    # array items from the full pointer below.
    TEXT_KEYS = GENERIC_JSON_TEXT_KEYS | {"texts"}

    def supports(self, path: Path) -> bool:
        if path.suffix.lower() != ".json":
            return False
        normalized = path.as_posix().lower()
        if LANG_PATTERN.search(normalized):
            return False
        if NON_SOURCE_LOCALE_RE.search(normalized):
            return False
        interesting = (
            "/quests/",
            "/ftbquests/",
            "/patchouli_books/",
            "/advancement/",
            "/advancements/",
            "/dialog/",
            "/datapacks/",
            "/kubejs/",
            "/scripts/",
            "/config/",
            "/defaultconfigs/",
        )
        return any(part in normalized for part in interesting) or bool(DATAPACK_NAMESPACE_RE.search(normalized))

    def extract(self, root: Path, path: Path) -> list[TextUnit]:
        self.last_filtered_counts: dict[str, int] = {}
        self.last_filtered_details: list[dict[str, object]] = []
        try:
            data = load_json(path)
        except (OSError, json.JSONDecodeError):
            return []
        rel = _rel(root, path)
        units: list[TextUnit] = []

        def filtered(reason: str, locator: str, source: str, *, policy: str | None = None, policy_reason: str | None = None) -> None:
            classified_policy, classified_reason = classify_json_pointer(locator, format_name=self.name, reason_hint=reason)
            policy = policy or classified_policy
            policy_reason = policy_reason or classified_reason
            _record_filtered(
                self.last_filtered_counts,
                self.last_filtered_details,
                reason=reason,
                file_path=rel,
                format_name=self.name,
                locator=locator,
                source=source,
                extra_reason=policy_reason,
                policy=policy,
            )

        def walk(value: Any, pointer: str, parent_key: str = "", skip_reason: str = "") -> None:
            if isinstance(value, dict):
                external_action = _is_external_url_action(value)
                for key, child in value.items():
                    child_skip = "external_link_label" if external_action and str(key).lower() == "label" else skip_reason
                    walk(child, f"{pointer}/{escape_pointer_part(str(key))}", str(key), child_skip)
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    walk(child, f"{pointer}/{index}", parent_key, skip_reason)
            elif isinstance(value, str):
                key_is_textual = parent_key.lower() in self.TEXT_KEYS
                if not key_is_textual:
                    return
                if skip_reason:
                    filtered(skip_reason, pointer, value)
                    return
                if is_visual_glyph_art(value):
                    filtered("visual_glyph", pointer, value)
                    return
                if looks_translatable(value):
                    risk = RiskLevel(detect_risk(value, pointer))
                    unit = apply_unit_policy(
                        TextUnit(
                            id=stable_id(rel, pointer, value),
                            source_text=value,
                            file_path=rel,
                            format=self.name,
                            locator=pointer,
                            context=pointer,
                            risk_level=risk,
                        )
                    )
                    # The extractor is the final boundary before translation;
                    # mechanism payloads must not become writable units even
                    # if a caller later supplies a target by hand.
                    if unit.policy != POLICY_TRANSLATABLE:
                        filtered("policy_blocked", pointer, value, policy=unit.policy, policy_reason=unit.policy_reason)
                        return
                    units.append(unit)

        walk(data, "")
        return units

    def write(self, root: Path, path: Path, units: list[TextUnit]) -> int:
        data = load_json(path)
        changed = 0
        for unit in units:
            if unit.final_target is None:
                continue
            set_json_pointer(data, unit.locator, unit.final_target)
            changed += 1
        if changed:
            dump_json(path, data)
        return changed


def _is_external_url_action(value: dict[str, Any]) -> bool:
    for key in ("action", "clickEvent", "click_event"):
        action = value.get(key)
        if not isinstance(action, dict):
            continue
        action_type = str(action.get("type") or action.get("action") or "").lower()
        url = action.get("url") or action.get("value")
        if action_type == "open_url" and isinstance(url, str) and url.startswith(("http://", "https://")):
            return True
    return False


def _emit_mcfunction_writeback_failed(unit: TextUnit, reason: str) -> None:
    """Record a command write-back that could not locate its payload."""

    emit_event(
        stage="writeback",
        file=unit.file_path,
        format=unit.format,
        locator=unit.locator,
        context=unit.context,
        unit_id=unit.id,
        source=unit.source_text,
        policy=unit.policy,
        writeback_status="failed",
        writeback_target=unit.final_target or "",
        writeback_changed=False,
        extra={"reason": reason},
    )


class McfunctionPlugin(ExtractorPlugin, WriterPlugin):
    name = "mcfunction"

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() == ".mcfunction"

    def extract(self, root: Path, path: Path) -> list[TextUnit]:
        rel = _rel(root, path)
        self.last_selector_refs: set[str] = set()
        self.last_mechanism_refs: set[str] = set()
        self.last_commands: list[tuple[str, str]] = []
        self.last_filtered_counts: dict[str, int] = {}
        self.last_filtered_details: list[dict[str, object]] = []
        units: list[TextUnit] = []
        for line_no, line in enumerate(read_text(path).splitlines()):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            self.last_selector_refs.update(collect_selector_refs(line))
            self.last_selector_refs.update(collect_mechanism_refs(line))
            self.last_mechanism_refs.update(collect_mechanism_refs(line))
            self.last_commands.append((rel, line))
            for command_locator, text in extract_command_texts(
                line, self.last_filtered_counts, filtered_details=self.last_filtered_details, file_path=rel
            ):
                locator = json.dumps({"line": line_no, "command": command_locator}, ensure_ascii=False)
                metadata = {"layout_container": "text-display"} if command_targets_text_display(line) else {}
                units.append(
                    apply_unit_policy(
                        TextUnit(
                            id=stable_id(rel, locator, text),
                            source_text=text,
                            file_path=rel,
                            format=self.name,
                            locator=locator,
                            context=f"line:{line_no + 1}",
                            risk_level=RiskLevel.HIGH,
                            metadata=metadata,
                        )
                    )
                )
        return units

    def write(self, root: Path, path: Path, units: list[TextUnit]) -> int:
        lines = read_text(path).splitlines()
        changed = 0
        by_line: dict[int, list[tuple[TextUnit, dict[str, object]]]] = defaultdict(list)
        for unit in units:
            if unit.final_target is not None:
                locator = json.loads(unit.locator)
                by_line[int(locator["line"])].append((unit, locator["command"]))
        for line_no, line_units in by_line.items():
            if not (0 <= line_no < len(lines)):
                for unit, _command_locator in line_units:
                    unit.metadata["writeback_status"] = "failed"
                    _emit_mcfunction_writeback_failed(unit, reason="line_missing")
                continue
            for unit, command_locator in sorted(line_units, key=lambda item: command_locator_offset(item[1]), reverse=True):
                if not command_locator_is_valid(lines[line_no], command_locator):
                    unit.metadata["writeback_status"] = "failed"
                    _emit_mcfunction_writeback_failed(unit, reason="locator_missing")
                    continue
                original = lines[line_no]
                new_line = apply_command_text(original, command_locator, unit.final_target or "")
                unit.metadata["writeback_status"] = "changed" if new_line != original else "unchanged"
                if new_line != original:
                    lines[line_no] = new_line
                    changed += 1
        if changed:
            write_text(path, "\n".join(lines) + "\n")
        return changed


class NbtTextPlugin(ExtractorPlugin, WriterPlugin):
    name = "nbt-text"

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in NBT_SUFFIXES

    def extract(self, root: Path, path: Path) -> list[TextUnit]:
        rel = _rel(root, path)
        self.last_filtered_counts: dict[str, int] = {}
        self.last_filtered_details: list[dict[str, object]] = []
        self.last_selector_refs: set[str] = set()
        self.last_player_names: set[str] = set()
        self.last_custom_names: set[str] = set()
        self.last_mechanism_refs: set[str] = set()
        self.last_commands: list[tuple[str, str]] = []
        self.last_warnings: list[str] = []
        try:
            if path.suffix.lower() == ".mca":
                units: list[TextUnit] = []
                for chunk in read_region_chunks(path):
                    chunk_id = str(chunk["index"])
                    units.extend(
                        _collect_nbt_units(
                            rel,
                            chunk["doc"].root,
                            chunk_id=chunk_id,
                            filtered_counts=self.last_filtered_counts,
                            filtered_details=self.last_filtered_details,
                            selector_refs=self.last_selector_refs,
                            player_names=self.last_player_names,
                            custom_names=self.last_custom_names,
                            mechanism_refs=self.last_mechanism_refs,
                            commands=self.last_commands,
                        )
                    )
                return units
            doc = read_nbt_file(path)
            return _collect_nbt_units(
                rel,
                doc.root,
                filtered_counts=self.last_filtered_counts,
                filtered_details=self.last_filtered_details,
                selector_refs=self.last_selector_refs,
                player_names=self.last_player_names,
                custom_names=self.last_custom_names,
                mechanism_refs=self.last_mechanism_refs,
                commands=self.last_commands,
            )
        except Exception as exc:
            if path.suffix.lower() == ".mca" and path.stat().st_size >= 8192:
                raise
            # Sub-header region stubs (0-byte poi/entities placeholders) cannot
            # hold any chunk, so nothing can be lost by skipping them; and
            # standalone .dat/.nbt parse failures must stay visible in the
            # report instead of silently dropping the file's text.
            self.last_filtered_counts["nbt_parse_error"] = self.last_filtered_counts.get("nbt_parse_error", 0) + 1
            _record_filtered(
                self.last_filtered_counts,
                self.last_filtered_details,
                reason="nbt_parse_error",
                file_path=rel,
                format_name=self.name,
                locator="",
                source="",
                extra_reason=str(exc),
                already_counted=True,
            )
            self.last_warnings.append(f"无法解析 NBT 文件，已跳过：{rel}（{exc}）")
            return []

    def write(self, root: Path, path: Path, units: list[TextUnit]) -> int:
        if not units:
            return 0
        rel = _rel(root, path)
        if path.suffix.lower() == ".mca":
            chunks = read_region_chunks(path)
            by_chunk: dict[str, list[TextUnit]] = {}
            for unit in units:
                locator = json.loads(unit.locator)
                by_chunk.setdefault(str(locator.get("chunk", "")), []).append(unit)
            changed = 0
            for chunk in chunks:
                chunk_units = by_chunk.get(str(chunk["index"]), [])
                if _apply_nbt_units(chunk["doc"].root, chunk_units, file_path=rel):
                    chunk["changed"] = True
                    changed += 1
            if changed:
                write_region_chunks(path, chunks)
            return changed

        doc = read_nbt_file(path)
        changed = _apply_nbt_units(doc.root, units, file_path=rel)
        if changed:
            write_nbt_file(path, doc)
        return 1 if changed else 0


def _collect_nbt_units(
    rel: str,
    root: NbtTag,
    chunk_id: str | None = None,
    filtered_counts: dict[str, int] | None = None,
    filtered_details: list[dict[str, object]] | None = None,
    selector_refs: set[str] | None = None,
    player_names: set[str] | None = None,
    custom_names: set[str] | None = None,
    mechanism_refs: set[str] | None = None,
    commands: list[tuple[str, str]] | None = None,
) -> list[TextUnit]:
    units: list[TextUnit] = []
    standalone = chunk_id is None
    structure_like = _is_structure_nbt_path(rel)

    def filtered(reason: str, path: list[str | int], source: str = "") -> None:
        policy, policy_reason = classify_nbt_path(path, reason_hint=reason)
        _record_filtered(
            filtered_counts,
            filtered_details,
            reason=reason,
            file_path=rel,
            format_name=NbtTextPlugin.name,
            locator=json.dumps({"path": path}, ensure_ascii=False),
            source=source,
            extra_reason=policy_reason,
            policy=policy,
        )

    def walk(tag: NbtTag, path: list[str | int]) -> None:
        if tag.type_id == TAG_STRING and _is_command_feedback_path(path):
            filtered("command_feedback", path, tag.value)
            return
        if tag.type_id == TAG_STRING and _is_author_path(path):
            filtered("author_name", path, tag.value)
            return
        if tag.type_id == TAG_STRING and path and str(path[-1]).lower() == "customname" and _looks_like_internal_id(_decode_custom_name_value(tag.value)):
            filtered("entity_name_preserved", path, tag.value)
            if custom_names is not None:
                custom_names.add(_decode_custom_name_value(tag.value))
            return
        if tag.type_id == TAG_STRING and (player_name := _player_name_of(path, tag.value)):
            filtered("player_name", path, tag.value)
            if player_names is not None:
                player_names.add(player_name)
            return
        if tag.type_id == TAG_STRING and path and str(path[-1]).lower() == "customname":
            # Ordinary entity CustomName can be referenced by @e[name=...] or
            # execute if/unless data. Player-shaped names stay filtered here;
            # natural-language names are extracted as display candidates and
            # only locked after the full-pack reference index is complete.
            # Namespaced item components (minecraft:custom_name) are display
            # text and are still extracted below.
            if custom_names is not None:
                custom_names.add(_decode_custom_name_value(tag.value))
            if _looks_like_internal_id(_decode_custom_name_value(tag.value)):
                filtered("entity_name_preserved", path, tag.value)
                return
            if not _looks_like_natural_display(_decode_custom_name_value(tag.value)):
                filtered("entity_name_preserved", path, tag.value)
                return
            _add_entity_display_name_units(rel, units, root, path, tag.value, chunk_id)
            return
        if tag.type_id == TAG_STRING and _is_safe_nbt_text_path(path, tag.value, standalone=standalone, structure_like=structure_like):
            _add_nbt_string_units(rel, units, root, path, tag.value, chunk_id)
            return
        if tag.type_id == TAG_STRING and path and str(path[-1]).lower() == "command":
            if selector_refs is not None:
                selector_refs.update(collect_selector_refs(tag.value))
                selector_refs.update(collect_mechanism_refs(tag.value))
            if mechanism_refs is not None:
                mechanism_refs.update(collect_mechanism_refs(tag.value))
            if commands is not None:
                commands.append((rel, tag.value))
            for command_locator, text in extract_command_texts(
                tag.value, filtered_counts, filtered_details=filtered_details, file_path=rel
            ):
                locator = {"path": path, "command": command_locator}
                if chunk_id is not None:
                    locator["chunk"] = chunk_id
                units.append(
                    apply_unit_policy(
                        TextUnit(
                            id=stable_id(rel, json.dumps(locator, ensure_ascii=False), text),
                            source_text=text,
                            file_path=rel,
                            format=NbtTextPlugin.name,
                            locator=json.dumps(locator, ensure_ascii=False),
                            context="/".join(map(str, path)),
                            risk_level=RiskLevel.HIGH,
                        )
                    )
                )
            return
        if (
            standalone
            and structure_like
            and tag.type_id == TAG_STRING
            and looks_translatable(tag.value)
            and not _is_safe_nbt_text_path(path, tag.value, standalone=standalone, structure_like=structure_like)
        ):
            # Conservative: unknown custom fields in standalone structure NBT
            # stay untranslated and are recorded for review. Region/level trees
            # are too noisy for a default walk of every leftover string.
            filtered("unknown_mechanism_field", path, tag.value)
            return
        for child_key, child in iter_children(tag):
            walk(child, [*path, child_key])

    walk(root, [])

    def visible(tag):
        if tag.type_id == TAG_STRING:
            return tag.value
        if tag.type_id == TAG_COMPOUND:
            return ''.join(visible(tag.value[k]) for k in ('raw', 'text', '', 'extra', 'with') if k in tag.value)
        if tag.type_id == TAG_LIST:
            return ''.join(visible(child) for _, child in iter_children(tag))
        return ''

    for unit in units:
        locator = json.loads(unit.locator)
        path = locator.get('path', [])
        if 'pages' in path:
            index = path.index('pages')
            pages = get_tag(root, path[:index + 1])
            page = int(path[index + 1])
            context = []
            for number, tag in iter_children(pages):
                if abs(int(number) - page) <= 1:
                    context.append(f'第{int(number) + 1}页：{visible(tag)}')
            unit.metadata['book_reading_context'] = '\n'.join(context)
            unit.metadata['book_page'] = page + 1
    return units


def _entity_display_metadata(root: NbtTag, path: list[str | int], chunk_id: str | None) -> dict[str, object]:
    """Keep entity type / visibility / NBT path for later display-name policy."""

    metadata: dict[str, object] = {
        "display_kind": "entity_display_name",
        "nbt_path": list(path),
    }
    if chunk_id is not None:
        metadata["chunk"] = chunk_id
    entity_path: list[str | int] = []
    for index, part in enumerate(path):
        entity_path.append(part)
        if isinstance(part, int) and index > 0:
            break
    if not entity_path:
        entity_path = list(path[:-1])
    try:
        entity = get_tag(root, entity_path).value if entity_path else {}
    except (KeyError, IndexError, TypeError, AttributeError):
        entity = {}
    if isinstance(entity, dict):
        entity_id = entity.get("id")
        if isinstance(entity_id, NbtTag) and isinstance(entity_id.value, str):
            metadata["entity_type"] = entity_id.value
        elif isinstance(entity_id, str):
            metadata["entity_type"] = entity_id
        visible = entity.get("CustomNameVisible")
        if isinstance(visible, NbtTag):
            metadata["custom_name_visible"] = bool(visible.value)
        elif visible is not None:
            metadata["custom_name_visible"] = bool(visible)
    lowered = {str(part).lower() for part in path}
    if "entities" in lowered:
        metadata["entity_container"] = "entities"
    elif "block_entities" in lowered:
        metadata["entity_container"] = "block_entities"
    return metadata


def _add_entity_display_name_units(
    rel: str,
    units: list[TextUnit],
    root: NbtTag,
    path: list[str | int],
    value: str,
    chunk_id: str | None,
) -> None:
    display = _decode_custom_name_value(value)
    json_texts = extract_json_texts(value)
    texts = [text for _pointer, text in json_texts] if json_texts else ([display] if looks_translatable(display) else [])
    if not texts:
        return
    metadata = _entity_display_metadata(root, path, chunk_id)
    for text in texts:
        locator: dict[str, object] = {"path": path}
        if chunk_id is not None:
            locator["chunk"] = chunk_id
        unit = apply_unit_policy(
            TextUnit(
                id=stable_id(rel, json.dumps(locator, ensure_ascii=False), text),
                source_text=text,
                file_path=rel,
                format=NbtTextPlugin.name,
                locator=json.dumps(locator, ensure_ascii=False),
                context="/".join(map(str, path)),
                risk_level=RiskLevel.MEDIUM,
                metadata=metadata,
            )
        )
        unit.policy = POLICY_UNKNOWN
        unit.policy_reason = "entity display name pending full-pack reference scan"
        unit.metadata["policy"] = unit.policy
        unit.metadata["policy_reason"] = unit.policy_reason
        units.append(unit)


def _add_nbt_string_units(
    rel: str,
    units: list[TextUnit],
    root: NbtTag,
    path: list[str | int],
    value: str,
    chunk_id: str | None,
) -> None:
    layout_metadata = _nbt_layout_metadata(root, path, chunk_id)
    json_texts = extract_json_texts(value)
    if json_texts:
        for pointer, text in json_texts:
            locator: dict[str, object] = {"path": path, "json": pointer}
            if chunk_id is not None:
                locator["chunk"] = chunk_id
            units.append(
                apply_unit_policy(
                    TextUnit(
                        id=stable_id(rel, json.dumps(locator, ensure_ascii=False), text),
                        source_text=text,
                        file_path=rel,
                        format=NbtTextPlugin.name,
                        locator=json.dumps(locator, ensure_ascii=False),
                        context="/".join(map(str, path)),
                        risk_level=RiskLevel.MEDIUM,
                        metadata=dict(layout_metadata),
                    )
                )
            )
        return

    # If the string is a JSON container or a bare JSON string literal but
    # extract_json_texts found nothing translatable, don't treat the whole
    # JSON (including its quoting) as plain text.
    stripped = value.strip()
    if stripped.startswith(("{", "[", '"')):
        try:
            json.loads(stripped)
            return
        except json.JSONDecodeError:
            pass

    if looks_translatable(value):
        locator = {"path": path}
        if chunk_id is not None:
            locator["chunk"] = chunk_id
        units.append(
            apply_unit_policy(
                TextUnit(
                    id=stable_id(rel, json.dumps(locator, ensure_ascii=False), value),
                    source_text=value,
                    file_path=rel,
                    format=NbtTextPlugin.name,
                    locator=json.dumps(locator, ensure_ascii=False),
                    context="/".join(map(str, path)),
                    risk_level=RiskLevel.MEDIUM,
                    metadata=layout_metadata,
                )
            )
        )


def _nbt_layout_metadata(root: NbtTag, path: list[str | int], chunk_id: str | None) -> dict[str, object]:
    """Expose map layout ownership without changing the NBT locator contract."""

    lowered = [str(part).lower() for part in path]
    if "block_entities" in lowered:
        index = lowered.index("block_entities")
        if index + 1 < len(path) and isinstance(path[index + 1], int):
            entity_path = path[: index + 2]
            try:
                entity = get_tag(root, entity_path).value
                side_index = next((offset for offset, part in enumerate(lowered) if part in {"front_text", "back_text"}), None)
                if side_index is not None:
                    side = str(path[side_index])
                    line = 0
                    if "messages" in lowered:
                        message_index = lowered.index("messages")
                        if message_index + 1 < len(path) and isinstance(path[message_index + 1], int):
                            line = int(path[message_index + 1])
                    position = _nbt_block_position(entity)
                    owner = f"{chunk_id or 'root'}:{entity_path[-1]}:{side}"
                    return {
                        "layout_container": "sign",
                        "layout_owner": owner,
                        "layout_line": line,
                        "layout_position": position,
                    }
            except (KeyError, TypeError):
                pass

    if "entities" in lowered:
        index = lowered.index("entities")
        if index + 1 < len(path) and isinstance(path[index + 1], int):
            entity_path = path[: index + 2]
            try:
                entity = get_tag(root, entity_path).value
                entity_id = str(entity.get("id").value) if entity.get("id") else ""
                if entity_id.lower() == "minecraft:text_display":
                    owner = f"{chunk_id or 'root'}:{entity_path[-1]}"
                    return {
                        "layout_container": "text-display",
                        "layout_owner": owner,
                        "layout_slot": _text_display_slot(path),
                    }
            except (KeyError, TypeError):
                pass
    return {}


def _nbt_block_position(entity: dict[str, NbtTag]) -> tuple[int, int, int] | None:
    try:
        return tuple(int(entity[key].value) for key in ("x", "y", "z"))
    except (KeyError, TypeError, ValueError):
        return None


def _text_display_slot(path: list[str | int]) -> tuple[int, ...]:
    lowered = [str(part).lower() for part in path]
    if "extra" not in lowered:
        return (0,)
    index = lowered.index("extra")
    if index + 1 < len(path) and isinstance(path[index + 1], int):
        return (1, int(path[index + 1]))
    return (1, 0)


def _apply_nbt_units(root: NbtTag, units: list[TextUnit], file_path: str = "") -> bool:
    changed = False
    by_path: dict[str, list[tuple[TextUnit, dict[str, object]]]] = defaultdict(list)
    for unit in units:
        if unit.final_target is not None:
            locator = json.loads(unit.locator)
            by_path[json.dumps(locator["path"], ensure_ascii=False)].append((unit, locator))

    for path_units in by_path.values():
        for unit, locator in sorted(
            path_units,
            key=lambda item: command_locator_offset(item[1].get("command", {})) if "command" in item[1] else -1,
            reverse=True,
        ):
            path = locator["path"]
            try:
                tag = get_tag(root, path)
            except (KeyError, TypeError):
                unit.metadata["writeback_status"] = "failed"
                emit_event(
                    stage="writeback",
                    file=file_path or unit.file_path,
                    format=unit.format,
                    locator=unit.locator,
                    context=unit.context,
                    unit_id=unit.id,
                    source=unit.source_text,
                    policy=unit.policy,
                    writeback_status="failed",
                    writeback_target=unit.final_target or "",
                    writeback_changed=False,
                    extra={"reason": "locator_missing"},
                )
                continue
            if tag.type_id != TAG_STRING:
                unit.metadata["writeback_status"] = "failed"
                emit_event(
                    stage="writeback",
                    file=file_path or unit.file_path,
                    format=unit.format,
                    locator=unit.locator,
                    context=unit.context,
                    unit_id=unit.id,
                    source=unit.source_text,
                    policy=unit.policy,
                    writeback_status="failed",
                    writeback_target=unit.final_target or "",
                    writeback_changed=False,
                    extra={"reason": "not_string"},
                )
                continue
            if "json" in locator:
                new_value = apply_json_text(tag.value, str(locator["json"]), unit.final_target)
            elif "command" in locator:
                new_value = apply_command_text(tag.value, locator["command"], unit.final_target)
            else:
                new_value = unit.final_target
            did_change = new_value != tag.value
            if did_change:
                set_string(root, path, new_value)
                changed = True
            unit.metadata["writeback_status"] = "changed" if did_change else "unchanged"
            emit_event(
                stage="writeback",
                file=file_path or unit.file_path,
                format=unit.format,
                locator=unit.locator,
                context=unit.context,
                unit_id=unit.id,
                source=unit.source_text,
                policy=unit.policy,
                writeback_status=unit.metadata["writeback_status"],
                writeback_target=unit.final_target or "",
                writeback_changed=did_change,
                final=unit.final_target or "",
            )
    return changed


def _is_structure_nbt_path(rel: str) -> bool:
    normalized = rel.replace("\\", "/").lower()
    return (
        normalized.endswith(".nbt")
        and (
            "/structure/" in f"/{normalized}"
            or "/structures/" in f"/{normalized}"
            or normalized.startswith("generated/")
        )
    )


def _is_safe_nbt_text_path(
    path: list[str | int],
    value: str,
    *,
    standalone: bool = False,
    structure_like: bool = False,
) -> bool:
    lowered_path = [str(part).lower() for part in path]
    lowered_set = set(lowered_path)
    if "lastoutput" in lowered_set:
        return False
    # List-index extras and Wiki array bare strings (NBT empty-name keys).
    # extra/1/"" has an int parent, so walk back to the nearest string ancestor.
    anonymous_leaf = not path or not isinstance(path[-1], str) or path[-1] == ""
    if anonymous_leaf:
        parent = ""
        for part in reversed(path[:-1] if path else []):
            if isinstance(part, str) and part != "":
                parent = part.lower()
                break
        return parent in {"pages", "filtered_pages", "lore", "messages", "extra", "minecraft:lore", "raw", "filtered"} or any(
            marker in lowered_set
            for marker in {
                "minecraft:written_book_content",
                "minecraft:writable_book_content",
                "minecraft:lore",
            }
        )
    key = str(path[-1])
    key_lower = key.lower()
    if key in {"Text1", "Text2", "Text3", "Text4"}:
        return True
    if key_lower == "displayname" and ({"objectives", "teams", "bossbars"} & lowered_set):
        return _looks_like_visible_label(value) or value.strip().startswith(("{", "["))
    if key_lower in {"prefix", "suffix"} and "teams" in lowered_set:
        return True
    if key_lower in {"minecraft:custom_name", "minecraft:item_name", "minecraft:lore"}:
        return True
    if key_lower in {"raw", "filtered"} and (
        {
            "minecraft:written_book_content",
            "minecraft:writable_book_content",
            "pages",
            "filtered_pages",
            "title",
            "written_book_content",
            "writable_book_content",
        }
        & lowered_set
    ):
        return True
    if key_lower in {"text", "fallback"} and _is_text_component_path(lowered_path):
        return True
    if standalone and structure_like:
        # Structure templates (generated/*/structures/*.nbt) store book pages
        # and TextDisplay text outside region entity lists. Only the audited
        # display keys below are unlocked; everything else stays UNKNOWN.
        if key_lower in {"text", "fallback"}:
            return True
        if key_lower in {"title", "subtitle"}:
            return True
        if key_lower in {"raw", "filtered"} and (
            {"pages", "filtered_pages", "title", "minecraft:written_book_content", "minecraft:writable_book_content"} & lowered_set
        ):
            return True
    if key_lower in {"displayname", "title", "subtitle"}:
        return True
    if key == "Name" and "display" in lowered_set:
        return True
    if key_lower in {"pages", "filtered_pages"}:
        return False
    if key_lower == "command":
        return False
    return value.strip().startswith(("{", "[")) and bool(extract_json_texts(value))


def _is_command_feedback_path(path: list[str | int]) -> bool:
    return any(str(part).lower() == "lastoutput" for part in path)


def _is_author_path(path: list[str | int]) -> bool:
    """Book authors are proper nouns and clear-command matching fields."""

    return any(str(part).lower() == "author" for part in path)


def _player_name_of(path: list[str | int], value: str) -> str | None:
    """Return the decoded name when an entity is named like a player.

    Scoped to entities (not chest/level display names): a single-word entity
    name like "Nether" can still be legitimate display text elsewhere.
    Translating player-shaped entity names also breaks @e[name=...] selectors.
    """

    if not path or str(path[-1]).lower() != "customname":
        return None
    if "entities" not in {str(part).lower() for part in path}:
        return None
    candidate = value.strip()
    if candidate.startswith(("{", "[", '"')):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, dict) and isinstance(parsed.get("text"), str):
            candidate = parsed["text"].strip()
        elif isinstance(parsed, str):
            candidate = parsed.strip()
        else:
            return None
    return candidate if PLAYER_NAME_RE.fullmatch(candidate) else None


def _decode_custom_name_value(value: str) -> str:
    """Decode a preserved CustomName to the visible string for reference counts."""

    candidate = value.strip()
    if candidate.startswith(("{", "[", '"')):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            return value
        if isinstance(parsed, dict) and isinstance(parsed.get("text"), str):
            return parsed["text"]
        if isinstance(parsed, str):
            return parsed
        return value
    return value


def _is_text_component_path(lowered_path: list[str]) -> bool:
    lowered_set = set(lowered_path)
    if "front_text" in lowered_set or "back_text" in lowered_set:
        return "messages" in lowered_set
    if lowered_path.count("text") >= 2:
        return True
    if {
        "minecraft:custom_name",
        "minecraft:item_name",
        "minecraft:lore",
        "minecraft:written_book_content",
        "minecraft:writable_book_content",
    } & lowered_set:
        return True
    if "raw" in lowered_set or "extra" in lowered_set:
        return True
    # Region / structure entity text components: Entities/N/text may be a
    # plain string ("Welcome!!") or a compound. Wiki stores the component
    # on the text_display `text` tag in either shape.
    if lowered_path and lowered_path[-1] in {"text", "fallback"} and "entities" in lowered_set:
        return True
    return False


def _record_filtered(
    counts: dict[str, int] | None,
    details: list[dict[str, object]] | None,
    *,
    reason: str,
    file_path: str,
    format_name: str,
    locator: str,
    source: object = "",
    extra_reason: str = "",
    policy: str = "",
    already_counted: bool = False,
) -> None:
    if counts is not None and not already_counted:
        counts[reason] = counts.get(reason, 0) + 1
    policy_reason = extra_reason or reason
    if details is not None and len(details) < filter_detail_limit():
        details.append(
            {
                "reason": reason,
                "file": file_path,
                "format": format_name,
                "locator": redact_text(locator),
                "source": redact_text(source),
                "policy": policy,
                "policy_reason": redact_text(policy_reason, 200),
            }
        )
    emit_event(
        stage="filter",
        file=file_path,
        format=format_name,
        locator=str(locator),
        source=source,
        policy=policy,
        policy_reason=policy_reason,
        extra={"reason": reason},
    )


def _looks_like_visible_label(value: str) -> bool:
    stripped = value.strip()
    if not stripped:
        return False
    if " " in stripped:
        return True
    if stripped.lower() != stripped:
        return True
    return len(stripped) > 10


_INTERNAL_ID_RE = re.compile(r"^[A-Za-z]+[_\-]\d+[A-Za-z0-9_\-]*$")
_INTERNAL_CODE_RE = re.compile(r"^[A-Za-z0-9]+(?:[_\-][A-Za-z0-9]+){2,}$")


def _looks_like_internal_id(value: str) -> bool:
    stripped = value.strip()
    if not stripped or " " in stripped:
        return False
    if _INTERNAL_ID_RE.fullmatch(stripped) or _INTERNAL_CODE_RE.fullmatch(stripped):
        return True
    return bool(re.fullmatch(r"[A-Za-z0-9_\-]{1,24}", stripped)) and ("_" in stripped or "-" in stripped)


def _looks_like_natural_display(value: str) -> bool:
    stripped = value.strip()
    if not stripped or _looks_like_internal_id(stripped):
        return False
    if " " in stripped:
        return True
    if any("a" <= char.lower() <= "z" for char in stripped) and stripped.lower() != stripped:
        return len(stripped) >= 3
    return looks_translatable(stripped) and len(stripped) >= 8


class LocatedTextReadError(Exception):
    """Raised when a unit locator cannot be resolved in the output copy."""

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(message or reason)
        self.reason = reason


def read_located_text(root: Path, unit: TextUnit, *, cache: dict[str, Any] | None = None) -> str:
    """Read the written field for ``unit`` without using looks_translatable.

    Language files are read from the sibling zh_cn file. JSON uses the unit
    pointer. NBT uses chunk/path (and optional JSON/command sub-locator).
    Commands use the structural locator, never a drifting byte-only offset.
    """

    path = root / unit.file_path
    if unit.format in {"json-lang", "legacy-lang"}:
        return _read_language_located_text(path, unit)
    if unit.format == "generic-json":
        return _read_generic_json_located_text(path, unit)
    if unit.format == "mcfunction":
        return _read_mcfunction_located_text(path, unit)
    if unit.format == "nbt-text":
        return _read_nbt_located_text(path, unit, cache=cache)
    raise LocatedTextReadError("unsupported_format", f"unsupported format {unit.format}")


def _read_language_located_text(path: Path, unit: TextUnit) -> str:
    suffix = ".json" if unit.format == "json-lang" else ".lang"
    target_path = path.with_name(f"{TARGET_LANG}{suffix}")
    if not target_path.is_file():
        raise LocatedTextReadError("missing_file", f"missing {target_path.name}")
    if unit.format == "json-lang":
        try:
            data = load_json(target_path)
        except (OSError, json.JSONDecodeError) as exc:
            raise LocatedTextReadError("source_corrupt", str(exc)) from exc
        if not isinstance(data, dict):
            raise LocatedTextReadError("source_corrupt", "language file is not an object")
        value = data.get(unit.locator)
        if not isinstance(value, str):
            raise LocatedTextReadError("locator_missing", f"missing key {unit.locator}")
        return value
    key = str(unit.metadata.get("key") or unit.context or "")
    if not key:
        raise LocatedTextReadError("locator_missing", "legacy language key missing")
    for line in read_text(target_path).splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        line_key, value = line.split("=", 1)
        if line_key == key:
            return value
    raise LocatedTextReadError("locator_missing", f"missing key {key}")


def _read_generic_json_located_text(path: Path, unit: TextUnit) -> str:
    if not path.is_file():
        raise LocatedTextReadError("missing_file", str(path))
    try:
        data = load_json(path)
        found = get_json_pointer(data, unit.locator)
    except FileNotFoundError as exc:
        raise LocatedTextReadError("missing_file", str(exc)) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise LocatedTextReadError("source_corrupt", str(exc)) from exc
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise LocatedTextReadError("locator_missing", str(exc)) from exc
    if not isinstance(found, str):
        raise LocatedTextReadError("locator_missing", "JSON pointer is not a string")
    return found


def _read_mcfunction_located_text(path: Path, unit: TextUnit) -> str:
    if not path.is_file():
        raise LocatedTextReadError("missing_file", str(path))
    try:
        locator = json.loads(unit.locator)
        line_no = int(locator["line"])
        command_locator = locator["command"]
    except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise LocatedTextReadError("locator_missing", str(exc)) from exc
    lines = read_text(path).splitlines()
    if not (0 <= line_no < len(lines)):
        raise LocatedTextReadError("locator_missing", f"line {line_no} missing")
    if not isinstance(command_locator, dict):
        raise LocatedTextReadError("locator_missing", "command locator missing")
    found = read_command_text(lines[line_no], command_locator)
    if found is None:
        raise LocatedTextReadError("locator_missing", "command leaf missing")
    return found


def _read_nbt_located_text(path: Path, unit: TextUnit, *, cache: dict[str, Any] | None = None) -> str:
    if not path.is_file():
        raise LocatedTextReadError("missing_file", str(path))
    try:
        locator = json.loads(unit.locator)
        nbt_path = locator.get("path")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise LocatedTextReadError("locator_missing", str(exc)) from exc
    if not isinstance(nbt_path, list):
        raise LocatedTextReadError("locator_missing", "NBT path missing")
    cache_key = str(path)
    try:
        if path.suffix.lower() == ".mca":
            chunk_id = str(locator.get("chunk", ""))
            chunks = cache.get(cache_key) if cache is not None else None
            if chunks is None:
                chunks = read_region_chunks(path)
                if cache is not None:
                    cache[cache_key] = chunks
            tag = None
            for chunk in chunks:
                if str(chunk["index"]) == chunk_id:
                    tag = get_tag(chunk["doc"].root, nbt_path)
                    break
            if tag is None:
                raise LocatedTextReadError("locator_missing", f"chunk {chunk_id} missing")
        else:
            root = cache.get(cache_key) if cache is not None else None
            if root is None:
                root = read_nbt_file(path).root
                if cache is not None:
                    cache[cache_key] = root
            tag = get_tag(root, nbt_path)
    except LocatedTextReadError:
        raise
    except ValueError as exc:
        raise LocatedTextReadError("source_corrupt", str(exc)) from exc
    except (KeyError, TypeError, OSError) as exc:
        raise LocatedTextReadError("locator_missing", str(exc)) from exc
    if tag.type_id != TAG_STRING:
        raise LocatedTextReadError("locator_missing", "NBT tag is not a string")
    if "json" in locator:
        try:
            found = get_json_pointer(json.loads(tag.value), str(locator["json"]))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, IndexError) as exc:
            raise LocatedTextReadError("locator_missing", str(exc)) from exc
        if not isinstance(found, str):
            raise LocatedTextReadError("locator_missing", "JSON leaf is not a string")
        return found
    if "command" in locator:
        command_locator = locator["command"]
        if not isinstance(command_locator, dict):
            raise LocatedTextReadError("locator_missing", "embedded command locator missing")
        found = read_command_text(tag.value, command_locator)
        if found is None:
            raise LocatedTextReadError("locator_missing", "embedded command leaf missing")
        return found
    return tag.value
