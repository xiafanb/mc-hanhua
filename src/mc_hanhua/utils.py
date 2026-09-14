from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

PLACEHOLDER_RE = re.compile(
    r"(%\d+\$[sdif]|%[sdif]|%s|\{[A-Za-z0-9_.:-]+\}|\$\{[^}]+\}|§[0-9A-FK-ORa-fk-or]|\\n)"
)
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


def preserve_placeholders(source: str, target: str) -> str:
    """Return target only when placeholders survived intact."""
    source_tokens = PLACEHOLDER_RE.findall(source)
    if not source_tokens:
        return target
    missing = [token for token in source_tokens if token not in target]
    if missing:
        return ""
    return target


def restore_boundary_newlines(source: str, target: str) -> str:
    """Restore only line-break boundaries stripped from structured text components."""

    if not target:
        return target
    leading = re.match(r"^[ \t\r\n]*", source)
    trailing = re.search(r"[ \t\r\n]*$", source)
    prefix = "".join(char for char in (leading.group(0) if leading else "") if char in "\r\n")
    suffix = "".join(char for char in (trailing.group(0) if trailing else "") if char in "\r\n")
    return prefix + target + suffix


def normalize_layout_newlines(source: str, target: str) -> str:
    """Restore escaped internal breaks for map text that was originally multiline.

    This deliberately applies only when the source contains a real line break.
    Callers further restrict it to sign and TextDisplay layout text so literal
    ``\\n`` tokens in commands and ordinary language resources remain intact.
    """

    if "\n" not in source or "\\n" not in target:
        return target
    return target.replace("\\r\\n", "\n").replace("\\n", "\n")


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
