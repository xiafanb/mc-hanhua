from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

# Reading a text file can fail in more ways than "cannot parse JSON":
# a file that is not UTF-8 (macOS AppleDouble metadata, an archive packed on a
# platform with a legacy codepage) raises UnicodeDecodeError, which is a
# *sibling* of json.JSONDecodeError under ValueError — catching the latter does
# not catch it. Every caller that treats "unreadable input" as "nothing to
# extract" must catch this tuple, so the decision lives in one place.
UNREADABLE_TEXT_ERRORS = (OSError, ValueError)

# One token set for everything that must survive a translation verbatim.
# Additions must stay INSIDE the capture group: ``quality._tokens`` used to
# read ``findall`` results, and an alternative placed outside the group yields
# an empty string per match, i.e. a gate that silently passes.
#
# The escape and macro alternatives are grammar-driven and ordered from most to
# least specific.  Two reasons they exist:
#   * translation must never rewrite non-linguistic syntax (fossil-frights
#     shipped `$(name)` translated to `$(名称)`, which broke every macro call);
#   * the SNBT encoder cannot re-encode a spelling it does not fully decode —
#     `\ue451` is a code point to the parser, while the re-encoded `\\ue451` is
#     plain text.  Incomplete escapes are therefore protected, never rewritten.
PLACEHOLDER_RE = re.compile(
    r"("
    r"%\d+\$[sdif]|%[sdif]|%s|\{[A-Za-z0-9_.:-]+\}|\$\{[^}]+\}"
    r"|§[0-9A-FK-ORa-fk-or]"
    # mcfunction macros: `$$(...)` is the escaped literal, `$(name)` a variable.
    r"|\$\$\([^)]*\)|\$\([A-Za-z_][A-Za-z0-9_]*\)"
    # Escape sequences: named, complete unicode/byte, then incomplete prefixes.
    r"|\\N\{[^}]*\}"
    r"|\\u[0-9A-Fa-f]{4}|\\U[0-9A-Fa-f]{8}|\\x[0-9A-Fa-f]{2}"
    r"|\\u[0-9A-Fa-f]{0,3}|\\x[0-9A-Fa-f]{0,1}"
    r"|\\[nrtbf'\"\\]|\\[A-Za-z]"
    r")"
)
# A Java conversion in its two spellings.  ``%s`` is "the next argument",
# ``%1$s`` is "the argument at this index"; Minecraft formats a language entry
# with Java's ``String.format``, so the two are the same message when the index
# names the argument the plain conversion would have taken.  Kept beside
# ``PLACEHOLDER_RE`` because they describe the same token: the gate and the
# coarse ``preserve_placeholders`` filter both need this grammar.
PLAIN_CONVERSION_RE = re.compile(r"%[sdif]")
INDEXED_CONVERSION_RE = re.compile(r"%(\d+)\$([sdif])")
CONVERSION_RE = re.compile(r"%(\d+)\$([sdif])|%([sdif])")
# A whole-string target selector (@e[tag=...], @a, ...) is mechanism syntax,
# never display text.
ENTITY_SELECTOR_RE = re.compile(r"@[praesn](\[[^\]]*\])?")
# Single alphanumeric tokens containing a digit (Piston07, log1, book9b) are
# identifiers, tags or player names, never prose.
DIGIT_TOKEN_RE = re.compile(r"[A-Za-z0-9_]*\d[A-Za-z0-9_]*")


def stable_id(*parts: str) -> str:
    h = hashlib.sha1()
    for part in parts:
        h.update(part.encode("utf-8", errors="surrogatepass"))
        h.update(b"\0")
    return h.hexdigest()


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="")


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8-sig") as f:
        return json.load(f)


def is_os_metadata_path(path: Path) -> bool:
    """True for operating-system bookkeeping entries, never game content.

    Zips packed on macOS carry an ``__MACOSX/`` tree of AppleDouble resource
    forks (``._name``) whose bytes are not text. Minecraft never reads them, so
    scanning them yields no text — only spurious "unparseable NBT/JSON"
    warnings (a real map produced 207). They are still copied through to the
    output unchanged; this only excludes them from extraction and auditing.
    """

    if "__MACOSX" in path.parts:
        return True
    name = path.name
    return name.startswith("._") or name == ".DS_Store"


def dump_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")


def looks_translatable(value: str) -> bool:
    stripped = value.strip()
    if len(stripped) < 2:
        return False
    if is_visual_glyph_art(stripped):
        return False
    if stripped.startswith(("http://", "https://", "minecraft:", "#")):
        return False
    if ENTITY_SELECTOR_RE.fullmatch(stripped):
        return False
    if DIGIT_TOKEN_RE.fullmatch(stripped):
        return False
    # Strip sentence-ending punctuation before the ID heuristic so single
    # words like "Beware." are not mistaken for dotted resource ids.
    id_candidate = stripped.lower().rstrip(".!?,;:")
    if re.fullmatch(r"[a-z0-9_.:/-]+", id_candidate) and any(sep in id_candidate for sep in "._:/-"):
        return False
    placeholder_free = PLACEHOLDER_RE.sub("", stripped).strip()
    if not placeholder_free:
        return False
    ascii_or_cyrillic = sum(1 for ch in placeholder_free if ("A" <= ch <= "Z") or ("a" <= ch <= "z") or ("\u0400" <= ch <= "\u04ff"))
    cjk = sum(1 for ch in stripped if "\u4e00" <= ch <= "\u9fff")
    if cjk and ascii_or_cyrillic == 0:
        return False
    if cjk > max(12, ascii_or_cyrillic * 2):
        return False
    return any(ch.isalpha() for ch in placeholder_free)


def is_visual_glyph_art(value: str) -> bool:
    """Identify custom-font grids that are visual assets rather than language."""

    visible = [char for char in value if not char.isspace()]
    if not visible:
        return False
    private_use = sum(
        1
        for char in visible
        if ("\ue000" <= char <= "\uf8ff") or ("\U000f0000" <= char <= "\U000ffffd") or ("\U00100000" <= char <= "\U0010fffd")
    )
    letters = sum(1 for char in visible if char.isalpha() and not ("\ue000" <= char <= "\uf8ff"))
    return private_use >= 3 and (private_use >= letters or ("\n" in value and private_use * 2 >= letters))


def detect_risk(text: str, context: str = "") -> str:
    high_markers = ("command", "tellraw", "title", "execute", "function")
    if any(marker in context.lower() for marker in high_markers):
        return "high"
    if PLACEHOLDER_RE.search(text):
        return "medium"
    return "low"


def indexed_conversions_equivalent(source: str, target: str) -> bool:
    """True when *target* spells the source's plain conversions by argument index.

    Minecraft formats a language entry with Java's ``String.format``: ``%s``
    means "the next argument" and ``%1$s`` means "argument 1".  The official
    Simplified-Chinese resources use the indexed spelling wherever the English
    repeats the plain one, and they use it to *reorder* — 104 of the 227 indexed
    strings in the bundled Mojang baseline are not in index order
    (``Block %s does not accept '%s' for %s property`` becomes
    ``%1$s的%3$s属性不能被设为“%2$s”``), because that is how a translation puts the
    arguments in Chinese word order while still rendering the same message.

    So the proof is about the *mapping*, not the target's word order:

    * every conversion in the source is plain — an indexed source is already a
      claim about argument order and is compared exactly, as before, because
      which argument an index names cannot be recovered from the text alone;
    * the target carries exactly as many conversions as the source arguments,
      all indexed, each index in ``1..n`` and used exactly once — equal count
      plus unique in-range indices is a permutation, so nothing is dropped or
      duplicated and no argument is invented;
    * the ``i``-th source conversion's type is the type the target gives argument
      ``i``, wherever that conversion sits in the target's own word order — a
      ``%d`` may not come back as ``%s``;
    * every other token is compared by the caller (``placeholder_tokens_match``
      for placeholders, the exact counters for URLs, commands and ``§`` codes),
      which is what keeps selectors, macros, escapes and formatting codes exact.
    """

    source_plain = [match.group(0)[1:] for match in PLAIN_CONVERSION_RE.finditer(source)]
    if not source_plain or INDEXED_CONVERSION_RE.search(source):
        return False
    target_conversions = [(match.group(1), match.group(2) or match.group(3)) for match in CONVERSION_RE.finditer(target)]
    if len(target_conversions) != len(source_plain):
        return False
    seen: set[int] = set()
    for index, kind in target_conversions:
        if index is None:
            return False
        position = int(index)
        if position < 1 or position > len(source_plain) or position in seen:
            return False
        if kind != source_plain[position - 1]:
            return False
        seen.add(position)
    return True


def _non_conversion_placeholders(tokens: Counter[str]) -> Counter[str]:
    """The placeholder multiset without the conversions, which have their own proof."""

    return Counter({token: count for token, count in tokens.items() if not CONVERSION_RE.fullmatch(token)})


def placeholder_tokens_match(source: str, target: str) -> bool:
    """Token-layer equality for placeholders, with the one proven rewrite.

    Reads whole matches (never ``findall`` groups) because the gate compares
    multisets: a duplicated or dropped placeholder is as unsafe as an invented
    one.  ``{name}``, ``${…}``, macro calls, escapes and ``§`` codes must match
    exactly; conversions agree either exactly or through
    ``indexed_conversions_equivalent``.
    """

    source_tokens = Counter(match.group(0) for match in PLACEHOLDER_RE.finditer(source))
    target_tokens = Counter(match.group(0) for match in PLACEHOLDER_RE.finditer(target))
    if source_tokens == target_tokens:
        return True
    if not indexed_conversions_equivalent(source, target):
        return False
    return _non_conversion_placeholders(source_tokens) == _non_conversion_placeholders(target_tokens)


def preserve_placeholders(source: str, target: str) -> str:
    """Return target only when placeholders survived intact.

    The coarse pre-filter in front of the token gate: every source placeholder
    must still be spelled in the candidate.  An indexed conversion is a spelling
    of the plain conversion it replaces (``indexed_conversions_equivalent``), so
    an official-style ``%1$s`` is not read as a missing ``%s``.
    """

    source_tokens = PLACEHOLDER_RE.findall(source)
    if not source_tokens:
        return target
    missing = [token for token in source_tokens if token not in target]
    if not missing:
        return target
    if any(not PLAIN_CONVERSION_RE.fullmatch(token) for token in missing):
        return ""
    if not indexed_conversions_equivalent(source, target):
        return ""
    return target


def restore_boundary_newlines(source: str, target: str) -> str:
    """Restore only line-break boundaries stripped from structured text components.

    Only a layout container needs this: nothing else rebuilds a sign page's
    leading/trailing newlines.  A span whose breaks are reflowed by the shared
    candidate repair (``quality.repair_layout_candidate``) must not be passed
    through here — the repair already gives it the source's exact break count and
    blank-line positions, and prepending the boundaries a reply already carries
    doubles that run and makes the token gate refuse a reply the reuse gate
    shapes.
    """

    if not target:
        return target
    leading = re.match(r"^[ \t\r\n]*", source)
    trailing = re.search(r"[ \t\r\n]*$", source)
    prefix = "".join(char for char in (leading.group(0) if leading else "") if char in "\r\n")
    suffix = "".join(char for char in (trailing.group(0) if trailing else "") if char in "\r\n")
    return prefix + target + suffix


_EDGE_CODE = r"(?:\s|§[0-9a-fk-orA-FK-OR])"
_LEADING_EDGE_RE = re.compile(rf"^{_EDGE_CODE}+")
_TRAILING_EDGE_RE = re.compile(rf"{_EDGE_CODE}+$")


def peel_text_edges(text: str) -> tuple[str, str, str]:
    """Split ``text`` into ``(leading, core, trailing)`` edges of whitespace and ``§`` codes.

    Whole-edge text (no core) is returned unpeeled so it keeps its old handling.
    """

    leading = _LEADING_EDGE_RE.match(text)
    head = leading.group(0) if leading else ""
    rest = text[len(head) :]
    trailing = _TRAILING_EDGE_RE.search(rest)
    tail = trailing.group(0) if trailing else ""
    core = rest[: len(rest) - len(tail)]
    if not core.strip():
        return "", text, ""
    return head, core, tail


def reattach_text_edges(leading: str, trailing: str, reply: str) -> str:
    """Put the exact original edges around a model reply that only saw the core.

    A reply that echoed the formatting codes of an edge is not given them twice.
    """

    body = reply.strip()
    head = leading.strip()
    if head and body.startswith(head):
        body = body[len(head) :].lstrip()
    tail = trailing.strip()
    if tail and body.endswith(tail):
        body = body[: len(body) - len(tail)].rstrip()
    return leading + body + trailing


def normalize_layout_newlines(source: str, target: str) -> str:
    """Restore escaped internal breaks for map text that was originally multiline.

    This deliberately applies only when the source contains a real line break.
    Callers further restrict it to sign and TextDisplay layout text so literal
    ``\\n`` tokens in commands and ordinary language resources remain intact.
    """

    if "\n" not in source or "\\n" not in target:
        return target
    return target.replace("\\r\\n", "\n").replace("\\n", "\n")


def restore_line_padding(source: str, target: str) -> str:
    """Give back alignment padding (>=2 leading spaces) a translated line lost.

    Only applies when both texts have the same number of lines; lines that
    already carry at least the source's padding are left untouched.
    """

    if "\n" not in source or "\n" not in target:
        return target
    source_lines = source.split("\n")
    target_lines = target.split("\n")
    if len(source_lines) != len(target_lines):
        return target
    changed = False
    for index, (src, dst) in enumerate(zip(source_lines, target_lines)):
        pad = len(src) - len(src.lstrip(" "))
        if pad < 2 or not src.strip() or not dst.strip():
            continue
        have = len(dst) - len(dst.lstrip(" "))
        if have < pad:
            target_lines[index] = " " * pad + dst.lstrip(" ")
            changed = True
    return "\n".join(target_lines) if changed else target


_RESIDUE_RE = re.compile(r"(?<![A-Za-z])([a-z]{2,})(?=[一-鿿])|(?<=[一-鿿])([a-z]{2,})(?![A-Za-z])")
_RESIDUE_SKIP = frozenset("kg cm mm km hp xp fps id ui ms mb gb kb hz px pm am".split())
_MARKUP_RE = re.compile(r"§[0-9a-fk-orA-FK-OR]|\{[^{}]*\}|%(?:\d+\$)?[sdf]|https?://\S+")


def source_residue_fragments(source: str, target: str) -> list[str]:
    """Lowercase Latin fragments glued to CJK in the target that come from the source.

    A hint for review only (e.g. an untranslated word left stuck to a translation).
    """

    if source.lstrip().startswith("/") or not re.search(r"[一-鿿]", target):
        return []
    clean_source = _MARKUP_RE.sub(" ", source).lower()
    clean_target = _MARKUP_RE.sub(" ", target)
    found: list[str] = []
    for match in _RESIDUE_RE.finditer(clean_target):
        word = match.group(1) or match.group(2)
        if word in _RESIDUE_SKIP or word in found:
            continue
        if re.search(rf"(?<![a-z]){re.escape(word)}|{re.escape(word)}(?![a-z])", clean_source) and word in clean_source:
            found.append(word)
    return found


def looks_like_chinese_translation(text: str) -> bool:
    if not text.strip():
        return False
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    kana = sum(1 for ch in text if "\u3040" <= ch <= "\u30ff")
    letters = sum(1 for ch in text if ch.isalpha())
    if cjk == 0:
        return False
    if kana > max(2, cjk // 3):
        return False
    if letters and cjk / max(1, letters) < 0.1:
        return False
    return True
