"""Pure task-record dataclasses, hashing and JSONL helpers. No PySide6."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
UNSUPPORTED_SCHEMA = "unsupported_schema_version"


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _canonical_locator(locator: str) -> str:
    try:
        parsed = json.loads(locator)
    except (TypeError, ValueError, json.JSONDecodeError):
        return locator
    if isinstance(parsed, dict):
        return json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if isinstance(parsed, list):
        return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
    return locator


def unit_key(
    archive_chain: list[str],
    file_path: str,
    format_name: str,
    locator: str,
    source_text: str,
) -> str:
    """SHA-256 of a stable JSON array. Paths are POSIX; locators sort object keys."""

    payload = [
        [str(part).replace("\\", "/") for part in archive_chain],
        str(file_path).replace("\\", "/"),
        str(format_name),
        _canonical_locator(locator),
        source_text,
    ]
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def archive_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def directory_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(
        (path for path in root.rglob("*") if path.is_file() and ".mc-hanhua" not in path.parts),
        key=lambda item: item.relative_to(root).as_posix(),
    )
    for path in files:
        rel = path.relative_to(root).as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(archive_fingerprint(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def input_fingerprint(path: Path) -> str:
    if path.is_file():
        return archive_fingerprint(path)
    return directory_fingerprint(path)


def _load_known(cls, payload: dict[str, Any]):
    version = int(payload.get("schema_version") or 0)
    if version and version != SCHEMA_VERSION:
        raise ValueError(UNSUPPORTED_SCHEMA)
    fields = getattr(cls, "__dataclass_fields__", {})
    data = {key: value for key, value in payload.items() if key in fields}
    data.setdefault("schema_version", SCHEMA_VERSION)
    return cls(**data)


@dataclass(slots=True)
class PreviewRecord:
    unit_key: str
    file_path: str
    archive_chain: list[str]
    format_name: str
    locator: str
    source_text: str
    policy: str
    reason_code: str
    will_send_ai: bool
    record_kind: str
    schema_version: int = SCHEMA_VERSION
    container_type: str = ""
    reference_sources: list[str] = field(default_factory=list)
    reuse_provider: str = ""
    target_text: str = ""
    outcome: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> PreviewRecord:
        return _load_known(cls, payload)


@dataclass(slots=True)
class ProblemRecord:
    problem_id: str
    job_id: str
    reason_code: str
    severity: str
    message: str
    status: str
    schema_version: int = SCHEMA_VERSION
    unit_key: str = ""
    candidate_target: str = ""
    reference_sources: list[str] = field(default_factory=list)
    attempt_count: int = 1
    file_path: str = ""
    format_name: str = ""
    locator: str = ""
    source_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ProblemRecord:
        return _load_known(cls, payload)


def make_problem_id(job_id: str, unit_key: str, reason_code: str) -> str:
    payload = json.dumps([job_id, unit_key, reason_code], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:24]


@dataclass(slots=True)
class ManualEdit:
    job_id: str
    input_fingerprint: str
    unit_key: str
    archive_chain: list[str]
    file_path: str
    format_name: str
    locator: str
    source_text: str
    target_text: str
    timestamp: str
    note: str = ""
    status: str = "draft"
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ManualEdit:
        return _load_known(cls, payload)


@dataclass(slots=True)
class TaskManifest:
    job_id: str
    input_path: str
    input_fingerprint: str
    output_path: str
    state: str
    created_at: str
    updated_at: str
    schema_version: int = SCHEMA_VERSION
    policy_version: str = ""
    official_terms_fingerprint: str = ""
    completed_group_ids: list[str] = field(default_factory=list)
    report_path: str = ""
    event_log_path: str = ""
    last_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> TaskManifest:
        return _load_known(cls, payload)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_manifest(path: Path) -> TaskManifest:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("manifest must be a JSON object")
    return TaskManifest.from_dict(payload)


def save_manifest(path: Path, manifest: TaskManifest) -> None:
    manifest.updated_at = _utc_now()
    atomic_write_json(path, manifest.to_dict())


def append_manual_edit(path: Path, edit: ManualEdit) -> None:
    if not edit.source_text:
        raise ValueError("file-level problems cannot save a translation")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(edit.to_dict(), ensure_ascii=False) + "\n")


def load_manual_edits(path: Path) -> tuple[list[ManualEdit], list[str]]:
    if not path.exists():
        return [], []
    edits: list[ManualEdit] = []
    errors: list[str] = []
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            errors.append(f"manual-edits.jsonl line {index} is truncated or invalid")
            continue
        if not isinstance(payload, dict):
            errors.append(f"manual-edits.jsonl line {index} is not an object")
            continue
        try:
            edits.append(ManualEdit.from_dict(payload))
        except (TypeError, ValueError) as exc:
            errors.append(f"manual-edits.jsonl line {index}: {exc}")
    return edits, errors


class PreviewIndex:
    """SQLite-backed preview rows with pagination. No Qt dependency."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.execute("pragma journal_mode=wal")
        self.conn.execute(
            """
            create table if not exists preview (
              unit_key text primary key,
              file_path text not null,
              archive_chain text not null,
              format_name text not null,
              locator text not null,
              source_text text not null,
              policy text not null,
              reason_code text not null,
              will_send_ai integer not null,
              record_kind text not null,
              container_type text not null default '',
              reuse_provider text not null default '',
              payload text not null
            )
            """
        )
        self.conn.commit()
        self._lock = threading.RLock()

    def replace_all(self, records: list[PreviewRecord]) -> None:
        with self._lock:
            self.conn.execute("delete from preview")
            self.conn.executemany(
                """
                insert into preview (
                  unit_key, file_path, archive_chain, format_name, locator, source_text,
                  policy, reason_code, will_send_ai, record_kind, container_type, reuse_provider, payload
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        record.unit_key,
                        record.file_path,
                        json.dumps(record.archive_chain, ensure_ascii=False),
                        record.format_name,
                        record.locator,
                        record.source_text,
                        record.policy,
                        record.reason_code,
                        int(record.will_send_ai),
                        record.record_kind,
                        record.container_type,
                        record.reuse_provider,
                        json.dumps(record.to_dict(), ensure_ascii=False),
                    )
                    for record in records
                ],
            )
            self.conn.commit()

    def search(
        self,
        *,
        needle: str = "",
        policy: str = "",
        offset: int = 0,
        limit: int = 200,
    ) -> tuple[list[PreviewRecord], int]:
        clauses = ["1=1"]
        args: list[object] = []
        if needle:
            clauses.append("(source_text like ? or file_path like ? or locator like ?)")
            like = f"%{needle}%"
            args.extend([like, like, like])
        if policy:
            clauses.append("policy = ?")
            args.append(policy)
        where = " and ".join(clauses)
        with self._lock:
            total = int(self.conn.execute(f"select count(*) from preview where {where}", args).fetchone()[0])
            rows = self.conn.execute(
                f"select payload from preview where {where} order by file_path, locator limit ? offset ?",
                [*args, limit, offset],
            ).fetchall()
        records = [PreviewRecord.from_dict(json.loads(row[0])) for row in rows]
        return records, total

    def close(self) -> None:
        with self._lock:
            self.conn.close()
