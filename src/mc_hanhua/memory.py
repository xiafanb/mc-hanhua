from __future__ import annotations

import json
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
        self.conn.execute(
            """
            create table if not exists passage_memory (
              namespace text not null,
              passage_id text not null,
              rule_version integer not null,
              source_sha256 text not null,
              members_json text not null,
              provider text not null,
              updated_at text not null default current_timestamp,
              primary key (namespace, passage_id)
            )
            """
        )
        columns = {str(row[1]) for row in self.conn.execute("pragma table_info(passage_memory)").fetchall()}
        if "chain" not in columns:
            self.conn.execute("alter table passage_memory add column chain integer not null default 0")
        self.conn.commit()

    def passage_records(self) -> list[dict[str, object]]:
        """Every stored passage record of this input version (unvalidated)."""

        with self._lock:
            rows = self.conn.execute(
                "select passage_id, rule_version, source_sha256, members_json, provider, chain from passage_memory where namespace = ?",
                (self.namespace,),
            ).fetchall()
        records = []
        for passage_id, rule_version, source_sha256, members_json, provider, chain in rows:
            try:
                members = json.loads(members_json)
            except ValueError:
                members = None
            record = {"passage_id": str(passage_id), "rule_version": int(rule_version), "source_sha256": str(source_sha256),
                      "members": members, "provider": str(provider)}
            if chain:
                record["chain"] = True
            records.append(record)
        return records

    def put_passage(self, passage_id: str, rule_version: int, source_sha256: str, members: dict[str, str], provider: str,
                    *, chain: bool = False) -> None:
        with self._lock:
            self.conn.execute(
                """
                insert into passage_memory (namespace, passage_id, rule_version, source_sha256, members_json, provider, chain, updated_at)
                values (?, ?, ?, ?, ?, ?, ?, current_timestamp)
                on conflict(namespace, passage_id) do update set
                  rule_version = excluded.rule_version,
                  source_sha256 = excluded.source_sha256,
                  members_json = excluded.members_json,
                  provider = excluded.provider,
                  chain = excluded.chain,
                  updated_at = current_timestamp
                """,
                (self.namespace, passage_id, rule_version, source_sha256, json.dumps(members, ensure_ascii=False, sort_keys=True), provider,
                 int(chain)),
            )
            self.conn.commit()

    def delete_passage(self, passage_id: str) -> None:
        with self._lock:
            self.conn.execute("delete from passage_memory where namespace = ? and passage_id = ?", (self.namespace, passage_id))
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

    def delete(self, source: str, context: str = "") -> None:
        """Evict one refused candidate in this input namespace only."""
        with self._lock:
            self.conn.execute("delete from memory where namespace = ? and source = ? and context = ?", (self.namespace, source, context))
            self.conn.commit()
