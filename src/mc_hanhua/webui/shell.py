"""Visual HTML desktop shell with fixed fixture data.

This is not the production window. It loads the migrated page inside
QWebEngineView so screenshots can be compared with the design HTML.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Slot
from PySide6.QtGui import QIcon
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QMainWindow, QVBoxLayout, QWidget

from .paths import SHELL_STATES, app_page_path, ensure_page_icon, icon_path, parse_shell_args, resource_path


class ShellBridge(QObject):
    """Placeholder bridge so the page can load qwebchannel.js without eval."""

    def __init__(self, window: ShellWindow) -> None:
        super().__init__(window)
        self._window = window

    @Slot(str, result=str)
    def ping(self, request_id: str) -> str:
        payload = {"ok": True, "request_id": request_id, "source": "python", "frozen": bool(getattr(sys, "frozen", False))}
        return json.dumps(payload, ensure_ascii=False)


class ShellWindow(QMainWindow):
    def __init__(self, *, self_test: bool, report_path: Path | None, state: str, capture_dir: Path | None, width: int, height: int) -> None:
        super().__init__()
        self.self_test = self_test
        self.report_path = report_path
        self.initial_state = state
        self.capture_dir = capture_dir
        self.view_width = width
        self.view_height = height
        self.events: list[dict[str, object]] = []
        self.metrics: dict[str, object] = {}
        self.metrics_by_state: dict[str, object] = {}
        self.captures: list[str] = []
        self.ready = False
        self.load_ok = False
        self._finished = False
        self._pending_states: list[str] = []
        self.page_path = app_page_path()
        ensure_page_icon()
        self.setWindowTitle("Minecraft 汉化工具 · 视觉对照")
        try:
            self.setWindowIcon(QIcon(str(icon_path())))
        except FileNotFoundError:
            pass

        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.view = QWebEngineView()
        self.view.setFixedSize(width, height)
        settings = self.view.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
        layout.addWidget(self.view)
        self.setCentralWidget(root)
        self.adjustSize()

        self.channel = QWebChannel(self.view.page())
        self.bridge = ShellBridge(self)
        self.channel.registerObject("backend", self.bridge)
        self.view.page().setWebChannel(self.channel)
        self.view.loadFinished.connect(self._on_loaded)
        self.view.titleChanged.connect(self._on_title)
        self.view.setUrl(QUrl.fromLocalFile(str(self.page_path.resolve())))

        if self.self_test:
            QTimer.singleShot(25000, self._timeout)

    def record(self, event: str, payload: dict[str, object]) -> None:
        self.events.append({"event": event, **payload})

    def _on_loaded(self, ok: bool) -> None:
        self.load_ok = bool(ok)
        self.record("loadFinished", {"ok": bool(ok), "url": self.view.url().toString(), "page": str(self.page_path)})
        if not ok:
            self._finish(False, "page load failed")

    def _on_title(self, title: str) -> None:
        if title.startswith("SHELL_READY") and not self.ready:
            self.ready = True
            self.record("ready", {"title": title})
            if self.self_test:
                self._begin_fixtures()
            else:
                self._run_js(f"applyFixture({json.dumps(self.initial_state)})")

    def _begin_fixtures(self) -> None:
        if self.capture_dir is not None:
            self._pending_states = list(SHELL_STATES)
        else:
            self._pending_states = [self.initial_state]
        self._next_fixture()

    def _next_fixture(self) -> None:
        if not self._pending_states:
            self._collect_metrics_then_finish()
            return
        state = self._pending_states.pop(0)
        self._run_js(f"applyFixture({json.dumps(state)})", lambda _value, current=state: self._after_fixture(current))

    def _after_fixture(self, state: str) -> None:
        self.record("fixture", {"state": state})
        self._run_js("JSON.stringify(collectMetrics())", lambda raw, current=state: self._on_state_metrics(current, raw))

    def _on_state_metrics(self, state: str, raw: object) -> None:
        parsed: dict[str, object]
        if isinstance(raw, str) and raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                parsed = {"error": str(exc), "raw": raw[:400]}
        else:
            parsed = {"error": "empty metrics", "raw": raw}
        self.metrics_by_state[state] = parsed
        self.metrics = parsed
        if self.capture_dir is not None:
            QTimer.singleShot(250, lambda current=state: self._capture(current))
        else:
            self._next_fixture()

    def _capture(self, state: str) -> None:
        assert self.capture_dir is not None
        self.capture_dir.mkdir(parents=True, exist_ok=True)
        path = self.capture_dir / f"shell-{_state_index(state):02d}-{state}.png"
        pixmap = self.view.grab()
        saved = pixmap.save(str(path), "PNG")
        self.captures.append(str(path))
        self.record("capture", {"state": state, "path": str(path), "ok": bool(saved), "width": pixmap.width(), "height": pixmap.height()})
        self._next_fixture()

    def _collect_metrics_then_finish(self) -> None:
        self._finish(True, "shell ready")

    def _run_js(self, script: str, callback=None) -> None:
        if callback is None:
            self.view.page().runJavaScript(script)
        else:
            self.view.page().runJavaScript(script, callback)

    def _timeout(self) -> None:
        if not self._finished:
            self._finish(False, "timed out waiting for shell page")

    def _finish(self, ok: bool, reason: str) -> None:
        if self._finished:
            return
        self._finished = True
        report = {
            "ok": ok,
            "reason": reason,
            "frozen": bool(getattr(sys, "frozen", False)),
            "load_ok": self.load_ok,
            "ready": self.ready,
            "page": str(self.page_path),
            "page_exists": self.page_path.is_file(),
            "css_base": str(self.page_path.with_name("design-base.css")),
            "css_overlay": str(self.page_path.with_name("design-overlay.css")),
            "icon": str(resource_path("assets/tubiao.png")),
            "url": self.view.url().toString(),
            "viewport": {"width": self.view_width, "height": self.view_height},
            "captures": self.captures,
            "metrics": self.metrics,
            "metrics_by_state": self.metrics_by_state,
            "events": self.events,
        }
        if self.report_path is not None:
            self.report_path.parent.mkdir(parents=True, exist_ok=True)
            self.report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        if self.self_test:
            QTimer.singleShot(0, QApplication.instance().quit)


def _state_index(state: str) -> int:
    try:
        return list(SHELL_STATES).index(state) + 1
    except ValueError:
        return 0


def main(argv: list[str] | None = None) -> int:
    os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
    args = parse_shell_args(sys.argv[1:] if argv is None else argv)
    if args.self_test and args.report is None:
        print("self-test requires --report", file=sys.stderr)
        return 2
    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)
    window = ShellWindow(
        self_test=args.self_test,
        report_path=args.report,
        state=args.state,
        capture_dir=args.capture_dir,
        width=args.width,
        height=args.height,
    )
    # Hidden grabs of QWebEngineView are often blank; keep the window on
    # screen when capturing fixture PNGs. Metrics-only self-test can hide.
    if args.self_test and args.capture_dir is None:
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
