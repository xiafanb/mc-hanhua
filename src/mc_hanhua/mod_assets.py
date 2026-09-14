from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

from .models import ModCoverageEntry

INDEX_NAME = "mod-assets.sqlite"
INDEX_DIRNAME = ".index"
SOURCE_PRIORITY = {"local": 0}


def sha256_file(path: Path) -> str:
    """Hash file content with normalized line endings and no BOM.

    Normalization keeps matching stable across git ``core.autocrlf`` settings:
    a CRLF checkout of ``en_us.json`` must still match the LF copy inside a
    mod JAR.
    """

    return sha256_bytes(_normalized_lang_bytes(path))


def _normalized_lang_bytes(path: Path) -> bytes:
    data = path.read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data.replace(b"\r\n", b"\n")


def sha256_raw_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(slots=True)
class ModAssetEntry:
    """One indexed ``assets/<modid>/lang/en_us.json`` + ``zh_cn.json`` pair."""

    modid: str
    source: str
    en_hash: str
    en_path: Path
    zh_path: Path
    en_keys: int = 0
    zh_keys: int = 0


@dataclass(slots=True)
class ModCoverageSummary:
    """Aggregated coverage view returned by :meth:`ModAssetLibrary.coverage`."""

    entries: list[ModCoverageEntry] = field(default_factory=list)


class ModAssetLibrary:
    """Index and query reusable mod translations keyed by en_us content hash.

    The library root contains one directory per source (``cfpa``, ``local``...),
    each laid out as ``<source>/assets/<modid>/lang/{en_us,zh_cn}.json``. Matching
    is deliberately version-agnostic: identical ``en_us.json`` content (SHA-256)
    reuses the paired ``zh_cn.json`` regardless of the Minecraft version the mod
    targets. The ``local`` source wins over every other source so private or
    custom mods can override public CFPA data.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._index_path = self.root / INDEX_DIRNAME / INDEX_NAME

    # ------------------------------------------------------------------ index
    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._index_path)
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS assets (
                modid TEXT NOT NULL,
                source TEXT NOT NULL,
                en_hash TEXT NOT NULL,
                en_path TEXT NOT NULL,
                zh_path TEXT NOT NULL,
                en_keys INTEGER NOT NULL,
                zh_keys INTEGER NOT NULL,
                en_mtime REAL NOT NULL,
                PRIMARY KEY (source, modid, en_hash)
            )
            """
        )
        connection.execute("CREATE INDEX IF NOT EXISTS idx_assets_lookup ON assets (modid, en_hash)")
        return connection

    def _iter_pairs(self) -> list[tuple[ModAssetEntry, float]]:
        pairs: list[tuple[ModAssetEntry, float]] = []
        if not self.root.is_dir():
            return pairs
        for source_dir in sorted(self.root.iterdir()):
            if not source_dir.is_dir() or source_dir.name.startswith("."):
                continue
            assets_dir = source_dir / "assets"
            if not assets_dir.is_dir():
                continue
            for modid_dir in sorted(assets_dir.iterdir()):
                lang_dir = modid_dir / "lang"
                en_path = lang_dir / "en_us.json"
                zh_path = lang_dir / "zh_cn.json"
                if not (lang_dir.is_dir() and en_path.is_file() and zh_path.is_file()):
                    continue
                en_keys = _count_json_keys(en_path)
                zh_keys = _count_json_keys(zh_path)
                entry = ModAssetEntry(
                    modid=modid_dir.name,
                    source=source_dir.name,
                    en_hash=sha256_file(en_path),
                    en_path=en_path,
                    zh_path=zh_path,
                    en_keys=en_keys,
                    zh_keys=zh_keys,
                )
                pairs.append((entry, en_path.stat().st_mtime))
        return pairs

    def _is_fresh(self, pairs: list[tuple[ModAssetEntry, float]]) -> bool:
        if not self._index_path.is_file():
            return False
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    "SELECT source, modid, en_hash, en_mtime FROM assets"
                ).fetchall()
        except sqlite3.Error:
            return False
        indexed = {(row[0], row[1], row[2]): row[3] for row in rows}
        current = {(entry.source, entry.modid, entry.en_hash): mtime for entry, mtime in pairs}
        return indexed == current

    def ensure_index(self) -> int:
        """Build the SQLite index when missing or stale; return entry count."""

        pairs = self._iter_pairs()
        if self._is_fresh(pairs):
            return len(pairs)
        self._index_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute("DELETE FROM assets")
            connection.executemany(
                """
                INSERT OR REPLACE INTO assets
                    (modid, source, en_hash, en_path, zh_path, en_keys, zh_keys, en_mtime)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        entry.modid,
                        entry.source,
                        entry.en_hash,
                        str(entry.en_path),
                        str(entry.zh_path),
                        entry.en_keys,
                        entry.zh_keys,
                        mtime,
                    )
                    for entry, mtime in pairs
                ],
            )
            connection.commit()
        return len(pairs)

    def rebuild_index(self) -> int:
        if self._index_path.exists():
            self._index_path.unlink()
        return self.ensure_index()

    # ----------------------------------------------------------------- lookup
    def _ordered_entries(self, modid: str) -> list[ModAssetEntry]:
        if not self._index_path.is_file():
            return []
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    "SELECT modid, source, en_hash, en_path, zh_path, en_keys, zh_keys FROM assets WHERE modid = ?",
                    (modid,),
                ).fetchall()
        except sqlite3.Error:
            return []
        entries = [
            ModAssetEntry(
                modid=row[0],
                source=row[1],
                en_hash=row[2],
                en_path=Path(row[3]),
                zh_path=Path(row[4]),
                en_keys=int(row[5]),
                zh_keys=int(row[6]),
            )
            for row in rows
        ]
        entries.sort(key=lambda entry: (SOURCE_PRIORITY.get(entry.source, 1), entry.source))
        return entries

    def find_full(self, modid: str, en_hash: str) -> Path | None:
        """Return the zh_cn.json path when the exact en_us content is indexed."""

        entry = self.find_full_entry(modid, en_hash)
        return entry.zh_path if entry else None

    def find_full_entry(self, modid: str, en_hash: str) -> ModAssetEntry | None:
        for entry in self._ordered_entries(modid):
            if entry.en_hash == en_hash and entry.zh_path.is_file():
                return entry
        return None

    def find_partial(self, modid: str, en_us: dict[str, object]) -> dict[str, str]:
        """Return ``{key: zh_cn_value}`` for keys whose source text matches.

        Matching is by key *and* identical English value, so a mod update that
        only rewords some strings still reuses everything unchanged.
        """

        seeds: dict[str, str] = {}
        for entry in self._ordered_entries(modid):
            en_reference = _load_str_dict(entry.en_path)
            zh_reference = _load_str_dict(entry.zh_path)
            if not en_reference or not zh_reference:
                continue
            for key, value in en_us.items():
                if key in seeds or not isinstance(value, str):
                    continue
                if en_reference.get(key) != value:
                    continue
                target = zh_reference.get(key)
                if isinstance(target, str) and target:
                    seeds[key] = target
        return seeds

    def coverage(self, modids: list[str]) -> ModCoverageSummary:
        """Summarize which mods have any indexed asset, without hashing files."""

        summary = ModCoverageSummary()
        for modid in modids:
            entries = self._ordered_entries(modid)
            summary.entries.append(
                ModCoverageEntry(
                    archive="",
                    modid=modid,
                    source=entries[0].source if entries else "",
                    match_type="full" if entries else "none",
                    keys_total=entries[0].en_keys if entries else 0,
                    keys_covered=entries[0].zh_keys if entries else 0,
                )
            )
        return summary


def _count_json_keys(path: Path) -> int:
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return 0
    return len(data) if isinstance(data, dict) else 0


def _load_str_dict(path: Path) -> dict[str, str]:
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): value for key, value in data.items() if isinstance(value, str)}


def default_mod_assets_root() -> Path | None:
    """Locate the asset library for source checkouts and packaged builds."""

    candidates = [
        Path.cwd() / "data" / "mod-assets",
        Path(__file__).resolve().parents[2] / "data" / "mod-assets",
        Path(sys.executable).resolve().parent / "data" / "mod-assets",
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None
