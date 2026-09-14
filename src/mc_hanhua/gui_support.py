from __future__ import annotations

import argparse
import base64
import ctypes
import os
import tempfile
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path


def sanitize_ssl_keylog_env() -> str:
    """Neutralise an unwritable SSLKEYLOGFILE inherited from the environment.

    Python honours SSLKEYLOGFILE for every HTTPS connection. Capture tools set
    it machine-wide, and a path inside a protected profile makes every request
    fail with PermissionError before it reaches the network — including model
    fetching and translation. Writable paths are kept so deliberate traffic
    inspection keeps working; only broken ones are dropped.
    """

    raw = os.environ.get("SSLKEYLOGFILE")
    if not raw:
        return ""
    try:
        with tempfile.TemporaryFile(dir=str(Path(raw).parent)):
            return raw
    except OSError:
        os.environ.pop("SSLKEYLOGFILE", None)
        return ""


@dataclass(frozen=True, slots=True)
class StartupRequest:
    input_path: Path | None = None
    output_path: Path | None = None
    start_immediately: bool = False
    runtime_log_path: Path | None = None
    html_self_test: bool = False
    report_path: Path | None = None
    startup_profile: bool = False


def _parse_startup_request(argv: list[str]) -> StartupRequest:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--input")
    parser.add_argument("--output")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--log")
    parser.add_argument("--html-self-test", action="store_true")
    parser.add_argument("--report")
    parser.add_argument("--startup-profile", action="store_true")
    args, _unknown = parser.parse_known_args(argv)
    input_path = Path(args.input).expanduser() if args.input else None
    output_path = Path(args.output).expanduser() if args.output else None
    runtime_log = Path(args.log).expanduser() if args.log else None
    report_path = Path(args.report).expanduser() if args.report else None
    return StartupRequest(
        input_path,
        output_path,
        bool(args.run),
        runtime_log,
        bool(args.html_self_test),
        report_path,
        bool(args.startup_profile),
    )


def _models_endpoint(base_url: str) -> str:
    base = base_url.strip().rstrip("/")
    if base.endswith("/models"):
        return base
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    return f"{base}/models"


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _win_crypto() -> tuple[object, object]:
    # windll only exists on Windows; resolve it lazily so importing this
    # module (tests, future CI) does not require the platform.
    crypt32 = ctypes.windll.crypt32
    ole32 = ctypes.windll.ole32
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    return crypt32, ole32


def _protect_text(text: str) -> str:
    raw = text.encode("utf-8")
    if not raw:
        return ""
    crypt32, ole32 = _win_crypto()
    in_buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    in_blob = DATA_BLOB(len(raw), in_buffer)
    out_blob = DATA_BLOB()
    if not crypt32.CryptProtectData(ctypes.byref(in_blob), "mc-hanhua api key", None, None, None, 0, ctypes.byref(out_blob)):
        raise ctypes.WinError()
    try:
        encrypted = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        return base64.b64encode(encrypted).decode("ascii")
    finally:
        ole32.CoTaskMemFree(out_blob.pbData)


def _unprotect_text(text: str) -> str:
    if not text:
        return ""
    try:
        crypt32, ole32 = _win_crypto()
        raw = base64.b64decode(text.encode("ascii"))
        in_buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
        in_blob = DATA_BLOB(len(raw), in_buffer)
        out_blob = DATA_BLOB()
        if not crypt32.CryptUnprotectData(ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)):
            return ""
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData).decode("utf-8")
        finally:
            ole32.CoTaskMemFree(out_blob.pbData)
    except Exception:
        return ""
