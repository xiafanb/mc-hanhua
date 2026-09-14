from __future__ import annotations

import html
import json
from dataclasses import asdict
from pathlib import Path

from .models import ArchiveResult, TextUnit, TranslationReport, summarize_failure_counts


def write_json_report(path: Path, report: TranslationReport, units: list[TextUnit]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    report_payload = asdict(report)
    report_payload["input_path"] = str(report.input_path)
    report_payload["output_path"] = str(report.output_path)
    report_payload["archive_results"] = [item.to_dict() if isinstance(item, ArchiveResult) else item for item in report.archive_results]
    payload = {
        "report": report_payload,
        "units": [
            {
                "id": unit.id,
                "source_text": unit.source_text,
                "final_target": unit.final_target,
                "file_path": unit.file_path,
                "format": unit.format,
                "locator": unit.locator,
                "context": unit.context,
                "risk_level": unit.risk_level.value,
                "policy": unit.policy,
                "policy_reason": unit.policy_reason,
                "reference_sources": list(unit.reference_sources),
                "object_key": unit.object_key,
                "canonical_key": unit.canonical_key,
                "translation_status": unit.metadata.get("translation_status", ""),
                "review_status": unit.metadata.get("review_status", ""),
                "writeback_status": unit.metadata.get("writeback_status", ""),
                "archive_id": unit.metadata.get("archive_id", ""),
                "archive_chain": list(unit.metadata.get("archive_chain") or []),
            }
            for unit in units
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_html_report(path: Path, report: TranslationReport, units: list[TextUnit]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for unit in units:
        rows.append(
            "<tr>"
            f"<td>{html.escape(unit.risk_level.value)}</td>"
            f"<td>{html.escape(unit.file_path)}</td>"
            f"<td>{html.escape(unit.context)}</td>"
            f"<td>{html.escape(unit.source_text)}</td>"
            f"<td>{html.escape(unit.final_target or '')}</td>"
            "</tr>"
        )
    body = "\n".join(rows)
    failure_samples = "".join(
        f"<li><strong>{html.escape(reason)}</strong>: {html.escape(sample)}</li>"
        for reason, sample in report.failure_samples.items()
    ) or "<li>none</li>"
    warnings = "".join(f"<li>{html.escape(warning)}</li>" for warning in report.warnings) or "<li>none</li>"
    empty_notice = (
        "<p><strong>未发现可翻译文本：未产生任何 AI 请求，输出为输入的原样副本。</strong></p>"
        if report.scanned == 0
        else ""
    )
    match_labels = {"full": "完整复用", "partial": "部分匹配", "existing": "包内已有汉化", "none": "未覆盖"}
    coverage_rows = "\n".join(
        "<tr>"
        f"<td>{html.escape(entry.archive or '(顶层目录)')}</td>"
        f"<td>{html.escape(entry.modid)}</td>"
        f"<td>{html.escape(entry.source or '-')}</td>"
        f"<td>{html.escape(match_labels.get(entry.match_type, entry.match_type))}</td>"
        f"<td>{entry.keys_covered} / {entry.keys_total}</td>"
        f"<td>{entry.keys_ai}</td>"
        "</tr>"
        for entry in report.mod_coverage
    )
    coverage_section = (
        f"""
  <h2>模组资产库覆盖</h2>
  <table>
    <thead><tr><th>来源</th><th>Mod ID</th><th>资产来源</th><th>匹配类型</th><th>覆盖 / 总键数</th><th>AI 翻译</th></tr></thead>
    <tbody>{coverage_rows}</tbody>
  </table>"""
        if report.mod_coverage
        else ""
    )
    conflict_rows = "".join(
        "<li>"
        f"<strong>{html.escape(str(entry.get('source', '')))}</strong>（归一化 {html.escape(str(entry.get('normalized', '')))}）："
        + "；".join(
            f"{html.escape(str(item.get('target', '')))} × {item.get('count', 0)}"
            + (
                "（" + "、".join(f"{html.escape(str(sample.get('file', '')))}#{html.escape(str(sample.get('context', '')))}" for sample in item.get("samples", [])) + "）"
                if item.get("samples")
                else ""
            )
            for item in entry.get("targets", [])
        )
        + "</li>"
        for entry in report.consistency_conflicts
    )
    conflict_section = (
        f"""
  <h2>术语一致性冲突</h2>
  <p>同一原文在不同上下文中保留了多种译文（多义普通词不机械统一，机制对象与强制术语优先统一）。</p>
  <ul>{conflict_rows}</ul>"""
        if report.consistency_conflicts
        else ""
    )
    ref_index = report.reference_index or {}
    ref_section = ""
    if ref_index:
        locked = "、".join(html.escape(str(value)) for value in ref_index.get("locked_values", [])[:20]) or "-"
        unresolved_rows = "".join(
            f"<li>{html.escape(str(item.get('object_key') or item.get('normalized') or ''))} "
            f"({html.escape(str(item.get('kind') or ''))})</li>"
            for item in ref_index.get("unresolved", [])[:20]
        ) or "<li>none</li>"
        ambiguous_rows = "".join(
            f"<li>{html.escape(str(item.get('value') or ''))}: "
            f"{html.escape(', '.join(str(key) for key in item.get('object_keys', [])))} "
            f"— {html.escape(str(item.get('reason') or ''))}</li>"
            for item in ref_index.get("ambiguous", [])[:20]
        ) or "<li>none</li>"
        candidate_rows = "".join(
            f"<li>{html.escape(str(item.get('object_key') or ''))}: "
            f"{html.escape(' / '.join(str(target) for target in item.get('targets', [])))}</li>"
            for item in ref_index.get("display_candidates", [])[:20]
        ) or "<li>none</li>"
        def_count = len(ref_index.get("definitions", []))
        use_count = len(ref_index.get("uses", []))
        ref_section = f"""
  <h2>跨文件引用索引</h2>
  <p>条目 {ref_index.get('entry_count', 0)}；定义 {def_count}；使用 {use_count}；锁定值 {ref_index.get('locked_value_count', 0)}（{locked}）。ID 永不翻译；精确匹配值锁定；未解析引用记 UNKNOWN_REVIEW。</p>
  <h3>未解析 / 无定义引用</h3>
  <ul>{unresolved_rows}</ul>
  <h3>歧义引用</h3>
  <ul>{ambiguous_rows}</ul>
  <h3>同一对象的多个候选译文</h3>
  <ul>{candidate_rows}</ul>"""
    audit = report.audit
    if audit:
        audit_blocked = bool(audit.get("blocked"))
        audit_rows = "".join(
            f"<li><strong>[{'阻断' if issue.get('level') == 'error' else issue.get('level')}]</strong> "
            f"{html.escape(str(issue.get('category', '')))}：{html.escape(str(issue.get('file', '')))} — {html.escape(str(issue.get('message', '')))}"
            "</li>"
            for issue in audit.get("errors", []) + audit.get("warnings", []) + audit.get("infos", [])
        )
        audit_section = f"""
  <h2>发布前安全审计</h2>
  <p>重新扫描输出：{audit.get('rescanned_units', 0)} 条文本；
  机制锁定值 {audit.get('locked_ref_count', 0)} 个（样本：{html.escape('、'.join(audit.get('locked_ref_samples', [])) or '-')}）；
  阻断问题 {audit.get('error_count', 0)}，警告 {audit.get('warning_count', 0)}，提示 {audit.get('info_count', 0)}。
  {"<strong>审计失败：存在阻断问题，未打包发布。</strong>" if audit_blocked else "审计通过：结构、机制引用与资源 ID 校验未发现阻断问题。"}</p>
  <ul>{audit_rows or '<li>无问题</li>'}</ul>"""
    else:
        audit_section = ""
    archive_rows = "".join(
        "<li>"
        f"<strong>{html.escape(item.archive_id)}</strong> "
        f"状态 {html.escape(item.status)} / 发布 {html.escape(item.publication_status)}；"
        f"扫描 {item.scanned}；问题 {len(item.audit_issues)}"
        + ("：" + html.escape(str(item.audit_issues[0].get('message') or '')) if item.audit_issues else "")
        + "</li>"
        for item in report.archive_results
    )
    archive_section = (
        f"""
  <h2>数据包结果</h2>
  <p>内嵌包无论是否发布都会保留扫描条目与审计问题；未通过的包不会写入最终输出。</p>
  <ul>{archive_rows or '<li>无内嵌包</li>'}</ul>"""
        if report.archive_results
        else ""
    )
    filter_rows = "".join(
        "<li>"
        f"<strong>{html.escape(str(item.get('reason', '')))}</strong> "
        f"{html.escape(str(item.get('file', '')))} "
        f"#{html.escape(str(item.get('locator', '')))} "
        f"{html.escape(str(item.get('source', '')))} "
        f"({html.escape(str(item.get('policy_reason') or item.get('policy') or ''))})"
        "</li>"
        for item in report.filtered_details[:80]
    )
    filter_section = (
        f"""
  <h2>提取过滤明细</h2>
  <p>以下为脱敏、限长后的过滤样本（共 {len(report.filtered_details)} 条）。未知字段默认不送 AI。</p>
  <ul>{filter_rows or '<li>none</li>'}</ul>"""
        if report.filtered_details
        else ""
    )
    nbt_diff = report.nbt_text_diff or {}
    nbt_section = ""
    if nbt_diff:
        nbt_section = f"""
  <h2>NBT 文本对比（只读）</h2>
  <p>提取 {nbt_diff.get('extracted', 0)}；输入英文 {nbt_diff.get('input_english', 0)}；AI 成功 {nbt_diff.get('ai_success', 0)}；仍为英文 {nbt_diff.get('still_english', 0)}；实际写回变化 {nbt_diff.get('writeback_changed', 0)}；写回失败 {nbt_diff.get('writeback_failed', 0)}。</p>
  <p>{html.escape(str(nbt_diff.get('note') or ''))}</p>"""
    conflict_items = "".join(
        "<li>"
        f"<strong>{html.escape(str(item.get('name', '')))}</strong> × {item.get('count', 0)}："
        + "、".join(html.escape(path) for path in item.get("paths", []))
        + f" — {html.escape(str(item.get('note', '')))}"
        "</li>"
        for item in report.structure_conflicts
    )
    structure_section = (
        f"""
  <h2>同名结构文件（只读，不改机制）</h2>
  <p>结构源已翻译不等于已生成世界已更新。</p>
  <ul>{conflict_items}</ul>"""
        if report.structure_conflicts
        else ""
    )
    page = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>mc-hanhua report</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 24px; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border: 1px solid #ddd; padding: 8px; vertical-align: top; }}
    th {{ background: #f5f5f5; text-align: left; }}
  </style>
</head>
<body>
  <h1>mc-hanhua report</h1>
  {empty_notice}
  <p>Scanned: {report.scanned} | Translated: {report.translated} | Reused: {report.reused} | Skipped: {report.skipped} | High risk: {report.high_risk}</p>
  <p>Units: total {report.units_total} | translated {report.units_translated} | original preserved {report.units_original_preserved} | validation failed {report.units_validation_failed} | writeback failed {report.units_writeback_failed}</p>
  <p>Text groups: {report.groups_completed} / {report.groups_total} | Failed groups: {report.failed_groups} | Reviewed: {report.reviewed_groups} | Review pending: {report.review_pending_groups} | Review corrections: {report.review_corrected}</p>
  <p>Planned calls: draft {report.planned_draft_calls} + review {report.planned_review_calls} | Seeded drafts: {report.seeded_drafts} | Actual API requests: {report.api_requests} | Rate-limit cooldowns: {report.rate_limit_cooldowns} | Checkpoints: {report.checkpointed_groups}</p>
  <p>API-reported tokens: input {report.token_usage.get('prompt_tokens', 'unavailable')} | output {report.token_usage.get('completion_tokens', 'unavailable')} | cached input {report.token_usage.get('cached_tokens', 'unavailable')} | reasoning {report.token_usage.get('reasoning_tokens', 'unavailable')} | Requests reporting usage: {report.usage_reported_requests}. Cached and reasoning tokens are subsets; missing usage is not zero.</p>
  <p>Layout reflow groups: {report.layout_reflow_groups} | Spatial sign clusters: {report.spatial_sign_clusters} | Same-source unified: {report.consistency_unified}</p>
  <p>Filtered before AI: {html.escape(summarize_failure_counts(report.filtered_counts) or 'none')}</p>
  <p>Failure reasons: {html.escape(summarize_failure_counts(report.failure_counts) or 'none')} | Retries: {html.escape(summarize_failure_counts(report.retry_counts) or 'none')}</p>
  <h2>诊断与警告</h2>
  <ul>{warnings}</ul>
  {filter_section}
  {nbt_section}
  {structure_section}
  {audit_section}
  {archive_section}
  {coverage_section}
  {ref_section}
  {conflict_section}
  <h2>Failure samples</h2>
  <ul>{failure_samples}</ul>
  <table>
    <thead><tr><th>Risk</th><th>File</th><th>Context</th><th>Source</th><th>Target</th></tr></thead>
    <tbody>{body}</tbody>
  </table>
</body>
</html>
"""
    path.write_text(page, encoding="utf-8")
