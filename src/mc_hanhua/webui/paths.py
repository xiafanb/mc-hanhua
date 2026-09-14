"""Resource paths for the HTML desktop shell. No Qt imports."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def resource_path(relative: str) -> Path:
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    else:
        base = Path(__file__).resolve().parents[3]
    return base / relative


def probe_page_path() -> Path:
    packaged = resource_path("src/mc_hanhua/webui/probe_page.html")
    if packaged.is_file():
        return packaged
    local = Path(__file__).with_name("probe_page.html")
    if local.is_file():
        return local
    raise FileNotFoundError("probe_page.html is missing from the application bundle")


def parse_probe_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="WebEngine packaging probe")
    parser.add_argument("--self-test", action="store_true", help="Load the page, wait for the channel, write a report, then exit")
    parser.add_argument("--report", type=Path, default=None, help="JSON report path used with --self-test")
    return parser.parse_args(argv)


def app_page_path() -> Path:
    packaged = resource_path("src/mc_hanhua/webui/app.html")
    if packaged.is_file():
        return packaged
    local = Path(__file__).with_name("app.html")
    if local.is_file():
        return local
    raise FileNotFoundError("app.html is missing from the application bundle")


def icon_path() -> Path:
    packaged = resource_path("assets/tubiao.png")
    if packaged.is_file():
        return packaged
    local = Path(__file__).resolve().parents[3] / "assets" / "tubiao.png"
    if local.is_file():
        return local
    raise FileNotFoundError("assets/tubiao.png is missing")


def ensure_page_icon() -> Path:
    destination = app_page_path().with_name("tubiao.png")
    if destination.is_file():
        return destination
    destination.write_bytes(icon_path().read_bytes())
    return destination


SHELL_STATES = (
    "initial",
    "loaded",
    "scanned",
    "card-expanded-edit",
    "prefs-open",
    "connection-dialog",
    "history-dialog",
    "summary-dialog",
    "complete",
    "filter-lock",
)


def parse_shell_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="HTML desktop visual shell (fixed data, not the production entry)")
    parser.add_argument("--self-test", action="store_true", help="Load the page, apply fixture states, write a report, then exit")
    parser.add_argument("--report", type=Path, default=None, help="JSON report path used with --self-test")
    parser.add_argument("--state", choices=SHELL_STATES, default="initial", help="Fixture state to show")
    parser.add_argument("--capture-dir", type=Path, default=None, help="Capture a PNG of each fixture state into this directory")
    parser.add_argument("--width", type=int, default=1680)
    parser.add_argument("--height", type=int, default=900)
    return parser.parse_args(argv)
