from __future__ import annotations

import json
import re
from typing import Any

from .utils import looks_translatable

# `translate` is a language-resource key, not player-visible prose.  Its
# arguments (for example text components in `with`) are still visited below.
TEXT_COMPONENT_KEYS = {"text", "fallback"}
PLAYER_NAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")

SELECTOR_RE = re.compile(r"@[a-zA-Z]\[([^\]]*)\]")
SELECTOR_NAME_TAG_RE = re.compile(r"(?:^|,)\s*(name|tag)\s*=\s*(!?)([^,\]]+)")

# SNBT keys whose values are player-visible display text and therefore safe to
# translate. Everything else (Tags, CustomName, Lock, author, id, custom_data,
# and any key we have not audited) is mechanism data and is never extracted.
# Only the namespaced item components are display; plain custom_name/item_name
# appear in item-predicate match values (execute if items ...) and stay locked.
SNBT_TEXT_KEYS = {
    "text",
    "pages",
    "filtered_pages",
    "lore",
    "Lore",
    "minecraft:lore",
    "minecraft:custom_name",
    "minecraft:item_name",
    "title",
}
SNBT_PARENTED_TEXT_KEYS = {
    ("front_text", "messages"),
    ("back_text", "messages"),
    ("display", "Name"),
    ("display", "Lore"),
    # Modern written-book pages/titles are compounds holding raw/filtered text.
    ("pages", "raw"),
    ("pages", "filtered"),
    ("filtered_pages", "raw"),
    ("filtered_pages", "filtered"),
    ("title", "raw"),
    ("title", "filtered"),
}

# Audited mechanism/identity fields: their values participate in selectors,
# NBT matching or resource identity and must never be sent to the AI.
SNBT_MECHANISM_KEYS = {
    "tags",
    "lock",
    "author",
    "id",
    "custom_data",
    "customname",
    "custom_name",
    "item_name",
    "uuid",
    "pos",
    "rotation",
    "motion",
    "dimension",
    "level",
    "scheduler",
    "command",
    "lastoutput",
    "owner",
    "target",
    "passengers",
    "inventory",
    "enderitems",
    "selecteditem",
    "handitems",
    "armoritems",
}

# Item predicate keys whose values are match values in consumer commands
# (clear, execute if/unless items, execute if/unless data). Producers (give,
# NBT item data) expose the same keys as display text; the reference index
# locks the shared normalized value across files instead of translating it.
ITEM_MATCH_KEYS = {
    "lore",
    "minecraft:lore",
    "custom_name",
    "item_name",
    "minecraft:custom_name",
    "minecraft:item_name",
}

# Audited non-text display attributes inside text components: never translated,
# and not interesting for the unknown-field report either.
SNBT_NONTEXT_KEYS = {
    "color",
    "bold",
    "italic",
    "underlined",
    "strikethrough",
    "obfuscated",
    "font",
    "insertion",
    "shadow",
    "background",
    "type",
    "action",
    "url",
    "value",
    "page",
    "translate",
    "score",
    "name",
    "objective",
    "selector",
    "nbt",
    "path",
    "interpret",
    "separator",
    "entity",
    "storage",
    "block",
}



def collect_selector_refs(command: str) -> set[str]:
    """Collect name=/tag= values referenced by entity selectors in a command.

    These strings are matching identifiers: any NBT text equal to one of them
    must stay untranslated or selectors like @e[name=...] stop matching.
    """

    refs: set[str] = set()
    for selector in SELECTOR_RE.finditer(command):
        for match in SELECTOR_NAME_TAG_RE.finditer(selector.group(1)):
            if match.group(2) == "!":
                continue
            value = match.group(3).strip().strip("\"'")
            if value:
                refs.add(value)
    return refs


CONSUMER_CLAUSE_RE = re.compile(r"\b(if|unless)\s+(items|data)\b", re.IGNORECASE)


def collect_mechanism_refs(command: str) -> set[str]:
    """Collect match values referenced by commands.

    CustomName values anywhere participate in ``execute if/unless data`` and
    selector matching. In consumer commands (``clear``, ``execute if|unless
    items|data``) item display keys (lore, custom_name, item_name and their
    namespaced forms) are exact match values too: the same normalized value in
    a producer (give command, NBT item data) must stay untranslated.
    """

    refs: set[str] = set()
    execute_run = _find_execute_run(command)
    if execute_run is not None:
        predicate = command[:execute_run]
        if CONSUMER_CLAUSE_RE.search(predicate):
            refs.update(_match_value_refs(predicate))
        refs.update(collect_mechanism_refs(command[execute_run:]))
        return refs
    stripped = command.strip().lstrip("/")
    lower = stripped.lower()
    if lower.startswith("say ") or lower.startswith("me "):
        # say/me payloads are display prose; braces inside them are literal
        # text, not match structures, and a translated say line must not make
        # the ref index look like a mechanism value was changed.
        return set()
    if lower.startswith("clear"):
        refs.update(_match_value_refs(command))
    refs.update(_custom_name_refs(command))
    return refs


def _custom_name_refs(command: str) -> set[str]:
    refs: set[str] = set()
    entries, _ok = _iter_snbt_values(command)
    for path, start, end, quoted in entries:
        if not path or str(path[-1]).lower() != "customname":
            continue
        refs.update(_match_value_texts(command[start:end], quoted))
    return refs


def _match_value_refs(command: str) -> set[str]:
    """Collect item display values inside a consumer (match) command."""

    refs: set[str] = set()
    entries, _ok = _iter_snbt_values(command)
    for path, start, end, quoted in entries:
        if not path:
            continue
        key = str(path[-1]).lower()
        display_name = key == "name" and len(path) >= 2 and str(path[-2]).lower() == "display"
        if key not in ITEM_MATCH_KEYS and key != "customname" and not display_name:
            continue
        refs.update(_match_value_texts(command[start:end], quoted))
    return refs


def _match_value_texts(span: str, quoted: bool) -> set[str]:
    """Decode an SNBT value into the visible strings it can match."""

    value = _snbt_unquote(span) if quoted else span
    stripped = value.strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, str):
        return {parsed}
    if isinstance(parsed, dict):
        texts: set[str] = set()
        if isinstance(parsed.get("text"), str):
            texts.add(parsed["text"])
        for _pointer, text in extract_json_texts(value):
            texts.add(text)
        return texts
    if isinstance(parsed, list):
        return {text for _pointer, text in extract_json_texts(value)}
    if stripped.startswith(("{", "[", '"')):
        return set()
    return {value} if value else set()


def extract_json_texts(raw: str) -> list[tuple[str, str]]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(value, str):
        # Bare JSON string literal ("...") as used by written-book pages and
        # sign lines: translate the decoded text and re-wrap on write-back.
        if looks_translatable(value):
            return [("", value)]
        return []
    results: list[tuple[str, str]] = []

    def walk(node: Any, pointer: str, skip_component_text: bool = False) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                child_pointer = f"{pointer}/{escape_pointer_part(str(key))}"
                if key in TEXT_COMPONENT_KEYS and isinstance(child, str) and not skip_component_text and looks_translatable(child):
                    results.append((child_pointer, child))
                else:
                    walk(child, child_pointer)
        elif isinstance(node, list):
            for index, child in enumerate(node):
                if isinstance(child, str) and not skip_component_text and looks_translatable(child):
                    results.append((f"{pointer}/{index}", child))
                else:
                    walk(child, f"{pointer}/{index}", _is_speaker_name(node, index))

    walk(value, "")
    return results


def _is_speaker_name(items: list[Any], index: int) -> bool:
    """Keep Minecraft player-name decorations out of translation requests."""

    item = items[index]
    if not isinstance(item, dict):
        return False
    text = item.get("text")
    if not isinstance(text, str) or not PLAYER_NAME_RE.fullmatch(text.strip()):
        return False
    before = items[index - 1] if index else None
    after = items[index + 1] if index + 1 < len(items) else None
    before_text = before.get("text", "") if isinstance(before, dict) else ""
    after_text = after.get("text", "") if isinstance(after, dict) else ""
    # endswith("<") also covers the "<<name>>" form.
    return isinstance(before_text, str) and isinstance(after_text, str) and before_text.strip().endswith("<") and after_text.lstrip().startswith(">")


def escape_pointer_part(key: str) -> str:
    return key.replace("~", "~0").replace("/", "~1")


def _unescape_pointer_part(part: str) -> str:
    return part.replace("~1", "/").replace("~0", "~")


def get_json_pointer(container: Any, pointer: str) -> Any:
    """Read a JSON-pointer path without extracting or translating."""

    if pointer == "":
        return container
    cur = container
    for raw_part in pointer.lstrip("/").split("/"):
        part = _unescape_pointer_part(raw_part)
        if isinstance(cur, list):
            cur = cur[int(part)]
        elif isinstance(cur, dict):
            cur = cur[part]
        else:
            raise KeyError(pointer)
    return cur


def set_json_pointer(container: Any, pointer: str, value: str) -> None:
    """Assign at a JSON-pointer path; the single shared implementation."""

    parts = [] if pointer == "" else pointer.lstrip("/").split("/")
    cur = container
    for raw_part in parts[:-1]:
        part = _unescape_pointer_part(raw_part)
        cur = cur[int(part)] if isinstance(cur, list) else cur[part]
    last = _unescape_pointer_part(parts[-1]) if parts else ""
    if isinstance(cur, list):
        cur[int(last)] = value
    else:
        cur[last] = value


def apply_json_text(raw: str, pointer: str, target: str) -> str:
    value = json.loads(raw)
    if pointer == "" and isinstance(value, str):
        return json.dumps(target, ensure_ascii=False)
    set_json_pointer(value, pointer, target)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


TITLE_TEXT_COMMAND_RE = re.compile(r"title\s+\S+\s+(title|subtitle|actionbar)\s+")
BOSSBAR_NAME_COMMAND_RE = re.compile(r"bossbar\s+(?:set\s+\S+\s+name|add\s+\S+)\s+")
EXECUTE_RUN_RE = re.compile(r"(^|\s)run\s+", re.IGNORECASE)


def extract_command_texts(
    command: str,
    counts: dict[str, int] | None = None,
    filtered_details: list[dict[str, object]] | None = None,
    file_path: str = "",
) -> list[tuple[dict[str, object], str]]:
    """Extract player-visible text from a command line.

    counts, when given, accumulates ``mechanism_locked`` (audited mechanism
    fields skipped), ``unknown_mechanism_field`` (un-audited values that
    look translatable but were skipped) and ``unparsed_mechanism`` (SNBT that
    failed to parse) so the report can explain every non-extracted command
    payload. filtered_details, when given, records a redacted sample for each
    skipped field (file/path/source/reason) instead of only incrementing a
    counter.
    """

    stripped = command.strip()
    lower = stripped.lstrip("/").lower()

    execute_run = _find_execute_run(command)
    if execute_run is not None:
        nested_start = execute_run
        results = []
        for locator, text in extract_command_texts(
            command[nested_start:], counts, filtered_details=filtered_details, file_path=file_path
        ):
            results.append((_shift_locator(locator, nested_start), text))
        return results

    if lower.startswith("say "):
        prefix_len = command.lower().find("say ") + 4
        text = command[prefix_len:].strip()
        if looks_translatable(text):
            return [({"kind": "plain", "start": prefix_len, "end": len(command)}, text)]
        return []

    if lower.startswith("clear"):
        # clear only takes item predicates: every string value is an exact
        # match value, never display prose to translate.
        return _extract_match_snbt_texts(command, counts, filtered_details=filtered_details, file_path=file_path)

    # Root text components may be quoted strings or SNBT, not only JSON.
    payload_match = re.match(r'^\s*/?(?:tellraw\s+\S+|title\s+\S+\s+(?:title|subtitle|actionbar))\s+', command, re.I)
    if payload_match:
        start = payload_match.end()
        payload = command[start:]
        try:
            json.loads(payload)
        except json.JSONDecodeError:
            wrapped = '{text:' + payload + '}'
            entries, valid = _iter_snbt_values(wrapped)
            if not valid:
                if counts is not None or filtered_details is not None:
                    _note_command_filter(
                        counts,
                        filtered_details,
                        reason="unparsed_mechanism",
                        file_path=file_path,
                        path=(),
                        source=payload,
                        extra_reason="tellraw/title payload failed to parse as SNBT; original text kept",
                    )
                return []
            found = []
            for path, left, right, quoted in entries:
                if not path or path[0] != 'text' or any(k not in {'text', 'fallback', 'extra', 'with'} for k in path):
                    continue
                if path[-1] not in {'text', 'fallback', 'extra', 'with'} or not quoted:
                    continue
                decoded = _snbt_unquote(wrapped[left:right])
                if looks_translatable(decoded):
                    found.append(
                        (
                            {
                                'kind': 'snbt',
                                'start': start + left - 6,
                                'end': start + right - 6,
                                'quoted': True,
                                'snbt_path': list(path),
                                'snbt_index': sum(1 for loc, _text in found if tuple(loc.get('snbt_path') or ()) == path),
                            },
                            decoded,
                        )
                    )
            return found
        else:
            if payload.lstrip().startswith('"'):
                value = json.loads(payload)
                if isinstance(value, str) and looks_translatable(value):
                    return [({'kind': 'snbt', 'start': start, 'end': len(command.rstrip()), 'quoted': True}, value)]
                return []

    json_start = _find_json_start(command)
    if json_start < 0:
        return []
    prefix = command[:json_start].lower()
    prefix_normalized = prefix.lstrip().lstrip("/")
    if not (
        prefix_normalized.startswith("tellraw ")
        or TITLE_TEXT_COMMAND_RE.match(prefix_normalized)
        or BOSSBAR_NAME_COMMAND_RE.match(prefix_normalized)
    ):
        return _extract_snbt_texts(command, counts, filtered_details=filtered_details, file_path=file_path)

    json_end = _matching_json_end(command, json_start)
    if json_end is not None:
        raw_json = command[json_start:json_end].strip()
    else:
        raw_json = command[json_start:].strip()
    results = []
    for pointer, text in extract_json_texts(raw_json):
        results.append(({"kind": "json", "json_start": json_start, "pointer": pointer}, text))
    return results


def command_targets_text_display(line: str) -> bool:
    """True when a command writes display text onto a TextDisplay entity.

    ``data merge/modify entity @n[type=text_display] {text:[...]}`` and the
    namespace-explicit variant are multiline layout containers: the extracted
    units must carry the text-display layout metadata so escaped ``\\n``
    breaks are normalized like other TextDisplay text (Realm of the Skies
    lever labels).
    """

    return bool(_TEXT_DISPLAY_TARGET_RE.search(line))


_TEXT_DISPLAY_TARGET_RE = re.compile(
    r"data\s+(?:merge|modify)\s+entity\b[^{]*?\btype\s*=\s*(?:minecraft:)?text_display\b",
    re.IGNORECASE,
)


def _snbt_path_tuple(locator: dict[str, object]) -> tuple[str, ...] | None:
    path = locator.get("snbt_path")
    if isinstance(path, (list, tuple)) and path and all(isinstance(part, str) for part in path):
        return tuple(str(part) for part in path)
    return None


def _snbt_span_from_offsets(
    command: str,
    start: int,
    quoted: bool,
) -> tuple[int, int, bool, str, str] | None:
    if not (0 <= start < len(command)):
        return None
    if quoted and command[start] not in "'\"":
        return None
    end = _snbt_value_end(command, start, quoted)
    if end is None or not (start < end <= len(command)):
        return None
    raw_span = command[start:end]
    decoded = _snbt_unquote(raw_span) if quoted else raw_span
    return start, end, quoted, raw_span, decoded


def _resolve_snbt_span(command: str, locator: dict[str, object]) -> tuple[int, int, bool, str, str] | None:
    """Resolve an SNBT value after sibling replacements shifted byte offsets.

    Stored start/end remain valid while earlier siblings are unchanged. After a
    left-hand Name/JSON slice shrinks, later Lore offsets no longer land on a
    quote. Relocate by the structural key path captured at extraction.

    When ``snbt_path`` + ``snbt_index`` identify an entry, that structural
    selection wins over the stored byte offset: after earlier siblings shrink,
    a stale offset can land exactly on a *neighbouring* entry's opening quote,
    and trusting it would read (or overwrite) the wrong lore line.
    """

    quoted = bool(locator.get("quoted"))
    try:
        start = int(locator["start"])
    except (KeyError, TypeError, ValueError):
        start = -1
    path = _snbt_path_tuple(locator)
    json_pointer = locator.get("json")
    json_pointer = json_pointer if isinstance(json_pointer, str) else None
    direct = _snbt_span_from_offsets(command, start, quoted)
    entries, _parsed_ok = _iter_snbt_values(command)
    matches = [
        (entry_start, entry_end, entry_quoted)
        for entry_path, entry_start, entry_end, entry_quoted in entries
        if path is None or entry_path == path
    ]
    if path is not None and not matches:
        # tellraw SNBT locators are extracted from a wrapped `{text:...}` view;
        # the original command may not contain that synthetic path.
        return direct
    if path is None and not matches:
        return direct
    wanted_index = locator.get("snbt_index")
    index_ok = (
        path is not None
        and isinstance(wanted_index, int)
        and 0 <= wanted_index < len(matches)
    )
    if not index_ok and direct is not None:
        for entry_start, _entry_end, _entry_quoted in matches:
            if entry_start == direct[0]:
                return direct
        if path is None and json_pointer is None:
            return None
    chosen = [matches[wanted_index]] if index_ok else matches
    if json_pointer is not None:
        json_hits = []
        for entry_start, entry_end, entry_quoted in chosen:
            raw_span = command[entry_start:entry_end]
            decoded = _snbt_unquote(raw_span) if entry_quoted else raw_span
            try:
                get_json_pointer(json.loads(decoded), json_pointer)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError, IndexError):
                continue
            json_hits.append((entry_start, entry_end, entry_quoted, raw_span, decoded))
        if json_hits:
            entry_start, entry_end, entry_quoted, raw_span, decoded = min(
                json_hits,
                key=lambda item: abs(item[0] - start) if start >= 0 else item[0],
            )
            return entry_start, entry_end, entry_quoted, raw_span, decoded
        if path is None:
            return None
    if path is None:
        return None
    entry_start, entry_end, entry_quoted = min(
        chosen,
        key=lambda item: abs(item[0] - start) if start >= 0 else item[0],
    )
    raw_span = command[entry_start:entry_end]
    decoded = _snbt_unquote(raw_span) if entry_quoted else raw_span
    return entry_start, entry_end, entry_quoted, raw_span, decoded


def apply_command_text(command: str, locator: dict[str, object], target: str) -> str:
    if locator.get("kind") == "plain":
        start = int(locator["start"])
        end = int(locator["end"])
        return command[:start] + target + command[end:]
    if locator.get("kind") == "json":
        start = int(locator["json_start"])
        end = _matching_json_end(command, start)
        if end is None:
            return command
        raw_json = command[start:end]
        replaced = apply_json_text(raw_json, str(locator["pointer"]), target)
        return command[:start] + replaced + command[end:]
    if locator.get("kind") == "json_slice":
        start = int(locator["json_start"])
        end = _matching_json_end(command, start) or int(locator["json_end"])
        raw_json = command[start:end]
        replaced = apply_json_text(raw_json, str(locator["pointer"]), target)
        return command[:start] + replaced + command[end:]
    if locator.get("kind") == "snbt":
        span = _resolve_snbt_span(command, locator)
        if span is None:
            return command
        start, end, quoted, raw_span, decoded = span
        if "json" in locator:
            replaced = apply_json_text(decoded, str(locator["json"]), target)
        else:
            replaced = target
        new_span = _snbt_quote(replaced, raw_span[0]) if quoted else replaced
        return command[:start] + new_span + command[end:]
    return command


def read_command_text(command: str, locator: dict[str, object]) -> str | None:
    """Read the display leaf identified by a structural command locator."""

    try:
        if locator.get("kind") == "plain":
            start = int(locator["start"])
            if not (0 <= start <= len(command)):
                return None
            # Payload length changes after write-back; the prefix before start
            # is the stable structure, so read through the current line end.
            return command[start:]
        if locator.get("kind") in {"json", "json_slice"}:
            start = int(locator["json_start"])
            end = _matching_json_end(command, start)
            if end is None:
                if locator.get("kind") == "json_slice" and "json_end" in locator:
                    end = int(locator["json_end"])
                else:
                    return None
            raw_json = command[start:end]
            value = json.loads(raw_json)
            pointer = str(locator.get("pointer") or "")
            found = get_json_pointer(value, pointer) if pointer else value
            return found if isinstance(found, str) else None
        if locator.get("kind") == "snbt":
            span = _resolve_snbt_span(command, locator)
            if span is None:
                return None
            _start, _end, _quoted, _raw_span, decoded = span
            if "json" in locator:
                value = json.loads(decoded)
                found = get_json_pointer(value, str(locator["json"]))
                return found if isinstance(found, str) else None
            return decoded
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, IndexError):
        return None
    return None


def command_locator_is_valid(command: str, locator: dict[str, object]) -> bool:
    """True when a command locator still resolves inside the current line.

    Write-back uses this to distinguish "translated to the same text"
    (unchanged) from "pointer vanished / line shifted" (writeback failed) so
    a broken locator is never silently dropped.
    """

    if locator.get("kind") == "plain":
        start = int(locator["start"])
        end = int(locator["end"])
        return 0 <= start < len(command) and start <= end <= len(command)
    if locator.get("kind") in {"json", "json_slice"}:
        start = int(locator["json_start"])
        return _matching_json_end(command, start) is not None
    if locator.get("kind") == "snbt":
        return _resolve_snbt_span(command, locator) is not None
    return False


def command_locator_offset(locator: dict[str, object]) -> int:
    """Return the source position used to keep sibling JSON slices stable."""

    if locator.get("kind") in {"json", "json_slice"}:
        return int(locator.get("json_start", -1))
    if locator.get("kind") == "snbt":
        return int(locator.get("start", -1))
    return -1


def _find_json_start(value: str) -> int:
    starts = [idx for idx in (value.find("{"), value.find("[")) if idx >= 0]
    return min(starts) if starts else -1


def _find_execute_run(command: str) -> int | None:
    match = EXECUTE_RUN_RE.search(command)
    if not match:
        return None
    prefix = command[: match.start()].strip().lower().lstrip("/")
    if not prefix.startswith("execute "):
        return None
    return match.end()


def _shift_locator(locator: dict[str, object], offset: int) -> dict[str, object]:
    shifted = dict(locator)
    if shifted.get("kind") == "plain":
        shifted["start"] = int(shifted["start"]) + offset
        shifted["end"] = int(shifted["end"]) + offset
    elif shifted.get("kind") == "json":
        shifted["json_start"] = int(shifted["json_start"]) + offset
    elif shifted.get("kind") == "json_slice":
        shifted["json_start"] = int(shifted["json_start"]) + offset
        shifted["json_end"] = int(shifted["json_end"]) + offset
    elif shifted.get("kind") == "snbt":
        shifted["start"] = int(shifted["start"]) + offset
        shifted["end"] = int(shifted["end"]) + offset
    return shifted


def _extract_match_snbt_texts(
    command: str,
    counts: dict[str, int] | None = None,
    filtered_details: list[dict[str, object]] | None = None,
    file_path: str = "",
) -> list[tuple[dict[str, object], str]]:
    """clear-style item predicates: string values are exact match values.

    Nothing is extracted; audited item/mechanism keys are counted as
    ``mechanism_locked``, un-audited translatable-looking values as
    ``unknown_mechanism_field``.
    """

    if counts is not None or filtered_details is not None:
        entries, parsed_ok = _iter_snbt_values(command)
        if not parsed_ok:
            _note_command_filter(
                counts,
                filtered_details,
                reason="unparsed_mechanism",
                file_path=file_path,
                path=(),
                source=command,
                extra_reason="command SNBT failed to parse",
            )
        for path, start, end, quoted in entries:
            key = str(path[-1]).lower() if path else ""
            decoded = _snbt_unquote(command[start:end]) if quoted else command[start:end]
            if key in ITEM_MATCH_KEYS or key in SNBT_MECHANISM_KEYS:
                _note_command_filter(
                    counts,
                    filtered_details,
                    reason="mechanism_locked",
                    file_path=file_path,
                    path=path,
                    source=decoded,
                    extra_reason=f"item-predicate match key {path[-1] if path else key}",
                )
            elif key not in SNBT_NONTEXT_KEYS:
                if looks_translatable(decoded):
                    _note_command_filter(
                        counts,
                        filtered_details,
                        reason="unknown_mechanism_field",
                        file_path=file_path,
                        path=path,
                        source=decoded,
                        extra_reason=f"unaudited command field {path[-1] if path else key}",
                    )
    return []


def _extract_snbt_texts(
    command: str,
    counts: dict[str, int] | None = None,
    filtered_details: list[dict[str, object]] | None = None,
    file_path: str = "",
) -> list[tuple[dict[str, object], str]]:
    """Extract display text from command SNBT by key name, never by JSON shape.

    Only values under whitelisted display keys (text/pages/lore/title/item
    names/sign messages) are candidates. Mechanism fields such as Tags,
    CustomName, Lock, author, id and custom_data are ignored even when they
    happen to be valid JSON, because selectors and NBT matching reference them.
    Skipped audited fields are reported as ``mechanism_locked``; skipped
    un-audited values that look translatable as ``unknown_mechanism_field``;
    SNBT that failed to parse as ``unparsed_mechanism``.
    """

    results: list[tuple[dict[str, object], str]] = []
    entries, parsed_ok = _iter_snbt_values(command)
    path_indexes: dict[tuple[str, ...], int] = {}
    indexed_entries: list[tuple[tuple[str, ...], int, int, bool, int]] = []
    for path, start, end, quoted in entries:
        index = path_indexes.get(path, 0)
        path_indexes[path] = index + 1
        indexed_entries.append((path, start, end, quoted, index))
    if not parsed_ok and (counts is not None or filtered_details is not None):
        _note_command_filter(
            counts,
            filtered_details,
            reason="unparsed_mechanism",
            file_path=file_path,
            path=(),
            source=command,
            extra_reason="command SNBT failed to parse",
        )
    for path, start, end, quoted, snbt_index in indexed_entries:
        if _snbt_key_allowed(path):
            raw_span = command[start:end]
            decoded = _snbt_unquote(raw_span) if quoted else raw_span
            json_texts = extract_json_texts(decoded)
            if json_texts:
                for pointer, text in json_texts:
                    locator: dict[str, object] = {
                        "kind": "snbt",
                        "start": start,
                        "end": end,
                        "quoted": quoted,
                        "json": pointer,
                        "snbt_path": list(path),
                        "snbt_index": snbt_index,
                    }
                    results.append((locator, text))
                continue
            stripped = decoded.strip()
            if stripped.startswith(("{", "[", '"')):
                try:
                    json.loads(stripped)
                    continue
                except json.JSONDecodeError:
                    pass
            if looks_translatable(decoded):
                results.append(
                    (
                        {
                            "kind": "snbt",
                            "start": start,
                            "end": end,
                            "quoted": quoted,
                            "snbt_path": list(path),
                            "snbt_index": snbt_index,
                        },
                        decoded,
                    )
                )
            continue
        if counts is not None or filtered_details is not None:
            # Root list elements have no owning field. They are not display
            # text, but must still be safe to inspect with diagnostics enabled.
            key = str(path[-1]).lower() if path else ""
            raw_span = command[start:end]
            decoded = _snbt_unquote(raw_span) if quoted else raw_span
            if key in SNBT_MECHANISM_KEYS:
                _note_command_filter(
                    counts,
                    filtered_details,
                    reason="mechanism_locked",
                    file_path=file_path,
                    path=path,
                    source=decoded,
                    extra_reason=f"command mechanism key {path[-1]}",
                )
            elif key not in SNBT_NONTEXT_KEYS:
                if looks_translatable(decoded):
                    _note_command_filter(
                        counts,
                        filtered_details,
                        reason="unknown_mechanism_field",
                        file_path=file_path,
                        path=path,
                        source=decoded,
                        extra_reason=f"unaudited command field {path[-1] if path else '<root>'}",
                    )
    return results


def _snbt_key_allowed(path: tuple[str, ...]) -> bool:
    if not path:
        return False
    key = path[-1]
    if key in SNBT_TEXT_KEYS:
        return True
    return len(path) >= 2 and (path[-2], key) in SNBT_PARENTED_TEXT_KEYS


_SNBT_NAMESPACED_KEY_RE = re.compile(r"[A-Za-z0-9_.+\-]+:[A-Za-z0-9_.+\-/]+")
_SNBT_BARE_KEY_RE = re.compile(r"[A-Za-z0-9_.+\-]+")
_SNBT_BARE_VALUE_STOPS = set(",}][= \t")


def _iter_snbt_values(value: str) -> tuple[list[tuple[tuple[str, ...], int, int, bool]], bool]:
    """Parse SNBT structures in a command and locate string/compound values.

    Returns (entries, parsed_ok). entries are (key_path, start, end, quoted);
    list elements inherit the key of their parent list so pages:[...] items
    resolve to the pages key. Selector and item-predicate assignments
    (key=value) are understood so give/clear component syntax parses like
    ordinary SNBT. parsed_ok is False when any structure failed to parse
    (callers only report it, never write back partial results).
    """

    results: list[tuple[tuple[str, ...], int, int, bool]] = []
    failed = False
    n = len(value)

    def skip_ws(index: int) -> int:
        while index < n and value[index] in " \t":
            index += 1
        return index

    def parse_quoted(index: int) -> int | None:
        quote = value[index]
        cursor = index + 1
        while cursor < n:
            ch = value[cursor]
            if ch == "\\":
                cursor += 2
                continue
            if ch == quote:
                return cursor + 1
            cursor += 1
        return None

    def parse_key(index: int) -> tuple[str, int] | None:
        index = skip_ws(index)
        if index < n and value[index] in "'\"":
            end = parse_quoted(index)
            if end is None:
                return None
            return _snbt_unquote(value[index:end]), end
        # A namespaced key (minecraft:lore) is only a key when another
        # separator follows; otherwise the colon belongs to the separator
        # (manuscript:1b -> key 'manuscript', bare value '1b').
        match = _SNBT_NAMESPACED_KEY_RE.match(value, index)
        if match:
            after = skip_ws(match.end())
            if after < n and value[after] in ":=":
                return match.group(0), match.end()
        match = _SNBT_BARE_KEY_RE.match(value, index)
        if not match:
            return None
        return match.group(0), match.end()

    def parse_value(index: int, path: tuple[str, ...]) -> int | None:
        index = skip_ws(index)
        if index >= n:
            return None
        ch = value[index]
        if ch in "'\"":
            end = parse_quoted(index)
            if end is None:
                return None
            results.append((path, index, end, True))
            return end
        if ch == "{":
            return parse_compound(index, path)
        if ch == "[":
            return parse_list(index, path)
        cursor = index
        while cursor < n and value[cursor] not in _SNBT_BARE_VALUE_STOPS:
            cursor += 1
        return cursor if cursor > index else None

    def parse_compound(index: int, path: tuple[str, ...]) -> int | None:
        cursor = index + 1
        while True:
            cursor = skip_ws(cursor)
            if cursor >= n:
                return None
            if value[cursor] == "}":
                return cursor + 1
            parsed = parse_key(cursor)
            if parsed is None:
                return None
            key, cursor = parsed
            cursor = skip_ws(cursor)
            if cursor >= n or value[cursor] not in ":=":
                return None
            cursor = parse_value(cursor + 1, (*path, key))
            if cursor is None:
                return None
            cursor = skip_ws(cursor)
            if cursor < n and value[cursor] == ",":
                cursor += 1

    def parse_list(index: int, path: tuple[str, ...]) -> int | None:
        cursor = index + 1
        while True:
            cursor = skip_ws(cursor)
            if cursor >= n:
                return None
            if value[cursor] == "]":
                return cursor + 1
            checkpoint = cursor
            parsed = parse_key(cursor)
            assigned = False
            if parsed is not None:
                key, after_key = parsed
                after_key = skip_ws(after_key)
                if after_key < n and value[after_key] in ":=":
                    cursor = parse_value(after_key + 1, (*path, key))
                    assigned = True
            if not assigned:
                cursor = parse_value(checkpoint, path)
            if cursor is None:
                return None
            cursor = skip_ws(cursor)
            if cursor < n and value[cursor] == ",":
                cursor += 1

    cursor = 0
    while cursor < n:
        ch = value[cursor]
        if ch in "'\"":
            end = parse_quoted(cursor)
            if end is None:
                failed = True
            cursor = end if end is not None else cursor + 1
            continue
        if ch in "{[":
            end = parse_value(cursor, ())
            if end is None:
                failed = True
            else:
                cursor = end
                continue
        cursor += 1
    return results, not failed


def _snbt_unquote(span: str) -> str:
    if len(span) < 2 or span[0] not in "'\"" or span[-1] != span[0]:
        return span
    quote = span[0]
    body = span[1:-1]
    out: list[str] = []
    cursor = 0
    while cursor < len(body):
        ch = body[cursor]
        if ch == "\\" and cursor + 1 < len(body):
            nxt = body[cursor + 1]
            escapes = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", quote: quote, "\\": "\\"}
            if nxt in escapes:
                out.append(escapes[nxt])
                cursor += 2
                continue
            if nxt in {"u", "x", "U"}:
                width = {"u": 4, "x": 2, "U": 8}[nxt]
                digits = body[cursor + 2:cursor + 2 + width]
                if len(digits) == width and all(c in "0123456789abcdefABCDEF" for c in digits):
                    codepoint = int(digits, 16)
                    if codepoint <= 0x10FFFF:
                        out.append(chr(codepoint))
                        cursor += 2 + width
                        continue
        out.append(ch)
        cursor += 1
    return "".join(out)


def _snbt_quote(text: str, quote: str) -> str:
    # Semantic line breaks must remain inside ONE physical command line.
    # Escape backslashes first so literal backslash-n stays distinct from LF.
    escapes = {"\n": "\\n", "\r": "\\r", "\t": "\\t", "\b": "\\b", "\f": "\\f"}
    escaped = "".join(
        "\\" + ch if ch in ("\\", quote) else escapes.get(ch, f"\\u{ord(ch):04x}" if ord(ch) < 32 or ch in "\x85\u2028\u2029" else ch)
        for ch in text
    )
    return quote + escaped + quote


def _snbt_value_end(command: str, start: int, quoted: bool) -> int | None:
    if quoted:
        quote = command[start]
        cursor = start + 1
        while cursor < len(command):
            ch = command[cursor]
            if ch == "\\":
                cursor += 2
                continue
            if ch == quote:
                return cursor + 1
            cursor += 1
        return None
    return _matching_json_end(command, start)


def _matching_json_end(value: str, start: int) -> int | None:
    opener = value[start]
    closer = "}" if opener == "{" else "]"
    stack = [closer]
    in_string = False
    escape = False
    for idx in range(start + 1, len(value)):
        ch = value[idx]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif stack and ch == stack[-1]:
            stack.pop()
            if not stack:
                return idx + 1
        elif ch == closer and not stack:
            return idx + 1
    return None


def _note_command_filter(
    counts: dict[str, int] | None,
    details: list[dict[str, object]] | None,
    *,
    reason: str,
    file_path: str,
    path: tuple[str, ...] | list[object],
    source: str,
    extra_reason: str,
) -> None:
    """Count plus a redacted detail record for a skipped command field."""

    from .events import emit_event, filter_detail_limit, redact_text
    from .policy import classify_command_path

    if counts is not None:
        counts[reason] = counts.get(reason, 0) + 1
    policy, policy_reason = classify_command_path(tuple(path), reason_hint=reason)
    locator = "/".join(map(str, path))
    if details is not None and len(details) < filter_detail_limit():
        details.append(
            {
                "reason": reason,
                "file": file_path,
                "format": "mcfunction",
                "locator": redact_text(locator),
                "source": redact_text(source),
                "policy": policy,
                "policy_reason": redact_text(policy_reason or extra_reason, 200),
            }
        )
    emit_event(
        stage="filter",
        file=file_path,
        format="mcfunction",
        locator=locator,
        source=source,
        policy=policy,
        policy_reason=policy_reason or extra_reason,
        extra={"reason": reason},
    )
