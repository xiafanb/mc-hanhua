from __future__ import annotations

import sqlite3
import threading
from pathlib import Path


class TranslationMemory:
    def __init__(self, path: Path) -> None:
        self.path = path
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
        self.conn.execute(
            """
            create table if not exists memory (
              source text not null,
              context text not null default '',
              target text not null,
              provider text not null,
              updated_at text not null default current_timestamp,
              primary key (source, context)
            )
            """
        )
        self.conn.commit()
        self._lock = threading.RLock()

    def get(self, source: str, context: str = "", allow_source_fallback: bool = False) -> str | None:
        with self._lock:
            row = self.conn.execute(
                "select target from memory where source = ? and context = ?",
                (source, context),
            ).fetchone()
            if row:
                return str(row[0])
            if not allow_source_fallback:
                return None
            row = self.conn.execute(
                "select target from memory where source = ? order by updated_at desc limit 1",
                (source,),
            ).fetchone()
            return str(row[0]) if row else None

    def put(self, source: str, target: str, context: str = "", provider: str = "unknown") -> None:
        with self._lock:
            self.conn.execute(
                """
                insert into memory (source, context, target, provider, updated_at)
                values (?, ?, ?, ?, current_timestamp)
                on conflict(source, context) do update set
                  target = excluded.target,
                  provider = excluded.provider,
                  updated_at = current_timestamp
                """,
                (source, context, target, provider),
            )
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.close()
