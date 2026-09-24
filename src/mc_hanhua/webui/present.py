"""Pure presentation mapping for the HTML desktop shell. No Qt."""

from __future__ import annotations

import json
from typing import Any

from mc_hanhua.models import FAILURE_REASON_LABELS, ProgressEvent, TranslationReport
from mc_hanhua.policy import POLICY_IMMUTABLE_MATCH, POLICY_TRANSLATABLE, POLICY_UNKNOWN, should_send_to_ai
from mc_hanhua.task_records import make_problem_id, unit_key

ROLE_LABELS = {
    "book": "书页正文",
    "sign": "告示牌",
    "entity_display_name": "实体名称",
    "text-display": "浮动文字",
    "dialogue": "对话",
    "json-lang": "语言文件",
    "legacy-lang": "语言文件",
    "mcfunction": "命令文本",
    "generic-json": "配置文本",
    "nbt-text": "NBT 文本",
}

# Frameless title-bar hit testing. Coordinates arrive in device pixels while the
# title bar is laid out in CSS pixels, so every threshold is multiplied by the
# page zoom (``scale``) — otherwise a zoomed window would drag from the wrong
# band and swallow clicks meant for the page.
#
# Layout: the top RESIZE_STRIP px belong to the resize strips, the drag zone is
# the rest of the 42 px title bar, and the right TITLEBAR_BUTTON_ZONE px hold the
# min/max/close buttons which must keep receiving their own HTML clicks.
TITLEBAR_RESIZE_STRIP = 6
TITLEBAR_HEIGHT = 42
TITLEBAR_BUTTON_ZONE = 150
DOUBLE_CLICK_MAX_GAP_SECONDS = 0.35
DOUBLE_CLICK_MAX_DRIFT = 12


def classify_titlebar_press(
    x: int,
    y: int,
    view_width: int,
    *,
    last_x: int,
    last_y: int,
    seconds_since_last: float,
    double_click: bool = False,
    scale: float = 1.0,
) -> str:
    """Decide what a left press inside the view does: move / maximize / none.

    ``double_click`` events follow the same zone rules — a double click on
    page content must stay with the WebEngine (A09), only the title bar
    toggles the window state.
    """

    factor = scale if scale > 0 else 1.0
    drag_top = TITLEBAR_RESIZE_STRIP * factor
    drag_bottom = TITLEBAR_HEIGHT * factor
    button_zone = TITLEBAR_BUTTON_ZONE * factor
    if not (drag_top <= y < drag_bottom):
        return "none"
    if x >= view_width - button_zone:
        return "none"
    if double_click:
        return "maximize"
    if (
        0 < seconds_since_last < DOUBLE_CLICK_MAX_GAP_SECONDS
        and abs(x - last_x) < DOUBLE_CLICK_MAX_DRIFT * factor
        and abs(y - last_y) < DOUBLE_CLICK_MAX_DRIFT * factor
    ):
        return "maximize"
    return "move"


# Page-zoom range. The lower bound keeps the layout usable at the 800x600
# minimum window size, the upper bound stops a runaway Ctrl+wheel.
ZOOM_MIN = 0.6
ZOOM_MAX = 2.0


def clamp_zoom(factor: float) -> float:
    """Clamp a requested page zoom into the supported range."""

    try:
        value = float(factor)
    except (TypeError, ValueError):
        return 1.0
    if value != value:  # NaN
        return 1.0
    return max(ZOOM_MIN, min(ZOOM_MAX, value))


def bank_logs(
    logs: object,
    pending: list[dict[str, Any]],
    pending_ids: set[str],
    delivered_ids: set[str],
    limit: int,
) -> None:
    """Bank log lines that the page has not been sent yet.

    A run emits one log line per text group, and every progress snapshot used to
    re-ship the last 200 of them — measured at ~105 KB of JSON per tick, several
    times a second, serialised on the GUI thread. Only new lines travel now.

    Identity is the line's ``id`` rather than its position: snapshots are
    coalesced (a replaced snapshot never reaches the page, and its lines must not
    be marked sent), and the controller trims its own buffer, so a positional
    cursor could silently drop a line the page never received.
    """

    if not isinstance(logs, list):
        return
    for entry in logs:
        if not isinstance(entry, dict):
            continue
        entry_id = entry.get("id")
        if isinstance(entry_id, str):
            if entry_id in delivered_ids or entry_id in pending_ids:
                continue
            pending_ids.add(entry_id)
        pending.append(entry)
    overflow = len(pending) - limit
    if overflow <= 0:
        return
    for entry in pending[:overflow]:
        entry_id = entry.get("id")
        if isinstance(entry_id, str):
            pending_ids.discard(entry_id)
    del pending[:overflow]


REASON_ZH = {
    **FAILURE_REASON_LABELS,
    "unreferenced natural-language entity display name": "未发现名称匹配引用，可安全翻译显示名。",
    "selector or NBT condition references this name": "命令或条件引用了该名称，必须保持原样。",
    "reference coverage incomplete; entity display name kept for review": "引用覆盖不完整，身份名保留待确认。",
    "nested_writeback_unsupported": "深层嵌套暂不写回，保留原文待确认。",
    "filtered_detail_limit": "问题审阅仅显示部分样本，完整数量以统计为准。",
    "unfinished_translation": "可译文本未落盘：运行结束时仍是原文，请重试或手动修订。",
}


def chinese_reason(code: str | None) -> str:
    text = str(code or "").strip()
    if not text:
        return "未分类"
    if text in REASON_ZH:
        return REASON_ZH[text]
    if text in FAILURE_REASON_LABELS:
        return FAILURE_REASON_LABELS[text]
    return "未分类"


def _locator_obj(locator: str) -> object:
    try:
        parsed = json.loads(locator)
    except (TypeError, ValueError, json.JSONDecodeError):
        return locator
    return parsed


def short_location(file_path: str, locator: str, format_name: str = "") -> str:
    parsed = _locator_obj(locator)
    if isinstance(parsed, dict):
        path = parsed.get("path")
        if isinstance(path, list) and path:
            tail = " / ".join(str(part) for part in path[-3:])
            if any(str(part).lower() == "pages" for part in path):
                return f"书本 / {tail}"
            if str(path[-1]).lower() in {"customname", "custom_name"}:
                return f"实体 / {path[-1]}"
            if str(path[-1]).lower() in {"text", "messages"}:
                return f"文本 / {tail}"
            return tail
        if parsed.get("key"):
            return str(parsed.get("key"))
    file_name = str(file_path or "").replace("\\", "/").split("/")[-1]
    return file_name or format_name or "未知位置"


def content_role(record: dict[str, Any]) -> str:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    kind = str(record.get("display_kind") or metadata.get("display_kind") or "")
    container = str(record.get("container_type") or metadata.get("container_type") or "")
    format_name = str(record.get("format_name") or record.get("format") or "")
    locator = str(record.get("locator") or "")
    if kind == "entity_display_name" or "customname" in locator.lower():
        entity_type = str(metadata.get("entity_type") or "")
        if "villager" in entity_type or "wandering_trader" in entity_type:
            return "商人头顶标签"
        return ROLE_LABELS["entity_display_name"]
    if container in ROLE_LABELS:
        return ROLE_LABELS[container]
    if "pages" in locator.lower():
        return ROLE_LABELS["book"]
    if "TextDisplay" in locator or "text_display" in locator.lower():
        return ROLE_LABELS["text-display"]
    if any(token in locator.lower() for token in ("/dialog", "/dialogue", "/options/", "/choices/")):
        return ROLE_LABELS["dialogue"]
    if format_name in ROLE_LABELS:
        return ROLE_LABELS[format_name]
    return "扫描内容"


def filter_type(policy: str) -> str:
    if policy == "INFO":
        return "info"
    if policy == POLICY_TRANSLATABLE:
        return "display"
    if policy == POLICY_IMMUTABLE_MATCH or policy == "IMMUTABLE_ID":
        return "lock"
    if policy == POLICY_UNKNOWN:
        return "unknown"
    return "unknown"


def tag_for(policy: str, *, preference_keep: bool = False) -> str:
    if policy == "INFO":
        return "提示"
    if preference_keep:
        return "偏好：保留原文"
    if policy == POLICY_TRANSLATABLE:
        return "可翻译"
    if policy == POLICY_IMMUTABLE_MATCH or policy == "IMMUTABLE_ID":
        return "机制锁定"
    if policy == POLICY_UNKNOWN:
        return "保留待确认"
    return "待确认"


def is_npc_display(record: dict[str, Any]) -> bool:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    return str(record.get("display_kind") or metadata.get("display_kind") or "") == "entity_display_name"


# Records built from a *finished* run's report. Only these can be judged as
# "translatable text that never landed": a scan-preview record has an empty
# ``final_target`` because nothing has run yet, not because a translation was
# missed, and the report is the only place that says how a unit ended.
REPORT_RECORD_KIND = "report"
UNFINISHED_REASON = "unfinished_translation"
# Write-back outcomes that are decisions rather than misses: the writer ran
# (``changed`` / ``unchanged``) or refused an unverifiable rewrite (``blocked``).
_DECIDED_WRITEBACK = {"changed", "unchanged", "blocked"}


def report_unit_unfinished(raw: dict[str, Any]) -> bool:
    """True when a finished report shows this translatable text left in English.

    ``mechanism_locked`` units and other non-translatable policies are deliberate
    keeps, a write-back that ran or was refused is a decision, and a source the
    provider kept on purpose (``intentional_keep``: a keepable short label) is a
    decision too — RoH's 1101 preserved texts are 254 locked identifiers, 30
    reverted candidates and 817 missed ones, so they cannot be lumped together.
    A report unit carries no per-unit failure reason (that lives in the JSONL
    event log), so the only unambiguous miss it can show is a text with no final
    target at all that was neither kept on purpose nor written back: that is
    ``pending``, ``candidate_rejected`` and ``writeback_failed`` together.
    """

    if raw.get("record_kind") != REPORT_RECORD_KIND:
        return False
    if not should_send_to_ai(str(raw.get("policy") or "")):
        return False
    if raw.get("intentional_keep"):
        return False
    if raw.get("final_target"):
        return False
    return str(raw.get("writeback_status") or "") not in _DECIDED_WRITEBACK


def present_record(raw: dict[str, Any], *, translate_npc: bool = True) -> dict[str, Any]:
    policy = str(raw.get("policy") or "")
    source = str(raw.get("source_text") or raw.get("source") or "")
    file_path = str(raw.get("file_path") or raw.get("file") or "")
    locator = str(raw.get("locator") or "")
    format_name = str(raw.get("format_name") or raw.get("format") or "")
    key = str(raw.get("unit_key") or "")
    if not key and source:
        key = unit_key([], file_path, format_name, locator, source)
    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    preference_keep = (not translate_npc) and is_npc_display(raw) and policy == POLICY_TRANSLATABLE
    editable = bool(should_send_to_ai(policy) and not preference_keep and raw.get("record_kind") != "filtered")
    reason_code = str(raw.get("reason") or raw.get("policy_reason") or raw.get("reason_code") or "")
    target = raw.get("final_target") or raw.get("target_text") or ""
    unfinished = report_unit_unfinished(raw)
    if unfinished:
        # The report has no per-unit failure reason to show, so replace the
        # extraction reason ("language-file value") with the fact the user needs.
        reason_code = UNFINISHED_REASON
    issue = bool(raw.get("is_problem")) or policy == POLICY_UNKNOWN or raw.get("status") in {"失败", "待处理", "审计"} or unfinished
    if raw.get("record_kind") == "info":
        issue = False
    if policy in {POLICY_IMMUTABLE_MATCH, "IMMUTABLE_ID"}:
        issue = False
    presented = {
        "id": key or str(raw.get("id") or ""),
        "unit_key": key,
        "source": source,
        "target": target,
        "policy": policy,
        "type": filter_type(policy),
        "role": content_role({**raw, "format_name": format_name, "metadata": metadata}),
        "why": chinese_reason(reason_code),
        "why_code": reason_code or "unclassified",
        "path": short_location(file_path, locator, format_name),
        "full_path": file_path,
        "archive_id": str(raw.get("archive_id") or metadata.get("archive_id") or ""),
        "archive_status": str(raw.get("archive_status") or metadata.get("archive_status") or ""),
        "audit_category": str(raw.get("audit_category") or metadata.get("audit_category") or ""),
        "locator": locator,
        "format_name": format_name,
        "editable": editable,
        "preference_keep": preference_keep,
        "tag": tag_for(policy, preference_keep=preference_keep),
        "issue": issue,
        "display_kind": str(raw.get("display_kind") or metadata.get("display_kind") or ""),
        "will_send_ai": editable,
        "container_type": str(raw.get("container_type") or metadata.get("container_type") or ""),
        "references": list(raw.get("reference_sources") or metadata.get("reference_sources") or []),
        "stage": str(raw.get("stage") or metadata.get("stage") or ("filtered" if raw.get("record_kind") == "filtered" else "extracted")),
    }
    if not presented["id"]:
        presented["id"] = make_problem_id("preview", file_path + locator + source, presented["why_code"])
    return presented


def present_scan_result(result: Any, *, translate_npc: bool = True) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for unit in getattr(result, "text_units", []):
        records.append(
            present_record(
                {
                    "file_path": unit.file_path,
                    "locator": unit.locator,
                    "format": unit.format,
                    "source_text": unit.source_text,
                    "policy": unit.policy,
                    "policy_reason": unit.policy_reason,
                    "final_target": unit.final_target or "",
                    "metadata": dict(unit.metadata or {}),
                    "display_kind": (unit.metadata or {}).get("display_kind", ""),
                    "reference_sources": list(getattr(unit, "reference_sources", None) or (unit.metadata or {}).get("reference_sources") or []),
                    "stage": "extracted",
                },
                translate_npc=translate_npc,
            )
        )
    for detail in getattr(result, "filtered_details", []) or []:
        records.append(
            present_record(
                {
                    "file_path": detail.get("file", ""),
                    "locator": detail.get("locator", ""),
                    "format": detail.get("format", ""),
                    "source_text": detail.get("source", ""),
                    "policy": detail.get("policy", "") or POLICY_UNKNOWN,
                    "reason": detail.get("reason", ""),
                    "record_kind": "filtered",
                    "metadata": detail,
                },
                translate_npc=translate_npc,
            )
        )
    detail_total = int(getattr(result, "filtered_detail_total", 0) or 0)
    shown = len(getattr(result, "filtered_details", []) or [])
    if detail_total > shown:
        records.append(
            present_record(
                {
                    "source_text": f"问题审阅显示 {shown} / 共 {detail_total} 条样本。",
                    "policy": "INFO",
                    "reason": "filtered_detail_limit",
                    "record_kind": "info",
                },
                translate_npc=translate_npc,
            )
        )
    return records


def npc_skip_keys(records: list[dict[str, Any]], translate_npc: bool) -> set[str]:
    if translate_npc:
        return set()
    return {str(record["id"]) for record in records if record.get("preference_keep") and record.get("id")}


def issue_count(records: list[dict[str, Any]]) -> int:
    """Scan-phase review items: un-audited fields and filtered values."""

    seen: set[str] = set()
    for record in records:
        if not record.get("issue"):
            continue
        seen.add(str(record.get("id") or record.get("why_code")))
    return len(seen)


# ``review_rejected`` is a second label attached to an already-counted
# validation failure, so counting it separately would double the same unit.
_SECONDARY_FAILURE_LABELS = {"review_rejected"}


def runtime_issue_count(failure_counts: dict[str, int] | None, validation_failed: int = 0) -> int:
    """Runtime problems a user must review: validation failures and request errors.

    Scan-phase records alone under-report badly: a run where the server returns
    503 for every group keeps 414 texts in English yet reports no scan issue at
    all (RoH, 2026-09-18). Service failures, format errors and quality
    rejections have to reach the same counter.
    """

    distinct = sum(
        count
        for reason, count in (failure_counts or {}).items()
        if reason not in _SECONDARY_FAILURE_LABELS
    )
    return max(int(validation_failed or 0), distinct)


def stats_from_progress(event: ProgressEvent, records: list[dict[str, Any]] | None = None) -> dict[str, int]:
    """Card values for one progress event.

    ``units_translated`` counts units whose final text differs from the source,
    whoever produced it (AI, memory, glossary, manual edit) — the same rule the
    final report uses, so the running figure and the finished one agree. It is
    not an "AI review passed" figure: a run that keeps every text in English
    reports 0, and one that reuses memory reports more than the AI did.

    It is also allowed to *fall*: write-back re-validates every candidate and
    reverts one it refuses, so a count that only ever grew would be reporting a
    state that no longer exists. Task-wide totals come from ``scanned_total`` /
    ``groups_done``; the per-package figures (``scanned``, ``local_groups_*``)
    describe the current package and must never overwrite them.
    """

    translated = int(getattr(event, "units_translated", 0) or 0)
    scanned = int(event.scanned_total or event.scanned or 0)
    groups = int(event.groups_done or 0)
    scan_issues = issue_count(records or [])
    runtime_issues = runtime_issue_count(
        getattr(event, "failure_counts", None),
        int(getattr(event, "units_validation_failed", 0) or 0),
    )
    return {
        "scanned": scanned,
        "groups": groups,
        "translated": translated,
        "ai_passed": translated,
        # These two explain a fall in ``translated``: a candidate write-back
        # refused becomes "original preserved", and a rejected candidate never
        # reaches the final text. Without them a dropping number looks like a bug.
        "original_preserved": int(getattr(event, "units_original_preserved", 0) or 0),
        "validation_failed": int(getattr(event, "units_validation_failed", 0) or 0),
        "issues": scan_issues + runtime_issues,
        "scan_issues": scan_issues,
        "runtime_issues": runtime_issues,
        "in_flight": int(getattr(event, "in_flight", 0) or 0),
        "retries": int(getattr(event, "retries", 0) or sum((event.retry_counts or {}).values())),
    }


def stats_from_report(report: TranslationReport, records: list[dict[str, Any]] | None = None) -> dict[str, int]:
    translated = int(report.units_translated or 0)
    scan_issues = issue_count(records or [])
    runtime_issues = runtime_issue_count(
        getattr(report, "failure_counts", None),
        int(getattr(report, "units_validation_failed", 0) or 0),
    )
    return {
        "scanned": int(report.scanned or 0),
        "groups": int(report.groups_completed or 0),
        "translated": translated,
        "ai_passed": translated,
        "original_preserved": int(report.units_original_preserved or 0),
        "validation_failed": int(report.units_validation_failed or 0),
        "issues": scan_issues + runtime_issues,
        "scan_issues": scan_issues,
        "runtime_issues": runtime_issues,
    }


# Outcomes that mean "a translatable text is still not translated in the
# delivered output"; ``mechanism_locked``, ``source_preserved`` and
# ``intentional_keep`` stay out because they are deliberate keeps.
UNFINISHED_OUTCOMES = ("pending", "candidate_rejected", "writeback_failed")


def _report_value(report: Any, name: str, default: Any = None) -> Any:
    """Read one field from a ``TranslationReport`` or from its JSON dict."""

    if isinstance(report, dict):
        return report.get(name, default)
    return getattr(report, name, default)


def unfinished_units(report: Any) -> int:
    """Translatable units that never reached the output as Chinese.

    ``unit_outcomes`` is the report's mutually exclusive account and the only
    exact source. Reports written before it existed cannot be split this way —
    ``units_original_preserved`` also holds mechanism-locked identifiers (RoH:
    254 of 1101) and ``units_validation_failed`` counts rejected attempts rather
    than units — so such a report contributes only the one thing it states per
    unit, the failed write-backs, instead of claiming every preserved English
    text was a missed translation.
    """

    outcomes = _report_value(report, "unit_outcomes") or {}
    if isinstance(outcomes, dict) and outcomes:
        return sum(int(outcomes.get(name) or 0) for name in UNFINISHED_OUTCOMES)
    return int(_report_value(report, "units_writeback_failed", 0) or 0)


def unfinished_note(count: int) -> str:
    """The one wording for "N translatable texts are still English"."""

    return f"{count} 条可译文本未落盘" if count > 0 else ""


def unfinished_review_hint(count: int) -> str:
    """Same count plus where the user can act on it; empty when nothing is left."""

    note = unfinished_note(count)
    return f"{note}（仍为原文），请查看问题审阅与质量摘要" if note else ""


def completion_title(scanned: int, unfinished: int) -> str:
    """Terminal title of a published run: never an unconditional "完成"."""

    if not scanned:
        return "未找到可翻译文本"
    note = unfinished_note(unfinished)
    return f"汉化完成（{note}）" if note else "汉化完成"


def format_duration_ms(ms: float | int | None) -> str:
    total_ms = max(0, int(ms or 0))
    seconds = total_ms // 1000
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, second = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} 分 {second} 秒"
    hours, minute = divmod(minutes, 60)
    return f"{hours} 小时 {minute} 分"


def format_token_count(count: Any) -> str:
    if count is None:
        return "未知"
    try:
        value = int(count)
    except (TypeError, ValueError):
        return "未知"
    if value >= 10000:
        # 12345 -> "1.2 万"; 1_000_000 -> "100 万" (no dangling ".0").
        text = f"{value / 10000:.1f}".rstrip("0").rstrip(".")
        return f"{text} 万"
    return str(value)


def format_token_raw(count: Any) -> str:
    if count is None:
        return "未知"
    try:
        return f"{int(count):,}"
    except (TypeError, ValueError):
        return "未知"


def usage_line(usage: dict[str, Any] | None, reported_requests: int = 0) -> str:
    """Compact per-task token summary: "输入 9505，输出 10.3 万".

    Reasoning tokens and reported-request counts stay in ``usage_detail``
    (tooltip / log line) so the strip reads at a glance.
    """

    if not usage:
        return ""
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if not prompt and not completion:
        return ""
    return f"输入 {format_token_count(prompt)}，输出 {format_token_count(completion)}"


def usage_detail(usage: dict[str, Any] | None, reported_requests: int = 0) -> str:
    """Exact numbers for tooltips: cost math needs the raw counts."""

    if not usage:
        return ""
    parts = [
        f"输入 {format_token_raw(usage.get('prompt_tokens'))}",
        f"输出 {format_token_raw(usage.get('completion_tokens'))}",
    ]
    if usage.get("reasoning_tokens"):
        parts.append(f"思考 {format_token_raw(usage.get('reasoning_tokens'))}")
    if usage.get("cached_tokens"):
        parts.append(f"缓存命中 {format_token_raw(usage.get('cached_tokens'))}")
    if reported_requests:
        parts.append(f"{reported_requests} 次请求有用量回报")
    return " · ".join(parts)


def model_price_for(prices: Any, model: str) -> dict[str, Any] | None:
    if not isinstance(prices, dict):
        return None
    entry = prices.get(str(model or ""))
    return entry if isinstance(entry, dict) else None


def estimate_cost(usage: dict[str, Any] | None, price: dict[str, Any] | None) -> float | None:
    """Estimated spend in the price's currency for one task.

    Vendors that report ``reasoning_tokens`` bill them inside completion
    output (DeepSeek, GLM), so there is no separate term; when a vendor
    lists thinking tokens separately the configured output price already
    reflects the blended rate. A partially configured price (only the
    input or only the output rate) still yields an estimate; the missing
    side simply contributes nothing.
    """

    if not usage or not price:
        return None
    try:
        input_per_m = float(price.get("input_per_m") or 0)
        output_per_m = float(price.get("output_per_m") or 0)
    except (TypeError, ValueError):
        return None
    if input_per_m <= 0 and output_per_m <= 0:
        return None
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    if prompt <= 0 and completion <= 0:
        return None
    return (prompt * input_per_m + completion * output_per_m) / 1_000_000


def format_amount(cost: float | None, currency: str = "CNY") -> str:
    if cost is None:
        return ""
    if str(currency or "CNY").upper() == "USD":
        return f"预计花费 ${cost:.2f}"
    return f"预计花费 {cost:.2f} 元"


def metrics_texts(
    *,
    elapsed_ms: float | int = 0,
    usage: dict[str, Any] | None = None,
    reported_requests: int = 0,
    price: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Display-ready strings for the metrics strip under the stat cards."""

    cost = estimate_cost(usage, price)
    return {
        "elapsed_text": format_duration_ms(elapsed_ms) if elapsed_ms else "",
        "usage_text": usage_line(usage, reported_requests),
        "cost_text": format_amount(cost, str((price or {}).get("currency") or "CNY")),
        "usage_detail": usage_detail(usage, reported_requests),
    }


def queue_advance(state: str, *, auto_continue: bool, has_queue: bool) -> str:
    """Queue policy: success advances automatically, any other terminal
    state (failed / paused / blocked) waits for the user."""

    if not (auto_continue and has_queue):
        return "wait"
    return "next" if state == "complete" else "wait"


#: States in which the current input has not been processed yet. While one of
#: these is current, the queue head is *not* "next": starting it would replace the
#: pending input (``set_input`` clears ``manual_targets`` and the records) and the
#: displaced pack is never put back in the queue — a silent task loss for exactly
#: the drop pattern this round introduced.
PENDING_INPUT_STATES = frozenset({"idle", "ready", "scanning", "scanned", "translating", "stopping"})


def queue_start_state(state: str, *, busy: bool, has_input: bool, has_queue: bool) -> str:
    """``enabled`` / ``disabled`` for the queue's explicit "start next" entry."""

    if busy or not has_queue:
        return "disabled"
    if has_input and state in PENDING_INPUT_STATES:
        return "disabled"
    return "enabled"


def drop_plan(*, has_input: bool, busy: bool, count: int, has_queue: bool = False) -> tuple[int, int]:
    """How one drop splits between the current input and the queue.

    A drop only ever claims an *empty* input slot (2026-09-24): every later drop
    queues, whether or not a task is running.  A single-file drop used to replace
    the current input while idle, so dragging three packs in a row silently kept
    only the last one — the user saw "the earlier ones vanish" and had no way to
    line up a batch.  Replacing the input stays on the「选择资源」button, which is
    an explicit act; dragging always means "line this up behind what I chose".

    A non-empty queue also blocks the claim: the queue already holds the batch
    this drop is joining, so the new path must not jump in front of it.

    Returns ``(paths_that_become_the_input, paths_that_queue)``.
    """

    if count <= 0:
        return 0, 0
    if has_input or busy or has_queue:
        return 0, count
    return 1, count - 1


def button_for_state(
    state: str, *, has_input: bool = False, complete: bool = False, has_queue: bool = False
) -> dict[str, str]:
    mapping = {
        "idle": {"scan": "disabled", "run": "disabled", "run_label": "开始汉化"},
        "ready": {"scan": "enabled", "run": "enabled", "run_label": "开始汉化"},
        "scanning": {"scan": "disabled", "run": "disabled", "run_label": "开始汉化"},
        "scanned": {"scan": "enabled", "run": "enabled", "run_label": "开始汉化"},
        "translating": {"scan": "disabled", "run": "stop", "run_label": "停止汉化"},
        "stopping": {"scan": "disabled", "run": "disabled", "run_label": "正在停止"},
        "paused": {"scan": "enabled", "run": "continue", "run_label": "继续汉化"},
        "failed": {"scan": "enabled", "run": "enabled", "run_label": "重新尝试"},
        "blocked": {"scan": "enabled", "run": "reaudit", "run_label": "重新审计并打包"},
        "complete": {"scan": "enabled", "run": "summary", "run_label": "查看质量摘要"},
    }
    if complete:
        state = "complete"
    if state == "idle" and has_input:
        state = "ready"
    if state == "complete" and has_queue:
        # 完成态 + 还有排队项：运行按钮必须是**显式**启动队首的入口。完成态把
        # 按钮让给质量摘要（摘要另有底部链接），而自动继续只由完成回调触发——
        # 用户在这之后才拖进来的包会入队却永远等不到启动（2026-09-24 缺陷）。
        return {"scan": "enabled", "run": "queue", "run_label": "开始下一个排队任务"}
    return mapping.get(state, mapping["idle"])
