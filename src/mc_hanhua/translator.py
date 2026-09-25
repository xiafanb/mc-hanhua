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
from .quality import (
    INTENTIONAL_KEEP,
    keepable_short_label,
    newline_rule,
    passage_runs,
    repair_layout_candidate,
    restore_prose_line_shape,
    validate_contextual_target,
)
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

# Bases may already carry a version segment of their own (``/v1``, ``/v4``,
# ``/api/coding/paas/v4``). Only version-less bases get ``/v1`` appended; a
# hard-coded append turned every non-``/v1`` provider (e.g. Z.ai's coding
# plan) into ``.../v4/v1/chat/completions`` and a 404.
_VERSION_SEGMENT_RE = re.compile(r"/v\d+$")

# Model listings are not standardised: OpenAI uses ``id``, some gateways use
# ``name``/``model``, and Z.ai's coding plan uses ``slug`` with a
# ``display_name`` label fallback.
_MODEL_NAME_KEYS = ("id", "name", "model", "slug", "display_name")

# Translation provider ids shared by the desktop shell and the env factory.
# "chat" keeps the historical OpenAI-compatible behaviour and stays the default
# for configs written before the provider field existed.
TRANSLATOR_PROVIDER_CHAT = "chat"
TRANSLATOR_PROVIDER_QWEN_MT = "qwen-mt"
TRANSLATOR_PROVIDERS = (TRANSLATOR_PROVIDER_CHAT, TRANSLATOR_PROVIDER_QWEN_MT)

QWEN_MT_MODEL = "qwen-mt-flash"
QWEN_MT_SOURCE_LANG = "English"
QWEN_MT_TARGET_LANG = "Chinese"
# Engineering budget for injected terminology, not a Qwen-MT API limit: terms,
# translation memory and the domain hint all consume input tokens, so only
# terms that actually occur in the current text are sent, capped here.
QWEN_MT_TERM_BUDGET = 16
# A domain hint only steers style. It is never a guarantee that %s, § codes,
# URLs or command fragments survive — the shared validation gate enforces that.
QWEN_MT_DOMAIN_HINT = (
    "The text belongs to a Minecraft Java Edition map or modpack: quests, dialogue, item and "
    "advancement names, book pages and in-game signs. Use the official Simplified Chinese "
    "Minecraft terminology and a natural game-localization style."
)
QWEN_MT_REVIEW_PENDING_REASON = "qwen_mt_no_review"

_LATIN_TERM_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9' _-]*")


def _requests_per_minute_default() -> float:
    """Proactive request pacing, below the measured ~60/minute ceiling.

    ``MC_HANHUA_REQUESTS_PER_MINUTE`` sets it (0 disables pacing). The default is
    deliberately conservative: guessing high only turns pacing into 429
    responses, guessing low costs wall-clock time.
    """

    raw = os.getenv("MC_HANHUA_REQUESTS_PER_MINUTE")
    if raw is None:
        return 50.0
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 50.0


def api_base(base_url: str) -> str:
    """Normalise a user-supplied base URL, keeping an explicit version path.

    ``https://host`` and ``https://host/openai`` gain ``/v1``; ``/v1``,
    ``/v4`` and other ``/v<N>`` endings are preserved as typed.
    """

    base = (base_url or "").strip().rstrip("/")
    if base and not _VERSION_SEGMENT_RE.search(base):
        base = f"{base}/v1"
    return base


def api_endpoint(base_url: str, route: str) -> str:
    """Join a version-aware base with a route such as ``chat/completions``."""

    return f"{api_base(base_url)}/{route.lstrip('/')}"


def models_endpoint(base_url: str) -> str:
    """Return the model-listing endpoint for a base URL or endpoint."""

    base = (base_url or "").strip().rstrip("/")
    if base.endswith("/models"):
        return base
    return api_endpoint(base, "models")


def parse_model_names(payload: object) -> list[str]:
    """Extract sorted, de-duplicated model names from a listing payload."""

    raw = payload.get("data", payload.get("models", [])) if isinstance(payload, dict) else []
    models: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                models.append(item)
            elif isinstance(item, dict):
                for key in _MODEL_NAME_KEYS:
                    value = item.get(key)
                    if isinstance(value, str) and value:
                        models.append(value)
                        break
    return sorted(set(models), key=lambda model: (model.lower(), model))


def normalize_provider(value: object) -> str:
    """Map a stored or typed provider name onto a supported provider id."""

    name = str(value or "").strip().lower().replace("_", "-")
    return TRANSLATOR_PROVIDER_QWEN_MT if name in {"qwen-mt", "qwenmt"} else TRANSLATOR_PROVIDER_CHAT


def translator_class_for(provider: object) -> type[OpenAICompatibleTranslator]:
    """Return the provider class for a configured provider id."""

    return QwenMTTranslator if normalize_provider(provider) == TRANSLATOR_PROVIDER_QWEN_MT else OpenAICompatibleTranslator


def _term_occurs(source: str, text: str) -> bool:
    """Whole-token match for Latin terms, literal containment otherwise."""

    if not source or not text:
        return False
    if _LATIN_TERM_RE.fullmatch(source):
        return re.search(rf"(?<![A-Za-z0-9_]){re.escape(source)}(?![A-Za-z0-9_])", text) is not None
    return source in text


def _injectable_term(source: str, note: str) -> bool:
    """Mirror ``Glossary.mask_terms`` guards so injection cannot resurface a
    term the mask path deliberately refuses: the dimension "End" must not be
    injected over ordinary narrative "end"."""

    if not source or len(source) < 2:
        return False
    return not (source == "End" and "dimension" in (note or "").lower())


def _locate_unique(text: str, needle: str) -> tuple[int, int] | None:
    """Span of the one occurrence of ``needle`` in ``text``, or ``None``.

    The passage rescue cuts a joined translation at the spans of components
    whose own target is already known. That cut is only a proof when the target
    appears exactly once — two occurrences would make the assignment a guess —
    and, for a Latin needle, when it is not a fragment of a longer word.
    """

    if not needle:
        return None
    start = text.find(needle)
    if start < 0 or text.find(needle, start + 1) >= 0:
        return None
    end = start + len(needle)
    if needle.isascii():
        if start and text[start - 1].isalnum():
            return None
        if end < len(text) and text[end].isalnum():
            return None
    return start, end


def _is_concatenated_component(unit: TextUnit) -> bool:
    """True when the game prints this element joined to its siblings.

    The text components of one ``tellraw`` / ``title`` payload are printed end
    to end, so a run of them is one sentence and can be cut back into its parts.
    An SNBT list (item lore, book pages, sign lines) prints one element per row
    instead: joining those would invent a sentence that is not on the screen.
    """

    try:
        locator = json.loads(unit.locator)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    command = locator.get("command") if isinstance(locator, dict) else None
    return isinstance(command, dict) and command.get("kind") == "json"


def _passage_container(unit: TextUnit) -> tuple[str, str, str]:
    """The command a display component belongs to: file, line and context.

    ``quality.passage_runs`` proves the sibling elements share one array; this
    adds the container that array is parsed from, because a group holds every
    command of a file and the payload offset repeats between them.
    """

    try:
        locator = json.loads(unit.locator)
    except (TypeError, ValueError, json.JSONDecodeError):
        return (unit.file_path, unit.context, "")
    line = locator.get("line") if isinstance(locator, dict) else None
    return (unit.file_path, unit.context, str(line))


def _configured_term_budget(max_terms: int | None) -> int:
    if max_terms is None:
        try:
            max_terms = int(os.getenv("MC_HANHUA_QWEN_MT_MAX_TERMS", str(QWEN_MT_TERM_BUDGET)))
        except ValueError:
            max_terms = QWEN_MT_TERM_BUDGET
    return max(0, min(64, int(max_terms)))

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


class OpenAICompatibleTransport(TranslatorProvider):
    """Shared machinery for OpenAI-compatible providers.

    Owns transport and bookkeeping only: HTTP requests with retries, rate
    limiting and pause handling; the counters and diagnostics the pipeline
    reads; canonical-target reuse; the one validation gate every candidate
    passes (``_finish_candidate``); and the group orchestration loop.

    How a *candidate is produced* belongs to the subclass. The loop's single
    hook is ``_translate_and_review_group``, which each strategy implements —
    a provider that cannot review returns drafts and reports the group pending
    instead.

    Keeping strategy out of this class is why a provider can declare what it
    does rather than undo what it inherited: chat and a dedicated MT model both
    subclass this, and neither inherits the other's prompt protocol.
    """

    name = "openai-compatible"
    supports_instruction_prompt = True
    supports_review = True
    supports_grouped_translation = True
    _FATAL_HTTP_CODES = {401: "API 密钥无效或未授权（401 Unauthorized）", 402: "API 余额不足或配额已用尽（402 Payment Required）", 403: "API 拒绝访问（403 Forbidden）"}

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
        self.max_consecutive_server_errors = max(1, int(os.getenv("MC_HANHUA_MAX_CONSECUTIVE_SERVER_ERRORS", "3")))
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
        # Bounded per-attempt latency history: retry sleeps are excluded so a
        # slow 5xx service is distinguishable from long backoff.
        self.attempt_timings: list[dict[str, object]] = []
        self.max_attempt_timings = 200
        self.last_attempt_ms = 0
        self.responded_requests = 0
        self.total_response_ms = 0
        self._consecutive_rate_limits = 0
        self._consecutive_server_errors = 0
        self._rate_limit_until = 0.0
        # Proactive pacing. The measured effective ceiling was ~60 requests per
        # minute, so the default spaces requests just below it instead of
        # discovering the limit through 429 responses; every worker shares the
        # same admission time. 0 disables pacing (tests, offline transports).
        self.requests_per_minute = _requests_per_minute_default()
        self.pacing_waits = 0
        self._next_request_at = 0.0
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
        masked_target = preserve_placeholders(validation_source, candidate)
        newline = newline_rule(unit)
        if masked_target and (layout_reflow or newline.repair):
            masked_target = normalize_layout_newlines(validation_source, masked_target)
            if not newline.reflow:
                # Only a layout container needs its boundary breaks put back
                # here: nothing else rebuilds a sign page's leading/trailing
                # newlines.  A reflowing span is rebuilt whole by the shared
                # repair below, which owns every break — restoring the
                # boundaries first duplicates a run the reply already carried,
                # and the reuse gate (which never restores them) would then
                # refuse a reply it can shape.
                masked_target = restore_boundary_newlines(validation_source, masked_target)
        if not masked_target:
            return None, "placeholder_mismatch"
        # Terms go back first so the repair below runs on the text the reuse
        # gate will see: picking the breaks from a masked token's width (or from
        # the masked text at all) would place the span's line shape where no
        # other layer measures it, and the provider and the gate would then
        # write different bytes for the same reply.
        restored = Glossary.restore_terms(masked_target, term_map) if term_map else masked_target
        # One repair, shared with the reuse gate: a reply that reflowed the
        # paragraph is rebuilt onto the span's own line shape and a clickable
        # row gets its pad (and, after unchanged furniture, its gap) back.
        target = repair_layout_candidate(unit, unit.source_text, restored, rule=newline)
        if target is None:
            return None, "newline_structure"
        if not target:
            return None, "placeholder_mismatch"
        # Token layer, masked view: the reply must keep the tokens the model was
        # given.  The line shape is not compared here — it is compared against
        # the span itself below, and both views carry the same breaks.
        reason = validate_contextual_target(validation_source, masked_target)
        if reason:
            return None, reason
        # Book pages wrap differently in Chinese, so they reflow (newline_rule
        # says so); everything else that carries layout-by-position breaks —
        # NBT text blocks and text with padding newlines — must keep them.
        restored_reason = validate_contextual_target(
            unit.source_text,
            target,
            strict_newlines=newline.strict and "\n" in unit.source_text,
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
            if keepable_short_label(validation_source) and not _has_cjk(target):
                # The provider keeps this label on purpose.  The pipeline will not
                # apply or cache an unchanged target (caching it would block the
                # retry on every later run), so the decision has to be visible on
                # the unit: without the marker the report counts a deliberate keep
                # as a text that never produced a candidate.
                unit.metadata[INTENTIONAL_KEEP] = {"reason": "keepable_short_label", "origin": "model"}
                return validation_source, None
            return None, "no_chinese_output" if not _has_cjk(target) else "low_quality"
        return target, None

    def plan_requests(self, groups: list[TranslationGroup]) -> tuple[int, int]:
        """Estimated (draft, review) request counts for these groups.

        A strategy decision, not a property of the orchestrator: how many calls
        a provider will make depends on how it translates, so the provider
        answers it. It is an **estimate** — layout merging, cache hits and
        degradation change the real number — and it exists so the UI can show a
        plan before a run without any strategy having to undo a guess afterwards.

        The chat default is one draft per group plus one review per high-risk
        group; a provider that cannot review reports no review calls.
        """

        draft_calls = sum(1 for group in groups if not group.review_only)
        review_calls = sum(1 for group in groups if group.requires_review) if self.supports_review else 0
        return draft_calls, review_calls

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
        draft_calls, review_calls = self.plan_requests(groups)
        with self._state_lock:
            self.groups_total += len(groups)
            self.planned_draft_calls += draft_calls
            self.planned_review_calls += review_calls
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
                        # Classify from the exception, not from a fixed label: a
                        # 429 that exhausted its retries arrives here as an
                        # HTTPError and used to be recorded as "unexpected_error",
                        # so the UI showed 未分类错误 instead of 接口限流.
                        self._record_failure(self._failure_reason(exc), str(exc))
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

    def _mark_review_pending(self, units: list[TextUnit], reason: str) -> None:
        for unit in units:
            unit.metadata["translation_status"] = "needs_review"
            unit.metadata["review_status"] = "pending"
            unit.metadata["review_reason"] = reason

    def _endpoint(self) -> str:
        return api_endpoint(self.base_url, "chat/completions")

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

    def _request_json(self, payload: dict[str, object], timeout: int) -> dict[str, object]:
        """Retry connection-level failures that should not abort a whole modpack."""

        last_error: BaseException | None = None
        for attempt in range(self.max_retries + 1):
            self._check_cancelled()
            attempt_started = time.monotonic()
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
                    self._clear_server_errors()
                    self._record_usage(data)
                    self._record_attempt(attempt, attempt_started, outcome="ok")
                    return data
                finally:
                    with self._state_lock:
                        self.in_flight_requests -= 1
            except urllib.error.HTTPError as exc:
                with self._state_lock:
                    self.api_requests += 1
                self._record_attempt(attempt, attempt_started, outcome=f"http_{exc.code}")
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
                if exc.code >= 500 and self._register_server_error(exc.code):
                    message = f"API 连续 {self.max_consecutive_server_errors} 次返回服务端错误（最近 HTTP {exc.code}），已暂停任务；已完成译文已保存，请稍后检查服务状态后继续。"
                    with self._state_lock:
                        self._pause_message = message
                        self.last_error = message
                    self._cancel_event.set()
                    raise TranslationPaused(message) from exc
                if exc.code == 429:
                    self._set_rate_limit_cooldown(self._retry_after_seconds(exc, attempt))
            except (urllib.error.URLError, OSError, http.client.HTTPException, TimeoutError) as exc:
                last_error = exc
                self._record_attempt(attempt, attempt_started, outcome="transport")

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

    def _record_attempt(self, attempt: int, started: float, *, outcome: str) -> None:
        """Record how long one HTTP attempt took, retries included.

        A stalled 5xx stream is only diagnosable if per-attempt latency is
        visible: the group median is dominated by retry sleeps, which are not
        server time.
        """

        elapsed_ms = int((time.monotonic() - started) * 1000)
        entry = {"attempt": attempt, "elapsed_ms": elapsed_ms, "outcome": outcome}
        with self._state_lock:
            self.attempt_timings.append(entry)
            del self.attempt_timings[: max(0, len(self.attempt_timings) - self.max_attempt_timings)]
            self.last_attempt_ms = elapsed_ms
            if outcome == "ok":
                self.responded_requests += 1
                self.total_response_ms += elapsed_ms
        emit_event(stage="api_attempt", extra=entry)

    @property
    def avg_response_ms(self) -> int:
        with self._state_lock:
            if not self.responded_requests:
                return 0
            return int(self.total_response_ms / self.responded_requests)

    def _clear_last_error(self) -> None:
        with self._state_lock:
            self.last_error = ""
            self._consecutive_rate_limits = 0
            self._consecutive_server_errors = 0

    def _clear_server_errors(self) -> None:
        with self._state_lock:
            self._consecutive_server_errors = 0

    def _register_server_error(self, status_code: int) -> bool:
        with self._state_lock:
            self._consecutive_server_errors += 1
            self.failure_samples.setdefault("server_error", f"HTTP {status_code}")
            return self._consecutive_server_errors >= self.max_consecutive_server_errors

    def _register_final_rate_limit(self) -> bool:
        with self._state_lock:
            self._consecutive_rate_limits += 1
            return self._consecutive_rate_limits >= self.max_consecutive_rate_limits

    def _wait_for_rate_limit(self) -> None:
        """Admission control: reactive cooldown plus proactive pacing.

        Both are global to the translator, so N workers share one request rate
        instead of multiplying it.
        """

        waited = False
        while True:
            with self._rate_limit_lock:
                now = time.monotonic()
                delay = max(self._rate_limit_until, self._next_request_at) - now
                if delay <= 0:
                    if self.requests_per_minute > 0:
                        self._next_request_at = now + 60.0 / self.requests_per_minute
                    return
            if not waited:
                # One admission, counted once, however many slices the wait takes.
                self.pacing_waits += 1
                waited = True
            if self._cancel_event.wait(min(delay, 5.0)):
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
    def _record_unit_status(unit: TextUnit, status: str, reason: str) -> None:
        """Keep the unit's own outcome in step with the event that was emitted.

        The report's ``unit_outcomes`` is mutually exclusive and reads unit
        metadata, not events: a candidate the write gate refused inside the
        provider used to leave no trace on the unit, so the run reported 644
        ``newline_structure`` rejections while the account showed 70 — and the
        remaining 574 missing translations were indistinguishable from units
        that were never attempted. ``origin="model"`` is the pipeline's name for
        a candidate that came from the provider rather than from reuse.
        """

        if status == "validation_failed":
            unit.metadata["candidate_validation"] = {"origin": "model", "reason": reason or "invalid_candidate"}
        elif status == "request_failed":
            unit.metadata["translation_status"] = "request_failed"
        elif status == "ok":
            # A later pass may have produced the target this unit was refused
            # for; keeping the rejection would misreport a written unit.
            unit.metadata.pop("candidate_validation", None)

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
        self._record_unit_status(unit, status, reason)
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

    def _translate_and_review_group(self, group: TranslationGroup, glossary: Glossary) -> dict[str, str]:
        """Produce validated targets for one group; implemented by each strategy."""

        raise NotImplementedError


class OpenAICompatibleTranslator(OpenAICompatibleTransport):
    """Chat-style provider: instruction prompt, batched segments, review passes."""

    name = "openai-compatible"
    supports_instruction_prompt = True
    supports_review = True
    supports_grouped_translation = True
    _FATAL_HTTP_CODES = {401: "API 密钥无效或未授权（401 Unauthorized）", 402: "API 余额不足或配额已用尽（402 Payment Required）", 403: "API 拒绝访问（403 Forbidden）"}

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
        passage_note: dict[int, str] = {}
        for run in passage_runs(group.units):
            for position, unit_index in enumerate(run, start=1):
                passage_note[unit_index] = f"（同一段连续文本 第 {position}/{len(run)} 行）"
        segment_lines = []
        for index, unit in enumerate(group.units, start=1):
            masked, _ = masked_sources[unit.id]
            note = passage_note.get(index - 1, "")
            if group.container_type == "book":
                page = unit.metadata.get("book_page", "未知")
                masked = f"[page={page}] {masked}"
            if review and drafts:
                segment_lines.append(f"@@{index}@@{note} 原文：{masked}\n草稿：{drafts[unit.id]}")
            else:
                segment_lines.append(f"@@{index}@@{note} {masked}")
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
        passage_hint = (
            "带（同一段连续文本）标记的片段是同一条语句被排版拆开的连续行：先连读理解整句，再按自然中文语序切回同样多的行，"
            "逐行返回；不要逐行孤立翻译，也不要合并或增删行数。\n"
            if passage_note
            else ""
        )
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
            + passage_hint
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
                # A transport failure is retried once more at the end of the run
                # (pipeline tail pass); a validation rejection is not.
                unit.metadata["translation_status"] = "request_failed"
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
                    # Same reason as the group path: classify by exception type,
                    # or a rate limit is mislabelled as an unclassified error.
                    self._record_failure(self._failure_reason(exc), str(exc))
                    target = None
                completed += 1
                if target:
                    results[unit.id] = target
                else:
                    failed += 1
                if on_progress:
                    on_progress(completed, len(units), failed, self.last_error)
        return results

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




class QwenMTTranslator(OpenAICompatibleTransport):
    """Qwen-MT (``qwen-mt-flash`` / ``plus`` / ``lite`` / ``turbo``) provider.

    Qwen-MT is a dedicated translation model rather than a chat model: it takes
    a single user message and reads its instructions from ``translation_options``
    (``source_lang`` / ``target_lang`` / ``terms`` / ``domains``). Three
    consequences shape this class:

    * glossary terms travel as the native ``terms`` array, so the lexical
      ``{termN}`` masking used by the chat provider is not applied here. Syntax
      protection (%s, § codes, URLs, command fragments) is provider-independent
      and stays mandatory through the shared validation gate;
    * review and repair passes need an instruction channel that this model does
      not offer, so groups flagged ``requires_review`` are reported as review
      pending instead of being silently accepted;
    * one request translates one unit; the group entry point only fans units
      out, so a future batching strategy can replace it without changing the
      provider interface.

    This class never changes whether a candidate may be written: every returned
    target still passes ``_finish_candidate`` / ``validate_contextual_target``.
    """

    name = "qwen-mt"
    supports_instruction_prompt = False
    supports_review = False
    supports_grouped_translation = False

    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        max_workers: int | None = None,
        official_terms: OfficialTermIndex | None = None,
        source_lang: str | None = None,
        target_lang: str | None = None,
        max_terms: int | None = None,
        domain_hint: str | None = None,
    ) -> None:
        super().__init__(
            # Resolve the MT default before the base class applies its chat default.
            model=model or os.getenv("MC_HANHUA_MODEL") or QWEN_MT_MODEL,
            base_url=base_url,
            api_key=api_key,
            max_workers=max_workers,
            official_terms=official_terms,
        )
        self.source_lang = (source_lang or os.getenv("MC_HANHUA_QWEN_MT_SOURCE_LANG") or QWEN_MT_SOURCE_LANG).strip() or QWEN_MT_SOURCE_LANG
        self.target_lang = (target_lang or os.getenv("MC_HANHUA_QWEN_MT_TARGET_LANG") or QWEN_MT_TARGET_LANG).strip() or QWEN_MT_TARGET_LANG
        self.max_terms = _configured_term_budget(max_terms)
        self.domain_hint = QWEN_MT_DOMAIN_HINT if domain_hint is None else domain_hint.strip()
        # Terms actually sent / terms whose target survived into the accepted
        # text. The hit rate is one of the observables of a first real run.
        self.terms_injected = 0
        self.terms_hit = 0

    def _relevant_terms(
        self,
        source_text: str,
        glossary: Glossary,
        unit: TextUnit | None = None,
        group: TranslationGroup | None = None,
    ) -> list[tuple[str, str]]:
        """Collect the terms of *this* text only, official terms first.

        Official terms lead the list so the budget can never cut one: the
        write-back gate rejects a candidate that contradicts an official term.
        """

        pairs: list[tuple[str, str]] = []
        seen: set[str] = set()
        index = getattr(self, "official_terms", None)
        if index is not None and getattr(index, "available", False):
            for term in index.terms_for_context(source_text, self._official_role_for(unit, group)):
                key = term.source.lower()
                if key in seen:
                    continue
                seen.add(key)
                pairs.append((term.source, term.target))
        for entry in reversed(glossary.entries):
            key = entry.source.lower()
            if key in seen or not _injectable_term(entry.source, entry.note):
                continue
            if not _term_occurs(entry.source, source_text):
                continue
            seen.add(key)
            pairs.append((entry.source, entry.target))
        return pairs[: self.max_terms]

    def _mt_payload(self, source_text: str, terms: list[tuple[str, str]]) -> dict[str, object]:
        options: dict[str, object] = {"source_lang": self.source_lang, "target_lang": self.target_lang}
        if terms:
            options["terms"] = [{"source": source, "target": target} for source, target in terms]
        if self.domain_hint:
            options["domains"] = self.domain_hint
        return {
            "model": self.model,
            "messages": [{"role": "user", "content": source_text}],
            # Sampling parameters are deliberately omitted: Qwen-MT's own
            # defaults are its recommended operating point. Pin
            # temperature/seed only after an A/B comparison.
            "translation_options": options,
        }

    def translate(self, unit: TextUnit, glossary: Glossary) -> str | None:
        return self._translate_unit(unit, glossary)

    def _translate_unit(
        self,
        unit: TextUnit,
        glossary: Glossary,
        group: TranslationGroup | None = None,
    ) -> str | None:
        """Translate exactly one unit through the shared validation gate."""

        if not self.api_key:
            self._record_failure("missing_api_key", "OPENAI_API_KEY is empty")
            return None
        source_text = unit.source_text
        reply = self._request_mt_text(source_text, glossary, unit=unit, group=group, timeout=60)
        if reply is None:
            unit.metadata["translation_status"] = "request_failed"
            with self._state_lock:
                self.units_original_preserved += 1
            self._emit_result(unit, group, "draft", submitted=source_text, target=None, status="request_failed", reason=self.last_error.split(":", 1)[0])
            return None
        content, terms = reply
        # No masked_source/term_map: lexical glossary masking belongs to the
        # chat provider. Injection happened through the native terms array.
        target, reason = self._finish_candidate(unit, content, layout_reflow=bool(group.layout_reflow) if group is not None else False)
        if target is None:
            reason = reason or "low_quality"
            self._record_failure(reason, self._unit_diagnostic(unit, group))
            with self._state_lock:
                self.units_validation_failed += 1
            self._emit_result(unit, group, "draft", submitted=source_text, candidate=content, target=None, status="validation_failed", reason=reason)
            return None
        with self._state_lock:
            self.terms_hit += sum(1 for _source, term_target in terms if term_target and term_target in target)
        self._clear_last_error()
        self._emit_result(unit, group, "draft", submitted=source_text, candidate=content, target=target, status="ok")
        return target

    def _emit_result(
        self,
        unit: TextUnit,
        group: TranslationGroup | None,
        request_type: str,
        *,
        submitted: str = "",
        candidate: str = "",
        target: str | None,
        status: str,
        reason: str = "",
    ) -> None:
        if group is not None:
            self._emit_unit_result(
                unit,
                group,
                request_type,
                submitted=submitted,
                candidate=candidate,
                target=target,
                status=status,
                reason=reason,
                original_preserved=target is None,
            )
            return
        self._record_unit_status(unit, status, reason)
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
            request_type=request_type,
            submitted=submitted,
            candidate=candidate or "",
            validation_status=status,
            validation_reason=reason,
            final=target if target is not None else unit.source_text,
            extra={"original_preserved": target is None},
        )

    def _mark_mt_review_pending(self, group: TranslationGroup, translated_ids: list[str]) -> None:
        """A high-risk group cannot be reviewed by an MT model; report it."""

        pending = [unit for unit in group.units if unit.id in translated_ids]
        if not group.requires_review or not pending:
            return
        with self._state_lock:
            self.review_pending_groups += 1
        self._mark_review_pending(pending, QWEN_MT_REVIEW_PENDING_REASON)

    def _request_mt_text(
        self,
        source_text: str,
        glossary: Glossary,
        *,
        unit: TextUnit | None = None,
        group: TranslationGroup | None = None,
        timeout: int = 60,
    ) -> tuple[str, list[tuple[str, str]]] | None:
        """One MT call for an arbitrary source string; returns (reply, terms).

        Returns None when the transport failed (already recorded as a failure);
        the caller decides whether that is a per-unit or per-group failure.
        """

        terms = self._relevant_terms(source_text, glossary, unit=unit, group=group)
        payload = self._mt_payload(source_text, terms)
        emit_event(
            stage="submitted",
            file=group.file_path if group is not None else (unit.file_path if unit is not None else ""),
            format=unit.format if unit is not None else (group.format if group is not None else ""),
            context=group.context if group is not None else (unit.context if unit is not None else ""),
            group_id=group.id if group is not None else "",
            unit_id=unit.id if unit is not None else "",
            request_type="draft",
            submitted=source_text,
            extra={"model": self.model, "provider": self.name, "terms_injected": len(terms)},
        )
        with self._state_lock:
            self.terms_injected += len(terms)
        try:
            data = self._request_json(payload, timeout=timeout)
            content = data["choices"][0]["message"]["content"].strip()
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            OSError,
            http.client.HTTPException,
            TimeoutError,
            json.JSONDecodeError,
            KeyError,
            IndexError,
            TypeError,
            AttributeError,
        ) as exc:
            reason = self._failure_reason(exc)
            detail = self._unit_diagnostic(unit, group) if unit is not None else self._group_diagnostic(group) if group is not None else source_text[:80]
            self._record_failure(reason, f"{detail}; {exc}")
            return None
        emit_event(
            stage="raw_response",
            file=group.file_path if group is not None else (unit.file_path if unit is not None else ""),
            format=unit.format if unit is not None else (group.format if group is not None else ""),
            context=group.context if group is not None else (unit.context if unit is not None else ""),
            group_id=group.id if group is not None else "",
            unit_id=unit.id if unit is not None else "",
            request_type="draft",
            raw_response=content,
            extra={"unit_count": 1},
        )
        return content, terms

    def _layout_probe(self, group: TranslationGroup, source_text: str) -> TextUnit:
        """Synthetic unit carrying the joined layout text through the shared gate."""

        return TextUnit(
            id=f"{group.id}:layout",
            source_text=source_text,
            file_path=group.file_path,
            format=group.format,
            locator=group.context,
            context=group.context,
            policy="TRANSLATABLE_DISPLAY",
            metadata={"layout_container": "sign"},
        )

    def _translate_layout_group(
        self,
        group: TranslationGroup,
        glossary: Glossary,
    ) -> dict[str, str] | None:
        """Translate a multi-slot sign / TextDisplay as one phrase, then distribute.

        Qwen-MT reads a *sentence*: a per-slot request cannot know that
        "Palace" and "of Heat" are one phrase, so each slot returns as a literal
        fragment ("宫殿" + "的热度"). Joining the slots restores the phrase the
        author actually wrote; the reply is then handed back to the slots.

        Any shape mismatch falls back to ``None``, which sends the group down the
        ordinary per-slot path — the joined call is a quality improvement, never
        a safety shortcut. Every distributed line still passes ``_finish_candidate``
        against its own source, so the write gate is unchanged.
        """

        slots = [unit for unit in group.units if unit.source_text.strip()]
        if len(slots) < 2:
            return None
        # A placeholder belongs to one specific slot; merging lines would leave
        # no way to tell which slot must keep it.
        if any(PLACEHOLDER_RE.search(unit.source_text) for unit in slots):
            return None

        joined = "\n".join(unit.source_text for unit in slots)
        reply = self._request_mt_text(joined, glossary, group=group, timeout=90)
        if reply is None:
            return None
        content, terms = reply
        # A sign group can span two adjacent blocks, and the joined MT call may
        # return fewer rows than the group has slots. Rebuild that shorter reply
        # onto the source's row shape before the shared candidate gate; if the
        # helper cannot prove a safe split, the ordinary per-slot fallback runs.
        candidate = normalize_layout_newlines(joined, content)
        returned_lines = [line for line in candidate.split("\n") if line.strip()]
        if 1 <= len(returned_lines) < len(slots):
            shaped = restore_prose_line_shape(
                joined,
                candidate,
                min_line_chars=2,
                # Sign rows have a shared physical width. Weighting each row
                # by the length of its English wording can collapse a short
                # source row into a one-character Chinese fragment.
                uniform_line_width=group.container_type == "sign",
            )
            if shaped is None:
                self._record_failure("newline_structure", f"{self._group_diagnostic(group)}; 合并译文无法安全恢复告示牌行数")
                return None
            candidate = shaped
        probe = self._layout_probe(group, joined)
        target, reason = self._finish_candidate(probe, candidate, layout_reflow=True)
        if target is None:
            self._record_failure(reason or "low_quality", f"{self._group_diagnostic(group)}; 合并译文未过校验")
            return None

        lines = [line.strip() for line in target.split("\n")]
        lines = [line for line in lines if line]
        if not lines:
            return None

        targets: dict[str, str] = {}
        if len(lines) == len(slots):
            for unit, line in zip(slots, lines):
                slot_target, slot_reason = self._finish_candidate(unit, line, layout_reflow=True)
                if slot_target is None:
                    # One bad slot invalidates the distribution: a partial
                    # assignment would silently drop text the author placed.
                    self._record_failure(slot_reason or "low_quality", f"{self._unit_diagnostic(unit, group)}; 合并译文分段未过校验")
                    return None
                targets[unit.id] = slot_target
        elif len(lines) == 1:
            # Chinese is shorter; the whole phrase fits the first slot and the
            # remaining slots become empty. An empty slot is legal for layout
            # containers only when it carried no placeholder (checked above),
            # matching the chat provider's reflow rule.
            first_target, first_reason = self._finish_candidate(slots[0], lines[0], layout_reflow=True)
            if first_target is None:
                self._record_failure(first_reason or "low_quality", f"{self._unit_diagnostic(slots[0], group)}; 合并译文未过校验")
                return None
            targets[slots[0].id] = first_target
            for unit in slots[1:]:
                targets[unit.id] = ""
        else:
            return None

        # Non-empty slots that received nothing keep the original text; the
        # caller treats the group as partial and reports it.
        with self._state_lock:
            self.terms_hit += sum(1 for _source, term_target in terms if term_target and term_target in target)
        self._clear_last_error()
        for unit in group.units:
            if unit.id not in targets:
                continue
            self._emit_result(unit, group, "draft", submitted=unit.source_text, candidate=targets[unit.id], target=targets[unit.id], status="ok")
        return targets

    def _rescue_passage_fragments(
        self,
        group: TranslationGroup,
        glossary: Glossary,
        targets: dict[str, str],
    ) -> list[str]:
        """Translate a visible passage as one sentence and hand out its shares.

        One sentence is often split across sibling components so that a colour,
        a click event or a link can sit in the middle of it: ``"\\nThe "`` +
        ``"Faron Woods"`` + ``" are located just ..."`` is one visible line. A
        component-at-a-time MT request cannot see that — the fragment comes back
        empty or as a literal fragment and stays English in the package (RoH:
        ``/3`` answered with an empty string, ``/5`` rejected for its lost line
        shape).

        ``quality.passage_runs`` names exactly those components that share one
        array, so the passage is the author's, not a guess. The distribution
        back is proven rather than estimated:

        * every component whose target is already known anchors its own span in
          the joined translation — it has to appear **exactly once**;
        * the text between two anchors can only belong to the components between
          them, so a gap must have exactly one owner (no owner means the gap has
          to be whitespace, or the model moved text across an anchor);
        * every share must pass the component's own write gate untouched and
          carry content: the non-empty rule for ordinary display text is not
          relaxed here, so a fragment that would end up empty stays as it is.

        Returns the ids it translated. Any other outcome leaves the per-unit
        results exactly as they are — a passage merge is a quality improvement,
        never a safety shortcut, and it never rewrites a component boundary, a
        colour or a click event: each component still receives its own text at
        its own locator.
        """

        from .references import policy_blocks_canonical_share

        if not self.api_key:
            # The per-unit loop already reported the missing key; a doomed
            # request per group would only add noise.
            return []
        runs: list[list[TextUnit]] = []
        for run in passage_runs(group.units):
            units = [group.units[index] for index in run]
            # Sibling elements of one array *inside one command*: a group holds
            # a whole file's commands, and two tellraw lines share their payload
            # offset, so the array identity alone is not enough to call two
            # components one visible passage.
            if len({_passage_container(unit) for unit in units}) != 1:
                continue
            # ... and an array is not always one string: only the components the
            # game prints end to end form a sentence (see the helper).
            if not all(_is_concatenated_component(unit) for unit in units):
                continue
            if not all(unit.source_text.strip() for unit in units):
                continue
            runs.append(units)
        rescued: list[str] = []
        for units in runs:
            if any(policy_blocks_canonical_share(unit) for unit in units):
                continue
            anchors = [unit for unit in units if targets.get(unit.id)]
            missing = [unit for unit in units if not targets.get(unit.id)]
            if not anchors or not missing:
                continue
            self._check_cancelled()
            joined = "".join(unit.source_text for unit in units)
            reply = self._request_mt_text(joined, glossary, group=group, timeout=90)
            if reply is None:
                continue
            content, terms = reply
            content = normalize_layout_newlines(joined, content)
            spans: list[tuple[int, int, int]] = []
            cursor = 0
            for index, unit in enumerate(units):
                known = targets.get(unit.id)
                if not known:
                    continue
                found = _locate_unique(content, known.strip())
                if found is None or found[0] < cursor:
                    spans = []
                    break
                spans.append((index, found[0], found[1]))
                cursor = found[1]
            if not spans:
                continue
            shares = self._passage_shares(units, spans, content)
            if shares is None:
                continue
            with self._state_lock:
                self.terms_hit += sum(1 for _source, term_target in terms if term_target and term_target in content)
            self._clear_last_error()
            for unit, share in shares:
                targets[unit.id] = share
                rescued.append(unit.id)
                self._emit_result(unit, group, "draft", submitted=unit.source_text, candidate=share, target=share, status="ok")
        return rescued

    def _passage_shares(
        self,
        units: list[TextUnit],
        spans: list[tuple[int, int, int]],
        content: str,
    ) -> list[tuple[TextUnit, str]] | None:
        """Give the components between two anchors the text between them."""

        shares: list[tuple[TextUnit, str]] = []
        for position, (index, start, _end) in enumerate(spans):
            previous = spans[position - 1][2] if position else 0
            first_owner = spans[position - 1][0] + 1 if position else 0
            owners = units[first_owner:index]
            region = content[previous:start]
            if not owners:
                if region.strip():
                    return None
                continue
            if len(owners) > 1 or not region.strip():
                return None
            target, _reason = self._finish_candidate(owners[0], region)
            if target is None:
                return None
            shares.append((owners[0], target))
        last_index, _start, last_end = spans[-1]
        owners = units[last_index + 1 :]
        region = content[last_end:]
        if not owners:
            return None if region.strip() else shares
        if len(owners) > 1 or not region.strip():
            return None
        target, _reason = self._finish_candidate(owners[0], region)
        if target is None:
            return None
        shares.append((owners[0], target))
        return shares

    def plan_requests(self, groups: list[TranslationGroup]) -> tuple[int, int]:
        """One request per unit, and no review passes at all.

        The base class books a call per *group* and a review per high-risk
        group, neither of which describes this provider. Declaring the estimate
        here replaces the previous approach of letting the base class book the
        chat shape and then rewriting the numbers once it returned.

        Still an estimate: a multi-slot layout container is merged into one
        request, a passage whose fragments have to be translated together costs
        one more than its components would, a cache hit costs none, and a
        degraded unit costs one more — the real count is only known when the run
        ends.
        """

        draft_units = sum(len(group.units) for group in groups if not group.review_only)
        return draft_units, 0

    def _translate_and_review_group(self, group: TranslationGroup, glossary: Glossary) -> dict[str, str]:
        from .references import keep_original_target, policy_blocks_canonical_share

        reused = self._reuse_canonical_targets(group)
        if reused is not None:
            return reused
        seeded = {unit.id: unit.suggested_target for unit in group.units if unit.suggested_target is not None}
        if group.review_only and len(seeded) == len(group.units):
            # Exact historical drafts are trusted as-is, as in the chat
            # provider; a high-risk group stays visible as pending instead of
            # being sent to a reviewer that does not exist here.
            self._mark_mt_review_pending(group, list(seeded))
            return dict(seeded)

        if group.layout_reflow and not any(policy_blocks_canonical_share(unit) for unit in group.units):
            # A sign / TextDisplay reads as one phrase across its slots, so it
            # is translated as one. Returns None when the joined call cannot be
            # distributed safely, which falls through to per-slot translation.
            self._check_cancelled()
            joined = self._translate_layout_group(group, glossary)
            if joined is not None:
                self._mark_mt_review_pending(group, list(joined))
                return joined

        targets: dict[str, str] = {}
        translated_ids: list[str] = []
        for unit in group.units:
            if policy_blocks_canonical_share(unit):
                # The write-back path keeps the original for these units, so a
                # request would only spend tokens on a target nobody reads. The
                # unit still reports as resolved to keep group accounting in
                # line with the chat provider, which returns them too.
                keep_original_target(unit)
                targets[unit.id] = unit.source_text
                continue
            if unit.final_target is not None:
                targets[unit.id] = unit.final_target
                continue
            self._check_cancelled()
            target = self._translate_unit(unit, glossary, group=group)
            if target is not None:
                targets[unit.id] = target
                translated_ids.append(unit.id)
        # A sentence split across sibling components cannot be translated one
        # fragment at a time; the passage gets one request of its own, and its
        # shares still pass each component's own gate.
        translated_ids.extend(self._rescue_passage_fragments(group, glossary, targets))
        # Partial results are returned on purpose: units that failed keep the
        # original text and are reported, while the rest of the group is kept.
        self._mark_mt_review_pending(group, translated_ids)
        return targets


def translator_from_env() -> TranslatorProvider:
    choice = os.getenv("MC_HANHUA_TRANSLATOR", "").lower()
    if choice in {"openai", "ai", "chat"}:
        return OpenAICompatibleTranslator()
    if choice in {"qwen-mt", "qwenmt"}:
        return QwenMTTranslator()
    return GlossaryOnlyTranslator()


def fetch_available_models(base_url: str, api_key: str = "", timeout: int = 20) -> list[str]:
    """List models from an OpenAI-compatible API server.

    Shares the translator's endpoint normalisation and Bearer auth so the
    desktop shell and the Qt dialog expose the same capability. Returns a
    sorted, de-duplicated list of model names. Raises on connection errors,
    HTTP errors, timeouts, bad JSON or an empty model list; callers map the
    exception to a stable user-facing message.

    ``base_url`` may point at the server root, an explicit version prefix
    (``/v1``, ``/v4``, …) or an explicit ``/models`` endpoint; ``api_key`` may
    be empty for local servers that need no auth.
    """

    endpoint = models_endpoint(base_url)
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(endpoint, headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    models = parse_model_names(payload)
    if not models:
        raise ValueError("接口未返回可用模型")
    return models
