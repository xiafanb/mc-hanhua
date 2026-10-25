"""Independent mechanism-skeleton audit (hardening plan W4).

This module exists because every other pre-publication check is driven by the
extractor: "what may change" is the set of leaves the extractor declared, the
token comparison at candidate time and the read-back comparison all share that
declaration.  A gap in the extractor (or in the write door's token grammar)
therefore disables the audit too — fossil-frights shipped a translated macro
argument and a doubled backslash with `audit error_count 0 / blocked false`.

The checks below re-read both sides themselves and never consult
``policy``/whitelists.  They share only the *grammar* with the write door
(``quality.STRUCTURAL_TOKEN_PATTERNS``), which is the single source of truth for
what counts as a non-linguistic token.

Rules
-----
T  token equality per declared unit: the decoded input value (``unit.source_text``)
   and the value read back from the output must carry the same tokens.
D  line tokens: every ``.mcfunction`` line must keep the same whole-line token
   multiset (catches damage outside declared spans too).  Escapes are compared by
   meaning, not spelling: ``\u25b6`` and ``▶`` are the same token to the game, so
   every resolvable escape is decoded before tokenizing, while an unresolvable one
   stays verbatim and must then match exactly.
A  function-call arguments are mechanism by default: a changed argument value is
   an error unless an audited allowlist entry covers it.  Display-by-design
   arguments (TypeWriter's ``dialog``) are the documented exception, granted per
   (function id, argument) in ``assets/mechanism-allowlist.json``.
P  payload skeleton: a JSON payload on a changed command line must still parse
   and keep its ``json_skeleton`` — keys, array lengths and non-string values,
   i.e. click / hover event attributes stay attached while text leaves stay free
   to change.  SNBT payloads (unquoted keys) are left to the residue rule.
I  interaction layout: a changed line that builds a clickable message (tellraw /
   title) must still render the same interaction.  The payload is parsed here —
   not through the write door's verdicts, unit metadata or exception tables —
   and the clickable fragments, their inherited events and commands, their
   control marks and fixed gaps, their trailing pads, the explicit line offset
   of each one and the fill line that places it must survive.  A rewritten
   interactive line whose payload cannot be read is an error too: unlike the
   skeleton rule, it is never silently skipped.  Ordinary prose without click
   events is not compared, and a translation may be longer or shorter.
L  fixed divider layout: every rendered ``====`` row in a display command must
   remain an independent row with the same bytes and multiplicity, whether or
   not the command has a click event.  Lost, resized, or prose-glued dividers
   are errors; this parser is independent of the candidate-time layout rule.
N  passage line breaks: when breaks moved between the plain-text components of
   one tellraw passage (one member lost a break while another gained one), the
   passage must keep its total, its fixed edges, no blank/divider rows and legal
   break positions.  Rebuilt from the input and output lines only (§3.12).

Known limits, reported instead of silently skipped: nested-archive spans (``!/``)
are not re-read (the archive is repacked); call-site argument blocks are matched
at the end of a line; a baseline capture cap surfaces as an ``info`` issue.
"""

from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import TextUnit, structured_locator
from .quality import COMMAND_RE, STRUCTURAL_TOKEN_PATTERNS, URL_RE, structural_tokens_match
from .text_components import (
    _COMMAND_PAYLOAD_RE,
    _command_payload_view,
    _find_execute_run,
    _find_json_start,
    _iter_snbt_values,
    _matching_json_end,
    _resolve_snbt_span,
    _snbt_unquote,
    _wrapped_snbt_tree,
    component_document,
    escape_pointer_part,
    is_display_text_path,
)
from .utils import UNREADABLE_TEXT_ERRORS, read_text

# ``function <ns>:<path> {…}`` at the end of a line: the only place a custom
# function receives arguments.  A macro call (``function ns:path with …``) has no
# braces and is therefore not treated as an argument block.
FUNCTION_CALL_RE = re.compile(r"(?<![\w:/])function\s+([a-z0-9_.-]+:[a-z0-9_./-]+)\s*(\{.*\})\s*$")
# A JSON payload starts (after any run of containers) with a quoted key; SNBT
# payloads use unquoted keys.  ``["",{...}]`` therefore counts as JSON while
# ``[{text:"..."}]`` does not.
_JSON_PAYLOAD_HEAD_RE = re.compile(r'^[\s\[{\]]*"')

ALLOWLIST_ENV = "MC_HANHUA_MECHANISM_ALLOWLIST"
ALLOWLIST_ASSET = Path("assets") / "mechanism-allowlist.json"


def json_skeleton(
    value: Any,
    *,
    writable: frozenset[str] | set[str] = frozenset(),
    pointer: str = "",
    fold_text: bool = False,
    keys: tuple[str, ...] = (),
) -> Any:
    """Structural skeleton: keys/containers/values, with components opened.

    ``writable`` names the leaves the extractor declared writable, in file-space
    pointer form.  An encoded component's inner leaf composes onto its outer
    pointer exactly as ``audit._writable_leaf_keys`` composes them
    (``/components/minecraft:lore/0`` + ``/text``), so masking and comparison
    cannot drift apart.

    A string that holds a JSON object/array is not an opaque text leaf: the game
    parses it as a component, so the skeleton descends into it.  Inside an
    encoded component comparison is strict --- undeclared leaves are compared by
    value, which is what keeps ``color``/``italic``/``bold``, event
    action/value/URL and numbers attached to the input.

    ``fold_text`` decides how the *outer* document's own string leaves are
    treated: with ``True`` only the *proven display* paths fold (a payload's
    ``text``/``fallback``/``extra``/``with``/``contents`` leaves and the audited
    book/sign/title containers), while style and event attributes ---
    ``color``, ``italic``, ``bold``, ``clickEvent``/``hoverEvent``
    ``action``/``value``/``url``, ``translate`` and every unknown field --- are
    compared by value; a display leaf under a mechanism object (``score.text``,
    ``clickEvent.text``, a plugin's own ``text``) is not display text either.
    ``keys`` is the key chain *inside the compared document* (array indices
    dropped), so a container's storage path never leaks into the decision.
    With ``False`` (a JSON file, an NBT document) nothing folds except the
    leaves ``writable`` masks.
    """

    if isinstance(value, str):
        if pointer in writable:
            return ("writable",)
        component = component_document(value)
        if component is not None:
            return json_skeleton(component, writable=writable, pointer=pointer, keys=keys)
        if fold_text and is_display_text_path(keys):
            return ("leaf",)
        return ("leaf", value)
    if isinstance(value, dict):
        return (
            "dict",
            sorted(
                (
                    key,
                    json_skeleton(
                        child,
                        writable=writable,
                        pointer=f"{pointer}/{escape_pointer_part(str(key))}",
                        fold_text=fold_text,
                        keys=(*keys, str(key)),
                    ),
                )
                for key, child in value.items()
            ),
        )
    if isinstance(value, list):
        return (
            "list",
            [
                json_skeleton(
                    child,
                    writable=writable,
                    pointer=f"{pointer}/{index}",
                    fold_text=fold_text,
                    keys=keys,
                )
                for index, child in enumerate(value)
            ],
        )
    return ("leaf", value)


@dataclass(slots=True)
class ArgumentAllowlist:
    """Audited exceptions for call-site arguments that are display text by design."""

    entries: set[tuple[str, str]] = field(default_factory=set)

    def allows(self, function_id: str, argument: str) -> bool:
        return (function_id, argument) in self.entries


def load_argument_allowlist(path: Path | str | None = None) -> ArgumentAllowlist:
    """Read the exception table; a missing or broken source grants nothing.

    An explicit ``path`` (or ``MC_HANHUA_MECHANISM_ALLOWLIST``) is authoritative:
    a typo there disables exceptions instead of silently falling back to the
    bundled table.  Only the default source is resolved next to the package.
    """

    override = str(path) if path is not None else (os.getenv(ALLOWLIST_ENV) or "")
    bundled = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2])) / ALLOWLIST_ASSET
    candidates = [Path(override)] if override else [bundled]
    for candidate in candidates:
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        entries: set[tuple[str, str]] = set()
        for entry in payload.get("function_arguments") or []:
            if not isinstance(entry, dict):
                continue
            function = str(entry.get("function") or "")
            argument = str(entry.get("argument") or "")
            if function and argument:
                entries.add((function, argument))
        return ArgumentAllowlist(entries)
    return ArgumentAllowlist()


def token_difference(source: str, target: str) -> list[str]:
    """Tokens present on one side only (URL / command / placeholder / § / macro / escape).

    The verdict comes from ``quality.structural_tokens_match`` — the same one the
    write door reads — so the audit's own question stays "did a structural token
    change?", not "did the candidate spell it the way the source did": an
    official-style indexed conversion for a plain one is accepted here too, and
    the list below is only the message for a real difference.
    """

    if structural_tokens_match(source, target):
        return []
    differences: list[str] = []
    for pattern in STRUCTURAL_TOKEN_PATTERNS:
        before = Counter(match.group(0) for match in pattern.finditer(source))
        after = Counter(match.group(0) for match in pattern.finditer(target))
        differences.extend(sorted((before - after) | (after - before)))
    return differences


def embedded_display_command_preserves_mechanisms(source: str, target: str) -> bool:
    """Prove that a supported embedded display command kept its mechanisms.

    Book pages can contain JSON click-event values that themselves hold a
    ``tellraw`` or ``title`` command, or a plain ``/say `` command with one
    display-only body. The outer book audit cannot treat that whole value as
    writable: this independent check first preserves the command shell, JSON
    payload skeleton or plain-message boundary, structural tokens, and any
    nested interaction layout. Callers may mask the value in their container
    skeleton only after this returns true. Other plain commands remain closed.
    """

    source_say_body = _embedded_say_command_body(source)
    target_say_body = _embedded_say_command_body(target)
    if source_say_body is not None or target_say_body is not None:
        return (
            source_say_body is not None
            and target_say_body is not None
            and structural_tokens_match(source, target)
        )

    source_skeleton = _embedded_display_command_skeleton(source)
    target_skeleton = _embedded_display_command_skeleton(target)
    if source_skeleton is None or target_skeleton is None or source_skeleton != target_skeleton:
        return False
    if not structural_tokens_match(source, target):
        return False
    source_scope, source_leaves = _audit_render(source)
    target_scope, target_leaves = _audit_render(target)
    if not source_scope or not target_scope or source_leaves is None or target_leaves is None:
        return False
    if _interaction_differences(source_leaves, target_leaves):
        return False
    return _audit_fixed_divider_rows(source) == _audit_fixed_divider_rows(target)


_EMBEDDED_SAY_UNSAFE_SEPARATORS = ("\n", "\r", "\x00", "\u2028", "\u2029", ";", "&&", "||")


def _embedded_say_command_body(command: str) -> str | None:
    """Read the body of the sole explicitly supported plain command shape."""

    prefix = "/say "
    if not command.startswith(prefix):
        return None
    body = command[len(prefix) :]
    if not body or not body.strip() or any(separator in command for separator in _EMBEDDED_SAY_UNSAFE_SEPARATORS):
        return None
    # A second command token cannot be treated as message text by this narrow
    # exception. Strip complete URLs first because a URL path may look like a
    # slash command; the shared token comparison still requires it unchanged.
    if COMMAND_RE.search(URL_RE.sub(" ", body)):
        return None
    return body


def _embedded_display_command_skeleton(command: str) -> Any | None:
    """Return the command shell plus its display-folded JSON skeleton."""

    state, payload = _payload_state(command)
    if state != "json":
        return None
    command_start = _find_execute_run(command)
    if command_start is None:
        command_start = 0
    tail = command[command_start:]
    match = _COMMAND_PAYLOAD_RE.match(tail)
    if match is None:
        return None
    remainder = tail[match.end() :]
    start = _find_json_start(remainder)
    end = _matching_json_end(remainder, start) if start >= 0 else None
    if start < 0 or end is None:
        return None
    absolute_start = command_start + match.end() + start
    absolute_end = command_start + match.end() + end
    return (
        command[:absolute_start],
        json_skeleton(payload, fold_text=True),
        command[absolute_end:],
    )


def audit_mechanism_skeleton(
    root: Path,
    baseline: Any,
    units: list[TextUnit],
    *,
    allowlist: ArgumentAllowlist | None = None,
) -> list[Any]:
    """Compare the pre-write baseline with the files under ``root``; read-only."""

    from .audit import AuditIssue
    from .extractors import LocatedTextReadError, read_located_text

    issues: list[AuditIssue] = []
    allowlist = allowlist if allowlist is not None else load_argument_allowlist()
    by_file: dict[str, list[TextUnit]] = {}
    for unit in units:
        # Nested-archive spans cannot be re-read after repacking: they are left to
        # the write door and reported by the located-writeback check.
        if unit.final_target is None or "!/" in unit.file_path:
            continue
        by_file.setdefault(unit.file_path, []).append(unit)

    command_lines: dict[str, dict[int, str]] = getattr(baseline, "command_lines", None) or {}
    for rel in sorted(set(by_file) | set(command_lines)):
        file_units = by_file.get(rel, [])
        cache: dict[str, Any] = {}
        for unit in file_units:
            try:
                actual = read_located_text(root, unit, cache=cache)
            except LocatedTextReadError:
                continue
            except Exception:  # pragma: no cover - defensive: audit must never abort a run
                continue
            differences = token_difference(unit.source_text, actual)
            if differences:
                issues.append(
                    AuditIssue(
                        "error",
                        "mechanism",
                        rel,
                        f"写回后的非语言 token 与输入不一致：{'、'.join(differences)}",
                        {
                            "locator": unit.locator,
                            "context": unit.context,
                            "before": unit.source_text,
                            "after": actual,
                            "tokens": differences,
                        },
                    )
                )
        input_lines = command_lines.get(rel)
        if not input_lines:
            continue
        try:
            output_lines = read_text(root / rel).splitlines()
        except UNREADABLE_TEXT_ERRORS:
            continue
        for line_no, input_line in sorted(input_lines.items()):
            if 0 <= line_no < len(output_lines) and output_lines[line_no] == input_line:
                continue
            if not (0 <= line_no < len(output_lines)):
                issues.append(
                    AuditIssue(
                        "error",
                        "structure",
                        rel,
                        "写回后命令行消失",
                        {"line": line_no + 1, "before": input_line},
                    )
                )
                continue
            issues.extend(_compare_command_line(rel, line_no, input_line, output_lines[line_no], file_units, allowlist))
    return issues


def _compare_command_line(
    rel: str,
    line_no: int,
    input_line: str,
    output_line: str,
    units: list[TextUnit],
    allowlist: ArgumentAllowlist,
) -> list[Any]:
    from .audit import AuditIssue

    issues: list[AuditIssue] = []
    label = f"第 {line_no + 1} 行"

    before_args = function_call_arguments(input_line)
    after_args = function_call_arguments(output_line)
    for key in sorted(set(before_args) | set(after_args)):
        before, after = before_args.get(key), after_args.get(key)
        if before == after:
            continue
        function_id, argument = key
        if before is not None and after is not None and allowlist.allows(function_id, argument):
            continue
        issues.append(
            AuditIssue(
                "error",
                "mechanism",
                rel,
                f"{label}：函数调用实参被改写（{function_id} 的 {argument}）",
                {"line": line_no + 1, "function": function_id, "argument": argument, "before": before or "", "after": after or ""},
            )
        )

    line_units = [unit for unit in units if _unit_line(unit) == line_no]
    issues.extend(_line_token_issues(rel, label, input_line, output_line))
    issues.extend(_payload_skeleton_issues(rel, line_no, input_line, output_line, _declared_payload_pointers(line_units)))
    issues.extend(_encoded_component_issues(rel, label, input_line, output_line, line_units))
    issues.extend(_fixed_divider_issues(rel, label, line_no, input_line, output_line))
    issues.extend(_interaction_issues(rel, label, line_no, input_line, output_line))
    issues.extend(_passage_newline_issues(rel, label, line_no, input_line, output_line))

    masked_before = _masked_line(input_line, line_units)
    masked_after = _masked_line(output_line, line_units)
    if masked_before is None or masked_after is None:
        # Not silently skipped, but not damage either: the anchors this span was
        # found by are gone, which the token / payload rules judge on their own.
        issues.append(
            AuditIssue(
                "warning",
                "structure",
                rel,
                f"{label}：无法在写回后的行上定位声明跨度，未比对跨度之外的字节",
                {"line": line_no + 1},
            )
        )
    elif masked_before != masked_after:
        issues.append(
            AuditIssue(
                "error",
                "structure",
                rel,
                f"{label}：声明跨度之外的字节被改写",
                {"line": line_no + 1, "before": masked_before, "after": masked_after},
            )
        )
    return issues


# ---------------------------------------------------------------------------
# Rule I: interaction layout of clickable display commands
# ---------------------------------------------------------------------------
#
# This check reads both sides itself and never asks the write door: no gate
# call, no verdict, no unit metadata, no exception table.  Only the JSON/SNBT
# syntax is shared with the rest of the product (the payload view and the SNBT
# parser), so a hole in the gate - or a span the gate never saw - still shows up
# here.  What is compared is the *rendered* message: which components are
# clickable and what their click runs, where they sit in the component tree,
# which text each one draws, the alignment marks, the row's own trailing pad,
# and the explicit breaks and padding that place them.

# ``clickEvent`` is the modern key; ``click_event`` is the 1.12 spelling.
CLICK_EVENT_KEYS = ("clickEvent", "click_event")
#: Click actions whose value is the command the player runs.
_AUDIT_COMMAND_ACTIONS = frozenset({"run_command", "run_command_if_loaded", "suggest_command"})
#: Components rendered from live game data: they show text, but none from a file.
_AUDIT_DYNAMIC_KEYS = ("score", "selector", "nbt", "keybind")
#: Alignment marks a menu row can start with.  Deliberately the audit's own set:
#: the check must not inherit the write door's idea of what furniture is, and a
#: clickable leaf made only of marks and padding is never translatable, so a
#: wider set can only make the audit stricter about layout, never about prose.
_AUDIT_CONTROL_MARKS = "►▶▷▸▹➤»→⌲>"


@dataclass(frozen=True, slots=True)
class _RenderedLeaf:
    """One visible leaf of a clickable message, as the audit re-reads it.

    ``pointer`` is where the leaf sits in the payload (for the message),
    ``events`` holds every click event on its ancestor chain together with the
    pointer of the component that declares it — so "the same click" and "the
    same click in the same place" are different facts, and moving an event
    between components is visible even when every fragment still inherits the
    same action.
    """

    text: str | None
    events: tuple[tuple[str, str], ...]
    pointer: str

    @property
    def clickable(self) -> bool:
        return bool(self.events)


def mentions_click_event(line: str) -> bool:
    """True when a command line carries a click-event key."""

    return any(key in line for key in CLICK_EVENT_KEYS)


def _audit_event_spec(value: Any, keys: tuple[str, ...] = ()) -> Any:
    """Event payload with display leaves folded, everything else by value."""

    if isinstance(value, str):
        return ("display",) if is_display_text_path(keys) else value
    if isinstance(value, dict):
        return tuple(
            (str(key), _audit_event_spec(child, (*keys, str(key))))
            for key, child in sorted(value.items(), key=lambda item: str(item[0]))
        )
    if isinstance(value, list):
        return tuple(_audit_event_spec(child, keys) for child in value)
    return value


def _audit_click_identity(event: Any) -> str:
    """``action``, the command a click runs, then the rest of the event by value."""

    if not isinstance(event, dict):
        return f"raw:{event!r}"
    action = str(event.get("action") or "")
    command = ""
    if action in _AUDIT_COMMAND_ACTIONS:
        value = event.get("value")
        command = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    return f"{action}\x00{command}\x00{_audit_event_spec(event)!r}"


def _audit_render_leaf(node: Any, pointer: str, events: tuple[tuple[str, str], ...], out: list[_RenderedLeaf]) -> None:
    """Append the visible leaves of one component in the order the game draws them."""

    if isinstance(node, str):
        out.append(_RenderedLeaf(node, events, pointer))
        return
    if isinstance(node, list):
        for index, child in enumerate(node):
            _audit_render_leaf(child, f"{pointer}/{index}", events, out)
        return
    if not isinstance(node, dict):
        return
    for key in CLICK_EVENT_KEYS:
        if key in node:
            events = (*events, (pointer, _audit_click_identity(node[key])))
            break
    own = node.get("text")
    if not isinstance(own, (str, list)):
        # ``{text:[…]}`` is a component list: preferring the ``fallback`` here
        # would silence the list branch below and compare the row as textless.
        own = node.get("fallback")
    if isinstance(own, str):
        out.append(_RenderedLeaf(own, events, pointer))
    elif isinstance(own, list):
        # ``{text:[…]}``: a component list stored under ``text``, as extraction
        # reads it.  Walking it here keeps such a payload comparable instead of
        # silently rendering nothing.
        _audit_render_leaf(own, f"{pointer}/text", events, out)
    for child_key in ("with", "extra"):
        if child_key in node:
            _audit_render_leaf(node[child_key], f"{pointer}/{child_key}", events, out)
    if not isinstance(own, str) and any(key in node for key in _AUDIT_DYNAMIC_KEYS):
        out.append(_RenderedLeaf(None, events, pointer))


def _audit_render(line: str) -> tuple[bool, tuple[_RenderedLeaf, ...] | None]:
    """``(is_display_command, leaves)``; ``leaves`` is ``None`` when unreadable."""

    view = _command_payload_view(line)
    if view is None:
        return False, None
    base, _wrapped = view
    try:
        tree: Any = json.loads(line[base:])
    except json.JSONDecodeError:
        tree = _wrapped_snbt_tree(line)
        if tree is None:
            return True, None
    leaves: list[_RenderedLeaf] = []
    _audit_render_leaf(tree, "", (), leaves)
    return True, tuple(leaves)

def _audit_fixed_divider_rows(line: str) -> Counter[str] | None:
    """Independent rendered ``====`` rows, or ``None`` when unreadable."""

    in_scope, leaves = _audit_render(line)
    if not in_scope:
        return Counter()
    if leaves is None:
        return None
    rendered = "".join(leaf.text or "" for leaf in leaves)
    return Counter(
        row
        for row in rendered.split("\n")
        if len(row.strip()) >= 4 and set(row.strip()) == {"="}
    )


def _fixed_divider_issues(rel: str, label: str, line_no: int, input_line: str, output_line: str) -> list[Any]:
    """Block a fixed separator row that was lost, resized, or glued to prose."""

    from .audit import AuditIssue

    before = _audit_fixed_divider_rows(input_line)
    if not before:
        return []
    after = _audit_fixed_divider_rows(output_line)
    if after == before:
        return []
    problems = ["写回后的显示载荷无法解析"] if after is None else [
        f"固定分隔行 {dict(before)} -> {dict(after)}"
    ]
    return [
        AuditIssue(
            "error",
            "layout",
            rel,
            f"{label}：显示命令的固定分隔行被改写（{problems[0]}）",
            {"line": line_no + 1, "problems": problems, "before": input_line, "after": output_line},
        )
    ]


def _audit_furniture(text: str) -> bool:
    """True when a leaf is only alignment marks and whitespace."""

    return all(char in _AUDIT_CONTROL_MARKS or char.isspace() for char in text)


def _audit_trailing_pad(text: str) -> str:
    """Trailing ASCII spaces/tabs of a clickable row: the author's row width."""

    return text[len(text.rstrip(" \t")) :]


def _audit_line_offset(text: str) -> tuple[int, tuple[int, ...]]:
    """Explicit line offset: how many real breaks, and where the empty lines are."""

    return text.count("\n"), tuple(index for index, line in enumerate(text.split("\n")) if not line)


def _audit_mark_prefix(text: str) -> tuple[str, str] | None:
    """``(marks, fixed gap)`` a clickable row starts with, or ``None``."""

    match = re.match(rf"([{re.escape(_AUDIT_CONTROL_MARKS)}]+)([ \t]*)", text.lstrip())
    if match is None:
        return None
    return match.group(1), match.group(2)


def _interaction_differences(before: tuple[_RenderedLeaf, ...], after: tuple[_RenderedLeaf, ...]) -> list[str]:
    """Every way the written message no longer renders the same interaction."""

    if len(before) != len(after):
        return [f"可见片段数 {len(before)} -> {len(after)}"]
    problems: list[str] = []
    source_prefix = ""
    target_prefix = ""
    for index, (source, target) in enumerate(zip(before, after)):
        position = f"第 {index + 1} 个片段"
        if (source.text is None) != (target.text is None):
            problems.append(f"{position}的渲染来源变了")
        if source.events != target.events:
            problems.append(f"{position}的点击事件、命令目标或其位置变了")
        source_text = source.text
        target_text = target.text
        if source_text is not None and target_text is not None:
            if _audit_furniture(source_text) and source_text != target_text:
                problems.append(f"{position}是排版空白/符号，内容被改写")
            elif source.clickable:
                open_source = source_prefix + source_text[: len(source_text) - len(source_text.lstrip())]
                open_target = target_prefix + target_text[: len(target_text) - len(target_text.lstrip())]
                if _audit_line_offset(open_source) != _audit_line_offset(open_target):
                    problems.append(f"{position}之前的显式换行数/空行位置变了")
                source_line = open_source.rsplit("\n", 1)[-1]
                target_line = open_target.rsplit("\n", 1)[-1]
                if not source_line.strip() and source_line != target_line:
                    problems.append(f"{position}所在行的填充空白被改写")
                if source_text.strip() and not target_text.strip():
                    problems.append(f"{position}的可点击文字被清空")
                if _audit_trailing_pad(source_text) != _audit_trailing_pad(target_text):
                    problems.append(f"{position}的尾随空格被改写")
                marks = _audit_mark_prefix(source_text)
                if marks is not None and _audit_mark_prefix(target_text) != marks:
                    problems.append(f"{position}的前导控制符号或其固定空格被改写")
        source_prefix += source_text or ""
        target_prefix += target_text or ""
    return problems


# Rule N (§3.12): passage line-break conservation, rebuilt from the input and
# output lines only.  Deliberately its own implementation: it shares JSON syntax
# helpers with the other rules but nothing with the writer's passage module.
_N_CLICK_KEYS = ("clickEvent", "click_event")
_N_NON_TEXT_KEYS = ("score", "selector", "nbt", "keybind", "translate", "extra")
_N_STYLE_KEYS = ("color", "bold", "italic", "underlined", "strikethrough", "obfuscated", "font", "hoverEvent", "hover_event", "insertion")
_N_BREAK = re.compile(r"[ \t]*\n[ \t]*")
_N_INNER_FIXED = re.compile(r"\n[ \t]*\n|\n[ \t]*={3,}")
_N_TAIL_DIVIDER = re.compile(r"\n[ \t]*={3,}[ \t]*$")
_N_HAN = re.compile(r"[㐀-鿿豈-﫿]")
_N_NO_START = set("，。！？；：、…）」』”’,.!?;:%")
_N_NO_END = set("（「『“‘(")


def _n_components(line: str) -> list | None:
    if "tellraw" not in line:
        return None
    view = _command_payload_view(line)
    if view is None:
        return None
    try:
        tree: Any = json.loads(line[view[0]:])
    except json.JSONDecodeError:
        return None
    return tree if isinstance(tree, list) else None


def _n_edges(text: str) -> tuple[str, str]:
    """Leading / trailing fixed edge of one component (only when it carries a break or a divider)."""

    lead = re.match(r"\s*", text).group()
    rest, trail = text[len(lead):], ""
    while True:
        tail = re.search(r"\s+$", rest)
        if tail:
            trail, rest = tail.group() + trail, rest[: tail.start()]
        divider = _N_TAIL_DIVIDER.search(rest)
        if not divider:
            break
        trail, rest = divider.group() + trail, rest[: divider.start()]
    return (lead if "\n" in lead else ""), (trail if ("\n" in trail or "=" in trail) else "")


def _n_passages(components: list) -> list[list[int]]:
    runs: list[list[int]] = []
    current: list[int] = []
    for index, component in enumerate(components):
        plain = (isinstance(component, dict) and isinstance(component.get("text"), str)
                 and not any(key in component for key in (*_N_CLICK_KEYS, *_N_NON_TEXT_KEYS)))
        if not plain:
            if current:
                runs.append(current)
            current = []
            continue
        lead, trail = _n_edges(component["text"])
        if lead and current:
            runs.append(current)
            current = []
        if component["text"].strip(" \t\n=") and component["text"][len(lead): len(component["text"]) - len(trail)].strip():
            current.append(index)
        if trail:
            if current:
                runs.append(current)
            current = []
    if current:
        runs.append(current)
    return runs


def _n_shape(text: str) -> tuple:
    """Row structure of one component: edge break counts, blank-row and divider-row indices."""

    rows = text.split("\n")
    lead = text[: len(text) - len(text.lstrip(" \t\n"))]
    stripped = text.rstrip(" \t\n")
    trail = text[len(stripped):] if stripped else ""
    return (
        lead.count("\n"),
        trail.count("\n"),
        tuple(i for i, row in enumerate(rows) if not row.strip() and 0 < i < len(rows) - 1),
        tuple(i for i, row in enumerate(rows) if re.fullmatch(r"\s*={3,}\s*", row)),
    )


def _passage_newline_issues(rel: str, label: str, line_no: int, input_line: str, output_line: str) -> list[Any]:
    """A passage whose breaks moved across components must conserve its layout."""

    from .audit import AuditIssue

    before = _n_components(input_line)
    if before is None:
        return []
    after = _n_components(output_line)
    if after is None or len(after) != len(before):
        return []  # skeleton changes are rule P's finding
    issues: list[Any] = []
    for members in _n_passages(before):
        texts_in = [before[i]["text"] for i in members]
        texts_out = [after[i].get("text") if isinstance(after[i], dict) else None for i in members]
        if not all(isinstance(text, str) for text in texts_out):
            continue
        counts_in = [text.count("\n") for text in texts_in]
        counts_out = [text.count("\n") for text in texts_out]
        moved = any(a < b for a, b in zip(counts_out, counts_in)) and any(a > b for a, b in zip(counts_out, counts_in))
        if not moved:
            # No break crossed a component.  The passage read as one text must keep
            # its row structure: edge break counts and the positions of blank and
            # divider rows.  Prose break positions are not judged here (per-component
            # output legitimately places them anywhere), and whitespace-only members
            # are reviewed fragment drops audited by the fragment rule.
            # Only when every member kept its own break count: that is the case no
            # other rule covers (a dropped break is the per-component path's own,
            # long-standing tolerance, and dividers/buttons have rules L and I).
            if texts_in != texts_out and counts_in == counts_out and _n_shape("".join(texts_in)) != _n_shape(
                "".join(t if t.strip() or not s.strip() else s for s, t in zip(texts_in, texts_out))
            ):
                issues.append(
                    AuditIssue(
                        "error",
                        "passage-layout",
                        rel,
                        f"{label}：段内换行结构被改写（首尾换行、空行或分隔线位置变化）",
                        {"line": line_no + 1, "components": members},
                    )
                )
            continue
        problems: list[str] = []
        if sum(counts_out) != sum(counts_in):
            problems.append("段内换行总数改变")
        stream: list[tuple[str, int]] = []
        cuts: list[int] = []
        for index, source, target in zip(members, texts_in, texts_out):
            lead, trail = _n_edges(source)
            if lead and not target.startswith(lead):
                problems.append("成员开头的固定换行被改写")
                continue
            trail_match = re.search(r"[ \t]*" + re.escape(trail.lstrip(" \t")) + r"$", target) if trail else None
            if trail and trail_match is None:
                problems.append("成员结尾的固定行被改写")
                continue
            core = target[len(lead): trail_match.start() if trail_match else len(target)]
            if _N_INNER_FIXED.search(core):
                problems.append("段内出现空行或分隔线")
            for piece_no, piece in enumerate(_N_BREAK.split(core)):
                if piece_no:
                    cuts.append(len(stream))
                stream.extend((char, index) for char in piece)
        style = {i: json.dumps({k: before[i].get(k) for k in _N_STYLE_KEYS}, sort_keys=True) for i in members}
        for position in cuts:
            if not 0 < position < len(stream) or cuts.count(position) > 1:
                problems.append("段内出现空行")
                continue
            (a, owner_a), (b, owner_b) = stream[position - 1], stream[position]
            same_style = owner_a == owner_b or style[owner_a] == style[owner_b]
            if _N_HAN.match(a) and _N_HAN.match(b) and same_style:
                problems.append(f"在两个汉字之间断行（{a}｜{b}）")
            elif b in _N_NO_START or a in _N_NO_END:
                problems.append(f"断行落在标点上（{a}｜{b}）")
        if problems:
            issues.append(
                AuditIssue(
                    "error",
                    "passage-layout",
                    rel,
                    f"{label}：跨组件换行重排不守恒（{'；'.join(dict.fromkeys(problems))}）",
                    {"line": line_no + 1, "components": members, "problems": problems},
                )
            )
    return issues


def _interaction_issues(rel: str, label: str, line_no: int, input_line: str, output_line: str) -> list[Any]:
    """Compare the interaction of a changed command line against its baseline.

    Ordinary lines are not compared at all: a line without click events, and a
    line whose click events are not on a supported display command, are silent.
    A clickable line that cannot be read on either side is an error — never a
    warning — because it is not proof of safety.
    """

    from .audit import AuditIssue

    if not mentions_click_event(input_line):
        return []
    in_scope, source_leaves = _audit_render(input_line)
    if not in_scope:
        return []
    unreadable = False
    if source_leaves is None:
        problems = ["源命令的显示载荷无法解析"]
        unreadable = True
    elif not any(leaf.clickable for leaf in source_leaves):
        return []
    else:
        target_in_scope, target_leaves = _audit_render(output_line)
        if not target_in_scope or target_leaves is None:
            problems = ["写回后的显示载荷无法解析"]
            unreadable = True
        else:
            problems = _interaction_differences(source_leaves, target_leaves)
            if not problems:
                return []
    if unreadable:
        message = f"{label}：含点击事件的显示命令无法核实（{problems[0]}）"
    else:
        message = f"{label}：可点击消息的交互排版被改写（{'；'.join(problems[:3])}）"
    return [
        AuditIssue(
            "error",
            "interaction",
            rel,
            message,
            {"line": line_no + 1, "problems": problems, "before": input_line, "after": output_line},
        )
    ]


# Every escape the game (or, for named escapes, the Unicode database) resolves.
# Unresolvable spellings stay verbatim, which is what makes a doubled backslash
# around them visible again.
_LINE_ESCAPE_RE = re.compile(r"\\(N\{([^}]*)\}|u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8}|x[0-9A-Fa-f]{2}|[nrtbf'\"\\\\])")
_SIMPLE_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "\\": "\\", "'": "'", '"': '"'}


def _normalize_line_escapes(line: str) -> str:
    """Decode resolvable escapes so token comparison is spelling-insensitive.

    ``\u25b6`` and the literal ``▶`` are the same character to the game; treating
    them as different tokens produced 823 false positives on a real map.  A single
    pass also keeps ``\\n`` (literal backslash + n) distinct from a real newline.
    """

    def replace(match: re.Match[str]) -> str:
        body = match.group(1)
        if body.startswith("N{"):
            try:
                return unicodedata.lookup(match.group(2) or "")
            except KeyError:
                return match.group(0)
        if body in _SIMPLE_ESCAPES:
            return _SIMPLE_ESCAPES[body]
        try:
            return chr(int(body[1:], 16))
        except ValueError:  # pragma: no cover - the regex only matches hex digits
            return match.group(0)

    return _LINE_ESCAPE_RE.sub(replace, line)


def _line_token_issues(rel: str, label: str, input_line: str, output_line: str) -> list[Any]:
    """Whole-line token multiset: structural syntax outside the units is protected too."""

    from .audit import AuditIssue

    differences = token_difference(_normalize_line_escapes(input_line), _normalize_line_escapes(output_line))
    if not differences:
        return []
    return [
        AuditIssue(
            "error",
            "mechanism",
            rel,
            f"{label}：命令行上的非语言 token 与输入不一致：{'、'.join(differences)}",
            {"line": label, "before": input_line, "after": output_line, "tokens": differences},
        )
    ]


def _declared_payload_pointers(units: list[TextUnit]) -> frozenset[str]:
    """Payload-relative pointers of the JSON leaves these units declared writable."""

    pointers: set[str] = set()
    for unit in units:
        if unit.final_target is None:
            continue
        locator = _parsed_locator(unit)
        command = locator.get("command") if isinstance(locator, dict) else None
        if not isinstance(command, dict) or command.get("kind") not in {"json", "json_slice"}:
            continue
        pointer = command.get("pointer")
        if isinstance(pointer, str):
            pointers.add(pointer)
    return frozenset(pointers)


def _parsed_locator(unit: TextUnit) -> dict[str, Any]:
    return structured_locator(unit) or {}


def _encoded_component_issues(
    rel: str,
    label: str,
    input_line: str,
    output_line: str,
    units: list[TextUnit],
) -> list[Any]:
    """Declared leaves inside a JSON-encoded component must keep their structure.

    The masked-span rule masks the whole declared span and the payload rule only
    looks at JSON payloads, so neither sees the *shape* of a component written as
    a string.  This re-reads both sides with the same structural comparison used
    for JSON files and NBT: only the declared inner leaves may differ, while the
    style and event fields around them, the keys and the numbers stay identical.

    The writable set is the union over every unit living in the same container
    (one component string can carry several declared leaves); comparing a unit
    against only its own leaf would report its siblings' translation as damage.
    """

    from .audit import AuditIssue

    containers: dict[tuple[Any, ...], dict[str, Any]] = {}
    for unit in units:
        command = _parsed_locator(unit).get("command")
        if not isinstance(command, dict) or command.get("kind") != "snbt":
            continue
        inner = command.get("json")
        if not isinstance(inner, str):
            continue
        key = (tuple(command.get("snbt_path") or ()), command.get("snbt_index"))
        entry = containers.setdefault(key, {"command": command, "writable": set()})
        entry["writable"].add(inner)

    issues: list[AuditIssue] = []
    for entry in containers.values():
        command = entry["command"]
        before_span = _resolve_snbt_span(input_line, command)
        after_span = _resolve_snbt_span(output_line, command)
        if before_span is None or after_span is None:
            continue
        before = component_document(before_span[4])
        if before is None:
            continue
        after = component_document(after_span[4])
        if after is None:
            issues.append(
                AuditIssue(
                    "error",
                    "structure",
                    rel,
                    f"{label}：JSON 编码组件在写回后无法解析",
                    {"before": before_span[4], "after": after_span[4]},
                )
            )
            continue
        writable = frozenset(entry["writable"])
        if json_skeleton(before, writable=writable) != json_skeleton(after, writable=writable):
            issues.append(
                AuditIssue(
                    "error",
                    "structure",
                    rel,
                    f"{label}：JSON 编码组件的非文本字段被改写（样式 / 事件 / 结构 / 数值）",
                    {"before": before_span[4], "after": after_span[4]},
                )
            )
    return issues


def _payload_skeleton_issues(
    rel: str,
    line_no: int,
    input_line: str,
    output_line: str,
    writable: frozenset[str] = frozenset(),
) -> list[Any]:
    """The line's JSON payload stays parseable and keeps its structure."""

    from .audit import AuditIssue

    before_state, before = _payload_state(input_line)
    if before_state != "json":
        # SNBT payloads and already-broken input lines are out of scope here.
        return []
    after_state, after = _payload_state(output_line)
    if after_state == "json" and json_skeleton(before, writable=writable, fold_text=True) == json_skeleton(
        after, writable=writable, fold_text=True
    ):
        return []
    message = "JSON 载荷在写回后无法解析" if after_state == "broken" else "JSON 载荷结构被改写（键 / 数组长度 / 事件属性）"
    if after_state == "none":
        message = "JSON 载荷在写回后消失"
    return [
        AuditIssue(
            "error",
            "structure",
            rel,
            f"第 {line_no + 1} 行：{message}",
            {"line": line_no + 1, "before": input_line, "after": output_line},
        )
    ]


def _payload_state(line: str) -> tuple[str, Any | None]:
    """``("json", value)`` / ``("broken", None)`` / ``("none", None)`` for the display payload."""

    index = _find_execute_run(line)
    tail = line[index:] if index is not None else line
    match = _COMMAND_PAYLOAD_RE.match(tail)
    remainder = tail[match.end():] if match else tail
    start = _find_json_start(remainder)
    if start < 0:
        return ("none", None)
    end = _matching_json_end(remainder, start)
    if end is None:
        return ("none", None)
    region = remainder[start:end]
    if not _JSON_PAYLOAD_HEAD_RE.match(region):
        # Unquoted keys mean SNBT: not this rule's business (residue covers bytes).
        return ("none", None)
    try:
        return ("json", json.loads(region))
    except (TypeError, ValueError):
        return ("broken", None)


def function_call_arguments(line: str) -> dict[tuple[str, str], str]:
    """``(function id, argument) -> value`` for a trailing ``function … {…}`` call."""

    match = FUNCTION_CALL_RE.search(line)
    if match is None:
        return {}
    function_id, block = match.group(1), match.group(2)
    entries, parsed_ok = _iter_snbt_values(block)
    if not parsed_ok:
        return {}
    arguments: dict[tuple[str, str], str] = {}
    for path, start, end, quoted in entries:
        if len(path) != 1:
            continue
        raw = block[start:end]
        arguments[(function_id, str(path[0]))] = _snbt_unquote(raw) if quoted else raw
    return arguments


def _unit_line(unit: TextUnit) -> int | None:
    try:
        locator = json.loads(unit.locator)
    except (TypeError, ValueError):
        return None
    if not isinstance(locator, dict) or "line" not in locator:
        return None
    try:
        return int(locator["line"])
    except (TypeError, ValueError):
        return None


def _masked_line(line: str, units: list[TextUnit]) -> str | None:
    """Replace every declared span with a marker; ``None`` when a span cannot be resolved.

    ``snbt`` spans resolve structurally on both sides (path + sibling index), so
    the same locator works before and after the write.  The other locator kinds
    describe one payload at the end of a command: masking through end-of-line on
    both sides keeps the comparison symmetric without tracking shifted offsets.
    """

    spans: list[tuple[int, int]] = []
    for unit in units:
        try:
            locator = json.loads(unit.locator)
        except (TypeError, ValueError):
            return None
        if not isinstance(locator, dict):
            return None
        command = locator.get("command")
        if not isinstance(command, dict):
            return None
        kind = command.get("kind")
        if kind == "snbt":
            span = _resolve_snbt_span(line, command)
            if span is None:
                return None
            spans.append((int(span[0]), int(span[1])))
        elif kind == "plain":
            try:
                start = int(command["start"])
            except (KeyError, TypeError, ValueError):
                return None
            spans.append((start, len(line)))
        else:
            try:
                start = int(command["json_start"])
            except (KeyError, TypeError, ValueError):
                return None
            spans.append((start, len(line)))
    masked = line
    for start, end in reversed(_merged_spans(spans)):
        if not (0 <= start <= end <= len(masked)):
            return None
        masked = masked[:start] + "\x00UNIT\x00" + masked[end:]
    return masked


def _merged_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Union of spans, so one payload carrying several units is masked once.

    Two units can declare the *same* span (one quoted component string with two
    JSON text leaves).  Masking that interval twice shifts every later offset and
    reports a legitimate translation as damage — that alone produced 52 false
    positives on a real map.
    """

    merged: list[list[int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]
