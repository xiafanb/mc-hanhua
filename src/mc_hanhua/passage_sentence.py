"""Whole-sentence translation of split passages ("D", DEVELOPMENT §3.14).

Scope: one sentence split over several members of one tellraw passage (§3.12)
and translated member by member.  D translates the passage text once and cuts
the reply back into the same members; every result is only a proposal for the
unified passage gate.

What decides how a reply is cut is structure, never colour:

* a member is an *anchor* when its text is fixed (protected or untranslated
  span: kept verbatim) or when its whole text is a name of the map name config
  (that name's translation is injected as a term and located in the reply);
* every other member is translated with the sentence.  When one gap between
  anchors holds more than one such member (also plain, unstyled splits), the
  members are tagged so the reply says where each one ends.

The map name config is data (``load_name_config``): this module holds no
map-specific names.  Raw replies are cached in the translation memory (input
namespace), so a rerun reproduces every decision without a request.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import Counter
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .glossary import Glossary
from .models import GlossaryEntry, TextUnit
from .passage_layout import PASSAGE_METADATA, divider_rows, fixed_edges, passages, tellraw_array
from .policy import should_send_to_ai
from .task_records import unit_key

SENTENCE_VERSION = "6"
# Raw replies depend only on the request text and its terms, not on how D cuts them.
REQUEST_CACHE_VERSION = "1"
PROVIDER = "sentence"
NAME_CONFIG_FORMAT = "mc-hanhua-passage-names"
_MEMORY_PREFIX = "\x00passage-sentence\x00"
_FULL = {"?": "？", "!": "！", ".": "。", ",": "，", ":": "：", ";": "；"}
_PUNCT_ONLY = re.compile(r"^[\s\W_]*$")
_PUNCT_EDGE = re.compile(r"^([\s\W_]*)(.*?)([\s\W_]*)$", re.S)
_TAG = re.compile(r"<\s*x(\d+)\s*>(.*?)<\s*/\s*x\1\s*>", re.S)
_ANY_TAG = re.compile(r"<\s*/?\s*x\d+\s*>")
# A soft break continues a sentence only on positive evidence: speaker labels and
# titles also end without punctuation.
_SENTENCE_END = re.compile(r"""[.!?…:;"')\]】」]\s*$|\.\s*\.\s*\.\s*$""")
_CONTINUES = re.compile(
    r"(?i)\b(the|a|an|of|to|in|on|at|for|with|from|into|your|my|his|her|their|our|its|and|or|but|that|this|these|those"
    r"|than|as|by|is|are|was|were|be|will|would|can|could|should|must|have|has|had)$")
_LEAD_PUNCT = re.compile(r"^[.!?](?:[.!?\s]*[.!?])?\s*")
_NO_ROW_START = set("，。！？；：、…）」』”’,.!?;:%")
_CJK_COMMA = re.compile(r"(?<=[㐀-鿿豈-﫿]),(?!\d)")


@dataclass(frozen=True)
class NameConfig:
    """Map-specific confirmed names: English as written in the source -> Chinese."""

    names: dict[str, str]
    input_fingerprint: str | None = None
    label: str = ""

    def applies_to(self, input_fingerprint: str) -> bool:
        """A config bound to one input (``input_fingerprint``) never applies to another."""

        return self.input_fingerprint in (None, "", input_fingerprint)


def load_name_config(path: Path) -> NameConfig:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("format") != NAME_CONFIG_FORMAT or not isinstance(data.get("names"), dict):
        raise ValueError(f"not a {NAME_CONFIG_FORMAT} file: {path}")
    names = {str(k): str(v) for k, v in data["names"].items() if str(k).strip() and str(v).strip()}
    return NameConfig(names, data.get("input_fingerprint"), str(data.get("map", "")))


def sentence_provider(names: NameConfig | None) -> str:
    """Provider recorded with each D passage: D's version and the name config it used."""

    payload = json.dumps(sorted((names.names if names is not None else {}).items()), ensure_ascii=False)
    return f"{PROVIDER}:{SENTENCE_VERSION}:{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:12]}"


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _nospace(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _norm_name(text: str) -> str:
    return _flat(text).strip(",.!?;:")


def _join_break_free(text: str) -> str:
    """A reply never carries breaks into a member: B places them afterwards."""

    return re.sub(r"(?<=[A-Za-z0-9])\s*\n\s*(?=[A-Za-z0-9])", " ", text).replace("\r", "").replace("\n", "")


@dataclass
class _Member:
    idx: int
    text: str
    lead: str
    core: str
    trail: str
    unit: TextUnit | None
    writable: bool
    kind: str = "free"          # free | name | locked | fixed
    term: str = ""
    outer_punct: str = ""
    tagged: bool = False
    # Sentence-final punctuation opening a passage closes the text before it
    # (e.g. after a player name): kept out of the request and re-attached.
    lead_punct: str = ""

    @property
    def request_core(self) -> str:
        return _flat(self.core[len(self.lead_punct):])


@dataclass
class _Passage:
    file_path: str
    line_no: int
    json_start: int
    source_line: str
    target_line: str
    members: list[_Member]
    outcome: dict[str, object] = field(default_factory=dict)
    # A soft-break chain: the passages (member index lists) translated together.
    runs: list[list[int]] | None = None


def _blank_prose(component: object) -> bool:
    return (isinstance(component, dict) and isinstance(component.get("text"), str) and not component["text"].strip()
            and not any(key in component for key in ("extra", "clickEvent", "click_event", "score", "selector", "nbt", "keybind", "translate")))


def soft_chains(components: list) -> list[list[list[int]]]:
    """Adjacent passages of one tellraw array that carry one sentence across a soft break.

    Two passages join when only a single line break on a component edge lies
    between them (no blank row, divider, clickable or dynamic component) and the
    first one shows the sentence goes on: it ends with a comma or a function
    word, or the next one starts lower-case.  Passage edges stay where they are;
    the chain only lets D translate the passages in one request.
    """

    runs = passages(components)
    chains: list[list[list[int]]] = []
    current = runs[:1]
    for first, second in zip(runs, runs[1:]):
        prev_text, next_text = components[first[-1]]["text"], components[second[0]]["text"]
        prev_core, next_core = fixed_edges(prev_text)[1].strip(), fixed_edges(next_text)[1].strip()
        between = components[first[-1] + 1: second[0]]
        # The edges between the two passages as the source fixes them (a divider row belongs to an edge).
        gap = fixed_edges(prev_text)[2] + "".join(c["text"] for c in between if isinstance(c, dict) and isinstance(c.get("text"), str)) \
            + fixed_edges(next_text)[0]
        joins = (
            all(_blank_prose(c) for c in between) and gap.count("\n") == 1 and "=" not in gap
            and re.search("[A-Za-z]", prev_core) is not None and not _SENTENCE_END.search(prev_core)
            and (re.match(r"[a-z]", next_core) is not None or prev_core.endswith(",") or _CONTINUES.search(prev_core) is not None)
        )
        if joins:
            current.append(second)
            continue
        if len(current) > 1:
            chains.append(current)
        current = [second]
    if len(current) > 1:
        chains.append(current)
    return chains


def _split_by_spans(members: list[_Member], content: str, spans: list[tuple[int, int, int]]) -> tuple[dict[int, str] | None, str | None]:
    shares: dict[int, str] = {}
    cursor, prev = 0, -1
    pending = ""
    for position, start, end in spans + [(len(members), len(content), len(content))]:
        owners = [i for i in range(prev + 1, position) if not (members[i].kind in ("name", "locked") or members[i].tagged)]
        region = content[cursor:start]
        if not owners:
            if re.sub(r"[\s，。,.!！?？、]", "", region):
                # The reply moved words outside every member (Chinese word order):
                # they join the adjacent ordinary member, previous first, in order.
                # Names and fixed members never take them.
                if prev >= 0 and members[prev].kind == "free":
                    shares[prev] += region
                elif position < len(members) and members[position].kind == "free":
                    pending = region
                else:
                    return None, "orphan_text"
        elif len(owners) > 1:
            return None, "multi_owner_gap"
        elif not region.strip():
            return None, "empty_share"
        else:
            shares[owners[0]] = region
        if position < len(members):
            shares[position] = pending + content[start:end]
            pending = ""
        cursor, prev = end, position
    return shares, None


class _Requests:
    """Raw MT replies through the input-scoped memory; new requests within a budget."""

    def __init__(self, translator, memory, budget: int | None) -> None:
        self.translator = translator
        self.memory = memory
        self.budget = budget
        self.sent = 0
        self.cache_hits = 0
        self.lock = threading.Lock()
        self._key_locks: dict[tuple[str, str], threading.Lock] = {}

    def ask(self, text: str, glossary: Glossary, kind: str) -> str | None:
        terms = self.translator._relevant_terms(text, glossary)
        context = f"passage-sentence:{REQUEST_CACHE_VERSION}:{kind}:" + hashlib.sha256(
            json.dumps(terms, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
        key = _MEMORY_PREFIX + text
        with self.lock:
            key_lock = self._key_locks.setdefault((key, context), threading.Lock())
        # One request per distinct text: identical passages elsewhere wait for
        # it, so a rerun (served from memory) sees the reply every copy used.
        with key_lock:
            cached = self.memory.get(key, context) if self.memory is not None else None
            if cached is not None:
                with self.lock:
                    self.cache_hits += 1
                return cached
            with self.lock:
                if self.budget is not None and self.sent >= self.budget:
                    raise _BudgetExhausted
                self.sent += 1
            reply = self.translator._request_mt_text(text, glossary, timeout=90)
            if not reply or not reply[0].strip():
                return None
            if self.memory is not None:
                self.memory.put(key, reply[0], context, "passage-sentence")
            return reply[0]


class _BudgetExhausted(Exception):
    pass


def _classify(members: list[_Member], names: dict[str, str]) -> None:
    for member in members:
        lettered = re.search(r"[A-Za-z]", member.core) is not None
        # A member the per-component result kept as written (player names,
        # untranslated spans) is fixed text for D as well.
        kept = member.unit is None or member.unit.final_target in (None, member.unit.source_text)
        if not member.writable or (lettered and kept):
            member.kind = "locked" if lettered else "fixed"
            member.term = _flat(member.core)
            continue
        lead_p, name_part, trail_p = _PUNCT_EDGE.match(_flat(member.core)).groups()
        target = names.get(_norm_name(name_part)) if name_part else None
        if target is not None:
            member.kind, member.term, member.outer_punct = "name", target, trail_p.strip()


def _glossary(base: Glossary, names: dict[str, str], members: list[_Member]) -> Glossary:
    entries = [GlossaryEntry(en, zh, note="map name config", priority=800) for en, zh in names.items()]
    for member in members:
        if member.kind == "locked":
            core = _PUNCT_EDGE.match(member.term).group(2) or member.term
            entries.append(GlossaryEntry(core, core, note="fixed member", priority=950))
    return base.merge(Glossary(entries))


def _choose_tags(members: list[_Member]) -> None:
    """Tag anchors, then enough free members that each gap keeps at most one untagged owner."""

    for member in members:
        member.tagged = member.kind in ("name", "locked")
    gap: list[_Member] = []
    for member in members + [None]:
        if member is not None and not member.tagged:
            gap.append(member)
            continue
        free = [m for m in gap if m.kind == "free"]
        fixed = [m for m in gap if m.kind == "fixed"]
        if fixed:
            for m in free:
                m.tagged = True
        elif len(free) > 1:
            keep = max(free, key=lambda m: len(m.core))
            for m in free:
                m.tagged = m is not keep
        gap = []


def _b_mode_possible(members: list[_Member]) -> bool:
    gap = 0
    for member in members:
        if member.kind in ("name", "locked"):
            if len(_nospace(member.term)) <= 1:
                return False
            gap = 0
        else:
            gap += 1
            if gap > 1:
                return False
    return True


def _try_b(passage: _Passage, joined: str, glossary: Glossary, requests: _Requests) -> tuple[dict[int, str] | None, str | None, str]:
    from .translator import _locate_unique

    reply = requests.ask(joined, glossary, "plain")
    if reply is None:
        return None, "mt_failed", ""
    members = passage.members
    spans, cursor = [], 0
    for i, member in enumerate(members):
        if member.kind not in ("name", "locked"):
            continue
        found = _locate_unique(reply, _nospace(member.term))
        if found is None:
            return None, "anchor_missing", reply
        if found[0] < cursor:
            return None, "anchor_order", reply
        start, end = found
        if member.outer_punct:
            nxt = reply[end:end + len(member.outer_punct)]
            if _nospace(nxt) and all(_FULL.get(a, a) == _FULL.get(b, b) for a, b in zip(member.outer_punct, nxt)):
                end += len(nxt)
        spans.append((i, start, end))
        cursor = end
    shares, why = _split_by_spans(members, reply, spans)
    return shares, why, reply


def _try_c(passage: _Passage, glossary: Glossary, requests: _Requests, order_ref: str) -> tuple[dict[int, str] | None, str | None]:
    members = passage.members
    tagged = _flat("".join(_request_piece(m, f"<x{i}>{m.request_core}</x{i}>" if m.tagged else None) for i, m in enumerate(members)))
    ids = [i for i, m in enumerate(members) if m.tagged]
    if len(ids) >= 2 and not order_ref:
        order_ref = requests.ask(_plain_request(members), glossary, "plain") or ""
    reply = requests.ask(tagged, glossary, "tagged")
    if reply is None:
        return None, "mt_failed"
    found = list(_TAG.finditer(reply))
    if sorted(int(m.group(1)) for m in found) != ids or _ANY_TAG.search(_TAG.sub("", reply)):
        return None, "tags_lost_or_duplicated"
    if [int(m.group(1)) for m in found] != ids:
        return None, "anchor_order"
    for m in found:
        member = members[int(m.group(1))]
        if not m.group(2).strip():
            return None, "empty_anchor"
        if member.kind in ("name", "locked") and _nospace(m.group(2).strip("，。！？,.!?：:")) != _nospace(member.term.strip("，。！？,.!?：:")):
            return None, "term_not_kept"
    if order_ref:
        pos = [(order_ref.find(_nospace(m.group(2))), int(m.group(1))) for m in found if len(_nospace(m.group(2))) > 1]
        pos = [p for p in pos if p[0] >= 0]
        if len(pos) >= 2 and [i for _p, i in sorted(pos)] != [i for _p, i in pos]:
            return None, "order_conflict_with_untagged"
    clean, spans, last = "", [], 0
    for m in found:
        clean += reply[last:m.start()]
        start = len(clean)
        clean += m.group(2)
        spans.append((int(m.group(1)), start, len(clean)))
        last = m.end()
    clean += reply[last:]
    return _split_by_spans(members, clean, spans)


def _full_punct(text: str) -> str:
    marks = "".join(ch for ch in text if not ch.isspace())
    if len(marks) >= 3 and set(marks) == {"."}:
        return "……"
    return "".join(_FULL.get(ch, ch) for ch in marks)


def _mark_lead_punct(members: list[_Member], first: list[int]) -> None:
    """The first member of each passage keeps a sentence end it opens with."""

    for member in members:
        if member.idx in first and member.kind == "free":
            match = _LEAD_PUNCT.match(member.core)
            if match and re.search("[A-Za-z]", member.core[match.end():]):
                member.lead_punct = match.group()


def _request_piece(member: _Member, body: str | None = None) -> str:
    """One member in a request: its core only.  Fixed edges (line breaks, divider rows)
    never reach the model; they are re-attached from the source afterwards."""

    lead = " " if member.text[:1].isspace() else ""
    tail = " " if member.text[-1:].isspace() else ""
    return lead + (member.request_core if body is None else body) + tail


def _plain_request(members: list[_Member]) -> str:
    return _flat("".join(_request_piece(m) for m in members))


def _member_texts(passage: _Passage, shares: dict[int, str]) -> tuple[dict[int, str] | None, str | None]:
    texts: dict[int, str] = {}
    previous = ""
    for i, member in enumerate(passage.members):
        share = shares.get(i)
        if share is None:
            if member.writable:
                return None, "empty_share"
            previous = member.core
            continue
        share = _join_break_free(share)
        if member.kind == "fixed":
            if not _PUNCT_ONLY.match(share):
                return None, "fixed_member_needs_text"
            previous = member.core
            continue
        if member.kind == "locked":
            if _nospace(share) not in (_nospace(member.core), _nospace(member.term)):
                return None, "fixed_member_changed"
            previous = member.core
            continue
        if member.kind == "name" and member.outer_punct and not re.search(r"[\W_]$", share.strip()):
            share = share.rstrip() + "".join(_FULL.get(ch, ch) for ch in member.outer_punct)
        if member.lead_punct and not re.match(r"[\W_]", share.strip()):
            share = _full_punct(member.lead_punct) + share.strip()
        if not share.strip():
            return None, "empty_share"
        # A half-width comma right after a Chinese character becomes full-width
        # (also across the member boundary).
        share = _CJK_COMMA.sub("，", previous[-1:] + share.strip())[len(previous[-1:]):]
        previous = share
        texts[member.idx] = member.lead + share + member.trail
    return texts, None


def _layout(passage: _Passage, texts: dict[int, str], wrap_width) -> tuple[dict[int, str] | None, str | None]:
    from .passage_reflow import reflow
    from .text_components import apply_command_text

    line = passage.target_line
    for idx in sorted(texts, reverse=True):
        line = apply_command_text(line, {"kind": "json", "json_start": passage.json_start, "pointer": f"/{idx}/text"}, texts[idx])
    out, records, refusal = reflow(passage.source_line, line, wrap_width, fill_missing=True)
    if refusal is not None:
        return None, f"layout:{refusal}"
    cores = {m.idx: m.core for m in passage.members}
    for run in passage.runs or [[m.idx for m in passage.members]]:
        record = next((r for r in records if r.get("components") == run), None)
        if record is None or record.get("status") == "refused":
            return None, f"layout:{(record or {}).get('code', 'passage_not_found')}"
        if record.get("status") != "applied" and any("\n" in cores[idx] for idx in run):
            return None, "layout:no_layout_applied"
    value, _end = json.JSONDecoder().raw_decode(out, passage.json_start)
    return {idx: value[idx]["text"] for idx in texts}, None


def _translate_passage(passage: _Passage, base: Glossary, names: dict[str, str], requests: _Requests, wrap_width, redrawn: set[str]) -> None:
    members = passage.members
    # Members are shared between a chain attempt and the single-passage retry.
    for member in members:
        member.tagged = False
    glossary = _glossary(base, names, members)
    joined = _plain_request(members)
    outcome = passage.outcome
    fails: list[str] = []
    shares = None
    try:
        if _b_mode_possible(members) and any(m.kind in ("name", "locked") for m in members):
            outcome["mode"] = "B"
            shares, why, reply = _try_b(passage, joined, glossary, requests)
            if shares is None:
                fails.append(f"B:{why}")
                if why != "mt_failed":
                    _choose_tags(members)
                    outcome["mode"] = "C"
                    shares, why = _try_c(passage, glossary, requests, reply)
                    if shares is None:
                        fails.append(f"C:{why}")
        else:
            outcome["mode"] = "C"
            _choose_tags(members)
            shares, why = _try_c(passage, glossary, requests, "")
            if shares is None:
                fails.append(f"C:{why}")
    except _BudgetExhausted:
        outcome.update(status="budget_exhausted")
        return
    if shares is None:
        outcome.update(status="fallback", reason=fails[-1], fails=fails)
        return
    texts, why = _member_texts(passage, shares)
    if texts is None:
        outcome.update(status="fallback", reason=why, fails=fails + [why])
        return
    if redrawn and redrawn & set("".join(texts.values())):
        outcome.update(status="fallback", reason="font_override", fails=fails + ["font_override"])
        return
    laid, why = _layout(passage, texts, wrap_width)
    if laid is None:
        outcome.update(status="fallback", reason=why, fails=fails + [why])
        return
    if passage.runs and _chain_row_starts_with_punctuation(passage, laid):
        # The reply put punctuation after a fixed break: the row would start with
        # it.  Moving it across the passage edge is not allowed; the chain falls back.
        outcome.update(status="fallback", reason="chain_leading_punctuation", fails=fails + ["chain_leading_punctuation"])
        return
    outcome.update(status="proposed", texts=laid, fails=fails)


def _chain_row_starts_with_punctuation(passage: _Passage, laid: dict[int, str]) -> bool:
    by_idx = {m.idx: m for m in passage.members}
    for run in (passage.runs or [])[1:]:
        first = by_idx[run[0]]
        text = laid.get(first.idx, first.text)
        body = text[len(first.lead):].lstrip()
        if body[:1] in _NO_ROW_START:
            return True
    return False


def propose_sentence_records(
    root: Path,
    units: list[TextUnit],
    commands: Iterable[tuple[str, str]],
    translator,
    glossary: Glossary,
    names: NameConfig | None,
    memory,
    *,
    font_overrides=None,
    blocked_keys: set[str] | frozenset[str] = frozenset(),
    budget: int | None = None,
    max_workers: int = 4,
) -> tuple[list[dict[str, object]], dict[str, object], list[dict[str, object]]]:
    """``(records, stats, outcomes)`` for every split passage of the root archive.

    A passage qualifies when it holds two or more members with Latin letters
    and at least one writable member, and none of its members is bound already.
    Soft-break chains (:func:`soft_chains`) are tried first as one request; a
    chain that fails leaves its passages to the single-passage attempt.  The
    records of one chain carry ``chain: True`` and are adopted or rolled back
    together (the chain itself is recomputed from the input at adoption).
    """

    from .passage_reflow import map_wrap_width
    from .text_components import apply_command_text, command_locator_offset

    stats: Counter = Counter()
    if not hasattr(translator, "_request_mt_text") or not hasattr(translator, "_relevant_terms"):
        return [], {"enabled": True, "available": False, "reason": "translator_without_whole_text_mt"}, []
    name_map = dict(names.names) if names is not None else {}
    wrap = map_wrap_width(line for rel, line in commands if rel.endswith(".mcfunction"))
    redrawn = set(getattr(font_overrides, "chars", None) or ())
    by_line: dict[tuple[str, int], list[TextUnit]] = {}
    for unit in units:
        if unit.format == "mcfunction":
            by_line.setdefault((unit.file_path, int(json.loads(unit.locator)["line"])), []).append(unit)
    file_cache: dict[str, list[str] | None] = {}
    singles: list[_Passage] = []
    chained: list[_Passage] = []
    for (file_path, line_no), line_units in sorted(by_line.items()):
        if file_path not in file_cache:
            try:
                file_cache[file_path] = (root / file_path).read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                file_cache[file_path] = None
        lines = file_cache[file_path]
        if lines is None or not 0 <= line_no < len(lines):
            continue
        source_line = lines[line_no]
        parsed = tellraw_array(source_line)
        if parsed is None:
            continue
        json_start, components = parsed
        slots: dict[int, TextUnit] = {}
        for unit in line_units:
            match = re.fullmatch(r"/(\d+)/text", str(json.loads(unit.locator)["command"].get("pointer") or ""))
            if match:
                slots[int(match.group(1))] = unit
        target_line = source_line
        for unit in sorted(line_units, key=lambda u: command_locator_offset(json.loads(u.locator)["command"]), reverse=True):
            if unit.final_target not in (None, unit.source_text):
                target_line = apply_command_text(target_line, json.loads(unit.locator)["command"], unit.final_target)

        def build(run: list[int]) -> tuple[list[_Member], str | None]:
            members = []
            for idx in run:
                text = components[idx]["text"]
                lead, core, trail = fixed_edges(text)
                unit = slots.get(idx)
                members.append(_Member(idx, text, lead, core, trail, unit, unit is not None and should_send_to_ai(unit.policy)))
            if any(m.unit is not None and PASSAGE_METADATA in m.unit.metadata for m in members):
                return members, "skipped_bound"
            if any(divider_rows(m.core) for m in members):
                # A divider row inside a member is layout, not prose: never part of a sentence.
                return members, "skipped_divider_in_member"
            keys = {unit_key([], m.unit.file_path, m.unit.format, m.unit.locator, m.unit.source_text) for m in members if m.writable}
            if keys & set(blocked_keys):
                return members, "skipped_manual_revision"
            _classify(members, name_map)
            return members, None

        built = {tuple(run): build(run) for run in passages(components)}
        for chain in soft_chains(components):
            parts = [built[tuple(run)] for run in chain]
            members = [m for part, _why in parts for m in part]
            stats["chains_considered"] += 1
            if any(why for _part, why in parts) or not any(m.writable and m.kind in ("free", "name") for m in members):
                stats["chains_skipped"] += 1
                continue
            _mark_lead_punct(members, [run[0] for run in chain])
            chained.append(_Passage(file_path, line_no, json_start, source_line, target_line, members, runs=[list(run) for run in chain]))
        for run, (members, why) in built.items():
            if sum(re.search(r"[A-Za-z]", m.core) is not None for m in members) < 2:
                continue
            stats["passages_considered"] += 1
            if not any(m.writable for m in members):
                stats["skipped_no_writable_member"] += 1
                continue
            if why:
                stats[why] += 1
                continue
            if not any(m.kind in ("free", "name") for m in members):
                stats["skipped_nothing_to_translate"] += 1
                continue
            _mark_lead_punct(members, [run[0]])
            singles.append(_Passage(file_path, line_no, json_start, source_line, target_line, members))
    requests = _Requests(translator, memory, budget)
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        list(pool.map(lambda p: _translate_passage(p, glossary, name_map, requests, wrap, redrawn), chained))
    covered = {(p.file_path, p.line_no, tuple(run)) for p in chained if p.outcome.get("status") == "proposed" for run in p.runs}
    singles = [p for p in singles if (p.file_path, p.line_no, tuple(m.idx for m in p.members)) not in covered]
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        list(pool.map(lambda p: _translate_passage(p, glossary, name_map, requests, wrap, redrawn), singles))
    records: list[dict[str, object]] = []
    outcomes: list[dict[str, object]] = []
    for passage in chained + singles:
        outcome = passage.outcome
        prefix = "chain:" if passage.runs else ""
        status = outcome.get("status", "fallback")
        stats[prefix + f"mode:{outcome.get('mode', '-')}"] += 1
        stats[prefix + (status if status != "fallback" else f"fallback:{outcome.get('reason')}")] += 1
        row = {"file": passage.file_path, "line": passage.line_no + 1, "members": [m.idx for m in passage.members],
               "kinds": [m.kind for m in passage.members], **({"chain": passage.runs} if passage.runs else {}),
               **{k: v for k, v in outcome.items() if k != "texts"}}
        outcomes.append(row)
        if status != "proposed":
            continue
        by_idx = {m.idx: m for m in passage.members}
        for run in passage.runs or [[m.idx for m in passage.members]]:
            record: dict[str, object] = {"members": {}, "sentence_version": SENTENCE_VERSION}
            if passage.runs:
                record["chain"] = True
            for idx in run:
                member = by_idx[idx]
                if member.writable:
                    key = unit_key([], member.unit.file_path, member.unit.format, member.unit.locator, member.unit.source_text)
                    record["members"][key] = outcome["texts"].get(idx, member.unit.final_target or member.unit.source_text)
            if record["members"]:
                records.append(record)
    stats["passages_proposed"] = len(records)
    result = {"enabled": True, "available": True, "version": SENTENCE_VERSION, "names": len(name_map),
              "names_label": names.label if names is not None else "", "requests": requests.sent,
              "cache_hits": requests.cache_hits, **dict(stats)}
    return records, result, outcomes
