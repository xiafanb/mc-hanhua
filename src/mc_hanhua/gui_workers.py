from __future__ import annotations

import json
import shutil
import urllib.error
import urllib.request
from pathlib import Path
from threading import Event
from typing import Any

from PySide6.QtCore import QThread, Signal

from .archives import ARCHIVE_SUFFIXES
from .diagnostics import write_failure_log
from .gui_support import _models_endpoint


def run_translation(*args, **kwargs):
    from .pipeline import run_translation as _run_translation

    return _run_translation(*args, **kwargs)


def scan(*args, **kwargs):
    from .pipeline import scan as _scan

    return _scan(*args, **kwargs)


def copy_input(*args, **kwargs):
    from .pipeline import copy_input as _copy_input

    return _copy_input(*args, **kwargs)


def translate_archive_to_file(*args, **kwargs):
    from .pipeline import translate_archive_to_file as _translate_archive_to_file

    return _translate_archive_to_file(*args, **kwargs)


class TranslationWorker(QThread):
    progress = Signal(object)
    completed = Signal(object)
    records_loaded = Signal(object)
    paused = Signal(str, str)
    failed = Signal(str, str)

    def __init__(
        self,
        input_path: Path,
        output_path: Path,
        translator: Any,
        max_ai: int | None,
        *,
        options: dict | None = None,
        resume: bool = False,
    ) -> None:
        super().__init__()
        self.input_path = input_path
        self.output_path = output_path
        self.translator = translator
        self.max_ai = max_ai
        self.options = options or {}
        self.resume = resume
        self.manifest = None
        self.manifest_path = None
        self._cancel_event = Event()

    def request_stop(self) -> None:
        # The pipeline-level event stops copy/scan/write/repack stages even
        # when no AI translator is configured.
        self._cancel_event.set()
        if self.translator is not None:
            self.translator.request_cancel()

    def run(self) -> None:
        try:
            if self.input_path.exists():
                from datetime import UTC, datetime

                from .task_records import TaskManifest, input_fingerprint, load_manifest, save_manifest
                if self.input_path.is_dir():
                    # A04: refuse before any workspace directory is created.
                    from .pipeline import reject_output_inside_input

                    reject_output_inside_input(self.input_path, self.output_path)
                fingerprint = input_fingerprint(self.input_path)
                expected = self.options.pop("expected_fingerprint", None)
                if expected is not None and expected != fingerprint:
                    raise ValueError("输入已变化，修订不能应用于不同资源。")
                self.manifest_path = self.output_path.parent / ".mc-hanhua" / (self.output_path.name + ".task.json")
                if self.manifest_path.exists() and not self.resume:
                    previous = load_manifest(self.manifest_path)
                    self.resume = previous.state == "paused"
                if self.resume:
                    old = load_manifest(self.manifest_path)
                    if old.input_fingerprint != fingerprint or old.input_path != str(self.input_path.resolve()):
                        raise ValueError("输入已变化，不能继续旧任务；请重新选择输入开始新任务。")
                now = datetime.now(UTC).isoformat()
                self.manifest = TaskManifest(self.output_path.name, str(self.input_path.resolve()), fingerprint,
                                             str(self.output_path.resolve()), "running", now, now)
                save_manifest(self.manifest_path, self.manifest)
            callback = self.progress.emit
            if self.input_path.is_file() and self.output_path.suffix.lower() in ARCHIVE_SUFFIXES:
                report = translate_archive_to_file(
                    self.input_path,
                    self.output_path,
                    translator=self.translator,
                    max_ai=self.max_ai,
                    progress_callback=callback,
                    cancel_event=self._cancel_event,
                    **self.options,
                )
            else:
                report = self._run_directory(callback)

            self._save_state("completed")
            self._emit_saved_records(getattr(report, "report_path", ""))
            self.completed.emit(report)
        except Exception as raw_exc:
            from .translator import TranslationPaused

            if isinstance(raw_exc, TranslationPaused):
                self._save_state("paused")
                self._emit_saved_records(str(getattr(raw_exc, "report_path", "") or ""))
                log_path = str(getattr(raw_exc, "diagnostic_log_path", ""))
                self.paused.emit(str(raw_exc), log_path)
                return
            self._save_state("failed")
            report_note = self._emit_saved_records(str(getattr(raw_exc, "report_path", "") or ""))
            log_path = str(getattr(raw_exc, "diagnostic_log_path", ""))
            if not log_path:
                logged = write_failure_log(
                    raw_exc,
                    operation="gui-translation",
                    input_path=self.input_path,
                    output_path=self.output_path,
                )
                log_path = str(logged or "")
            message = str(raw_exc)
            if report_note:
                message = f"{message}\n{report_note}"
            self.failed.emit(message, log_path)

    def _run_directory(self, callback):
        import tempfile

        runtime = self.output_path.parent / ".mc-hanhua"
        runtime.mkdir(parents=True, exist_ok=True)
        work_dir = runtime / "work"
        work_dir.mkdir(parents=True, exist_ok=True)
        job_dir = Path(tempfile.mkdtemp(prefix="directory-job-", dir=work_dir))
        stage = job_dir / "output"
        options = dict(self.options)
        options.setdefault("memory_path", runtime / "cache" / "translation-memory-context-v3.sqlite")
        options.setdefault("task_root", job_dir)
        options.setdefault("task_id", job_dir.name)
        try:
            report = run_translation(self.input_path, stage, translator=self.translator, max_ai=self.max_ai,
                                     display_output_path=self.output_path, progress_callback=callback,
                                     cancel_event=self._cancel_event, **options)
            if stage.exists():
                stage.rename(self.output_path)
            report.output_path = self.output_path
            # The job dir is removed below; the report moved with the output (A11).
            report.report_path = str(self.output_path / ".mc-hanhua" / "translation-report.json")
            report.workspace_path = str(self.output_path / ".mc-hanhua")
            shutil.rmtree(job_dir, ignore_errors=True)
            return report
        except Exception as cop:
            setattr(cop, "workspace_path", str(job_dir))
            candidate = job_dir / "translation-report.json"
            nested = stage / ".mc-hanhua" / "translation-report.json"
            if not candidate.is_file() and nested.is_file():
                candidate = nested
            setattr(cop, "report_path", str(candidate))
            setattr(cop, "recoverable", True)
            raise

    def _save_state(self, state: str) -> None:
        if self.manifest is not None and self.manifest_path is not None:
            from .task_records import save_manifest
            self.manifest.state = state
            save_manifest(self.manifest_path, self.manifest)

    def _candidate_report_paths(self, preferred: str = "") -> list[Path]:
        paths: list[Path] = []
        if preferred:
            paths.append(Path(preferred))
        runtime = self.output_path.parent / ".mc-hanhua"
        is_archive = self.output_path.suffix.lower() in ARCHIVE_SUFFIXES
        if is_archive:
            paths.append(runtime / "reports" / f"{self.output_path.stem}-translation-report.json")
            work = runtime / "work"
            if work.is_dir():
                newest = sorted(work.glob("job-*/translation-report.json"), key=lambda item: item.stat().st_mtime, reverse=True)
                paths.extend(newest[:3])
        else:
            paths.append(self.output_path / ".mc-hanhua" / "translation-report.json")
            paths.append(runtime / "work")
        if not is_archive:
            # A directory task may legitimately produce an archive-named
            # output (and vice versa); keep both lookup orders (A11).
            paths.append(runtime / "reports" / f"{self.output_path.stem}-translation-report.json")
        else:
            paths.append(self.output_path / ".mc-hanhua" / "translation-report.json")
        return paths

    def _emit_saved_records(self, preferred: str = "") -> str:
        for path in self._candidate_report_paths(preferred):
            if not path.is_file():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                return f"报告读取失败：{path}"
            report = payload.get("report") if isinstance(payload, dict) else {}
            if not isinstance(report, dict):
                continue
            input_value = str(report.get("input_path") or "")
            if input_value and Path(input_value).resolve() != self.input_path.resolve():
                continue
            self.records_loaded.emit(payload)
            return ""
        if preferred:
            return f"报告读取失败：{preferred}"
        return ""


class ReauditWorker(QThread):
    progress = Signal(object)
    completed = Signal(object)
    records_loaded = Signal(object)
    paused = Signal(str, str)
    failed = Signal(str, str)

    def __init__(self, workspace: Path, output_path: Path) -> None:
        super().__init__()
        self.workspace = workspace
        self.output_path = output_path
        self._cancel_event = Event()

    def request_stop(self) -> None:
        self._cancel_event.set()

    def run(self) -> None:
        from .translator import TranslationPaused

        try:
            from .pipeline import republish_from_workspace

            report = republish_from_workspace(self.workspace, self.output_path, progress_callback=self.progress.emit, cancel_event=self._cancel_event)
            report_path = Path(report.report_path) if report.report_path else self.workspace / "translation-report.json"
            if report_path.is_file():
                self.records_loaded.emit(json.loads(report_path.read_text(encoding="utf-8")))
            self.completed.emit(report)
        except TranslationPaused as exc:
            # A cancel during re-audit is a pause, not a failure (A12).
            self.paused.emit(str(exc), str(getattr(exc, "diagnostic_log_path", "") or ""))
        except Exception as raw_exc:
            from .audit import AuditBlockedError

            if isinstance(raw_exc, AuditBlockedError):
                report_path = Path(raw_exc.report_path) if raw_exc.report_path else self.workspace / "translation-report.json"
                if report_path.is_file():
                    self.records_loaded.emit(json.loads(report_path.read_text(encoding="utf-8")))
            log_path = str(getattr(raw_exc, "diagnostic_log_path", "") or "")
            if not log_path:
                logged = write_failure_log(raw_exc, operation="reaudit", input_path=self.workspace, output_path=self.output_path)
                log_path = str(logged or "")
            self.failed.emit(str(raw_exc), log_path)


class ScanPreviewWorker(QThread):
    progress = Signal(object)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, input_path: Path) -> None:
        super().__init__()
        self.input_path = input_path
        self._cancel_event = Event()

    def request_stop(self) -> None:
        self._cancel_event.set()

    def run(self) -> None:
        import tempfile

        from .events import _preview_details

        token = _preview_details.set(True)
        try:
            with tempfile.TemporaryDirectory(prefix="mc-hanhua-scan-") as tmp:
                working = self.input_path if self.input_path.is_dir() else copy_input(self.input_path, Path(tmp) / "input", cancel_event=self._cancel_event)
                result = scan(working, cancel_event=self._cancel_event)
                self._scan_nested(working, result, "", 0)
            self.completed.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            _preview_details.reset(token)

    def _scan_nested(self, root, result, prefix, depth):
        import tempfile

        from .pipeline import _check_cancel, copy_input, iter_files, scan

        for archive in iter_files(root):
            _check_cancel(self._cancel_event)
            if archive.suffix.lower() not in ARCHIVE_SUFFIXES:
                continue
            if depth >= 3:
                result.warnings.append("嵌套超过三层，预览未展开：" + prefix + archive.name)
                continue
            with tempfile.TemporaryDirectory(prefix="mc-preview-nested-") as tmp:
                nested = copy_input(archive, Path(tmp) / "input", cancel_event=self._cancel_event)
                child = scan(nested, cancel_event=self._cancel_event, allow_new_names=False)
                child_prefix = prefix + archive.relative_to(root).as_posix() + "!/"
                for unit in child.text_units:
                    unit.file_path = child_prefix + unit.file_path
                for detail in child.filtered_details:
                    detail["file"] = child_prefix + str(detail.get("file", ""))
                result.text_units.extend(child.text_units)
                result.filtered_details.extend(child.filtered_details)
                for key, value in child.filtered_counts.items():
                    result.filtered_counts[key] = result.filtered_counts.get(key, 0) + value
                result.warnings.extend(child.warnings)
                if depth:
                    result.warnings.append("深层嵌套内容仅供检查，当前翻译流程不递归写回：" + child_prefix)
                    for unit in child.text_units:
                        unit.policy = "UNKNOWN_REVIEW"
                        unit.policy_reason = "nested_writeback_unsupported"
                self._scan_nested(nested, result, child_prefix, depth + 1)


class ModelListWorker(QThread):
    loaded = Signal(object)
    failed = Signal(str)

    def __init__(self, base_url: str, api_key: str) -> None:
        super().__init__()
        self.base_url = base_url
        self.api_key = api_key

    def run(self) -> None:
        try:
            headers = {"Accept": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            request = urllib.request.Request(_models_endpoint(self.base_url), headers=headers, method="GET")
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
            raw_models = payload.get("data", payload.get("models", [])) if isinstance(payload, dict) else []
            models: list[str] = []
            if isinstance(raw_models, list):
                for item in raw_models:
                    if isinstance(item, str):
                        models.append(item)
                    elif isinstance(item, dict):
                        value = item.get("id") or item.get("name") or item.get("model")
                        if isinstance(value, str):
                            models.append(value)
            models = sorted(set(models), key=str.lower)
            if not models:
                raise ValueError("接口未返回可用模型")
            self.loaded.emit(models)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            self.failed.emit(str(exc))
