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

# Frameless title-bar hit testing. The top 10 px belong to the resize strips,
# y in [10, 36) is the drag zone, and the right 140 px hold the min/max/close
# buttons which must keep receiving HTML clicks.
TITLEBAR_DRAG_TOP = 10
TITLEBAR_DRAG_BOTTOM = 36
TITLEBAR_BUTTON_ZONE = 140
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
) -> str:
    """Decide what a left press inside the view does: move / maximize / none.

    ``double_click`` events follow the same zone rules — a double click on
    page content must stay with the WebEngine (A09), only the title bar
    toggles the window state.
    """

    if not (TITLEBAR_DRAG_TOP <= y < TITLEBAR_DRAG_BOTTOM):
        return "none"
    if x >= view_width - TITLEBAR_BUTTON_ZONE:
        return "none"
    if double_click:
        return "maximize"
    if (
        0 < seconds_since_last < DOUBLE_CLICK_MAX_GAP_SECONDS
        and abs(x - last_x) < DOUBLE_CLICK_MAX_DRIFT
        and abs(y - last_y) < DOUBLE_CLICK_MAX_DRIFT
    ):
        return "maximize"
    return "move"

REASON_ZH = {
    **FAILURE_REASON_LABELS,
    "unreferenced natural-language entity display name": "未发现名称匹配引用，可安全翻译显示名。",
    "selector or NBT condition references this name": "命令或条件引用了该名称，必须保持原样。",
    "reference coverage incomplete; entity display name kept for review": "引用覆盖不完整，身份名保留待确认。",
    "nested_writeback_unsupported": "深层嵌套暂不写回，保留原文待确认。",
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
    if policy == POLICY_TRANSLATABLE:
        return "display"
    if policy == POLICY_IMMUTABLE_MATCH or policy == "IMMUTABLE_ID":
        return "lock"
    if policy == POLICY_UNKNOWN:
        return "unknown"
    return "unknown"


def tag_for(policy: str, *, preference_keep: bool = False) -> str:
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
    issue = bool(raw.get("is_problem")) or policy == POLICY_UNKNOWN or raw.get("status") in {"失败", "待处理", "审计"}
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
    return records


def npc_skip_keys(records: list[dict[str, Any]], translate_npc: bool) -> set[str]:
    if translate_npc:
        return set()
    return {str(record["id"]) for record in records if record.get("preference_keep") and record.get("id")}


def issue_count(records: list[dict[str, Any]]) -> int:
    seen: set[str] = set()
    for record in records:
        if not record.get("issue"):
            continue
        seen.add(str(record.get("id") or record.get("why_code")))
    return len(seen)


def stats_from_progress(event: ProgressEvent, records: list[dict[str, Any]] | None = None) -> dict[str, int]:
    ai_passed = int(getattr(event, "units_translated", 0) or 0)
    scanned = int(event.scanned_total or event.scanned or 0)
    groups = int(event.groups_done or 0)
    return {
        "scanned": scanned,
        "groups": groups,
        "ai_passed": ai_passed,
        "issues": issue_count(records or []),
        "in_flight": int(getattr(event, "in_flight", 0) or 0),
        "retries": int(getattr(event, "retries", 0) or sum((event.retry_counts or {}).values())),
    }


def stats_from_report(report: TranslationReport, records: list[dict[str, Any]] | None = None) -> dict[str, int]:
    return {
        "scanned": int(report.scanned or 0),
        "groups": int(report.groups_completed or 0),
        "ai_passed": int(report.units_translated or 0),
        "issues": issue_count(records or []),
    }


def button_for_state(state: str, *, has_input: bool = False, complete: bool = False) -> dict[str, str]:
    mapping = {
        "idle": {"scan": "disabled", "run": "disabled", "run_label": "开始汉化", "reset": "enabled"},
        "ready": {"scan": "enabled", "run": "enabled", "run_label": "开始汉化", "reset": "enabled"},
        "scanning": {"scan": "disabled", "run": "disabled", "run_label": "开始汉化", "reset": "disabled"},
        "scanned": {"scan": "enabled", "run": "enabled", "run_label": "开始汉化", "reset": "enabled"},
        "translating": {"scan": "disabled", "run": "stop", "run_label": "停止汉化", "reset": "disabled"},
        "stopping": {"scan": "disabled", "run": "disabled", "run_label": "正在停止", "reset": "disabled"},
        "paused": {"scan": "enabled", "run": "continue", "run_label": "继续汉化", "reset": "enabled"},
        "failed": {"scan": "enabled", "run": "enabled", "run_label": "重新尝试", "reset": "enabled"},
        "blocked": {"scan": "enabled", "run": "reaudit", "run_label": "重新审计并打包", "reset": "enabled"},
        "complete": {"scan": "enabled", "run": "summary", "run_label": "查看质量摘要", "reset": "enabled"},
    }
    if complete:
        state = "complete"
    if state == "idle" and has_input:
        state = "ready"
    return mapping.get(state, mapping["idle"])
