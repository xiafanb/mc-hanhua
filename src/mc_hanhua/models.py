from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


FAILURE_REASON_LABELS = {
    "missing_api_key": "缺少 API Key",
    "network_disconnected": "服务端断开连接",
    "network_error": "网络连接失败",
    "timeout": "请求超时",
    "rate_limited": "接口限流",
    "server_error": "服务端错误",
    "http_error": "API HTTP 错误",
    "response_format": "响应格式无效",
    "no_chinese_output": "未返回中文译文",
    "placeholder_mismatch": "占位符不匹配",
    "low_quality": "质量校验未通过",
    "unexpected_expansion": "出现无来源扩写",
    "structural_token": "新增链接或命令",
    "review_response_format": "审校响应格式无效",
    "review_rejected": "审校未通过",
    "visual_glyph": "视觉字形资源",
    "external_link_label": "外链按钮标签",
    "command_feedback": "命令执行回显",
    "reference_locked": "命令选择器引用锁定",
    "player_name": "玩家名/署名",
    "author_name": "书籍作者名",
    "entity_name_preserved": "实体名保留",
    "entity_display_name": "安全增量显示文本",
    "unknown_identity": "未知身份字段",
    "mechanism_locked": "机制字段锁定",
    "unknown_mechanism_field": "未审计机制字段",
    "unparsed_mechanism": "未解析命令",
    "nbt_parse_error": "NBT 解析失败",
    "newline_structure": "换行结构被破坏",
    "unexpected_error": "未分类错误",
    "meta_response": "模型元回复穿漏",
    "cross_id_mismatch": "批翻译 ID 交叉错位",
    "official_term_conflict": "官方术语冲突",
}


def summarize_failure_counts(counts: dict[str, int]) -> str:
    return "，".join(f"{FAILURE_REASON_LABELS.get(reason, reason)} {count}" for reason, count in counts.items() if count)


@dataclass(slots=True)
class TextUnit:
    id: str
    source_text: str
    file_path: str
    format: str
    locator: str
    context: str = ""
    risk_level: RiskLevel = RiskLevel.LOW
    suggested_target: str | None = None
    final_target: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # Conservative field policy. IMMUTABLE_* never go to the AI;
    # UNKNOWN_REVIEW is recorded and kept original; only TRANSLATABLE_DISPLAY is sent.
    policy: str = ""
    policy_reason: str = ""
    reference_sources: list[str] = field(default_factory=list)
    # Mechanism object / display association used by the reference index.
    # Never inferred from source text alone when multiple objects share a word.
    object_key: str = ""
    canonical_key: str = ""


@dataclass(slots=True)
class ScanResult:
    root: Path
    text_units: list[TextUnit] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    filtered_counts: dict[str, int] = field(default_factory=dict)
    # Cross-file mechanism reference index: values consumed by selectors, NBT
    # predicates or clear/execute-if-items match clauses. A value in this set
    # must never change between input and output.
    mechanism_refs: set[str] = field(default_factory=set)
    # (file_path, command_line) pairs seen during scan, used by the read-only
    # pre-publication resource audit without a second file pass.
    commands: list[tuple[str, str]] = field(default_factory=list)
    # Per-file filtered counts (reason -> count), giving the report file
    # positions for unparsed_mechanism / mechanism_locked diagnostics.
    file_filtered: dict[str, dict[str, int]] = field(default_factory=dict)
    # Bounded, redacted details for every filtered string (not just counts).
    filtered_details: list[dict[str, Any]] = field(default_factory=list)
    # Cross-file reference index (definitions / uses / locked / unresolved).
    # Typed as Any so models stays import-cycle free; pipeline stores a ReferenceIndex.
    reference_index: Any = None


@dataclass(slots=True)
class TranslationGroup:
    id: str
    file_path: str
    format: str
    context: str
    units: list[TextUnit]
    container_type: str = "generic"
    requires_review: bool = False
    review_only: bool = False
    layout_reflow: bool = False
    spatial_sign_cluster: bool = False


@dataclass(slots=True)
class ModCoverageEntry:
    """Per-mod asset library outcome recorded in the translation report."""

    archive: str
    modid: str
    source: str
    match_type: str  # full | partial | existing | none
    keys_total: int = 0
    keys_covered: int = 0
    keys_ai: int = 0
    ai_failures: int = 0


@dataclass(slots=True)
class ArchiveResult:
    """Per-archive outcome. Survives nested audit failure and old-report loading."""

    archive_id: str
    archive_chain: list[str] = field(default_factory=list)
    status: str = "pending"
    scanned: int = 0
    groups_completed: int = 0
    candidate_count: int = 0
    unit_ids: list[str] = field(default_factory=list)
    filtered_details: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    audit_issues: list[dict[str, Any]] = field(default_factory=list)
    staging_path: str = ""
    report_path: str = ""
    publication_status: str = "unknown"
    translated: int = 0
    reused: int = 0
    skipped: int = 0
    original_fingerprint: str = ""
    error_code: str = ""
    phase: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "archive_id": self.archive_id,
            "archive_chain": list(self.archive_chain),
            "status": self.status,
            "scanned": self.scanned,
            "groups_completed": self.groups_completed,
            "candidate_count": self.candidate_count,
            "unit_ids": list(self.unit_ids),
            "filtered_details": list(self.filtered_details),
            "warnings": list(self.warnings),
            "audit_issues": list(self.audit_issues),
            "staging_path": self.staging_path,
            "report_path": self.report_path,
            "publication_status": self.publication_status,
            "translated": self.translated,
            "reused": self.reused,
            "skipped": self.skipped,
            "original_fingerprint": self.original_fingerprint,
            "error_code": self.error_code,
            "phase": self.phase,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> ArchiveResult:
        data = payload if isinstance(payload, dict) else {}
        known = {key: data[key] for key in cls.__dataclass_fields__ if key in data}
        known.setdefault("archive_id", str(data.get("archive_id") or ""))
        known.setdefault("publication_status", "unknown")
        return cls(**known)


@dataclass(slots=True)
class TranslationReport:
    input_path: Path
    output_path: Path
    scanned: int = 0
    translated: int = 0
    reused: int = 0
    skipped: int = 0
    high_risk: int = 0
    warnings: list[str] = field(default_factory=list)
    failure_counts: dict[str, int] = field(default_factory=dict)
    failure_samples: dict[str, str] = field(default_factory=dict)
    retry_counts: dict[str, int] = field(default_factory=dict)
    groups_total: int = 0
    groups_completed: int = 0
    failed_groups: int = 0
    reviewed_groups: int = 0
    review_corrected: int = 0
    review_pending_groups: int = 0
    planned_draft_calls: int = 0
    planned_review_calls: int = 0
    api_requests: int = 0
    token_usage: dict[str, int] = field(default_factory=dict)
    usage_reported_requests: int = 0
    rate_limit_cooldowns: int = 0
    checkpointed_groups: int = 0
    seeded_drafts: int = 0
    consistency_unified: int = 0
    consistency_conflicts: list[dict[str, object]] = field(default_factory=list)
    # Cross-file reference index snapshot for JSON / HTML / JSONL reports.
    reference_index: dict[str, Any] = field(default_factory=dict)
    canonical_conflicts: list[dict[str, object]] = field(default_factory=list)
    filtered_counts: dict[str, int] = field(default_factory=dict)
    layout_reflow_groups: int = 0
    mod_coverage: list[ModCoverageEntry] = field(default_factory=list)
    spatial_sign_clusters: int = 0
    # Per-unit outcomes. Group completion is not the same as unit success:
    # a group can finish while some units keep the original or fail write-back.
    units_total: int = 0
    units_translated: int = 0
    units_original_preserved: int = 0
    units_validation_failed: int = 0
    units_writeback_failed: int = 0
    unit_outcomes: dict[str, int] = field(default_factory=dict)
    # Bounded, redacted filter details (file/path/source/reason).
    filtered_details: list[dict[str, Any]] = field(default_factory=list)
    # Path of the structured JSONL event stream when one was written.
    event_log_path: str = ""
    # Read-only input/output NBT text comparison and same-name structure
    # conflict diagnosis. Never implies a generated world was updated.
    nbt_text_diff: dict[str, Any] | None = None
    structure_conflicts: list[dict[str, Any]] = field(default_factory=list)
    # Read-only pre-publication audit (rescan of the output copy + resource
    # ID / mechanism / book layout checks). audit_blocked means clear
    # structural damage was found and publication must not proceed.
    audit: dict[str, Any] | None = None
    audit_blocked: bool = False
    entity_display_name: int = 0
    unknown_identity: int = 0
    task_id: str = ""
    publication_status: str = "unknown"
    archive_results: list[ArchiveResult] = field(default_factory=list)
    workspace_path: str = ""
    report_path: str = ""
    recoverable: bool = False


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """A UI-safe snapshot emitted while a translation job is running."""

    stage: str
    message: str
    percent: int
    scanned: int = 0
    translated: int = 0
    reused: int = 0
    skipped: int = 0
    ai_total: int = 0
    ai_done: int = 0
    ai_failed: int = 0
    current_file: str = ""
    last_error: str = ""
    failure_counts: dict[str, int] = field(default_factory=dict)
    retry_counts: dict[str, int] = field(default_factory=dict)
    review_pending_groups: int = 0
    groups_total: int = 0
    groups_done: int = 0
    reviewed_groups: int = 0
    review_corrected: int = 0
    planned_draft_calls: int = 0
    planned_review_calls: int = 0
    api_requests: int = 0
    token_usage: dict[str, int] = field(default_factory=dict)
    usage_reported_requests: int = 0
    rate_limit_cooldowns: int = 0
    checkpointed_groups: int = 0
    seeded_drafts: int = 0
    filtered_counts: dict[str, int] = field(default_factory=dict)
    units_total: int = 0
    units_translated: int = 0
    units_original_preserved: int = 0
    units_validation_failed: int = 0
    units_writeback_failed: int = 0
    audit_errors: int = 0
    audit_warnings: int = 0
    # Optional rich HTML payload (e.g. colored per-segment translation
    # results). When non-empty the GUI renders it in place of ``message``;
    # the runtime/text log always uses ``message`` (plain, searchable).
    message_html: str = ""
    elapsed_ms: int = 0
    problem_id: str = ""
    task_id: str = ""
    archive_id: str = ""
    sequence: int = 0
    scanned_total: int = 0
    groups_discovered: int = 0
    units_candidate_passed: int = 0
    units_published: int = 0
    archive_index: int = 0
    archives_discovered: int = 0
    local_groups_total: int = 0
    local_groups_completed: int = 0
    requests_started: int = 0
    requests_completed: int = 0
    requests_failed: int = 0
    retries: int = 0
    in_flight: int = 0
    queued: int = 0
    last_response_at: str = ""
    last_tool_progress_at: str = ""
    waiting_reason: str = ""
    next_retry_at: str = ""


@dataclass(frozen=True, slots=True)
class GlossaryEntry:
    source: str
    target: str
    note: str = ""
    priority: int = 100
