from __future__ import annotations

import sqlite3
import threading
from pathlib import Path


class TranslationMemory:
    """Context-keyed translation cache, scoped to one input version.

    The store may be shared by every task that writes into one output
    directory, so a row is only ever served to the input version that wrote it.
    ``namespace`` is the caller's input identity (see ``task_input_fingerprint``
    in the pipeline); rows written without one — every row created before
    namespaces existed — stay on disk but are never read, because their
    provenance cannot be established. Two maps that happen to share a file path
    and a source string therefore no longer hit each other (B1).
    """

    #: Table holding rows from before namespaces existed. Kept for reference,
    #: never queried: reading it would be guessing at provenance.
    LEGACY_TABLE = "memory_legacy"

    def __init__(self, path: Path, namespace: str = "") -> None:
        self.path = path
        self.namespace = namespace
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False lets worker threads share one connection;
        # every query still goes through ``_lock`` so writes are serialised.
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        # WAL keeps the per-checkpoint commits cheap (no rollback-journal
        # fsync per put) while preserving crash-safe durability semantics;
        # busy_timeout avoids instant "database is locked" failures when two
        # tasks share one output directory.
        self.conn.execute("pragma journal_mode=wal")
        self.conn.execute("pragma synchronous=normal")
        self.conn.execute("pragma busy_timeout=5000")
        self._migrate()
        self._lock = threading.RLock()

    def _migrate(self) -> None:
        """Create the namespaced table, retiring any pre-namespace one."""

        columns = {
            str(row[1])
            for row in self.conn.execute("pragma table_info(memory)").fetchall()
        }
        if columns and "namespace" not in columns:
            # SQLite cannot alter a primary key, so the old table is rebuilt
            # rather than converted. Its rows are deliberately NOT copied: an
            # unattributed row must not be served, and keeping it under its own
            # name preserves the data for anyone who wants to inspect it.
            self.conn.execute(f"alter table memory rename to {self.LEGACY_TABLE}")
        self.conn.execute(
            """
            create table if not exists memory (
              namespace text not null default '',
              source text not null,
              context text not null default '',
              target text not null,
              provider text not null,
              updated_at text not null default current_timestamp,
              primary key (namespace, source, context)
            )
            """
        )
        self.conn.commit()

    def get(self, source: str, context: str = "", allow_source_fallback: bool = False) -> str | None:
        with self._lock:
            row = self.conn.execute(
                "select target from memory where namespace = ? and source = ? and context = ?",
                (self.namespace, source, context),
            ).fetchone()
            if row:
                return str(row[0])
            if not allow_source_fallback:
                return None
            # The fallback drops the context but never the input identity: a
            # hit from another map is exactly what this scoping prevents.
            row = self.conn.execute(
                "select target from memory where namespace = ? and source = ? order by updated_at desc limit 1",
                (self.namespace, source),
            ).fetchone()
            return str(row[0]) if row else None

    def put(self, source: str, target: str, context: str = "", provider: str = "unknown") -> None:
        with self._lock:
            self.conn.execute(
                """
                insert into memory (namespace, source, context, target, provider, updated_at)
                values (?, ?, ?, ?, ?, current_timestamp)
                on conflict(namespace, source, context) do update set
                  target = excluded.target,
                  provider = excluded.provider,
                  updated_at = current_timestamp
                """,
                (self.namespace, source, context, target, provider),
            )
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.close()
