from __future__ import annotations

import re
from collections.abc import Callable

_NAME = r"[A-Z][A-Za-z0-9_]{2,}"
_CUE = (
    r"(?:created by|made by|map by|built by|tested by|programmed by|designed by|developed by|hecho por|creado por"
    r"|(?:special )?thanks to|gracias a"
    r"|(?:credits|special thanks|agradecimientos|authors?|autor|builders?|designers?|developers?)\s*[:\-–])"
)
_CREDIT_RE = re.compile(
    rf"(?i:\b{_CUE})\s*({_NAME}(?:\s*(?:,|&|\+|\band\b|\by\b)\s*{_NAME})*)(?![A-Za-z0-9_])"
)
_NAME_RE = re.compile(_NAME)
_STOP = frozenset(
    "the hand me myself note notes name unknown anonymous everyone all you your team our people "
    "this that these those his her their some many for and with from playing reading watching "
    "helping testing using everybody".split()
)
_GAMERTAG_RE = re.compile(r"(?<![A-Za-z0-9_§.:/{%\$@#-])[A-Za-z0-9_]{4,}(?![A-Za-z0-9_:=}])")
_COMMON_WORD_BEFORE_DIGITS = frozenset(
    "level room stage part floor chapter phase area zone house point key door task step test day round "
    "wave team player boss sector nivel sala piso fase parte zona puerta capitulo paso page quest "
    "world map spawn base checkpoint lobby arena button lever chest sign".split()
)
_KEY_POSITION_RE = re.compile(r"""[\\"']*\s*[:=]""")
_COLOR_CODE_PREFIX = frozenset("0123456789abcdefklmnor")


def _gamertag_like(token: str) -> bool:
    if not re.search(r"[A-Za-z]", token):
        return False
    if token[0] in _COLOR_CODE_PREFIX and re.fullmatch(r"[A-Z][a-z]+|[A-Z]+", token[1:] or "a"):
        return False
    if "_" in token:
        parts = token.split("_")
        return all(parts) and (any(c.isupper() for c in token) or any(c.isdigit() for c in token)) and not token.isupper()
    if re.search(r"[a-z][A-Z]", token):
        return True
    trailing = re.fullmatch(r"([A-Za-z]{3,})(\d{1,4})", token)
    if trailing:
        word = trailing.group(1)
        return not word.isupper() and word.lower() not in _COMMON_WORD_BEFORE_DIGITS
    return False


def find_name_spans(text: str, is_known: Callable[[str], bool]) -> list[tuple[int, int]]:
    spans: set[tuple[int, int]] = set()
    for match in _CREDIT_RE.finditer(text):
        group_start = match.start(1)
        for name in _NAME_RE.finditer(match.group(1)):
            word = name.group(0)
            if word.lower() in _STOP or is_known(word):
                continue
            spans.add((group_start + name.start(), group_start + name.end()))
    for match in _GAMERTAG_RE.finditer(text):
        token = match.group(0)
        if _gamertag_like(token) and not is_known(token) and not _KEY_POSITION_RE.match(text, match.end()):
            spans.add((match.start(), match.end()))
    return sorted(spans)


def mask_names(text: str, is_known: Callable[[str], bool]) -> tuple[str, dict[str, str]]:
    spans = find_name_spans(text, is_known)
    if not spans:
        return text, {}
    mapping: dict[str, str] = {}
    pieces: list[str] = []
    cursor = 0
    index = 0
    for start, end in spans:
        if start < cursor:
            continue
        token = f"{{term{index}}}"
        while token in text:
            index += 1
            token = f"{{term{index}}}"
        pieces.append(text[cursor:start])
        pieces.append(token)
        mapping[token] = text[start:end]
        cursor = end
        index += 1
    pieces.append(text[cursor:])
    return "".join(pieces), mapping
