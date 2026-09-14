"""Production desktop bootstrap. Avoids importing the legacy widget window."""

from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path

from mc_hanhua.gui_support import _parse_startup_request, sanitize_ssl_keylog_env

APP_TITLE = "MC 汉化器"


def _resource_path(relative: str) -> Path:
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    else:
        base = Path(__file__).resolve().parents[2]
    return base / relative


class StartupProfiler:
    """Monotonic-clock stages for optional --startup-profile. Never logs keys."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self.origin = time.perf_counter()
        self.marks: dict[str, float] = {}

    def mark(self, name: str) -> None:
        if not self.enabled:
            return
        self.marks[name] = round((time.perf_counter() - self.origin) * 1000, 1)

    def dump(self) -> dict[str, float]:
        return dict(self.marks)

    def write_report(self, path: Path | None) -> None:
        if not self.enabled:
            return
        payload = {"ok": True, "stages_ms": self.dump(), "frozen": bool(getattr(sys, "frozen", False))}
        text = json.dumps(payload, ensure_ascii=False)
        print(f"[startup-profile] {text}", file=sys.stderr)
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _register_ui_font(app) -> None:
    from PySide6.QtGui import QFont, QFontDatabase

    font_path = _resource_path("assets/NotoSansSC-VF.ttf")
    font_id = QFontDatabase.addApplicationFont(str(font_path)) if font_path.exists() else -1
    families = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
    app.setFont(QFont(families[0] if families else "Microsoft YaHei UI", 10))


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Must run before any HTTPS request and before Qt spawns its network
    # child processes, which inherit the environment as-is.
    sanitize_ssl_keylog_env()
    request = _parse_startup_request(argv)
    profiler = StartupProfiler(request.startup_profile)
    profiler.mark("python_entry")
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication

        from mc_hanhua.webui.window import HtmlMainWindow

        profiler.mark("core_imports")
        app = QApplication.instance() or QApplication(sys.argv)
        app.setStyle("Fusion")
        profiler.mark("qapplication")
        window = HtmlMainWindow(request, profiler=profiler)
        if request.html_self_test:
            window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        profiler.mark("window_constructed")
        window.show()
        profiler.mark("window_shown")
        QApplication.instance().processEvents()
        if not request.html_self_test:
            from PySide6.QtCore import QTimer

            QTimer.singleShot(0, lambda: _register_ui_font(app))
        else:
            _register_ui_font(app)
        sys.exit(app.exec())
    except Exception as exc:
        from mc_hanhua.diagnostics import write_failure_log

        log_path = write_failure_log(exc, operation="gui-startup")
        detail = traceback.format_exc()
        try:
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.critical(None, APP_TITLE, f"程序启动失败。\n\n日志：{log_path or '无法写入'}\n\n{detail}")
        finally:
            raise


if __name__ == "__main__":
    main()
