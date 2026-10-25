"""Task controller for the HTML desktop shell. No Qt widgets."""

from __future__ import annotations

import json
import os
import time
import urllib.error
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from mc_hanhua.archives import ARCHIVE_SUFFIXES
from mc_hanhua.gui_support import _models_endpoint, _protect_text, _unprotect_text
from mc_hanhua.map_version import default_output_path
from mc_hanhua.models import (
    ArchiveResult,
    ProgressEvent,
    TranslationReport,
    summarize_failure_counts,
)
from mc_hanhua.quality import validate_contextual_target
from mc_hanhua.task_records import atomic_write_json, input_fingerprint, load_manifest
from mc_hanhua.utils import UNREADABLE_TEXT_ERRORS
from mc_hanhua.webui.present import (
    REPORT_RECORD_KIND,
    button_for_state,
    chinese_reason,
    completion_title,
    estimate_cost,
    format_amount,
    format_duration_ms,
    issue_count,
    metrics_texts,
    model_price_for,
    npc_skip_keys,
    present_record,
    present_scan_result,
    queue_start_state,
    stats_from_progress,
    stats_from_report,
    unfinished_note,
    unfinished_review_hint,
    unfinished_units,
    usage_line,
)

APP_TITLE = "Minecraft 汉化工具"
DEFAULT_BASE_URL = "https://token-plan-cn.xiaomimimo.com/v1"
MODEL_CACHE_TTL_SEC = 24 * 60 * 60
QUEUE_LIMIT = 50
RUNTIME_PROBLEM_LIMIT = 500
RUNTIME_PROBLEM_SNAPSHOT_LIMIT = 200
# Log lines a snapshot carries when the page has no cursor yet (first load or a
# reconnect); with a cursor it only carries what is new.
LOG_TAIL_MAX = 200
# Failure reasons that mean "the service itself is broken", logged as errors
# rather than warnings in the runtime problem feed.
ERROR_LEVEL_REASONS = {
    "missing_api_key",
    "network_disconnected",
    "network_error",
    "timeout",
    "rate_limited",
    "server_error",
    "http_error",
    "unexpected_error",
}
KEY_ACTIONS = {"keep", "replace", "clear"}
STAGE_TITLES = {
    "prepare": "准备输入",
    "scan": "扫描",
    "plan": "历史复用",
    "ai": "翻译与审校",
    "repair": "修复",
    "review": "审校",
    "write": "写回",
    "audit": "写回审计",
    "nested": "内嵌包",
    "pack": "打包",
    "save": "保存",
    "complete": "汉化完成",
    "blocked": "待处理",
    "paused": "已暂停",
    "failed": "失败",
}


def config_path() -> Path:
    base = Path(os.getenv("APPDATA") or Path.home() / "AppData" / "Roaming")
    return base / "mc-hanhua" / "gui-config.json"


def archive_output(path: Path) -> Path:
    return default_output_path(path)


def unique_output_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.stem}_{index}{path.suffix}") if path.suffix else path.with_name(f"{path.name}_{index}")
        if not candidate.exists():
            return candidate
    return path


def job_log_path(output_path: Path) -> Path:
    directory = output_path.resolve().parent / ".mc-hanhua" / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"job-{datetime.now():%Y%m%d-%H%M%S}.log"


def model_request_is_current(
    request: dict[str, Any] | None,
    *,
    active_request: dict[str, Any] | None,
    credential_version: int,
) -> bool:
    """True when a finished model fetch still answers the live connection.

    ``credential_version`` 0 is a legitimate fresh-session value; a missing or
    unparsable version means the request predates version tracking and is
    treated as stale, never as "always current".
    """

    if not request:
        return False
    raw = request.get("credential_version")
    if raw is None:
        return False
    try:
        version = int(raw)
    except (TypeError, ValueError):
        return False
    if version != credential_version:
        return False
    if active_request and str(active_request.get("request_id") or "") != str(request.get("request_id") or ""):
        return False
    return True


class TaskController:
    """Owns task state, config, history and presentation snapshots."""

    def __init__(self, *, config_file: Path | None = None, emit: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.config_file = config_file or config_path()
        self.emit = emit or (lambda _payload: None)
        self.task_id = uuid4().hex
        self.state = "idle"
        self.input_path: Path | None = None
        self.output_path: Path | None = None
        self.translate_npc = True
        self.records: list[dict[str, Any]] = []
        self.logs: list[dict[str, str]] = []
        # Monotonic count of log lines ever appended; the page sends back the
        # last value it saw so a snapshot only carries the lines it is missing.
        self.log_revision = 0
        self.manual_targets: dict[str, str] = {}
        self.last_report: TranslationReport | None = None
        self.resume_pending = False
        self.stop_requested = False
        self.percent = 0
        self.status_title = "等待开始"
        self.status_desc = "选择输入资源后即可开始"
        self.stage_text = "准备 · 扫描 · 翻译与审校 · 写回审计 · 打包"
        self.stats = {"scanned": 0, "groups": 0, "ai_passed": 0, "issues": 0}
        self.config = self.load_config()
        if "translate_npc" in self.config:
            self.translate_npc = bool(self.config.get("translate_npc"))
        # D (DEVELOPMENT §3.14): off unless the user turns it on.
        self.passage_sentence = bool(self.config.get("passage_sentence", False))
        self.passage_names = str(self.config.get("passage_names") or "")
        self.api_key = ""
        self._key_unavailable = False
        self._credential_version = 0
        self._model_cache: dict[str, dict[str, Any]] = {}
        self._apply_saved_key()
        self.busy = False
        self.current_request_id = ""
        # Translation queue: in-memory FIFO consumed by the window between
        # tasks; survives resets but not an app restart.
        self.queue: list[dict[str, Any]] = []
        self.auto_continue = bool(self.config.get("auto_continue", True))
        # Per-task wall-clock and the last token usage seen in progress events.
        # Events without usage (e.g. ai-result) must not reset the numbers to
        # zero — the last known values stay until the task ends.
        self.task_started_mono: float | None = None
        self.task_elapsed_ms = 0
        self.last_usage: dict[str, int] = {}
        self.last_usage_requests = 0
        # Batch totals across a queue session: every terminal task (complete,
        # blocked, failed) consumed tokens, so all three accumulate.
        self.session_totals: dict[str, float] = {"tasks": 0, "elapsed_ms": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}
        # Runtime problems surfaced live while a task runs; each carries a
        # ``ref`` back to its log entry for the problem<->log jump.
        self.runtime_problems: list[dict[str, Any]] = []
        self._failure_seen: dict[str, int] = {}
        self._last_error_text = ""
        self._runtime_seq = 0

    def _reset_runtime_tracking(self) -> None:
        self.task_started_mono = None
        self.task_elapsed_ms = 0
        self.last_usage = {}
        self.last_usage_requests = 0
        self.runtime_problems = []
        self._failure_seen = {}
        self._last_error_text = ""

    def load_config(self) -> dict[str, Any]:
        try:
            data = json.loads(self.config_file.read_text(encoding="utf-8"))
        except UNREADABLE_TEXT_ERRORS:
            return {}
        return data if isinstance(data, dict) else {}

    def _apply_saved_key(self) -> None:
        stored = str(self.config.get("api_key_dpapi") or "")
        if not self.config.get("remember_api_key"):
            return
        key = _unprotect_text(stored) if stored else ""
        if key:
            self.api_key = key
            self._key_unavailable = False
            self._log("已加载加密保存的 API Key。")
        elif stored:
            self._key_unavailable = True
            self._log("已保存的 API Key 无法解密（可能更换了电脑或 Windows 账户），请在“连接控制”重新填写。")

    def key_state(self) -> str:
        if self.api_key:
            return "saved" if bool(self.config.get("remember_api_key")) else "session"
        if self._key_unavailable and str(self.config.get("api_key_dpapi") or ""):
            return "unavailable"
        return "missing"

    def credential_endpoint(self, base_url: str | None = None) -> str:
        return _models_endpoint(base_url or self.base_url())

    def save_config(self) -> None:
        data = self._config_payload()
        atomic_write_json(self.config_file, data)
        self.config = data

    def _config_payload(
        self,
        *,
        api_key: str | None = None,
        remember: bool | None = None,
        unavailable: bool | None = None,
    ) -> dict[str, Any]:
        remember_api_key = bool(self.config.get("remember_api_key") if remember is None else remember)
        key = self.api_key if api_key is None else api_key
        key_unavailable = self._key_unavailable if unavailable is None else unavailable
        data: dict[str, Any] = {
            "base_url": self.base_url(),
            "model": self.model(),
            "provider": self.provider(),
            "max_ai": self.config.get("max_ai", 0),
            "ai_workers": self.workers(),
            "quality_policy_version": 1,
            "remember_api_key": remember_api_key,
            "official_terms_dir": self.config.get("official_terms_dir", ""),
            "official_terms_version": self.config.get("official_terms_version", ""),
            "translate_npc": self.translate_npc,
            "passage_sentence": self.passage_sentence,
            "passage_names": self.passage_names,
            "auto_continue": self.auto_continue,
            "notify_system": self.notify_system_enabled(),
            "notify_sound": self.notify_sound_enabled(),
            "model_prices": self.config.get("model_prices", {}),
            "recent_tasks": self.config.get("recent_tasks", []),
        }
        stored = str(self.config.get("api_key_dpapi") or "")
        if remember_api_key and key:
            data["api_key_dpapi"] = _protect_text(key)
        elif remember_api_key and key_unavailable and stored and not key:
            # Keep the original ciphertext until the user replaces or clears it.
            data["api_key_dpapi"] = stored
        return data

    def base_url(self) -> str:
        return str(self.config.get("base_url") or DEFAULT_BASE_URL)

    def model(self) -> str:
        return str(self.config.get("model") or "mimo-v2.5")

    def provider(self) -> str:
        """Translation provider id; configs without the field keep chat mode."""

        from mc_hanhua.translator import normalize_provider

        return normalize_provider(self.config.get("provider"))

    def workers(self) -> int:
        try:
            value = int(self.config.get("ai_workers", 1))
        except (TypeError, ValueError):
            value = 1
        return min(6, max(1, value))

    def max_ai(self) -> int | None:
        try:
            value = int(self.config.get("max_ai") or 0)
        except (TypeError, ValueError):
            value = 0
        return value or None

    def set_input(self, path: Path) -> dict[str, Any]:
        if self.busy:
            return self._reject("任务进行中，不能更换输入。")
        if not path.exists():
            return self._reject("输入路径不存在。")
        self.input_path = path
        self.output_path = unique_output_path(archive_output(path))
        self.records = []
        self.manual_targets = {}
        self.last_report = None
        self.resume_pending = False
        self.percent = 0
        self.stats = {"scanned": 0, "groups": 0, "ai_passed": 0, "issues": 0}
        self._reset_runtime_tracking()
        self.state = "ready"
        self.status_title = "等待开始"
        kind = "文件夹" if path.is_dir() else ("压缩包" if path.suffix.lower() in ARCHIVE_SUFFIXES else "文件")
        self.status_desc = f"{kind}已选择，可扫描或开始汉化。"
        self._log(f"输入：{path}")
        return self.snapshot(ok=True)

    def set_output(self, path: Path) -> dict[str, Any]:
        if self.busy:
            return self._reject("任务进行中，不能更改输出。")
        if self.input_path and path.resolve() == self.input_path.resolve():
            return self._reject("输出路径不能与输入路径相同。")
        if self.input_path is not None:
            from mc_hanhua.pipeline import reject_output_inside_input

            try:
                reject_output_inside_input(self.input_path, path)
            except ValueError as exc:
                return self._reject(str(exc))
        self.output_path = path
        return self.snapshot(ok=True)

    def set_npc(self, enabled: bool) -> dict[str, Any]:
        from mc_hanhua.policy import POLICY_TRANSLATABLE
        from mc_hanhua.webui.present import tag_for

        previous = self.translate_npc
        self.translate_npc = bool(enabled)
        self.config["translate_npc"] = self.translate_npc
        try:
            self.save_config()
        except Exception as exc:
            self.translate_npc = previous
            self.config["translate_npc"] = previous
            return self._reject(f"偏好保存失败：{exc}")
        for record in self.records:
            preference_keep = (not self.translate_npc) and record.get("display_kind") == "entity_display_name" and record.get("policy") == POLICY_TRANSLATABLE
            record["preference_keep"] = preference_keep
            record["editable"] = bool(record.get("policy") == POLICY_TRANSLATABLE and not preference_keep and not record.get("record_kind") == "filtered")
            record["will_send_ai"] = record["editable"]
            record["tag"] = tag_for(str(record.get("policy") or ""), preference_keep=preference_keep)
        return self.snapshot(ok=True)

    def set_passage_sentence(self, enabled: bool) -> dict[str, Any]:
        previous = self.passage_sentence
        self.passage_sentence = bool(enabled)
        try:
            self.save_config()
        except Exception as exc:
            self.passage_sentence = previous
            return self._reject(f"偏好保存失败：{exc}")
        return self.snapshot(ok=True)

    def set_passage_names(self, path: str) -> dict[str, Any]:
        """Map name config for D; ``""`` clears it.  The file must load as a name config."""

        from mc_hanhua.passage_sentence import load_name_config

        text = str(path or "").strip().strip('"')
        if text:
            try:
                names = load_name_config(Path(text))
            except (OSError, ValueError) as exc:
                return self._reject(f"名称配置无法读取：{exc}")
            label = f"名称配置已选择：{len(names.names)} 个名称"
        else:
            label = "已清除名称配置"
        previous = self.passage_names
        self.passage_names = text
        try:
            self.save_config()
        except Exception as exc:
            self.passage_names = previous
            return self._reject(f"偏好保存失败：{exc}")
        return self.snapshot(ok=True, toast=label)

    def resolve_fetch_credentials(self, payload: dict[str, Any] | None = None) -> tuple[str, str, str]:
        """Pick the key that belongs to the requested endpoint.

        Same normalised endpoint may reuse the in-memory key; a different
        address never inherits another connection's secret. An explicit
        non-empty ``api_key`` in the payload always wins for that request.
        """

        payload = payload or {}
        requested = str(payload.get("base_url") or "").strip()
        typed_key = str(payload.get("api_key") or "")
        saved_endpoint = self.credential_endpoint()
        if requested:
            target_url = requested
            target_endpoint = self.credential_endpoint(requested)
        else:
            target_url = self.base_url()
            target_endpoint = saved_endpoint
        if typed_key:
            return target_url, typed_key, target_endpoint
        if target_endpoint == saved_endpoint:
            return target_url, self.api_key, target_endpoint
        return target_url, "", target_endpoint

    def cached_models(self, endpoint: str, credential_version: int | None = None) -> list[str] | None:
        entry = self._model_cache.get(endpoint)
        if not entry:
            return None
        if credential_version is not None:
            # Version 0 is a legitimate fresh-session value; only a missing or
            # unparsable version means "cannot match" (A13).
            raw = entry.get("credential_version")
            if raw is None:
                return None
            try:
                version = int(raw)
            except (TypeError, ValueError):
                return None
            if version != credential_version:
                return None
        age = datetime.now().timestamp() - float(entry.get("saved_at") or 0)
        if age > MODEL_CACHE_TTL_SEC:
            return None
        models = entry.get("models")
        return list(models) if isinstance(models, list) else None

    def store_model_cache(self, endpoint: str, models: list[str], credential_version: int) -> None:
        self._model_cache[endpoint] = {
            "models": list(models),
            "saved_at": datetime.now().timestamp(),
            "credential_version": credential_version,
        }

    def fetch_models(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Probe the configured API for available models without saving config.

        Mirrors the Qt ``ConnectionDialog`` "拉取模型" action. The API key is
        only sent to the server, never recorded in logs or the snapshot.
        Synchronous callers (offline tests) still use this path; the HTML
        window starts a worker and later calls ``on_models_loaded``.
        """

        from mc_hanhua.translator import fetch_available_models

        prepared = self.prepare_fetch_models(payload)
        if not prepared.get("ok"):
            return prepared
        base_url, api_key, endpoint = self.resolve_fetch_credentials(payload)
        request_id = str(prepared.get("request_id") or "")
        credential_version = int(prepared.get("credential_version") or self._credential_version)
        try:
            models = fetch_available_models(base_url, api_key)
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            OSError,
            TimeoutError,
            UnicodeDecodeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            return self.on_models_failed(str(exc) or type(exc).__name__, request_id=request_id, endpoint=endpoint)
        return self.on_models_loaded(models, request_id=request_id, endpoint=endpoint, credential_version=credential_version)

    def prepare_fetch_models(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.busy:
            return self._reject("任务进行中，不能修改连接配置。")
        base_url, api_key, endpoint = self.resolve_fetch_credentials(payload)
        cached = self.cached_models(endpoint, self._credential_version)
        request_id = uuid4().hex
        extra: dict[str, Any] = {
            "accepted": True,
            "request_id": request_id,
            "base_url": base_url,
            "endpoint": endpoint,
            "credential_version": self._credential_version,
        }
        if cached:
            extra["cached_models"] = cached
        return self.snapshot(ok=True, model_status="正在拉取可用模型…", **extra)

    def on_models_loaded(
        self,
        models: list[str],
        *,
        request_id: str = "",
        endpoint: str = "",
        credential_version: int | None = None,
    ) -> dict[str, Any]:
        version = self._credential_version if credential_version is None else credential_version
        if endpoint:
            self.store_model_cache(endpoint, models, version)
        return self.snapshot(
            ok=True,
            models=list(models),
            model_status=f"已获取 {len(models)} 个可用模型",
            request_id=request_id,
            endpoint=endpoint,
            credential_version=version,
        )

    def on_models_failed(
        self,
        message: str,
        *,
        request_id: str = "",
        endpoint: str = "",
        cached: list[str] | None = None,
    ) -> dict[str, Any]:
        safe = self._public_error(message)
        models = list(cached) if cached is not None else (self.cached_models(endpoint) or [])
        return self.snapshot(
            ok=False,
            models=models,
            model_status=f"拉取模型失败：{safe}；可继续手动填写。",
            toast="拉取模型失败，可继续手动填写模型名。",
            request_id=request_id,
            endpoint=endpoint,
        )

    def apply_connection(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.busy:
            return self._reject("任务进行中，不能修改连接配置。")
        previous_config = dict(self.config)
        previous_key = self.api_key
        previous_unavailable = self._key_unavailable
        previous_version = self._credential_version
        previous_endpoint = self.credential_endpoint()

        key_action = str(payload.get("key_action") or "").strip().lower()
        incoming_key = str(payload.get("api_key") or "")
        if not key_action:
            if incoming_key:
                key_action = "replace"
            else:
                key_action = "keep"
        if key_action not in KEY_ACTIONS:
            return self._reject("无效的密钥操作。")
        if key_action == "replace" and not incoming_key.strip():
            return self._reject("替换密钥时必须提供非空 API Key。")

        if "base_url" in payload:
            self.config["base_url"] = str(payload.get("base_url") or DEFAULT_BASE_URL)
        if "model" in payload:
            self.config["model"] = str(payload.get("model") or "mimo-v2.5")
        if "provider" in payload:
            from mc_hanhua.translator import normalize_provider

            self.config["provider"] = normalize_provider(payload.get("provider"))
        if "workers" in payload:
            try:
                self.config["ai_workers"] = min(6, max(1, int(payload.get("workers") or 1)))
            except (TypeError, ValueError):
                self.config["ai_workers"] = 1
        if "max_ai" in payload:
            try:
                self.config["max_ai"] = int(payload.get("max_ai") or 0)
            except (TypeError, ValueError):
                self.config["max_ai"] = 0
        if "remember_api_key" in payload:
            self.config["remember_api_key"] = bool(payload.get("remember_api_key"))

        if any(key in payload for key in ("price_input", "price_output", "price_currency")):
            prices = dict(self.config.get("model_prices") or {})
            if not isinstance(prices, dict):
                prices = {}
            model = str(self.config.get("model") or "mimo-v2.5")
            entry = dict(prices.get(model) or {}) if isinstance(prices.get(model), dict) else {}
            for source_key, target_key in (("price_input", "input_per_m"), ("price_output", "output_per_m")):
                raw = payload.get(source_key)
                if raw is None or str(raw).strip() == "":
                    entry.pop(target_key, None)
                    continue
                try:
                    value = float(raw)
                except (TypeError, ValueError):
                    return self._reject("模型单价必须是数字。")
                if value < 0:
                    return self._reject("模型单价不能为负数。")
                entry[target_key] = value
            currency = str(payload.get("price_currency") or entry.get("currency") or "CNY")
            entry["currency"] = "USD" if currency.upper() == "USD" else "CNY"
            if "input_per_m" in entry or "output_per_m" in entry:
                prices[model] = entry
            else:
                prices.pop(model, None)
            self.config["model_prices"] = prices

        candidate_key = previous_key
        candidate_unavailable = previous_unavailable
        endpoint_changed = self.credential_endpoint() != previous_endpoint
        if key_action == "replace":
            candidate_key = incoming_key
            candidate_unavailable = False
        elif key_action == "clear":
            candidate_key = ""
            candidate_unavailable = False
        elif endpoint_changed and candidate_key:
            # A05: a key belongs to the endpoint it was issued for. Keeping the
            # old service's secret after switching addresses would leak it to
            # the new endpoint; start unauthenticated instead.
            candidate_key = ""
            candidate_unavailable = False
            self._log("接口地址已更换，原服务的 API Key 已停止沿用；请填写新服务的密钥。")

        try:
            data = self._config_payload(
                api_key=candidate_key,
                remember=bool(self.config.get("remember_api_key")),
                unavailable=candidate_unavailable,
            )
            atomic_write_json(self.config_file, data)
        except Exception as exc:
            self.config = previous_config
            self.api_key = previous_key
            self._key_unavailable = previous_unavailable
            self._credential_version = previous_version
            return self._reject(f"配置保存失败：{self._public_error(str(exc))}")

        self.config = data
        self.api_key = candidate_key
        self._key_unavailable = candidate_unavailable
        if (
            key_action in {"replace", "clear"}
            or previous_key != candidate_key
            or self.credential_endpoint() != previous_endpoint
        ):
            self._credential_version = previous_version + 1
        self._log("连接设置已保存。")
        return self.snapshot(ok=True, toast="已应用连接配置")

    # ------------------------------------------------------------------
    # Translation queue
    # ------------------------------------------------------------------

    #: One rejection per unusable drop candidate, so a batch can report the mix
    #: instead of one toast per file.
    QUEUE_REJECTIONS = {
        "duplicate": ("已在队列", "该资源已在队列中。"),
        "missing": ("路径不存在", "加入队列的路径不存在。"),
        "full": ("队列已满", f"队列已满（最多 {QUEUE_LIMIT} 个），请先处理或清空。"),
    }

    def _try_enqueue(self, path: Path) -> str:
        """``added`` / ``missing`` / ``duplicate`` / ``full`` for one candidate."""

        if not path.exists():
            return "missing"
        key = str(path.resolve())
        if any(str(Path(str(item.get("input") or "")).resolve()) == key for item in self.queue):
            return "duplicate"
        if len(self.queue) >= QUEUE_LIMIT:
            return "full"
        output = unique_output_path(archive_output(path))
        self.queue.append(
            {
                "id": uuid4().hex[:8],
                "name": path.name,
                "input": str(path),
                "output": str(output),
                "added_at": datetime.now().strftime("%H:%M:%S"),
            }
        )
        self._log(f"已加入队列：{path.name}（等待 {len(self.queue)} 个）")
        return "added"

    def enqueue_task(self, path: Path) -> dict[str, Any]:
        outcome = self._try_enqueue(path)
        if outcome != "added":
            return self._reject(self.QUEUE_REJECTIONS[outcome][1])
        return self.snapshot(ok=True, toast=f"已加入队列：{path.name}")

    def enqueue_paths(self, paths: list[Path]) -> dict[str, Any]:
        """Queue a whole drop: one summary, with the reasons nothing else landed.

        Silent rejection is what makes a batch drop feel broken — the user drops
        five files, two are already queued and the queue is nearly full, and a
        single "已加入队列 1 个" says nothing about the other four.
        """

        outcomes = [self._try_enqueue(Path(path)) for path in paths]
        added = outcomes.count("added")
        detail = "、".join(
            f"{outcomes.count(outcome)} 个{label}"
            for outcome, (label, _reason) in self.QUEUE_REJECTIONS.items()
            if outcomes.count(outcome)
        )
        if not added:
            return self._reject(f"没有加入任何任务：{detail}" if detail else "没有可加入的资源。")
        if detail:
            self._log(f"本次拖入 {len(paths)} 个：加入队列 {added} 个，{detail}")
            return self.snapshot(ok=True, toast=f"已加入队列 {added} 个（{detail}）")
        return self.snapshot(ok=True, toast=f"已加入队列 {added} 个")

    def queue_remove(self, index: int) -> dict[str, Any]:
        if not 0 <= index < len(self.queue):
            return self._reject("队列条目不存在。")
        removed = self.queue.pop(index)
        return self.snapshot(ok=True, toast=f"已移除：{removed.get('name')}")

    def queue_clear(self) -> dict[str, Any]:
        count = len(self.queue)
        self.queue = []
        return self.snapshot(ok=True, toast=f"已清空 {count} 个排队任务" if count else "队列已是空的")

    def queue_pop_head(self) -> dict[str, Any] | None:
        return self.queue.pop(0) if self.queue else None

    def claim_queue_head(self) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        """Pop the queue head into the input slot, or explain why it must not.

        The explicit "start next" entry must never run ahead of an input the user
        has not started yet: the window used to ``set_input`` the head
        unconditionally, which replaced a pending pack (``set_input`` clears
        ``manual_targets`` and the records) and never put it back in the queue —
        a silent task loss for exactly the drop pattern this round introduced.
        Advancing after a *completed* task still works, because a completed input
        is not pending.

        Returns ``(entry, snapshot)``; ``entry is None`` means nothing was taken
        and the snapshot carries the reason.
        """

        if self.busy:
            return None, self.snapshot(ok=False, error="任务进行中，队列稍后再继续。")
        if not self.queue:
            return None, self.snapshot(ok=False, error="队列为空。")
        if queue_start_state(
            self.state, busy=False, has_input=self.input_path is not None, has_queue=True
        ) == "disabled":
            return None, self.snapshot(
                ok=False,
                error="当前输入还没开始处理，队列不会跳过它；先点「开始汉化」，或用「选择资源」换掉当前输入。",
                toast="当前输入还没开始，队列不会跳过它",
            )
        entry = self.queue_pop_head()
        if entry is None:
            return None, self.snapshot(ok=False, error="队列为空。")
        input_path = Path(str(entry.get("input") or ""))
        output_path = Path(str(entry.get("output") or ""))
        loaded = self.set_input(input_path)
        if not loaded.get("ok"):
            self.queue.insert(0, entry)
            return None, loaded
        if output_path.exists():
            output_path = unique_output_path(output_path)
        placed = self.set_output(output_path)
        if not placed.get("ok"):
            # 输入槽已经换成队首条目，输出没设成功：把它退回队首，别让它既不
            # 在队列里也没被启动。
            self.queue.insert(0, entry)
            return None, placed
        return entry, self.snapshot(ok=True)

    def set_auto_continue(self, enabled: bool) -> dict[str, Any]:
        previous = self.auto_continue
        self.auto_continue = bool(enabled)
        self.config["auto_continue"] = self.auto_continue
        try:
            self.save_config()
        except Exception as exc:
            self.auto_continue = previous
            self.config["auto_continue"] = previous
            return self._reject(f"偏好保存失败：{exc}")
        return self.snapshot(ok=True)

    # ------------------------------------------------------------------
    # Completion notifications
    # ------------------------------------------------------------------

    def notify_system_enabled(self) -> bool:
        return bool(self.config.get("notify_system", True))

    def notify_sound_enabled(self) -> bool:
        return bool(self.config.get("notify_sound", False))

    def set_notify(self, *, system: bool | None = None, sound: bool | None = None) -> dict[str, Any]:
        previous = (self.notify_system_enabled(), self.notify_sound_enabled())
        if system is not None:
            self.config["notify_system"] = bool(system)
        if sound is not None:
            self.config["notify_sound"] = bool(sound)
        try:
            self.save_config()
        except Exception as exc:
            self.config["notify_system"], self.config["notify_sound"] = previous
            return self._reject(f"偏好保存失败：{exc}")
        return self.snapshot(ok=True)

    def _terminal_notify(self, kind: str, detail: str, *, title: str = "") -> dict[str, str]:
        """System-notification and banner payload for a terminal state.

        ``title`` overrides the terminal label only where the state itself needs
        a qualifier (a published run that still has texts left to translate);
        notification permissions stay untouched either way.
        """

        titles = {
            "complete": "汉化完成",
            "blocked": "任务待处理",
            "failed": "任务失败",
            "paused": "任务已暂停",
        }
        parts = [detail] if detail else []
        if self.task_elapsed_ms:
            parts.append(f"耗时 {format_duration_ms(self.task_elapsed_ms)}")
        usage_text = usage_line(self.last_usage or None, self.last_usage_requests)
        if usage_text:
            parts.append(usage_text)
        price = model_price_for(self.config.get("model_prices"), self.model())
        cost_text = format_amount(estimate_cost(self.last_usage or None, price), str((price or {}).get("currency") or "CNY"))
        if cost_text:
            parts.append(cost_text)
        return {"kind": kind, "title": title or titles.get(kind, "任务状态更新"), "body": " · ".join(parts)}

    # ------------------------------------------------------------------
    # Elapsed time / token / cost metrics
    # ------------------------------------------------------------------

    def mark_task_started(self) -> None:
        self.task_started_mono = time.monotonic()
        self.task_elapsed_ms = 0

    def _freeze_elapsed(self) -> int:
        if self.task_started_mono is None:
            return int(self.task_elapsed_ms or 0)
        return int((time.monotonic() - self.task_started_mono) * 1000)

    def _metrics_texts(self) -> dict[str, str]:
        elapsed = self.task_elapsed_ms
        if not elapsed and self.task_started_mono is not None and self.busy:
            # A started task always shows a live value, even in its first
            # millisecond, so the strip appears as soon as translation begins.
            elapsed = max(1, self._freeze_elapsed())
        price = model_price_for(self.config.get("model_prices"), self.model())
        return metrics_texts(elapsed_ms=elapsed, usage=self.last_usage or None, reported_requests=self.last_usage_requests, price=price)

    def _accumulate_session(self) -> None:
        usage = self.last_usage or {}
        price = model_price_for(self.config.get("model_prices"), self.model())
        cost = estimate_cost(usage, price)
        totals = self.session_totals
        totals["tasks"] = int(totals.get("tasks", 0)) + 1
        totals["elapsed_ms"] = int(totals.get("elapsed_ms", 0)) + int(self.task_elapsed_ms or 0)
        totals["prompt_tokens"] = int(totals.get("prompt_tokens", 0)) + int(usage.get("prompt_tokens") or 0)
        totals["completion_tokens"] = int(totals.get("completion_tokens", 0)) + int(usage.get("completion_tokens") or 0)
        totals["cost"] = float(totals.get("cost", 0.0)) + (cost or 0.0)

    # ------------------------------------------------------------------
    # Runtime problem feed (problem <-> log sync)
    # ------------------------------------------------------------------

    def _add_runtime_problem(self, reason: str, event: ProgressEvent, *, source: str = "") -> str:
        self._runtime_seq += 1
        problem_id = f"rt-{self._runtime_seq}"
        why = chinese_reason(reason)
        origin = source or str(event.current_file or "") or str(event.message or "")
        record = {
            "id": problem_id,
            "record_kind": "runtime",
            "is_problem": True,
            "issue": True,
            "type": "runtime",
            "policy": "UNKNOWN_REVIEW",
            "role": "运行期问题",
            "source": origin or why,
            "target": "",
            "why": why,
            "why_code": reason,
            "path": str(event.archive_id or "") or "运行中",
            "full_path": str(event.current_file or ""),
            "locator": "",
            "time": datetime.now().strftime("%H:%M:%S"),
            "tag": "运行失败",
            "editable": False,
        }
        self.runtime_problems.append(record)
        if len(self.runtime_problems) > RUNTIME_PROBLEM_LIMIT:
            self.runtime_problems = self.runtime_problems[-RUNTIME_PROBLEM_LIMIT:]
        level = "error" if reason in ERROR_LEVEL_REASONS else "warning"
        self._log(f"运行期问题：{why} · {origin}", level=level, ref=problem_id)
        return problem_id

    def _absorb_runtime_problems(self, event: ProgressEvent) -> None:
        """Diff cumulative failure counters into per-item problem records.

        ``failure_counts`` accumulates over the whole task, so the delta
        against the previously seen value is exactly the number of new
        failures since the last progress frame.
        """

        counts = event.failure_counts or {}
        for reason in sorted(counts):
            total = int(counts[reason] or 0)
            delta = total - self._failure_seen.get(reason, 0)
            self._failure_seen[reason] = total
            for _ in range(min(delta, 10)):
                self._add_runtime_problem(reason, event)
        error_text = self._public_error(str(event.last_error or ""))
        if error_text and error_text != self._last_error_text:
            self._last_error_text = error_text
            self._add_runtime_problem("unexpected_error", event, source=error_text)

    def on_scan_result(self, result: Any) -> dict[str, Any]:
        self.records = present_scan_result(result, translate_npc=self.translate_npc)
        self._reload_manual_targets()
        self.state = "scanned"
        self.percent = 18
        scanned = len(getattr(result, "text_units", []) or [])
        self.stats = {"scanned": scanned, "groups": 0, "ai_passed": 0, "issues": issue_count(self.records)}
        self.status_title = "扫描完成"
        self.status_desc = f"扫描完成：{scanned} 条文本。" if scanned else "未找到可翻译文本。"
        self.busy = False
        self._log(self.status_desc)
        return self.snapshot(ok=True, view="preview")

    def _reload_manual_targets(self) -> None:
        """Re-apply saved manual revisions to freshly scanned records (A10)."""

        if self.output_path is None:
            return
        if not self.manual_targets:
            path = self.output_path.parent / ".mc-hanhua" / (self.output_path.name + ".manual.json")
            if path.is_file():
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    targets = payload.get("targets") if isinstance(payload, dict) else None
                    if isinstance(targets, dict):
                        self.manual_targets = {str(key): str(value) for key, value in targets.items()}
                except (OSError, ValueError, json.JSONDecodeError):
                    pass
        if not self.manual_targets:
            return
        applied = 0
        for record in self.records:
            target = self.manual_targets.get(str(record.get("id") or ""))
            if target and record.get("editable"):
                record["target"] = target
                record["tag"] = "手工修订"
                applied += 1
        if applied:
            self._log(f"已恢复 {applied} 条手工修订草稿。")

    def on_scan_failed(self, message: str) -> dict[str, Any]:
        self.busy = False
        self.state = "failed" if self.input_path else "idle"
        self.status_title = "扫描失败"
        self.status_desc = message
        self._log(f"扫描失败：{message}", level="error")
        return self.snapshot(ok=False, error=message)

    def on_progress(self, event: ProgressEvent) -> dict[str, Any]:
        estimated = event.stage not in {"complete", "blocked", "failed"}
        self.percent = 99 if estimated and int(event.percent) >= 100 else max(0, min(100, int(event.percent)))
        self.status_title = STAGE_TITLES.get(event.stage, "正在处理")
        detail: list[str] = []
        if event.archives_discovered:
            detail.append(f"数据包 {event.archive_index or 0} / {event.archives_discovered}")
        if event.local_groups_total:
            detail.append(f"当前包 {event.local_groups_completed} / {event.local_groups_total} 组")
        if event.groups_discovered or event.groups_done:
            detail.append(f"全任务已发现 {event.groups_discovered or event.groups_total} 组，已完成 {event.groups_done} 组")
        if event.in_flight or event.api_requests:
            detail.append(f"请求进行中 {event.in_flight} · 已完成 {event.requests_completed or event.api_requests}")
        if event.responded_requests and event.avg_response_ms:
            detail.append(
                f"服务端响应 最近 {event.last_attempt_ms} ms · 平均 {event.avg_response_ms} ms"
                f"（{event.responded_requests} 次成功响应，不含本地重试等待）"
            )
        if event.token_usage:
            usage = event.token_usage
            self.last_usage = dict(usage)
            self.last_usage_requests = int(event.usage_reported_requests or 0)
            reasoning = usage.get("reasoning_tokens")
            # DeepSeek bills thinking tokens inside completion; showing only
            # "输出" makes a small visible completion look like the whole cost.
            reasoning_text = f" · 其中思考 {reasoning}" if isinstance(reasoning, int) else ""
            detail.append(
                f"Token 输入 {usage.get('prompt_tokens', '未知')} · 输出 {usage.get('completion_tokens', '未知')}"
                f"{reasoning_text}"
                f"（{event.usage_reported_requests} 次请求有用量回报）"
            )
        if event.failure_counts:
            detail.append(f"失败统计 {summarize_failure_counts(event.failure_counts)}")
        if event.last_error:
            detail.append(f"最近错误 {event.last_error}")
        if event.waiting_reason:
            detail.append(event.waiting_reason)
        if event.current_file:
            detail.append(f"最近完成 {event.current_file}")
        # 2026-09-19 反馈：运行状态行与处理日志逐条重复，状态区只保留阶段标题、
        # 耗时/Token 指标行与进度条；停滞的服务（失败统计/最近错误）在日志行和
        # 运行期问题列表（徽标 + 问题审阅）中仍然可见。
        self.status_desc = ""
        self.stage_text = self.status_title
        # New failures and a changed last_error become per-item problem
        # records (with linked log entries) while the task is still running,
        # instead of appearing only after the report is loaded.
        self._absorb_runtime_problems(event)
        self.stats = stats_from_progress(event, self.records)
        self.stats.update(self._metrics_texts())
        self._log(f"[{self.percent}%] {event.message}{' · ' + ' · '.join(detail) if detail else ''}")
        return self.snapshot(ok=True, compact=True)

    def on_completed(self, report: TranslationReport) -> dict[str, Any]:
        self.last_report = report
        self.busy = False
        self.stop_requested = False
        self.resume_pending = False
        self.task_elapsed_ms = self._freeze_elapsed()
        if getattr(report, "token_usage", None):
            self.last_usage = dict(report.token_usage)
            self.last_usage_requests = int(getattr(report, "usage_reported_requests", 0) or 0)
        blocked = bool(report.audit_blocked or report.publication_status == "withheld")
        self.state = "blocked" if blocked else "complete"
        self.percent = 100 if not blocked else 99
        self.stats = stats_from_report(report, self.records)
        self.stats.update(self._metrics_texts())
        if blocked:
            withheld = [item.archive_id for item in report.archive_results if item.publication_status == "withheld"]
            issue_count_value = sum(len(item.audit_issues) for item in report.archive_results)
            self.status_title = "待处理"
            self.status_desc = f"译文已暂存；发布被阻止。未发布包 {len(withheld)}，问题 {issue_count_value}。"
            self._remember_task("blocked")
            self._log(self.status_desc, level="error")
            notify = self._terminal_notify("blocked", f"发布被阻止，问题 {issue_count_value} 个")
        else:
            # 安全发布与任务状态仍是 complete：这些单元没有写回不代表整包失败，
            # 但界面不能说无条件的「汉化完成」——RoH 交付包就是这样带着 817 条
            # 仍是英文的可译文本被判为完成（2026-09-23 反馈）。
            unfinished = unfinished_units(report)
            hint = unfinished_review_hint(unfinished)
            self.status_title = completion_title(report.scanned, unfinished)
            self.status_desc = f"输出：{report.output_path}" + (f"。{hint}。" if hint else "")
            self._remember_task("completed")
            self._log(f"完成：{report.output_path}" + (f" · {hint}" if hint else ""))
            self._log_passage_sentence(report)
            notify_detail = " · ".join(part for part in (f"输出 {report.output_path.name}", unfinished_note(unfinished)) if part)
            notify = self._terminal_notify("complete", notify_detail, title=self.status_title if unfinished else "")
        self._accumulate_session()
        return self.snapshot(ok=True, notify=notify)

    def _log_passage_sentence(self, report: TranslationReport) -> None:
        stats = getattr(report, "passage_sentence", None) or {}
        if not stats.get("enabled"):
            return
        if not stats.get("available", True):
            self._log("整句翻译（D）未执行：当前模型不支持整句翻译，需要 Qwen-MT 专用翻译模型。", level="warn")
            return
        self._log(
            f"整句翻译（D）：适用 {stats.get('passages_considered', 0)} 段，新采纳 {stats.get('adopted', 0)} 段，"
            f"沿用 {stats.get('adopted_from_memory', 0)} 段；新请求 {stats.get('requests', 0)} 次，缓存命中 {stats.get('cache_hits', 0)} 次；"
            f"名称配置 {stats.get('names', 0)} 个名称。未采纳的段保留逐组件译文。"
        )

    def on_failed(self, message: str, log_path: str = "") -> dict[str, Any]:
        self.busy = False
        self.stop_requested = False
        self.task_elapsed_ms = self._freeze_elapsed()
        blocked = "审计阻断" in message or "发布被阻止" in message
        self.state = "blocked" if blocked else "failed"
        self.status_title = "待处理" if blocked else "处理失败"
        self.status_desc = "译文已暂存；发布被阻止。" if blocked else message
        self._remember_task(self.state)
        self._log(f"错误：{message}", level="error")
        if log_path:
            self._log(f"诊断日志：{log_path}")
        kind = "blocked" if blocked else "failed"
        notify = self._terminal_notify(kind, self.status_desc if blocked else message[:120])
        self._accumulate_session()
        extra = {"error": message, "recovery": self.recovery_payload(), "notify": notify}
        return self.snapshot(ok=False, **extra)

    def on_paused(self, message: str, log_path: str = "") -> dict[str, Any]:
        self.busy = False
        self.resume_pending = True
        self.task_elapsed_ms = self._freeze_elapsed()
        self.state = "paused"
        self.status_title = "已停止" if self.stop_requested else "请求已暂停"
        self.status_desc = message
        self.stop_requested = False
        self._remember_task("paused")
        self._log(message)
        if log_path:
            self._log(f"诊断日志：{log_path}")
        return self.snapshot(ok=True, notify=self._terminal_notify("paused", message[:120]))

    def load_report_records(self, payload: dict[str, Any]) -> dict[str, Any]:
        units = payload.get("units") or []
        report = payload.get("report") or {}
        records = []
        seen: set[str] = set()
        for item in units:
            record = dict(item)
            record["format_name"] = item.get("format", "")
            # 报告单元来自已经结束的运行，只有它们能说明「这条可译文本最终有没有
            # 落盘」；扫描预览的记录没有这个语义（final_target 为空只是因为还没
            # 跑），来源必须显式标出，否则记录列表会把预览当成漏翻。
            record["record_kind"] = REPORT_RECORD_KIND
            presented = present_record(record, translate_npc=self.translate_npc)
            seen.add(str(presented.get("id") or ""))
            records.append(presented)
        filtered_details = report.get("filtered_details") or []
        for detail in filtered_details:
            records.append(
                present_record(
                    {
                        "file_path": detail.get("file", ""),
                        "locator": detail.get("locator", ""),
                        "format": detail.get("format", ""),
                        "source_text": detail.get("source", ""),
                        "policy": detail.get("policy", "") or "UNKNOWN_REVIEW",
                        "reason": detail.get("reason", ""),
                        "record_kind": "filtered",
                        "metadata": detail,
                    },
                    translate_npc=self.translate_npc,
                )
            )
        detail_total = int(report.get("filtered_detail_total") or 0)
        if detail_total > len(filtered_details):
            records.append(
                present_record(
                    {
                        "source_text": f"问题审阅显示 {len(filtered_details)} / 共 {detail_total} 条样本。",
                        "policy": "INFO",
                        "reason": "filtered_detail_limit",
                        "record_kind": "info",
                    },
                    translate_npc=self.translate_npc,
                )
            )
        for warning in report.get("warnings", []):
            records.append(present_record({"source_text": str(warning), "policy": "UNKNOWN_REVIEW", "reason": "warning", "is_problem": True}, translate_npc=self.translate_npc))
        for raw_archive in report.get("archive_results") or []:
            archive = ArchiveResult.from_dict(raw_archive if isinstance(raw_archive, dict) else {})
            for issue in archive.audit_issues:
                if not isinstance(issue, dict):
                    continue
                records.append(
                    present_record(
                        {
                            "source_text": str(issue.get("message") or archive.archive_id),
                            "file_path": str(issue.get("file") or archive.archive_id),
                            "locator": str((issue.get("details") or {}).get("locator") or issue.get("locator") or ""),
                            "policy": "UNKNOWN_REVIEW",
                            "reason": str(issue.get("category") or "audit"),
                            "is_problem": True,
                            "archive_id": archive.archive_id,
                            "archive_status": archive.status,
                            "audit_category": issue.get("category") or "",
                        },
                        translate_npc=self.translate_npc,
                    )
                )
        self.records = records
        self.stats["issues"] = issue_count(records)
        if report.get("audit_blocked") or report.get("publication_status") == "withheld":
            self.state = "blocked"
            self.status_title = "待处理"
            withheld = [item.get("archive_id") for item in report.get("archive_results") or [] if isinstance(item, dict) and item.get("publication_status") == "withheld"]
            self.status_desc = f"译文已暂存；发布被阻止。未发布包 {len(withheld)}。"
        elif self.state == "complete":
            # 报告与完成事件的到达顺序不固定（重新审计、历史报告重载）。完成态已经
            # 成立时，重载的报告不能把「仍有 N 条未落盘」悄悄换回无条件的完成。
            unfinished = unfinished_units(report)
            hint = unfinished_review_hint(unfinished)
            self.status_title = completion_title(int(report.get("scanned") or 0), unfinished)
            self.status_desc = f"输出：{report.get('output_path')}" + (f"。{hint}。" if hint else "")
        # A02: a loaded report must restore the recovery payload, otherwise the
        # UI offers "重新审计并打包" while recovery_payload() still reports
        # nothing usable (and after a restart the workspace is simply lost).
        workspace_path = str(report.get("workspace_path") or "")
        report_file = str(report.get("report_path") or "")
        recoverable = bool(report.get("recoverable")) and bool(workspace_path) and Path(workspace_path).is_dir()
        if recoverable:
            archive_models = []
            for raw_archive in report.get("archive_results") or []:
                if not isinstance(raw_archive, dict):
                    continue
                try:
                    archive_models.append(ArchiveResult.from_dict(raw_archive))
                except Exception:
                    continue
            self.last_report = TranslationReport(
                input_path=Path(str(report.get("input_path") or "")),
                output_path=Path(str(report.get("output_path") or "")),
                scanned=int(report.get("scanned") or 0),
                translated=int(report.get("translated") or 0),
                reused=int(report.get("reused") or 0),
                skipped=int(report.get("skipped") or 0),
                audit_blocked=bool(report.get("audit_blocked")),
                publication_status=str(report.get("publication_status") or "withheld"),
                archive_results=archive_models,
                workspace_path=workspace_path,
                report_path=report_file,
                recoverable=True,
                # 恢复/重载的报告仍要回答「还有多少可译文本没落盘」，否则重启后
                # 的质量摘要会把这件事说成零。
                units_writeback_failed=int(report.get("units_writeback_failed") or 0),
                unit_outcomes=dict(report.get("unit_outcomes") or {}),
            )
        return self.snapshot(ok=True)

    def save_revision(self, unit_id: str, target: str) -> dict[str, Any]:
        record = next((item for item in self.records if item.get("id") == unit_id), None)
        if record is None:
            return self._reject("找不到要修订的条目。")
        if not record.get("editable"):
            return self._reject("机制锁定或未知项不能修订。")
        source = str(record.get("source") or "")
        reason = validate_contextual_target(source, target)
        if not target.strip() or reason:
            return self._reject(f"译文校验失败：{reason or '不能为空'}")
        self.manual_targets[unit_id] = target
        record["target"] = target
        record["tag"] = "手工修订"
        output = self.last_report.output_path if self.last_report is not None else self.output_path
        if output is not None:
            # A10: drafts must survive even when saved before completion.
            path = output.parent / ".mc-hanhua" / (output.name + ".manual.json")
            try:
                atomic_write_json(path, {"schema_version": 1, "targets": self.manual_targets})
            except Exception as exc:
                return self._reject(f"草稿保存失败：{exc}")
            self._log(f"草稿已保存：{path}；输出包尚未更改。")
        else:
            self._log("已保存修订草稿；输出包尚未更改。")
        return self.snapshot(ok=True, toast="已保存草稿，未修改现有包")

    def summary_payload(self) -> dict[str, Any]:
        if self.last_report is None:
            return {"ready": False, "html": "<p>任务尚未完成。完成后可查看统计与保留原因。</p>"}
        report = self.last_report
        # 耗时/Token/费用也进摘要：运行中的指标行只是单行概览（2026-09-21 用户
        # 反馈它太占高度），完整数值在这里留档，任务结束后仍有据可查。
        metrics = self._metrics_texts()
        run_line = ""
        if any(metrics.get(key) for key in ("elapsed_text", "usage_text", "cost_text")):
            fields = []
            if metrics.get("elapsed_text"):
                fields.append(f"耗时 {metrics['elapsed_text']}")
            if metrics.get("usage_text"):
                # 后端原文是「输入 X，输出 Y」；指标行的标签是 Token，这里保持一致。
                fields.append("Token " + str(metrics["usage_text"]))
            if metrics.get("cost_text"):
                fields.append(str(metrics["cost_text"]))
            run_line = "<p>运行：{}。</p>".format(" · ".join(fields))
        hint = unfinished_review_hint(unfinished_units(report))
        unfinished_line = f"<p>{hint}。</p>" if hint else ""
        html = (
            f"<p>扫描 {report.scanned} · 已处理文本组 {report.groups_completed} · AI 通过 {report.units_translated}</p>"
            f"{run_line}"
            f"{unfinished_line}"
            f"<p>待处理问题 {self.stats.get('issues', 0)}。保存草稿不会改现有包；生成修订副本会重新审计并另存。</p>"
            f"<p>输出：{report.output_path}</p>"
        )
        detail = str(metrics.get("usage_detail") or "")
        return {"ready": True, "html": html, "output": str(report.output_path), "usage_detail": detail}

    def history_payload(self) -> list[dict[str, Any]]:
        items = []
        for item in self.config.get("recent_tasks") or []:
            if not isinstance(item, dict):
                continue
            output = Path(str(item.get("output") or ""))
            missing = bool(item.get("output") and not output.exists())
            items.append({**item, "missing": missing, "reason": "输出文件缺失" if missing else ""})
        return items

    def translation_options(self) -> dict[str, Any]:
        skip = npc_skip_keys(self.records, self.translate_npc)
        options: dict[str, Any] = {"skip_unit_keys": skip, "translate_npc": self.translate_npc}
        if self.passage_sentence:
            options["passage_sentence"] = True
            if self.passage_names:
                options["passage_names"] = Path(self.passage_names)
        if self.config.get("official_terms_dir"):
            options.update(
                official_terms_dir=self.config["official_terms_dir"],
                official_terms_version=self.config.get("official_terms_version", ""),
            )
        if self.manual_targets:
            options["manual_targets"] = dict(self.manual_targets)
        return options

    def snapshot(self, *, ok: bool = True, compact: bool = False, **extra: Any) -> dict[str, Any]:
        buttons = button_for_state(
            self.state,
            has_input=self.input_path is not None,
            complete=self.state == "complete",
            has_queue=bool(self.queue),
        )
        buttons["queue_start"] = queue_start_state(
            self.state,
            busy=self.busy,
            has_input=self.input_path is not None,
            has_queue=bool(self.queue),
        )
        payload: dict[str, Any] = {
            "ok": ok,
            "task_id": self.task_id,
            "state": self.state,
            "status_title": self.status_title,
            "status_desc": self.status_desc,
            "percent": self.percent,
            "stage_text": self.stage_text,
            "stats": self.stats,
            "buttons": buttons,
            "npc": self.translate_npc,
            "input_name": self.input_path.name if self.input_path else "尚未选择输入资源",
            "input_detail": str(self.input_path) if self.input_path else "支持 zip / jar / mrpack，以及地图或整合包目录",
            "output_path": str(self.output_path) if self.output_path else "",
            "busy": self.busy,
            "recovery": self.recovery_payload(),
            "connection": {
                "base_url": self.base_url(),
                "model": self.model(),
                "provider": self.provider(),
                "workers": self.workers(),
                "max_ai": self.config.get("max_ai", 0),
                "remember_api_key": bool(self.config.get("remember_api_key")),
                "has_api_key": bool(self.api_key),
                "key_state": self.key_state(),
                "credential_version": self._credential_version,
                "cached_models": self.cached_models(self.credential_endpoint(), self._credential_version) or [],
                "price": model_price_for(self.config.get("model_prices"), self.model()),
            },
            "history": self.history_payload(),
            "summary": self.summary_payload(),
            "queue": self.queue,
            "auto_continue": self.auto_continue,
            "prefs": {
                "notify_system": self.notify_system_enabled(),
                "notify_sound": self.notify_sound_enabled(),
            },
            "passage_sentence": {
                "enabled": self.passage_sentence,
                "names": self.passage_names,
                # D needs a whole-text MT endpoint; with another provider it reports unavailable.
                "ready": self.provider() == "qwen-mt",
            },
            "runtime_problems": self.runtime_problems[-RUNTIME_PROBLEM_SNAPSHOT_LIMIT:],
            "session": dict(self.session_totals),
        }
        payload.update(extra)
        payload.pop("api_key", None)
        if not compact:
            payload["records"] = self.records
        # Progress snapshots omit expensive text records, but must deliver logs
        # while the worker is running, not only on completion or pause. This is
        # the *full* recent tail; the desktop layer replaces it with a delta
        # against what the page already holds (see HtmlMainWindow._with_log_delta),
        # because re-sending 200 lines per tick cost ~243 KB per snapshot.
        payload["logs"] = self.logs[-LOG_TAIL_MAX:]
        payload["log_revision"] = self.log_revision
        return payload

    def _remember_task(self, state: str) -> None:
        if self.input_path is None or self.output_path is None:
            return
        entry = {
            "name": self.output_path.name,
            "input": str(self.input_path),
            "output": str(self.output_path),
            "state": state,
            "task_id": self.task_id,
        }
        recent = [item for item in self.config.get("recent_tasks") or [] if isinstance(item, dict) and item.get("output") != entry["output"]]
        self.config["recent_tasks"] = [entry, *recent][:20]

    def _log(self, text: str, level: str = "info", ref: str = "") -> None:
        text = self._public_error(text)
        entry = {"id": uuid4().hex[:8], "text": text, "level": level, "time": datetime.now().strftime("%H:%M:%S")}
        if ref:
            # ``ref`` links a log line to its runtime problem record so the
            # frontend can jump between the two views.
            entry["ref"] = ref
        self.logs.append(entry)
        self.logs = self.logs[-2000:]
        # Monotonic revision: the desktop layer compares it against the cursor it
        # last delivered to decide which lines are still unseen (see
        # HtmlMainWindow._with_log_delta).
        self.log_revision += 1

    def _public_error(self, message: str) -> str:
        lowered = message.lower()
        if any(token in lowered for token in ("api_key", "authorization", "sk-")):
            return "[redacted]"
        return message

    def _reject(self, message: str) -> dict[str, Any]:
        safe = self._public_error(message)
        return self.snapshot(ok=False, error=safe, toast=safe)

    def recovery_payload(self) -> dict[str, Any]:
        report = self.last_report
        if report is None:
            return {"available": False, "workspace_path": "", "report_path": "", "recoverable": False}
        withheld = [item.archive_id for item in report.archive_results if item.publication_status == "withheld"]
        return {
            "available": bool(report.recoverable or report.audit_blocked),
            "workspace_path": report.workspace_path,
            "report_path": report.report_path,
            "recoverable": bool(report.recoverable),
            "withheld_archives": withheld,
            "issue_count": sum(len(item.audit_issues) for item in report.archive_results),
        }

    def can_restore(self, item: dict[str, Any]) -> tuple[bool, str]:
        input_path = Path(str(item.get("input") or ""))
        output_path = Path(str(item.get("output") or ""))
        if not input_path.exists():
            return False, "输入文件缺失"
        manifest_path = output_path.parent / ".mc-hanhua" / (output_path.name + ".task.json")
        if item.get("state") == "paused":
            try:
                manifest = load_manifest(manifest_path)
            except (OSError, ValueError):
                return False, "任务记录不可读"
            try:
                if manifest.input_fingerprint != input_fingerprint(input_path):
                    return False, "输入已变化，不能继续旧任务"
            except OSError:
                return False, "无法读取输入指纹"
        return True, ""
