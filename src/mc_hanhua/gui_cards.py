"""Compact, selectable content cards for the desktop result lists."""
from __future__ import annotations

from html import escape

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QTextDocument
from PySide6.QtWidgets import QStyle, QStyledItemDelegate


class ContentCardDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        record = index.data(Qt.ItemDataRole.UserRole) or {}
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        policy = record.get("policy", "")
        label = record.get("status") or {
            "TRANSLATABLE_DISPLAY": "可翻译", "IMMUTABLE_MATCH_VALUE": "机制锁定",
            "UNKNOWN_REVIEW": "保留待确认", "IMMUTABLE_ID": "标识保护",
        }.get(policy, "待确认")
        protected = policy != "TRANSLATABLE_DISPLAY"
        color = "#946526" if protected else "#26763a"
        source = str(record.get("source_text") or record.get("message") or "任务提示")
        target = record.get("final_target") or record.get("target_text")
        detail = str(record.get("reason") or record.get("message") or record.get("file_path") or "选择查看位置与处理依据")
        path = str(record.get("locator") or record.get("file_path") or "")
        expanded = bool(record.get("_expanded"))
        # Full, untruncated content remains available in the detail pane.
        def short(value, limit):
            value = str(value)
            return escape(value[:limit] + ("…" if len(value) > limit else ""))
        document = QTextDocument()
        document.setDefaultFont(option.font)
        document.setDocumentMargin(0)
        role = record.get("role") or record.get("locator") or record.get("format_name") or "扫描内容"
        document.setHtml(
            f'<table width="100%"><tr><td><b style="font-size:15px;color:#10243d">{escape(str(role))}</b></td>'
            f'<td align="right"><span style="color:{color};background:#eaf7ed">{escape(str(label))}</span></td></tr></table>'
            f'<div style="font-size:14px;color:#10243d;margin-top:8px">{short(source, 100)}</div>'
            f'<div style="font-size:13px;color:#26763a;margin-top:7px">'
            f'{"译文：" + short(target, 90) if target else "保留原文" if protected else "等待翻译"}</div>'
            f'<div style="font-size:12px;color:#69809c;margin-top:7px">{short(detail, 80)}</div>'
            f'<div style="font-size:12px;color:#356a46;margin-top:9px">{"▼" if expanded else "▶"} 查看位置与处理依据</div>'
            + (f'<div style="font-size:12px;color:#69809c;margin-top:4px">{short(path, 100)}</div>' if expanded else '')
        )
        document.setTextWidth(max(100, option.rect.width() - 32))
        painter.save()
        painter.fillRect(option.rect, QColor("#eaf7ed" if selected else "#fbfdff"))
        painter.setPen(QColor("#dce7f1"))
        painter.drawLine(option.rect.bottomLeft(), option.rect.bottomRight())
        painter.setClipRect(option.rect.adjusted(12, 8, -12, -8))
        painter.translate(option.rect.left() + 16, option.rect.top() + 12)
        document.drawContents(painter)
        painter.restore()

    def sizeHint(self, option, index):
        return QSize(300, 210 if not index.data(Qt.ItemDataRole.UserRole).get("_expanded") else 292)
