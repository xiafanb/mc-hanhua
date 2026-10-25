"""Adoption, re-validation, rollback and memory of passage revisions (DEVELOPMENT §3.12).

A passage revision moves prose line breaks between the members of one tellraw
passage.  It is adopted only after every check has been recomputed from the
current input; afterwards the members are bound together, so any later failure
rolls the whole passage back to what each member had before adoption.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path

from .models import TextUnit
from .passage_layout import PASSAGE_METADATA, PASSAGE_RULE_VERSION, check_line, locate, passage_id, passages, pointer_index, tellraw_array
from .policy import should_send_to_ai
from .quality import validate_passage_member, validate_reuse
from .task_records import unit_key

ROLLBACK_METADATA = "passage_rollback"


def _key(unit: TextUnit) -> str:
    return unit_key([], unit.file_path, unit.format, unit.locator, unit.source_text)


def _source_lines(root: Path, file_path: str, cache: dict[str, list[str] | None]) -> list[str] | None:
    if file_path not in cache:
        try:
            cache[file_path] = (root / file_path).read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            cache[file_path] = None
    return cache[file_path]


def line_sha256(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


def adopt_passage_revisions(
    root: Path,
    units: list[TextUnit],
    records: Iterable[tuple[str, dict[str, object]]],
    *,
    member_reason=None,
    blocked_keys: set[str] | frozenset[str] = frozenset(),
) -> list[dict[str, object]]:
    """Adopt each ``(provider, record)`` that proves itself against the current input.

    ``member_reason(unit, target)`` adds caller-side per-member checks (official
    terms).  Rejected records leave every unit exactly as it was.
    """

    by_key = {_key(unit): unit for unit in units}
    by_slot: dict[tuple[str, int, int], TextUnit] = {}
    for unit in units:
        if unit.format != "mcfunction":
            continue
        locator = json.loads(unit.locator)
        index = pointer_index(locator.get("command") or {})
        if index is not None:
            by_slot[(unit.file_path, int(locator["line"]), index)] = unit
    cache: dict[str, list[str] | None] = {}
    statuses: list[dict[str, object]] = []
    for provider, record in records:
        status: dict[str, object] = {"provider": provider, "status": "rejected", "passage_id": record.get("passage_id")}
        statuses.append(status)
        reason = _adopt_one(root, record, by_key, by_slot, cache, status, member_reason, blocked_keys)
        if reason is not None:
            status["reason"] = reason
        else:
            status["status"] = "adopted"
    return statuses


def _adopt_one(root, record, by_key, by_slot, cache, status, member_reason, blocked_keys) -> str | None:
    members = record.get("members")
    if not isinstance(members, dict) or not members or not all(isinstance(k, str) and isinstance(v, str) for k, v in members.items()):
        return "invalid_record"
    units = [by_key.get(key) for key in members]
    if any(unit is None for unit in units):
        return "member_missing"
    if any(key in blocked_keys for key in members):
        return "conflicts_manual_revision"
    if any(PASSAGE_METADATA in unit.metadata for unit in units):
        return "member_already_bound"
    if {unit.format for unit in units} != {"mcfunction"} or len({unit.file_path for unit in units}) != 1:
        return "not_one_line"
    locators = [json.loads(unit.locator) for unit in units]
    if len({int(loc["line"]) for loc in locators}) != 1:
        return "not_one_line"
    file_path, line_no = units[0].file_path, int(locators[0]["line"])
    commands = [loc["command"] for loc in locators]
    indices = [pointer_index(command) for command in commands]
    if any(index is None for index in indices):
        return "not_json_member"
    lines = _source_lines(root, file_path, cache)
    if lines is None or not 0 <= line_no < len(lines):
        return "source_line_missing"
    source_line = lines[line_no]
    located = locate(source_line, set(indices))
    if located is None:
        return "passage_not_found"
    json_start, passage_members = located
    if any(command.get("json_start") != json_start for command in commands):
        return "passage_not_found"
    pid = passage_id(file_path, line_no, json_start, passage_members, source_line)
    status.update(passage_id=pid, file=file_path, line=line_no + 1)
    if record.get("passage_id") not in (None, pid):
        return "passage_id_mismatch"
    if record.get("rule_version") not in (None, PASSAGE_RULE_VERSION):
        return "rule_version_mismatch"
    if record.get("source_sha256") not in (None, line_sha256(source_line)):
        return "source_changed"
    writable = {
        _key(unit)
        for index in passage_members
        if (unit := by_slot.get((file_path, line_no, index))) is not None and should_send_to_ai(unit.policy)
    }
    if set(members) != writable:
        return "member_set_mismatch"
    for unit, target in zip(units, members.values()):
        reason = validate_passage_member(unit, target)
        if reason is None and member_reason is not None:
            reason = member_reason(unit, target)
        if reason is not None:
            return f"member:{reason}"
    reason = check_line(source_line, passage_members, json_start, list(zip(commands, members.values())))
    if reason is not None:
        return reason
    chain_ids: list[str] = []
    if record.get("chain"):
        # The record only says "part of a chain"; which passages form it is
        # recomputed from the input line, like everything else.
        from .passage_sentence import soft_chains

        chain = next((c for c in soft_chains(tellraw_array(source_line)[1]) if passage_members in c), None)
        if chain is None:
            return "chain_not_found"
        chain_ids = sorted(
            passage_id(file_path, line_no, json_start, run, source_line)
            for run in chain
            if any((u := by_slot.get((file_path, line_no, i))) is not None and should_send_to_ai(u.policy) for i in run)
        )
    ids = sorted(unit.id for unit in units)
    # A remembered passage keeps the provider that produced it, so switching
    # that provider off still recognises it after any number of reruns.
    origin = str(record.get("provider") or "manual") if status["provider"] == "memory" else status["provider"]
    for unit, target in zip(units, members.values()):
        unit.metadata[PASSAGE_METADATA] = {
            "id": pid,
            "members": ids,
            "passage_members": passage_members,
            "json_start": json_start,
            "source_sha256": line_sha256(source_line),
            "target": target,
            "fallback": unit.final_target,
            "provider": origin,
            **({"chain": chain_ids} if chain_ids else {}),
        }
        unit.final_target = target
        # Reviewed revision semantics: no consistency unification, no sharing.
        unit.metadata["desktop_revision"] = True
        # A passage rolled back earlier in this run and bound again is not a rollback.
        unit.metadata.pop(ROLLBACK_METADATA, None)
    return None


def passage_groups(units: Iterable[TextUnit]) -> dict[str, list[TextUnit]]:
    groups: dict[str, list[TextUnit]] = {}
    for unit in units:
        meta = unit.metadata.get(PASSAGE_METADATA)
        if isinstance(meta, dict):
            groups.setdefault(str(meta["id"]), []).append(unit)
    return groups


def enforce_chains(units: list[TextUnit]) -> list[dict[str, object]]:
    """A chain's passages share one translation: if any is not bound, none stays bound."""

    groups = passage_groups(units)
    rolled: list[dict[str, object]] = []
    for pid, members in groups.items():
        if PASSAGE_METADATA not in members[0].metadata:
            continue
        chain = members[0].metadata[PASSAGE_METADATA].get("chain")
        if not chain:
            continue
        intact = all(p in groups and PASSAGE_METADATA in groups[p][0].metadata
                     and groups[p][0].metadata[PASSAGE_METADATA].get("chain") == chain for p in chain)
        if intact:
            continue
        for p in chain:
            for unit in groups.get(p, []):
                if PASSAGE_METADATA in unit.metadata:
                    rollback_member(unit, "chain_incomplete")
            if p in groups:
                rolled.append({"passage_id": p, "status": "rolled_back", "reason": "chain_incomplete", "file": groups[p][0].file_path})
    return rolled


def rollback_member(unit: TextUnit, reason: str) -> None:
    """Return one member to its pre-adoption value (re-validated), else its source."""

    meta = unit.metadata.pop(PASSAGE_METADATA, None) or {}
    unit.metadata.pop("desktop_revision", None)
    unit.metadata[ROLLBACK_METADATA] = {"reason": reason, "passage_id": meta.get("id")}
    fallback = meta.get("fallback")
    unit.final_target = None
    if isinstance(fallback, str):
        result = validate_reuse(unit, fallback)
        if result.normalized is not None and result.reason is None:
            unit.final_target = result.normalized


def revalidate_groups(units: list[TextUnit], *, member_reason=None, require_target: bool = True) -> list[dict[str, object]]:
    """Re-run membership and per-member checks; roll back whole groups that fail."""

    rolled: list[dict[str, object]] = []
    for pid, members in passage_groups(units).items():
        meta = members[0].metadata[PASSAGE_METADATA]
        reason = None
        if sorted(unit.id for unit in members) != list(meta["members"]):
            reason = "passage_member_missing"
        else:
            for unit in members:
                own = unit.metadata[PASSAGE_METADATA]
                if not should_send_to_ai(unit.policy):
                    # A later stage (content safety) protected this member: the
                    # per-unit policy gate would refuse it alone and leave its
                    # siblings written, so the whole passage goes back here.
                    reason = "member:protected_policy"
                    break
                if require_target and unit.final_target != own["target"]:
                    reason = "passage_target_changed"
                    break
                reason = validate_passage_member(unit, unit.final_target)
                if reason is None and member_reason is not None:
                    reason = member_reason(unit, unit.final_target)
                if reason is not None:
                    reason = f"member:{reason}"
                    break
        if reason is not None:
            for unit in members:
                rollback_member(unit, reason)
            rolled.append({"passage_id": pid, "status": "rolled_back", "reason": reason, "file": members[0].file_path})
    rolled.extend(enforce_chains(units))
    return rolled


def memory_records(memory) -> list[tuple[str, dict[str, object]]]:
    return [("memory", record) for record in memory.passage_records()]


def stale_memory_ids(memory, statuses: list[dict[str, object]], records: list[tuple[str, dict[str, object]]]) -> None:
    """Delete every memory record that did not prove itself this run."""

    for (provider, record), status in zip(records, statuses):
        if provider == "memory" and status["status"] != "adopted":
            memory.delete_passage(str(record.get("passage_id")))


def store_published(memory, units: list[TextUnit], *, blocked: bool) -> list[str]:
    """Persist fully written passages after an unblocked publication; forget them otherwise."""

    stored: list[str] = []
    for pid, members in passage_groups(units).items():
        meta = members[0].metadata[PASSAGE_METADATA]
        written = all(unit.metadata.get("writeback_status") in {"changed", "unchanged"} for unit in members)
        if blocked or not written or sorted(unit.id for unit in members) != list(meta["members"]):
            memory.delete_passage(pid)
            continue
        memory.put_passage(pid, PASSAGE_RULE_VERSION, str(meta["source_sha256"]),
                           {_key(unit): str(unit.final_target) for unit in members}, str(meta.get("provider", "manual")),
                           chain=bool(meta.get("chain")))
        stored.append(pid)
    return stored


def propose_reflow_records(
    root: Path,
    units: list[TextUnit],
    commands: Iterable[tuple[str, str]],
    font_overrides=None,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    """B v1.4 proposals for the current per-component targets (§3.13).

    Only proposes: every record still goes through ``adopt_passage_revisions``.
    Lines that hold a bound passage member, an unwritable span or a character a
    resource pack redraws (its width is unknown) are skipped and counted.
    """

    from collections import Counter

    from .passage_reflow import REFLOW_VERSION, map_wrap_width, reflow
    from .text_components import apply_command_text, command_locator_offset

    wrap = map_wrap_width(line for rel, line in commands if rel.endswith(".mcfunction"))
    redrawn = set(getattr(font_overrides, "chars", None) or ())
    by_line: dict[tuple[str, int], list[TextUnit]] = {}
    for unit in units:
        if unit.format == "mcfunction":
            by_line.setdefault((unit.file_path, int(json.loads(unit.locator)["line"])), []).append(unit)
    cache: dict[str, list[str] | None] = {}
    stats: Counter = Counter()
    records: list[dict[str, object]] = []
    for (file_path, line_no), line_units in sorted(by_line.items()):
        if not any(unit.final_target not in (None, unit.source_text) for unit in line_units):
            continue
        lines = _source_lines(root, file_path, cache)
        if lines is None or not 0 <= line_no < len(lines) or "tellraw" not in lines[line_no]:
            continue
        stats["lines_considered"] += 1
        # Passages already bound (memory, manual, D) keep their text; B still
        # considers the other passages of the same line.
        bound = {pointer_index(json.loads(unit.locator)["command"]) for unit in line_units if PASSAGE_METADATA in unit.metadata}
        source_line = lines[line_no]
        if bound:
            parsed = tellraw_array(source_line)
            stats["skipped_bound"] += sum(1 for run in passages(parsed[1]) if bound & set(run)) if parsed else 1
        target_line = source_line
        refusals: list[str] = []
        for unit in sorted(line_units, key=lambda u: command_locator_offset(json.loads(u.locator)["command"]), reverse=True):
            if unit.final_target not in (None, unit.source_text):
                target_line = apply_command_text(target_line, json.loads(unit.locator)["command"], unit.final_target, refusals=refusals)
        if refusals:
            stats["skipped_unwritable"] += 1
            continue
        if redrawn and redrawn & set(target_line):
            stats["skipped_font_override"] += 1
            continue
        out, passage_records, refusal = reflow(source_line, target_line, wrap)
        if refusal is not None:
            stats[f"refused:{refusal}"] += 1
            continue
        for record in passage_records:
            if record.get("status") == "refused" and not bound & set(record.get("components", [])):
                stats[f"refused:{record.get('code')}"] += 1
        if out == target_line:
            continue
        parsed = tellraw_array(source_line)
        json_start = parsed[0] if parsed else None
        try:
            out_components = json.JSONDecoder().raw_decode(out, json_start)[0] if json_start is not None else None
        except ValueError:
            out_components = None
        slot = {pointer_index(json.loads(unit.locator)["command"]): unit for unit in line_units}
        for record in passage_records:
            if bound & set(record.get("components", [])):
                continue
            if record.get("status") != "applied":
                continue
            if out_components is None:
                stats["refused:unreadable_output"] += 1
                continue
            members = {
                _key(slot[index]): out_components[index]["text"]
                for index in record["components"]
                if index in slot and should_send_to_ai(slot[index].policy)
            }
            records.append({"members": members, "reflow_version": REFLOW_VERSION})
            stats["passages_proposed"] += 1
    return records, {"wrap_width": wrap, "version": REFLOW_VERSION, **dict(stats)}
