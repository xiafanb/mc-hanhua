from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from .content_safety import candidate_content_reason, display_container_identity
from .models import RiskLevel, TextUnit, TranslationGroup, structured_locator
from .text_components import (
    INTERACTIVE_LEAF_METADATA,
    ROW_PAD,
    control_row_gap,
    control_row_pad,
    split_encoded_component_locator,
)
from .utils import PLACEHOLDER_RE, normalize_layout_newlines, peel_text_edges, placeholder_tokens_match, stable_id

#: Rule generation for every stored candidate.  Raised whenever the gate's
#: acceptance changes, so rows written under an older grammar become unreachable
#: instead of answering a rule set they were never judged by.
QUALITY_CACHE_VERSION = "context-v9"
BOOK_CACHE_VERSION = "book-context-v4"
LAYOUT_CACHE_VERSION = "layout-v4"
#: Name of the input-version-scoped translation memory. The rule generation is
#: part of the file name (as it is part of every context key) because rows
#: written under an older grammar are unreachable and must not look like a
#: working cache when an upgraded build opens the same directory.
MEMORY_DB_FILENAME = f"translation-memory-{QUALITY_CACHE_VERSION}.sqlite"
#: Unit metadata key for a source a provider kept on purpose — and the outcome
#: name the report files it under.  The provider writes it at the decision
#: (``keepable_short_label``), and the pipeline leaves it in place while it skips
#: applying and caching the unchanged text, so ``unit_outcome_status`` can tell a
#: deliberate keep from a unit that never produced a candidate.
INTENTIONAL_KEEP = "intentional_keep"
MAX_GROUP_SEGMENTS = 12
MAX_GROUP_CHARS = 3000
LANG_BATCH_FORMATS = {"json-lang", "legacy-lang"}
LANG_BATCH_SEGMENTS = 48
LANG_BATCH_CHARS = 9000
URL_RE = re.compile(r"https?://[^\s\"']+", re.IGNORECASE)
COMMAND_RE = re.compile(r"(?<![A-Za-z0-9_])/[a-z][a-z0-9:_-]*", re.IGNORECASE)
FORMAT_CODE_RE = re.compile(r"§[0-9A-FK-ORa-fk-or]")
STRUCTURAL_TOKEN_PATTERNS = (URL_RE, COMMAND_RE, PLACEHOLDER_RE, FORMAT_CODE_RE)
_TRAILING_INT_RE = re.compile(r"(\d+)$")
# Signs continue on the same Y level, one or two blocks apart on a single axis.
_SIGN_NEIGHBOR_OFFSETS = ((-2, 0), (-1, 0), (1, 0), (2, 0), (0, -2), (0, -1), (0, 1), (0, 2))


_BOOK_PATH_MARKERS = (
    "written_book_content",
    "writable_book_content",
    "minecraft:written_book_content",
    "minecraft:writable_book_content",
)


def is_book_page_unit(unit: TextUnit) -> bool:
    """True for written/writable book page or title NBT, not signs or commands."""

    if unit.format != "nbt-text":
        return False
    haystack = f"{unit.locator} {unit.context}".lower()
    if any(marker in haystack for marker in _BOOK_PATH_MARKERS):
        return True
    if '"pages"' in haystack or "/pages" in haystack or haystack.endswith("pages") or " pages" in haystack:
        return True
    if "filtered_pages" in haystack:
        return True
    # Bare locator used by book-page unit tests (context/locator == "page").
    if unit.context.lower() == "page" or str(unit.locator).lower() == "page":
        return True
    try:
        locator = json.loads(unit.locator)
        path = locator.get("path", []) if isinstance(locator, dict) else []
    except (TypeError, ValueError, json.JSONDecodeError):
        path = []
    if isinstance(path, list) and any(str(part).lower() in {"pages", "filtered_pages"} for part in path):
        return True
    return False


def is_multiline_layout_unit(unit: TextUnit) -> bool:
    """True for proven sign, TextDisplay, and book-page display containers."""

    if unit.metadata.get("layout_container") in {"sign", "text-display"}:
        return True
    return is_book_page_unit(unit)


def quality_memory_context(unit: TextUnit) -> str:
    owner, container = semantic_owner(unit)
    version = LAYOUT_CACHE_VERSION if container in {"sign", "text-display"} else (BOOK_CACHE_VERSION if container == "book" else QUALITY_CACHE_VERSION)
    context = f"{version}|{unit.file_path}|{unit.format}|{owner}|{unit.context}"
    # Nested archives share in-archive file paths; without the chain in the
    # namespace, one archive's translations would leak into another's (A08).
    chain = unit.metadata.get("archive_chain") or []
    if chain:
        context += "|archive:" + ">".join(str(part) for part in chain)
    return context


def build_translation_groups(units: list[TextUnit]) -> list[TranslationGroup]:
    """Build bounded, ordered groups from Minecraft semantic containers."""

    spatial_owners = _spatial_sign_owners(units)
    buckets: dict[tuple[str, str, str], list[TextUnit]] = defaultdict(list)
    container_types: dict[tuple[str, str, str], str] = {}
    spatial_clusters: set[tuple[str, str, str]] = set()
    for unit in units:
        owner, container_type = semantic_owner(unit)
        spatial_owner = spatial_owners.get(unit.id)
        if spatial_owner:
            owner = spatial_owner
            unit.metadata["layout_cluster_order"] = spatial_owners.order_for(unit.id)
        key = (unit.file_path, unit.format, owner)
        buckets[key].append(unit)
        container_types[key] = container_type
        if spatial_owner:
            spatial_clusters.add(key)

    groups: list[TranslationGroup] = []
    for (file_path, format_name, owner), members in buckets.items():
        members.sort(key=_unit_order)
        container_type = container_types[(file_path, format_name, owner)]
        layout_reflow = container_type in {"sign", "text-display"}
        for offset, chunk in _chunk_members(members, format_name, container_type):
            group_context = f"{owner}#{offset + 1}"
            groups.append(
                TranslationGroup(
                    id=stable_id(QUALITY_CACHE_VERSION, file_path, format_name, group_context, *(unit.id for unit in chunk)),
                    file_path=file_path,
                    format=format_name,
                    context=group_context,
                    units=chunk,
                    container_type=container_type,
                    requires_review=(
                        container_type in {"sign", "book", "text-display", "dialogue"}
                        or any(unit.risk_level.value == "high" for unit in chunk)
                    ),
                    review_only=all(unit.suggested_target is not None for unit in chunk),
                    layout_reflow=layout_reflow,
                    spatial_sign_cluster=(file_path, format_name, owner) in spatial_clusters,
                )
            )
    return groups


def estimate_group_requests(groups: list[TranslationGroup]) -> tuple[int, int]:
    return (
        sum(1 for group in groups if not group.review_only),
        sum(1 for group in groups if group.requires_review),
    )


def semantic_owner(unit: TextUnit) -> tuple[str, str]:
    """Return a stable container ID without relying on a pack-specific path."""

    if unit.format == "mcfunction":
        return "commands", "function"
    if unit.format in {"json-lang", "legacy-lang"}:
        return "file", "language"
    if unit.format == "generic-json":
        return _json_owner(unit.locator)
    if unit.format == "nbt-text":
        layout_container = str(unit.metadata.get("layout_container", ""))
        layout_owner = unit.metadata.get("layout_owner")
        if layout_container in {"sign", "text-display"} and isinstance(layout_owner, str) and layout_owner:
            return layout_owner, layout_container
        return _nbt_owner(unit)
    return unit.context or "file", "generic"


def _json_locator_pointers(locator: str) -> tuple[str, str]:
    """``(outer pointer, inner component pointer)`` for a generic-json locator.

    Plain locators (a JSON pointer into the file) have an empty inner pointer;
    a JSON-encoded component leaf carries both parts so its identity stays
    distinct from a sibling holding the same text.
    """

    encoded = split_encoded_component_locator(locator)
    return encoded if encoded is not None else (str(locator), "")


def _json_owner(locator: str) -> tuple[str, str]:
    outer, _inner = _json_locator_pointers(locator)
    parts = [part.replace("~1", "/").replace("~0", "~") for part in outer.lstrip("/").split("/") if part]
    lowered = [part.lower() for part in parts]
    for index, part in enumerate(lowered):
        if part in {"messages", "pages", "lore", "lines"}:
            return "/".join(parts[: index + 1]) or "root", "book" if part == "pages" else "text-list"
        if part in {"dialog", "dialogue", "dialogs"}:
            end = index + 1
            if index + 1 < len(parts) and parts[index + 1].isdigit():
                end = index + 2
            return "/".join(parts[:end]) or "root", "dialogue"
        if part in {"options", "choices"}:
            return "/".join(parts[:index]) or "root", "dialogue"
    return "/".join(parts[:-1]) or "root", "json-object"


def _nbt_owner(unit: TextUnit) -> tuple[str, str]:
    locator = structured_locator(unit)
    if locator is None:
        return unit.context or "nbt", "nbt"
    raw_path = locator.get("path", [])
    parts = [str(part) for part in raw_path] if isinstance(raw_path, list) else unit.context.split("/")
    chunk = str(locator.get("chunk", "root"))
    lower = [part.lower() for part in parts]
    for index, part in enumerate(lower):
        if part in {"front_text", "back_text"}:
            return f"chunk:{chunk}/{'/'.join(parts[: index + 1])}", "sign"
        if part == "pages":
            return f"chunk:{chunk}/{'/'.join(parts[: index + 1])}", "book"
        if part in {"lore", "minecraft:lore"}:
            return f"chunk:{chunk}/{'/'.join(parts[: index + 1])}", "lore"
    for collection in ("block_entities", "entities", "objectives", "teams"):
        if collection in lower:
            index = lower.index(collection)
            return f"chunk:{chunk}/{'/'.join(parts[: index + 1])}", "nbt-collection"
    return f"chunk:{chunk}/{'/'.join(parts[:-1]) or 'root'}", "nbt"


class _SpatialSignOwners(dict[str, str]):
    def __init__(self) -> None:
        super().__init__()
        self._orders: dict[str, tuple[int, int]] = {}

    def add(self, unit: TextUnit, owner: str, sign_index: int) -> None:
        self[unit.id] = owner
        self._orders[unit.id] = (sign_index, int(unit.metadata.get("layout_line", 0)))

    def order_for(self, unit_id: str) -> tuple[int, int]:
        return self._orders[unit_id]


def _spatial_sign_owners(units: list[TextUnit]) -> _SpatialSignOwners:
    """Merge only clearly continuous, nearby map signs into one reading group."""

    signs: dict[tuple[str, str], dict[str, list[TextUnit]]] = defaultdict(lambda: defaultdict(list))
    positions: dict[tuple[str, str], tuple[int, int, int]] = {}
    for unit in units:
        if unit.format != "nbt-text" or unit.metadata.get("layout_container") != "sign":
            continue
        owner = unit.metadata.get("layout_owner")
        position = unit.metadata.get("layout_position")
        side = _sign_side(owner)
        if not isinstance(owner, str) or not isinstance(position, (tuple, list)) or len(position) != 3 or not side:
            continue
        try:
            positions[(unit.file_path, owner)] = tuple(int(value) for value in position)
        except (TypeError, ValueError):
            continue
        signs[(unit.file_path, side)][owner].append(unit)

    result = _SpatialSignOwners()
    for (file_path, _side), sign_units in signs.items():
        owners = sorted(sign_units)
        reading = {owner: _sign_reading_text(sign_units[owner]) for owner in owners}
        by_position: dict[tuple[int, int, int], list[str]] = defaultdict(list)
        for owner in owners:
            by_position[positions[(file_path, owner)]].append(owner)
        outgoing: dict[str, list[str]] = defaultdict(list)
        incoming: dict[str, list[str]] = defaultdict(list)
        for source_owner in owners:
            x, y, z = positions[(file_path, source_owner)]
            for dx, dz in _SIGN_NEIGHBOR_OFFSETS:
                for target_owner in by_position.get((x + dx, y, z + dz), ()):
                    if target_owner != source_owner and _sign_continues(reading[source_owner], reading[target_owner]):
                        outgoing[source_owner].append(target_owner)
                        incoming[target_owner].append(source_owner)

        next_owner = {
            source_owner: targets[0]
            for source_owner, targets in outgoing.items()
            if len(targets) == 1 and len(incoming[targets[0]]) == 1
        }
        previous = {target for target in next_owner.values()}
        for start in sorted(owner for owner in next_owner if owner not in previous):
            chain = [start]
            while chain[-1] in next_owner:
                chain.append(next_owner[chain[-1]])
            if len(chain) < 2:
                continue
            cluster_owner = f"sign-cluster:{stable_id(file_path, *chain)}"
            for sign_index, sign_owner in enumerate(chain):
                for unit in sign_units[sign_owner]:
                    result.add(unit, cluster_owner, sign_index)
    return result


def _sign_side(owner: object) -> str:
    if not isinstance(owner, str):
        return ""
    return owner.rsplit(":", 1)[-1]


def _sign_reading_text(units: list[TextUnit]) -> str:
    return " ".join(unit.source_text.strip() for unit in sorted(units, key=_unit_order) if unit.source_text.strip())


def _sign_continues(first_text: str, second_text: str) -> bool:
    if not first_text or not second_text or first_text[-1] in ".!?。！？":
        return False
    first_char = second_text[0]
    return first_char.islower() or first_char.isdigit() or first_char in "([{'\""


def _chunk_members(
    members: list[TextUnit],
    format_name: str,
    container_type: str = "",
) -> list[tuple[int, list[TextUnit]]]:
    chunks: list[tuple[int, list[TextUnit]]] = []
    current: list[TextUnit] = []
    current_chars = 0
    previous_line: int | None = None
    # Language files are uniform key->text entries: low-risk, order-insensitive,
    # and safe to batch densely. Fewer, larger requests cut API call volume for
    # large mod language files without touching contextual formats.
    low_risk_lang = format_name in LANG_BATCH_FORMATS and all(unit.risk_level != RiskLevel.HIGH for unit in members)
    book_container = container_type == "book"
    # A whole written book should stay in one request when it fits the char
    # budget. Segment count (12) was splitting Ruralist guides mid-sentence.
    max_segments = LANG_BATCH_SEGMENTS if low_risk_lang else (10**9 if book_container else MAX_GROUP_SEGMENTS)
    max_chars = LANG_BATCH_CHARS if low_risk_lang else MAX_GROUP_CHARS
    for unit in members:
        line = _mcfunction_line(unit) if format_name == "mcfunction" else None
        separated = line is not None and previous_line is not None and line - previous_line > 4
        page = _book_page_index(unit) if book_container else None
        current_page = _book_page_index(current[-1]) if book_container and current else None
        exceeds_chars = current and current_chars + len(unit.source_text) > max_chars
        exceeds_segments = len(current) >= max_segments
        # Over-budget books split on a page boundary, never inside pages/N.
        page_break = book_container and exceeds_chars and page is not None and current_page is not None and page != current_page
        exceeds = (not book_container and (exceeds_segments or exceeds_chars)) or page_break
        same_payload = bool(current and display_container_identity(unit) is not None
                            and display_container_identity(unit) == display_container_identity(current[-1]))
        if current and (separated or exceeds) and not same_payload:
            chunks.append((len(chunks), current))
            current = []
            current_chars = 0
        current.append(unit)
        current_chars += len(unit.source_text)
        previous_line = line
    if current:
        chunks.append((len(chunks), current))
    return chunks


def _book_page_index(unit: TextUnit) -> int | None:
    try:
        locator = json.loads(unit.locator)
        path = locator.get("path") if isinstance(locator, dict) else None
        parts = [str(part) for part in path] if isinstance(path, list) else []
    except (TypeError, ValueError, json.JSONDecodeError):
        parts = []
    if not parts:
        parts = [part for part in (unit.context or "").split("/") if part]
    lowered = [part.lower() for part in parts]
    for index, part in enumerate(lowered):
        if part == "pages" and index + 1 < len(parts):
            try:
                return int(parts[index + 1])
            except (TypeError, ValueError):
                return None
    return None


def _json_pointer_order(locator: str) -> tuple:
    outer, inner = _json_locator_pointers(locator)
    parts = [part.replace("~1", "/").replace("~0", "~") for part in str(outer).lstrip("/").split("/") if part]
    parts.extend(part.replace("~1", "/").replace("~0", "~") for part in str(inner).lstrip("/").split("/") if part)
    numbered: list[int | str] = []
    for part in parts:
        numbered.append(int(part) if part.isdigit() else part.lower())
    return (len(parts), tuple(numbered))


def _unit_order(unit: TextUnit) -> tuple:
    if unit.format == "generic-json":
        return (0, _json_pointer_order(unit.locator), unit.context)
    if semantic_owner(unit)[1] == "book":
        try:
            path = (structured_locator(unit) or {}).get("path", [])
            page_index = next(i for i, part in enumerate(path) if part == "pages")
            page = int(path[page_index + 1])
            tail = path[page_index + 2:]
            # Root text renders before extra, regardless of compound storage order.
            slot = int(tail[tail.index("extra") + 1]) + 1 if "extra" in tail else 0
            return (page * 10000 + slot, unit.context)
        except (ValueError, TypeError, StopIteration, IndexError, KeyError):
            pass
    layout_order = unit.metadata.get("layout_cluster_order")
    if isinstance(layout_order, tuple) and len(layout_order) == 2:
        return (int(layout_order[0]) * 10000 + int(layout_order[1]), unit.context)
    layout_slot = unit.metadata.get("layout_slot")
    if isinstance(layout_slot, tuple) and layout_slot:
        return (int(layout_slot[0]) * 10000 + (int(layout_slot[1]) if len(layout_slot) > 1 else 0), unit.context)
    layout_line = unit.metadata.get("layout_line")
    if isinstance(layout_line, int):
        return (layout_line, unit.context)
    line = _mcfunction_line(unit)
    return (line if line is not None else 0, unit.context)


def _mcfunction_line(unit: TextUnit) -> int | None:
    if unit.format != "mcfunction":
        return None
    try:
        locator = json.loads(unit.locator)
        return int(locator["line"])
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        match = _TRAILING_INT_RE.search(unit.context)
        return int(match.group(1)) - 1 if match else None


def _tokens(value: str, pattern: re.Pattern[str]) -> Counter[str]:
    # Count whole matches, never ``findall`` groups: a pattern with one group
    # returns that group's text, and an empty string whenever the group does not
    # participate — added alternatives outside the group would compare as equal
    # on both sides and the gate would pass silently.
    counter: Counter[str] = Counter()
    for match in pattern.finditer(value):
        counter[match.group(0)] += 1
    return counter


def _blank_line_indices(text: str) -> tuple[int, ...]:
    """Positions of empty lines; consecutive blank lines are structural."""

    return tuple(index for index, line in enumerate(text.split("\n")) if not line)


_CLUE_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_CLUE_JUMP_RE = re.compile(r"\b\d+\.\d+\b")


def clue_token_drift(source: str, target: str) -> list[str]:
    """Return clue markers whose numbers or jumps changed; not a semantic pass."""

    reasons: list[str] = []
    if Counter(_CLUE_NUMBER_RE.findall(source)) != Counter(_CLUE_NUMBER_RE.findall(target)):
        reasons.append("clue_number")
    if Counter(_CLUE_JUMP_RE.findall(source)) != Counter(_CLUE_JUMP_RE.findall(target)):
        reasons.append("clue_jump")
    source_neg = any(token in source.lower() for token in (" not ", "n't", "unless", "without"))
    target_neg = any(token in target for token in ("不", "未", "非", "没有"))
    if source_neg and not target_neg:
        reasons.append("clue_negation")
    return reasons


def joined_visible_text(units: list[TextUnit]) -> str:
    ordered = sorted(units, key=_unit_order)
    return "".join((unit.final_target if unit.final_target is not None else unit.source_text) for unit in ordered)


def container_display_risk(units: list[TextUnit], *, limit: int = 400) -> str | None:
    """Warn from concatenated container text, never a single-unit character count."""

    visible = joined_visible_text(units)
    if len(visible) > limit:
        return "display_capacity"
    return None


def unit_outcome_status(unit: TextUnit) -> str:
    """Mutually exclusive player-facing outcome for a scanned unit."""

    if unit.metadata.get("writeback_status") == "failed":
        return "writeback_failed"
    if unit.metadata.get("candidate_validation"):
        return "candidate_rejected"
    if unit.metadata.get("writeback_status") == "blocked":
        return "source_preserved"
    if unit.policy and unit.policy != "TRANSLATABLE_DISPLAY" and (unit.final_target in {None, unit.source_text}):
        return "mechanism_locked"
    if unit.final_target is None:
        # A source the provider returned on purpose (a keepable short label) is
        # a deliberate keep, not a missing translation: the pipeline must not
        # apply or cache the unchanged text, so without this marker the unit is
        # indistinguishable from one that never produced a candidate.
        return INTENTIONAL_KEEP if unit.metadata.get(INTENTIONAL_KEEP) else "pending"
    if unit.final_target == unit.source_text:
        return "source_preserved"
    if unit.metadata.get("writeback_status") in {"changed", "unchanged"} or looks_like_chinese_target(unit.final_target):
        return "written"
    return "pending"


def looks_like_chinese_target(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


_SHORT_UPPER_LABEL_RE = re.compile(r"^[A-Z]{2,6}$")
_SHORT_WORD_LABEL_RE = re.compile(r"^[A-Za-z]{1,8}$")
_BRACKET_PLACEHOLDER_RE = re.compile(r"^(?:\[[A-Z0-9_]+\]|\{[A-Z0-9_]+\}|<[A-Z0-9_]+>)$")
_VOWELLESS_RUN_RE = re.compile(r"^[A-Za-z]{6,}$")


def keepable_short_label(text: str) -> bool:
    """True for labels that may stay English without counting as a failure.

    One definition for every reader: the provider asks it to decide a kept
    candidate, and the pipeline asks it (before writing the ``intentional_keep``
    marker) to prove that a source it is about to skip really was such a label.
    """

    stripped = peel_text_edges((text or "").strip())[1].strip()
    if not stripped or "\n" in text or " " in stripped:
        return False
    if _SHORT_UPPER_LABEL_RE.fullmatch(stripped):
        return True
    if _SHORT_WORD_LABEL_RE.fullmatch(stripped) and "." not in stripped:
        return True
    if _BRACKET_PLACEHOLDER_RE.fullmatch(stripped):
        return True
    if _VOWELLESS_RUN_RE.fullmatch(stripped) and not re.search(r"[aeiouyAEIOUY]", stripped):
        return True
    return False


def structural_tokens_match(source: str, target: str) -> bool:
    """True when every non-linguistic token of the source survived in the target.

    The single definition of that question: URLs, command fragments and ``§``
    codes are compared as exact multisets, and placeholders through
    ``utils.placeholder_tokens_match``, which carries the one proven rewrite (an
    official-style indexed conversion for a plain one, see
    ``utils.indexed_conversions_equivalent``).  ``validate_contextual_target``
    and the independent audit's token comparison both read this function, so the
    two can only agree — they keep disagreeing about nothing but the *grammar*
    they already share by design.
    """

    for pattern in STRUCTURAL_TOKEN_PATTERNS:
        if pattern is PLACEHOLDER_RE:
            if not placeholder_tokens_match(source, target):
                return False
        elif _tokens(source, pattern) != _tokens(target, pattern):
            return False
    return True


def validate_contextual_target(source: str, target: str, strict_newlines: bool = False) -> str | None:
    """Reject candidates that alter non-linguistic text payloads.

    Every translation route calls this gate before a target can be written.  It
    intentionally compares tokens as multisets: duplicating or dropping a URL,
    command, placeholder, or formatting code is just as unsafe as inventing one,
    and ``structural_tokens_match`` holds the whole rule.

    strict_newlines additionally requires real newline count and blank-line
    positions to survive exactly (book pages and other fixed line layout text).
    """

    if not structural_tokens_match(source, target):
        return "structural_token"
    content_reason = candidate_content_reason(source, target)
    if content_reason:
        return content_reason
    if strict_newlines and "\n" in source:
        if target.count("\n") != source.count("\n") or _blank_line_indices(source) != _blank_line_indices(target):
            return "newline_structure"
    if target.count("\n") > source.count("\n") + 1:
        return "unexpected_expansion"
    if len(target.strip()) > max(80, len(source.strip()) * 4 + 16):
        return "unexpected_expansion"
    return None


class ReuseResult(NamedTuple):
    """A candidate that passed the reuse gate, or why it was rejected."""

    normalized: str | None
    reason: str | None


def validate_passage_member(unit: TextUnit, candidate: object) -> str | None:
    """Per-member gate of a passage revision (§3.12); ``None`` = acceptable.

    Every check of ``validate_reuse`` that is not about line shape applies
    unchanged: tokens, content rules, length, emptiness and puzzle protection.
    The two shape rules — per-member break count / rebuild and the +1 break cap —
    are replaced by passage-level conservation in ``passage_layout``, which the
    caller must run on the whole passage.  The candidate is never rewritten: a
    member that would need normalising is refused instead.
    """

    if not isinstance(candidate, str):
        return "invalid_candidate_type"
    source = unit.source_text
    if unit.metadata.get("content_preservation") and candidate != source:
        return "puzzle_content"
    if normalize_candidate_scaffolding(unit, candidate) != candidate:
        return "candidate_scaffolding"
    if not structural_tokens_match(source, candidate):
        return "structural_token"
    content_reason = candidate_content_reason(source, candidate)
    if content_reason:
        return content_reason
    if len(candidate.strip()) > max(80, len(source.strip()) * 4 + 16):
        return "unexpected_expansion"
    if source.strip() and not candidate.strip():
        return "empty_candidate"
    return None


@dataclass(frozen=True)
class NewlineRule:
    """How one unit's real line breaks may change.

    ``strict``: real break count and blank-line positions must survive exactly.
    ``repair``: escaped breaks may be normalized and stripped boundaries restored
    before the strict check, so a reflowing candidate is repaired rather than
    rejected.
    ``reflow``: the internal breaks wrap one prose passage (a dialogue block or
    tooltip the author wrapped by hand), so a candidate that reflowed the text
    may be re-wrapped across the source's own line shape instead of being
    rejected. Only ever set together with ``strict``: the rule being served is
    still "the shape survives exactly".
    """

    strict: bool
    repair: bool
    reflow: bool = False


def interactive_leaf_flags(unit: TextUnit) -> tuple[bool, bool, bool]:
    """``(clickable, gap_after_furniture, layout_breaks)`` for one span.

    Stamped by the scanner for the leaves of a clickable display command
    (``text_components.INTERACTIVE_LEAF_METADATA``); anything else — a sign, an
    NBT string, a synthetic probe unit a provider builds for a joined layout
    request — reads as "not part of a clickable message" and keeps the ordinary
    rules.
    """

    declared = unit.metadata.get(INTERACTIVE_LEAF_METADATA)
    if not isinstance(declared, dict):
        return False, False, False
    return (
        bool(declared.get("clickable")),
        bool(declared.get("gap_after_furniture")),
        bool(declared.get("layout_breaks")),
    )


_FIXED_DIVIDER_ROW_RE = re.compile(r"(?m)^[ \t]*={4,}[ \t]*$")


def _has_fixed_divider_row(text: str) -> bool:
    """Whether a display span contains an independent ``====`` layout row."""

    return bool(_FIXED_DIVIDER_ROW_RE.search(text))


def newline_rule(unit: TextUnit) -> NewlineRule:
    """Line-break policy per display container.

    Layout containers (signs, TextDisplay, book pages) reflow following their own
    wrapping, so only the boundaries are restored. Everything else that carries
    layout-by-position breaks — an NBT text block, or any text whose source starts,
    ends, or contains a blank line, or ends in a spaces-only fill line (chat-menu
    padding, dialogue blocks) — must keep them: RL maps paint layout with
    newlines, and a model that silently reflows the text moves the whole menu.

    Non-layout NBT text keeps the exact-shape rule without the prose reflow: a
    single NBT string is as often a stored value as a wrapped paragraph, and the
    surrounding bytes give no way to tell wrapping from data.

    A span that sits before a clickable fragment of the *same message* (a chat
    menu, stamped by the scanner) is layout by the same argument: the write door
    compares the explicit break count and blank-line positions from the start of
    the message to every clickable fragment, so a reply that reflows such a span
    moves the control row.  It is therefore strict, and it reflows under the
    proof of ``restore_prose_line_shape`` instead of being thrown away.

    An independent ``====`` row is layout even without a click event.  Such a
    span is strict and proof-reflowing: a missing row can be restored from the
    source, while a row glued into prose or redrawn ambiguously is rejected.
    """

    if isinstance(unit.metadata.get("fragment_merge"), dict):
        # A reviewed sentence fragment is layout, not a sentence of its own: its
        # break belongs to the passage that continues on the next segment, so
        # the newline shape is restored rather than required verbatim.  The text
        # itself is not lost — ``audit._audit_fragment_merges`` proves the
        # passage carries it.
        return NewlineRule(strict=False, repair=True)
    if is_multiline_layout_unit(unit):
        return NewlineRule(strict=False, repair=True)
    source = unit.source_text or ""
    if _has_fixed_divider_row(source):
        # A fixed divider is layout even on a non-clickable command.  Its row
        # boundary must survive exactly; the proof-style prose repair may put a
        # dropped row back, while an ambiguous row glued into prose is rejected.
        return NewlineRule(strict=True, repair=True, reflow=True)
    if interactive_leaf_flags(unit)[2] and "\n" in source:
        # This span sits before a clickable fragment of the message, so its own
        # explicit breaks are exactly what the write door compares: they place
        # the control row.  Keep them, and rebuild a reply that read the
        # paragraph instead of the wrapping.  Checked after the fragment rule on
        # purpose — a reviewed fragment's break belongs to the passage around
        # it, never to this rule.
        return NewlineRule(strict=True, repair=True, reflow=True)
    if unit.format == "nbt-text":
        return NewlineRule(strict=True, repair=False)
    padding = "\n" in source and (
        source.startswith("\n")
        or source.endswith("\n")
        or "\n\n" in source
        # A trailing run of spaces/tabs is a fill line: the author uses it to
        # keep the next component (typically a clickable control row) on a line
        # of its own, right-aligned.  Without this branch such a span looks like
        # ordinary single-break prose, so a candidate that drops both the break
        # and the fill is accepted and the button ends up glued to the sentence.
        or bool(_TRAILING_FILL_RE.search(source))
    )
    return NewlineRule(strict=padding, repair=padding, reflow=padding)


#: Ends a sentence: the natural place to end a rebuilt line.
_SENTENCE_ENDS = "。！？!?…"
#: Ends a clause, or is whitespace: acceptable, slightly less natural.
_CLAUSE_ENDS = "，、；：,;:"
#: A rebuilt line may not start with these — a closing mark would be orphaned
#: (``…需要它们。”`` must not become ``”`` on a line of its own), and the second
#: character of ``……`` or ``!!`` belongs to the run before it.
_NO_LINE_START = "。，、；：！？…·～）〕］｝〉》」』】”’!?,.:;)]}"
#: A rebuilt line may not end with an opening mark or a bare backslash.
_NO_LINE_END = "（〔［｛〈《「『【“‘([{\\"
#: A list row — the source's breaks separate rows, so they are never re-wrapped.
_ROW_START_RE = re.compile(r"^\s*(?:[-*•·▪◦>►▶]|\(?\d{1,2}[.)、])")
_BULLET_ROW_RE = re.compile(r"^([ \t]*•[ \t]+)(\S.*)$")
#: A final line made of spaces/tabs only — the map's way of pushing the next
#: component (usually the clickable control row) to the right of its own line.
_TRAILING_FILL_RE = re.compile(r"\n[ \t]+$")


def _is_wide(char: str) -> bool:
    return unicodedata.east_asian_width(char) in {"W", "F"}


def _display_width(text: str) -> int:
    """Width in display columns: a CJK glyph takes two, everything else one."""

    return sum(2 if _is_wide(char) else 1 for char in text)


def _token_free(line: str) -> str:
    for pattern in STRUCTURAL_TOKEN_PATTERNS:
        line = pattern.sub(" ", line)
    return line


def _line_kind(line: str) -> str:
    """``empty`` / ``spaces`` / ``verbatim`` / ``prose`` — see the reflow.

    A line keeps no language after its structural tokens are removed (a
    ``====`` separator, a bare ``§c`` or placeholder, a number) is ``verbatim``:
    it is layout, not text, and the candidate may not rewrite or reflow it.
    """

    if not line:
        return "empty"
    if not line.strip():
        return "spaces"
    if not any(char.isalpha() for char in _token_free(line)):
        return "verbatim"
    return "prose"


def _source_segments(lines: list[str]) -> list[tuple[str, tuple[str, ...]]]:
    """Split a source into ``("fixed", text)`` and ``("prose", lines)`` parts."""

    segments: list[tuple[str, tuple[str, ...]]] = []
    run: list[str] = []
    for line in lines:
        if _line_kind(line) == "prose":
            run.append(line)
            continue
        if run:
            segments.append(("prose", tuple(run)))
            run = []
        segments.append(("fixed", (line,)))
    if run:
        segments.append(("prose", tuple(run)))
    return segments


def _find_equal_line(target_lines: list[str], cursor: int, text: str) -> int | None:
    stripped = text.strip()
    for position in range(cursor, len(target_lines)):
        if target_lines[position].strip() == stripped:
            return position
    return None


def _join_lines(texts: list[str]) -> str:
    """Undo the candidate's own breaks so the text can be wrapped afresh.

    Only a break that a reader would read as a space gets one: a break between
    two ASCII word characters ("...it just needs" / "something different")
    separated words, while CJK prose continues without a space.
    """

    stream = texts[0]
    for text in texts[1:]:
        if not stream or not text:
            stream += text
            continue
        before, after = stream[-1], text[0]
        if before.isspace() or after.isspace():
            stream += text
        elif before.isascii() and after.isascii() and before.isalnum() and after.isalnum():
            stream += " " + text
        else:
            stream += text
    return stream


def _token_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for pattern in STRUCTURAL_TOKEN_PATTERNS:
        spans.extend((match.start(), match.end()) for match in pattern.finditer(text))
    return spans


def _break_rank(text: str, position: int, spans: list[tuple[int, int]]) -> int | None:
    """Preference for breaking *before* ``position``; ``None`` means unsafe.

    Unsafe: inside a URL, command fragment, placeholder or ``§`` code, or
    inside an ASCII word ("Northwalk" must not become "Nor" + "thwalk").
    """

    for start, end in spans:
        if start < position < end:
            return None
    before, after = text[position - 1], text[position]
    if before.isascii() and after.isascii() and before.isalnum() and after.isalnum():
        return None
    if after in _NO_LINE_START or before in _NO_LINE_END:
        return None
    if before in _SENTENCE_ENDS:
        return 0
    if before.isspace() or before in _CLAUSE_ENDS:
        return 1
    if before == after and _is_wide(before):
        # Reduplicated word (看看, 慢慢): legal, but the worst place to cut.
        return 3
    if _is_wide(before) or _is_wide(after):
        return 2
    return 3


def _width_index(text: str, width: float) -> int:
    """First character index whose cumulative display width reaches ``width``."""

    consumed = 0
    for index, char in enumerate(text):
        consumed += 2 if _is_wide(char) else 1
        if consumed >= width:
            return index
    return len(text) - 1


def _pick_break(
    text: str,
    ideal: int,
    lower: int,
    spans: list[tuple[int, int]],
    *,
    upper: int | None = None,
    prefer_nearest: bool = False,
) -> int | None:
    """Nearest natural, safe break to ``ideal``, never at or before ``lower``."""

    window = max(6, len(text) // 4)
    best: tuple[tuple[int, int, int, int], int] | None = None
    last = min(len(text) - 1, upper if upper is not None else len(text) - 1)
    for position in range(lower, last + 1):
        rank = _break_rank(text, position, spans)
        if rank is None:
            continue
        distance = abs(position - ideal)
        # Keep the long-standing prose preference for a stronger natural
        # boundary. Equal-width sign reflow opts into nearest-first so the
        # display can fill physical rows without collapsing short slots.
        key = (
            (0 if distance <= window else 1, distance, rank, position)
            if prefer_nearest
            else (0 if distance <= window else 1, rank, distance, position)
        )
        if best is None or key < best[0]:
            best = (key, position)
    return best[1] if best is not None else None


def _wrap_prose(
    texts: list[str],
    slots: list[str],
    *,
    min_line_chars: int = 1,
    uniform_line_width: bool = False,
) -> list[str] | None:
    """Rebuild a paragraph's lines across the source's own line shape.

    The candidate's lines are joined and re-split in proportion to the source
    line widths — the author's wrapping — with each break moved to the nearest
    natural boundary. Returns ``None`` when the required number of lines cannot
    be reached without breaking a token or an ASCII word.
    """

    minimum = max(1, int(min_line_chars))
    if len(texts) == len(slots) and all(len(line.strip()) >= minimum for line in texts):
        # The candidate already has one line per source line: its own breaks are
        # the author's, and re-wrapping them would only move text around.
        return list(texts)
    stream = _join_lines(texts)
    # Line minimums count visible characters: a cut that leaves only the
    # stream's trailing space on the last line yields a blank prose line, which
    # the next pass treats as a dropped line and re-wraps differently.
    visible_after = [0] * (len(stream) + 1)
    for index in range(len(stream) - 1, -1, -1):
        visible_after[index] = visible_after[index + 1] + (not stream[index].isspace())
    if visible_after[0] < minimum * len(slots):
        return None
    spans = _token_spans(stream)
    weights = [1 for _slot in slots] if uniform_line_width else [max(1, _display_width(slot.strip())) for slot in slots]
    total = sum(weights)
    width = _display_width(stream)
    cuts: list[int] = []
    line_start = 0
    consumed = 0
    for slot_index, weight in enumerate(weights[:-1]):
        consumed += weight
        remaining_slots = len(slots) - slot_index - 1
        lower = line_start + 1
        while lower < len(stream) and visible_after[line_start] - visible_after[lower] < minimum:
            lower += 1
        upper = len(stream) - 1
        while upper >= lower and visible_after[upper] < minimum * remaining_slots:
            upper -= 1
        if upper < lower:
            return None
        position = _pick_break(
            stream,
            _width_index(stream, consumed / total * width),
            lower,
            spans,
            upper=upper,
            prefer_nearest=uniform_line_width,
        )
        if position is None:
            return None
        cuts.append(position)
        line_start = position
    lines: list[str] = []
    start = 0
    for position in [*cuts, len(stream)]:
        lines.append(stream[start:position])
        start = position
    return lines


def _bullet_rows(text: str) -> tuple[list[str], list[list[tuple[str, str]]]] | None:
    """Read a bullet list without treating its row breaks as prose wrapping."""

    leading: list[str] = []
    rows: list[list[tuple[str, str]]] = []
    for line in text.split("\n"):
        if not line and not rows:
            leading.append(line)
            continue
        match = _BULLET_ROW_RE.fullmatch(line)
        if match:
            rows.append([(match.group(1), match.group(2))])
            continue
        if not rows or not line.strip() or not line.startswith((" ", "\t")):
            return None
        indent = line[: len(line) - len(line.lstrip(" \t"))]
        rows[-1].append((indent, line[len(indent) :]))
    return (leading, rows) if rows else None


def restore_bullet_list_shape(source: str, target: str) -> str | None:
    """Restore list row positions and indentation when an MT reply trims them.

    Each source bullet must have exactly one candidate bullet in the same order.
    A wrapped bullet may be reflowed within its own row, but text never crosses
    a bullet boundary. Unclear row ownership remains a validation failure.
    """

    source_rows = _bullet_rows(source)
    target_rows = _bullet_rows(target)
    if source_rows is None or target_rows is None:
        return None
    leading, source_groups = source_rows
    _target_leading, target_groups = target_rows
    if len(source_groups) != len(target_groups):
        return None
    restored = list(leading)
    for source_group, target_group in zip(source_groups, target_groups):
        wrapped = _wrap_prose([text for _prefix, text in target_group], [text for _prefix, text in source_group])
        if wrapped is None or len(wrapped) != len(source_group) or any(not line.strip() for line in wrapped):
            return None
        restored.extend(prefix + line.strip() for (prefix, _source), line in zip(source_group, wrapped))
    return "\n".join(restored)


def _run_stop(
    segments: list[tuple[str, tuple[str, ...]]],
    index: int,
    target_lines: list[str],
    cursor: int,
    folded: dict[int, int],
) -> int:
    """Index of the candidate line that starts the next fixed source part."""

    if index + 1 >= len(segments):
        return len(target_lines)
    text = segments[index + 1][1][0]
    if _line_kind(text) == "verbatim":
        anchor = _find_equal_line(target_lines, cursor, text)
        if anchor is None:
            # A punctuation row the reply drew in its own script still ends the
            # prose run: the row is not part of the paragraph, and swallowing it
            # would leave the following prose part without text (a refusal).  The
            # same holds when the reply drew the row at the head of its own
            # sentence — there the run stays on the row's line and the rest of
            # that line is prose again.
            position = folded.get(index + 1)
            if position is not None and position >= cursor:
                return position
            split = _leading_row_split(text, target_lines, cursor)
            if split is not None:
                return split[0]
            return len(target_lines)
        return anchor
    for position in range(cursor, len(target_lines)):
        if not target_lines[position].strip():
            return position
    return len(target_lines)


#: Marks a translation redraws in the Chinese script and that no normalization
#: unifies with their ASCII spelling: ``。`` for ``.``, ``、`` for ``,``, ``“`` for
#: ``"``.  Same mark, same order, different script — the fold table is applied to
#: *both* sides of the comparison, so it can only make two spellings of one row
#: meet, never rewrite a row.
_PUNCTUATION_FOLD = str.maketrans(
    {
        "。": ".",
        "、": ",",
        "；": ";",
        "：": ":",
        "“": '"',
        "”": '"',
        "「": '"',
        "」": '"',
        "『": '"',
        "』": '"',
        "（": "(",
        "）": ")",
        "【": "[",
        "】": "]",
        "～": "~",
        "－": "-",
        "—": "-",
        "–": "-",
    }
)


def _punctuation_skeleton(line: str) -> str:
    """Folded identity of a non-language row, so a fold cannot hide from us.

    ``". . ."``, ``"…"`` and ``"……"`` are one row drawn in three scripts and
    ``"! "`` is ``"！"``: a rebuild that put the English row back would show the
    row twice.  Compatibility folding (``NFKC``) plus dropping whitespace and
    repeated characters makes those spellings meet without touching letters or
    digits, so a prose line never folds onto a punctuation row.

    Unicode does not unify every pair a translation uses — ``。`` is not ``.``
    and ``“`` is not ``"`` under any normalization, but they are the same row
    drawn in the Chinese script.  ``_PUNCTUATION_FOLD`` adds those pairs
    explicitly; both sides go through the same table, so this stays a
    comparison, never a rewrite.
    """

    folded = unicodedata.normalize("NFKC", line).translate(_PUNCTUATION_FOLD)
    return "".join(dict.fromkeys(char for char in folded if not char.isspace()))


def _row_is_drawn_by_candidate(row: str, target_lines: list[str]) -> bool:
    """True when the candidate already drew this non-language row itself."""

    skeleton = _punctuation_skeleton(row)
    if not skeleton:
        return False
    return any(
        _line_kind(line) == "verbatim" and _punctuation_skeleton(line) == skeleton for line in target_lines
    )


def _mark_row(line: str) -> str:
    """Folded marks of a pure punctuation row, or ``""`` when the row is not one.

    The proof a fold may rest on.  A row qualifies only when it carries nothing
    but punctuation: no letters or digits, and no URL, command, placeholder,
    ``§`` code, macro call or escape — so ``. . .``, ``! ``, ``?`` and ``,`` do,
    while a ``====`` divider (a math symbol), a bare ``§c``, ``%s`` or an unknown
    row never can.  The folding itself is ``_punctuation_skeleton``: compatibility
    folding makes ``……``, ``…`` and ``...`` meet, whitespace never counts, and
    how often a mark is repeated is the reply's own rendering of the same row.
    """

    if any(pattern.search(line) for pattern in STRUCTURAL_TOKEN_PATTERNS):
        return ""
    folded = _punctuation_skeleton(line)
    if not folded or not all(unicodedata.category(char).startswith("P") for char in folded):
        return ""
    return folded


def _folded_row_anchors(
    segments: list[tuple[str, tuple[str, ...]]],
    target_lines: list[str],
) -> dict[int, int]:
    """Pair a source punctuation row with the candidate line that drew it.

    Reply-side folding is common: the model renders the author's ``. . .`` row as
    ``……`` and ``! `` as ``！``.  Writing the English row back would then show the
    row twice, so the pairing has to be *proven*, or not made at all:

    * only a **pure punctuation** source row qualifies (``_mark_row``);
    * the candidate line must itself be a pure punctuation row carrying the same
      marks in the same order;
    * the marks must pair one to one — the candidate has to have drawn the row
      exactly as many times as the source wrote it, and the pairing is by order.
      A row drawn twice, or a row the candidate never drew, yields no anchor: it
      is then written back from the source as before, and a row that survives
      only inside a translated line still makes the whole rebuild ambiguous.
    """

    source_rows: dict[str, list[int]] = {}
    for index, (kind, texts) in enumerate(segments):
        if kind != "fixed":
            continue
        marks = _mark_row(texts[0])
        if marks:
            source_rows.setdefault(marks, []).append(index)
    if not source_rows:
        return {}
    drawn: dict[str, list[int]] = {}
    for position, line in enumerate(target_lines):
        marks = _mark_row(line)
        if marks:
            drawn.setdefault(marks, []).append(position)
    anchors: dict[int, int] = {}
    for marks, indices in source_rows.items():
        positions = drawn.get(marks) or []
        if len(positions) != len(indices):
            continue
        for index, position in zip(indices, positions):
            anchors[index] = position
    return anchors


def _restore_row_edges(source_row: str, drawn: str) -> str:
    """The candidate's own marks, with the source row's leading/trailing fill.

    The marks are the reply's translation of the row; the spaces around them are
    the author's layout (a fill row's width, a mark's own gap) and go back byte
    for byte.  Nothing is taken from the candidate but the row itself, so no
    English punctuation is ever written back on top of a translated row.
    """

    lead = source_row[: len(source_row) - len(source_row.lstrip())]
    trail = source_row[len(source_row.rstrip()) :]
    return lead + drawn.strip() + trail


def _leading_row_split(row: str, target_lines: list[str], cursor: int) -> tuple[int, str, str] | None:
    """``(line, drawn row, sentence text)`` for a row drawn inside the sentence.

    A reply that folded the row into the sentence itself (the author's ``". . . "``
    came back inside ``"……真的，拿去吧！"``) has still drawn the row: the marks it
    used are the source row's own marks, in the source's own order, at the head
    of the line that carries the row's position.  Keeping them inside the
    sentence and writing the English row back would show the row twice, so the
    run moves onto the row's own line and the rest of the line stays prose.

    Only a **pure punctuation run** at the head of that line qualifies (the same
    ``_mark_row`` proof as the whole-line pairing), so arbitrary text can never
    become a layout row; a line that is nothing but the run, or whose run carries
    other marks, is left to the paths below (write the row back as a dropped row,
    or refuse an ambiguous rebuild).
    """

    marks = _mark_row(row)
    if not marks:
        return None
    for position in range(cursor, len(target_lines)):
        line = target_lines[position]
        stripped = line.lstrip()
        if not stripped:
            continue
        end = 0
        for index, char in enumerate(stripped):
            if char.isspace() or unicodedata.category(char).startswith("P"):
                end = index + 1
            else:
                break
        run, rest = stripped[:end], stripped[end:]
        if not rest.strip() or _mark_row(run) != marks:
            return None
        return position, line[: len(line) - len(rest)], rest
    return None


def _align_candidate(
    segments: list[tuple[str, tuple[str, ...]]],
    target_lines: list[str],
    folded: dict[int, int],
) -> tuple[dict[int, str], dict[int, list[str]]] | None:
    """Pair the candidate's lines with the source's parts, or refuse.

    Every fixed line is placed: an empty or padding line consumes the candidate's
    own blank lines, a verbatim line is matched by content. A verbatim line the
    candidate dropped is written back from the source (it is layout, not
    language); one that survives only *inside* a translated line, or that the
    candidate drew in another script without a proven pairing (``". . ."`` folded
    into ``"……"`` — see ``_folded_row_anchors``), is ambiguous, so the whole
    repair is refused rather than duplicating it. A prose part that the candidate
    left no text for means a lost paragraph, which is also a refusal: text may
    never disappear into a rebuild.
    """

    anchored: dict[int, str] = {}
    chunks: dict[int, list[str]] = {}
    cursor = 0
    for index, (kind, texts) in enumerate(segments):
        if kind == "prose":
            stop = _run_stop(segments, index, target_lines, cursor, folded)
            chunk = [line for line in target_lines[cursor:stop] if line.strip()]
            cursor = stop
            if not chunk:
                return None
            chunks[index] = chunk
            continue
        text = texts[0]
        if _line_kind(text) != "verbatim":
            while cursor < len(target_lines) and not target_lines[cursor].strip():
                cursor += 1
            continue
        stripped = text.strip()
        anchor = _find_equal_line(target_lines, cursor, text)
        if anchor is None:
            drawn = folded.get(index)
            if drawn is not None:
                # The reply wrote this punctuation row in its own script: the
                # row it drew is kept (with the source's own edge fill), because
                # putting the English row back would show the row twice.
                anchored[index] = _restore_row_edges(text, target_lines[drawn])
                cursor = max(cursor, drawn + 1)
                continue
            split = _leading_row_split(text, target_lines, cursor)
            if split is not None:
                # The reply drew the row at the head of its own sentence: the run
                # moves onto the row's line and the rest of that line stays prose
                # for the following part to rebuild.
                position, run, rest = split
                anchored[index] = _restore_row_edges(text, run)
                target_lines[position] = rest
                cursor = max(cursor, position)
                continue
            # A separator that is missing as a line but present *inside* another
            # line is ambiguous: writing it back would duplicate the characters, so
            # the rebuild is refused instead of guessing where they belong.  The
            # same holds when the candidate drew the row itself in another script
            # (``". . ."`` folded into ``"……"``, ``"! "`` into ``"！"``) without a
            # proven pairing: putting the English row back would duplicate it
            # rather than repair the layout.
            if stripped and (
                stripped in "\n".join(target_lines) or _row_is_drawn_by_candidate(stripped, target_lines)
            ):
                return None
            continue
        leftover = "\n".join(target_lines[:anchor] + target_lines[anchor + 1 :])
        if stripped and stripped in leftover:
            return None
        anchored[index] = text
        cursor = anchor + 1
    return anchored, chunks


def _shape_lines_intact(source_lines: list[str], target_lines: list[str]) -> bool:
    """True when an already equal-shaped candidate kept every fixed line.

    Equal break counts and equal blank-line positions mean the two texts share
    one line partition, so the alignment is the index itself.  A whitespace-only
    or non-language source line that the candidate replaced with text cannot be
    restored without guessing where that text belongs (a refusal, never a silent
    rewrite), and a prose line the candidate blanked has lost its translation.
    """

    for index, line in enumerate(source_lines):
        if _line_kind(line) == "prose":
            if not target_lines[index].strip():
                return False
        elif target_lines[index] != line:
            return False
    return True


def restore_prose_line_shape(
    source: str,
    target: str,
    *,
    min_line_chars: int = 1,
    uniform_line_width: bool = False,
) -> str | None:
    """Re-wrap a reflowing candidate across the source's own line shape.

    Map dialogue is prose the author wrapped by hand: the breaks are where the
    game's chat box would otherwise wrap, the empty line and the run of spaces
    after a paragraph are what keep the clickable rows under the text. A
    translation model reads the paragraph, not the layout, and returns it as one
    long line — which ``strict_newlines`` must reject, because writing it back
    would move the whole menu.

    This repairs the candidate instead of relaxing that rule. The source's shape
    is the authority: empty lines, padding lines and non-language lines (``====``
    separators, bare ``§`` codes, placeholders) go back byte for byte, and only
    the prose lines are rebuilt, by re-wrapping the candidate's own text across
    them in proportion to the source line widths. A punctuation row the reply
    drew in its own script (``". . ."`` as ``"……"``, ``"! "`` as ``"！"``) is kept
    as the reply drew it, with the source row's own edge fill restored
    (``_folded_row_anchors``): writing the English row back would show the row
    twice, so the pairing is proven or not made. Nothing is invented and nothing
    is dropped: every break lands on a natural boundary outside any token or
    ASCII word.

    Returns the repaired text, the target unchanged when it already has the
    required shape, or ``None`` when the repair cannot be trusted — a separator
    line that is ambiguous, a punctuation row drawn without a proven pairing, a
    paragraph the candidate dropped, a required break with no natural position,
    or list-like source lines whose breaks are row boundaries rather than
    wrapping.
    """

    if "\n" not in source or not target.strip():
        return target
    source_lines = source.split("\n")
    target_lines = [line.rstrip("\r") for line in target.split("\n")]
    if (
        target.count("\n") == source.count("\n")
        and _blank_line_indices(target) == _blank_line_indices(source)
        and _shape_lines_intact(source_lines, target_lines)
        and all(
            _line_kind(source_line) != "prose" or len(target_line.strip()) >= max(1, int(min_line_chars))
            for source_line, target_line in zip(source_lines, target_lines)
        )
    ):
        return target
    prose = [line for line in source_lines if _line_kind(line) == "prose"]
    if not prose or any(_ROW_START_RE.match(line) for line in prose):
        return None
    segments = _source_segments(source_lines)
    aligned = _align_candidate(segments, target_lines, _folded_row_anchors(segments, target_lines))
    if aligned is None:
        return None
    anchored, chunks = aligned
    lines: list[str] = []
    for index, (kind, texts) in enumerate(segments):
        if kind != "prose":
            lines.append(anchored.get(index, texts[0]))
            continue
        wrapped = _wrap_prose(
            chunks[index],
            list(texts),
            min_line_chars=min_line_chars,
            uniform_line_width=uniform_line_width,
        )
        if wrapped is None:
            return None
        lines.extend(wrapped)
    rebuilt = "\n".join(lines)
    if rebuilt.count("\n") != source.count("\n") or _blank_line_indices(rebuilt) != _blank_line_indices(source):
        return None
    return rebuilt


def restore_clickable_row_layout(
    source: str,
    target: str,
    *,
    clickable: bool,
    gap_after_furniture: bool,
) -> str:
    """Put a clickable row's own padding back around a translated label.

    Only the two edges of *this* span are touched, so no text ever moves across
    a component boundary: the author's trailing pad (the row's width, which
    separates the label from the next mark) and, when the fragment drawn before
    it is unchanged furniture, the leading gap that a control mark and its label
    share across the split.  A leaf that is not clickable keeps whatever the
    reply had, and so does a gap whose preceding fragment is a translated
    sentence — a space between English words is not spacing Chinese writes.
    """

    if not clickable or not target.strip():
        return target
    repaired = target
    pad = control_row_pad(source)
    if control_row_pad(repaired) != pad and repaired.rsplit("\n", 1)[-1].strip():
        # Never across a break: padding an empty last line would invent a fill
        # row rather than restore the row's own width.
        repaired = repaired.rstrip(ROW_PAD) + pad
    if gap_after_furniture:
        gap = control_row_gap(source)
        if control_row_gap(repaired) != gap:
            repaired = gap + repaired.lstrip(ROW_PAD)
    return repaired


def repair_layout_candidate(
    unit: TextUnit,
    source: str,
    target: str,
    *,
    rule: NewlineRule | None = None,
) -> str | None:
    """Shape one candidate against the source's own layout; ``None`` = refuse.

    The single repair every route shares: the provider shapes its reply with it
    and the reuse gate applies it to every stored, shared or revised candidate,
    so a memory row, a reviewed draft, a manual revision and a fresh model reply
    are shaped by the same rules.

    ``source`` is the span that will be written and ``target`` the text that
    will be written with it: the caller restores glossary terms *before* calling
    this, so the breaks are chosen from the text the reuse gate and the write
    door measure, never from a masked token's width (which would pick different
    break points for the same reply).  Only the span it is given is ever
    touched: text never moves between components, and the repair never invents
    text.  A reply that reflowed the paragraph is re-wrapped onto the source's
    own line shape (refused when that cannot be proven, and a row the reply
    already drew is never written back twice), and a clickable row's own pad and
    control-row gap are restored from the source.  A reviewed sentence fragment
    is left exactly as the reply had it: its break belongs to the passage that
    carries the sentence.
    """

    if isinstance(unit.metadata.get("fragment_merge"), dict):
        return target
    clickable, gap_after_furniture, _layout_breaks = interactive_leaf_flags(unit)
    target = restore_clickable_row_layout(
        source, target, clickable=clickable, gap_after_furniture=gap_after_furniture
    )
    rule = rule if rule is not None else newline_rule(unit)
    if not rule.reflow or not target.strip():
        return target
    repaired = restore_bullet_list_shape(source, target) if _bullet_rows(source) is not None else restore_prose_line_shape(source, target)
    if repaired is None:
        return None
    # The re-wrap may move the edges with the text it re-splits; put them back.
    return restore_clickable_row_layout(
        source, repaired, clickable=clickable, gap_after_furniture=gap_after_furniture
    )


def passage_runs(units: list[TextUnit]) -> list[list[int]]:
    """Positions of units that form one continuous passage inside a group.

    Item lore is authored as one array of component strings and a dialogue line as
    several ``extra`` leaves; the reading order is the array order. Translating
    those lines one by one is what turns ``This will help guide / you should you
    lose / your way.`` into three unrelated fragments, so the prompt must say they
    are one passage and the reply must keep the same number of lines.

    Runs never cross a container element list: only consecutive elements of the
    same array (``lore`` entries, ``extra`` leaves) belong together.
    """

    keys = [_passage_element_key(unit) for unit in units]
    runs: list[list[int]] = []
    start: int | None = None
    for index, key in enumerate([*keys, None]):
        previous = keys[index - 1] if index else None
        continues = (
            start is not None
            and key is not None
            and previous is not None
            and key[0] == previous[0]
            and key[1] == previous[1] + 1
        )
        if continues:
            continue
        if start is not None and index - start > 1:
            runs.append(list(range(start, index)))
        start = index if key is not None else None
    return runs


def _passage_element_key(unit: TextUnit) -> tuple[str, int] | None:
    """Identity of the array and the element's reading index, or ``None``."""

    try:
        locator = json.loads(unit.locator)
    except (TypeError, ValueError):
        return None
    if unit.format == "generic-json" and isinstance(locator, dict):
        # An encoded component leaf: the array identity is the composed path
        # (outer pointer + inner component pointer), so the lore lines of one
        # item are one passage while two items' lore lines are not.
        outer, inner = _json_locator_pointers(unit.locator)
        key = _element_key_in_pointer(f"{outer}{inner}")
        return (f"{unit.file_path}:{key[0]}", key[1]) if key else None
    command = locator.get("command") if isinstance(locator, dict) else None
    if not isinstance(command, dict):
        return None
    kind = command.get("kind")
    if kind == "snbt":
        path = tuple(str(part) for part in command.get("snbt_path") or ())
        index = command.get("snbt_index")
        if not path or not isinstance(index, int):
            return None
        identity = display_container_identity(unit)
        # The element index differs between list siblings, so use the command's
        # actual owner rather than its display-container identity here.
        owner = (unit.file_path, locator.get("chunk"), locator.get("path"), locator.get("line"), path, command.get("json"))
        return (f"snbt:{json.dumps(owner, ensure_ascii=False)}", index) if identity else None
    if kind == "json":
        start = command.get("json_start")
        pointer = str(command.get("pointer") or "")
        if not isinstance(start, int):
            return None
        key = _element_key_in_pointer(pointer)
        if key is None:
            return None
        return (f"json:{display_container_identity(unit)}:{key[0]}", key[1])
    return None


def _element_key_in_pointer(pointer: str) -> tuple[str, int] | None:
    """``(parent pointer, last numeric index)`` of a pointer, or ``None``."""

    positions = list(re.finditer(r"/(\d+)(?=/|$)", pointer))
    if not positions:
        return None
    last = positions[-1]
    return pointer[: last.start()], int(last.group(1))


def _discardable_fragment(unit: TextUnit, source: str) -> bool:
    """True when this exact span was reviewed as a droppable sentence fragment.

    The permission is never global: it requires the audited table entry stamped
    on the unit (input fingerprint + exact locator), it must name *this* source
    span, and the span must still have the reviewed fragment shape.  A whole
    sentence, a mechanism value or a leaf from another input can therefore never
    become empty through this path.
    """

    declared = unit.metadata.get("fragment_merge")
    if not isinstance(declared, dict):
        return False
    if str(declared.get("source") or "") != source:
        return False
    return fragment_shape_ok(source)


def fragment_shape_ok(source: str) -> bool:
    """A sentence fragment carries no terminal punctuation and few words."""

    stripped = source.strip()
    if not stripped or len(stripped) > 24:
        return False
    if any(char in stripped for char in ".!?。！？"):
        return False
    return len(stripped.split()) <= 3


FRAGMENT_MERGE_ASSET = Path("assets") / "fragment-merge-allowlist.json"
FRAGMENT_MERGE_ENV = "MC_HANHUA_FRAGMENT_MERGE_ALLOWLIST"


def _unit_relative_path(path: str) -> str:
    """File identity of a fragment entry: separators unified, nothing else.

    It must equal the production ``unit.file_path`` exactly.  ``copy_input``
    extracts archive members *as stored*, so a map packaged inside a wrapper
    folder keeps that folder in every path (``RoH - Release 1.0/datapacks/…``);
    a reviewed table is therefore written with the production spelling, and no
    prefix or suffix is ever stripped or matched loosely.
    """

    normalized = str(path).replace("\\", "/").lstrip(".")
    return normalized.lstrip("/")


@dataclass(frozen=True)
class FragmentMergeAllowlist:
    """Reviewed fragments whose sentence continues in the passage around them.

    Keyed by (input fingerprint, scanner-relative file, exact locator): editing
    or repacking the input expires every entry instead of inheriting the
    decision, exactly like the mechanism and display-argument tables.
    """

    entries: dict[tuple[str, str, str], dict[str, object]]

    def entry_for(self, fingerprint: str, file_path: str, locator: str) -> dict[str, object] | None:
        if not fingerprint:
            return None
        return self.entries.get((fingerprint, _unit_relative_path(file_path), locator))


def bundled_fragment_merge_path() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2])) / FRAGMENT_MERGE_ASSET


def load_fragment_merge_allowlist(path: Path | str | None = None) -> FragmentMergeAllowlist:
    """A missing or broken source grants nothing (fail-closed)."""

    override = str(path) if path is not None else (os.getenv(FRAGMENT_MERGE_ENV) or "")
    candidates = [Path(override)] if override else [bundled_fragment_merge_path()]
    for candidate in candidates:
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        entries = payload.get("entries") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            continue
        collected: dict[tuple[str, str, str], dict[str, object]] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            fingerprint = str(entry.get("package_fingerprint") or "")
            file_path = _unit_relative_path(str(entry.get("file") or ""))
            locator = str(entry.get("locator") or "")
            source = str(entry.get("source") or "")
            if not fingerprint or not file_path or not locator or not source:
                continue
            collected[(fingerprint, file_path, locator)] = entry
        return FragmentMergeAllowlist(collected)
    return FragmentMergeAllowlist({})


def stamp_fragment_merges(
    units: list[TextUnit],
    fingerprint: str,
    allowlist: FragmentMergeAllowlist | None = None,
) -> int:
    """Mark reviewed fragments so the write door may drop them.

    The entry must name the unit's exact locator *and* source span, and the
    passage's other locators must exist in the same file: a table entry that
    nobody can corroborate against the scanned input grants nothing.
    """

    allowlist = allowlist if allowlist is not None else load_fragment_merge_allowlist()
    if not allowlist.entries or not fingerprint:
        return 0
    by_file: dict[str, set[str]] = {}
    for unit in units:
        by_file.setdefault(unit.file_path, set()).add(unit.locator)
    stamped = 0
    for unit in units:
        entry = allowlist.entry_for(fingerprint, unit.file_path, unit.locator)
        if entry is None:
            continue
        source = str(entry.get("source") or "")
        if unit.source_text != source or not fragment_shape_ok(source):
            continue
        passage = [str(part) for part in entry.get("passage") or [] if isinstance(part, str)]
        if not passage or not set(passage) <= by_file.get(unit.file_path, set()):
            continue
        unit.metadata["fragment_merge"] = {
            "source": source,
            "passage": passage,
            "reason": str(entry.get("reason") or ""),
            "evidence": str(entry.get("evidence") or ""),
            "promoted": bool(unit.metadata.get("fragment_merge", {}).get("promoted")),
        }
        stamped += 1
    return stamped


def normalize_candidate_scaffolding(unit: TextUnit, candidate: str) -> str:
    """Remove only the known leading book prompt label, never visible content."""
    if is_book_page_unit(unit) and not re.match(r"^\s*\[page=", unit.source_text):
        return re.sub(r"^\s*\[page=(?:\d+|未知)\][ \t]*", "", candidate)
    return candidate


def validate_reuse(
    unit: TextUnit,
    candidate: object,
    *,
    source_text: str | None = None,
    allow_layout_slot: bool = True,
) -> ReuseResult:
    """Single gate for every stored, shared or model candidate.

    The candidate is judged against the **current input span**, never against the
    ``(source, target)`` pair it was stored with: a translation accepted under an
    older rule set — or reused from another unit — must still satisfy today's
    grammar for the text that is on disk now.  Callers that can prove a different
    view of the source (a masked MT source, say) pass it as ``source_text``.

    Everything that may reach a writer funnels through here: translation memory,
    prior-task drafts, mod-asset seeds, glossary hits, official terms, canonical
    sharing, desktop manual revisions and model output.
    """

    if not isinstance(candidate, str):
        return ReuseResult(None, "invalid_candidate_type")
    if unit.metadata.get("content_preservation") and candidate != unit.source_text:
        return ReuseResult(None, "puzzle_content")
    text = normalize_candidate_scaffolding(unit, candidate)
    rule = newline_rule(unit)
    if rule.repair:
        text = normalize_layout_newlines(unit.source_text, text)
    # A proven TextDisplay renders real line breaks, but the map may author them
    # as literal "\n" sequences that the game only shows as backslash-n and no
    # model reproduces reliably. Convert both sides for proven text-display
    # containers only; commands and language files keep their literal tokens.
    if unit.metadata.get("layout_container") == "text-display" and "\\n" in unit.source_text:
        text = text.replace("\\n", "\n")
        source = unit.source_text.replace("\\n", "\n")
    else:
        source = unit.source_text
    if source_text is not None:
        source = source_text
    repaired = repair_layout_candidate(unit, source, text, rule=rule)
    if repaired is None:
        # The reply reflowed the text and the rebuild could not be proven: the
        # same rejection as before the repair existed.
        return ReuseResult(None, "newline_structure")
    text = repaired
    reason = validate_contextual_target(source, text, strict_newlines=rule.strict)
    if reason is None and not text.strip() and source.strip():
        # A sign or TextDisplay slot may legitimately be emptied; anything else
        # losing its text is a rejection, not a translation -- unless this exact
        # span was reviewed as a fragment of the passage around it.
        layout_slot = allow_layout_slot and unit.metadata.get("layout_container") in {"sign", "text-display"}
        if not layout_slot and not _discardable_fragment(unit, source):
            reason = "empty_candidate"
    return ReuseResult(None if reason is not None else text, reason)
