from __future__ import annotations

import io
import json
import os
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

_PAGE_RE = re.compile(r"(?:^|/)assets/[^/]+/textures/font/unicode_page_([0-9a-fA-F]{2})\.png$", re.IGNORECASE)
_FONT_JSON_RE = re.compile(r"(?:^|/)assets/[^/]+/font/.+\.json$", re.IGNORECASE)
_PACK_SUFFIXES = (".zip", ".jar")
_MAX_NESTED_DEPTH = 3
_MAX_NESTED_BYTES = 256 * 1024 * 1024
_MAX_JSON_BYTES = 8 * 1024 * 1024
# Packs often copy vanilla font definitions to restyle letters; those are not icons.
_VANILLA_FONT_TEXTURES = frozenset(
    {"ascii.png", "accented.png", "nonlatin_european.png", "ascii_sga.png", "asciillager.png"}
)
# Characters this common are the likeliest to be reached for by a translator.
_EXAMPLES_PER_PAGE = ("首", "香", "馆", "饭", "饮", "饰", "驾", "骑", "验", "高", "鱼", "鸟", "鼓", "黑", "龙")


@dataclass(frozen=True)
class FontOverrides:
    pages: frozenset[int] = frozenset()
    chars: frozenset[str] = frozenset()

    def __bool__(self) -> bool:
        return bool(self.pages or self.chars)

    def __contains__(self, char: object) -> bool:
        if not isinstance(char, str) or len(char) != 1:
            return False
        return char in self.chars or (ord(char) >> 8) in self.pages

    def union(self, other: FontOverrides) -> FontOverrides:
        return FontOverrides(self.pages | other.pages, self.chars | other.chars)

    def pattern(self) -> re.Pattern[str] | None:
        if not self:
            return None
        parts = [f"\\u{page:02x}00-\\u{page:02x}ff" for page in sorted(self.pages)]
        parts.extend(re.escape(char) for char in sorted(self.chars) if (ord(char) >> 8) not in self.pages)
        return re.compile("[" + "".join(parts) + "]+")

    def find(self, text: str) -> set[str]:
        return {char for char in text if char in self}

    def new_in(self, source: str, target: str) -> set[str]:
        """Overridden chars the target uses that the source did not."""

        if not self or not target:
            return set()
        return self.find(target) - self.find(source)

    def only_icons(self, text: str) -> bool:
        pattern = self.pattern()
        if pattern is None or not text.strip():
            return False
        rest = re.sub(r"§[0-9A-FK-ORa-fk-or]", "", pattern.sub("", text))
        return not rest.strip()

    def describe(self) -> str:
        parts = [f"page {page:02X} (U+{page:02X}00–{page:02X}FF)" for page in sorted(self.pages)]
        loose = sorted(char for char in self.chars if (ord(char) >> 8) not in self.pages)
        if loose:
            shown = "".join(loose[:20])
            parts.append(f"字符 {shown}" + (f" 等 {len(loose)} 个" if len(loose) > 20 else ""))
        return "；".join(parts)

    def prompt_hint(self) -> str:
        if not self:
            return ""
        hints = []
        for page in sorted(self.pages):
            examples = "、".join(c for c in _EXAMPLES_PER_PAGE if (ord(c) >> 8) == page)
            if not examples:
                examples = "、".join(chr((page << 8) + n) for n in range(0x11, 0x16) if chr((page << 8) + n).isprintable())
            hints.append(f"U+{page:02X}00–U+{page:02X}FF（如 {examples}）")
        loose = sorted(char for char in self.chars if (ord(char) >> 8) not in self.pages)
        if loose:
            hints.append("字符 " + "".join(loose[:12]))
        return "地图资源包把以下字符重绘为图标，译文不要使用它们，改用同义词：" + "；".join(hints) + "。"


def _scan_page(match: re.Match[str], pages: set[int]) -> None:
    page = int(match.group(1), 16)
    if page:
        pages.add(page)


def _scan_font_json(data: bytes, chars: set[str]) -> None:
    try:
        doc = json.loads(data.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError):
        return
    providers = doc.get("providers") if isinstance(doc, dict) else None
    if not isinstance(providers, list):
        return
    for provider in providers:
        if not isinstance(provider, dict) or provider.get("type") != "bitmap":
            continue
        texture = str(provider.get("file", "")).rsplit("/", 1)[-1].lower()
        if texture in _VANILLA_FONT_TEXTURES:
            continue
        rows = provider.get("chars")
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, str):
                chars.update(ch for ch in row if not ch.isspace() and ord(ch) >= 0xA0)


def _scan_zip(zf: zipfile.ZipFile, pages: set[int], chars: set[str], depth: int) -> None:
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename
        lower = name.lower()
        match = _PAGE_RE.search(name)
        if match:
            _scan_page(match, pages)
        elif _FONT_JSON_RE.search(name):
            if info.file_size <= _MAX_JSON_BYTES:
                _scan_font_json(zf.read(info), chars)
        elif depth < _MAX_NESTED_DEPTH and lower.endswith(_PACK_SUFFIXES) and info.file_size <= _MAX_NESTED_BYTES:
            try:
                with zipfile.ZipFile(io.BytesIO(zf.read(info))) as inner:
                    _scan_zip(inner, pages, chars, depth + 1)
            except (zipfile.BadZipFile, OSError, RuntimeError):
                continue


def scan_font_overrides(root: Path) -> FontOverrides:
    """Collect characters a resource pack under ``root`` redraws as glyphs."""

    pages: set[int] = set()
    chars: set[str] = set()
    root = Path(root)
    if root.is_file():
        candidates = [root]
        directories: list[Path] = []
    else:
        candidates = []
        directories = [root]
    for directory in directories:
        for current, _dirs, files in os.walk(directory):
            for file_name in files:
                path = Path(current) / file_name
                rel = path.relative_to(directory).as_posix()
                match = _PAGE_RE.search(rel)
                if match:
                    _scan_page(match, pages)
                elif _FONT_JSON_RE.search(rel):
                    try:
                        if path.stat().st_size <= _MAX_JSON_BYTES:
                            _scan_font_json(path.read_bytes(), chars)
                    except OSError:
                        continue
                elif file_name.lower().endswith(_PACK_SUFFIXES):
                    candidates.append(path)
    for path in candidates:
        try:
            with zipfile.ZipFile(path) as zf:
                _scan_zip(zf, pages, chars, 1)
        except (zipfile.BadZipFile, OSError, RuntimeError):
            continue
    return _drop_font_restyles(pages, chars)


_CJK_PAGES = range(0x4E, 0xA0)
# Icon packs redraw a page or two; more than this is a whole-font restyle, not icons.
_RESTYLE_PAGE_LIMIT = 8
_RESTYLE_CHAR_LIMIT = 512


def _drop_font_restyles(pages: set[int], chars: set[str]) -> FontOverrides:
    if sum(1 for page in pages if page in _CJK_PAGES) > _RESTYLE_PAGE_LIMIT:
        pages = {page for page in pages if page not in _CJK_PAGES}
    if sum(1 for char in chars if (ord(char) >> 8) in _CJK_PAGES) > _RESTYLE_CHAR_LIMIT:
        chars = {char for char in chars if (ord(char) >> 8) not in _CJK_PAGES}
    return FontOverrides(frozenset(pages), frozenset(chars))
