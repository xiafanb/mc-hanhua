"""Production HTML desktop window. Reuses workers, config and the task controller."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, QUrl, Slot
from PySide6.QtGui import QDragEnterEvent, QDragLeaveEvent, QDragMoveEvent, QDropEvent, QIcon, QMouseEvent, QWheelEvent
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QFileDialog, QMainWindow, QSystemTrayIcon, QVBoxLayout, QWidget

from mc_hanhua.gui_support import StartupRequest
from mc_hanhua.webui.controller import TaskController, model_request_is_current, unique_output_path
from mc_hanhua.webui.paths import app_page_path, ensure_page_icon, icon_path
from mc_hanhua.webui.present import bank_logs, clamp_zoom, classify_titlebar_press, drop_plan, queue_advance

# Log lines banked between pushes. The page keeps 2000 and the controller 2000,
# so this only bounds a pathological burst; normal runs bank a handful.
LOG_PENDING_MAX = 2000

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
        self._page_zoom = 1.0
        self._pending_snapshot: dict | None = None
        self._snapshot_in_flight = False
        # Log lines banked for the next push, and the ids already delivered. The
        # page renders what it is sent, so these two sets are what guarantee that
        # coalescing a burst of snapshots cannot drop log text.
        self._pending_logs: list[dict] = []
        self._pending_log_ids: set[str] = set()
        self._delivered_log_ids: set[str] = set()
        # The HTML shell paints its own themed title bar; the native one only
        # provides the taskbar entry, so it is removed entirely.
        self.setWindowFlag(Qt.FramelessWindowHint, True)
        self.setWindowTitle("MC 汉化器")
        self.setMinimumSize(800, 600)
        available = self.screen().availableGeometry()
        self.resize(min(1280, available.width()), min(800, available.height()))
        try:
            self.setWindowIcon(QIcon(str(icon_path())))
        except FileNotFoundError:
            pass

        # Windows toasts are delivered through a tray icon; without one
        # showMessage() silently does nothing on recent Windows versions.
        self.tray = QSystemTrayIcon(self.windowIcon())
        self.tray.setToolTip("MC 汉化器")
        self.tray.show()

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
        # Drops are accepted at the window AND at the application level. The
        # frameless layout puts the web view under the whole window, and on some
        # Windows builds a drop lands on the internal RenderWidgetHostViewQtDelegate
        # child instead of the QMainWindow — that child has acceptDrops() false, so
        # the drag died before any dragEnterEvent reached the window. setAcceptDrops
        # on the child plus this filter is what makes dropping files work.
        self.setAcceptDrops(True)
        self.view.setAcceptDrops(True)
        for child in self.view.findChildren(QWidget):
            child.setAcceptDrops(True)
        self._drop_hovering = False
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
        """Push a state snapshot to the page, coalescing bursts.

        A translation run emits an event per text group, and each snapshot
        carries the recent log lines — measured at ~243 KB of JSON per tick.
        Serialising that on the GUI thread and parsing it in the page, several
        times a second, is what made the window feel stuttery: the main thread
        was busy shipping log text instead of servicing input.

        Two things keep that cheap: at most one push is in flight (only the
        newest snapshot is kept while it is busy — a run's hundredth tick
        matters more than its ninety-ninth, and every snapshot is complete), and
        each pushed snapshot carries only the log lines the page has not seen.
        Coalescing therefore cannot lose log text: the delta is computed here
        against the cursor last *delivered*, not against the last rendered tick.
        """

        payload = dict(payload)
        payload["window_maximized"] = bool(self.isMaximized())
        # Log lines are banked as they arrive, and the page gets the bank when a
        # snapshot actually goes out. Taking them at push time would lose the
        # lines carried by a snapshot that a newer one replaced, and taking only
        # the newest snapshot's tail would lose anything older than that tail.
        self._bank_logs(payload.get("logs"))
        self._pending_snapshot = payload
        if self._snapshot_in_flight:
            return
        self._flush_snapshot()

    def _bank_logs(self, logs: object) -> None:
        bank_logs(logs, self._pending_logs, self._pending_log_ids, self._delivered_log_ids, LOG_PENDING_MAX)

    def _with_log_delta(self, payload: dict) -> dict:
        """Replace the snapshot's log tail with the banked, not-yet-sent lines."""

        if not isinstance(payload.get("log_revision"), int) and not self._pending_logs:
            return payload
        payload["logs"] = list(self._pending_logs)
        self._pending_logs = []
        self._delivered_log_ids |= self._pending_log_ids
        self._pending_log_ids = set()
        if len(self._delivered_log_ids) > 8000:
            # Bound the identity set. The page keeps 2000 lines and the
            # controller 2000 more, so a few thousand ids is ample headroom.
            self._delivered_log_ids = set(
                str(entry.get("id")) for entry in payload["logs"][-1000:] if isinstance(entry, dict)
            )
        return payload

    def _flush_snapshot(self) -> None:
        payload = self._pending_snapshot
        if payload is None or self._closing:
            return
        self._pending_snapshot = None
        self._snapshot_in_flight = True
        payload = self._with_log_delta(payload)
        encoded = json.dumps(payload, ensure_ascii=False)
        self.view.page().runJavaScript(f"applySnapshot({encoded})", self._on_snapshot_applied)

    def _on_snapshot_applied(self, _result: object = None) -> None:
        self._snapshot_in_flight = False
        if self._pending_snapshot is not None:
            self._flush_snapshot()

    def set_page_zoom(self, factor: float) -> float:
        """Scale the whole page proportionally, like a browser's zoom.

        WebEngine's zoom factor scales layout, fonts and the interaction points
        together (devicePixelRatio rises with it), so the page keeps its
        proportions instead of stretching one region.
        """

        clamped = clamp_zoom(factor)
        self._page_zoom = clamped
        self.view.setZoomFactor(clamped)
        self.push_snapshot(self.controller.snapshot(ok=True))
        return clamped

    def _zoom_step(self, delta: float) -> None:
        self.set_page_zoom(self._page_zoom + delta)

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
        # Ctrl+wheel zooms the whole page. WebEngine consumes plain-wheel scroll
        # itself; only the Ctrl chord needs to be claimed here.
        if etype == QEvent.Type.Wheel and not self._closing and isinstance(event, QWheelEvent):
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                steps = event.angleDelta().y() / 120.0
                if steps:
                    self._zoom_step(0.1 * steps)
                    return True
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
                    scale=self._page_zoom,
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
        # Drop events arrive on whichever widget the OS hands them to. The view's
        # internal render child does not forward them to QMainWindow on every
        # Windows build, so they are handled here for any widget of this window.
        if etype in (QEvent.Type.DragEnter, QEvent.Type.DragMove, QEvent.Type.DragLeave, QEvent.Type.Drop):
            if isinstance(obj, QWidget) and (obj is self.view or self.view.isAncestorOf(obj) or obj is self):
                if self._route_drop_event(obj, event):
                    return True
        return super().eventFilter(obj, event)

    def _route_drop_event(self, widget: QWidget, event: QEvent) -> bool:
        """Handle one drag/drop stage for a widget, returning True when consumed."""

        mime = getattr(event, "mimeData", lambda: None)()
        if mime is None or not mime.hasUrls():
            return False
        paths = [Path(url.toLocalFile()) for url in mime.urls()]
        paths = [path for path in paths if path.exists()]
        etype = event.type()
        if etype in (QEvent.Type.DragEnter, QEvent.Type.DragMove):
            if not paths:
                return False
            event.acceptProposedAction()
            if etype == QEvent.Type.DragEnter or not self._drop_hovering:
                self._drop_hovering = True
                self._set_drop_feedback(paths)
            return True
        if etype == QEvent.Type.DragLeave:
            self._drop_hovering = False
            self.run_js("applyDropState('idle', '')")
            event.accept()
            return True
        # Drop
        self._drop_hovering = False
        self.run_js("applyDropState('idle', '')")
        if not paths:
            return False
        event.acceptProposedAction()
        self._handle_dropped_paths(paths)
        return True

    def _handle_dropped_paths(self, paths: list[Path]) -> None:
        """A drop fills the queue; it only claims the input slot while that slot is empty.

        2026-09-24: single-file drops used to replace the current input whenever
        the app was idle, so dragging three packs in a row silently kept only the
        last one.  Replacing the input stays on the「选择资源」button — an explicit
        act — while dragging always means "line this behind what I chose".
        """

        to_input, _to_queue = drop_plan(
            has_input=self.controller.input_path is not None,
            busy=self.controller.busy,
            count=len(paths),
            has_queue=bool(self.controller.queue),
        )
        if to_input:
            first = self.controller.set_input(paths[0])
            self.push_snapshot(first)
            if not first.get("ok"):
                # An unusable first path is not silently dropped either: the batch
                # reports why it did not land.
                self.push_snapshot(self.controller.enqueue_paths(paths))
                return
            paths = paths[1:]
        if paths:
            self.push_snapshot(self.controller.enqueue_paths(paths))

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
        if command == "window_zoom":
            self.set_page_zoom(float(payload.get("factor") or self._page_zoom))
            return self.controller.snapshot(ok=True)
        if command == "window_zoom_step":
            self._zoom_step(float(payload.get("delta") or 0))
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
        if command == "enqueue_task":
            raw_path = str(payload.get("path") or "").strip().strip('"')
            if not raw_path:
                return self.controller.snapshot(ok=False, error="缺少要排队的路径。")
            return self.controller.enqueue_task(Path(raw_path))
        if command == "choose_enqueue":
            return self._choose_enqueue()
        if command == "queue_remove":
            return self.controller.queue_remove(int(payload.get("index") or 0))
        if command == "queue_clear":
            return self.controller.queue_clear()
        if command == "start_next":
            # 完成态下队列的显式启动入口（用户可能是在上一个任务跑完之后才拖入
            # 新包的，那种情况不会再有完成回调来推进队列）。
            return self._start_next_queued()
        if command == "set_auto_continue":
            return self.controller.set_auto_continue(bool(payload.get("enabled")))
        if command == "set_notify":
            system = payload.get("system")
            sound = payload.get("sound")
            return self.controller.set_notify(
                system=None if system is None else bool(system),
                sound=None if sound is None else bool(sound),
            )
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
        from mc_hanhua.translator import translator_class_for

        # The provider decides how a candidate is produced; whether a candidate
        # may be written stays the one shared validation gate either way.
        provider_class = translator_class_for(self.controller.provider())
        translator = (
            provider_class(
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
        self.controller.mark_task_started()
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
        snapshot = self.controller.on_completed(report)
        self.push_snapshot(snapshot)
        self._maybe_notify(snapshot)
        if queue_advance(
            str(snapshot.get("state") or ""),
            auto_continue=self.controller.auto_continue,
            has_queue=bool(self.controller.queue),
        ) == "next":
            QTimer.singleShot(0, self._start_next_queued)

    def _on_failed(self, message: str, log_path: str) -> None:
        snapshot = self.controller.on_failed(message, log_path)
        self.push_snapshot(snapshot)
        self._maybe_notify(snapshot)

    def _on_paused(self, message: str, log_path: str) -> None:
        snapshot = self.controller.on_paused(message, log_path)
        self.push_snapshot(snapshot)
        self._maybe_notify(snapshot)

    def _maybe_notify(self, snapshot: dict) -> None:
        """System-side completion feedback: only when the user is not looking."""

        notify = snapshot.get("notify") or {}
        if not notify or self._closing:
            return
        if self.isActiveWindow() and not self.isMinimized():
            return
        if self.controller.notify_system_enabled():
            try:
                self.tray.showMessage(
                    str(notify.get("title") or "MC 汉化器"),
                    str(notify.get("body") or ""),
                    QSystemTrayIcon.Information,
                    6000,
                )
            except Exception:
                pass
        QApplication.alert(self)
        if self.controller.notify_sound_enabled():
            QApplication.beep()

    def _start_next_queued(self) -> dict:
        entry, snapshot = self.controller.claim_queue_head()
        if entry is None:
            # 没取出条目就一定没有启动：把原因推给页面，队列与当前输入保持原样。
            self.push_snapshot(snapshot)
            return snapshot
        self.controller._log(f"队列继续：{entry.get('name')}（剩余 {len(self.controller.queue)} 个）")
        started = self._start_translation()
        if not started.get("ok"):
            started = self.controller.on_failed(str(started.get("error") or "队列任务启动失败"))
        self.push_snapshot(started)
        return started

    def _choose_enqueue(self) -> dict:
        files, _ = QFileDialog.getOpenFileNames(self, "选择要排队的文件", "", "Minecraft archives (*.zip *.jar *.mrpack);;All files (*.*)")
        if files:
            snapshot = None
            for item in files:
                snapshot = self.controller.enqueue_task(Path(item))
            return snapshot or self.controller.snapshot(ok=True)
        folder = QFileDialog.getExistingDirectory(self, "选择要排队的地图或整合包目录")
        if folder:
            return self.controller.enqueue_task(Path(folder))
        return self.controller.snapshot(ok=True)

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
        self.controller.mark_task_started()
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

    def _set_drop_feedback(self, paths: list[Path]) -> None:
        """Tell the page what a drop would do, since it cannot see the drag itself.

        The window accepts the drop, so the web view never receives dragover or
        dragleave: without this the drop zone could not highlight. The hint
        states the outcome up front so the user knows whether the input is
        claimed or queued before releasing — it reads the same ``drop_plan`` the
        drop itself uses, so the promise and the result cannot drift apart.
        """

        if not paths:
            self.run_js("applyDropState('idle', '')")
            return
        has_input = self.controller.input_path is not None
        has_queue = bool(self.controller.queue)
        to_input, _to_queue = drop_plan(
            has_input=has_input, busy=self.controller.busy, count=len(paths), has_queue=has_queue
        )
        if to_input:
            if len(paths) == 1:
                detail = f"松开后设为当前输入 · 点击开始后处理：{paths[0].name}"
            else:
                detail = f"松开后：1 个设为当前输入，另外 {len(paths) - 1} 个加入队列"
            self.run_js(f"applyDropState('over', {json.dumps(detail, ensure_ascii=False)})")
            return
        names = "、".join(path.name for path in paths[:3])
        more = f" 等 {len(paths)} 个" if len(paths) > 3 else ""
        if self.controller.busy:
            prefix = "松开后加入队列 · 不影响正在运行的任务："
        elif has_input:
            current = self.controller.input_path
            prefix = f"松开后加入队列 · 保留当前输入 {current.name}：" if current else "松开后加入队列："
        else:  # pragma: no cover - an idle, empty input slot already returned above
            prefix = "松开后加入队列："
        self.run_js(f"applyDropState('queue', {json.dumps(prefix + names + more, ensure_ascii=False)})")

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        # Delegated to the shared path so the window hooks and the application
        # filter cannot drift apart; a drop may arrive through either.
        self._route_drop_event(self, event)

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        self._route_drop_event(self, event)

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:
        # Without this the highlight stays on after the pointer leaves, because
        # the page never sees a dragleave of its own.
        self._drop_hovering = False
        self.run_js("applyDropState('idle', '')")
        event.accept()

    def run_js(self, source: str) -> None:
        """Run a snippet in the page, ignoring a view that is not ready yet."""

        if self._closing:
            return
        try:
            self.view.page().runJavaScript(source)
        except RuntimeError:
            pass

    def dropEvent(self, event: QDropEvent) -> None:
        self._route_drop_event(self, event)

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
        try:
            self.tray.hide()
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
        self.controller.mark_task_started()
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
