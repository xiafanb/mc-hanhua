from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
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
    "dialog",
    "pages",
    "filtered_pages",
    # `lore` / `Lore` are deliberately absent: they are item-component keys and
    # are display text only where the context proves an item (see
    # ITEM_COMPONENT_DISPLAY_KEYS / ITEM_CONTAINER_PATH_KEYS). A plugin compound
    # that happens to use the same name keeps the default protection.
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
    return [
        (str(entry[0].get("pointer") or ""), entry[1])
        for entry in _extract_json_entries(raw)
        if entry[0].get("kind") == "json"
    ]


# ---------------------------------------------------------------------------
# JSON-encoded text components (item components written as strings)
# ---------------------------------------------------------------------------
# Datapack loot functions and give/item commands commonly store an item's
# component as a *string* holding JSON (``"minecraft:custom_name":
# "{\"text\":\"Hearty Bass\",\"color\":\"#99ccff\"}"``).  The game parses that
# string as a component, so the visible text lives one level deeper than the
# file's own JSON pointer, and the surrounding style/event fields are the
# component's mechanism data.
#
# The leaves below are the *only* ones this path opens.  A component is walked
# by its audited display keys -- text/fallback and the recursive extra/with --
# and nothing else: clickEvent/hoverEvent action, value and URL, `translate`
# keys, `nbt`/`score`/`selector`/`keybind` payloads and every unknown field are
# never entered, so they cannot become writable units.
COMPONENT_RECURSIVE_KEYS = frozenset({"extra", "with"})
#: Locator key holding the inner pointer of an encoded component.  The name
#: matches the nested-JSON sub-pointer already used by NBT and SNBT locators.
COMPONENT_LOCATOR_KEY = "json"


def component_document(value: str) -> Any | None:
    """The JSON object/array a component-carrying string holds, else ``None``.

    A component string is only opened when it is actually shaped like one: plain
    display prose that merely starts with a brace or bracket is left alone, and
    malformed JSON returns ``None`` so every caller fails closed.
    """

    stripped = value.strip()
    if not stripped.startswith(("{", "[")):
        return None
    try:
        parsed = json.loads(stripped)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, (dict, list)) else None


def extract_component_texts(raw: str) -> list[tuple[str, str]]:
    """Visible text leaves of a JSON-encoded component, with inner pointers.

    Returns ``(inner pointer, text)`` pairs for the component's display leaves
    (``text``/``fallback`` and the recursive ``extra``/``with`` descendants); a
    bare JSON string literal is one leaf at the root, exactly like the NBT/SNBT
    path treats it.  The list is empty when ``raw`` is not a component document.
    Callers decide translatability; this function never guesses a key's role.
    """

    stripped = raw.strip()
    try:
        value = json.loads(stripped)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if isinstance(value, str):
        return [("", value)]
    if not isinstance(value, (dict, list)):
        return []
    results: list[tuple[str, str]] = []

    def walk(node: Any, pointer: str) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                child_pointer = f"{pointer}/{escape_pointer_part(str(key))}"
                if key in TEXT_COMPONENT_KEYS or key in COMPONENT_RECURSIVE_KEYS:
                    walk(child, child_pointer)
            return
        if isinstance(node, list):
            for index, child in enumerate(node):
                walk(child, f"{pointer}/{index}")
            return
        if isinstance(node, str):
            results.append((pointer, node))

    walk(value, "")
    return results


#: A one- or two-letter ASCII word: the only shape promoted from "not
#: translatable" to a reviewed fragment leaf.  ``looks_translatable`` rejects it
#: (no language of its own), but inside a display payload between two real
#: display leaves it is an article/pronoun split off from its sentence.
SHORT_FRAGMENT_MAX_CHARS = 2


def is_short_fragment_candidate(text: str) -> bool:
    stripped = text.strip()
    return 0 < len(stripped) <= SHORT_FRAGMENT_MAX_CHARS and stripped.isascii() and stripped.isalpha()


def component_skip_reason(value: str) -> str:
    """Why an item component string produced no writable leaf.

    Recorded instead of silently dropping the string, so the report explains
    every unopened component (fail-closed: none of these is ever half-opened).
    """

    if value.strip().startswith('"'):
        # A JSON string literal whose decoded text is empty or not prose.
        return "component_leaf_filtered"
    if component_document(value) is not None:
        return "component_no_visible_text"
    return "component_json_unparsed"


def encoded_component_locator(outer_pointer: str, component_pointer: str) -> dict[str, str]:
    """Locator object for a text leaf inside a JSON-encoded component.

    The identity is deliberately two-part: the outer JSON pointer of the string
    in the file plus the inner component pointer, so two leaves carrying the
    same text stay distinguishable (and a later edit cannot silently retarget
    one for the other).
    """

    return {"pointer": outer_pointer, COMPONENT_LOCATOR_KEY: component_pointer}


def split_encoded_component_locator(locator: str) -> tuple[str, str] | None:
    """``(outer pointer, inner component pointer)`` of such a locator, else ``None``."""

    try:
        parsed = json.loads(locator)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    outer = parsed.get("pointer")
    inner = parsed.get(COMPONENT_LOCATOR_KEY)
    if isinstance(outer, str) and isinstance(inner, str):
        return outer, inner
    return None


def composed_pointer(outer_pointer: str, inner_pointer: str) -> str:
    """File-space pointer of an encoded component's inner leaf.

    One composition rule for every consumer (write-back, quality grouping,
    audit's writable-leaf set): the inner pointer is appended to the outer one,
    matching how the NBT side already composes ``path`` and its ``json``
    sub-pointer.
    """

    if not inner_pointer:
        return outer_pointer
    if inner_pointer.startswith("/"):
        return f"{outer_pointer}{inner_pointer}"
    return f"{outer_pointer}/{inner_pointer}"


def read_json_pointer_text(raw: str, pointer: str) -> str | None:
    """Read a string leaf out of a JSON document held in a string."""

    try:
        found = get_json_pointer(json.loads(raw), pointer)
    except (TypeError, ValueError, json.JSONDecodeError, KeyError, IndexError):
        return None
    return found if isinstance(found, str) else None


def _extract_json_entries(
    raw: str,
    counts: dict[str, int] | None = None,
    filtered_details: list[dict[str, object]] | None = None,
    file_path: str = "",
    fragment_candidates: list[dict[str, object]] | None = None,
    lenient: bool = False,
) -> list[tuple[dict[str, object], str]]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        parsed = _lenient_json(raw) if lenient else None
        if parsed is None:
            return []
        value = parsed[0]
        # Unquoted-key components are written by span, never re-serialised.
        return [item for item in _extract_json_entries(json.dumps(value), counts, filtered_details, file_path)
                if item[0].get("kind") == "json"]
    if isinstance(value, str):
        # Bare JSON string literal ("...") as used by written-book pages and
        # sign lines: translate the decoded text and re-wrap on write-back.
        if looks_translatable(value):
            return [({"kind": "json", "pointer": ""}, value)]
        return []
    results: list[tuple[dict[str, object], str]] = []

    def command_field(key: str, parent: dict[str, Any], child: Any) -> bool:
        if not isinstance(child, str):
            return False
        lowered = key.lower()
        action = str(parent.get("action") or parent.get("type") or "").lower()
        if lowered == "value" and action in {"run_command", "run_command_if_loaded"}:
            return True
        if lowered == "command":
            candidate = child.lstrip()
            return candidate.startswith("/") or candidate.split(" ", 1)[0].lower() in {
                "bossbar",
                "data",
                "execute",
                "function",
                "give",
                "particle",
                "playsound",
                "say",
                "scoreboard",
                "setblock",
                "summon",
                "tellraw",
                "teleport",
                "title",
                "tp",
            }
        return False

    # Ordered display leaves of this payload: (pointer, keys, text, translatable).
    # A near-miss leaf is only offered as a fragment candidate when real display
    # text exists on *both* sides of it inside the same payload, which is what
    # separates a mid-sentence article from a trailing UI marker.
    ordered_display: list[tuple[str, tuple[str, ...], str, bool]] = []

    def walk(node: Any, pointer: str, skip_component_text: bool = False, keys: tuple[str, ...] = ()) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                child_pointer = f"{pointer}/{escape_pointer_part(str(key))}"
                child_keys = (*keys, str(key))
                if key in TEXT_COMPONENT_KEYS and isinstance(child, str) and not skip_component_text:
                    translatable = looks_translatable(child)
                    ordered_display.append((child_pointer, child_keys, child, translatable))
                    if translatable:
                        results.append(({"kind": "json", "pointer": child_pointer}, child))
                elif command_field(str(key), node, child):
                    for command_locator, text in extract_command_texts(
                        child,
                        counts,
                        filtered_details=filtered_details,
                        file_path=file_path,
                    ):
                        results.append(
                            (
                                {
                                    "kind": "embedded_command",
                                    "pointer": child_pointer,
                                    "command": command_locator,
                                },
                                text,
                            )
                        )
                else:
                    walk(child, child_pointer, False, child_keys)
        elif isinstance(node, list):
            for index, child in enumerate(node):
                if isinstance(child, str) and not skip_component_text:
                    child_pointer = f"{pointer}/{index}"
                    translatable = looks_translatable(child)
                    ordered_display.append((child_pointer, keys, child, translatable))
                    if translatable:
                        results.append(({"kind": "json", "pointer": child_pointer}, child))
                else:
                    walk(child, f"{pointer}/{index}", _is_speaker_name(node, index), keys)

    walk(value, "")
    if fragment_candidates is not None:
        for index, (candidate_pointer, candidate_keys, text, translatable) in enumerate(ordered_display):
            if translatable or not is_short_fragment_candidate(text):
                continue
            if not is_display_text_path(candidate_keys):
                continue
            before = any(entry[3] for entry in ordered_display[:index])
            after = any(entry[3] for entry in ordered_display[index + 1 :])
            if before and after:
                fragment_candidates.append({"kind": "json", "pointer": candidate_pointer, "text": text})
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


_LENIENT_BARE_RE = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null")
_LENIENT_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _lenient_json(raw: str) -> tuple[Any, dict[str, tuple[int, int]]] | None:
    """Parse pre-1.12 Gson-lenient JSON (unquoted object keys).

    Returns ``(tree, string_spans)`` where ``string_spans`` maps the JSON pointer
    of every string value to its ``(start, end)`` literal span in ``raw`` so a
    write can replace only that literal.  ``None`` on anything else.
    """

    n = len(raw)
    spans: dict[str, tuple[int, int]] = {}
    decoder = json.JSONDecoder()

    def ws(i: int) -> int:
        while i < n and raw[i] in " \t\r\n":
            i += 1
        return i

    def string(i: int) -> tuple[str, int] | None:
        try:
            return decoder.raw_decode(raw, i)
        except json.JSONDecodeError:
            return None

    def value(i: int, pointer: str) -> tuple[Any, int] | None:
        i = ws(i)
        if i >= n:
            return None
        ch = raw[i]
        if ch == '"':
            got = string(i)
            if got is None:
                return None
            spans[pointer] = (i, got[1])
            return got
        if ch == "{":
            out: dict[str, Any] = {}
            i = ws(i + 1)
            if i < n and raw[i] == "}":
                return out, i + 1
            while True:
                i = ws(i)
                if i < n and raw[i] == '"':
                    got = string(i)
                    if got is None:
                        return None
                    key, i = got
                else:
                    match = _LENIENT_KEY_RE.match(raw, i)
                    if match is None:
                        return None
                    key, i = match.group(0), match.end()
                i = ws(i)
                if i >= n or raw[i] != ":":
                    return None
                got = value(i + 1, f"{pointer}/{escape_pointer_part(key)}")
                if got is None:
                    return None
                out[key], i = got
                i = ws(i)
                if i < n and raw[i] == ",":
                    i += 1
                    continue
                if i < n and raw[i] == "}":
                    return out, i + 1
                return None
        if ch == "[":
            items: list[Any] = []
            i = ws(i + 1)
            if i < n and raw[i] == "]":
                return items, i + 1
            while True:
                got = value(i, f"{pointer}/{len(items)}")
                if got is None:
                    return None
                item, i = got
                items.append(item)
                i = ws(i)
                if i < n and raw[i] == ",":
                    i += 1
                    continue
                if i < n and raw[i] == "]":
                    return items, i + 1
                return None
        match = _LENIENT_BARE_RE.match(raw, i)
        if match is None:
            return None
        return json.loads(match.group(0)), match.end()

    start = ws(0)
    if start >= n or raw[start] not in "{[":
        return None
    got = value(start, "")
    if got is None or ws(got[1]) != n:
        return None
    return got[0], spans


def _loads_component(raw: str) -> Any:
    """Strict JSON, else lenient (unquoted-key) JSON; raises ``JSONDecodeError``."""

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        parsed = _lenient_json(raw)
        if parsed is None:
            raise
        return parsed[0]


def apply_json_text(raw: str, pointer: str, target: str) -> str:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        parsed = _lenient_json(raw)
        if parsed is None:
            raise
        tree, spans = parsed
        if _json_pointer_equals(tree, pointer, target) or pointer not in spans:
            return raw
        begin, end = spans[pointer]
        return raw[:begin] + json.dumps(target, ensure_ascii=False) + raw[end:]
    if pointer == "" and isinstance(value, str):
        return raw if value == target else json.dumps(target, ensure_ascii=False)
    if _json_pointer_equals(value, pointer, target):
        # Identity write: keep the author's bytes instead of re-serializing.
        return raw
    set_json_pointer(value, pointer, target)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_pointer_equals(value: Any, pointer: str, target: str) -> bool:
    try:
        return get_json_pointer(value, pointer) == target
    except (KeyError, TypeError, ValueError, IndexError):
        return False


TITLE_TEXT_COMMAND_RE = re.compile(r"title\s+\S+\s+(title|subtitle|actionbar)\s+")
BOSSBAR_NAME_COMMAND_RE = re.compile(r"bossbar\s+(?:set\s+\S+\s+name|add\s+\S+)\s+")
EXECUTE_RUN_RE = re.compile(r"(^|\s)run\s+", re.IGNORECASE)
# An entity selector or plain player name. The bracket body may contain spaces
# (`@a[team=p2, limit=1]`, `@e[type=item,name="Foo Bar"]`); a bare `\S+` would
# stop at the first one and mistake the rest of the selector for the payload.
_SELECTOR_TOKEN = r"(?:@[a-zA-Z](?:\[[^\]]*\])?|\S+)"
_COMMAND_PAYLOAD_RE = re.compile(
    rf'^\s*/?(?:tellraw\s+{_SELECTOR_TOKEN}|title\s+{_SELECTOR_TOKEN}\s+(?:title|subtitle|actionbar))\s+',
    re.I,
)
# Extraction, write-back and the audit re-read all parse the tellraw/title
# payload inside a synthetic ``{text:...}`` wrapper, because a trailing
# component's real `text:` key would otherwise shadow the wrapper's own path.
# Offsets inside that view map back to the command with this shift.
_WRAPPED_VIEW_PREFIX = '{text:'
_WRAPPED_VIEW_SHIFT = len(_WRAPPED_VIEW_PREFIX)


def extract_command_texts(
    command: str,
    counts: dict[str, int] | None = None,
    filtered_details: list[dict[str, object]] | None = None,
    file_path: str = "",
    fragment_candidates: list[dict[str, object]] | None = None,
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
        nested_candidates: list[dict[str, object]] = []
        for locator, text in extract_command_texts(
            command[nested_start:],
            counts,
            filtered_details=filtered_details,
            file_path=file_path,
            fragment_candidates=nested_candidates,
        ):
            results.append((_shift_locator(locator, nested_start), text))
        if fragment_candidates is not None:
            for candidate in nested_candidates:
                shifted = dict(candidate)
                shifted["locator"] = _shift_locator(candidate["locator"], nested_start)
                fragment_candidates.append(shifted)
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
    payload_match = _COMMAND_PAYLOAD_RE.match(command)
    if payload_match:
        start = payload_match.end()
        payload = command[start:]
        try:
            json.loads(payload)
        except json.JSONDecodeError:
            wrapped_entries = _wrapped_snbt_entries(command)
            if wrapped_entries is None:
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
            for path, left, right, quoted, index in wrapped_entries:
                if not path or path[0] != 'text' or any(k not in {'text', 'fallback', 'extra', 'with'} for k in path):
                    continue
                if path[-1] not in {'text', 'fallback', 'extra', 'with'} or not quoted:
                    continue
                decoded = _snbt_unquote(command[left:right])
                if looks_translatable(decoded):
                    found.append(
                        (
                            {
                                'kind': 'snbt',
                                'start': left,
                                'end': right,
                                'quoted': True,
                                'snbt_path': list(path),
                                'snbt_index': index,
                                # Minted inside the wrapped `{text:...}` view: the
                                # path is only meaningful there, never in the raw
                                # command (where a trailing component's real
                                # `text:` key can collide with it).
                                'wrapped': True,
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
            results = []
            candidates: list[dict[str, object]] = []
            for entry, text in _extract_json_entries(
                payload,
                counts,
                filtered_details=filtered_details,
                file_path=file_path,
                fragment_candidates=candidates,
            ):
                results.append((_command_json_locator(start, entry), text))
            if fragment_candidates is not None:
                for candidate in candidates:
                    fragment_candidates.append(
                        {"locator": _command_json_locator(start, candidate), "text": candidate.get("text", "")}
                    )
            return results

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
    try:
        json.loads(raw_json)
    except json.JSONDecodeError:
        # The payload after the verb is SNBT rather than JSON (bossbar names
        # commonly are). Delegate to the key-whitelisted SNBT path, which also
        # records unparsed_mechanism when it cannot parse the command either.
        return _extract_snbt_texts(command, counts, filtered_details=filtered_details, file_path=file_path)
    results = []
    candidates = []
    for entry, text in _extract_json_entries(
        raw_json,
        counts,
        filtered_details=filtered_details,
        file_path=file_path,
        fragment_candidates=candidates,
    ):
        results.append((_command_json_locator(json_start, entry), text))
    if fragment_candidates is not None:
        for candidate in candidates:
            fragment_candidates.append(
                {"locator": _command_json_locator(json_start, candidate), "text": candidate.get("text", "")}
            )
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


def _index_siblings(
    entries: list[tuple[tuple[str, ...], int, int, bool]],
) -> list[tuple[tuple[str, ...], int, int, bool, int]]:
    """Pair each SNBT entry with its ordinal among same-path siblings.

    The ordinal is what a locator stores as ``snbt_index``. Every consumer —
    extraction, write-back and the audit re-read — parses the same view, so all
    of them must count *every* sibling sharing a path, including the ones they
    then reject as untranslatable. Numbering only the accepted candidates made
    the stored index address a different slot once an earlier sibling's
    translation changed the command length, which surfaced as 73 false
    "写回后文本不一致" blocks on a byte-correct write-back.
    """

    indexes: dict[tuple[str, ...], int] = {}
    indexed: list[tuple[tuple[str, ...], int, int, bool, int]] = []
    for path, start, end, quoted in entries:
        index = indexes.get(path, 0)
        indexes[path] = index + 1
        indexed.append((path, start, end, quoted, index))
    return indexed


def _command_payload_view(command: str) -> tuple[int, str] | None:
    """Return ``(base, wrapped)`` for a tellraw/title SNBT payload.

    ``execute ... run`` prefixes are stripped repeatedly, mirroring the
    recursion ``extract_command_texts`` performs, so nested prefixes land on the
    same base here. ``None`` means the line carries no display payload.
    """

    base = 0
    while True:
        nested = _find_execute_run(command[base:])
        if not nested:  # None, or no progress: treat as no further prefix
            break
        base += nested
    payload = _COMMAND_PAYLOAD_RE.match(command[base:])
    if payload is None:
        return None
    base += payload.end()
    return base, _WRAPPED_VIEW_PREFIX + command[base:] + "}"


def _wrapped_snbt_entries(
    command: str,
) -> list[tuple[tuple[str, ...], int, int, bool, int]] | None:
    """Parse the extraction-time ``{text:...}`` view of a display command.

    Entries are ``(path, start, end, quoted, snbt_index)`` with the offsets
    already mapped back to ``command``; the return is ``None`` when the line is
    not a display payload or the payload does not parse. Extraction and
    resolution both go through this one view, so a locator's ``snbt_path`` +
    ``snbt_index`` name the same slot on both sides.
    """

    view = _command_payload_view(command)
    if view is None:
        return None
    base, wrapped = view
    entries, parsed_ok = _iter_snbt_values(wrapped)
    if not parsed_ok:
        return None
    return [
        (path, base + start - _WRAPPED_VIEW_SHIFT, base + end - _WRAPPED_VIEW_SHIFT, quoted, index)
        for path, start, end, quoted, index in _index_siblings(entries)
    ]


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
    if locator.get("wrapped") and path is not None:
        # Locator was minted inside the extraction-time `{text:...}` view; its
        # key path is meaningless in the raw command, where a trailing
        # component's real `text:` key can shadow the wrapped list-element path
        # and route the value into the wrong slot. Resolve in that view only.
        wrapped = _resolve_wrapped_snbt_span(command, path, start, locator.get("snbt_index"))
        if wrapped is not None:
            return wrapped
        return _snbt_span_from_offsets(command, start, quoted)
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
        # Pre-wrapper locators minted before `wrapped` existed carry a path from
        # the synthetic view that the raw command never contains. Re-resolve
        # there: stored byte offsets go stale once an earlier sibling's
        # translation changes the command length (e.g. "<King> " -> "<国王>"),
        # and trusting them reads the wrong entry.
        wrapped = _resolve_wrapped_snbt_span(command, path, start, locator.get("snbt_index"))
        if wrapped is not None:
            return wrapped
        return direct
    if path is None and not matches:
        return direct
    wanted_index = locator.get("snbt_index")
    index_ok = path is not None and isinstance(wanted_index, int) and 0 <= wanted_index < len(matches)
    if not index_ok and direct is not None:
        for entry_start, _entry_end, _entry_quoted in matches:
            if entry_start == direct[0]:
                return direct
        if path is None and json_pointer is None:
            return None
    # A structural index pins one sibling; without one, every same-path sibling
    # stays a candidate until the JSON sub-pointer (when present) discards the
    # ones that do not carry it. The old offset-first shortcut lives above:
    # when the stored offset still lands on a match, that span is returned as-is.
    candidates = [matches[wanted_index]] if index_ok else matches
    if json_pointer is not None:
        json_hits = []
        for entry_start, entry_end, entry_quoted in candidates:
            raw_span = command[entry_start:entry_end]
            decoded = _snbt_unquote(raw_span) if entry_quoted else raw_span
            try:
                get_json_pointer(_loads_component(decoded), json_pointer)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError, IndexError):
                continue
            json_hits.append((entry_start, entry_end, entry_quoted, raw_span, decoded))
        hit = _select_sibling(json_hits, None, start, lambda item: item[0])
        if hit is not None:
            return hit
        if path is None:
            return None
    if path is None:
        return None
    entry_start, entry_end, entry_quoted = _select_sibling(candidates, None, start, lambda item: item[0])
    raw_span = command[entry_start:entry_end]
    decoded = _snbt_unquote(raw_span) if entry_quoted else raw_span
    return entry_start, entry_end, entry_quoted, raw_span, decoded


def _select_sibling(
    candidates: list[Any],
    wanted_index: object,
    stale_start: int,
    start_of: Callable[[Any], int],
) -> Any | None:
    """Pick the sibling a locator refers to: stored index first, offset second.

    The stored ``snbt_index`` is the structural answer and is stable across any
    translation length change. Proximity to the stale scan-time offset is only a
    fallback for locators that carry no usable index (older reports, callers
    that drop the field) and only holds while sibling lengths barely shift.
    """

    if not candidates:
        return None
    if isinstance(wanted_index, int) and 0 <= wanted_index < len(candidates):
        return candidates[wanted_index]
    return min(
        candidates,
        key=lambda item: abs(start_of(item) - stale_start) if stale_start >= 0 else start_of(item),
    )


def _resolve_wrapped_snbt_span(
    command: str,
    path: tuple[str, ...],
    stale_start: int,
    wanted_index: object = None,
) -> tuple[int, int, bool, str, str] | None:
    """Resolve a tellraw/title locator inside the extraction-time view.

    Shares ``_wrapped_snbt_entries`` with extraction, so the ``snbt_index`` a
    locator stored selects the same sibling here. Write-back only rewrites
    string payloads, so that structural order survives any length change a
    translation introduces.
    """

    entries = _wrapped_snbt_entries(command)
    if entries is None:
        return None
    candidates = [entry for entry in entries if entry[0] == path]
    selected = _select_sibling(candidates, wanted_index, stale_start, lambda item: item[1])
    if selected is None:
        return None
    _path, real_start, real_end, was_quoted, _index = selected
    if not (0 <= real_start < real_end <= len(command)):
        return None
    if was_quoted and command[real_start] not in "'\"":
        return None
    raw_span = command[real_start:real_end]
    decoded = _snbt_unquote(raw_span) if was_quoted else raw_span
    return real_start, real_end, was_quoted, raw_span, decoded


def _command_json_locator(json_start: int, entry: dict[str, object]) -> dict[str, object]:
    if entry.get("kind") == "json":
        return {
            "kind": "json",
            "json_start": json_start,
            "pointer": entry.get("pointer", ""),
        }
    nested = entry.get("command")
    return {
        "kind": "json_command",
        "json_start": json_start,
        "pointer": entry.get("pointer", ""),
        "command": nested,
        "command_offset": command_locator_offset(nested) if isinstance(nested, dict) else 0,
    }


def apply_json_command_text(raw_json: str, locator: dict[str, object], target: str, refusals: list[str] | None = None) -> str:
    value = json.loads(raw_json)
    pointer = str(locator.get("pointer") or "")
    command = get_json_pointer(value, pointer)
    nested = locator.get("command")
    if not isinstance(command, str) or not isinstance(nested, dict):
        return raw_json
    updated = apply_command_text(command, nested, target, refusals=refusals)
    if updated == command:
        return raw_json
    set_json_pointer(value, pointer, updated)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def read_json_command_text(raw_json: str, locator: dict[str, object]) -> str | None:
    value = json.loads(raw_json)
    pointer = str(locator.get("pointer") or "")
    command = get_json_pointer(value, pointer)
    nested = locator.get("command")
    if not isinstance(command, str) or not isinstance(nested, dict):
        return None
    return read_command_text(command, nested)


def apply_command_text(
    command: str,
    locator: dict[str, object],
    target: str,
    refusals: list[str] | None = None,
) -> str:
    """Write one display leaf back into a command.

    ``refusals`` (optional out-parameter) collects reasons for spans that were
    deliberately left untouched because the rewrite could not be proven to keep
    the game's meaning; the caller reports them instead of claiming a write.
    """

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
    if locator.get("kind") == "json_command":
        start = int(locator["json_start"])
        end = _matching_json_end(command, start)
        if end is None:
            return command
        raw_json = command[start:end]
        replaced = apply_json_command_text(raw_json, locator, target, refusals=refusals)
        return command[:start] + replaced + command[end:]
    if locator.get("kind") == "snbt":
        span = _resolve_snbt_span(command, locator)
        if span is None:
            return command
        start, end, quoted, raw_span, decoded = span
        if "json_command" in locator:
            nested = locator["json_command"]
            replaced = apply_json_command_text(decoded, nested, target, refusals=refusals) if isinstance(nested, dict) else decoded
        elif "json" in locator:
            replaced = apply_json_text(decoded, str(locator["json"]), target)
        else:
            replaced = target
        if replaced == decoded:
            # Nothing to write: keep the author's exact spelling instead of
            # re-encoding escapes the decoder did not resolve.
            return command
        if quoted and _snbt_has_undecodable_escape(raw_span):
            # Re-encoding this span may change what the game reads (see
            # ``_snbt_has_undecodable_escape``): refuse rather than guess.
            if refusals is not None:
                refusals.append("unverifiable_escape")
            return command
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
        if locator.get("kind") == "json_command":
            start = int(locator["json_start"])
            end = _matching_json_end(command, start)
            if end is None:
                return None
            return read_json_command_text(command[start:end], locator)
        if locator.get("kind") == "snbt":
            span = _resolve_snbt_span(command, locator)
            if span is None:
                return None
            _start, _end, _quoted, _raw_span, decoded = span
            if "json_command" in locator:
                nested = locator["json_command"]
                return read_json_command_text(decoded, nested) if isinstance(nested, dict) else None
            if "json" in locator:
                value = _loads_component(decoded)
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
    if locator.get("kind") == "json_command":
        start = int(locator["json_start"])
        end = _matching_json_end(command, start)
        return end is not None and read_json_command_text(command[start:end], locator) is not None
    if locator.get("kind") == "snbt":
        return _resolve_snbt_span(command, locator) is not None
    return False


def command_locator_offset(locator: dict[str, object]) -> int:
    """Return the source position used to keep sibling JSON slices stable."""

    if locator.get("kind") in {"json", "json_slice"}:
        return int(locator.get("json_start", -1))
    if locator.get("kind") == "json_command":
        return int(locator.get("json_start", -1)) * 1_000_000 + int(locator.get("command_offset", 0))
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
    elif shifted.get("kind") == "json_command":
        shifted["json_start"] = int(shifted["json_start"]) + offset
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


_BROKEN_COMPONENT_RE = re.compile(r'[{\[]\s*"?[A-Za-z_]\w*"?\s*:')


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
    indexed_entries = _index_siblings(entries)
    item_production = item_production_context(command)
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
        if _snbt_key_allowed(path, item_production=item_production):
            raw_span = command[start:end]
            decoded = _snbt_unquote(raw_span) if quoted else raw_span
            json_entries = _extract_json_entries(
                decoded,
                counts,
                filtered_details=filtered_details,
                file_path=file_path,
                lenient=True,
            )
            if json_entries:
                for entry, text in json_entries:
                    locator: dict[str, object] = {
                        "kind": "snbt",
                        "start": start,
                        "end": end,
                        "quoted": quoted,
                        "snbt_path": list(path),
                        "snbt_index": snbt_index,
                    }
                    if entry.get("kind") == "embedded_command":
                        locator["json_command"] = {
                            "pointer": entry.get("pointer", ""),
                            "command": entry.get("command"),
                        }
                    else:
                        locator["json"] = entry.get("pointer", "")
                    results.append((locator, text))
                continue
            stripped = decoded.strip()
            if stripped.startswith(("{", "[", '"')):
                try:
                    _loads_component(stripped)
                    continue
                except json.JSONDecodeError:
                    pass
            if _BROKEN_COMPONENT_RE.match(stripped):
                # A page that opens a component but never parses: an unescaped
                # quote ended the SNBT string, and 1.10's JsonToNBT then rejects
                # the whole command, so no span of it is ever displayed.
                _note_command_filter(
                    counts,
                    filtered_details,
                    reason="unparsed_mechanism",
                    file_path=file_path,
                    path=path,
                    source=decoded,
                    extra_reason="unparseable component page; the game rejects this command",
                )
                continue
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


#: Bare 1.21 item-component display keys. They are display text only in an item
#: *production* command whose trailing SNBT is the item's own component list;
#: the same key in clear / if-items / nbt= is an exact match value, and any
#: other command's same-named field stays an unknown mechanism field.
ITEM_COMPONENT_DISPLAY_KEYS = frozenset({"custom_name", "item_name", "lore", "Name", "Lore"})
#: Lower-case display key names, hoisted: the extractor asks this for every
#: string leaf.  ``name`` is deliberately absent --- an item id (``"name":
#: "minecraft:cod"``) is not a label.
_ITEM_COMPONENT_LOWER_DISPLAY_KEYS = frozenset({"custom_name", "item_name", "lore"})
#: The capitalised 1.20 forms, which only mean a display field inside a
#: ``display`` container (``display:{Name:…}`` / ``display:{Lore:[…]}``).
_ITEM_COMPONENT_LEGACY_DISPLAY_KEYS = frozenset({"Name", "Lore"})
#: Loot-function ids that write an item's own component list.  Namespaces are
#: ignored because both `minecraft:set_components` and the bare
#: `set_components` spelling occur in real packs (RoH uses both).  A new entry
#: needs the same evidence: a map that actually authors through that function.
ITEM_COMPONENT_PRODUCER_FUNCTIONS = frozenset({"set_components"})
#: A key path passing through one of these proves an item's own data, which is
#: what lets the capitalised 1.20 forms (`display:{Name:…}`) stay display text
#: without opening every plugin compound that happens to use `Name`.
ITEM_CONTAINER_PATH_KEYS = frozenset({"components", "display", "item", "items"})
#: Ancestor keys of an item *consumer*: inside a predicate, criteria or match
#: condition the same component keys are exact match values, so their display
#: text must never be opened (the game compares them byte for byte).
ITEM_MATCH_PATH_KEYS = frozenset({"predicate", "predicates", "condition", "criteria", "trigger", "equipment"})
#: Directories whose JSON documents are item predicates, never item producers.
ITEM_MATCH_PATH_MARKERS = ("/predicate/", "/predicates/")
_ITEM_PRODUCTION_RE = re.compile(r"^(?:give|item\s+(?:replace|modify))\b", re.IGNORECASE)


def item_production_context(command: str) -> bool:
    """True when the command writes an item's own component list (give / item)."""

    return bool(_ITEM_PRODUCTION_RE.match(command.strip().lstrip("/")))


def _snbt_key_allowed(path: tuple[str, ...], *, item_production: bool = False) -> bool:
    if not path:
        return False
    key = path[-1]
    if key in SNBT_TEXT_KEYS:
        return True
    if len(path) >= 2 and (path[-2], key) in SNBT_PARENTED_TEXT_KEYS:
        return True
    if key in ITEM_COMPONENT_DISPLAY_KEYS:
        # Display text only where the item itself is proven: the command's own
        # component list, or a path passing through an item container.
        return item_production or any(str(part).lower() in ITEM_CONTAINER_PATH_KEYS for part in path[:-1])
    return False


def item_component_display_key(key: str, ancestors: tuple[str, ...] | list[str] = ()) -> bool:
    """True for an item component key holding the item's own display text.

    Modern ids (``minecraft:custom_name``, and the bare lower-case forms) count
    wherever a proven component list carries them.  The capitalised 1.20 forms
    only count inside a ``display`` container, so an item id (``"name":
    "minecraft:cod"``) is never mistaken for a label.
    """

    text = str(key)
    if text.rsplit(":", 1)[-1] in _ITEM_COMPONENT_LOWER_DISPLAY_KEYS:
        return True
    if text in _ITEM_COMPONENT_LEGACY_DISPLAY_KEYS:
        return any(str(part).lower() == "display" for part in ancestors)
    return False


def item_component_producer_function(value: dict[str, Any]) -> bool:
    """True when a loot-function object writes an item's own component list."""

    function = value.get("function")
    return isinstance(function, str) and function.rsplit(":", 1)[-1].lower() in ITEM_COMPONENT_PRODUCER_FUNCTIONS


def is_item_component_producer_path(keys: tuple[str, ...] | list[str]) -> bool:
    """Pointer-shape proof that a key chain is a loot function's component list.

    ``…/functions/<i>/components/<display key>`` (a loot table's or entity's
    function array) or ``components/<display key>`` at the document root (an
    item-modifier file).  An arbitrary object that merely happens to contain a
    ``components`` key --- a plugin config, an advancement predicate's equipment
    match --- does not qualify.
    """

    if len(keys) < 2:
        return False
    lowered = [str(part).lower() for part in keys]
    if lowered[-2] != "components":
        return False
    return len(lowered) == 2 or lowered[-3] == "functions"


def pointer_key_parts(pointer: str) -> list[str]:
    """Key chain of a JSON pointer: array indexes are dropped, keys unescaped."""

    return [
        _unescape_pointer_part(part)
        for part in str(pointer).lstrip("/").split("/")
        if part and not part.isdigit()
    ]


#: Keys whose string value is player-visible text wherever a payload carries it:
#: text-component leaves (``text``/``fallback``), the recursive component
#: containers (``extra``/``with``, where a string is a literal component), the
#: hover tooltip body (``contents``), and the audited display containers of
#: books, signs, titles and teams.  The independent audit uses this to decide
#: which payload strings may change; everything else (``color``, ``italic``,
#: ``bold``, event ``action``/``value``/``url``, ``translate``, ``font``,
#: ``insertion``, ``selector``/``nbt``/``score``/``keybind`` payloads and every
#: unknown field) is compared by value.  ``name`` is deliberately absent: it is
#: an identifier far more often than a label.
PAYLOAD_DISPLAY_LEAF_KEYS = frozenset(
    {
        "text",
        "fallback",
        "extra",
        "with",
        "contents",
        "title",
        "subtitle",
        "raw",
        "filtered",
        "lore",
        "pages",
        "filtered_pages",
        "messages",
        "texts",
        "dialog",
        "prefix",
        "suffix",
    }
)
#: Ancestor keys a display leaf may live under.  A leaf under anything else is
#: mechanism or unknown data (``score``, ``clickEvent``, ``custom_data``, a
#: plugin's own object) and therefore never free to change.
PAYLOAD_DISPLAY_CONTAINER_KEYS = frozenset(
    {
        "extra",
        "with",
        "contents",
        "pages",
        "filtered_pages",
        "lore",
        "messages",
        "raw",
        "filtered",
        "title",
        "subtitle",
        "texts",
        "dialog",
        "written_book_content",
        "writable_book_content",
        "display",
        "front_text",
        "back_text",
    }
)
#: Hover events are display only through their ``contents``; ``action`` and any
#: other field under them is mechanism data.
PAYLOAD_HOVER_KEYS = frozenset({"hoverevent", "hover_event"})


def is_display_text_path(keys: tuple[str, ...] | list[str]) -> bool:
    """True when a JSON key chain names player-visible display text.

    ``keys`` is the chain of *keys* from the document root to the leaf, with
    array indices dropped: ``["extra"]`` for a string element of ``extra``,
    ``["0"]``-less ``["hoverEvent", "contents", "text"]`` for a hover tooltip.

    The leaf must be an audited display key **and** every ancestor must be a
    known display container; a leaf under anything else keeps its value.  That
    is what stops ``{"score":{"text":…}}``, ``{"clickEvent":{"text":…}}``,
    ``{"custom_data":{"text":…}}`` or a plugin's own ``{"plugin":{"text":…}}``
    from being treated as free text.  A hover tooltip is display only through
    its ``contents``.
    """

    if not keys:
        return False
    parts = [str(part) for part in keys]
    for index, part in enumerate(parts[:-1]):
        lowered = part.rsplit(":", 1)[-1].lower()
        if lowered in PAYLOAD_HOVER_KEYS:
            if index + 1 >= len(parts) or str(parts[index + 1]).lower() != "contents":
                return False
            continue
        if lowered not in PAYLOAD_DISPLAY_CONTAINER_KEYS:
            return False
    leaf = str(parts[-1]).rsplit(":", 1)[-1].lower()
    return leaf in PAYLOAD_DISPLAY_LEAF_KEYS


def is_item_match_context(keys: tuple[str, ...] | list[str], file_path: str = "") -> bool:
    """True when an item component sits in a *consumer* (match) position.

    ``clear``/``execute if items`` commands, item predicates and advancement
    criteria compare component values exactly; the same keys there are match
    data, never display text, so they are never opened (and never written back).
    """

    normalized = str(file_path).replace("\\", "/").lower()
    if any(marker in normalized for marker in ITEM_MATCH_PATH_MARKERS):
        return True
    return any(str(part).lower() in ITEM_MATCH_PATH_KEYS for part in keys)


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

    entries, parsed_ok, _values = _parse_snbt(value)
    return entries, parsed_ok


def _parse_snbt(value: str) -> tuple[list[tuple[tuple[str, ...], int, int, bool]], bool, list[Any]]:
    """``(entries, parsed_ok, top_level_values)`` — one SNBT parser, two views.

    The spans feed extraction, write-back and the audit locators; the values let
    the interaction gate read the same text as a component tree instead of
    growing a second grammar for the same syntax.
    """

    results: list[tuple[tuple[str, ...], int, int, bool]] = []
    values: list[Any] = []
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

    def parse_value(index: int, path: tuple[str, ...]) -> tuple[int, Any] | None:
        index = skip_ws(index)
        if index >= n:
            return None
        ch = value[index]
        if ch in "'\"":
            end = parse_quoted(index)
            if end is None:
                return None
            results.append((path, index, end, True))
            return end, _snbt_unquote(value[index:end])
        if ch == "{":
            return parse_compound(index, path)
        if ch == "[":
            return parse_list(index, path)
        cursor = index
        while cursor < n and value[cursor] not in _SNBT_BARE_VALUE_STOPS:
            cursor += 1
        return (cursor, value[index:cursor]) if cursor > index else None

    def parse_compound(index: int, path: tuple[str, ...]) -> tuple[int, dict[str, Any]] | None:
        compound: dict[str, Any] = {}
        cursor = index + 1
        while True:
            cursor = skip_ws(cursor)
            if cursor >= n:
                return None
            if value[cursor] == "}":
                return cursor + 1, compound
            parsed = parse_key(cursor)
            if parsed is None:
                return None
            key, cursor = parsed
            cursor = skip_ws(cursor)
            if cursor >= n or value[cursor] not in ":=":
                return None
            parsed = parse_value(cursor + 1, (*path, key))
            if parsed is None:
                return None
            cursor, child = parsed
            compound[key] = child
            cursor = skip_ws(cursor)
            if cursor < n and value[cursor] == ",":
                cursor += 1

    def parse_list(index: int, path: tuple[str, ...]) -> tuple[int, list[Any]] | None:
        items: list[Any] = []
        cursor = index + 1
        while True:
            cursor = skip_ws(cursor)
            if cursor >= n:
                return None
            if value[cursor] == "]":
                return cursor + 1, items
            checkpoint = cursor
            parsed = parse_key(cursor)
            assigned = False
            child: Any = None
            if parsed is not None:
                key, after_key = parsed
                after_key = skip_ws(after_key)
                if after_key < n and value[after_key] in ":=":
                    parsed = parse_value(after_key + 1, (*path, key))
                    assigned = True
            if not assigned:
                parsed = parse_value(checkpoint, path)
            if parsed is None:
                return None
            cursor, child = parsed
            items.append(child)
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
            parsed = parse_value(cursor, ())
            if parsed is None:
                failed = True
            else:
                cursor, parsed_value = parsed
                values.append(parsed_value)
                continue
        cursor += 1
    # A failed structure is rescanned from the next character, which re-reports
    # spans already located with their full path. Keep the first (most
    # specific) entry so no span is ever claimed twice.
    seen_spans: set[tuple[int, int]] = set()
    unique = []
    for entry in results:
        if (entry[1], entry[2]) not in seen_spans:
            seen_spans.add((entry[1], entry[2]))
            unique.append(entry)
    return unique, not failed, values


# Escapes ``_snbt_unquote`` resolves today, i.e. the spellings the writer is
# allowed to re-encode.  Keep in sync with the decoder's escape table: a form
# listed here but not decoded (or vice versa) turns into silent meaning drift.
_SNBT_DECODABLE_ESCAPE_RE = re.compile(
    r"\\(?:u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8}|x[0-9A-Fa-f]{2}|[nrtbf'\"\\])"
)


def _snbt_has_undecodable_escape(span: str) -> bool:
    """True when a quoted span carries a backslash ``_snbt_unquote`` cannot resolve.

    Such a spelling cannot be re-encoded safely: ``"\\ue451"`` is a code point to
    the SNBT parser, while the re-encoded ``"\\\\ue451"`` is plain text, and
    ``\\N{...}`` is left verbatim by our decoder but may be a named escape to the
    game.  The writer refuses those spans instead of guessing (fossil-frights
    shipped exactly this corruption).
    """

    if len(span) < 2 or span[0] not in "'\"" or span[-1] != span[0]:
        return False
    body = span[1:-1]
    cursor = 0
    while True:
        index = body.find("\\", cursor)
        if index < 0:
            return False
        match = _SNBT_DECODABLE_ESCAPE_RE.match(body, index)
        if match is None:
            return True
        cursor = match.end()


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


# ---------------------------------------------------------------------------
# Clickable display commands: interaction layout
# ---------------------------------------------------------------------------
#
# A chat menu paints its rows with newlines and fill lines, and the clickable
# labels are separate components inside the same command.  The write door judges
# one span at a time, so nothing there can see that a translation moved the
# clickable control row onto the sentence: a span that ends in a fill line loses
# it, or a neighbouring wrapped line loses its break, and the button is then
# drawn at the end of the prose instead of on its own control line.  The gate
# below compares the *rendered* message instead: the components that carry a
# click event, the visible text each one shows, its explicit line offset, the
# alignment marks, and the fixed padding on both sides of the row.

#: Component keys that carry a click event; ``click_event`` is the 1.12 spelling.
CLICK_EVENT_KEYS = ("clickEvent", "click_event")
#: Alignment marks a menu row may start with.  A clickable label keeps its mark
#: and the fixed gap after it (``► Sure.`` -> ``► 当然。``); a fragment made only
#: of marks and whitespace is furniture and is kept byte for byte.
CONTROL_MARKS = "►▶▷▸▹➤»→⌲"
#: The only whitespace a candidate repair may touch: the row's own ASCII pad.
ROW_PAD = " \t"
#: Metadata key the scanner uses for one extracted leaf of a clickable message:
#: ``{"clickable": …, "gap_after_furniture": …, "layout_breaks": …}``.  The write
#: door and the audit stay independent of it; the shared candidate repair
#: (``quality.repair_layout_candidate``) is the only reader.
INTERACTIVE_LEAF_METADATA = "interactive_leaf"
#: Keys whose value is rendered from live game data, never from the file.
_DYNAMIC_COMPONENT_KEYS = ("score", "selector", "nbt", "keybind")
#: Click actions whose value is the command the player runs.
_COMMAND_CLICK_ACTIONS = frozenset({"run_command", "run_command_if_loaded", "suggest_command"})

#: A proven interaction regression: the source text must be kept.
INTERACTION_LAYOUT = "interaction_layout"
#: The line carries click events but one side cannot be read: keep the source.
INTERACTION_UNVERIFIABLE = "interaction_unverifiable"


@dataclass(frozen=True, slots=True)
class InteractiveFragment:
    """One visible fragment of a display command, in render order.

    ``text`` is ``None`` for a component rendered from game data
    (``score``/``selector``/``nbt``/``keybind``): it stays in the stream so that
    positions line up and its inherited event is still compared, but there is no
    file text to read.  ``events`` is the click-event chain inherited from the
    component tree, outermost first; the last entry is the effective event.
    """

    text: str | None
    events: tuple[Any, ...]

    @property
    def clickable(self) -> bool:
        return bool(self.events)


def _mentions_click_event(line: str) -> bool:
    # Gate-side on purpose: the pre-publication audit keeps its own marker test
    # (``mechanism_skeleton.mentions_click_event``) so that a change here cannot
    # decide what the independent check is allowed to see.
    return any(key in line for key in CLICK_EVENT_KEYS)


def _event_signature(event: Any) -> Any:
    """Comparable identity of one click event.

    ``action`` and the command a click runs must survive: they are what the
    player's click executes.  Every other field is compared by value with the
    hover tooltip's display leaves masked, because a translated tooltip is
    display text while a changed ``url``/``page``/``item`` is not.
    """

    if not isinstance(event, dict):
        return ("raw", repr(event))
    action = str(event.get("action") or "")
    target = ""
    if action in _COMMAND_CLICK_ACTIONS:
        value = event.get("value")
        target = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    return ("click", action, target, _masked_event_fields(event))


def _masked_event_fields(event: dict[Any, Any], keys: tuple[str, ...] = ()) -> Any:
    return tuple(
        (str(key), _masked_event_field(value, (*keys, str(key))))
        for key, value in sorted(event.items(), key=lambda item: str(item[0]))
    )


def _masked_event_field(value: Any, keys: tuple[str, ...]) -> Any:
    if isinstance(value, str):
        return ("display",) if is_display_text_path(keys) else value
    if isinstance(value, dict):
        return _masked_event_fields(value, keys)
    if isinstance(value, list):
        return tuple(_masked_event_field(item, keys) for item in value)
    return value


def _component_fragments(node: Any, events: tuple[Any, ...], out: list[InteractiveFragment]) -> None:
    """Visible fragments of one component in the order the game renders them.

    Only real display keys are walked: ``text`` (or ``fallback`` when a
    ``translate`` component has no text of its own), then the ``with`` arguments
    and then ``extra`` children.  An event object's own fields are never body
    text, and a component rendered from game data contributes only its event.
    """

    if isinstance(node, str):
        out.append(InteractiveFragment(node, events))
        return
    if isinstance(node, list):
        for child in node:
            _component_fragments(child, events, out)
        return
    if not isinstance(node, dict):
        return
    for key in CLICK_EVENT_KEYS:
        if key in node:
            events = (*events, _event_signature(node[key]))
            break
    own = node.get("text")
    if not isinstance(own, (str, list)):
        # A ``{text:[…]}`` component *is* its child list: prefer it over the
        # ``fallback`` a repair-only component would carry, or the list branch
        # below can never run and the row is compared as if it had no text.
        own = node.get("fallback")
    if isinstance(own, str):
        out.append(InteractiveFragment(own, events))
    elif isinstance(own, list):
        # ``{text:[…]}``: extraction reads that list as child components (the
        # real SNBT tellraw form in several packs), so the gate reads the same
        # shape instead of silently finding no clickable component at all.
        _component_fragments(own, events, out)
    for key in ("with", "extra"):
        if key in node:
            _component_fragments(node[key], events, out)
    if not isinstance(own, str) and any(key in node for key in _DYNAMIC_COMPONENT_KEYS):
        out.append(InteractiveFragment(None, events))


def _wrapped_snbt_tree(command: str) -> Any | None:
    """The payload of a display command as data, read through the shared view.

    Extraction, write-back and the audit all parse tellraw/title payloads inside
    the synthetic ``{text:...}`` view (``_wrapped_snbt_entries``); the
    interaction gate reads its structure the same way instead of growing a
    second SNBT grammar.  ``None`` means the payload did not parse.
    """

    view = _command_payload_view(command)
    if view is None:
        return None
    _base, wrapped = view
    _entries, parsed_ok, values = _parse_snbt(wrapped)
    if not parsed_ok or not values:
        return None
    first = values[0]
    if isinstance(first, dict) and "text" in first:
        return first["text"]
    return None


def _interaction_fragments(line: str) -> tuple[bool, list[InteractiveFragment] | None]:
    """``(is_supported_display_command, fragments)``; ``None`` when unreadable."""

    view = _command_payload_view(line)
    if view is None:
        return False, None
    base, _wrapped = view
    payload = line[base:]
    try:
        tree: Any = json.loads(payload)
    except json.JSONDecodeError:
        tree = _wrapped_snbt_tree(line)
        if tree is None:
            return True, None
    fragments: list[InteractiveFragment] = []
    _component_fragments(tree, (), fragments)
    return True, fragments


def _is_furniture(text: str) -> bool:
    """True when a fragment is only alignment marks and whitespace."""

    return all(char in CONTROL_MARKS or char.isspace() for char in text)


def _open_line(text: str) -> str:
    """The text the next fragment starts after: everything past the last break."""

    return text.rsplit("\n", 1)[-1]


def _leading_space(text: str) -> str:
    return text[: len(text) - len(text.lstrip())]


def _mark_prefix(text: str) -> tuple[str, str] | None:
    """``(marks, fixed gap)`` a label starts with, or ``None``."""

    match = re.match(rf"([{re.escape(CONTROL_MARKS)}]+)([ \t]*)", text.lstrip())
    if match is None:
        return None
    return match.group(1), match.group(2)


def control_row_pad(text: str) -> str:
    """Trailing ASCII spaces/tabs of a fragment — part of a clickable row.

    A menu label that ends in spaces keeps them: the pad is what separates it
    from the next mark on the row, and the author's numbers are load-bearing
    (``► Not now. `` and a bare ``▶ `` control cell are real shapes).  Chinese
    prose may be longer or shorter, but it may not silently drop this suffix.

    One definition, two users: the write door compares the pad, and the shared
    candidate repair (``quality.repair_layout_candidate``) puts it back, so a
    reply is never refused for a suffix that can be restored from the source.
    """

    return text[len(text.rstrip(ROW_PAD)) :]


def control_row_gap(text: str) -> str:
    """Leading ASCII spaces/tabs of a fragment — its side of the row's gap.

    A control mark and its label are often split across two components
    (``►`` + `` Sure.``): the gap is then a leading run of the *label's* own
    span.  Only spaces and tabs count — a leading line break is layout too, but
    it belongs to the source's line shape, not to the row's gap.
    """

    return text[: len(text) - len(text.lstrip(ROW_PAD))]


def _line_shape(text: str) -> tuple[int, tuple[int, ...]]:
    """Explicit line offset: real break count and where the empty lines are.

    Only the *author's* breaks are counted — the client's own word wrapping is
    not modelled — which is what makes this comparable between an English
    source and a Chinese translation of any width.
    """

    return text.count("\n"), tuple(index for index, line in enumerate(text.split("\n")) if not line)


def _fragment_layout_reason(
    before: InteractiveFragment,
    after: InteractiveFragment,
    source_prefix: str,
    target_prefix: str,
) -> str | None:
    """Why this fragment pair is not the same interaction, if it is not."""

    source_text, target_text = before.text, after.text
    if source_text is None or target_text is None:
        return None
    if _is_furniture(source_text) and target_text != source_text:
        # Marks and padding carry no language: byte for byte, or the row moved.
        return INTERACTION_LAYOUT
    if not before.clickable:
        # Ordinary prose may be translated, re-wrapped and widened freely; what
        # it may not do is move a control row, which the check below owns.
        return None
    source_open = source_prefix + _leading_space(source_text)
    target_open = target_prefix + _leading_space(target_text)
    if _line_shape(source_open) != _line_shape(target_open):
        # From the start of the message to this clickable fragment the explicit
        # break count (and the blank lines) must be unchanged: a neighbouring
        # leaf that loses a wrap moves the control row exactly like a lost fill
        # line does.  Chinese is allowed to be shorter, never to reflow the menu.
        return INTERACTION_LAYOUT
    source_line = _open_line(source_open)
    target_line = _open_line(target_open)
    if not source_line.strip() and source_line != target_line:
        # The clickable fragment began a line whose leading content was only
        # padding (or nothing): that fill is what places the control row, and a
        # target that shortened it is a layout change even with the same breaks.
        return INTERACTION_LAYOUT
    if source_text.strip() and not target_text.strip():
        return INTERACTION_LAYOUT
    if control_row_pad(target_text) != control_row_pad(source_text):
        # The row's own trailing pad: a label that ends in spaces keeps exactly
        # those spaces (and one that ends in a character does not grow a pad).
        return INTERACTION_LAYOUT
    marks = _mark_prefix(source_text)
    if marks is not None and _mark_prefix(target_text) != marks:
        return INTERACTION_LAYOUT
    return None


def validate_interactive_command_change(source_line: str, target_line: str) -> str | None:
    """Judge one rewritten display command line by its rendered interaction.

    Returns ``None`` when the change keeps the interaction intact --- including
    every line that carries no click event at all --- :data:`INTERACTION_LAYOUT`
    for a proven change (an event added, removed or retargeted, a clickable
    label emptied, a control mark or its fixed gap dropped, a clickable row's
    trailing spaces/tabs changed, the explicit line offset of a clickable
    fragment, or the alignment padding / fill line before it) and
    :data:`INTERACTION_UNVERIFIABLE` when the source carries click events but a
    side cannot be read; both keep the source text.

    The comparison is deliberately text-agnostic: a translation may be longer,
    shorter and differently wide, and ordinary prose without events may reflow.
    What must survive is the interaction: which components are clickable and
    what they run, which visible text they show, the alignment marks, and where
    the control row sits in the message.  Callers that need a blocking judgement
    (the pre-publication audit) turn either reason into an error.
    """

    if not _mentions_click_event(source_line):
        return INTERACTION_LAYOUT if _mentions_click_event(target_line) else None
    in_scope, source_fragments = _interaction_fragments(source_line)
    if not in_scope:
        # Not a tellraw/title payload: every other display carrier (books, item
        # components, NBT and the data-merge TextDisplay commands) has its own
        # door, and the write door never rewrites their component structure.
        return None
    if source_fragments is None:
        return INTERACTION_UNVERIFIABLE
    if not any(fragment.clickable for fragment in source_fragments):
        return None
    target_in_scope, target_fragments = _interaction_fragments(target_line)
    if not target_in_scope or target_fragments is None:
        return INTERACTION_UNVERIFIABLE
    if len(source_fragments) != len(target_fragments):
        return INTERACTION_LAYOUT
    source_prefix = ""
    target_prefix = ""
    for before, after in zip(source_fragments, target_fragments):
        if (before.text is None) != (after.text is None):
            return INTERACTION_LAYOUT
        if before.events != after.events:
            return INTERACTION_LAYOUT
        reason = _fragment_layout_reason(before, after, source_prefix, target_prefix)
        if reason is not None:
            return reason
        source_prefix += before.text or ""
        target_prefix += after.text or ""
    return None


@dataclass(frozen=True, slots=True)
class InteractiveLeaf:
    """One visible leaf of a clickable message, by the pointer of its own text.

    ``previous_pointer`` is the leaf drawn right before it (``None`` for the
    first one) and ``preceded_by_furniture`` says whether that leaf was only
    alignment marks and whitespace.  A leaf rendered from live game data is a
    visible fragment but not furniture, so it never passes furniture on.
    """

    pointer: str
    clickable: bool
    preceded_by_furniture: bool
    previous_pointer: str | None
    before_last_clickable: bool


def _interactive_walk(node: Any, pointer: str, events: tuple[Any, ...], out: list[tuple[str | None, str | None, tuple[Any, ...]]]) -> None:
    """Visible leaves of one payload in render order, with their own pointers.

    The traversal is the gate's own (:func:`_component_fragments`): ``text`` (or
    ``fallback``), then ``with``, then ``extra``, an event object's fields never
    read as body text, and a component rendered from game data contributing a
    leaf without file text.  The pointer is the one the extractor mints for the
    same value (``/5/text`` for the text of the component at ``/5``), which is
    what makes the leaf's identity exact instead of a text search.
    """

    if isinstance(node, str):
        out.append((pointer, node, events))
        return
    if isinstance(node, list):
        for index, child in enumerate(node):
            _interactive_walk(child, f"{pointer}/{index}", events, out)
        return
    if not isinstance(node, dict):
        return
    for key in CLICK_EVENT_KEYS:
        if key in node:
            events = (*events, _event_signature(node[key]))
            break
    own = node.get("text")
    own_key = "text"
    if not isinstance(own, (str, list)):
        own = node.get("fallback")
        own_key = "fallback"
    if isinstance(own, str):
        out.append((f"{pointer}/{own_key}", own, events))
    elif isinstance(own, list):
        _interactive_walk(own, f"{pointer}/{own_key}", events, out)
    for key in ("with", "extra"):
        if key in node:
            _interactive_walk(node[key], f"{pointer}/{key}", events, out)
    if not isinstance(own, str) and any(key in node for key in _DYNAMIC_COMPONENT_KEYS):
        out.append((None, None, events))


def interactive_leaf_contexts(line: str) -> dict[str, InteractiveLeaf]:
    """Layout context of every visible leaf of a clickable display command.

    Only a ``tellraw``/``title`` payload that mentions a click event has one:
    the write door and the independent audit ignore every other line, so no
    other line needs per-leaf context, and a leaf the render walk never reaches
    (a hover tooltip, a command inside the payload) stays on the ordinary
    rules.  An SNBT payload returns nothing: its leaves carry no pointer
    identity yet, and both doors still read the line — a candidate there is
    refused rather than repaired (documented boundary).
    """

    if not _mentions_click_event(line):
        return {}
    view = _command_payload_view(line)
    if view is None:
        return {}
    base, _wrapped = view
    try:
        tree: Any = json.loads(line[base:])
    except json.JSONDecodeError:
        return {}
    ordered: list[tuple[str | None, str | None, tuple[Any, ...]]] = []
    _interactive_walk(tree, "", (), ordered)
    clickable_positions = [
        index for index, (_pointer, text, events) in enumerate(ordered) if text is not None and events
    ]
    last_clickable = clickable_positions[-1] if clickable_positions else None
    contexts: dict[str, InteractiveLeaf] = {}
    furniture_before = False
    previous_pointer: str | None = None
    for index, (pointer, text, events) in enumerate(ordered):
        if text is None or pointer is None:
            furniture_before = False
            previous_pointer = None
            continue
        furniture = _is_furniture(text)
        contexts[pointer] = InteractiveLeaf(
            pointer=pointer,
            clickable=bool(events),
            preceded_by_furniture=furniture_before,
            previous_pointer=previous_pointer,
            before_last_clickable=last_clickable is not None and index < last_clickable,
        )
        furniture_before = furniture
        previous_pointer = pointer
    return contexts
