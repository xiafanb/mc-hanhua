from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QDragEnterEvent, QDropEvent, QFont, QFontDatabase, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLayout,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .archives import ARCHIVE_SUFFIXES
from .diagnostics import write_failure_log
from .gui_cards import ContentCardDelegate
from .gui_styles import STYLESHEET
from .gui_support import StartupRequest, _parse_startup_request, _protect_text, _unprotect_text
from .gui_workers import ModelListWorker, ScanPreviewWorker, TranslationWorker
from .models import ProgressEvent, TranslationReport, summarize_failure_counts
from .translator import OpenAICompatibleTranslator

APP_TITLE = "Minecraft 汉化工具"


def _resource_path(relative: str) -> Path:
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    else:
        base = Path(__file__).resolve().parents[2]
    return base / relative


def _archive_output(path: Path) -> Path:
    if path.suffix.lower() in ARCHIVE_SUFFIXES:
        return path.with_name(f"{path.stem}_zh_cn{path.suffix}")
    return path.with_name(f"{path.name}_zh_cn")


def _unique_output_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 1000):
        if path.suffix:
            candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
        else:
            candidate = path.with_name(f"{path.name}_{index}")
        if not candidate.exists():
            return candidate
    return path


def _config_path() -> Path:
    base = Path(os.getenv("APPDATA") or Path.home() / "AppData" / "Roaming")
    return base / "mc-hanhua" / "gui-config.json"


def _job_log_path(output_path: Path) -> Path:
    directory = output_path.resolve().parent / ".mc-hanhua" / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"job-{datetime.now():%Y%m%d-%H%M%S}.log"


class ConnectionDialog(QDialog):
    def __init__(
        self,
        base_url: str,
        api_key: str,
        remember_api_key: bool,
        model: str,
        max_ai: int | None,
        max_workers: int,
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("连接设置")
        self.setModal(True)
        self.setObjectName("connectionDialog")
        self.setMinimumSize(640, 690)
        self.resize(684, 720)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(26, 22, 26, 20)
        layout.setSpacing(12)

        header = QHBoxLayout()
        title = QLabel("连接设置")
        title.setObjectName("dialogTitle")
        header.addWidget(title)
        header.addStretch(1)
        close = QToolButton()
        close.setText("×")
        close.setToolTip("取消")
        close.setObjectName("dialogClose")
        close.clicked.connect(self.reject)
        header.addWidget(close)
        layout.addLayout(header)
        subtitle = QLabel("配置兼容 API、可用模型和稳定请求策略")
        subtitle.setObjectName("muted")
        layout.addWidget(subtitle)

        api_label = QLabel("API 地址")
        api_label.setObjectName("fieldLabel")
        layout.addWidget(api_label)
        self.base_url = QLineEdit(base_url)
        self.base_url.setPlaceholderText("https://.../v1")
        layout.addWidget(self.base_url)

        key_label = QLabel("API Key")
        key_label.setObjectName("fieldLabel")
        layout.addWidget(key_label)
        self.api_key = QLineEdit(api_key)
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("留空时仅使用本地词库")
        layout.addWidget(self.api_key)
        self.remember = QCheckBox("安全记住 API Key")
        self.remember.setChecked(remember_api_key)
        layout.addWidget(self.remember)

        model_label = QLabel("模型")
        model_label.setObjectName("fieldLabel")
        layout.addWidget(model_label)
        model_row = QHBoxLayout()
        model_row.setSpacing(10)
        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.addItem(model or "mimo-v2.5")
        self.model.setCurrentText(model or "mimo-v2.5")
        model_row.addWidget(self.model, 1)
        self.fetch_button = QPushButton("拉取模型")
        self.fetch_button.setObjectName("fetchButton")
        self.fetch_button.clicked.connect(self._fetch_models)
        model_row.addWidget(self.fetch_button)
        layout.addLayout(model_row)
        self.model_status = QLabel("可手动填写模型名，或拉取当前 API 的可用模型。")
        self.model_status.setObjectName("muted")
        layout.addWidget(self.model_status)

        divider = QFrame()
        divider.setObjectName("divider")
        divider.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(divider)

        settings = QGridLayout()
        settings.setHorizontalSpacing(26)
        settings.setVerticalSpacing(8)
        workers_label = QLabel("并发 AI 请求数")
        workers_label.setObjectName("fieldLabel")
        settings.addWidget(workers_label, 0, 0)
        max_label = QLabel("最大 AI 翻译文本组数")
        max_label.setObjectName("fieldLabel")
        settings.addWidget(max_label, 0, 1)
        self.max_workers = QSpinBox()
        self.max_workers.setRange(1, 6)
        self.max_workers.setValue(max_workers)
        settings.addWidget(self.max_workers, 1, 0)
        self.max_ai = QSpinBox()
        self.max_ai.setRange(0, 1_000_000)
        self.max_ai.setSpecialValueText("不限")
        self.max_ai.setValue(max_ai or 0)
        settings.addWidget(self.max_ai, 1, 1)
        layout.addLayout(settings)

        hint = QLabel("默认单并发以避免限流；仅高风险、告示牌和书页进行二次审校。")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addStretch(1)

        footer = QHBoxLayout()
        footer.addStretch(1)
        cancel = QPushButton("取消")
        cancel.setObjectName("dialogCancel")
        cancel.setMinimumHeight(48)
        cancel.clicked.connect(self.reject)
        save = QPushButton("保存")
        save.setObjectName("dialogSave")
        save.setMinimumHeight(48)
        save.clicked.connect(self.accept)
        footer.addWidget(cancel)
        footer.addWidget(save)
        layout.addLayout(footer)
        self._model_worker: ModelListWorker | None = None

    def _fetch_models(self) -> None:
        if self._model_worker and self._model_worker.isRunning():
            return
        self.fetch_button.setEnabled(False)
        self.model_status.setText("正在拉取可用模型…")
        self._model_worker = ModelListWorker(self.base_url.text(), self.api_key.text())
        self._model_worker.loaded.connect(self._on_models_loaded)
        self._model_worker.failed.connect(self._on_models_failed)
        self._model_worker.finished.connect(lambda: self.fetch_button.setEnabled(True))
        self._model_worker.start()

    def _on_models_loaded(self, models: object) -> None:
        current = self.model.currentText().strip()
        self.model.clear()
        self.model.addItems([str(item) for item in models])
        self.model.setCurrentText(current or self.model.currentText())
        self.model_status.setText(f"已获取 {self.model.count()} 个可用模型")

    def _on_models_failed(self, message: str) -> None:
        self.model_status.setText(f"拉取模型失败：{message[:120]}；可继续手动填写。")


class MainWindow(QMainWindow):
    def __init__(self, startup_request: StartupRequest | None = None) -> None:
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.setMinimumSize(1080, 720)
        self.resize(1440, 900)
        self.setAcceptDrops(True)
        self.worker: TranslationWorker | None = None
        self.scan_worker: ScanPreviewWorker | None = None
        self._stop_requested = False
        self._model_api_key = ""
        self.log_lines: list[str] = []
        self.log_html: list[str] = []
        self.log_meta: list[dict[str, str]] = []
        self.startup_request = startup_request or StartupRequest()
        self.runtime_log_path = self.startup_request.runtime_log_path
        self.last_stage = ""
        self.last_ai_log = -1
        self.preview_records: list[dict[str, object]] = []
        self.problem_records: list[dict[str, object]] = []
        self.skip_unit_keys: set[str] = set()
        self._resume_pending = False
        self._visible_preview = []
        self._preview_limit = 200
        self._manual_targets = {}
        self._report_units = []
        self.follow_log = True
        self.log_level_filter = "all"
        self.last_report: TranslationReport | None = None
        self.config = self._load_config()
        self._build_ui()
        self._refresh_preview_table()
        self._refresh_problems()
        self._apply_config()
        self._append_log("拖入 zip、jar、mrpack 或地图文件夹，也可点击重新选择。")
        if self.startup_request.input_path:
            QTimer.singleShot(0, self._apply_startup_request)

    def _build_ui(self) -> None:
        icon_path = _resource_path("assets/app_icon.ico")
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        central = QWidget()
        central.setObjectName("root")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(28, 22, 28, 22)
        root.setSpacing(18)

        header = QHBoxLayout()
        header.setSpacing(16)
        self.logo = QLabel()
        self.logo.setFixedSize(54, 54)
        self.logo.setObjectName("logo")
        logo_path = _resource_path("assets/tubiao.png")
        pixmap = QPixmap(str(logo_path)) if logo_path.exists() else QPixmap()
        if not pixmap.isNull():
            self.logo.setPixmap(pixmap.scaled(48, 48, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
            self.logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(self.logo)

        title_box = QVBoxLayout()
        title_box.setSpacing(0)
        title = QLabel(APP_TITLE)
        title.setObjectName("title")
        subtitle = QLabel("本地处理整合包、地图、资源包和模组文本")
        subtitle.setObjectName("subtitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch(1)
        desktop_badge = QLabel("桌面版")
        desktop_badge.setObjectName("badge")
        desktop_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        desktop_badge.setFixedSize(74, 32)
        header.addWidget(desktop_badge)
        # The reference layout starts directly with the two work cards;
        # branding is already represented by the window title and icon.
        root.addLayout(header)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setObjectName("workspaceSplitter")
        splitter.setChildrenCollapsible(False)
        # Keep the task controls compact so the work area matches the
        # single-column HTML preview while retaining all actions.
        splitter.setHandleWidth(1)
        task_panel = self._build_task_panel()
        task_panel.layout().setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        splitter.addWidget(task_panel)
        splitter.addWidget(self._build_right_panel())
        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 4)
        splitter.setSizes([520, 840])
        root.addWidget(splitter, 1)

        self.setStyleSheet(STYLESHEET)

    def _build_task_panel(self) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(8)
        heading = QHBoxLayout()
        title = QLabel("翻译任务")
        title.setObjectName("cardTitle")
        heading.addWidget(title)
        heading.addStretch(1)
        advanced = QPushButton("连接控制")
        advanced.setObjectName("linkButton")
        advanced.clicked.connect(self._open_connection_settings)
        heading.addWidget(advanced)
        hint = QLabel("上下文复审、缓存复用和限流保护已启用")
        hint.setObjectName("muted")
        layout.addLayout(heading)
        hint.hide()
        divider = QFrame()
        divider.setObjectName("divider")
        divider.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(divider)

        title = QLabel("输入资源")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)

        source = QFrame()
        source.setObjectName("sourceBox")
        source_layout = QHBoxLayout(source)
        source_layout.setContentsMargins(18, 14, 14, 14)
        source_layout.setSpacing(14)
        plus = QLabel("+")
        plus.setObjectName("plus")
        plus.setAlignment(Qt.AlignmentFlag.AlignCenter)
        plus.setFixedSize(54, 54)
        source_layout.addWidget(plus)
        source_text = QVBoxLayout()
        source_text.setSpacing(2)
        self.input_name = QLabel("尚未选择输入资源")
        self.input_name.setObjectName("fileName")
        self.input_detail = QLabel("支持 zip / jar / mrpack，以及地图或整合包目录")
        self.input_detail.setObjectName("muted")
        self.input_detail.setWordWrap(True)
        source_text.addWidget(self.input_name)
        source_text.addWidget(self.input_detail)
        source_layout.addLayout(source_text, 1)
        choose = QPushButton("重新选择")
        choose.setObjectName("secondaryButton")
        choose.clicked.connect(self._choose_input)
        source_layout.addWidget(choose)
        layout.addWidget(source)

        output_label = QLabel("输出位置")
        output_label.setObjectName("sectionTitle")
        layout.addWidget(output_label)
        output_line = QHBoxLayout()
        output_line.setSpacing(0)
        self.output_path = QLineEdit()
        self.output_path.setPlaceholderText("选择输入资源后自动生成")
        self.output_path.setObjectName("outputPath")
        output_line.addWidget(self.output_path, 1)
        output_button = QPushButton("更改")
        output_button.setObjectName("secondaryButton")
        output_button.clicked.connect(self._choose_output)
        output_line.addWidget(output_button)
        layout.addLayout(output_line)

        self.status_box = QFrame()
        self.status_box.setObjectName("statusBox")
        self.status_box.setMinimumHeight(160)
        status_layout = QGridLayout(self.status_box)
        status_layout.setContentsMargins(18, 14, 18, 14)
        status_layout.setHorizontalSpacing(14)
        status_layout.setVerticalSpacing(5)
        self.status_title = QLabel("等待开始")
        self.status_title.setObjectName("statusTitle")
        self.status_subtitle = QLabel("选择输入资源后即可开始汉化")
        self.status_subtitle.setObjectName("muted")
        self.status_subtitle.setWordWrap(False)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(12)
        self.percent = QLabel("0%")
        self.percent.setObjectName("percent")
        self.percent.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.start_button = QPushButton("开始汉化")
        self.start_button.setObjectName("primaryButton")
        self.start_button.setMinimumHeight(34)
        self.start_button.clicked.connect(self._start_translation)
        self.scan_button = QPushButton("仅扫描")
        self.scan_button.setObjectName("secondaryButton")
        self.scan_button.setMinimumHeight(34)
        self.scan_button.clicked.connect(self._start_scan_only)
        self.stop_button = QPushButton("停止汉化")
        self.stop_button.setObjectName("secondaryButton")
        self.stop_button.setMinimumHeight(34)
        self.stop_button.setVisible(False)
        self.stop_button.clicked.connect(self._stop_translation)
        actions = QWidget()
        actions.setMinimumHeight(44)
        actions_layout = QHBoxLayout(actions)
        actions_layout.setContentsMargins(0, 0, 0, 0)
        actions_layout.setSpacing(10)
        actions_layout.addWidget(self.scan_button)
        actions_layout.addWidget(self.start_button, 1)
        actions_layout.addWidget(self.stop_button)
        status_layout.setColumnStretch(0, 1)
        status_layout.addWidget(self.status_title, 0, 0, 1, 2)
        status_layout.addWidget(self.status_subtitle, 1, 0, 1, 2)
        status_layout.addWidget(self.progress_bar, 2, 0)
        status_layout.addWidget(self.percent, 2, 1)
        status_layout.addWidget(actions, 3, 0, 1, 2)
        layout.addWidget(self.status_box)

        self.stage_list = QLabel("准备输入 · 扫描 · 历史复用 · AI 翻译 · 质量审校 · 写回审计 · 重新打包")
        self.stage_list.setObjectName("muted")
        self.stage_list.setWordWrap(True)
        self.stage_list.setMinimumHeight(32)
        self.stage_list.show()
        layout.addWidget(self.stage_list)
        stats = QGridLayout()
        stats.setHorizontalSpacing(20)
        stats.setVerticalSpacing(10)
        self.stat_scanned = self._stat_card("扫描", "0", "")
        self.stat_processed = self._stat_card("已翻译", "0", "accent")
        self.stat_passed = self._stat_card("历史复用", "0", "")
        self.stat_pending = self._stat_card("跳过/锁定", "0", "")
        stats.addWidget(self.stat_scanned, 0, 0)
        stats.addWidget(self.stat_processed, 0, 1)
        stats.addWidget(self.stat_passed, 1, 0)
        stats.addWidget(self.stat_pending, 1, 1)
        stats.setColumnStretch(0, 1)
        stats.setColumnStretch(1, 1)
        layout.addLayout(stats)
        self.translate_npc = QCheckBox("安全时翻译商人 / NPC 标签")
        self.translate_npc.setChecked(True)
        self.translate_npc.setToolTip("被命令引用的名称始终保留")
        # The HTML reference keeps preferences inside a collapsed details
        # section; do not expose this control in the default task view.
        self.translate_npc.hide()
        layout.addWidget(self.translate_npc)
        self.filter_locked = QPushButton("机制锁定")
        self.filter_players = QPushButton("玩家名")
        self.filter_unknown = QPushButton("未知字段")
        self.filter_quality = QPushButton("质量失败")
        self.filter_audit = QPushButton("审计错误")
        for button, reason in (
            (self.filter_locked, "mechanism_locked"),
            (self.filter_players, "player_name"),
            (self.filter_unknown, "unknown_identity"),
            (self.filter_quality, "quality"),
            (self.filter_audit, "audit"),
        ):
            button.setObjectName("linkButton")
            button.clicked.connect(lambda _checked=False, value=reason: self._apply_issue_filter(value))
            button.hide()
        self.report_button = QPushButton("查看质量摘要")
        self.report_button.setObjectName("secondaryButton")
        self.report_button.setEnabled(False)
        self.report_button.clicked.connect(self._open_report)
        self.report_button.setObjectName("linkButton")
        self.report_button.hide()
        layout.addWidget(self.report_button, 0, Qt.AlignmentFlag.AlignRight)
        self.official_button = QPushButton("官方术语资源…")
        self.official_button.setObjectName("linkButton")
        self.official_button.clicked.connect(self._choose_official_terms)
        self.official_button.hide()
        layout.addStretch(1)
        return card

    def _build_right_panel(self) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        card.setMinimumWidth(440)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(12, 12, 12, 12)
        self.right_tabs = QTabWidget()
        self.right_tabs.setObjectName("rightTabs")
        self.right_tabs.setDocumentMode(True)
        self.right_tabs.tabBar().setDrawBase(False)
        self.right_tabs.addTab(self._build_log_panel(), "处理日志")
        self.right_tabs.addTab(self._build_preview_panel(), "扫描预览")
        self.right_tabs.addTab(self._build_review_panel(), "问题审阅")
        self.right_tabs.setCurrentIndex(1)
        layout.addWidget(self.right_tabs, 1)
        return card

    def _build_log_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(8)
        heading = QHBoxLayout()
        self.log_filter = QLineEdit()
        self.log_filter.setObjectName("logFilter")
        self.log_filter.setPlaceholderText("搜索日志…")
        self.log_filter.setClearButtonEnabled(True)
        self.log_filter.textChanged.connect(self._refresh_log_view)
        heading.addWidget(self.log_filter)
        layout.addLayout(heading)
        heading = QHBoxLayout()
        self.log_all = QPushButton("全部")
        self.log_progress = QPushButton("进展")
        self.log_errors = QPushButton("警告与失败")
        self.log_all.clicked.connect(lambda: self._set_log_level("all"))
        self.log_progress.clicked.connect(lambda: self._set_log_level("info"))
        self.log_errors.clicked.connect(lambda: self._set_log_level("error"))
        for button in (self.log_all, self.log_progress, self.log_errors):
            button.setObjectName("secondaryButton")
            heading.addWidget(button)
        self.follow_toggle = QCheckBox("自动跟随")
        self.follow_toggle.setChecked(True)
        self.follow_toggle.toggled.connect(self._set_follow_log)
        heading.addWidget(self.follow_toggle)
        copy_button = QToolButton()
        copy_button.setObjectName("copyLog")
        copy_button.setText("⧉")
        copy_button.setToolTip("复制当前日志")
        copy_button.clicked.connect(self._copy_visible_log)
        heading.addWidget(copy_button)
        layout.addLayout(heading)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setObjectName("log")
        self.log.document().setMaximumBlockCount(2000)
        layout.addWidget(self.log, 1)
        footer = QLabel("日志只记录阶段事件，不重复展示完整原文译文。")
        footer.setObjectName("muted")
        layout.addWidget(footer)
        return panel

    def _build_preview_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 8, 12, 8)
        tools = QHBoxLayout()
        self.preview_filter = QLineEdit()
        self.preview_filter.setPlaceholderText("搜索原文、文件或位置…")
        self.preview_filter.textChanged.connect(self._refresh_preview_table)
        self.preview_policy = QComboBox()
        for label, code in [("全部策略", "全部策略"), ("可翻译", "TRANSLATABLE_DISPLAY"), ("机制保护", "IMMUTABLE_MATCH_VALUE"), ("待确认", "UNKNOWN_REVIEW")]:
            self.preview_policy.addItem(label, code)
        self.preview_policy.currentTextChanged.connect(self._refresh_preview_table)
        tools.addWidget(self.preview_filter, 1)
        tools.addWidget(self.preview_policy)
        layout.addLayout(tools)
        self.preview_table = QTableWidget(0, 8)
        self.preview_table.setHorizontalHeaderLabels(["原文摘要", "文件", "位置", "格式", "处理策略", "发送 AI", "复用", "风险"])
        self.preview_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.preview_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.preview_table.setColumnWidth(1, 150)
        self.preview_table.setColumnWidth(4, 95)
        for column in (2, 3, 5, 6, 7):
            self.preview_table.setColumnHidden(column, True)
        self.preview_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.preview_table.verticalHeader().hide()
        self.preview_table.setShowGrid(False)
        self.preview_table.setAlternatingRowColors(True)
        self.preview_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.preview_table.setItemDelegate(ContentCardDelegate(self.preview_table))
        self.preview_table.setColumnCount(1)
        self.preview_table.setHorizontalHeaderLabels(["扫描内容"])
        self.preview_empty = QLabel("尚未扫描 · 选择输入后点击「仅扫描」")
        self.preview_empty.setObjectName("muted")
        self.preview_empty.setWordWrap(True)
        self.preview_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_empty.setMinimumHeight(260)
        layout.addWidget(self.preview_empty)
        layout.addWidget(self.preview_table, 1)
        self.more_button = QPushButton("加载更多")
        self.more_button.setObjectName("linkButton")
        self.more_button.clicked.connect(self._load_more_preview)
        layout.addWidget(self.more_button)
        self.more_button.hide()
        self.preview_edit = QPushButton("修订所选译文")
        self.preview_edit.setObjectName("linkButton")
        self.preview_edit.hide()
        self.preview_edit.clicked.connect(self._edit_preview_target)
        layout.addWidget(self.preview_edit)
        self.preview_detail = QTextEdit()
        self.preview_detail.setReadOnly(True)
        self.preview_detail.setObjectName("log")
        self.preview_detail.setPlaceholderText("选择内容查看位置、处理策略及原因")
        self.preview_detail.setMaximumHeight(120)
        self.preview_detail.hide()
        layout.addWidget(self.preview_detail)
        self.preview_table.itemSelectionChanged.connect(self._show_preview_detail)
        return panel

    def _build_review_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 8, 12, 8)
        self.review_table = QTableWidget(0, 6)
        self.review_table.setHorizontalHeaderLabels(["原因", "文件", "位置", "原文摘要", "候选译文", "状态"])
        self.review_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.review_table.setColumnHidden(2, True)
        self.review_table.verticalHeader().hide()
        self.review_table.setShowGrid(False)
        self.review_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.review_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.review_empty = QLabel("暂无待处理问题 · 完成任务后会在此展示异常")
        self.review_empty.setObjectName("muted")
        self.review_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.review_empty.setMinimumHeight(260)
        layout.addWidget(self.review_empty)
        layout.addWidget(self.review_table, 1)
        self.review_detail = QTextEdit()
        self.review_detail.setReadOnly(True)
        self.review_detail.setMaximumHeight(90)
        layout.addWidget(self.review_detail)
        actions = QHBoxLayout()
        self.copy_locator_button = QPushButton("复制定位")
        self.edit_target_button = QPushButton("编辑译文")
        self.save_edit_button = QPushButton("保存修订")
        self.apply_edit_button = QPushButton("应用修订")
        for button in (self.copy_locator_button, self.edit_target_button, self.save_edit_button, self.apply_edit_button):
            button.setObjectName("secondaryButton")
            button.hide()
            actions.addWidget(button)
        actions.addStretch(1)
        actions.setEnabled(False)
        actions.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(actions)
        self.edit_target_button.clicked.connect(self._edit_selected_target)
        self.copy_locator_button.clicked.connect(self._copy_selected_locator)
        self.save_edit_button.clicked.connect(self._save_manual_edit_stub)
        self.apply_edit_button.clicked.connect(self._apply_manual_edit_stub)
        self.review_table.itemSelectionChanged.connect(self._show_review_detail)
        return panel

    def _stat_card(self, label: str, value: str, accent: str) -> QFrame:
        frame = QFrame()
        frame.setObjectName("statCard")
        frame.setMinimumHeight(54)
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(15, 0, 15, 0)
        name = QLabel(label)
        name.setObjectName("statLabel")
        number = QLabel(value)
        number.setObjectName("statValue" if not accent else "statValueAccent")
        number.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(name)
        layout.addStretch(1)
        layout.addWidget(number)
        frame.value_label = number  # type: ignore[attr-defined]
        return frame

    def _choose_input(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择待汉化文件",
            "",
            "Minecraft archives (*.zip *.jar *.mrpack);;All files (*.*)",
        )
        if path:
            self._set_input(Path(path))
            return
        folder = QFileDialog.getExistingDirectory(self, "选择地图或整合包目录")
        if folder:
            self._set_input(Path(folder))

    def _choose_output(self) -> None:
        if self._busy():
            return
        input_path = self._input_path()
        if not input_path:
            QMessageBox.information(self, APP_TITLE, "请先选择输入资源。")
            return
        if input_path.is_file():
            suggestion = self.output_path.text() or str(_archive_output(input_path))
            path, _ = QFileDialog.getSaveFileName(
                self,
                "选择输出文件",
                suggestion,
                "Minecraft archives (*.zip *.jar *.mrpack);;All files (*.*)",
            )
            if path:
                self.output_path.setText(path)
        else:
            folder = QFileDialog.getExistingDirectory(self, "选择输出目录")
            if folder:
                self.output_path.setText(str(Path(folder) / f"{input_path.name}_zh_cn"))

    def _set_input(self, path: Path) -> None:
        if self._busy():
            return
        self.preview_records.clear()
        self.problem_records.clear()
        self.skip_unit_keys.clear()
        self._manual_targets.clear()
        self._report_units.clear()
        self.preview_edit.setEnabled(False)
        self._resume_pending = False
        self.last_report = None
        self.report_button.setEnabled(False)
        self.runtime_log_path = None
        self._refresh_preview_table()
        self._refresh_problems()
        if not path.exists():
            QMessageBox.warning(self, APP_TITLE, "输入路径不存在。")
            return
        self.input_name.setText(path.name)
        kind = "文件夹" if path.is_dir() else ("压缩包" if path.suffix.lower() in ARCHIVE_SUFFIXES else "文件")
        self.input_detail.setText(f"{kind}已选择，输出路径将自动生成。")
        self.input_name.setToolTip(str(path))
        self.input_detail.setToolTip(str(path))
        self.input_name.setProperty("input_path", str(path))
        self.output_path.setText(str(_unique_output_path(_archive_output(path))))
        self._set_status("等待开始", "选择输入资源后即可开始汉化", 0)
        self._set_stats(0, 0, 0, 0)
        self._append_log(f"输入：{path}")

    def _apply_startup_request(self) -> None:
        request = self.startup_request
        if not request.input_path:
            return
        self._set_input(request.input_path)
        if request.output_path:
            self.output_path.setText(str(request.output_path))
        if request.start_immediately and request.input_path.exists():
            self._start_translation()

    def _input_path(self) -> Path | None:
        raw = str(self.input_name.property("input_path") or "").strip()
        return Path(raw) if raw else None

    def _open_connection_settings(self) -> None:
        if self._busy():
            return
        max_ai = self._max_ai_value()
        dialog = ConnectionDialog(
            self._base_url(),
            self._model_api_key,
            bool(self.config.get("remember_api_key")),
            self._model_value(),
            max_ai,
            self._ai_workers_value(),
            self,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._set_base_url(dialog.base_url.text().strip())
            self._set_max_ai_value(None if dialog.max_ai.value() == 0 else dialog.max_ai.value())
            self._set_ai_workers_value(dialog.max_workers.value())
            self.config["model"] = dialog.model.currentText().strip() or "mimo-v2.5"
            self.config["remember_api_key"] = dialog.remember.isChecked()
            self._model_api_key = dialog.api_key.text().strip()
            try:
                self._save_config()
                self._append_log("连接设置已保存。")
            except Exception as exc:
                self._append_log(f"配置保存失败：{exc}")

    def _base_url(self) -> str:
        return str(self.config.get("base_url") or "https://token-plan-cn.xiaomimimo.com/v1")

    def _set_base_url(self, value: str) -> None:
        self.config["base_url"] = value or "https://token-plan-cn.xiaomimimo.com/v1"

    def _model_value(self) -> str:
        return str(self.config.get("model") or "mimo-v2.5")

    def _max_ai_value(self) -> int | None:
        raw = self.config.get("max_ai")
        try:
            value = int(raw) if raw not in {None, ""} else 0
        except (TypeError, ValueError):
            value = 0
        return value or None

    def _set_max_ai_value(self, value: int | None) -> None:
        self.config["max_ai"] = value or 0

    def _ai_workers_value(self) -> int:
        try:
            value = int(self.config.get("ai_workers", 1))
        except (TypeError, ValueError):
            value = 1
        return min(6, max(1, value))

    def _set_ai_workers_value(self, value: int) -> None:
        self.config["ai_workers"] = min(6, max(1, value))

    def _start_translation(self) -> None:
        if self._busy():
            return
        input_path = self._input_path()
        output_text = self.output_path.text().strip().strip('"')
        if input_path is None or not input_path.exists():
            QMessageBox.warning(self, APP_TITLE, "请选择有效的输入资源。")
            return
        if not output_text:
            QMessageBox.warning(self, APP_TITLE, "请选择输出位置。")
            return
        output_path = Path(output_text)
        if input_path.resolve() == output_path.resolve():
            QMessageBox.warning(self, APP_TITLE, "输出路径不能与输入路径相同。")
            return
        if output_path.exists() and self.last_report is not None and not self._resume_pending:
            output_path = _unique_output_path(output_path)
            self.output_path.setText(str(output_path))
        if output_path.exists():
            QMessageBox.warning(self, APP_TITLE, "输出路径已存在，请更改文件名。")
            return
        if self.runtime_log_path is None:
            self.runtime_log_path = _job_log_path(output_path)

        try:
            self._save_config()
        except Exception as exc:
            self._append_log(f"配置保存失败：{exc}")

        api_key = self._model_api_key
        translator = (
            OpenAICompatibleTranslator(
                api_key=api_key,
                base_url=self._base_url(),
                model=self._model_value(),
                max_workers=self._ai_workers_value(),
            )
            if api_key
            else None
        )
        self.last_stage = ""
        self.last_ai_log = -1
        self._stop_requested = False
        self.start_button.setEnabled(False)
        self.start_button.setText("汉化中...")
        self.stop_button.setText("停止汉化")
        self.stop_button.setEnabled(True)
        self.stop_button.setVisible(True)
        self._set_status("正在准备资源", "正在检查输入、输出和写入权限", 1)
        self._append_log(f"[1%] 输出：{output_path}")
        options = {"skip_unit_keys": set(self.skip_unit_keys)}
        if self.config.get("official_terms_dir"):
            options.update(official_terms_dir=self.config["official_terms_dir"], official_terms_version=self.config.get("official_terms_version", ""))
        self.worker = TranslationWorker(input_path, output_path, translator, self._max_ai_value(), options=options, resume=self._resume_pending)
        self.worker.records_loaded.connect(self._load_report_records)
        self.scan_button.setEnabled(False)
        self.worker.progress.connect(self._on_progress)
        self.worker.completed.connect(self._on_completed)
        self.worker.paused.connect(self._on_paused)
        self.worker.failed.connect(self._on_failed)
        self.worker.finished.connect(self._on_worker_finished)
        self.worker.start()

    def _start_scan_only(self) -> None:
        if self._busy():
            return
        input_path = self._input_path()
        if input_path is None or not input_path.exists():
            QMessageBox.warning(self, APP_TITLE, "请选择有效的输入资源。")
            return
        self.right_tabs.setCurrentIndex(1)
        self._set_status("扫描", "仅扫描，不调用翻译接口", 8)
        self.start_button.setEnabled(False)
        self.scan_button.setEnabled(False)
        self.stop_button.setVisible(True)
        self.stop_button.setEnabled(True)
        self.scan_worker = ScanPreviewWorker(input_path)
        self.scan_worker.completed.connect(self._on_scan_preview)
        self.scan_worker.failed.connect(lambda message: self._append_log(f"扫描失败：{message}", level="error"))
        self.scan_worker.finished.connect(self._scan_finished)
        self.scan_worker.start()

    def _on_scan_preview(self, result) -> None:
        from .policy import should_send_to_ai
        from .task_records import unit_key

        records = []
        for unit in result.text_units:
            records.append(
                {
                    "unit_key": unit_key([], unit.file_path, unit.format, unit.locator, unit.source_text),
                    "file_path": unit.file_path,
                    "locator": unit.locator,
                    "format_name": unit.format,
                    "source_text": unit.source_text,
                    "policy": unit.policy,
                    "reason": unit.policy_reason,
                    "will_send_ai": should_send_to_ai(unit.policy),
                    "reuse_provider": "",
                    "risk": unit.risk_level.value,
                }
            )
        for detail in result.filtered_details:
            records.append(
                {
                    "unit_key": "",
                    "file_path": detail.get("file", ""),
                    "locator": detail.get("locator", ""),
                    "format_name": detail.get("format", ""),
                    "source_text": detail.get("source", ""),
                    "policy": detail.get("policy", ""),
                    "will_send_ai": False,
                    "reuse_provider": "",
                    "risk": detail.get("reason", ""),
                    "record_kind": "filtered",
                }
            )
        self._preview_limit = 200
        self.preview_records = records
        self._refresh_preview_table()
        for warning in result.warnings:
            self._append_log(warning, level="warning")
        self._append_log(f"扫描完成：{len(result.text_units)} 条文本，{sum(result.filtered_counts.values())} 条被过滤或保留")

    def _stop_translation(self) -> None:
        if self.scan_worker is not None and self.scan_worker.isRunning():
            self.scan_worker.request_stop()
            self.stop_button.setEnabled(False)
            self.status_subtitle.setText("正在停止扫描，等待安全收尾")
            return
        if self.worker is None:
            return
        self._stop_requested = True
        self.stop_button.setEnabled(False)
        self.stop_button.setText("正在停止...")
        self.worker.request_stop()
        self.status_subtitle.setText("正在停止，等待当前请求收尾，最多一个请求超时周期")
        self._append_log("已请求停止：当前请求收尾后结束任务，已完成文本组会保存到缓存。")

    def _reset_stop_button(self) -> None:
        self._stop_requested = False
        self.stop_button.setEnabled(True)
        self.stop_button.setText("停止汉化")
        self.stop_button.setVisible(False)

    def _on_progress(self, event: ProgressEvent) -> None:
        title_map = {
            "prepare": "准备输入",
            "scan": "扫描",
            "plan": "历史复用",
            "ai": "AI 翻译",
            "write": "写回审计",
            "audit": "写回审计",
            "nested": "内嵌包",
            "pack": "重新打包",
            "save": "重新打包",
            "complete": "完成",
            "blocked": "待处理",
            "paused": "已暂停",
            "failed": "失败",
        }
        title = title_map.get(event.stage, "正在处理")
        file_bit = f" · {event.current_file}" if event.current_file else ""
        elapsed_bit = f" · {event.elapsed_ms}ms" if getattr(event, "elapsed_ms", 0) else ""
        summary = f"{event.message}{file_bit}{elapsed_bit}" if event.stage != "ai" else f"请求 {event.api_requests}，重试 {sum(event.retry_counts.values()) if event.retry_counts else 0}，剩余组 {max(0, event.groups_total - event.groups_done)}"
        self._set_status(title, summary, event.percent)
        self.stage_list.setText(self._stage_summary(event))
        skipped = event.skipped or sum(event.filtered_counts.values()) if event.filtered_counts else event.skipped
        self._set_stats(event.scanned, event.translated, event.reused, skipped)
        if getattr(event, "problem_id", ""):
            self._append_log(f"[{event.stage}] {event.message} #{event.problem_id}", level="error", problem_id=event.problem_id)
            self.last_stage = event.stage
            return
        should_log = event.stage != self.last_stage or event.stage in {"ai", "failed", "paused"}
        if getattr(event, "message_html", ""):
            self.last_stage = event.stage
            return
        detail = f"[{event.percent}%] {event.message}"
        if event.stage == "plan":
            detail += f"；首译请求 {event.planned_draft_calls}，审校请求 {event.planned_review_calls}，历史草稿 {event.seeded_drafts}"
        if event.stage == "ai" and event.ai_total:
            detail += (
                f"；文本组 {event.groups_done} / {event.groups_total}，失败 {event.ai_failed}，审校 {event.reviewed_groups}，修正 {event.review_corrected}"
                f"，API 请求 {event.api_requests}，冷却 {event.rate_limit_cooldowns}，已检查点 {event.checkpointed_groups}"
            )
        if event.current_file:
            detail += f"：{event.current_file}"
        if event.last_error:
            detail += f"；AI 错误：{event.last_error}"
        if event.failure_counts:
            detail += f"；失败统计：{summarize_failure_counts(event.failure_counts)}"
        if event.filtered_counts:
            detail += f"；提取过滤：{summarize_failure_counts(event.filtered_counts)}"
        if event.retry_counts:
            detail += f"；重试统计：{summarize_failure_counts(event.retry_counts)}"
        if should_log:
            self._append_log(detail, level="error" if event.stage in {"failed", "paused"} else "info")
            self.last_stage = event.stage

    def _on_completed(self, report: TranslationReport) -> None:
        self.last_report = report
        self.preview_edit.setEnabled(True)
        draft_path = report.output_path.parent / ".mc-hanhua" / (report.output_path.name + ".manual.json")
        if draft_path.exists():
            try:
                self._manual_targets.update(json.loads(draft_path.read_text(encoding="utf-8")).get("targets", {}))
            except (OSError, ValueError):
                self._append_log("修订草稿读取失败，请检查文件。", level="warning")
        self.report_button.setEnabled(True)
        self._set_stats(report.scanned, report.translated, report.reused, report.skipped)
        empty_result = report.scanned == 0
        blocked = bool(report.audit_blocked or report.publication_status == "withheld")
        if blocked:
            self._set_status("待处理", "译文已暂存；发布被阻止", 99)
        elif empty_result:
            self._set_status("未找到可翻译文本", "输入中没有可汉化的内容，原因见处理日志", 100)
        else:
            self._set_status("汉化完成", "输出文件已保存，详细统计见处理日志", 100)
        self._append_log(f"[100%] 完成：{report.output_path}")
        if empty_result:
            self._append_log("翻译内容说明：本次未提取到任何文本，未向 AI 投递任何内容，输出文件为输入的原样副本。")
        self._append_log(
            f"质量统计：文本组 {report.groups_completed} / {report.groups_total}；"
            f"失败 {report.failed_groups}；审校 {report.reviewed_groups}；修正 {report.review_corrected}。"
        )
        self._append_log(
            f"调用统计：计划首译 {report.planned_draft_calls}；计划审校 {report.planned_review_calls}；"
            f"历史草稿 {report.seeded_drafts}；实际 API 请求 {report.api_requests}；限流冷却 {report.rate_limit_cooldowns}；已检查点 {report.checkpointed_groups}。"
        )
        self._append_log(f"文本统计：扫描 {report.scanned}；AI 输出 {report.translated}；精确复用 {report.reused}；未处理 {report.skipped}。")
        if report.filtered_counts:
            self._append_log(f"提取过滤统计：{summarize_failure_counts(report.filtered_counts)}")
        if report.failure_counts:
            self._append_log(f"失败原因统计：{summarize_failure_counts(report.failure_counts)}")
        for reason, sample in report.failure_samples.items():
            self._append_log(f"失败样本 {reason}：{sample}", level="error")
        if report.retry_counts:
            self._append_log(f"已恢复重试统计：{summarize_failure_counts(report.retry_counts)}")
        for warning in report.warnings:
            self._append_log(f"提示：{warning}", level="warning")
        self._reset_stop_button()
        self.start_button.setEnabled(True)
        self.start_button.setText("开始新任务")
        self._resume_pending = False
        if not self._closing:
            if empty_result:
                QMessageBox.information(
                    self,
                    APP_TITLE,
                    "未找到可翻译文本，未产生任何 AI 请求。\n\n"
                    f"输出文件为输入的原样副本：\n{report.output_path}\n\n"
                    "详细原因见处理日志与质量报告。",
                )
            elif blocked:
                QMessageBox.warning(self, APP_TITLE, f"译文已暂存；发布被阻止。\n\n{report.output_path}")
            else:
                QMessageBox.information(self, APP_TITLE, f"汉化完成。\n\n输出文件：\n{report.output_path}")

    def _on_failed(self, message: str, log_path: str) -> None:
        self.problem_records.append({"message": message, "file_path": log_path, "status": "失败"})
        self._refresh_problems()
        self._reset_stop_button()
        self.start_button.setEnabled(True)
        self.start_button.setText("重新尝试")
        self.status_title.setText("处理失败")
        self.status_subtitle.setText("完整原因已保存到诊断日志")
        self._append_log(f"错误：{message}")
        if log_path:
            self._append_log(f"诊断日志：{log_path}")
        details = f"\n\n诊断日志：\n{log_path}" if log_path else ""
        if not self._closing:
            QMessageBox.critical(self, APP_TITLE, f"汉化失败：\n{message}{details}")

    def _on_paused(self, message: str, log_path: str) -> None:
        self._resume_pending = True
        manual_stop = self._stop_requested
        self._reset_stop_button()
        self.start_button.setEnabled(True)
        self.start_button.setText("继续当前任务" if manual_stop else "额度恢复后继续")
        self.status_title.setText("已停止" if manual_stop else "请求已暂停")
        self.status_subtitle.setText("已完成文本组已保存到缓存，保持相同的输入与输出重新开始即可继续")
        self._append_log(("手动停止：" if manual_stop else "请求暂停：") + message)
        if log_path:
            self._append_log(f"诊断日志：{log_path}")
        title = "已停止" if manual_stop else "请求已暂停"
        if not self._closing:
            QMessageBox.information(self, APP_TITLE, f"{title}。\n\n{message}\n\n已完成的文本组已保存到缓存。")

    def _set_status(self, title: str, subtitle: str, percent: int) -> None:
        self.status_title.setText(title)
        self.status_subtitle.setText(subtitle)
        self.progress_bar.setValue(max(0, min(100, percent)))
        self.percent.setText(f"{max(0, min(100, percent))}%")

    def _set_stats(
        self,
        scanned: int,
        translated: int | None = None,
        reused: int | None = None,
        skipped: int | None = None,
        groups_total: int | None = None,
    ) -> None:
        self.stat_scanned.value_label.setText(str(scanned))  # type: ignore[attr-defined]
        if translated is not None:
            self.stat_processed.value_label.setText(str(translated))  # type: ignore[attr-defined]
        if reused is not None:
            self.stat_passed.value_label.setText(str(reused))  # type: ignore[attr-defined]
        if skipped is not None:
            self.stat_pending.value_label.setText(str(skipped))  # type: ignore[attr-defined]
        _ = groups_total

    def _stage_summary(self, event: ProgressEvent) -> str:
        names = ["准备输入", "扫描", "历史复用", "AI 翻译", "质量审校", "写回审计", "重新打包"]
        current = {
            "prepare": "准备输入",
            "scan": "扫描",
            "plan": "历史复用",
            "ai": "AI 翻译",
            "write": "写回审计",
            "audit": "写回审计",
            "pack": "重新打包",
            "save": "重新打包",
            "complete": "重新打包",
        }.get(event.stage, "扫描")
        marked = [f"[{name}]" if name == current else name for name in names]
        extra = []
        if event.current_file:
            extra.append(event.current_file)
        if event.elapsed_ms:
            extra.append(f"{event.elapsed_ms}ms")
        if extra:
            return " · ".join(marked) + " · " + " · ".join(extra)
        return " · ".join(marked)

    def _escape_html(self, text: str) -> str:
        import html
        return html.escape(text, quote=False)

    def _plain_to_html(self, message: str) -> str:
        return self._escape_html(message).replace("\n", "<br>")

    def _append_log(self, message: str, *, level: str = "info", problem_id: str = "") -> None:
        if any(token in message.lower() for token in ("api_key", "authorization", "sk-")):
            message = "[redacted]"
        self._append_rich(message, self._plain_to_html(message), level=level, problem_id=problem_id)

    def _append_rich(self, message: str, html: str | None = None, *, level: str = "info", problem_id: str = "") -> None:
        """Append a log entry, keeping a bounded, searchable copy."""
        rendered = html if html is not None else self._plain_to_html(message)
        self.log_lines.append(message)
        self.log_html.append(rendered)
        if not hasattr(self, "log_meta"):
            self.log_meta = []
        self.log_meta.append({"level": level, "problem_id": problem_id})
        overflow = max(0, len(self.log_lines) - 2000)
        if overflow:
            self.log_lines = self.log_lines[overflow:]
            self.log_html = self.log_html[overflow:]
            self.log_meta = self.log_meta[overflow:]
        if not self.log_filter.text().strip() and self.log_level_filter == "all":
            scroll = self.log.verticalScrollBar().value()
            self.log.append(rendered)
            if self.follow_log:
                self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())
            else:
                self.log.verticalScrollBar().setValue(scroll)
        else:
            self._refresh_log_view(self.log_filter.text())
        self._write_runtime_log(message)

    def _refresh_log_view(self, filter_text: str) -> None:
        needle = filter_text.strip().lower()
        html_parts: list[str] = []
        metas = getattr(self, "log_meta", [{}] * len(self.log_lines))
        for line, rendered, meta in zip(self.log_lines, self.log_html, metas):
            level = str(meta.get("level", "info"))
            if self.log_level_filter == "info" and level not in {"info", "warning"}:
                continue
            if self.log_level_filter == "error" and level not in {"warning", "error"}:
                continue
            if needle and needle not in line.lower():
                continue
            html_parts.append(rendered)
        scroll = self.log.verticalScrollBar().value()
        self.log.setHtml("<br>".join(html_parts))
        if not self.follow_log:
            self.log.verticalScrollBar().setValue(scroll)
        if self.follow_log:
            self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())

    def _copy_visible_log(self) -> None:
        QApplication.clipboard().setText(self.log.toPlainText())

    def _set_follow_log(self, checked: bool) -> None:
        self.follow_log = bool(checked)

    def _set_log_level(self, level: str) -> None:
        self.log_level_filter = level
        self._refresh_log_view(self.log_filter.text())

    def _apply_issue_filter(self, reason: str) -> None:
        self.right_tabs.setCurrentIndex(2 if reason in {"quality", "audit", "unknown_identity"} else 1)
        if reason in {"quality", "audit"}:
            return
        index = self.preview_policy.findData("IMMUTABLE_MATCH_VALUE" if reason == "mechanism_locked" else "UNKNOWN_REVIEW" if reason == "unknown_identity" else "全部策略")
        if reason == "player_name":
            self.preview_policy.setCurrentIndex(0)
            self.preview_filter.setText("player_name")
        elif index >= 0:
            self.preview_filter.clear()
            self.preview_policy.setCurrentIndex(index)

    def _open_report(self) -> None:
        if self.last_report is None:
            return
        output = self.last_report.output_path
        path = (output.parent / ".mc-hanhua" / "reports" / f"{output.stem}-translation-report.html"
                if output.suffix.lower() in ARCHIVE_SUFFIXES else output / ".mc-hanhua" / "translation-report.html")
        if not path.exists():
            QMessageBox.warning(self, APP_TITLE, "报告文件不存在，请查看任务日志。")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))

    def _refresh_preview_table(self, *_args: object) -> None:
        needle = self.preview_filter.text().strip().lower()
        policy = self.preview_policy.currentData()
        rows = []
        for record in self.preview_records:
            if policy != "全部策略" and record.get("policy") != policy:
                continue
            blob = " ".join(str(record.get(key, "")) for key in ("source_text", "file_path", "locator", "reason", "risk"))
            if needle and needle not in blob.lower():
                continue
            rows.append(record)
        self._visible_preview = rows[:self._preview_limit]
        self.preview_table.setRowCount(len(self._visible_preview))
        self.more_button.setVisible(len(rows) > self._preview_limit)
        self.preview_empty.setVisible(not rows)
        self.preview_table.setVisible(bool(rows))
        self.preview_detail.setVisible(False)
        self.preview_empty.setText("没有匹配的内容" if self.preview_records else "尚无扫描结果 · 点击「仅扫描」")
        for index, record in enumerate(self._visible_preview):
            self.preview_table.setItem(index, 0, QTableWidgetItem(str(record.get("source_text", ""))))
            self.preview_table.item(index, 0).setData(Qt.ItemDataRole.UserRole, record)
            self.preview_table.setRowHeight(index, 292 if record.get("_expanded") else 210)
            continue
            values = [
                str(record.get("source_text", ""))[:80],
                str(record.get("file_path", "")),
                str(record.get("locator", "")),
                str(record.get("format_name", "")),
                {"TRANSLATABLE_DISPLAY": "可翻译", "IMMUTABLE_MATCH_VALUE": "机制保护", "UNKNOWN_REVIEW": "待确认"}.get(str(record.get("policy", "")), str(record.get("policy", ""))),
                "是" if record.get("will_send_ai") else "否",
                str(record.get("reuse_provider", "")),
                str(record.get("risk", "")),
            ]
            for column, value in enumerate(values):
                self.preview_table.setItem(index, column, QTableWidgetItem(value))

    def _show_preview_detail(self) -> None:
        row = self.preview_table.currentRow()
        if row < 0 or row >= len(self._visible_preview):
            return
        record = self._visible_preview[row]
        record["_expanded"] = not bool(record.get("_expanded"))
        self.preview_detail.setPlainText("\n".join(f"{label}：{record.get(key, chr(8212))}" for label, key in [("原文", "source_text"), ("文件", "file_path"), ("位置", "locator"), ("策略", "policy"), ("原因", "reason"), ("复用", "reuse_provider")]))
        self.preview_table.item(row, 0).setData(Qt.ItemDataRole.UserRole, record)
        self.preview_table.setRowHeight(row, 292 if record["_expanded"] else 210)
        self.preview_table.viewport().update()

    def _skip_selected_preview(self) -> None:
        if self._busy():
            return
        row = self.preview_table.currentRow()
        if row < 0 or row >= len(self._visible_preview):
            return
        record = self._visible_preview[row]
        if not record.get("will_send_ai") or record.get("policy") in {"IMMUTABLE_MATCH_VALUE", "UNKNOWN_REVIEW"}:
            return
        key = str(record.get("unit_key", ""))
        if key in self.skip_unit_keys:
            self.skip_unit_keys.remove(key)
            self._append_log("已恢复所选文本参与翻译")
        else:
            self.skip_unit_keys.add(key)
            self._append_log("已将所选文本标记为本次跳过")

    def _show_review_detail(self) -> None:
        row = self.review_table.currentRow()
        if row < 0 or row >= len(self.problem_records):
            self.review_detail.setPlainText("详情尚未加载/不可用")
            return
        problem = self.problem_records[row]
        self.review_detail.setPlainText(str(problem.get("message", "")))

    def _copy_selected_locator(self) -> None:
        row = self.review_table.currentRow()
        if row < 0 or row >= len(self.problem_records):
            return
        QApplication.clipboard().setText(str(self.problem_records[row].get("locator", "")))

    def _busy(self) -> bool:
        return any(w is not None and w.isRunning() for w in (self.worker, self.scan_worker))

    def _scan_finished(self) -> None:
        self.scan_worker = None
        self.start_button.setEnabled(True)
        self.scan_button.setEnabled(True)
        self._reset_stop_button()
        self._set_status("扫描结束", "可查看预览，或开始汉化", 0)

    def _load_more_preview(self) -> None:
        self._preview_limit += 200
        self._refresh_preview_table()

    def _load_report_records(self, payload) -> None:
        from .task_records import make_problem_id, unit_key
        self._report_units = payload.get("units", [])
        self.problem_records = []
        records = []
        for item in self._report_units:
            record = dict(item)
            record["format_name"] = item.get("format", "")
            record["unit_key"] = unit_key([], item["file_path"], item["format"], item["locator"], item["source_text"])
            record["will_send_ai"] = item.get("policy") == "TRANSLATABLE_DISPLAY"
            records.append(record)
            if not item.get("final_target") and record["will_send_ai"]:
                self.problem_records.append(dict(record, problem_id=make_problem_id("current", record["unit_key"], "untranslated"),
                                                 message="译文未通过或尚未处理", status="待处理"))
        report = payload.get("report", {})
        for warning in report.get("warnings", []):
            self.problem_records.append({"message": warning, "status": "提示", "problem_id": make_problem_id("current", "", warning)})
        for issue in (report.get("audit") or {}).get("issues", []):
            self.problem_records.append(dict(issue, message=issue.get("message", str(issue)), status="审计"))
        # Surface unknown/filtered issues without presenting protected names as failures.
        for detail in report.get("filtered_details", []):
            if detail.get("policy") == "UNKNOWN_REVIEW":
                self.problem_records.append(dict(detail, message=detail.get("reason", "待确认"), status="待确认"))
        self.preview_records = records
        self._refresh_preview_table()
        self._refresh_problems()

    def _refresh_problems(self) -> None:
        self.review_table.setRowCount(len(self.problem_records))
        for row, record in enumerate(self.problem_records):
            for col, key in enumerate(("message", "file_path", "locator", "source_text", "final_target", "status")):
                self.review_table.setItem(row, col, QTableWidgetItem(str(record.get(key, ""))))
        self.review_empty.setVisible(not self.problem_records)
        self.review_table.setVisible(bool(self.problem_records))
        self.review_detail.setVisible(bool(self.problem_records))

    def _edit_preview_target(self) -> None:
        if self._busy() or self.last_report is None:
            return
        row = self.preview_table.currentRow()
        if row < 0 or row >= len(self._visible_preview):
            return
        record = dict(self._visible_preview[row])
        if record.get("policy") != "TRANSLATABLE_DISPLAY":
            return
        record.update(message="人工修订", status="待编辑")
        self.problem_records.append(record)
        self._refresh_problems()
        self.right_tabs.setCurrentIndex(2)
        self.review_table.selectRow(len(self.problem_records) - 1)
        self._edit_selected_target()

    def _edit_selected_target(self) -> None:
        if self._busy():
            return
        row = self.review_table.currentRow()
        if row < 0 or row >= len(self.problem_records):
            return
        record = self.problem_records[row]
        if not record.get("unit_key") or record.get("policy") != "TRANSLATABLE_DISPLAY":
            QMessageBox.information(self, APP_TITLE, "此项为文件级诊断或保护内容，不支持修改译文。")
            return
        target, ok = QInputDialog.getMultiLineText(self, "修订译文", record["source_text"], self._manual_targets.get(record["unit_key"], record.get("final_target") or ""))
        if ok:
            from .quality import validate_contextual_target
            reason = validate_contextual_target(record["source_text"], target)
            if not target.strip() or reason:
                QMessageBox.warning(self, APP_TITLE, f"译文校验失败：{reason or '不能为空'}")
                return
            self._manual_targets[record["unit_key"]] = target
            record["final_target"] = target
            record["status"] = "未保存草稿"
            self._refresh_problems()

    def _save_manual_edit_stub(self) -> None:
        if not self._manual_targets or self.last_report is None or self._busy():
            self._append_log("请先选择问题、编辑译文，再保存草稿。")
            return
        from .task_records import atomic_write_json
        output = self.last_report.output_path
        path = output.parent / ".mc-hanhua" / (output.name + ".manual.json")
        atomic_write_json(path, {"schema_version": 1, "targets": self._manual_targets})
        self._append_log(f"草稿已保存：{path}；输出包尚未更改。")

    def _apply_manual_edit_stub(self) -> None:
        if self._busy() or self.last_report is None or not self._manual_targets:
            return
        from .task_records import load_manifest
        from .translator import GlossaryOnlyTranslator
        old = self.last_report.output_path
        manifest_path = old.parent / ".mc-hanhua" / (old.name + ".task.json")
        try:
            manifest = load_manifest(manifest_path)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, APP_TITLE, f"无法验证原任务输入：{exc}")
            return
        seeds = {(u["file_path"], u["format"], u["locator"], u["source_text"]): u["final_target"]
                 for u in self._report_units if u.get("final_target")}
        target = _unique_output_path(old.with_name(old.stem + "_修订" + old.suffix))
        options = {"seed_translations": seeds, "manual_targets": dict(self._manual_targets), "expected_fingerprint": manifest.input_fingerprint}
        if self.config.get("official_terms_dir"):
            options.update(official_terms_dir=self.config["official_terms_dir"], official_terms_version=self.config.get("official_terms_version", ""))
        self.worker = TranslationWorker(Path(manifest.input_path), target, GlossaryOnlyTranslator(), 0, options=options)
        self.worker.records_loaded.connect(self._load_report_records)
        self.worker.progress.connect(self._on_progress)
        self.worker.completed.connect(self._on_completed)
        self.worker.failed.connect(self._on_failed)
        self.worker.paused.connect(self._on_paused)
        self.worker.finished.connect(self._on_worker_finished)
        self.start_button.setEnabled(False)
        self.scan_button.setEnabled(False)
        self.stop_button.setVisible(True)
        self.output_path.setText(str(target))
        self._append_log("正在新副本中应用修订，重新审计后另存；不调用 AI。")
        self.worker.start()

    def _choose_official_terms(self) -> None:
        if self._busy():
            return
        folder = QFileDialog.getExistingDirectory(self, "选择含 en_us.json 和 zh_cn.json 的官方语言目录")
        if not folder:
            return
        version, ok = QInputDialog.getText(self, "官方术语版本", "填写资源对应的 Minecraft Java 版本")
        if not ok or not version.strip():
            return
        from .official_terms import OfficialTermIndex
        index = OfficialTermIndex.from_directory(folder, version.strip())
        if not index.available:
            QMessageBox.warning(self, APP_TITLE, "无法读取成对的官方语言文件。")
            return
        self.config.update(official_terms_dir=folder, official_terms_version=version.strip())
        self._save_config()
        self._append_log(f"已选择官方术语资源：{version}，{len(index.terms)} 条。")

    def jump_to_problem(self, problem_id: str) -> None:
        for index, problem in enumerate(self.problem_records):
            if problem.get("problem_id") == problem_id:
                self.right_tabs.setCurrentIndex(2)
                self.review_table.selectRow(index)
                return
        self.right_tabs.setCurrentIndex(2)
        self.review_detail.setPlainText("详情尚未加载/不可用")

    def _write_runtime_log(self, message: str) -> None:
        if self.runtime_log_path:
            try:
                self.runtime_log_path.parent.mkdir(parents=True, exist_ok=True)
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                with self.runtime_log_path.open("a", encoding="utf-8") as handle:
                    handle.write(f"{timestamp} {message}\n")
            except OSError:
                pass

    def _load_config(self) -> dict[str, object]:
        try:
            data = json.loads(_config_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        if "quality_policy_version" not in data:
            # The former default of three workers caused rate-limit retry storms.
            data["ai_workers"] = 1
            data["quality_policy_version"] = 1
        return data

    def _apply_config(self) -> None:
        if bool(self.config.get("remember_api_key")):
            stored = str(self.config.get("api_key_dpapi") or "")
            api_key = _unprotect_text(stored)
            if api_key:
                self._model_api_key = api_key
                self._append_log("已加载加密保存的 API Key。")
            elif stored:
                self._append_log("已保存的 API Key 无法解密（可能更换了电脑或 Windows 账户），请在“连接控制”重新填写。")

    def _save_config(self) -> None:
        data: dict[str, object] = {
            "base_url": self._base_url(),
            "model": self._model_value(),
            "max_ai": self.config.get("max_ai", 0),
            "ai_workers": self._ai_workers_value(),
            "quality_policy_version": 1,
            "remember_api_key": bool(self.config.get("remember_api_key")),
            "official_terms_dir": self.config.get("official_terms_dir", ""),
            "official_terms_version": self.config.get("official_terms_version", ""),
        }
        if bool(data["remember_api_key"]):
            data["api_key_dpapi"] = _protect_text(self._model_api_key) if self._model_api_key else ""
        path = _config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self.config = data

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        for url in event.mimeData().urls():
            path = Path(url.toLocalFile())
            if path.exists():
                self._set_input(path)
                event.acceptProposedAction()
                return

    _closing = False

    def _on_worker_finished(self) -> None:
        # Runs after run() has returned; the QThread is safe to release here,
        # unlike inside the completed/paused/failed slots.
        self.worker = None
        self.scan_button.setEnabled(True)

    def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        worker = self.scan_worker if self.scan_worker is not None and self.scan_worker.isRunning() else self.worker
        if worker is not None and worker.isRunning():
            # Never destroy a running QThread: ask it to stop and close the
            # window automatically once it has finished.
            if not self._closing:
                self._closing = True
                worker.finished.connect(self._close_after_worker)
                self._stop_translation()
                self._append_log("窗口将在后台任务安全收尾后自动关闭。")
            event.ignore()
            return
        try:
            self._save_config()
        except Exception:
            pass
        super().closeEvent(event)

    def _close_after_worker(self) -> None:
        self.worker = None
        self.close()


def main() -> None:
    try:
        app = QApplication.instance() or QApplication(sys.argv)
        app.setStyle("Fusion")
        font_path = _resource_path("assets/NotoSansSC-VF.ttf")
        font_id = QFontDatabase.addApplicationFont(str(font_path)) if font_path.exists() else -1
        families = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
        app.setFont(QFont(families[0] if families else "Microsoft YaHei UI", 10))
        from .webui.window import HtmlMainWindow

        request = _parse_startup_request(sys.argv[1:])
        window = HtmlMainWindow(request)
        if request.html_self_test:
            window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        window.show()
        sys.exit(app.exec())
    except Exception as exc:
        log_path = write_failure_log(exc, operation="gui-startup")
        detail = traceback.format_exc()
        try:
            QMessageBox.critical(None, APP_TITLE, f"程序启动失败。\n\n日志：{log_path or '无法写入'}\n\n{detail}")
        finally:
            raise


if __name__ == "__main__":
    main()
