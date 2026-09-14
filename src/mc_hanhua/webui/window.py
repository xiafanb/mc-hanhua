"""Production HTML desktop window. Reuses workers, config and the task controller."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, QUrl, Slot
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QIcon, QMouseEvent
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QFileDialog, QMainWindow, QVBoxLayout, QWidget

from mc_hanhua.gui_support import StartupRequest
from mc_hanhua.webui.controller import TaskController, model_request_is_current, unique_output_path
from mc_hanhua.webui.paths import app_page_path, ensure_page_icon, icon_path
from mc_hanhua.webui.present import classify_titlebar_press

# Frameless-window resize handles. Keys are the CSS data-edge values from the
# HTML shell; the window itself never paints a native title bar.
RESIZE_EDGES = {
    "n": Qt.Edge.TopEdge,
    "s": Qt.Edge.BottomEdge,
    "e": Qt.Edge.RightEdge,
    "w": Qt.Edge.LeftEdge,
    "ne": Qt.Edge.TopEdge | Qt.Edge.RightEdge,
    "nw": Qt.Edge.TopEdge | Qt.Edge.LeftEdge,
    "se": Qt.Edge.BottomEdge | Qt.Edge.RightEdge,
    "sw": Qt.Edge.BottomEdge | Qt.Edge.LeftEdge,
}


class DesktopBridge(QObject):
    def __init__(self, window: HtmlMainWindow) -> None:
        super().__init__(window)
        self._window = window

    @Slot(str, result=str)
    def call(self, raw: str) -> str:
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return json.dumps({"ok": False, "error": "invalid command"}, ensure_ascii=False)
        result = self._window.handle_command(payload if isinstance(payload, dict) else {})
        if isinstance(result, dict):
            result["window_maximized"] = bool(self._window.isMaximized())
        return json.dumps(result, ensure_ascii=False)


class HtmlMainWindow(QMainWindow):
    def __init__(self, startup_request: StartupRequest | None = None, *, config_file: Path | None = None, profiler=None) -> None:
        super().__init__()
        self.startup_request = startup_request or StartupRequest()
        self._profiler = profiler
        self.controller = TaskController(config_file=config_file, emit=self.push_snapshot)
        self.worker = None
        self.scan_worker = None
        self._model_worker = None
        self._model_request: dict | None = None
        self._closing = False
        self._page_ready = False
        self._last_titlebar_press = (0.0, 0, 0)  # (monotonic seconds, x, y)
        # The HTML shell paints its own themed title bar; the native one only
        # provides the taskbar entry, so it is removed entirely.
        self.setWindowFlag(Qt.FramelessWindowHint, True)
        self.setWindowTitle("MC 汉化器")
        self.setMinimumSize(800, 600)
        available = self.screen().availableGeometry()
        self.resize(min(1280, available.width()), min(800, available.height()))
        self.setAcceptDrops(True)
        try:
            self.setWindowIcon(QIcon(str(icon_path())))
        except FileNotFoundError:
            pass

        ensure_page_icon()
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.view = QWebEngineView()
        settings = self.view.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
        layout.addWidget(self.view)
        self.setCentralWidget(root)

        self.channel = QWebChannel(self.view.page())
        self.bridge = DesktopBridge(self)
        self.channel.registerObject("backend", self.bridge)
        self.view.page().setWebChannel(self.channel)
        # Dragging is a synchronous Qt concern. QWebEngineView forwards real
        # mouse input to an internal render delegate child, so neither the
        # view nor an async JS->bridge round trip reliably sees the press; an
        # application-level filter is the one hook that always does.
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        self.view.loadFinished.connect(self._on_page_loaded)
        self.view.titleChanged.connect(self._on_title)
        self.view.setUrl(QUrl.fromLocalFile(str(app_page_path().resolve())))
        if self.startup_request.html_self_test:
            QTimer.singleShot(20000, lambda: self._finish_self_test(False, "timed out waiting for HTML shell"))
        elif self.startup_request.input_path:
            QTimer.singleShot(0, self._apply_startup)

    def _mark(self, name: str) -> None:
        marker = getattr(self._profiler, "mark", None)
        if callable(marker):
            marker(name)

    def _on_page_loaded(self, ok: bool) -> None:
        self._mark("page_load_finished")
        if not ok:
            return

    def _on_title(self, title: str) -> None:
        if title.startswith("SHELL_READY"):
            self._page_ready = True
            self._mark("webchannel_hello")
            if self._profiler is not None and getattr(self._profiler, "enabled", False):
                report_path = self.startup_request.report_path
                writer = getattr(self._profiler, "write_report", None)
                if callable(writer):
                    writer(report_path)
        if self.startup_request.html_self_test and title.startswith("SHELL_READY"):
            self._finish_self_test(True, "shell ready")

    def _finish_self_test(self, ok: bool, reason: str) -> None:
        if getattr(self, "_self_test_done", False):
            return
        self._self_test_done = True
        report = {
            "ok": ok,
            "reason": reason,
            "frozen": bool(getattr(sys, "frozen", False)),
            "page": str(app_page_path()),
            "url": self.view.url().toString(),
        }
        if self.startup_request.report_path is not None:
            self.startup_request.report_path.parent.mkdir(parents=True, exist_ok=True)
            self.startup_request.report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        QTimer.singleShot(0, QApplication.instance().quit)

    def push_snapshot(self, payload: dict) -> None:
        payload = dict(payload)
        payload["window_maximized"] = bool(self.isMaximized())
        encoded = json.dumps(payload, ensure_ascii=False)
        self.view.page().runJavaScript(f"applySnapshot({encoded})")

    def _start_system_move(self) -> None:
        # PySide6 exposes the system move/resize loops on QWindow only.
        handle = self.windowHandle()
        if handle is not None:
            handle.startSystemMove()

    def _start_system_resize(self, edges: Qt.Edge) -> None:
        handle = self.windowHandle()
        if handle is not None:
            handle.startSystemResize(edges)

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        # Keep the in-page title bar glyph in sync with Win+Arrow snapping and
        # any other state change the HTML cannot observe by itself.
        if event.type() == QEvent.Type.WindowStateChange and not self._closing:
            self.push_snapshot(self.controller.snapshot())

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        etype = event.type()
        if etype in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick) and not self._closing:
            if (
                isinstance(obj, QWidget)
                and isinstance(event, QMouseEvent)
                and (obj is self.view or self.view.isAncestorOf(obj))
                and event.button() == Qt.MouseButton.LeftButton
            ):
                pos = obj.mapTo(self.view, event.position().toPoint())
                now = time.monotonic()
                last_time, last_x, last_y = self._last_titlebar_press
                action = classify_titlebar_press(
                    pos.x(),
                    pos.y(),
                    self.view.width(),
                    last_x=last_x,
                    last_y=last_y,
                    seconds_since_last=now - last_time if last_time else 0.0,
                    double_click=etype == QEvent.Type.MouseButtonDblClick,
                )
                if action == "none":
                    # Page content: double clicks stay with the WebEngine (A09).
                    return super().eventFilter(obj, event)
                self._last_titlebar_press = (0.0, 0, 0)
                if action == "move":
                    self._last_titlebar_press = (now, pos.x(), pos.y())
                    self._start_system_move()
                else:
                    if self.isMaximized():
                        self.showNormal()
                    else:
                        self.showMaximized()
                return True
        return super().eventFilter(obj, event)

    def handle_command(self, payload: dict) -> dict:
        command = str(payload.get("command") or "")
        if command in {"hello", "snapshot"}:
            return self.controller.snapshot()
        if command == "window_minimize":
            self.showMinimized()
            return self.controller.snapshot(ok=True)
        if command == "window_toggle_maximize":
            if self.isMaximized():
                self.showNormal()
            else:
                self.showMaximized()
            return self.controller.snapshot(ok=True)
        if command == "window_close":
            self.close()
            return self.controller.snapshot(ok=True)
        if command == "window_move":
            if not self.isMaximized():
                self._start_system_move()
            return self.controller.snapshot(ok=True)
        if command == "window_resize":
            edge = RESIZE_EDGES.get(str(payload.get("edge") or ""))
            if edge is not None and not self.isMaximized():
                self._start_system_resize(edge)
            return self.controller.snapshot(ok=True)
        if command == "choose_resource":
            return self._choose_resource()
        if command == "choose_output":
            return self._choose_output()
        if command == "set_output_text":
            text = str(payload.get("path") or "").strip().strip('"')
            if not text:
                return self.controller.snapshot(ok=False, error="输出路径为空")
            return self.controller.set_output(Path(text))
        if command == "set_npc":
            return self.controller.set_npc(bool(payload.get("enabled")))
        if command == "apply_connection":
            return self.controller.apply_connection(payload)
        if command == "fetch_models":
            return self._start_fetch_models(payload)
        if command == "scan":
            return self._start_scan()
        if command == "start":
            return self._start_translation(resume=bool(payload.get("resume")))
        if command == "stop":
            return self._stop()
        if command == "reset":
            return self.controller.reset_task()
        if command == "save_revision":
            return self.controller.save_revision(str(payload.get("id") or ""), str(payload.get("target") or ""))
        if command == "save_draft":
            if not self.controller.manual_targets:
                return self.controller.snapshot(ok=False, error="没有可保存的修订。", toast="没有可保存的修订。")
            return self.controller.snapshot(ok=True, toast="草稿已保存，未修改现有包")
        if command == "export_copy":
            return self._export_copy()
        if command == "restore_task":
            return self._restore(int(payload.get("index") or 0))
        if command == "open_report":
            return self._open_path(self.controller.recovery_payload().get("report_path") or "")
        if command == "open_workspace":
            return self._open_path(self.controller.recovery_payload().get("workspace_path") or "")
        if command == "reaudit":
            return self._reaudit()
        return self.controller.snapshot(ok=False, error="未知命令")

    def _start_fetch_models(self, payload: dict) -> dict:
        if self._model_worker is not None and self._model_worker.isRunning():
            return self.controller.snapshot(
                ok=False,
                error="正在拉取模型，请稍候。",
                toast="正在拉取模型，请稍候。",
                model_status="正在拉取可用模型…",
                request_id=str((self._model_request or {}).get("request_id") or ""),
            )
        prepared = self.controller.prepare_fetch_models(payload)
        if not prepared.get("ok"):
            return prepared
        base_url, api_key, endpoint = self.controller.resolve_fetch_credentials(payload)
        request_id = str(prepared.get("request_id") or "")
        credential_version = int(prepared.get("credential_version") or 0)
        request = {
            "request_id": request_id,
            "endpoint": endpoint,
            "credential_version": credential_version,
            "base_url": base_url,
        }
        self._model_request = request
        from mc_hanhua.gui_workers import ModelListWorker

        worker = ModelListWorker(base_url, api_key)
        self._model_worker = worker
        # Capture this request in the slots. QThread.finished can run before
        # queued loaded/failed signals; comparing against a cleared pointer
        # previously showed a false "stale list" message and dropped models.
        worker.loaded.connect(lambda models, req=request: self._on_models_loaded(models, req))
        worker.failed.connect(lambda message, req=request: self._on_models_failed(message, req))
        worker.finished.connect(self._on_model_worker_finished)
        worker.start()
        return prepared

    def _model_request_current(self, request: dict | None = None) -> bool:
        req = request if request is not None else self._model_request
        return model_request_is_current(
            req,
            active_request=self._model_request,
            credential_version=self.controller._credential_version,
        )

    def _finish_stale_fetch(self, request_id: str) -> None:
        if self._closing:
            return
        # The result belongs to an outdated request (connection changed or a
        # newer fetch is running). The page must still leave its pending
        # state, so carry an explicit status even though no fresh data landed.
        self.push_snapshot(
            self.controller.snapshot(
                ok=True,
                request_id=request_id,
                fetch_done=True,
                model_status="连接已变化，这次结果已跳过；可重新拉取。",
            )
        )

    def _on_models_loaded(self, models, request: dict | None = None) -> None:
        if self._closing:
            return
        req = request or self._model_request or {}
        request_id = str(req.get("request_id") or "")
        if not self._model_request_current(req):
            self._finish_stale_fetch(request_id)
            return
        snapshot = self.controller.on_models_loaded(
            list(models or []),
            request_id=request_id,
            endpoint=str(req.get("endpoint") or ""),
            credential_version=int(req.get("credential_version") or 0),
        )
        self.push_snapshot(snapshot)

    def _on_models_failed(self, message: str, request: dict | None = None) -> None:
        if self._closing:
            return
        req = request or self._model_request or {}
        request_id = str(req.get("request_id") or "")
        if not self._model_request_current(req):
            self._finish_stale_fetch(request_id)
            return
        endpoint = str(req.get("endpoint") or "")
        snapshot = self.controller.on_models_failed(
            str(message),
            request_id=request_id,
            endpoint=endpoint,
            cached=self.controller.cached_models(endpoint),
        )
        self.push_snapshot(snapshot)

    def _on_model_worker_finished(self) -> None:
        self._model_worker = None

    def _choose_resource(self) -> dict:
        if self.controller.busy:
            return self.controller.snapshot(ok=False, error="任务进行中，不能更换输入。")
        path, _ = QFileDialog.getOpenFileName(self, "选择待汉化文件", "", "Minecraft archives (*.zip *.jar *.mrpack);;All files (*.*)")
        if path:
            return self.controller.set_input(Path(path))
        folder = QFileDialog.getExistingDirectory(self, "选择地图或整合包目录")
        if folder:
            return self.controller.set_input(Path(folder))
        return self.controller.snapshot(ok=True)

    def _choose_output(self) -> dict:
        if self.controller.busy:
            return self.controller.snapshot(ok=False, error="任务进行中，不能更改输出。")
        input_path = self.controller.input_path
        if input_path is None:
            return self.controller.snapshot(ok=False, error="请先选择输入资源。", toast="请先选择输入资源。")
        if input_path.is_file():
            suggestion = str(self.controller.output_path or "")
            path, _ = QFileDialog.getSaveFileName(self, "选择输出文件", suggestion, "Minecraft archives (*.zip *.jar *.mrpack);;All files (*.*)")
            if path:
                return self.controller.set_output(Path(path))
        else:
            folder = QFileDialog.getExistingDirectory(self, "选择输出目录")
            if folder:
                return self.controller.set_output(Path(folder) / f"{input_path.name}_zh_cn")
        return self.controller.snapshot(ok=True)

    def _start_scan(self) -> dict:
        if self.controller.busy:
            return self.controller.snapshot(ok=False, error="已有任务在运行。")
        input_path = self.controller.input_path
        if input_path is None or not input_path.exists():
            return self.controller.snapshot(ok=False, error="请选择有效的输入资源。", toast="请选择有效的输入资源。")
        self.controller.busy = True
        self.controller.state = "scanning"
        self.controller.status_title = "扫描"
        self.controller.status_desc = "仅扫描，不调用翻译接口"
        self.controller.percent = 5
        self.controller._log("开始扫描资源")
        from mc_hanhua.gui_workers import ScanPreviewWorker

        self.scan_worker = ScanPreviewWorker(input_path)
        self.scan_worker.completed.connect(self._on_scan)
        self.scan_worker.failed.connect(self._on_scan_failed)
        self.scan_worker.finished.connect(self._scan_finished)
        self.scan_worker.start()
        return self.controller.snapshot(ok=True)

    def _on_scan(self, result) -> None:
        self.push_snapshot(self.controller.on_scan_result(result))

    def _on_scan_failed(self, message: str) -> None:
        self.push_snapshot(self.controller.on_scan_failed(str(message)))

    def _scan_finished(self) -> None:
        self.scan_worker = None
        self.controller.busy = False

    def _start_translation(self, *, resume: bool = False) -> dict:
        if self.controller.busy:
            return self.controller.snapshot(ok=False, error="已有任务在运行。")
        input_path = self.controller.input_path
        output_path = self.controller.output_path
        if input_path is None or not input_path.exists():
            return self.controller.snapshot(ok=False, error="请选择有效的输入资源。", toast="请选择有效的输入资源。")
        if output_path is None:
            return self.controller.snapshot(ok=False, error="请选择输出位置。", toast="请选择输出位置。")
        if input_path.resolve() == output_path.resolve():
            return self.controller.snapshot(ok=False, error="输出路径不能与输入路径相同。", toast="输出路径不能与输入路径相同。")
        if output_path.exists() and self.controller.last_report is not None and not resume:
            output_path = unique_output_path(output_path)
            self.controller.output_path = output_path
        if output_path.exists() and not resume:
            return self.controller.snapshot(ok=False, error="输出路径已存在，请更改文件名。", toast="输出路径已存在，请更改文件名。")
        from mc_hanhua.gui_workers import TranslationWorker
        from mc_hanhua.translator import OpenAICompatibleTranslator

        translator = (
            OpenAICompatibleTranslator(
                api_key=self.controller.api_key,
                base_url=self.controller.base_url(),
                model=self.controller.model(),
                max_workers=self.controller.workers(),
            )
            if self.controller.api_key
            else None
        )
        self.controller.busy = True
        self.controller.stop_requested = False
        self.controller.state = "translating"
        self.controller.current_request_id = uuid4().hex
        options = self.controller.translation_options()
        self.worker = TranslationWorker(input_path, output_path, translator, self.controller.max_ai(), options=options, resume=resume or self.controller.resume_pending)
        self.worker.records_loaded.connect(self._on_records)
        self.worker.progress.connect(self._on_progress)
        self.worker.completed.connect(self._on_completed)
        self.worker.paused.connect(self._on_paused)
        self.worker.failed.connect(self._on_failed)
        self.worker.finished.connect(self._on_worker_finished)
        self.worker.start()
        return self.controller.snapshot(ok=True)

    def _stop(self) -> dict:
        worker = self.scan_worker if self.scan_worker is not None and self.scan_worker.isRunning() else self.worker
        if worker is None:
            return self.controller.snapshot()
        self.controller.stop_requested = True
        self.controller.state = "stopping"
        worker.request_stop()
        return self.controller.snapshot(ok=True)

    def _on_progress(self, event) -> None:
        self.push_snapshot(self.controller.on_progress(event))

    def _on_completed(self, report) -> None:
        self.push_snapshot(self.controller.on_completed(report))

    def _on_failed(self, message: str, log_path: str) -> None:
        self.push_snapshot(self.controller.on_failed(message, log_path))

    def _on_paused(self, message: str, log_path: str) -> None:
        self.push_snapshot(self.controller.on_paused(message, log_path))

    def _on_records(self, payload) -> None:
        self.push_snapshot(self.controller.load_report_records(payload))

    def _on_worker_finished(self) -> None:
        self.worker = None
        self.controller.busy = False

    def _export_copy(self) -> dict:
        if self.controller.busy or self.controller.last_report is None or not self.controller.manual_targets:
            return self.controller.snapshot(ok=False, error="没有可应用的修订。", toast="没有可应用的修订。")
        from mc_hanhua.task_records import load_manifest

        old = self.controller.last_report.output_path
        manifest_path = old.parent / ".mc-hanhua" / (old.name + ".task.json")
        try:
            manifest = load_manifest(manifest_path)
        except (OSError, ValueError) as exc:
            return self.controller.snapshot(ok=False, error=str(exc), toast=str(exc))
        target = unique_output_path(old.with_name(old.stem + "_修订" + old.suffix))
        options = self.controller.translation_options()
        options["expected_fingerprint"] = manifest.input_fingerprint
        from mc_hanhua.gui_workers import TranslationWorker
        from mc_hanhua.translator import GlossaryOnlyTranslator

        self.controller.busy = True
        self.controller.state = "translating"
        self.worker = TranslationWorker(Path(manifest.input_path), target, GlossaryOnlyTranslator(), 0, options=options)
        self.worker.records_loaded.connect(self._on_records)
        self.worker.progress.connect(self._on_progress)
        self.worker.completed.connect(self._on_completed)
        self.worker.failed.connect(self._on_failed)
        self.worker.paused.connect(self._on_paused)
        self.worker.finished.connect(self._on_worker_finished)
        self.controller.output_path = target
        self.worker.start()
        return self.controller.snapshot(ok=True, toast="正在新副本中应用修订，不调用 AI。")

    def _restore(self, index: int) -> dict:
        history = self.controller.history_payload()
        if index < 0 or index >= len(history):
            return self.controller.snapshot(ok=False, error="找不到该任务。")
        item = history[index]
        ok, reason = self.controller.can_restore(item)
        if not ok:
            return self.controller.snapshot(ok=False, error=reason, toast=reason)
        self.controller.set_input(Path(item["input"]))
        self.controller.set_output(Path(item["output"]))
        if item.get("state") == "paused":
            self.controller.resume_pending = True
            self.controller.state = "paused"
        return self.controller.snapshot(ok=True)

    def _apply_startup(self) -> None:
        request = self.startup_request
        if request.input_path:
            self.controller.set_input(request.input_path)
        if request.output_path:
            self.controller.set_output(request.output_path)
        self.push_snapshot(self.controller.snapshot())
        if request.start_immediately and request.input_path and request.input_path.exists():
            self._start_translation()

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        for url in event.mimeData().urls():
            path = Path(url.toLocalFile())
            if path.exists():
                self.push_snapshot(self.controller.set_input(path))
                event.acceptProposedAction()
                return

    def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        worker = self.scan_worker if self.scan_worker is not None and self.scan_worker.isRunning() else self.worker
        model_worker = self._model_worker if self._model_worker is not None and self._model_worker.isRunning() else None
        if worker is not None and worker.isRunning():
            if not self._closing:
                self._closing = True
                worker.finished.connect(self._close_after_worker)
                self._stop()
            event.ignore()
            return
        if model_worker is not None:
            self._model_request = None
            if not self._closing:
                self._closing = True
                model_worker.finished.connect(self._close_after_worker)
            event.ignore()
            return
        try:
            self.controller.save_config()
        except Exception:
            pass
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        super().closeEvent(event)

    def _close_after_worker(self) -> None:
        # Clear only the workers that have actually finished (A06): a model
        # fetch may still be in flight while the translation worker closes.
        for attr in ("worker", "scan_worker", "_model_worker"):
            value = getattr(self, attr)
            if value is not None and not value.isRunning():
                setattr(self, attr, None)
        self.close()

    def _open_path(self, raw: str) -> dict:
        path = Path(str(raw or ""))
        if not raw or not path.exists():
            return self.controller.snapshot(ok=False, error="找不到报告或工作目录。", toast="找不到报告或工作目录。")
        from PySide6.QtGui import QDesktopServices

        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path if path.is_dir() else path.parent)))
        return self.controller.snapshot(ok=True, toast=f"已打开：{path}")

    def _reaudit(self) -> dict:
        if self.controller.busy:
            return self.controller.snapshot(ok=False, error="已有任务在运行。")
        recovery = self.controller.recovery_payload()
        workspace = Path(str(recovery.get("workspace_path") or ""))
        if not recovery.get("recoverable") or not workspace.exists():
            return self.controller.snapshot(ok=False, error="暂存译文缺失，需要恢复翻译。", toast="暂存译文缺失，需要恢复翻译。")
        output_path = self.controller.output_path
        if output_path is None:
            return self.controller.snapshot(ok=False, error="请选择输出位置。")
        from mc_hanhua.gui_workers import ReauditWorker

        self.controller.busy = True
        self.controller.state = "translating"
        self.controller.status_title = "重新审计"
        self.controller.status_desc = "不调用翻译接口，仅重新审计并打包已暂存译文。"
        self.worker = ReauditWorker(workspace, output_path)
        self.worker.records_loaded.connect(self._on_records)
        self.worker.progress.connect(self._on_progress)
        self.worker.completed.connect(self._on_completed)
        self.worker.paused.connect(self._on_paused)
        self.worker.failed.connect(self._on_failed)
        self.worker.finished.connect(self._on_worker_finished)
        self.worker.start()
        return self.controller.snapshot(ok=True)
