"""Structured per-text translation diagnostics.

Events are JSONL, thread-safe, length-limited and never record API keys.
A permanent copy is written under %APPDATA%/mc-hanhua/logs (see
``diagnostics.log_directory``); a second copy is mirrored into the job's
``.mc-hanhua/logs/translation-events.jsonl`` once the output directory exists.
If a destination cannot be created, events stay in an in-memory buffer so
critical diagnostics are not lost.
"""

from __future__ import annotations

import json
import os
import re
import threading
from contextvars import ContextVar
from contextvars import ContextVar as _ContextVar
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .diagnostics import log_directory

MAX_SOURCE_CHARS = 400
MAX_SUBMITTED_CHARS = 1500
MAX_RESPONSE_CHARS = 2000
MAX_CANDIDATE_CHARS = 400
MAX_BUFFERED_EVENTS = 400
MAX_FILTER_DETAILS = 500

# Credentials, bearer tokens and common key material must never land in JSONL.
_SECRET_RE = re.compile(
    r"(?i)(?:api[_-]?key|authorization|bearer|secret|token|password)"
    r"(?:[\"']?\s*[:=]\s*)([^\s\"']+)"
    r"|sk-[A-Za-z0-9]{8,}"
    r"|Bearer\s+[A-Za-z0-9._\-]+"
)


def redact_text(value: object, limit: int = MAX_SOURCE_CHARS) -> str:
    """Length-limit a diagnostic string and strip credential-shaped tokens."""

    text = "" if value is None else str(value)
    if not text:
        return ""
    cleaned = _SECRET_RE.sub("<redacted>", text)
    if len(cleaned) > limit:
        return cleaned[:limit] + "…"
    return cleaned


@dataclass
class TranslationEvent:
    """One structured record for a scanned, filtered, translated or written unit."""

    stage: str
    file: str = ""
    format: str = ""
    locator: str = ""
    context: str = ""
    unit_id: str = ""
    source: str = ""
    policy: str = ""
    policy_reason: str = ""
    group_id: str = ""
    request_type: str = ""
    submitted: str = ""
    raw_response: str = ""
    candidate: str = ""
    validation_status: str = ""
    validation_reason: str = ""
    review_status: str = ""
    writeback_status: str = ""
    writeback_target: str = ""
    writeback_changed: bool | None = None
    final: str = ""
    extra: dict[str, Any] = field(default_factory=dict)
    ts: str = ""

    def __post_init__(self) -> None:
        if not self.ts:
            self.ts = datetime.now().isoformat(timespec="seconds")
        self.source = redact_text(self.source, MAX_SOURCE_CHARS)
        self.submitted = redact_text(self.submitted, MAX_SUBMITTED_CHARS)
        self.raw_response = redact_text(self.raw_response, MAX_RESPONSE_CHARS)
        self.candidate = redact_text(self.candidate, MAX_CANDIDATE_CHARS)
        self.final = redact_text(self.final, MAX_SOURCE_CHARS)
        self.locator = redact_text(self.locator, MAX_SOURCE_CHARS)
        self.context = redact_text(self.context, MAX_SOURCE_CHARS)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        extra = payload.pop("extra") or {}
        for key, value in extra.items():
            if key not in payload or payload[key] in ("", None):
                payload[key] = value
        return {key: value for key, value in payload.items() if value not in ("", None, {}, [])}


class EventSink:
    """Append-only JSONL writer with a permanent path plus optional mirrors."""

    def __init__(self, primary: Path | None = None, mirrors: list[Path] | None = None) -> None:
        self.primary = primary
        self.mirrors: list[Path] = list(mirrors or [])
        self._lock = threading.Lock()
        self._buffer: list[str] = []
        self.written = 0
        self.disk_failures = 0

    @classmethod
    def create_default(cls) -> EventSink:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        primary: Path | None
        try:
            primary = log_directory() / f"translation-events-{timestamp}.jsonl"
            primary.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            primary = None
        return cls(primary)

    def add_mirror(self, path: Path) -> None:
        """Also write to ``path``, replaying the in-memory buffer first."""

        with self._lock:
            if path in self.mirrors or (self.primary is not None and path == self.primary):
                return
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                if self._buffer and not path.exists():
                    path.write_text("".join(self._buffer), encoding="utf-8")
                self.mirrors.append(path)
            except OSError:
                self.disk_failures += 1

    def emit(self, event: TranslationEvent | dict[str, Any]) -> None:
        if isinstance(event, dict):
            event = TranslationEvent(**{key: value for key, value in event.items() if key in TranslationEvent.__dataclass_fields__})
        line = json.dumps(event.to_dict(), ensure_ascii=False) + "\n"
        with self._lock:
            if len(self._buffer) < MAX_BUFFERED_EVENTS:
                self._buffer.append(line)
            elif self._buffer:
                self._buffer.pop(0)
                self._buffer.append(line)
            targets = [path for path in [self.primary, *self.mirrors] if path is not None]
            if not targets:
                return
            for path in targets:
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with path.open("a", encoding="utf-8") as handle:
                        handle.write(line)
                    self.written += 1
                except OSError:
                    self.disk_failures += 1

    def recent_events(self) -> list[dict[str, Any]]:
        with self._lock:
            parsed: list[dict[str, Any]] = []
            for line in self._buffer:
                try:
                    parsed.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            return parsed


_SINK: ContextVar[EventSink | None] = ContextVar("mc_hanhua_event_sink", default=None)
_ENABLED: ContextVar[bool] = ContextVar("mc_hanhua_events_enabled", default=True)
_PROCESS_SINK: EventSink | None = None
_PROCESS_SINK_LOCK = threading.Lock()


def process_sink() -> EventSink | None:
    """Process-wide sink shared by worker threads. Thread-safe to read."""

    return _PROCESS_SINK


def current_sink() -> EventSink | None:
    return _SINK.get() or _PROCESS_SINK


def set_current_sink(sink: EventSink | None):
    """Install a sink for this context and as the process-wide default.

    ContextVar remains the test / nested-job override. Worker threads created
    by ThreadPoolExecutor do not inherit ContextVar values, so emit_event also
    falls back to the process-level sink.
    """

    global _PROCESS_SINK
    with _PROCESS_SINK_LOCK:
        _PROCESS_SINK = sink
    return _SINK.set(sink)


def reset_current_sink(token) -> None:
    global _PROCESS_SINK
    previous = None
    old = getattr(token, "old_value", None)
    missing = getattr(type(token), "MISSING", None)
    if old is not None and old is not missing and isinstance(old, EventSink):
        previous = old
    with _PROCESS_SINK_LOCK:
        _PROCESS_SINK = previous
    _SINK.reset(token)


def bind_current_sink(sink: EventSink | None = None) -> None:
    """Copy the process sink onto this thread's ContextVar (executor initializer)."""

    chosen = sink if sink is not None else _PROCESS_SINK
    if chosen is not None:
        _SINK.set(chosen)


def events_enabled() -> bool:
    return bool(_ENABLED.get())


def set_events_enabled(enabled: bool):
    return _ENABLED.set(enabled)


def reset_events_enabled(token) -> None:
    _ENABLED.reset(token)


def emit_event(**fields: Any) -> None:
    """Record a diagnostic event when a sink is installed and events are enabled."""

    if not events_enabled():
        return
    sink = current_sink()
    if sink is None:
        return
    extra = fields.pop("extra", None) or {}
    known = {key: fields.pop(key) for key in list(fields) if key in TranslationEvent.__dataclass_fields__}
    extra.update(fields)
    known["extra"] = extra
    sink.emit(TranslationEvent(**known))


def output_event_log_path(root: Path) -> Path:
    return Path(root) / ".mc-hanhua" / "logs" / "translation-events.jsonl"


def user_logs_disabled() -> bool:
    """Tests can isolate themselves without writing into the real APPDATA tree."""

    return bool(os.getenv("PYTEST_CURRENT_TEST"))


# Preview scans run in their own worker/context; report samples remain bounded.

_preview_details = _ContextVar("preview_details", default=False)


def filter_detail_limit() -> int:
    return 2**31 - 1 if _preview_details.get() else MAX_FILTER_DETAILS
