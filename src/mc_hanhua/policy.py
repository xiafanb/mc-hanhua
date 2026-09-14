"""Conservative field-policy classification for extracted and filtered text.

Values used to identify, reference or match game mechanics are never sent to
the AI. Player-visible display text is allowed only on already-audited safe
paths. Unknown custom fields stay UNKNOWN_REVIEW and are recorded, not
translated.

Classification is metadata only: existing extractors remain the gate that
decides what becomes a TextUnit. Policy never globally unlocks strings.
"""

from __future__ import annotations

import json
from typing import Any

from .models import TextUnit
from .text_components import SNBT_MECHANISM_KEYS, SNBT_NONTEXT_KEYS, SNBT_PARENTED_TEXT_KEYS, SNBT_TEXT_KEYS

POLICY_IMMUTABLE_ID = "IMMUTABLE_ID"
POLICY_IMMUTABLE_MATCH = "IMMUTABLE_MATCH_VALUE"
POLICY_TRANSLATABLE = "TRANSLATABLE_DISPLAY"
POLICY_UNKNOWN = "UNKNOWN_REVIEW"

# Resource / identity keys that participate in commands, selectors, recipes,
# predicates, functions, storage, dimensions, scoreboards, teams and bossbars.
IMMUTABLE_ID_KEYS = {
    "id",
    "uuid",
    "dimension",
    "function",
    "functions",
    "storage",
    "objective",
    "objectives",
    "team",
    "teams",
    "bossbar",
    "recipe",
    "recipes",
    "predicate",
    "predicates",
    "loot_table",
    "advancement",
    "advancements",
    "tag",
    "tags",
    "namespace",
    "type",
    "entity",
    "block",
    "item",
    "target",
    "owner",
    "scheduler",
    "command",
    "pos",
    "rotation",
    "motion",
    "level",
}

# Exact-match / identity fields that must stay byte-identical.
IMMUTABLE_MATCH_KEYS = {
    "author",
    "lock",
    "customname",
    "custom_name",
    "item_name",
    "custom_data",
    "lastoutput",
    "name",  # selector name= and most unscoped Name keys
}

# Already-audited player-visible display keys.
TRANSLATABLE_LEAF_KEYS = {
    "text",
    "fallback",
    "title",
    "subtitle",
    "lore",
    "pages",
    "filtered_pages",
    "raw",
    "filtered",
    "minecraft:custom_name",
    "minecraft:item_name",
    "minecraft:lore",
    "prefix",
    "suffix",
}

TRANSLATABLE_PARENTED = SNBT_PARENTED_TEXT_KEYS | {
    ("display", "Name"),
    ("display", "Lore"),
    ("front_text", "messages"),
    ("back_text", "messages"),
    ("written_book_content", "title"),
    ("writable_book_content", "title"),
    ("minecraft:written_book_content", "title"),
    ("minecraft:writable_book_content", "title"),
    ("teams", "DisplayName"),
    ("teams", "Prefix"),
    ("teams", "Suffix"),
    ("objectives", "DisplayName"),
    ("bossbar", "name"),
}

# Generic JSON keys already treated as display text by GenericJsonTextPlugin.
GENERIC_JSON_TEXT_KEYS = {
    "text",
    "title",
    "subtitle",
    "description",
    "name",
    "display",
    "summary",
    "tooltip",
    "body",
    "label",
    "message",
    "contents",
    "fallback",
    "item_name",
    "custom_name",
    "lore",
    "texts",
    # Dialogue schemas commonly use these as either scalar values or array
    # items.  Keep this list shared with the extractor so policy cannot drift.
    "question",
    "answer",
    "reply",
    "options",
    "choices",
    "dialogue",
}

# JSON object names whose descendants are mechanism payloads.  This is an
# ancestor check, rather than a global leaf-key ban: display text in a
# hoverEvent can still be handled by the existing audited text-key rules.
GENERIC_JSON_MECHANISM_CONTAINERS = {
    "score",
    "custom_data",
    "clickevent",
    "click_event",
}


def _path_from_locator(locator: str) -> list[Any]:
    if not locator:
        return []
    try:
        parsed = json.loads(locator)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if isinstance(parsed, dict):
        path = parsed.get("path")
        if isinstance(path, list):
            return path
        pointer = parsed.get("json") or parsed.get("pointer")
        if isinstance(pointer, str) and pointer:
            return [part for part in pointer.strip("/").split("/") if part]
        return []
    if isinstance(locator, str) and locator.startswith("/"):
        return [part for part in locator.strip("/").split("/") if part]
    return []


def _leaf_and_parent(path: list[Any]) -> tuple[str, str]:
    strings = [str(part) for part in path if isinstance(part, str) or (isinstance(part, int) is False and part is not None)]
    # Prefer the last string key even when the leaf is a list index.
    leaf = ""
    parent = ""
    for part in reversed(path):
        if isinstance(part, str):
            if not leaf:
                leaf = part
            elif not parent:
                parent = part
                break
    if not leaf and strings:
        leaf = strings[-1]
    return leaf, parent


def classify_nbt_path(path: list[Any], *, reason_hint: str = "") -> tuple[str, str]:
    """Classify a standalone NBT path. Conservative: unknown stays UNKNOWN_REVIEW."""

    if reason_hint in {"author_name"}:
        return POLICY_IMMUTABLE_MATCH, "book author is an identity / match field"
    if reason_hint in {"player_name"}:
        return POLICY_IMMUTABLE_MATCH, "player-shaped name is a selector match value"
    if reason_hint in {"entity_name_preserved"}:
        return POLICY_IMMUTABLE_MATCH, "entity CustomName is an identity or internal id"
    if reason_hint in {"unknown_identity"}:
        return POLICY_UNKNOWN, "entity display name needs full-pack reference confirmation"
    if reason_hint in {"command_feedback"}:
        return POLICY_IMMUTABLE_MATCH, "command LastOutput is runtime feedback, not display text"
    if reason_hint in {"mechanism_locked", "reference_locked"}:
        return POLICY_IMMUTABLE_MATCH, reason_hint
    if reason_hint in {"unknown_mechanism_field"}:
        return POLICY_UNKNOWN, "unaudited command SNBT field"
    if reason_hint in {"unparsed_mechanism"}:
        return POLICY_UNKNOWN, "command SNBT failed to parse"
    if reason_hint in {"visual_glyph"}:
        return POLICY_IMMUTABLE_ID, "visual glyph art is a resource, not language"
    if reason_hint in {"nbt_parse_error"}:
        return POLICY_UNKNOWN, "NBT document failed to parse"

    if not path:
        return POLICY_UNKNOWN, "empty NBT path"

    leaf, parent = _leaf_and_parent(path)
    leaf_lower = leaf.lower()
    parent_lower = parent.lower()
    lowered = [str(part).lower() for part in path]

    if (parent, leaf) in TRANSLATABLE_PARENTED or (parent_lower, leaf_lower) in {
        (str(a).lower(), str(b).lower()) for a, b in TRANSLATABLE_PARENTED
    }:
        return POLICY_TRANSLATABLE, f"audited parented display path {parent}/{leaf}"

    if leaf in SNBT_TEXT_KEYS or leaf_lower in TRANSLATABLE_LEAF_KEYS:
        return POLICY_TRANSLATABLE, f"audited display key {leaf}"

    if leaf_lower in IMMUTABLE_ID_KEYS or (leaf_lower in SNBT_MECHANISM_KEYS and leaf_lower not in {"customname"}):
        if leaf_lower in {"id", "uuid", "dimension", "function", "storage", "objective", "team", "bossbar", "recipe", "predicate"}:
            return POLICY_IMMUTABLE_ID, f"resource/identity key {leaf}"
        if leaf_lower in {"tags", "lock", "author", "custom_data", "command", "lastoutput", "owner", "target"}:
            return POLICY_IMMUTABLE_MATCH, f"mechanism match key {leaf}"

    if leaf_lower == "customname" or (leaf_lower == "custom_name" and "minecraft:" not in leaf_lower):
        return POLICY_UNKNOWN, "plain CustomName pending full-pack reference scan"

    if leaf_lower == "author":
        return POLICY_IMMUTABLE_MATCH, "author is never translated"

    if leaf_lower in SNBT_NONTEXT_KEYS and leaf_lower not in TRANSLATABLE_LEAF_KEYS:
        return POLICY_IMMUTABLE_ID, f"non-text component attribute {leaf}"

    if leaf_lower in {"raw", "filtered"} and any(
        marker in lowered
        for marker in {
            "pages",
            "filtered_pages",
            "title",
            "minecraft:written_book_content",
            "minecraft:writable_book_content",
            "written_book_content",
            "writable_book_content",
        }
    ):
        return POLICY_TRANSLATABLE, "standard book raw/filtered page"

    if leaf_lower == "text" and any(marker in lowered for marker in {"front_text", "back_text", "messages", "extra", "text"}):
        return POLICY_TRANSLATABLE, "text component leaf"

    if leaf == "Name" and "display" in lowered:
        return POLICY_TRANSLATABLE, "item display.Name"

    if leaf_lower == "displayname" and ({"objectives", "teams", "bossbars"} & set(lowered)):
        return POLICY_TRANSLATABLE, "scoreboard/team/bossbar display name"

    if leaf_lower in {"prefix", "suffix"} and "teams" in set(lowered):
        return POLICY_TRANSLATABLE, "team prefix/suffix"

    if parent_lower in {"pages", "filtered_pages", "lore", "messages", "extra", "raw", "filtered"}:
        return POLICY_TRANSLATABLE, f"list item under display container {parent}"

    return POLICY_UNKNOWN, f"unaudited NBT field {leaf or '/'}"


def classify_command_path(path: tuple[str, ...] | list[Any], *, reason_hint: str = "") -> tuple[str, str]:
    if reason_hint:
        policy, reason = classify_nbt_path(list(path), reason_hint=reason_hint)
        return policy, reason
    if not path:
        return POLICY_UNKNOWN, "command SNBT with empty key path"
    if _snbt_display_allowed(path):
        return POLICY_TRANSLATABLE, f"audited command display key {'/'.join(map(str, path))}"
    leaf = str(path[-1]).lower()
    if leaf in SNBT_MECHANISM_KEYS or leaf in IMMUTABLE_MATCH_KEYS:
        return POLICY_IMMUTABLE_MATCH, f"command mechanism key {path[-1]}"
    if leaf in IMMUTABLE_ID_KEYS:
        return POLICY_IMMUTABLE_ID, f"command identity key {path[-1]}"
    if leaf in SNBT_NONTEXT_KEYS:
        return POLICY_IMMUTABLE_ID, f"command non-text attribute {path[-1]}"
    return POLICY_UNKNOWN, f"unaudited command field {path[-1]}"


def _snbt_display_allowed(path: tuple[str, ...] | list[Any]) -> bool:
    if not path:
        return False
    key = path[-1]
    if key in SNBT_TEXT_KEYS or str(key).lower() in TRANSLATABLE_LEAF_KEYS:
        return True
    if len(path) >= 2 and (path[-2], key) in SNBT_PARENTED_TEXT_KEYS:
        return True
    return False


def classify_json_pointer(pointer: str, *, format_name: str = "generic-json", reason_hint: str = "") -> tuple[str, str]:
    if reason_hint:
        return classify_nbt_path([], reason_hint=reason_hint)
    parts = [part for part in str(pointer).strip("/").split("/") if part]
    if not parts:
        if format_name in {"json-lang", "legacy-lang"}:
            return POLICY_TRANSLATABLE, "language-file value"
        return POLICY_UNKNOWN, "empty JSON pointer"
    lowered = [part.replace("~1", "/").replace("~0", "~").lower() for part in parts]
    leaf = parts[-1]
    leaf_lower = lowered[-1]
    if format_name in {"json-lang", "legacy-lang"}:
        return POLICY_TRANSLATABLE, "language-file value (keys are never extracted)"
    if any(part in GENERIC_JSON_MECHANISM_CONTAINERS or part.rsplit(":", 1)[-1] == "custom_data" for part in lowered[:-1]):
        return POLICY_IMMUTABLE_MATCH, f"mechanism payload under {'/'.join(parts[:-1])}"
    if leaf_lower == "name" and (len(lowered) < 2 or lowered[-2] not in {
        "display", "item", "entity", "dialog", "dialogue", "option", "choice"
    }):
        return POLICY_UNKNOWN, f"unaudited JSON name field {leaf}"
    if leaf_lower in GENERIC_JSON_TEXT_KEYS:
        return POLICY_TRANSLATABLE, f"generic-json display key {leaf}"
    # A list item inherits the role of its immediate parent only.  This
    # handles description/question arrays without allowing an arbitrary
    # ancestor object to bless unrelated descendants.
    if leaf.isdigit() and len(lowered) >= 2 and lowered[-2] in GENERIC_JSON_TEXT_KEYS:
        if lowered[-2] == "name" and (len(lowered) < 3 or lowered[-3] not in {
            "display", "item", "entity", "dialog", "dialogue", "option", "choice"
        }):
            return POLICY_UNKNOWN, "unaudited JSON name array"
        return POLICY_TRANSLATABLE, f"generic-json display array item under {parts[-2]}"
    return POLICY_UNKNOWN, f"unaudited JSON field {leaf}"


def _command_payload_policy(locator: dict[str, Any]) -> tuple[str, str] | None:
    """Classify an embedded command payload by SNBT/JSON role, not the Command key."""

    command = locator.get("command")
    if not isinstance(command, dict):
        return None
    kind = command.get("kind")
    if kind == "plain":
        return POLICY_TRANSLATABLE, "say/plain command payload"
    if kind in {"json", "json_slice"}:
        return POLICY_TRANSLATABLE, "tellraw/title/bossbar JSON text"
    if kind == "snbt":
        # Extraction already restricted these spans to SNBT display keys.
        return POLICY_TRANSLATABLE, "audited command SNBT display value"
    return None


def classify_unit(unit: TextUnit) -> tuple[str, str]:
    """Assign policy to an extracted unit without changing extraction."""

    existing = getattr(unit, "policy", "") or (unit.metadata or {}).get("policy")
    existing_reason = getattr(unit, "policy_reason", "") or (unit.metadata or {}).get("policy_reason") or "preset"
    # Embedded command payloads must be classified by SNBT/JSON role even when
    # a prior pass stamped IMMUTABLE because the outer NBT leaf is Command.
    try:
        parsed_locator = json.loads(unit.locator) if unit.locator else None
    except (TypeError, ValueError, json.JSONDecodeError):
        parsed_locator = None
    if isinstance(parsed_locator, dict):
        payload = _command_payload_policy(parsed_locator)
        if payload is not None:
            if existing in {POLICY_IMMUTABLE_ID, POLICY_UNKNOWN}:
                return str(existing), str(existing_reason)
            if existing == POLICY_IMMUTABLE_MATCH and existing_reason in {
                "mechanism_locked",
                "reference_locked",
                "value consumed by a match clause",
            }:
                return str(existing), str(existing_reason)
            return payload

    if existing in {POLICY_IMMUTABLE_ID, POLICY_IMMUTABLE_MATCH, POLICY_TRANSLATABLE, POLICY_UNKNOWN}:
        return str(existing), str(existing_reason)

    if unit.format == "nbt-text":
        return classify_nbt_path(_path_from_locator(unit.locator))
    if unit.format == "mcfunction":
        try:
            locator = json.loads(unit.locator)
        except (TypeError, ValueError, json.JSONDecodeError):
            return POLICY_TRANSLATABLE, "command display text"
        command = locator.get("command") if isinstance(locator, dict) else None
        if isinstance(command, dict) and command.get("kind") == "plain":
            return POLICY_TRANSLATABLE, "say/plain command payload"
        if isinstance(command, dict) and command.get("kind") in {"json", "json_slice"}:
            return POLICY_TRANSLATABLE, "tellraw/title/bossbar JSON text"
        if isinstance(command, dict) and command.get("kind") == "snbt":
            # SNBT extraction already restricted to SNBT_TEXT_KEYS.
            return POLICY_TRANSLATABLE, "audited command SNBT display value"
        return POLICY_TRANSLATABLE, "command display text"
    if unit.format in {"json-lang", "legacy-lang"}:
        return POLICY_TRANSLATABLE, "language-file value"
    if unit.format == "generic-json":
        return classify_json_pointer(unit.locator, format_name=unit.format)
    return POLICY_UNKNOWN, f"unknown format {unit.format}"


def apply_unit_policy(unit: TextUnit) -> TextUnit:
    policy, reason = classify_unit(unit)
    unit.policy = policy
    unit.policy_reason = reason
    unit.metadata.setdefault("policy", policy)
    unit.metadata.setdefault("policy_reason", reason)
    return unit


def should_send_to_ai(policy: str) -> bool:
    return policy == POLICY_TRANSLATABLE
