from __future__ import annotations

import re
import zipfile
from pathlib import Path

from .archives import ARCHIVE_SUFFIXES
from .nbt import TAG_STRING, get_tag, read_nbt_bytes

_MAX_LEVEL_DAT = 8 * 1024 * 1024
_MAX_DIR_DEPTH = 3
_UNSAFE = re.compile(r"[^0-9A-Za-z._\- ≤]")


def _sanitize(value: str) -> str | None:
    cleaned = _UNSAFE.sub("", value).strip()[:32].strip()
    return cleaned or None


def _version_from_level_dat(data: bytes) -> str | None:
    root = read_nbt_bytes(data).root
    try:
        name = get_tag(root, ["Data", "Version", "Name"])
        if name.type_id == TAG_STRING and name.value:
            return _sanitize(str(name.value))
    except (KeyError, IndexError):
        pass
    try:
        legacy = get_tag(root, ["Data", "version"]).value
    except (KeyError, IndexError):
        return None
    if legacy == 19133:
        return "≤1.8"
    if legacy == 19132:
        return "≤1.1"
    return None


def _zip_level_dat(path: Path) -> bytes | None:
    with zipfile.ZipFile(path) as zf:
        best: zipfile.ZipInfo | None = None
        best_depth = 0
        for info in zf.infolist():
            parts = [p for p in info.filename.replace("\\", "/").split("/") if p]
            if not parts or parts[-1] != "level.dat" or parts[0] == "__MACOSX":
                continue
            if any("##MCEDIT" in p for p in parts):
                continue
            if best is None or len(parts) < best_depth:
                best, best_depth = info, len(parts)
        if best is None or best.file_size > _MAX_LEVEL_DAT:
            return None
        return zf.read(best)


def _dir_level_dat(root: Path) -> bytes | None:
    level = [(root, 0)]
    while level:
        nxt: list[tuple[Path, int]] = []
        for folder, depth in level:
            candidate = folder / "level.dat"
            if candidate.is_file() and candidate.stat().st_size <= _MAX_LEVEL_DAT:
                return candidate.read_bytes()
            if depth >= _MAX_DIR_DEPTH:
                continue
            try:
                children = sorted(p for p in folder.iterdir() if p.is_dir())
            except OSError:
                continue
            nxt.extend((p, depth + 1) for p in children if p.name != "__MACOSX" and "##MCEDIT" not in p.name)
        level = nxt
    return None


def detect_game_version(path: Path) -> str | None:
    try:
        if path.is_dir():
            data = _dir_level_dat(path)
        elif path.suffix.lower() in ARCHIVE_SUFFIXES and zipfile.is_zipfile(path):
            data = _zip_level_dat(path)
        else:
            return None
        return _version_from_level_dat(data) if data else None
    except Exception:
        return None


def default_output_path(input_path: Path, directory: Path | None = None) -> Path:
    version = detect_game_version(input_path)
    prefix = f"[{version}]" if version else ""
    is_archive = input_path.suffix.lower() in ARCHIVE_SUFFIXES and not input_path.is_dir()
    name = f"{prefix}{input_path.stem}_zh_cn{input_path.suffix}" if is_archive else f"{prefix}{input_path.name}_zh_cn"
    return (directory / name) if directory is not None else input_path.with_name(name)
