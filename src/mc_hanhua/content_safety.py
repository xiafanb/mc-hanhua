"""Content checks shared by extraction, reuse and publication.

These are conservative, observable checks, not a proof of translation meaning.
Dense ciphertext is held for review; an ordinary uppercase label is not.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Iterable

from .models import TextUnit, structured_locator

_ARTIFACT_PATTERNS = (
    re.compile(r"@@(?:\d+|OK)@@"),
    re.compile(r"\[page=(?:\d+|未知)\]"),
    re.compile(r"```"),
    re.compile(r"\*\*[^*\n]+\*\*|__[^_\n]+__"),
)
_NEGATIVE_ACTION = re.compile(
    r"^\s*(?:please\s+)?(?:do\s+not|don't|never)\s+"
    r"(?:press|click|push|pull|open|close|break|destroy|place|enter|leave|use|touch|activate)\b",
    re.IGNORECASE,
)
_CHINESE_NEGATIVE = re.compile(r"不|勿|别|禁止|切忌|避免|无需|不能")
_CONSONANT_RUN = re.compile(r"[B-DF-HJ-NP-TV-Z]{5,}")


def candidate_content_reason(source: str, target: str) -> str | None:
    """Reject introduced protocol/Markdown wrappers and lost action negation."""

    if source == target:
        return None
    for pattern in _ARTIFACT_PATTERNS:
        # Emphasis contents may translate; only the wrapper count is relevant.
        before = len(pattern.findall(source))
        after = len(pattern.findall(target))
        if after > before:
            return "model_scaffolding"
    if _NEGATIVE_ACTION.search(source) and re.search(r"[\u4e00-\u9fff]", target):
        if not _CHINESE_NEGATIVE.search(target):
            return "instruction_negation"
    return None


def display_container_identity(unit: TextUnit) -> str | None:
    """Locate one display payload, including command, chunk and archive identity."""

    if unit.format in {"json-lang", "legacy-lang"}:
        return None
    try:
        locator = json.loads(unit.locator)
    except (TypeError, ValueError):
        return None
    if not isinstance(locator, dict):
        return None
    command = locator.get("command")
    own_path = locator.get("path")
    identity: list[object] = [unit.file_path, unit.format, unit.metadata.get("archive_chain", []),
                              locator.get("chunk"), own_path, locator.get("line")]
    if isinstance(command, dict):
        if command.get("kind") not in {"json", "snbt", "wrapped"}:
            return None
        pointer = str(command.get("pointer") or command.get("json") or "")
        # Hover text and translation arguments are not concatenated with the body.
        scope = re.search(r"/(?:hoverEvent|hover_event|with)(?:/[^/]+)?", pointer)
        identity.extend([command.get("kind"), command.get("json_start"),
                         command.get("snbt_path"), command.get("snbt_index"),
                         scope.group(0) if scope else ""])
    elif isinstance(own_path, list) and "json" in locator:
        identity.append("component")
    else:
        return None
    return json.dumps(identity, ensure_ascii=False, sort_keys=True)


def grouped_display_units(units: Iterable[TextUnit]) -> dict[str, list[TextUnit]]:
    groups: dict[str, list[TextUnit]] = defaultdict(list)
    for unit in units:
        key = display_container_identity(unit)
        if key is not None:
            groups[key].append(unit)
    return dict(groups)


def suspected_cipher_units(units: Iterable[TextUnit]) -> set[str]:
    """Find dense, fragmented uppercase code; never infer from one short label.

    Multiple independent signals are required: substantial letter content,
    many distinct tokens, almost entirely uppercase Latin, and repeated long
    consonant runs. This deliberately returns a review requirement, not a
    claim that every uppercase string is a puzzle.
    """

    units = list(units)
    protected: set[str] = {
        unit.id for unit in units
        if display_container_identity(unit) is not None and len(re.findall(r"(?m)^[a-z]:[ \t]+[.\-]+[ \t]*$", unit.source_text)) >= 6
    }
    for members in grouped_display_units(units).values():
        if len(members) < 8:
            continue
        text = " ".join(unit.source_text for unit in members)
        letters = re.findall(r"[A-Za-z]", text)
        words = re.findall(r"\b[A-Z]{6,}\b", text)
        if len(letters) < 160 or not letters or sum(letter.isupper() for letter in letters) / len(letters) < 0.95:
            continue
        if len(set(words)) < 8 or len({word for word in words if _CONSONANT_RUN.search(word)}) < 4:
            continue
        protected.update(unit.id for unit in members)
    # A short key on another page of this same book is still puzzle content.
    # Never propagate to another book, block, command or archive.
    books = {_book_identity(unit) for unit in units if unit.id in protected} - {None}
    protected.update(unit.id for unit in units if _book_identity(unit) in books)
    return protected


def _book_identity(unit: TextUnit) -> str | None:
    identity = display_container_identity(unit)
    if identity is None:
        return None
    locator = structured_locator(unit)
    if locator is None:
        return None
    path = list(locator.get("path") or [])
    command = locator.get("command") or {}
    if command.get("kind") == "snbt" and "pages" in command.get("snbt_path", []):
        owner = [unit.file_path, unit.format, unit.metadata.get("archive_chain", []), locator.get("chunk"), path,
                 locator.get("line"), command.get("snbt_path")]
    elif "pages" in path:
        owner = [unit.file_path, unit.format, unit.metadata.get("archive_chain", []), locator.get("chunk"), path[:path.index("pages") + 1]]
    else:
        return None
    return json.dumps(owner, ensure_ascii=False, sort_keys=True)


def stamp_content_context(units: list[TextUnit]) -> set[str]:
    protected = suspected_cipher_units(units)
    for members in grouped_display_units(units).values():
        if len(members) > 1:
            reading = "".join(unit.source_text for unit in members)
            if len(reading) <= 12000:
                for unit in members:
                    unit.metadata["display_reading_context"] = reading
        for unit in members:
            if unit.id in protected:
                unit.metadata["content_preservation"] = {"reason": "suspected_cipher", "origin": "container_analysis"}
    return protected
