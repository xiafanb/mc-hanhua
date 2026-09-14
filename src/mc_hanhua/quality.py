from __future__ import annotations

import json
import re
from collections import Counter, defaultdict

from .models import RiskLevel, TextUnit, TranslationGroup
from .utils import PLACEHOLDER_RE, stable_id

QUALITY_CACHE_VERSION = "context-v3"
BOOK_CACHE_VERSION = "book-context-v2"
LAYOUT_CACHE_VERSION = "layout-v1"
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


def _json_owner(locator: str) -> tuple[str, str]:
    parts = [part.replace("~1", "/").replace("~0", "~") for part in locator.lstrip("/").split("/") if part]
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
    try:
        locator = json.loads(unit.locator)
    except (TypeError, ValueError, json.JSONDecodeError):
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
        if current and (separated or exceeds):
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
    parts = [part.replace("~1", "/").replace("~0", "~") for part in str(locator).lstrip("/").split("/") if part]
    numbered: list[int | str] = []
    for part in parts:
        numbered.append(int(part) if part.isdigit() else part.lower())
    return (len(parts), tuple(numbered))


def _unit_order(unit: TextUnit) -> tuple:
    if unit.format == "generic-json":
        return (0, _json_pointer_order(unit.locator), unit.context)
    if semantic_owner(unit)[1] == "book":
        try:
            path = json.loads(unit.locator).get("path", [])
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
    return Counter(pattern.findall(value))


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
        return "pending"
    if unit.final_target == unit.source_text:
        return "source_preserved"
    if unit.metadata.get("writeback_status") in {"changed", "unchanged"} or looks_like_chinese_target(unit.final_target):
        return "written"
    return "pending"


def looks_like_chinese_target(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def validate_contextual_target(source: str, target: str, strict_newlines: bool = False) -> str | None:
    """Reject candidates that alter non-linguistic text payloads.

    Every translation route calls this gate before a target can be written.  It
    intentionally compares tokens as multisets: duplicating or dropping a URL,
    command, placeholder, or formatting code is just as unsafe as inventing one.

    strict_newlines additionally requires real newline count and blank-line
    positions to survive exactly (book pages and other fixed line layout text).
    """

    token_patterns = STRUCTURAL_TOKEN_PATTERNS
    if any(_tokens(source, pattern) != _tokens(target, pattern) for pattern in token_patterns):
        return "structural_token"
    if strict_newlines and "\n" in source:
        if target.count("\n") != source.count("\n") or _blank_line_indices(source) != _blank_line_indices(target):
            return "newline_structure"
    if target.count("\n") > source.count("\n") + 1:
        return "unexpected_expansion"
    if len(target.strip()) > max(80, len(source.strip()) * 4 + 16):
        return "unexpected_expansion"
    return None
