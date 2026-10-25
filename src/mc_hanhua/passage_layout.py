"""Passage-level line-break conservation for tellraw prose (DEVELOPMENT §3.12).

A passage is a run of adjacent plain-text components inside one ``tellraw`` JSON
array, bounded by the *source's* fixed edges (a component's leading/trailing
line break, blank or fill rows, ``====`` rows, clickable and non-text
components).  A passage revision may move prose line breaks between its members;
it never moves characters between members and never changes the passage's
total break count, its fixed edges or any clickable row.

Nothing a revision record says is trusted: :func:`check_passage` recomputes the
passage from the current input line every time it is called, and the caller
recomputes the member set from the current scan.
"""

from __future__ import annotations

import hashlib
import json
import re

from .text_components import (
    apply_command_text,
    command_locator_offset,
    extract_command_texts,
    validate_interactive_command_change,
)

#: Bump on any change to passage boundaries, legal breaks or checks: stored
#: passage records written under another version are discarded.
PASSAGE_RULE_VERSION = 2
PASSAGE_METADATA = "passage_layout"

_NON_TEXT_KEYS = ("score", "selector", "nbt", "keybind", "translate")
_CLICK_KEYS = ("clickEvent", "click_event")
_STYLE_KEYS = ("color", "bold", "italic", "underlined", "strikethrough", "obfuscated", "font", "hoverEvent", "hover_event", "insertion")
_BREAK = re.compile(r"[ \t]*\n[ \t]*")
_DIVIDER_TAIL = re.compile(r"\n[ \t]*={3,}[ \t]*$")
_INNER_FIXED_ROW = re.compile(r"\n[ \t]*\n|\n[ \t]*={3,}")
_DIVIDER_ROW = re.compile(r"^[ \t]*={3,}[ \t]*$")
_HAN = re.compile(r"[㐀-鿿豈-﫿]")
_NO_LINE_START = set("，。！？；：、…）」』”’,.!?;:%")
_NO_LINE_END = set("（「『“‘(")
_DECODER = json.JSONDecoder()


def _ascii_word(char: str) -> bool:
    return char.isascii() and char.isalnum()


def fixed_edges(text: str) -> tuple[str, str, str]:
    """``(lead, core, trail)``; edges are kept only when they carry a break or a divider row."""

    lead = re.match(r"\s*", text).group()
    core, trail = text[len(lead):], ""
    while True:
        tail = re.search(r"\s+$", core)
        if tail:
            trail, core = tail.group() + trail, core[: tail.start()]
        divider = _DIVIDER_TAIL.search(core)
        if divider:
            trail, core = divider.group() + trail, core[: divider.start()]
            continue
        break
    if "\n" not in lead:
        core, lead = lead + core, ""
    if "\n" not in trail and "=" not in trail:
        core, trail = core + trail, ""
    return lead, core, trail


def tellraw_array(line: str) -> tuple[int, list] | None:
    """``(json_start, components)`` of a single tellraw JSON array payload, else ``None``."""

    if "tellraw" not in line:
        return None
    starts = {locator.get("json_start") for locator, _text in extract_command_texts(line) if locator.get("kind") == "json"}
    if len(starts) != 1:
        return None
    start = starts.pop()
    if not isinstance(start, int):
        return None
    try:
        value, _end = _DECODER.raw_decode(line, start)
    except ValueError:
        return None
    return (start, value) if isinstance(value, list) else None


def _is_prose(component: object) -> bool:
    return (
        isinstance(component, dict)
        and isinstance(component.get("text"), str)
        and "extra" not in component
        and not any(key in component for key in _CLICK_KEYS)
        and not any(key in component for key in _NON_TEXT_KEYS)
    )


def passages(components: list) -> list[list[int]]:
    """Component indices of every passage, bounded by the source's fixed edges."""

    runs: list[list[int]] = []
    current: list[int] = []
    for index, component in enumerate(components):
        if not _is_prose(component):
            if current:
                runs.append(current)
            current = []
            continue
        lead, core, trail = fixed_edges(component["text"])
        if lead and current:
            runs.append(current)
            current = []
        if core.strip():
            current.append(index)
        if trail:
            if current:
                runs.append(current)
            current = []
    if current:
        runs.append(current)
    return runs


def passage_id(file_path: str, line_no: int, json_start: int, members: list[int], source_line: str) -> str:
    """Identity of one passage of the *current* input line."""

    payload = json.dumps(
        [PASSAGE_RULE_VERSION, file_path.replace("\\", "/"), line_no, json_start, members,
         hashlib.sha256(source_line.encode("utf-8")).hexdigest()],
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def pointer_index(command_locator: dict[str, object]) -> int | None:
    """Top-level component index of a ``/N/text`` JSON locator."""

    match = re.fullmatch(r"/(\d+)/text", str(command_locator.get("pointer") or ""))
    return int(match.group(1)) if match and command_locator.get("kind") == "json" else None


def locate(source_line: str, indices: set[int]) -> tuple[int, list[int]] | None:
    """``(json_start, members)`` of the one passage that holds every index, else ``None``."""

    parsed = tellraw_array(source_line)
    if parsed is None:
        return None
    start, components = parsed
    holders = [run for run in passages(components) if indices & set(run)]
    if len(holders) != 1 or not indices <= set(holders[0]):
        return None
    return start, holders[0]


def divider_rows(core: str) -> list[tuple[int, str]]:
    """Divider rows of one member core with their row (from the start, or from the end when not first)."""

    rows = core.split("\n")
    return [(number if number == 0 else number - len(rows), row) for number, row in enumerate(rows) if _DIVIDER_ROW.match(row)]


def _style(component: dict) -> str:
    return json.dumps({key: component.get(key) for key in _STYLE_KEYS}, sort_keys=True)


def check_passage(source_line: str, members: list[int], targets: dict[int, str], json_start: int) -> str | None:
    """Reason the revised passage is not provably safe, or ``None``.

    ``targets`` holds the revised text of every writable member; the other
    members keep their source text.  Character content per member is the
    caller's per-member gate; this function owns everything about breaks.
    """

    parsed = tellraw_array(source_line)
    if parsed is None or parsed[0] != json_start:
        return "passage_not_found"
    components = parsed[1]
    if members not in passages(components) or not set(targets) <= set(members):
        return "passage_not_found"
    source_breaks = 0
    target_breaks = 0
    stream: list[str] = []
    owner: list[int] = []
    cuts: list[int] = []
    for index in members:
        source = components[index]["text"]
        target = targets.get(index, source)
        lead, source_core, trail = fixed_edges(source)
        if lead and not target.startswith(lead):
            return "passage_fixed_edge"
        if trail:
            match = re.search(r"[ \t]*" + re.escape(trail.lstrip(" \t")) + r"$", target)
            if match is None:
                return "passage_fixed_edge"
            target_trail = match.group()
        else:
            target_trail = ""
        if len(lead) + len(target_trail) > len(target):
            return "passage_fixed_edge"
        core = target[len(lead): len(target) - len(target_trail)]
        if _INNER_FIXED_ROW.search(core):
            return "passage_inner_fixed_row"
        # v2: a divider row inside a member (e.g. one that starts "====\n") is
        # fixed like an edge: the same rows, byte for byte, at the same place.
        if divider_rows(source_core) != divider_rows(core):
            return "passage_inner_fixed_row"
        source_breaks += source_core.count("\n")
        target_breaks += len(_BREAK.findall(core))
        for piece_no, piece in enumerate(_BREAK.split(core)):
            if piece_no:
                cuts.append(len(stream))
            for char in piece:
                stream.append(char)
                owner.append(index)
    if target_breaks != source_breaks:
        return "passage_break_count"
    if len(set(cuts)) != len(cuts):
        return "passage_empty_row"
    styles = {index: _style(components[index]) for index in members}
    for position in cuts:
        if not 0 < position < len(stream):
            return "passage_empty_row"
        before, after = stream[position - 1], stream[position]
        style_edge = owner[position - 1] != owner[position] and styles[owner[position - 1]] != styles[owner[position]]
        if _HAN.match(before) and _HAN.match(after) and not style_edge:
            return "passage_break_between_hanzi"
        if after in _NO_LINE_START or before in _NO_LINE_END:
            return "passage_break_punctuation"
        # Inside one component a break between two letters stands for the space
        # the author replaced; only a break across same-style components can
        # split a word.
        if _ascii_word(before) and _ascii_word(after) and owner[position - 1] != owner[position] and not style_edge:
            return "passage_break_in_word"
    return None


def compose_line(line: str, located: list[tuple[dict[str, object], str]]) -> tuple[str, list[str]]:
    """Apply every member to ``line`` at once; ``(new_line, refusals)``."""

    refusals: list[str] = []
    for command_locator, target in sorted(located, key=lambda item: command_locator_offset(item[0]), reverse=True):
        line = apply_command_text(line, command_locator, target, refusals=refusals)
    return line, refusals


def check_line(source_line: str, members: list[int], json_start: int,
               located: list[tuple[dict[str, object], str]]) -> str | None:
    """Full passage check on the composed line, including the interaction gate."""

    targets = {}
    for command_locator, target in located:
        index = pointer_index(command_locator)
        if index is None or command_locator.get("json_start") != json_start:
            return "passage_not_found"
        targets[index] = target
    reason = check_passage(source_line, members, targets, json_start)
    if reason is not None:
        return reason
    composed, refusals = compose_line(source_line, located)
    if refusals:
        return refusals[0]
    return validate_interactive_command_change(source_line, composed)
