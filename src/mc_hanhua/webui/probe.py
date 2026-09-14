"""Standalone WebEngine packaging probe.

This is not the production window. It only proves that a frozen EXE can load a
local HTML page, talk over QWebChannel, and invoke a native file picker.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Slot
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QFileDialog, QMainWindow, QVBoxLayout, QWidget

from .paths import parse_probe_args, probe_page_path


class ProbeBridge(QObject):
    """Minimal, non-eval bridge used by the packaging probe only."""

    def __init__(self, window: ProbeWindow) -> None:
        super().__init__(window)
        self._window = window

    @Slot(str, result=str)
    def ping(self, request_id: str) -> str:
        payload = {
            "ok": True,
            "request_id": request_id,
            "source": "python",
            "frozen": bool(getattr(sys, "frozen", False)),
        }
        self._window.record("ping", payload)
        return json.dumps(payload, ensure_ascii=False)

    @Slot(str, result=str)
    def choose_resource(self, request_id: str) -> str:
        if self._window.self_test:
            payload = {
                "ok": True,
                "request_id": request_id,
                "cancelled": True,
                "path": "",
                "name": "",
                "reason": "self-test does not open a native dialog",
            }
            self._window.record("choose_resource", payload)
            return json.dumps(payload, ensure_ascii=False)
        path, _ = QFileDialog.getOpenFileName(
            self._window,
            "选择输入资源",
            "",
            "Minecraft archives (*.zip *.jar *.mrpack);;All files (*.*)",
        )
        chosen = Path(path) if path else None
        payload = {
            "ok": True,
            "request_id": request_id,
            "cancelled": chosen is None,
            "path": str(chosen) if chosen else "",
            "name": chosen.name if chosen else "",
        }
        self._window.record("choose_resource", payload)
        return json.dumps(payload, ensure_ascii=False)


class ProbeWindow(QMainWindow):
    def __init__(self, *, self_test: bool, report_path: Path | None) -> None:
        super().__init__()
        self.self_test = self_test
        self.report_path = report_path
        self.events: list[dict[str, object]] = []
        self.ready = False
        self.load_ok = False
        self.channel_ok = False
        self._finished = False
        self.page_path = probe_page_path()
        self.setWindowTitle("WebEngine 打包探针")
        self.resize(960, 640)

        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        self.view = QWebEngineView()
        layout.addWidget(self.view)
        self.setCentralWidget(root)

        self.channel = QWebChannel(self.view.page())
        self.bridge = ProbeBridge(self)
        self.channel.registerObject("backend", self.bridge)
        self.view.page().setWebChannel(self.channel)
        self.view.loadFinished.connect(self._on_loaded)
        self.view.titleChanged.connect(self._on_title)
        self.view.setUrl(QUrl.fromLocalFile(str(self.page_path.resolve())))

        if self.self_test:
            QTimer.singleShot(20000, self._timeout)

    def record(self, event: str, payload: dict[str, object]) -> None:
        self.events.append({"event": event, **payload})

    def _on_loaded(self, ok: bool) -> None:
        self.load_ok = bool(ok)
        self.record("loadFinished", {"ok": bool(ok), "url": self.view.url().toString(), "page": str(self.page_path)})
        if not ok:
            self._finish(False, "page load failed")

    def _on_title(self, title: str) -> None:
        if title.startswith("PROBE_READY"):
            self.channel_ok = True
            self.ready = True
            self.record("ready", {"title": title})
            if self.self_test:
                self.bridge.choose_resource("self-test-choose")
                self._finish(True, "channel ready")

    def _timeout(self) -> None:
        if not self.ready:
            self._finish(False, "timed out waiting for QWebChannel")

    def _finish(self, ok: bool, reason: str) -> None:
        if self._finished:
            return
        self._finished = True
        report = {
            "ok": ok,
            "reason": reason,
            "frozen": bool(getattr(sys, "frozen", False)),
            "load_ok": self.load_ok,
            "channel_ok": self.channel_ok,
            "page": str(self.page_path),
            "page_exists": self.page_path.is_file(),
            "url": self.view.url().toString(),
            "events": self.events,
        }
        if self.report_path is not None:
            self.report_path.parent.mkdir(parents=True, exist_ok=True)
            self.report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        QTimer.singleShot(0, QApplication.instance().quit)


def main(argv: list[str] | None = None) -> int:
    os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
    args = parse_probe_args(sys.argv[1:] if argv is None else argv)
    if args.self_test and args.report is None:
        print("self-test requires --report", file=sys.stderr)
        return 2
    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)
    window = ProbeWindow(self_test=args.self_test, report_path=args.report)
    if args.self_test:
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
