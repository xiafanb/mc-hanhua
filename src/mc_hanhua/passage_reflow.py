"""B v1.4: redistribute prose line breaks inside one tellraw passage (DEVELOPMENT §3.13).

The per-component translation of a sentence split across coloured components
often breaks a line between two hanzi.  B moves those breaks, inside one passage,
to sentence ends, clause marks and colour boundaries, keeping the passage's row
count, its fixed edges and every character in its own component.  It only
*proposes*: its output becomes a passage revision record that the unified
passage gate (``passage_revisions.adopt_passage_revisions``) re-proves from the
current input before anything is written.

Ported verbatim from the frozen experiment (``.local``, sha256 ddec9dde0d23);
only the glyph-width source and the map wrap width moved from globals to the
bundled width table and a parameter.  Any change here must bump
``REFLOW_VERSION``.
"""

from __future__ import annotations

import bisect
import json
import re
import sys
import unicodedata
from functools import lru_cache
from pathlib import Path

from .text_components import apply_command_text, extract_command_texts

REFLOW_VERSION = "1.4"
CHAT_WIDTH = 320  # default chat width in GUI pixels
WIDTH_ASSET = Path("assets") / "glyph-widths.json"


@lru_cache(maxsize=1)
def _width_table() -> tuple[list[int], list[int], list[int], int]:
    path = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2])) / WIDTH_ASSET
    data = json.loads(path.read_text(encoding="utf-8"))
    runs = data["runs"]
    return [r[0] for r in runs], [r[1] for r in runs], [r[2] for r in runs], int(data["default"])


def glyph_width(ch: str) -> int:
    """Advance of one character in the vanilla default font, in GUI pixels."""

    starts, ends, widths, default = _width_table()
    cp = ord(ch)
    index = bisect.bisect_right(starts, cp) - 1
    return widths[index] if index >= 0 and cp <= ends[index] else default


def text_width(text: str) -> int:
    return sum(glyph_width(ch) for ch in text)


STYLE_KEYS = ("color", "bold", "italic", "underlined", "strikethrough", "obfuscated", "font", "hoverEvent", "hover_event", "insertion")
NON_TEXT = ("score", "selector", "nbt", "keybind", "translate")
PUNCT_AFTER = set("，。！？；：、…）」』”’,.!?;:")
NO_LINE_START = set("，。！？；：、…）」』”’,.!?;:%")
NO_LINE_END = set("（「『“‘(")
HAN = re.compile(r"[㐀-鿿豈-﫿]")
BREAK = re.compile(r"[ \t]*\n[ \t]*")
DIVIDER = re.compile(r"\n[ \t]*={3,}[ \t]*$")
_DEC = json.JSONDecoder()
# v1.3 break preference (weights only order the legal points; they never add new ones)
SENTENCE_END = set("。！？!?")  # v1.3.1: "……" is often a translation placeholder, not a sentence end
CLOSERS = set("」』”’）)")
CLAUSE_MARKS = set("，、；：,;:…")
W_CLAUSE, W_BOUNDARY, W_PATTERN, W_SHORT, W_BALANCE = 1000.0, 1500.0, 800.0, 600.0, 100.0


def _visible(chars) -> int:
    return sum(1 for c in chars if not c.isspace())


class Refused(Exception):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code, self.detail = code, detail


def _asc(ch: str) -> bool:
    return ch.isascii() and ch.isalnum()


def edges(text: str) -> tuple[str, str, str]:
    """(lead, core, trail): lead/trail kept only when they carry a newline or a divider row."""
    lead = re.match(r"\s*", text).group()
    core, trail = text[len(lead):], ""
    while True:
        m = re.search(r"\s+$", core)
        if m:
            trail, core = m.group() + trail, core[: m.start()]
        m = DIVIDER.search(core)
        if m:
            trail, core = m.group() + trail, core[: m.start()]
            continue
        break
    if "\n" not in lead:
        core, lead = lead + core, ""
    if "\n" not in trail and "=" not in trail:
        core, trail = core + trail, ""
    return lead, core, trail


def payload(line: str, start: int | None = None):
    """(json_start, array) of the tellraw payload.

    The start is located once on the SOURCE line with the product's command
    locator; the target is read at the same offset (callers verify the prefix).
    """
    if start is None:
        leaves = extract_command_texts(line)
        starts = {loc.get("json_start") for loc, _ in leaves if loc.get("kind") == "json"}
        if len(starts) != 1 or "tellraw" not in line:
            raise Refused("out_of_scope", "not a single tellraw JSON payload")
        start = starts.pop()
    try:
        arr, _ = _DEC.raw_decode(line, start)
    except ValueError as exc:
        raise Refused("not_json", str(exc)) from None
    if not isinstance(arr, list):
        raise Refused("out_of_scope", "payload is not an array")
    return start, arr


def _mask(value):
    if isinstance(value, dict):
        return {k: ("<t>" if k == "text" and isinstance(v, str) else _mask(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask(v) for v in value]
    return value


def is_prose(comp) -> bool:
    return (isinstance(comp, dict) and isinstance(comp.get("text"), str) and "extra" not in comp
            and "clickEvent" not in comp and "click_event" not in comp and not any(k in comp for k in NON_TEXT))


def passages(src_arr):
    """Index runs of prose components bounded by the SOURCE's fixed edges."""
    runs, cur = [], []
    for i, comp in enumerate(src_arr):
        if not is_prose(comp):
            if cur:
                runs.append(cur)
            cur = []
            continue
        lead, core, trail = edges(comp["text"])
        if lead and cur:
            runs.append(cur)
            cur = []
        if core.strip():
            cur.append(i)
        if trail:
            if cur:
                runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    return [r for r in runs if r]


def bad_positions(stream, owner, cuts, styles) -> list[int]:
    """Positions of ``bad_breaks`` entries, same order."""
    out = []
    for p in cuts:
        a, b = stream[p - 1], stream[p]
        edge = owner[p - 1] != owner[p] and styles[owner[p - 1]] != styles[owner[p]]
        if (HAN.match(a) and HAN.match(b) and not edge) or b in NO_LINE_START or a in NO_LINE_END or (_asc(a) and _asc(b) and not edge):
            out.append(p)
    return out


HARD_ENDS = set("。！？!?…")
LABEL_OR_BULLET = re.compile(r"^\s*([A-Z][\w' /&().-]{0,40}:(\s|$)|[-•►*>]|\d+[.)]\s)")


def classify_breaks(src_rows, wrap_width: float | None) -> list[str]:
    """v1.4: each source row break is a soft wrap or an intentional break.

    soft: hyphenated word, next row continues in lower case, or the next word would
    not fit the map's wrap width. hard: it would fit and the row ends a sentence or
    label, the next row is a label/bullet, or the row is less than half full.
    Anything else stays soft (the v1.3.2 behaviour).
    """
    rows = [r.strip() for r in src_rows]
    if wrap_width is None:
        return ["soft"] * (len(rows) - 1)
    out = []
    for a, b in zip(rows, rows[1:]):
        if not a or not b or a.endswith("-") or b[:1].islower():
            out.append("soft")
            continue
        first = b.split(" ")[0]
        if text_width(a) + text_width(" " + first) > wrap_width + 4:
            out.append("soft")
        elif re.search(r"[.!?:]$", a) and not re.search(r"\.\s*\.\s*\.$", a):
            out.append("hard")
        elif LABEL_OR_BULLET.match(b) or text_width(a) < 0.5 * wrap_width:
            out.append("hard")
        else:
            out.append("soft")
    return out


def bad_breaks(stream, owner, cuts, styles) -> list[str]:
    """Forbidden breaks among ``cuts`` (break before stream[p]); a style boundary is a phrase edge."""
    bad = []
    for p in cuts:
        a, b = stream[p - 1], stream[p]
        edge = owner[p - 1] != owner[p] and styles[owner[p - 1]] != styles[owner[p]]
        ctx = f"「{''.join(stream[max(0, p - 2):p])}｜{''.join(stream[p:p + 2])}」"
        if HAN.match(a) and HAN.match(b) and not edge:
            bad.append("汉字之间" + ctx)
        elif b in NO_LINE_START:
            bad.append("标点行首" + ctx)
        elif a in NO_LINE_END:
            bad.append("开括号行尾" + ctx)
        elif _asc(a) and _asc(b) and not edge:
            bad.append("英文词内" + ctx)
    return bad


def _style(comp):
    return {k: comp.get(k) for k in STYLE_KEYS}


def reflow_line(src_line: str, tgt_line: str, wrap_width: float | None, *, fill_missing: bool = False) -> tuple[str, list[dict]]:
    """Return (new_line, per-passage records).  new_line == tgt_line when nothing applies.

    Raises Refused for line-level refusals; passage-level refusals are recorded and
    leave that passage byte-for-byte as it was.
    """
    s_start, src = payload(src_line)
    if tgt_line[:s_start] != src_line[:s_start]:
        raise Refused("prefix_mismatch", "command prefix differs between source and target")
    t_start, tgt = payload(tgt_line, s_start)
    if len(src) != len(tgt) or [_mask(c) for c in src] != [_mask(c) for c in tgt]:
        raise Refused("skeleton_mismatch")
    records, line = [], tgt_line
    for run in passages(src):
        rec = {"components": run}
        try:
            line = _reflow_passage(src, tgt, run, line, t_start, rec, wrap_width, fill_missing)
        except Refused as exc:
            rec.update(status="refused", code=exc.code, detail=exc.detail)
        records.append(rec)
    return line, records


def _reflow_passage(src, tgt, run, line, t_start, rec, wrap_width, fill_missing=False):
    base = _style(src[run[0]])
    anchors = {i for i in run if _style(src[i]) != base}
    writable = {i for i in run if tgt[i]["text"] != src[i]["text"]}
    parts, src_rows_text = {}, ""
    for i in run:
        s_lead, s_core, s_trail = edges(src[i]["text"])
        t = tgt[i]["text"]
        # Fixed edges are defined by the SOURCE; a break B placed on a component
        # boundary is prose, not an edge, so the target is cut by the source's shape.
        t_lead = s_lead if s_lead and t.startswith(s_lead) else ("" if not s_lead else None)
        t_trail = ""
        if s_trail:
            m = re.search(r"[ \t]*" + re.escape(s_trail.lstrip(" \t")) + r"$", t)
            t_trail = m.group() if m else None
        if t_lead is None or t_trail is None or len(t_lead) + len(t_trail) > len(t):
            raise Refused("fixed_edge_mismatch", f"component {i}")
        t_core = t[len(t_lead): len(t) - len(t_trail)]
        if re.search(r"\n[ \t]*\n", t_core) or re.search(r"\n[ \t]*={3,}", t_core):
            raise Refused("internal_fixed_row", f"component {i}")
        parts[i] = (t_lead, t_core, t_trail)
        src_rows_text += s_core
    src_rows = src_rows_text.split("\n")
    src_breaks = len(src_rows) - 1
    tgt_breaks = sum(len(BREAK.findall(parts[i][1])) for i in run)
    if src_breaks == 0:
        rec.update(status="no_trigger", reason="single-row passage")
        return line
    # fill_missing: a break-free target (a whole-sentence translation) gets the source's break count.
    if tgt_breaks != src_breaks and not (fill_missing and tgt_breaks == 0):
        raise Refused("break_count_mismatch", f"source {src_breaks}, target {tgt_breaks}")
    styles = {i: _style(src[i]) for i in run}
    stream, owner, a_cuts = [], [], []
    for i in run:
        pieces = BREAK.split(parts[i][1])
        for k, piece in enumerate(pieces):
            if k:
                if stream and piece and _asc(stream[-1]) and _asc(piece[0]) and owner[-1] == i:
                    stream.append(" ")
                    owner.append(i)
                a_cuts.append(len(stream))
            for ch in piece:
                stream.append(ch)
                owner.append(i)
    rows = [r.strip() for r in "".join(parts[i][1] for i in run).split("\n")]
    raw_cuts = list(a_cuts)
    a_cuts = sorted({p for p in a_cuts if 0 < p < len(stream)})
    n = len(stream)
    # v1.4: the source's intentional breaks (list items, labels, separate sentences)
    kinds = classify_breaks(src_rows, wrap_width)
    hard_fixed, kept_as_item = [], set()
    if tgt_breaks == src_breaks and "hard" in kinds:
        bad_at = set(bad_positions(stream, owner, a_cuts, styles))
        hard_a = [(k, raw_cuts[k]) for k, kind in enumerate(kinds) if kind == "hard" and 0 < raw_cuts[k] < n]
        for j, (k, pos) in enumerate(hard_a):
            if pos not in bad_at:
                hard_fixed.append(pos)
                continue
            # Only a source SENTENCE end may move to a Chinese sentence end; a label,
            # list item or short row keeps A's own break (A broke between items).
            sentence = re.search(r"([.!?]|…)$", src_rows[k].strip())  # an ellipsis ends an utterance too
            lo = hard_a[j - 1][1] if j else 0
            hi = hard_a[j + 1][1] if j + 1 < len(hard_a) else n
            ends = [p for p in range(lo + 1, hi) if stream[p - 1] in HARD_ENDS and stream[p] not in NO_LINE_START and p not in hard_fixed] if sentence else []
            if ends:
                hard_fixed.append(min(ends, key=lambda p: abs(p - pos)))
            elif not sentence:
                hard_fixed.append(pos)  # a label / list item: A already broke between items
                kept_as_item.add(pos)
            # else: a sentence break with no Chinese sentence end nearby stays a
            # defect and is handled as a soft break (the v1.3.2 behaviour).
    rec["break_kinds"] = kinds
    bad = [b for p, b in zip(bad_positions(stream, owner, a_cuts, styles), bad_breaks(stream, owner, a_cuts, styles)) if p not in kept_as_item]
    if tgt_breaks == src_breaks and not bad:
        rec.update(status="no_trigger")
        return line
    rec["before_bad"] = bad
    bounds = [0, *a_cuts, len(stream)]
    punct_rows = [(x, y) for x, y in zip(bounds, bounds[1:])
                  if "".join(stream[x:y]).strip()
                  and all(unicodedata.category(c).startswith("P") or c.isspace() for c in stream[x:y])]
    for i in run:
        if i not in writable and BREAK.search(parts[i][1]):
            raise Refused("locked_leaf_break", f"component {i}")
    n = len(stream)

    def slot(p):
        """Where a break before stream[p] would be written, or None."""
        a, b = stream[p - 1], stream[p]
        forced = p in kept_as_item  # v1.4: A's own item break, kept exactly where it is
        if not forced and (b in NO_LINE_START or a in NO_LINE_END or b == " "):
            return None
        kind = "item" if forced else None
        if kind:
            pass
        elif a in PUNCT_AFTER:
            kind = "punct"
        elif owner[p - 1] != owner[p] and styles[owner[p - 1]] != styles[owner[p]]:
            kind = "anchor"
        elif (a.isascii() and a.isalpha() and HAN.match(b)) or (HAN.match(a) and b.isascii() and b.isalpha()) or (a == " " and not b.isdigit()):
            # Latin words only: a digit binds to its classifier/unit (各3个, 50卢比).
            kind = "script"
        if kind is None:
            return None
        if owner[p - 1] == owner[p]:
            return (owner[p], "inside") if owner[p] in writable else None
        prev, nxt = owner[p - 1], owner[p]
        for leaf, where in ((prev, "end"), (nxt, "start")):
            if leaf in writable and leaf not in anchors:
                return (leaf, where)
        for leaf, where in ((prev, "end"), (nxt, "start")):
            if leaf in writable:
                return (leaf, where)
        return None

    points = [p for p in range(1, n) if slot(p)]
    need = src_breaks
    if len(points) < need:
        raise Refused("no_layout", f"{len(points)} legal points, need {need}")
    widths = [text_width(r.strip()) for r in src_rows]
    prefix = [0]
    for ch in stream:
        prefix.append(prefix[-1] + glyph_width(ch))
    total = prefix[-1]
    targets = [w / max(1, sum(widths)) * total for w in widths]
    cand = [0, *points, n]
    INF = float("inf")
    mean = total / (need + 1)

    def placeholder_ellipsis(p):
        """v1.3.2: "进入……|【下水道】" — the translation drew "……" where the English verb
        waits for its object in the next coloured component; the English has no ellipsis."""
        if stream[p - 1] != "…" or owner[p - 1] == owner[p] or styles[owner[p - 1]] == styles[owner[p]]:
            return False
        en = edges(src[owner[p - 1]]["text"])[1].rstrip()
        return not re.search(r"(\.\s*\.\s*\.|…)$", en)

    def cut_cost(p):
        """v1.3: sentence end < clause mark < colour/script boundary; greetings stay whole."""
        if p >= n:
            return 0.0
        a = stream[p - 1]
        if placeholder_ellipsis(p):
            return W_BOUNDARY + W_PATTERN
        if a in SENTENCE_END or (a in CLOSERS and p >= 2 and stream[p - 2] in SENTENCE_END):
            cost = 0.0
        elif a in CLAUSE_MARKS:
            cost = W_CLAUSE
        else:
            cost = W_BOUNDARY
        if a in CLAUSE_MARKS:
            # "你好，|访客！": the clause after the mark is a few characters closing the sentence.
            q = p
            while q < n and stream[q] not in PUNCT_AFTER:
                q += 1
            if q < n and stream[q] in SENTENCE_END and _visible(stream[p:q]) <= 4:
                cost += W_PATTERN
        return cost

    def row_cost(x, y, k):
        text = "".join(stream[x:y]).strip()
        cost = 0.0
        if _visible(text) <= 3:
            cost += W_SHORT
        if owner[x:y] and len(set(owner[x:y])) == 1 and owner[x] in anchors and (x == 0 or owner[x - 1] != owner[x]) and (y == n or owner[y] != owner[x]):
            cost += W_PATTERN  # a colour component alone on its row
        if re.fullmatch(r"[A-Za-z][A-Za-z' .-]*[,.!?，。！？]?", text):
            cost += W_PATTERN  # a name alone on its row
        w = prefix[y] - prefix[x]
        return cost + W_BALANCE * ((w - targets[k - 1]) / max(1.0, mean)) ** 2

    # Keep A's punctuation-only rows whole when a layout can (the v1.2 refusal still
    # applies when it cannot): a row may not swallow one of their boundaries.
    required = sorted({x for x, _y in punct_rows if x > 0} | {y for _x, y in punct_rows if y < n} | set(hard_fixed))

    def solve(req):
        best = {(0, 0): (0.0, None)}
        for k in range(1, need + 2):
            for j in range(1, len(cand)):
                if (k == need + 1) != (j == len(cand) - 1):
                    continue
                for i in range(j):
                    prev = best.get((k - 1, i))
                    if prev is None:
                        continue
                    w = prefix[cand[j]] - prefix[cand[i]]
                    if w <= 0 or w > CHAT_WIDTH:
                        continue
                    if any(cand[i] < b < cand[j] for b in req):
                        continue
                    c = prev[0] + row_cost(cand[i], cand[j], k) + cut_cost(cand[j])
                    if c < best.get((k, j), (INF,))[0]:
                        best[(k, j)] = (c, i)
        return best

    best = solve(required)
    end = (need + 1, len(cand) - 1)
    if end not in best:
        if required and end in solve([]):
            raise Refused("punct_row_merge", "punctuation-only row cannot stay whole")
        raise Refused("no_layout", "no arrangement within chat width")
    cuts, node = [], end
    while node[0] > 1:
        _, i = best[node]
        cuts.append(cand[i])
        node = (node[0] - 1, i)
    cuts.sort()
    # v1.2: a row that is only punctuation in A (an English mark kept after a name,
    # or a mark the translation drew on its own row) must stay its own row; merging
    # it shows doubled marks such as "?？" or ". . .……".  Refuse rather than merge.
    for x, y in punct_rows:
        if (x and x not in cuts) or (y < len(stream) and y not in cuts):
            raise Refused("punct_row_merge", "".join(stream[x:y]).strip())
    # rebuild component cores
    new_core = {i: [] for i in run}
    starts = {i: False for i in run}
    for p, ch in enumerate(stream):
        if p in cuts:
            leaf, where = slot(p)
            if where == "inside":
                new_core[leaf].append("\n")
            elif where == "end":
                new_core[leaf].append("\n")
            else:
                starts[leaf] = True
        new_core[owner[p]].append(ch)
    texts = {}
    for i in run:
        core = ("\n" if starts[i] else "") + "".join(new_core[i])
        lead, _old, trail = parts[i]
        texts[i] = lead + core + trail
    out_rows = [r for r in "".join(texts[i][len(parts[i][0]):len(texts[i]) - len(parts[i][2]) or None] for i in run).split("\n")]
    after_bad = [b for p, b in zip(bad_positions(stream, owner, cuts, styles), bad_breaks(stream, owner, cuts, styles)) if p not in kept_as_item]
    if after_bad:
        raise Refused("gate_fail", "forbidden break produced: " + "、".join(after_bad))
    for i in run:
        if texts[i] != tgt[i]["text"]:
            if i not in writable:
                raise Refused("locked_leaf_break", f"component {i}")
            line = apply_command_text(line, {"kind": "json", "json_start": t_start, "pointer": f"/{i}/text"}, texts[i])
    rec.update(status="applied", before_rows=rows, after_rows=[r.strip() for r in out_rows])
    return line


def reflow(src_line: str, tgt_line: str, wrap_width: float | None, *, fill_missing: bool = False) -> tuple[str, list[dict], str | None]:
    """``(line, records, refusal)``: B's proposal for one line; ``tgt_line`` unchanged when refused."""

    try:
        out, records = reflow_line(src_line, tgt_line, wrap_width, fill_missing=fill_missing)
    except Refused as exc:
        return tgt_line, [{"status": "refused", "code": exc.code, "detail": exc.detail}], exc.code
    if out == tgt_line:
        return tgt_line, records, None
    again, _records = reflow_line(src_line, out, wrap_width)
    if again != out:
        return tgt_line, records + [{"status": "refused", "code": "not_idempotent"}], "not_idempotent"
    return out, records, None


def map_wrap_width(lines) -> float | None:
    """The map author's wrap width (90th percentile of non-last source rows), or None when too few rows."""

    widths = []
    for line in lines:
        try:
            _start, arr = payload(line)
        except Refused:
            continue
        for run in passages(arr):
            rows = "".join(edges(arr[i]["text"])[1] for i in run).split(chr(10))
            widths += [text_width(r.strip()) for r in rows[:-1]]
    if len(widths) < 20:
        return None
    widths.sort()
    return widths[int(len(widths) * 0.9)]
