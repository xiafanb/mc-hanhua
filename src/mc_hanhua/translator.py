from __future__ import annotations

import http.client
import json
import os
import re
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from threading import Event, Lock

from .events import bind_current_sink, emit_event
from .glossary import Glossary
from .models import TextUnit, TranslationGroup
from .official_terms import OfficialTermIndex
from .quality import is_book_page_unit, is_multiline_layout_unit, validate_contextual_target
from .utils import (
    PLACEHOLDER_RE,
    looks_like_chinese_translation,
    normalize_layout_newlines,
    preserve_placeholders,
    restore_boundary_newlines,
)

BatchProgressCallback = Callable[[int, int, int, str], None]
GroupProgressCallback = Callable[[int, int, int, str, TranslationGroup], None]
GroupResultCallback = Callable[[TranslationGroup, dict[str, str]], None]


class TranslationPaused(RuntimeError):
    """Raised after repeated API rate limits so checkpointed work can be resumed."""


STOPPED_MESSAGE = "已手动停止汉化；已完成文本组已保存到缓存，保持相同的输入与输出重新开始即可继续。"


_META_PROMPT_PREFIX_RE = re.compile(r"^\s*(?:只输出\s*)?第\s*\d+\s*段(?:的)?译文[：:]?\s*")
_META_SEGMENT_MARK_RE = re.compile(r"^\s*\[\d{1,3}\]\s*")


def strip_meta_response(source: str, text: str) -> str | None:
    """Remove leaked batch-prompt scaffolding from a model reply.

    Returns None when the reply is pure meta commentary (e.g. the model
    echoing "只输出第 12 段的译文：") so callers can fall back to the source.
    """

    cleaned = text.strip()
    if not cleaned:
        return None
    if not source.strip().startswith("["):
        cleaned = _META_SEGMENT_MARK_RE.sub("", cleaned)
    cleaned = _META_PROMPT_PREFIX_RE.sub("", cleaned)
    if not cleaned:
        return None
    if "只输出" in cleaned or cleaned.startswith(("译文：", "译文:", "翻译：", "翻译:")):
        return None
    return cleaned


def _has_cjk(text: str) -> bool:
    return any("一" <= char <= "鿿" for char in text)


MARKED_SEGMENT_RE = re.compile(r"(?m)^@@(?P<index>\d+)@@[ \t]*(?P<target>.*?)(?=^@@\d+@@|\Z)", re.DOTALL)


class TranslatorProvider(ABC):
    name: str

    @abstractmethod
    def translate(self, unit: TextUnit, glossary: Glossary) -> str | None:
        raise NotImplementedError

    def translate_many(
        self,
        units: list[TextUnit],
        glossary: Glossary,
        on_progress: BatchProgressCallback | None = None,
    ) -> dict[str, str]:
        results: dict[str, str] = {}
        total = len(units)
        failed = 0
        for index, unit in enumerate(units, start=1):
            target = self.translate(unit, glossary)
            if target:
                results[unit.id] = target
            else:
                failed += 1
            if on_progress:
                on_progress(index, total, failed, "")
        return results

    def translate_groups(
        self,
        groups: list[TranslationGroup],
        glossary: Glossary,
        on_progress: GroupProgressCallback | None = None,
        on_result: GroupResultCallback | None = None,
    ) -> dict[str, str]:
        if not groups:
            return {}

        results: dict[str, str] = {}
        failed_groups = 0
        for index, group in enumerate(groups, start=1):
            targets = self.translate_many(group.units, glossary)
            if len(targets) != len(group.units):
                failed_groups += 1
            results.update(targets)
            if on_result:
                on_result(group, targets)
            if on_progress:
                on_progress(index, len(groups), failed_groups, "", group)
        return results

    def request_cancel(self) -> None:
        """Ask a running provider to stop; providers without remote work ignore it."""


class GlossaryOnlyTranslator(TranslatorProvider):
    name = "glossary-only"

    def translate(self, unit: TextUnit, glossary: Glossary) -> str | None:
        exact = glossary.exact_match(unit.source_text)
        if exact:
            return preserve_placeholders(unit.source_text, exact)
        return None


class OpenAICompatibleTranslator(TranslatorProvider):
    name = "openai-compatible"

    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        max_workers: int | None = None,
        official_terms: OfficialTermIndex | None = None,
    ) -> None:
        self.model = model or os.getenv("MC_HANHUA_MODEL", "mimo-v2.5")
        self.base_url = (base_url or os.getenv("MC_HANHUA_OPENAI_BASE_URL", "https://api.openai.com")).rstrip("/")
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        configured_workers = max_workers if max_workers is not None else int(os.getenv("MC_HANHUA_AI_WORKERS", "1"))
        self.max_workers = min(6, max(1, configured_workers))
        self.max_retries = min(4, max(0, int(os.getenv("MC_HANHUA_API_RETRIES", "2"))))
        self.max_consecutive_rate_limits = 3
        self.official_terms = official_terms
        self.last_error = ""
        self.failure_counts: dict[str, int] = {}
        self.retry_counts: dict[str, int] = {}
        self.failure_samples: dict[str, str] = {}
        # Bounded per-group raw model replies for UI/runtime diagnostics.
        self.result_diagnostics: dict[str, str] = {}
        self.groups_total = 0
        self.groups_completed = 0
        self.failed_groups = 0
        self.reviewed_groups = 0
        self.review_corrected = 0
        self.review_pending_groups = 0
        self.planned_draft_calls = 0
        self.planned_review_calls = 0
        self.checkpointed_groups = 0
        self.seeded_drafts = 0
        self.consistency_unified = 0
        self.consistency_conflicts: list[dict[str, object]] = []
        self.canonical_conflicts: list[dict[str, object]] = []
        self.api_requests = 0
        self.in_flight_requests = 0
        self.token_usage: dict[str, int] = {}
        self.usage_reported_requests = 0
        self.rate_limit_cooldowns = 0
        self.units_translated = 0
        self.units_original_preserved = 0
        self.units_validation_failed = 0
        self._consecutive_rate_limits = 0
        self._rate_limit_until = 0.0
        self._state_lock = Lock()
        self._rate_limit_lock = Lock()
        self._cancel_event = Event()
        self._pause_message: str | None = None

    def request_cancel(self) -> None:
        self._cancel_event.set()

    def _check_cancelled(self) -> None:
        if self._cancel_event.is_set():
            raise TranslationPaused(self._pause_message or STOPPED_MESSAGE)

    def _official_role_for(self, unit: TextUnit | None = None, group: TranslationGroup | None = None) -> str:
        if unit is not None and unit.format in {"json-lang", "legacy-lang"} and (".minecraft." in unit.locator or "assets/minecraft/lang/" in unit.file_path.replace("\\", "/")):
            return unit.locator.split(".", 1)[0]
        if group is not None and group.container_type and group.container_type not in {"generic", "nbt", "json-object"}:
            return group.container_type
        return ""

    def _official_terms_block(self, source_text: str, unit: TextUnit | None = None, group: TranslationGroup | None = None) -> str:
        index = getattr(self, "official_terms", None)
        if index is None or not getattr(index, "available", False):
            return ""
        collected = []
        seen: set[tuple[str, str]] = set()
        units = list(group.units) if group is not None else ([unit] if unit is not None else [])
        if units:
            for item in units:
                role = self._official_role_for(item, group)
                for term in index.terms_for_context(item.source_text, role):
                    marker = (term.source, term.target)
                    if marker in seen:
                        continue
                    seen.add(marker)
                    collected.append(term)
        else:
            collected = index.terms_for_context(source_text, self._official_role_for(unit, group))
        if not collected:
            return ""
        rendered = ";".join(f"{term.source}=>{term.target}" for term in collected[:12])
        return f"官方术语：{rendered}"

    def translate(self, unit: TextUnit, glossary: Glossary) -> str | None:
        if not self.api_key:
            self._record_failure("missing_api_key", "OPENAI_API_KEY is empty")
            return None
        masked, term_map = glossary.mask_terms(unit.source_text)
        term_hits = [
            f"{entry.source}=>{entry.target}"
            for entry in glossary.entries
            if entry.source.lower() in masked.lower()
        ][:12]
        term_hint = f"。术语：{'; '.join(term_hits)}" if term_hits else ""
        official_hint = self._official_terms_block(unit.source_text, unit=unit)
        official_clause = f"。{official_hint}" if official_hint else ""
        prompt = (
            "把以下 Minecraft 文本翻译成简体中文，只输出译文，不要解释"
            f"{term_hint}{official_clause}。保留占位符、§颜色代码和\\n：{masked}"
        )
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        }
        try:
            data = self._request_json(payload, timeout=60)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, http.client.HTTPException, TimeoutError, json.JSONDecodeError) as exc:
            self._record_failure(self._failure_reason(exc), f"{self._unit_diagnostic(unit)}; {exc}")
            return None
        try:
            content = data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            self._record_failure("response_format", f"{self._unit_diagnostic(unit)}; {exc}")
            return None
        target, reason = self._sanitize_candidate(unit, content, masked_source=masked, term_map=term_map)
        if target is None:
            self._record_failure(reason or "low_quality", self._unit_diagnostic(unit))
            return None
        self._clear_last_error()
        return target

    def _reuse_canonical_targets(self, group: TranslationGroup) -> dict[str, str] | None:
        """If every shareable unit already has a validated sibling target, skip the request."""

        from .references import canonical_key_for, is_shareable_canonical, keep_original_target, policy_blocks_canonical_share

        done = getattr(self, "_canonical_done", {})
        reused: dict[str, str] = {}
        for unit in group.units:
            if policy_blocks_canonical_share(unit):
                keep_original_target(unit)
                reused[unit.id] = unit.source_text
                continue
            if unit.final_target is not None:
                reused[unit.id] = unit.final_target
                continue
            if not is_shareable_canonical(unit):
                return None
            key = unit.canonical_key or canonical_key_for(unit)
            cached = done.get(key)
            if not cached:
                return None
            reused[unit.id] = cached
        return reused if len(reused) == len(group.units) else None

    def _remember_canonical_targets(self, group: TranslationGroup, targets: dict[str, str]) -> None:
        from .references import canonical_key_for, is_shareable_canonical, policy_blocks_canonical_share

        done = getattr(self, "_canonical_done", None)
        if done is None:
            return
        with self._state_lock:
            for unit in group.units:
                if policy_blocks_canonical_share(unit):
                    continue
                target = targets.get(unit.id)
                if not target or not is_shareable_canonical(unit):
                    continue
                done[unit.canonical_key or canonical_key_for(unit)] = target

    def _sanitize_candidate(
        self,
        unit: TextUnit,
        candidate: str,
        *,
        layout_reflow: bool = False,
        masked_source: str | None = None,
        term_map: dict[str, str] | None = None,
    ) -> tuple[str | None, str | None]:
        """Run the shared post-translation gate; returns (target, failure_reason).

        masked_source/term_map come from Glossary.mask_terms: validation runs
        against the masked source so immutable term tokens must survive the
        model output, then the tokens are restored to glossary targets.
        """

        source = masked_source if masked_source is not None else unit.source_text
        cleaned = strip_meta_response(source, candidate)
        if cleaned is None:
            return None, "meta_response"
        return self._finish_candidate(
            unit,
            cleaned,
            layout_reflow=layout_reflow,
            masked_source=masked_source,
            term_map=term_map,
        )

    def _finish_candidate(
        self,
        unit: TextUnit,
        candidate: str,
        *,
        layout_reflow: bool = False,
        masked_source: str | None = None,
        term_map: dict[str, str] | None = None,
    ) -> tuple[str | None, str | None]:
        validation_source = masked_source if masked_source is not None else unit.source_text
        target = preserve_placeholders(validation_source, candidate)
        layout_unit = is_multiline_layout_unit(unit)
        if target and (layout_reflow or layout_unit):
            target = normalize_layout_newlines(validation_source, target)
            target = restore_boundary_newlines(validation_source, target)
        if not target:
            return None, "placeholder_mismatch"
        # Book pages wrap differently in Chinese; keep exact blank-line
        # positions only for non-book NBT (signs/TextDisplay already reflow).
        book_page = is_book_page_unit(unit)
        strict_newlines = (
            (not layout_reflow)
            and (not book_page)
            and "\n" in validation_source
            and unit.format == "nbt-text"
        )
        reason = validate_contextual_target(validation_source, target, strict_newlines=strict_newlines)
        if reason:
            return None, reason
        if term_map:
            target = Glossary.restore_terms(target, term_map)
            restored_reason = validate_contextual_target(
                unit.source_text,
                target,
                strict_newlines=strict_newlines and "\n" in unit.source_text,
            )
            if restored_reason:
                return None, restored_reason
        index = getattr(self, "official_terms", None)
        if index is not None:
            official_reason = index.validate_candidate(
                unit.source_text,
                target,
                key=unit.locator if unit.format in {"json-lang", "legacy-lang"} else None,
                role=self._official_role_for(unit) or None,
            )
            if official_reason:
                return None, official_reason
        if not looks_like_chinese_translation(target):
            if _is_keepable_short_label(validation_source) and not _has_cjk(target):
                return validation_source, None
            return None, "no_chinese_output" if not _has_cjk(target) else "low_quality"
        return target, None

    def translate_many(
        self,
        units: list[TextUnit],
        glossary: Glossary,
        on_progress: BatchProgressCallback | None = None,
    ) -> dict[str, str]:
        if not self.api_key:
            self._record_failure("missing_api_key", "OPENAI_API_KEY is empty")
            if on_progress:
                on_progress(0, len(units), len(units), self.last_error)
            return {}
        return self._translate_individually(units, glossary, on_progress)

    def translate_groups(
        self,
        groups: list[TranslationGroup],
        glossary: Glossary,
        on_progress: GroupProgressCallback | None = None,
        on_result: GroupResultCallback | None = None,
    ) -> dict[str, str]:
        if not self.api_key:
            self._record_failure("missing_api_key", "OPENAI_API_KEY is empty")
            return {}

        if not groups:
            return {}

        self._check_cancelled()
        results: dict[str, str] = {}
        completed = 0
        failed_groups = 0
        # One in-flight request per shareable canonical key. Followers wait
        # for the leader's validated target instead of racing a second call.
        self._canonical_done: dict[str, str] = getattr(self, "_canonical_done", {})
        with self._state_lock:
            self.groups_total += len(groups)
            self.planned_draft_calls += sum(1 for group in groups if not group.review_only)
            self.planned_review_calls += sum(1 for group in groups if group.requires_review)
        workers = min(self.max_workers, len(groups))
        group_iterator = iter(groups)
        with ThreadPoolExecutor(max_workers=workers, initializer=bind_current_sink) as executor:
            futures: dict[object, TranslationGroup] = {}

            def submit_next() -> bool:
                if self._cancel_event.is_set():
                    return False
                try:
                    group = next(group_iterator)
                except StopIteration:
                    return False
                futures[executor.submit(self._translate_and_review_group, group, glossary)] = group
                return True

            for _ in range(workers):
                submit_next()
            while futures:
                self._check_cancelled()
                completed_futures, _ = wait(futures, return_when=FIRST_COMPLETED)
                for future in completed_futures:
                    group = futures.pop(future)
                    try:
                        group_targets = future.result()
                    except TranslationPaused:
                        for pending in futures:
                            pending.cancel()
                        raise
                    except Exception as exc:
                        self._record_failure("unexpected_error", str(exc))
                        group_targets = {}
                    completed += 1
                    group_failed = len(group_targets) != len(group.units)
                    if group_failed:
                        failed_groups += 1
                    results.update(group_targets)
                    self._remember_canonical_targets(group, group_targets)
                    if on_result:
                        on_result(group, group_targets)
                    with self._state_lock:
                        self.groups_completed += 1
                        if group_failed:
                            self.failed_groups += 1
                        else:
                            self.checkpointed_groups += 1
                        last_error = self.last_error if group_failed else ""
                    if on_progress:
                        on_progress(completed, len(groups), failed_groups, last_error, group)
                    submit_next()
        return results

    def _translate_and_review_group(self, group: TranslationGroup, glossary: Glossary) -> dict[str, str]:
        reused = self._reuse_canonical_targets(group)
        if reused is not None:
            return reused
        seeded = {unit.id: unit.suggested_target for unit in group.units if unit.suggested_target is not None}
        if group.review_only and len(seeded) == len(group.units):
            if not group.requires_review:
                # Exact historical drafts are trusted for ordinary text. Sending every
                # low-risk cached group back to the model defeats cache reuse.
                return dict(seeded)
            with self._state_lock:
                self.reviewed_groups += 1
            reviewed = self._request_group_translation(group, glossary, review=True, drafts=seeded)
            if reviewed is None:
                # A saved draft remains preferable to dropping a completed prior
                # run when the reviewer is temporarily unavailable.
                with self._state_lock:
                    self.review_pending_groups += 1
                self._mark_review_pending(group.units, "review_unavailable")
                return dict(seeded)
            if reviewed != seeded:
                with self._state_lock:
                    self.review_corrected += 1
            self._mark_review_passed(group.units)
            return reviewed

        draft = self._request_group_translation(group, glossary, review=False)
        if draft is None:
            # A whole-block repair preserves context and avoids an expensive fan-out
            # into one request per segment after a malformed model response.
            draft = self._request_group_translation(group, glossary, review=False, repair=True)
        if draft is None:
            # Batch translation and repair both failed. Fall back to individual
            # translation for each segment; keep original text as last resort.
            draft = self._translate_group_fallback(group, glossary)
            if len(draft) != len(group.units):
                for unit in group.units:
                    if unit.id not in draft:
                        draft[unit.id] = unit.source_text
                        self._record_failure("no_chinese_output", self._unit_diagnostic(unit, group))
                        with self._state_lock:
                            self.units_original_preserved += 1
                        self._emit_unit_result(
                            unit,
                            group,
                            "fallback",
                            submitted=unit.source_text,
                            candidate="",
                            target=None,
                            status="validation_failed",
                            reason="no_chinese_output",
                            original_preserved=True,
                        )
            translated_any = any(draft.get(unit.id) not in (None, unit.source_text) for unit in group.units)
            if group.requires_review and translated_any:
                # High-risk fallback drafts get one light review pass; a failed
                # review keeps the fallback draft instead of dropping it.
                with self._state_lock:
                    self.reviewed_groups += 1
                reviewed = self._request_group_translation(group, glossary, review=True, drafts=draft)
                if reviewed is not None:
                    if reviewed != draft:
                        with self._state_lock:
                            self.review_corrected += 1
                    self._mark_review_passed(group.units)
                    return reviewed
                with self._state_lock:
                    self.review_pending_groups += 1
                self._mark_review_pending(group.units, "review_failed")
            return draft
        if len(draft) != len(group.units):
            return draft

        if not group.requires_review:
            return draft

        with self._state_lock:
            self.reviewed_groups += 1
        reviewed = self._request_group_translation(group, glossary, review=True, drafts=draft)
        if reviewed is None:
            with self._state_lock:
                self.review_pending_groups += 1
            self._mark_review_pending(group.units, "review_failed")
            return draft
        if reviewed != draft:
            with self._state_lock:
                self.review_corrected += 1
        self._mark_review_passed(group.units)
        return reviewed

    def _mark_review_pending(self, units: list[TextUnit], reason: str) -> None:
        for unit in units:
            unit.metadata["translation_status"] = "needs_review"
            unit.metadata["review_status"] = "pending"
            unit.metadata["review_reason"] = reason

    def _mark_review_passed(self, units: list[TextUnit]) -> None:
        for unit in units:
            unit.metadata["translation_status"] = "reviewed"
            unit.metadata["review_status"] = "passed"

    def _request_group_translation(
        self,
        group: TranslationGroup,
        glossary: Glossary,
        *,
        review: bool,
        drafts: dict[str, str] | None = None,
        repair: bool = False,
    ) -> dict[str, str] | None:
        masked_sources = {unit.id: glossary.mask_terms(unit.source_text) for unit in group.units}
        segment_lines = []
        for index, unit in enumerate(group.units, start=1):
            masked, _ = masked_sources[unit.id]
            if group.container_type == "book":
                page = unit.metadata.get("book_page", "未知")
                masked = f"[page={page}] {masked}"
            if review and drafts:
                segment_lines.append(f"@@{index}@@ 原文：{masked}\n草稿：{drafts[unit.id]}")
            else:
                segment_lines.append(f"@@{index}@@ {masked}")
        # Neighbor windows overlap. Deduplicate complete page blocks (not lines,
        # since identical lines can be meaningful on different pages).
        contexts = list(dict.fromkeys(
            page.rstrip("\n")
            for unit in group.units
            for page in re.split(r"(?m)(?=^第\d+页：)", str(unit.metadata.get("book_reading_context", "")))
            if page
        ))
        if contexts:
            segment_lines.insert(0, "以下为只读相邻页上下文（含已有中文，不作为额外输出段落）：\n" + "\n".join(contexts)[:12000])
        term_hits = [f"{entry.source}=>{entry.target}" for entry in glossary.entries if any(entry.source.lower() in unit.source_text.lower() for unit in group.units)][:16]
        terms = "；".join(term_hits) or "无"
        official_block = self._official_terms_block(" ".join(unit.source_text for unit in group.units), group=group)
        official_line = f"{official_block}\n" if official_block else ""
        if group.container_type == "book":
            official_line += "本组为同一本书，片段可能跨页续句。先通读全组，再逐段输出；保留全部条件、动作、对象、编号与跳转，不将续句误作独立句，不漏译。品牌名按上下文保持一致。\n"
        task = "审校并改写草稿" if review else "翻译"
        repair_hint = "上一轮输出不符合标记格式；请重新输出完整译文。\n" if repair else ""
        layout_hint = (
            "这是地图中的可视排版槽位。请先理解全部原文，再按自然中文语序把完整含义重新分配到各槽位；"
            "不必逐槽对应英文词序，允许某个槽位为空，但所有信息必须保留且不能擅自补写。"
            "跨槽或跨告示牌的句子必须在最后一个槽位自然收束；可为中文语序补充必要的功能词、连接成分和标点"
            "（如“中”“期间”“。”），但不能增添任何事实。\n"
            if group.layout_reflow
            else ""
        )
        prompt = (
            f"Minecraft 简体中文本地化：{task}以下{group.container_type}片段。\n"
            + (f"术语：{terms}\n" if term_hits else "")
            + official_line
            + "按片段阅读顺序理解上下文；保留占位符、§颜色码、URL、命令、换行语义，不增删事实。\n"
            + layout_hint
            + "仅输出 @@序号@@译文，不要解释或 Markdown。\n"
            + ("若全部草稿准确且格式完整，仅输出 @@OK@@；否则输出全部片段的修订译文，保留原文中的 {termN} 标记。\n" if review else "")
            + repair_hint
            + "\n".join(segment_lines)
        )
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        }
        request_type = "review" if review else ("repair" if repair else "draft")
        emit_event(
            stage="submitted",
            file=group.file_path,
            format=group.format,
            context=group.context,
            group_id=group.id,
            request_type=request_type,
            submitted=prompt,
            extra={"model": self.model, "unit_count": len(group.units), "prompt_chars": len(prompt)},
        )
        try:
            data = self._request_json(payload, timeout=120)
            content = data["choices"][0]["message"]["content"].strip()
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, http.client.HTTPException, TimeoutError, json.JSONDecodeError, KeyError, IndexError, TypeError, AttributeError) as exc:
            reason = self._failure_reason(exc)
            self._record_failure(reason, f"{self._group_diagnostic(group)}; {exc}")
            if reason == "rate_limited" and self._register_final_rate_limit():
                # Cancel running workers too; keeping them retrying against a
                # rate-limited API contradicts the pause and worsens the limit.
                self._pause_message = "API 连续限流，已暂停任务；已完成文本组已保存到缓存。"
                self._cancel_event.set()
                raise TranslationPaused(self._pause_message) from exc
            return None
        # Expand a review acknowledgement through the same validation gate.
        # Drafts contain restored glossary targets; validate those against the
        # original source instead of demanding masked tokens in saved drafts.
        acknowledged = review and content == "@@OK@@" and drafts is not None and all(unit.id in drafts for unit in group.units)
        if acknowledged:
            parsed = {i: drafts[unit.id] for i, unit in enumerate(group.units, 1)}
            parse_info = {}
            masked_sources = {unit.id: (unit.source_text, {}) for unit in group.units}
        else:
            parsed, parse_info = self._parse_marked_segments(content, len(group.units), allow_empty=group.layout_reflow)
        with self._state_lock:
            self.result_diagnostics[group.id] = content[:4000].replace("\\n", "\\\\n")
        emit_event(
            stage="raw_response",
            file=group.file_path,
            format=group.format,
            context=group.context,
            group_id=group.id,
            request_type=request_type,
            raw_response=content,
            extra={"unit_count": len(group.units)},
        )
        if parsed is None:
            self._record_failure("review_response_format" if review else "response_format", self._group_diagnostic(group))
            self._emit_parse_failure(group, content, request_type, parse_info)
            return None
        targets: dict[str, str] = {}
        for index, unit in enumerate(group.units, start=1):
            masked, term_map = masked_sources[unit.id]
            candidate = parsed[index]
            if candidate:
                cleaned = strip_meta_response(masked, candidate)
                if cleaned is None:
                    self._reject_group_unit(unit, group, "meta_response", review, request_type=request_type, candidate=candidate, submitted=masked)
                    return None
                candidate = cleaned
            if group.layout_reflow and not candidate and not PLACEHOLDER_RE.search(masked):
                # Reflow may legitimately leave a slot empty when the source
                # carried no placeholder that must survive.
                targets[unit.id] = ""
                self._emit_unit_result(unit, group, request_type, submitted=masked, candidate="", target="", status="ok", review=review)
                continue
            target, reason = self._finish_candidate(
                unit,
                candidate,
                layout_reflow=group.layout_reflow,
                masked_source=masked,
                term_map=term_map,
            )
            if target is None:
                self._reject_group_unit(
                    unit,
                    group,
                    reason or "low_quality",
                    review,
                    request_type=request_type,
                    candidate=candidate,
                    submitted=masked,
                )
                return None
            targets[unit.id] = target
            self._emit_unit_result(
                unit,
                group,
                request_type,
                submitted=masked,
                candidate=candidate,
                target=target,
                status="ok",
                review=review,
            )
        self._clear_last_error()
        return targets

    def _reject_group_unit(
        self,
        unit: TextUnit,
        group: TranslationGroup,
        reason: str,
        review: bool,
        *,
        request_type: str = "draft",
        candidate: str = "",
        submitted: str = "",
    ) -> None:
        self._record_failure(reason, self._unit_diagnostic(unit, group))
        if review and reason != "placeholder_mismatch":
            # Historical behavior: placeholder mismatches never counted as
            # review rejections.
            self._record_failure("review_rejected", self._unit_diagnostic(unit, group))
        with self._state_lock:
            self.units_validation_failed += 1
        self._emit_unit_result(
            unit,
            group,
            request_type,
            submitted=submitted,
            candidate=candidate,
            target=None,
            status="validation_failed",
            reason=reason,
            review=review,
            original_preserved=True,
        )

    def _translate_group_fallback(self, group: TranslationGroup, glossary: Glossary) -> dict[str, str]:
        masked_sources = {unit.id: glossary.mask_terms(unit.source_text) for unit in group.units}
        context = "\n".join(f"[{index}] {masked_sources[unit.id][0]}" for index, unit in enumerate(group.units, start=1))
        targets: dict[str, str] = {}
        for index, unit in enumerate(group.units, start=1):
            masked, term_map = masked_sources[unit.id]
            official_block = self._official_terms_block(unit.source_text, unit=unit, group=group)
            official_line = f"{official_block}\n" if official_block else ""
            prompt = (
                "你是 Minecraft Java 整合包的中文本地化编辑。以下是同一文本块，翻译其中指定片段为自然简体中文。"
                "保留占位符、§颜色码、URL、命令和换行；不得添加原文没有的信息。\n"
                f"文件：{group.file_path}\n位置：{group.context}\n"
                + official_line
                + f"完整文本块：\n{context}\n\n"
                f"只输出第 {index} 段的译文：{masked}"
            )
            payload = {"model": self.model, "messages": [{"role": "user", "content": prompt}], "temperature": 0}
            try:
                data = self._request_json(payload, timeout=90)
                content = data["choices"][0]["message"]["content"].strip()
            except (urllib.error.URLError, urllib.error.HTTPError, OSError, http.client.HTTPException, TimeoutError, json.JSONDecodeError, KeyError, IndexError, TypeError, AttributeError) as exc:
                self._record_failure(self._failure_reason(exc), f"{self._unit_diagnostic(unit, group)}; {exc}")
                targets[unit.id] = unit.source_text
                with self._state_lock:
                    self.units_original_preserved += 1
                self._emit_unit_result(
                    unit,
                    group,
                    "fallback",
                    submitted=masked,
                    candidate="",
                    target=None,
                    status="request_failed",
                    reason=self._failure_reason(exc),
                    original_preserved=True,
                )
                continue
            cleaned = strip_meta_response(masked, content)
            if cleaned is None:
                self._record_failure("meta_response", self._unit_diagnostic(unit, group))
                targets[unit.id] = unit.source_text
                with self._state_lock:
                    self.units_original_preserved += 1
                    self.units_validation_failed += 1
                self._emit_unit_result(
                    unit,
                    group,
                    "fallback",
                    submitted=masked,
                    candidate=content,
                    target=None,
                    status="validation_failed",
                    reason="meta_response",
                    original_preserved=True,
                )
                continue
            target, reason = self._finish_candidate(unit, cleaned, masked_source=masked, term_map=term_map)
            if target is None:
                self._record_failure(reason or "low_quality", self._unit_diagnostic(unit, group))
                targets[unit.id] = unit.source_text
                with self._state_lock:
                    self.units_original_preserved += 1
                    self.units_validation_failed += 1
                self._emit_unit_result(
                    unit,
                    group,
                    "fallback",
                    submitted=masked,
                    candidate=cleaned,
                    target=None,
                    status="validation_failed",
                    reason=reason or "low_quality",
                    original_preserved=True,
                )
                continue
            targets[unit.id] = target
            self._emit_unit_result(unit, group, "fallback", submitted=masked, candidate=cleaned, target=target, status="ok")
        return targets

    def _translate_individually(
        self,
        units: list[TextUnit],
        glossary: Glossary,
        on_progress: BatchProgressCallback | None,
    ) -> dict[str, str]:
        if self.max_workers == 1:
            return super().translate_many(units, glossary, on_progress)

        results: dict[str, str] = {}
        completed = 0
        failed = 0
        with ThreadPoolExecutor(
            max_workers=min(self.max_workers, len(units)),
            initializer=bind_current_sink,
        ) as executor:
            futures = {executor.submit(self.translate, unit, glossary): unit for unit in units}
            for future in as_completed(futures):
                unit = futures[future]
                try:
                    target = future.result()
                except TranslationPaused:
                    raise
                except Exception as exc:
                    self._record_failure("unexpected_error", str(exc))
                    target = None
                completed += 1
                if target:
                    results[unit.id] = target
                else:
                    failed += 1
                if on_progress:
                    on_progress(completed, len(units), failed, self.last_error)
        return results

    def _endpoint(self) -> str:
        if self.base_url.endswith("/v1"):
            return f"{self.base_url}/chat/completions"
        return f"{self.base_url}/v1/chat/completions"

    def _request(self, payload: dict[str, object]) -> urllib.request.Request:
        return urllib.request.Request(
            self._endpoint(),
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )

    # Billing/credential failures never resolve by retrying or by translating
    # the remaining groups: every further request would burn the same error.
    _FATAL_HTTP_CODES = {401: "API 密钥无效或未授权（401 Unauthorized）", 402: "API 余额不足或配额已用尽（402 Payment Required）", 403: "API 拒绝访问（403 Forbidden）"}

    def _request_json(self, payload: dict[str, object], timeout: int) -> dict[str, object]:
        """Retry connection-level failures that should not abort a whole modpack."""

        last_error: BaseException | None = None
        for attempt in range(self.max_retries + 1):
            self._check_cancelled()
            try:
                self._wait_for_rate_limit()
                with self._state_lock:
                    self.in_flight_requests += 1
                try:
                    with urllib.request.urlopen(self._request(payload), timeout=timeout) as response:
                        # Count requests the server actually received; connection
                        # failures below never reached it and are visible in
                        # retry_counts instead.
                        with self._state_lock:
                            self.api_requests += 1
                        data = json.loads(response.read().decode("utf-8"))
                    self._record_usage(data)
                    return data
                finally:
                    with self._state_lock:
                        self.in_flight_requests -= 1
            except urllib.error.HTTPError as exc:
                with self._state_lock:
                    self.api_requests += 1
                if exc.code in self._FATAL_HTTP_CODES:
                    message = f"{self._FATAL_HTTP_CODES[exc.code]}，任务已暂停；已完成译文已保存，请检查密钥或账户后继续。"
                    with self._state_lock:
                        self._pause_message = message
                        self.last_error = message
                    self._cancel_event.set()
                    raise TranslationPaused(message) from exc
                retryable = exc.code in {408, 409, 425, 429} or exc.code >= 500
                last_error = exc
                if not retryable:
                    raise
                if exc.code == 429:
                    self._set_rate_limit_cooldown(self._retry_after_seconds(exc, attempt))
            except (urllib.error.URLError, OSError, http.client.HTTPException, TimeoutError) as exc:
                last_error = exc

            if attempt == self.max_retries:
                break
            delay = min(30.0, 1.0 * (2**attempt))
            self._record_retry(self._failure_reason(last_error), str(last_error))
            if self._failure_reason(last_error) != "rate_limited":
                if self._cancel_event.wait(delay):
                    raise TranslationPaused(self._pause_message or STOPPED_MESSAGE)

        if last_error is not None:
            raise last_error
        raise RuntimeError("AI request failed without an error")

    def _record_usage(self, data: object) -> None:
        usage = data.get("usage") if isinstance(data, dict) else None
        if not isinstance(usage, dict):
            return
        values = {}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                values[key] = value
        for parent, key in (("prompt_tokens_details", "cached_tokens"), ("completion_tokens_details", "reasoning_tokens")):
            details = usage.get(parent)
            value = details.get(key) if isinstance(details, dict) else None
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                values[key] = value
        if not values:
            return
        with self._state_lock:
            self.usage_reported_requests += 1
            for key, value in values.items():
                self.token_usage[key] = self.token_usage.get(key, 0) + value
        emit_event(stage="api_usage", extra={"model": self.model, "token_usage": values})

    def _record_failure(self, reason: str, detail: str) -> None:
        with self._state_lock:
            self.failure_counts[reason] = self.failure_counts.get(reason, 0) + 1
            self.failure_samples.setdefault(reason, detail[:240])
            self.last_error = f"{reason}: {detail}"

    @staticmethod
    def _group_diagnostic(group: TranslationGroup) -> str:
        previews = " | ".join(repr(unit.source_text[:80]) for unit in group.units[:3])
        return f"{group.file_path} [{group.container_type}] {group.context}: {previews}"

    @staticmethod
    def _unit_diagnostic(unit: TextUnit, group: TranslationGroup | None = None) -> str:
        location = f"{unit.file_path} [{unit.format}] {unit.context}"
        if group is not None:
            location += f"; 容器 {group.container_type} {group.context}"
        return f"{location}: {unit.source_text[:160]!r}"

    def _record_retry(self, reason: str, detail: str) -> None:
        with self._state_lock:
            self.retry_counts[reason] = self.retry_counts.get(reason, 0) + 1
            self.last_error = f"{reason}: {detail}"

    def _clear_last_error(self) -> None:
        with self._state_lock:
            self.last_error = ""
            self._consecutive_rate_limits = 0

    def _register_final_rate_limit(self) -> bool:
        with self._state_lock:
            self._consecutive_rate_limits += 1
            return self._consecutive_rate_limits >= self.max_consecutive_rate_limits

    def _wait_for_rate_limit(self) -> None:
        with self._rate_limit_lock:
            delay = self._rate_limit_until - time.monotonic()
        if delay > 0:
            if self._cancel_event.wait(delay):
                raise TranslationPaused(self._pause_message or STOPPED_MESSAGE)

    def _set_rate_limit_cooldown(self, delay: float) -> None:
        with self._rate_limit_lock:
            next_allowed = time.monotonic() + delay
            if next_allowed > self._rate_limit_until:
                self._rate_limit_until = next_allowed
                with self._state_lock:
                    self.rate_limit_cooldowns += 1

    @staticmethod
    def _retry_after_seconds(error: urllib.error.HTTPError, attempt: int) -> float:
        raw = error.headers.get("Retry-After") if error.headers else None
        try:
            return min(120.0, max(5.0, float(raw))) if raw is not None else min(60.0, 5.0 * (2**attempt))
        except (TypeError, ValueError):
            return min(60.0, 5.0 * (2**attempt))

    @staticmethod
    def _failure_reason(error: BaseException | None) -> str:
        if isinstance(error, http.client.RemoteDisconnected):
            return "network_disconnected"
        if isinstance(error, TimeoutError):
            return "timeout"
        if isinstance(error, urllib.error.HTTPError):
            if error.code == 429:
                return "rate_limited"
            if error.code >= 500:
                return "server_error"
            return "http_error"
        if isinstance(error, (urllib.error.URLError, OSError, http.client.HTTPException)):
            return "network_error"
        return "unexpected_error"

    @staticmethod
    def _parse_marked_segments(
        content: str, count: int, *, allow_empty: bool = False
    ) -> tuple[dict[int, str] | None, dict[str, object]]:
        """Return strict @@index@@ segments plus a parse diagnostic.

        The diagnostic always reports expected/actual/missing/duplicate/extra
        so a malformed batch response can be explained without dumping the
        whole reply into the GUI log.
        """

        matches = list(MARKED_SEGMENT_RE.finditer(content))
        seen: list[int] = [int(match.group("index")) for match in matches]
        expected = list(range(1, count + 1))
        actual = seen
        missing = [index for index in expected if index not in seen]
        extra = [index for index in seen if index not in expected]
        duplicates = sorted({index for index in seen if seen.count(index) > 1})
        info: dict[str, object] = {
            "expected": expected,
            "actual": actual,
            "missing": missing,
            "duplicate": duplicates,
            "extra": extra,
        }
        if len(matches) != count:
            return None, info
        if content[: matches[0].start()].strip() or content[matches[-1].end() :].strip():
            info["reason"] = "leading_or_trailing_text"
            return None, info

        parsed: dict[int, str] = {}
        for match in matches:
            index = int(match.group("index"))
            target = match.group("target").strip()
            if index in parsed or (not target and not allow_empty):
                if index in parsed:
                    info["duplicate"] = sorted(set(duplicates + [index]))
                else:
                    info["reason"] = "empty_segment"
                return None, info
            parsed[index] = target
        if set(parsed) != set(range(1, count + 1)):
            return None, info
        return parsed, info

    def _emit_unit_result(
        self,
        unit: TextUnit,
        group: TranslationGroup,
        request_type: str,
        *,
        submitted: str = "",
        candidate: str = "",
        target: str | None,
        status: str,
        reason: str = "",
        review: bool = False,
        original_preserved: bool = False,
    ) -> None:
        emit_event(
            stage=request_type,
            file=unit.file_path,
            format=unit.format,
            locator=unit.locator,
            context=unit.context,
            unit_id=unit.id,
            source=unit.source_text,
            policy=unit.policy,
            policy_reason=unit.policy_reason,
            group_id=group.id,
            request_type=request_type,
            submitted=submitted,
            candidate=candidate or "",
            validation_status=status,
            validation_reason=reason,
            review_status="reviewed" if review else "",
            final=target if target is not None else (unit.source_text if original_preserved else ""),
            extra={"original_preserved": original_preserved},
        )

    def _emit_parse_failure(
        self,
        group: TranslationGroup,
        content: str,
        request_type: str,
        parse_info: dict[str, object],
    ) -> None:
        emit_event(
            stage=request_type,
            file=group.file_path,
            format=group.format,
            context=group.context,
            group_id=group.id,
            request_type=request_type,
            raw_response=content,
            validation_status="parse_failed",
            validation_reason="response_format",
            extra={
                "expected": parse_info.get("expected"),
                "actual": parse_info.get("actual"),
                "missing": parse_info.get("missing"),
                "duplicate": parse_info.get("duplicate"),
                "extra_ids": parse_info.get("extra"),
                "original_preserved": True,
                "parse_reason": parse_info.get("reason", "count_mismatch"),
            },
        )
        for unit in group.units:
            emit_event(
                stage=request_type,
                file=unit.file_path,
                format=unit.format,
                locator=unit.locator,
                context=unit.context,
                unit_id=unit.id,
                source=unit.source_text,
                policy=unit.policy,
                group_id=group.id,
                request_type=request_type,
                validation_status="parse_failed",
                validation_reason="response_format",
                final=unit.source_text,
                extra={"original_preserved": True},
            )


_SHORT_UPPER_LABEL_RE = re.compile(r"^[A-Z]{2,6}$")
_SHORT_WORD_LABEL_RE = re.compile(r"^[A-Za-z]{1,8}$")


def _is_keepable_short_label(text: str) -> bool:
    """True for labels that may stay English without counting as a failure."""

    stripped = (text or "").strip()
    if not stripped or "\n" in text or " " in stripped:
        return False
    if _SHORT_UPPER_LABEL_RE.fullmatch(stripped):
        return True
    if _SHORT_WORD_LABEL_RE.fullmatch(stripped) and "." not in stripped:
        return True
    return False


def translator_from_env() -> TranslatorProvider:
    if os.getenv("MC_HANHUA_TRANSLATOR", "").lower() in {"openai", "ai"}:
        return OpenAICompatibleTranslator()
    return GlossaryOnlyTranslator()


def fetch_available_models(base_url: str, api_key: str = "", timeout: int = 20) -> list[str]:
    """List models from an OpenAI-compatible API server.

    Shares the translator's endpoint normalisation and Bearer auth so the
    desktop shell and the Qt dialog expose the same capability. Returns a
    sorted, de-duplicated list of model names. Raises on connection errors,
    HTTP errors, timeouts, bad JSON or an empty model list; callers map the
    exception to a stable user-facing message.

    ``base_url`` may point at the server root, an explicit ``/v1`` prefix or
    an explicit ``/models`` endpoint; ``api_key`` may be empty for local
    servers that need no auth.
    """

    base = (base_url or "").strip().rstrip("/")
    if base.endswith("/models"):
        endpoint = base
    elif base.endswith("/v1"):
        endpoint = f"{base}/models"
    else:
        endpoint = f"{base}/v1/models"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(endpoint, headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    raw = payload.get("data", payload.get("models", [])) if isinstance(payload, dict) else []
    models: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                models.append(item)
            elif isinstance(item, dict):
                value = item.get("id") or item.get("name") or item.get("model")
                if isinstance(value, str):
                    models.append(value)
    models = sorted(set(models), key=lambda model: (model.lower(), model))
    if not models:
        raise ValueError("接口未返回可用模型")
    return models
