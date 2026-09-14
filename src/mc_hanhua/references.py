"""Cross-file mechanism reference index and canonical translation keys.

The index records every reliably parsed identifier, exact-match value and
display association. It never loosens extraction: unknown or unparsed
constructs are stored as UNKNOWN_REVIEW and are not sent to the AI.

Canonical keys prefer a mechanism object / reference id. Only when no object
is known do we fall back to exact source + semantic display role. Ordinary
polysemous words are never forced onto the same object just because the
source string matches.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .events import emit_event, redact_text
from .models import ScanResult, TextUnit
from .policy import (
    POLICY_IMMUTABLE_ID,
    POLICY_IMMUTABLE_MATCH,
    POLICY_TRANSLATABLE,
    POLICY_UNKNOWN,
)
from .quality import semantic_owner
from .text_components import (
    ITEM_MATCH_KEYS,
    SELECTOR_NAME_TAG_RE,
    SELECTOR_RE,
    _find_execute_run,
    _iter_snbt_values,
    _match_value_texts,
)

KIND_ID = "id"
KIND_MATCH = "match_value"
KIND_DISPLAY = "display"
KIND_UNKNOWN = "unknown"

ROLE_DEFINITION = "definition"
ROLE_USE = "use"

RESOURCE_FILE_KINDS = {
    "functions": "function",
    "predicates": "predicate",
    "recipes": "recipe",
    "advancements": "advancement",
    "advancement": "advancement",
    "loot_tables": "loot_table",
}

FUNCTION_ID_RE = re.compile(r"^#?[a-z0-9_.-]+:[a-z0-9_./-]+$", re.IGNORECASE)
BOSSBAR_ID_RE = re.compile(r"^[a-z0-9_.-]+:[a-z0-9_.-]+$", re.IGNORECASE)
STORAGE_ID_RE = re.compile(r"^[a-z0-9_.-]+:[a-z0-9_./-]+$", re.IGNORECASE)
# Single-token objective / team names. Selectors and numeric fake-players are not IDs.
SIMPLE_NAME_RE = re.compile(r"^[A-Za-z0-9_./+-]+$")
_POLYSEMOUS_WORD_RE = re.compile(r"^[a-z]{2,16}$")
_MAX_INDEX_ENTRIES = 400
_MAX_OCCURRENCES = 12
_MAX_CONFLICT_SAMPLES = 3
_MAX_CONFLICT_TARGETS = 8


@dataclass(slots=True)
class ReferenceOccurrence:
    file: str
    locator: str
    context: str
    kind: str
    policy: str
    unit_id: str = ""
    role: str = ROLE_USE
    source: str = ""
    object_key: str = ""
    detail: str = ""

    def source_label(self) -> str:
        detail = self.detail or self.kind
        where = self.context or self.locator
        if where:
            return f"{self.file}:{detail}:{where}"
        return f"{self.file}:{detail}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "locator": redact_text(self.locator, 200),
            "context": redact_text(self.context, 200),
            "kind": self.kind,
            "policy": self.policy,
            "unit_id": self.unit_id,
            "role": self.role,
            "source": redact_text(self.source, 200),
            "object_key": self.object_key,
            "detail": self.detail,
        }


@dataclass(slots=True)
class ReferenceEntry:
    normalized: str
    kind: str
    object_key: str
    policy: str
    definitions: list[ReferenceOccurrence] = field(default_factory=list)
    uses: list[ReferenceOccurrence] = field(default_factory=list)

    @property
    def locked(self) -> bool:
        return self.policy in {POLICY_IMMUTABLE_ID, POLICY_IMMUTABLE_MATCH}

    def to_dict(self) -> dict[str, Any]:
        return {
            "normalized": self.normalized,
            "kind": self.kind,
            "object_key": self.object_key,
            "policy": self.policy,
            "locked": self.locked,
            "definitions": [item.to_dict() for item in self.definitions[:_MAX_OCCURRENCES]],
            "uses": [item.to_dict() for item in self.uses[:_MAX_OCCURRENCES]],
            "definition_count": len(self.definitions),
            "use_count": len(self.uses),
        }


@dataclass
class ReferenceIndex:
    """normalized value + object key → definitions / uses."""

    entries: dict[str, ReferenceEntry] = field(default_factory=dict)
    by_object: dict[str, ReferenceEntry] = field(default_factory=dict)
    by_value: dict[str, list[str]] = field(default_factory=dict)
    locked_values: set[str] = field(default_factory=set)
    unresolved: list[dict[str, Any]] = field(default_factory=list)
    ambiguous: list[dict[str, Any]] = field(default_factory=list)
    unknown: list[dict[str, Any]] = field(default_factory=list)
    display_candidates: list[dict[str, Any]] = field(default_factory=list)

    def add(self, occ: ReferenceOccurrence) -> None:
        if not occ.source:
            return
        key = occ.object_key or f"{occ.kind}:{normalize_value(occ.source)}"
        occ.object_key = key
        entry = self.by_object.get(key)
        if entry is None:
            entry = ReferenceEntry(
                normalized=normalize_value(occ.source),
                kind=occ.kind,
                object_key=key,
                policy=occ.policy,
            )
            self.by_object[key] = entry
            self.entries[key] = entry
        if occ.role == ROLE_DEFINITION:
            entry.definitions.append(occ)
        else:
            entry.uses.append(occ)
        self.by_value.setdefault(occ.source, []).append(key)
        if occ.source != normalize_value(occ.source):
            self.by_value.setdefault(normalize_value(occ.source), []).append(key)
        if entry.locked:
            self.locked_values.add(occ.source)
        if occ.kind == KIND_UNKNOWN or occ.policy == POLICY_UNKNOWN:
            if len(self.unknown) < 80:
                self.unknown.append(occ.to_dict())

    def object_keys_for_value(self, value: str) -> list[str]:
        keys = list(dict.fromkeys(self.by_value.get(value, []) + self.by_value.get(normalize_value(value), [])))
        return keys

    def match_values(self) -> set[str]:
        return {
            occ.source
            for entry in self.by_object.values()
            if entry.kind == KIND_MATCH
            for occ in (*entry.definitions, *entry.uses)
            if occ.source
        }

    def finalize(self) -> None:
        """Classify unresolved / ambiguous references after the full scan."""

        for entry in self.by_object.values():
            if entry.kind == KIND_ID and entry.uses and not entry.definitions:
                if len(self.unresolved) < 80:
                    self.unresolved.append(
                        {
                            "object_key": entry.object_key,
                            "normalized": entry.normalized,
                            "kind": entry.kind,
                            "uses": [occ.to_dict() for occ in entry.uses[:_MAX_OCCURRENCES]],
                        }
                    )
        for value, keys in self.by_value.items():
            distinct = []
            seen: set[str] = set()
            for key in keys:
                if key in seen:
                    continue
                seen.add(key)
                entry = self.by_object.get(key)
                if entry is not None and entry.kind in {KIND_ID, KIND_MATCH}:
                    distinct.append(key)
            if len(distinct) > 1 and len(self.ambiguous) < 80:
                self.ambiguous.append(
                    {
                        "value": redact_text(value, 200),
                        "object_keys": distinct[:12],
                        "reason": "same normalized value maps to multiple mechanism objects",
                    }
                )

    def to_report(self) -> dict[str, Any]:
        locked = sorted(self.locked_values)[:80]
        entries = [entry.to_dict() for entry in list(self.by_object.values())[:_MAX_INDEX_ENTRIES]]
        return {
            "entry_count": len(self.by_object),
            "locked_value_count": len(self.locked_values),
            "locked_values": locked,
            "unresolved": list(self.unresolved),
            "ambiguous": list(self.ambiguous),
            "unknown": list(self.unknown),
            "display_candidates": list(self.display_candidates),
            "definitions": [
                {
                    "object_key": entry.object_key,
                    "kind": entry.kind,
                    "normalized": entry.normalized,
                    "occurrences": [occ.to_dict() for occ in entry.definitions[:_MAX_OCCURRENCES]],
                }
                for entry in self.by_object.values()
                if entry.definitions
            ][:_MAX_INDEX_ENTRIES],
            "uses": [
                {
                    "object_key": entry.object_key,
                    "kind": entry.kind,
                    "normalized": entry.normalized,
                    "occurrences": [occ.to_dict() for occ in entry.uses[:_MAX_OCCURRENCES]],
                }
                for entry in self.by_object.values()
                if entry.uses
            ][:_MAX_INDEX_ENTRIES],
            "entries": entries,
        }


def normalize_value(value: str) -> str:
    return value.strip()


def build_reference_index(
    result: ScanResult,
    identity_hits: Iterable[dict[str, Any]] | None = None,
) -> ReferenceIndex:
    """Build the index from scanned commands, identity hits and resource files."""

    index = ReferenceIndex()
    for file_path, line in result.commands:
        for occ in collect_command_occurrences(file_path, line):
            index.add(occ)
        if _command_unparsed(line):
            index.add(
                ReferenceOccurrence(
                    file=file_path,
                    locator="command",
                    context=line[:120],
                    kind=KIND_UNKNOWN,
                    policy=POLICY_UNKNOWN,
                    role=ROLE_USE,
                    source="",
                    object_key=f"unknown:{file_path}",
                    detail="unparsed_command",
                )
            )
            if len(index.unknown) < 80:
                index.unknown.append(
                    {
                        "file": file_path,
                        "kind": KIND_UNKNOWN,
                        "policy": POLICY_UNKNOWN,
                        "reason": "command SNBT or resource id could not be fully parsed",
                        "context": redact_text(line, 200),
                    }
                )
    for hit in identity_hits or []:
        index.add(_occurrence_from_hit(hit))
    _add_resource_file_definitions(index, result.root)
    _attach_display_units(index, result.text_units)
    index.finalize()
    result.reference_index = index
    _emit_index_events(index)
    return index


def collect_command_occurrences(file_path: str, command: str) -> list[ReferenceOccurrence]:
    """Parse one command into typed reference occurrences.

    Only constructs the existing parsers already recognise are recorded as
    id / match_value. Anything we cannot classify stays UNKNOWN and is not
    treated as a translatable unlock.
    """

    occs: list[ReferenceOccurrence] = []
    execute_run = _find_execute_run(command)
    head = command[:execute_run] if execute_run is not None else command
    tail = command[execute_run:] if execute_run is not None else ""
    occs.extend(_selector_occurrences(file_path, command))
    occs.extend(_snbt_match_occurrences(file_path, head, consumer=_is_consumer_command(head)))
    # Resource IDs live in the execute prefix too (if predicate / if score / storage).
    occs.extend(_resource_id_occurrences(file_path, head))
    if tail:
        occs.extend(collect_command_occurrences(file_path, tail))
    return occs


def apply_index_to_units(result: ScanResult, index: ReferenceIndex | None = None) -> None:
    """Stamp object keys and concrete reference_sources onto scanned units."""

    index = index or result.reference_index
    if index is None:
        return
    for unit in result.text_units:
        keys = index.object_keys_for_value(unit.source_text)
        match_keys = [key for key in keys if key in index.by_object and index.by_object[key].kind == KIND_MATCH]
        display_keys = [
            key
            for key in keys
            if key in index.by_object and index.by_object[key].kind == KIND_DISPLAY and _unit_belongs_to_object(unit, index.by_object[key])
        ]
        chosen = ""
        if len(display_keys) == 1:
            chosen = display_keys[0]
        elif len(display_keys) > 1:
            index.ambiguous.append(
                {
                    "value": redact_text(unit.source_text, 200),
                    "unit_id": unit.id,
                    "file": unit.file_path,
                    "object_keys": display_keys,
                    "reason": "display unit matches multiple objects; not bound",
                }
            )
        elif len(match_keys) == 1:
            chosen = match_keys[0]
        if chosen:
            unit.object_key = chosen
            unit.metadata["object_key"] = chosen
        sources: list[str] = []
        for key in match_keys or ([chosen] if chosen else []):
            entry = index.by_object.get(key)
            if entry is None:
                continue
            for occ in (*entry.uses, *entry.definitions):
                label = occ.source_label()
                if label not in sources:
                    sources.append(label)
        if sources:
            unit.reference_sources = sources[:20]
            unit.metadata["reference_sources"] = list(unit.reference_sources)


def canonical_key_for(unit: TextUnit) -> str:
    """Stable grouping key used before AI dispatch and at write-back audit.

    Priority: mechanism object / reference key, then exact source + semantic
    display role. Layout slots stay unique so sign reflow is never unified.
    """

    if unit.canonical_key:
        return unit.canonical_key
    if unit.metadata.get("layout_container") in {"sign", "text-display"}:
        return f"layout:{unit.id}"
    if unit.object_key:
        return f"object:{unit.object_key}"
    _owner, role = semantic_owner(unit)
    return f"display:{role}:{unit.source_text}"


def assign_canonical_keys(units: Iterable[TextUnit]) -> None:
    for unit in units:
        key = canonical_key_for(unit)
        unit.canonical_key = key
        unit.metadata["canonical_key"] = key


_NONSHAREABLE_POLICIES = {POLICY_IMMUTABLE_ID, POLICY_IMMUTABLE_MATCH, POLICY_UNKNOWN}


def unit_policy_name(unit: TextUnit) -> str:
    return str(unit.policy or (unit.metadata or {}).get("policy") or "")


def policy_blocks_canonical_share(unit: TextUnit) -> bool:
    """Identity / match / unknown units must keep their source text."""

    return unit_policy_name(unit) in _NONSHAREABLE_POLICIES


def keep_original_target(unit: TextUnit) -> None:
    """Force an immutable / unknown unit back to its source text."""

    unit.final_target = unit.source_text
    unit.metadata["canonical_role"] = unit.metadata.get("canonical_role") or "locked"


def is_shareable_canonical(unit: TextUnit) -> bool:
    """Whether this unit may reuse another unit's validated target.

    Sharing is only allowed between safe display units. IMMUTABLE_ID,
    IMMUTABLE_MATCH_VALUE and UNKNOWN_REVIEW never donate or receive a
    shared target, even when they share an object key with a book title.
    Layout slots stay unique so sign / text-display reflow is not unified.
    Ordinary polysemous words are never bound together just because the
    source string matches.
    """

    if policy_blocks_canonical_share(unit):
        return False
    if unit.metadata.get("layout_container") in {"sign", "text-display"}:
        return False
    key = unit.canonical_key or canonical_key_for(unit)
    if key.startswith("layout:"):
        return False
    if key.startswith("object:"):
        return True
    if unit.format == "mcfunction":
        return True
    if _POLYSEMOUS_WORD_RE.fullmatch(unit.source_text.strip()):
        return False
    return True


def can_receive_shared_target(unit: TextUnit) -> bool:
    """Whether a validated sibling translation may be written onto ``unit``."""

    if unit.final_target is not None:
        return False
    return is_shareable_canonical(unit)


def share_existing_canonical_targets(units: list[TextUnit]) -> int:
    """Copy an already-known target onto same-key units that still need one."""

    assign_canonical_keys(units)
    shared = 0
    for _key, members in _shareable_groups(units).items():
        existing = [
            member.final_target
            for member in members
            if member.final_target not in (None, "") and is_shareable_canonical(member)
        ]
        distinct = set(existing)
        if len(distinct) != 1:
            continue
        target = existing[0]
        for member in members:
            if can_receive_shared_target(member):
                member.final_target = target
                member.metadata["canonical_role"] = "follower"
                shared += 1
    return shared


def select_canonical_leaders(units: list[TextUnit]) -> list[TextUnit]:
    """Keep one AI leader per shareable canonical key; mark the rest followers."""

    assign_canonical_keys(units)
    leaders: list[TextUnit] = []
    seen: dict[str, TextUnit] = {}
    for unit in units:
        if policy_blocks_canonical_share(unit):
            keep_original_target(unit)
            continue
        if unit.final_target is not None:
            continue
        key = unit.canonical_key
        if not is_shareable_canonical(unit):
            unit.metadata["canonical_role"] = "solo"
            leaders.append(unit)
            continue
        leader = seen.get(key)
        if leader is None:
            seen[key] = unit
            unit.metadata["canonical_role"] = "leader"
            leaders.append(unit)
        else:
            unit.metadata["canonical_role"] = "follower"
            unit.metadata["canonical_leader"] = leader.id
    return leaders


def apply_leader_targets(units: list[TextUnit]) -> tuple[int, list[dict[str, object]]]:
    """Reuse the leader's validated target; never overwrite a different context."""

    assign_canonical_keys(units)
    applied = 0
    conflicts: dict[str, dict[str, object]] = {}
    for unit in units:
        if policy_blocks_canonical_share(unit):
            keep_original_target(unit)
    for key, members in _shareable_groups(units).items():
        by_target: dict[str, list[TextUnit]] = defaultdict(list)
        pending: list[TextUnit] = []
        for member in members:
            if policy_blocks_canonical_share(member):
                keep_original_target(member)
                continue
            if member.final_target is None:
                pending.append(member)
            else:
                by_target[member.final_target].append(member)
        if len(by_target) > 1:
            _record_canonical_conflict(conflicts, key, members, "multiple validated targets")
            continue
        if len(by_target) == 1 and pending:
            target = next(iter(by_target))
            for member in pending:
                if not can_receive_shared_target(member):
                    continue
                member.final_target = target
                applied += 1
        elif len(by_target) == 0:
            continue
    return applied, sorted(conflicts.values(), key=lambda entry: str(entry.get("canonical_key") or entry.get("normalized") or ""))


def audit_canonical_consistency(units: list[TextUnit]) -> list[dict[str, object]]:
    """Report same-object display conflicts without rewriting mechanism IDs."""

    assign_canonical_keys(units)
    conflicts: dict[str, dict[str, object]] = {}
    for key, members in _shareable_groups(units).items():
        targets = {member.final_target for member in members if member.final_target not in (None, "")}
        if len(targets) > 1:
            reason = "referenced object has conflicting display translations"
            if not key.startswith("object:"):
                reason = "same source and display role produced different translations"
            _record_canonical_conflict(conflicts, key, members, reason)
            # Conservative: do not pick a winner. Leave each target as-is so a
            # later human pass can decide; mechanism IDs were never in this set.
    return sorted(conflicts.values(), key=lambda entry: str(entry.get("normalized") or entry.get("canonical_key") or ""))


def record_display_candidates(index: ReferenceIndex | None, units: Iterable[TextUnit]) -> None:
    if index is None:
        return
    by_object: dict[str, list[TextUnit]] = defaultdict(list)
    for unit in units:
        if unit.object_key:
            by_object[unit.object_key].append(unit)
    for object_key, members in by_object.items():
        targets = sorted({member.final_target or "" for member in members if member.final_target})
        if len(targets) > 1 and len(index.display_candidates) < 80:
            index.display_candidates.append(
                {
                    "object_key": object_key,
                    "source": redact_text(members[0].source_text, 200),
                    "targets": targets[:_MAX_CONFLICT_TARGETS],
                    "files": [member.file_path for member in members[:_MAX_OCCURRENCES]],
                }
            )


def _shareable_groups(units: Iterable[TextUnit]) -> dict[str, list[TextUnit]]:
    groups: dict[str, list[TextUnit]] = defaultdict(list)
    for unit in units:
        if not is_shareable_canonical(unit):
            continue
        groups[unit.canonical_key or canonical_key_for(unit)].append(unit)
    return groups


def _record_canonical_conflict(
    conflicts: dict[str, dict[str, object]],
    key: str,
    members: list[TextUnit],
    reason: str,
    normalized: str = "",
) -> None:
    source = " | ".join(sorted({member.source_text for member in members}))
    entry = conflicts.get(key)
    if entry is None:
        entry = {
            "source": source,
            "normalized": normalized or members[0].source_text.strip().lower(),
            "canonical_key": key,
            "reason": reason,
            "targets": [],
        }
        conflicts[key] = entry
    elif source not in str(entry["source"]):
        entry["source"] = f"{entry['source']} | {source}"
    by_target: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for member in members:
        if member.final_target:
            by_target[member.final_target].append((member.file_path, member.context))
    for target, samples in sorted(by_target.items(), key=lambda item: (-len(item[1]), item[0])):
        existing = next((item for item in entry["targets"] if item["target"] == target), None)
        if existing is None:
            if len(entry["targets"]) >= _MAX_CONFLICT_TARGETS:
                continue
            existing = {"target": target, "count": 0, "samples": []}
            entry["targets"].append(existing)
        existing["count"] += len(samples)
        for file_path, context in samples[:_MAX_CONFLICT_SAMPLES]:
            sample = {"file": file_path, "context": context}
            if sample not in existing["samples"]:
                existing["samples"].append(sample)


def _selector_occurrences(file_path: str, command: str) -> list[ReferenceOccurrence]:
    occs: list[ReferenceOccurrence] = []
    for selector in SELECTOR_RE.finditer(command):
        for match in SELECTOR_NAME_TAG_RE.finditer(selector.group(1)):
            if match.group(2) == "!":
                continue
            value = match.group(3).strip().strip("\"'")
            if not value:
                continue
            kind_sel = match.group(1).lower()
            if kind_sel == "tag":
                occs.append(
                    ReferenceOccurrence(
                        file=file_path,
                        locator=f"selector tag={value}",
                        context=f"@{selector.group(0)[:40]}",
                        kind=KIND_ID,
                        policy=POLICY_IMMUTABLE_ID,
                        role=ROLE_USE,
                        source=value,
                        object_key=f"id:tag:{value}",
                        detail="selector_tag",
                    )
                )
            else:
                occs.append(
                    ReferenceOccurrence(
                        file=file_path,
                        locator=f"selector name={value}",
                        context=f"@{selector.group(0)[:40]}",
                        kind=KIND_MATCH,
                        policy=POLICY_IMMUTABLE_MATCH,
                        role=ROLE_USE,
                        source=value,
                        object_key=f"match:selector_name:{value}",
                        detail="selector_name",
                    )
                )
    return occs


def _snbt_match_occurrences(file_path: str, command: str, *, consumer: bool) -> list[ReferenceOccurrence]:
    occs: list[ReferenceOccurrence] = []
    stripped = command.strip().lstrip("/")
    lower = stripped.lower()
    if lower.startswith("say ") or lower.startswith("me "):
        return occs
    entries, parsed_ok = _iter_snbt_values(command)
    if not parsed_ok and "{" in command:
        occs.append(
            ReferenceOccurrence(
                file=file_path,
                locator="snbt",
                context=command[:120],
                kind=KIND_UNKNOWN,
                policy=POLICY_UNKNOWN,
                role=ROLE_USE,
                source="",
                object_key=f"unknown:snbt:{file_path}",
                detail="unparsed_snbt",
            )
        )
    for path, start, end, quoted in entries:
        if not path:
            continue
        key = str(path[-1]).lower()
        display_name = key == "name" and len(path) >= 2 and str(path[-2]).lower() == "display"
        is_custom_name = key == "customname"
        is_item_match = key in ITEM_MATCH_KEYS or display_name
        if not is_custom_name and not (is_item_match and consumer):
            continue
        texts = _match_value_texts(command[start:end], quoted)
        locator = "/".join(str(part) for part in path)
        for text in texts:
            if is_custom_name:
                role = ROLE_USE if consumer or _is_data_predicate(command) else ROLE_DEFINITION
                detail = "custom_name"
                object_key = f"match:custom_name:{text}"
            else:
                role = ROLE_USE
                detail = f"item_match:{path[-1]}"
                object_key = f"match:item:{text}"
            occs.append(
                ReferenceOccurrence(
                    file=file_path,
                    locator=locator,
                    context=command[:120],
                    kind=KIND_MATCH,
                    policy=POLICY_IMMUTABLE_MATCH,
                    role=role,
                    source=text,
                    object_key=object_key,
                    detail=detail,
                )
            )
    return occs


def _resource_id_occurrences(file_path: str, command: str) -> list[ReferenceOccurrence]:
    occs: list[ReferenceOccurrence] = []
    for start in _command_starts(command):
        tokens = _split_tokens(start)
        if not tokens:
            continue
        cmd = tokens[0].lstrip("/").lower()
        if cmd == "function" and len(tokens) >= 2:
            occs.append(_id_occ(file_path, tokens[1], "function", ROLE_USE, start))
        elif cmd == "schedule" and len(tokens) >= 3 and tokens[1].lower() == "function":
            occs.append(_id_occ(file_path, tokens[2], "function", ROLE_USE, start))
        elif cmd == "scoreboard":
            occs.extend(_scoreboard_occs(file_path, tokens, start))
        elif cmd == "team" and len(tokens) >= 3:
            occs.extend(_team_occs(file_path, tokens, start))
        elif cmd == "bossbar" and len(tokens) >= 3:
            occs.extend(_bossbar_occs(file_path, tokens, start))
        elif cmd == "data" and len(tokens) >= 4 and tokens[2].lower() == "storage":
            occs.append(_id_occ(file_path, tokens[3], "storage", ROLE_USE, start))
        elif cmd == "execute":
            occs.extend(_execute_id_occs(file_path, start))
        elif cmd == "predicate" and len(tokens) >= 2:
            occs.append(_id_occ(file_path, tokens[1], "predicate", ROLE_USE, start))
        elif cmd == "recipe" and len(tokens) >= 3:
            occs.append(_id_occ(file_path, tokens[-1], "recipe", ROLE_USE, start))
        elif cmd == "advancement" and len(tokens) >= 4:
            occs.append(_id_occ(file_path, tokens[-1], "advancement", ROLE_USE, start))
        elif cmd == "loot" and "loot_table" not in start.lower():
            for token in tokens[1:]:
                if ":" in token and FUNCTION_ID_RE.match(token):
                    occs.append(_id_occ(file_path, token, "loot_table", ROLE_USE, start))
                    break
    return [occ for occ in occs if occ.source]


def _scoreboard_occs(file_path: str, tokens: list[str], start: str) -> list[ReferenceOccurrence]:
    occs: list[ReferenceOccurrence] = []
    if len(tokens) >= 4 and tokens[1].lower() == "objectives":
        op = tokens[2].lower()
        name = tokens[3]
        if op == "add":
            occs.append(_id_occ(file_path, name, "scoreboard", ROLE_DEFINITION, start))
        elif op in {"remove", "modify"}:
            occs.append(_id_occ(file_path, name, "scoreboard", ROLE_USE, start))
        elif op == "setdisplay" and len(tokens) >= 5:
            occs.append(_id_occ(file_path, tokens[4], "scoreboard", ROLE_USE, start))
    elif len(tokens) >= 5 and tokens[1].lower() == "players":
        op = tokens[2].lower()
        if op in {"add", "set", "remove", "get", "enable", "random", "reset"}:
            occs.append(_id_occ(file_path, tokens[4], "scoreboard", ROLE_USE, start))
        elif op == "operation" and len(tokens) >= 7:
            occs.append(_id_occ(file_path, tokens[4], "scoreboard", ROLE_USE, start))
            occs.append(_id_occ(file_path, tokens[6], "scoreboard", ROLE_USE, start))
    return occs


def _team_occs(file_path: str, tokens: list[str], start: str) -> list[ReferenceOccurrence]:
    op = tokens[1].lower()
    name = tokens[2]
    if op == "add":
        return [_id_occ(file_path, name, "team", ROLE_DEFINITION, start)]
    if op in {"join", "leave", "empty", "modify", "msg", "remove"}:
        return [_id_occ(file_path, name, "team", ROLE_USE, start)]
    return []


def _bossbar_occs(file_path: str, tokens: list[str], start: str) -> list[ReferenceOccurrence]:
    op = tokens[1].lower()
    bid = tokens[2]
    if not BOSSBAR_ID_RE.match(bid):
        return [
            ReferenceOccurrence(
                file=file_path,
                locator="bossbar",
                context=start[:120],
                kind=KIND_UNKNOWN,
                policy=POLICY_UNKNOWN,
                role=ROLE_USE,
                source=bid,
                object_key=f"unknown:bossbar:{bid}",
                detail="invalid_bossbar_id",
            )
        ]
    role = ROLE_DEFINITION if op == "add" else ROLE_USE
    return [_id_occ(file_path, bid, "bossbar", role, start)]


def _execute_id_occs(file_path: str, start: str) -> list[ReferenceOccurrence]:
    occs: list[ReferenceOccurrence] = []
    for match in re.finditer(r"\b(?:if|unless)\s+score\s+(\S+)\s+(\S+)", start, re.IGNORECASE):
        occs.append(_id_occ(file_path, match.group(2), "scoreboard", ROLE_USE, start))
    for match in re.finditer(r"\bstore\s+(?:result|success)\s+score\s+(\S+)\s+(\S+)", start, re.IGNORECASE):
        occs.append(_id_occ(file_path, match.group(2), "scoreboard", ROLE_USE, start))
    for match in re.finditer(r"\b(?:if|unless)\s+predicate\s+(\S+)", start, re.IGNORECASE):
        occs.append(_id_occ(file_path, match.group(1), "predicate", ROLE_USE, start))
    for match in re.finditer(r"\b(?:if|unless)\s+data\s+storage\s+(\S+)", start, re.IGNORECASE):
        occs.append(_id_occ(file_path, match.group(1), "storage", ROLE_USE, start))
    for match in re.finditer(r"\bwith\s+storage\s+(\S+)", start, re.IGNORECASE):
        occs.append(_id_occ(file_path, match.group(1), "storage", ROLE_USE, start))
    return occs


def _id_occ(file_path: str, raw: str, kind: str, role: str, start: str) -> ReferenceOccurrence:
    value = raw.strip().strip("\"'")
    valid = True
    if kind in {"function", "predicate", "recipe", "advancement", "storage", "loot_table"}:
        valid = bool(FUNCTION_ID_RE.match(value) or (kind == "storage" and STORAGE_ID_RE.match(value)))
    elif kind == "bossbar":
        valid = bool(BOSSBAR_ID_RE.match(value))
    elif kind in {"scoreboard", "team", "tag"}:
        valid = bool(value) and SIMPLE_NAME_RE.match(value) is not None and not value.startswith("@") and value != "*"
    if not value or not valid:
        return ReferenceOccurrence(
            file=file_path,
            locator=kind,
            context=start[:120],
            kind=KIND_UNKNOWN,
            policy=POLICY_UNKNOWN,
            role=role,
            source=value,
            object_key=f"unknown:{kind}:{value}",
            detail=f"unparsed_{kind}",
        )
    return ReferenceOccurrence(
        file=file_path,
        locator=kind,
        context=start[:120],
        kind=KIND_ID,
        policy=POLICY_IMMUTABLE_ID,
        role=role,
        source=value,
        object_key=f"id:{kind}:{value}",
        detail=kind,
    )


def _add_resource_file_definitions(index: ReferenceIndex, root: Path) -> None:
    if not root or not Path(root).exists():
        return
    for path in Path(root).rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        parts = Path(rel).parts
        try:
            data_index = parts.index("data")
        except ValueError:
            continue
        if data_index + 2 >= len(parts):
            continue
        ns = parts[data_index + 1]
        folder = parts[data_index + 2]
        kind = RESOURCE_FILE_KINDS.get(folder)
        if kind is None:
            continue
        suffix = path.suffix.lower()
        if kind == "function" and suffix != ".mcfunction":
            continue
        if kind != "function" and suffix != ".json":
            continue
        rel_under = Path(*parts[data_index + 3 :]).as_posix()
        stem = rel_under[: -len(path.suffix)] if path.suffix else rel_under
        resource_id = f"{ns}:{stem}"
        index.add(
            ReferenceOccurrence(
                file=rel,
                locator=resource_id,
                context=rel,
                kind=KIND_ID,
                policy=POLICY_IMMUTABLE_ID,
                role=ROLE_DEFINITION,
                source=resource_id,
                object_key=f"id:{kind}:{resource_id}",
                detail=f"{kind}_file",
            )
        )


def _attach_display_units(index: ReferenceIndex, units: Iterable[TextUnit]) -> None:
    """Bind audited display units to a known object when the locator says so.

    We never bind a random tellraw / lore string to an object just because
    the source text matches: that would collapse unrelated polysemes.
    """

    for unit in units:
        object_key, detail = _display_object_key(unit)
        if not object_key:
            continue
        unit.object_key = object_key
        unit.metadata["object_key"] = object_key
        index.add(
            ReferenceOccurrence(
                file=unit.file_path,
                locator=unit.locator,
                context=unit.context,
                kind=KIND_DISPLAY,
                policy=unit.policy or POLICY_TRANSLATABLE,
                unit_id=unit.id,
                role=ROLE_DEFINITION,
                source=unit.source_text,
                object_key=object_key,
                detail=detail,
            )
        )


def _display_object_key(unit: TextUnit) -> tuple[str, str]:
    locator_obj: dict[str, Any] = {}
    path: list[Any] = []
    try:
        parsed = json.loads(unit.locator)
    except (TypeError, ValueError, json.JSONDecodeError):
        parsed = None
    if isinstance(parsed, dict):
        locator_obj = parsed
        raw_path = parsed.get("path")
        if isinstance(raw_path, list):
            path = raw_path
    lowered = [str(part).lower() for part in path]
    if "objectives" in lowered and any(part == "displayname" for part in lowered):
        name = _collection_member_name(path, "objectives")
        if name:
            return f"object:scoreboard:{name}", "objective_display"
    if "teams" in lowered and any(part in {"displayname", "prefix", "suffix"} for part in lowered):
        name = _collection_member_name(path, "teams")
        if name:
            return f"object:team:{name}", "team_display"
    if "bossbars" in lowered or "bossbar" in lowered:
        if any(part in {"name", "displayname"} for part in lowered):
            name = _collection_member_name(path, "bossbars") or _collection_member_name(path, "bossbar")
            if name:
                return f"object:bossbar:{name}", "bossbar_display"
    command = locator_obj.get("command") if isinstance(locator_obj.get("command"), dict) else None
    # Command display text is not an object key by itself.
    _ = command
    return "", ""


def _collection_member_name(path: list[Any], collection: str) -> str:
    lowered = [str(part).lower() for part in path]
    try:
        index = lowered.index(collection)
    except ValueError:
        return ""
    if index + 1 >= len(path):
        return ""
    member = path[index + 1]
    if isinstance(member, str) and member and not member.isdigit():
        return member
    return ""


def _unit_belongs_to_object(unit: TextUnit, entry: ReferenceEntry) -> bool:
    if unit.object_key and unit.object_key == entry.object_key:
        return True
    object_key, _detail = _display_object_key(unit)
    return bool(object_key) and object_key == entry.object_key


def _occurrence_from_hit(hit: dict[str, Any]) -> ReferenceOccurrence:
    value = str(hit.get("value") or hit.get("source") or "")
    kind = str(hit.get("kind") or KIND_MATCH)
    detail = str(hit.get("detail") or hit.get("object_type") or kind)
    object_key = str(hit.get("object_key") or f"match:{detail}:{value}")
    policy = str(hit.get("policy") or (POLICY_IMMUTABLE_MATCH if kind == KIND_MATCH else POLICY_IMMUTABLE_ID))
    return ReferenceOccurrence(
        file=str(hit.get("file") or ""),
        locator=str(hit.get("locator") or ""),
        context=str(hit.get("context") or ""),
        kind=kind,
        policy=policy,
        unit_id=str(hit.get("unit_id") or ""),
        role=str(hit.get("role") or ROLE_DEFINITION),
        source=value,
        object_key=object_key,
        detail=detail,
    )


def _is_consumer_command(command: str) -> bool:
    stripped = command.strip().lstrip("/")
    lower = stripped.lower()
    if lower.startswith("clear"):
        return True
    if re.search(r"\b(if|unless)\s+(items|data)\b", lower):
        return True
    return False


def _is_data_predicate(command: str) -> bool:
    return bool(re.search(r"\b(if|unless)\s+data\b", command, re.IGNORECASE))


def _command_unparsed(command: str) -> bool:
    if "{" not in command and "[" not in command:
        return False
    stripped = command.strip().lstrip("/").lower()
    if stripped.startswith("say ") or stripped.startswith("me "):
        return False
    _entries, parsed_ok = _iter_snbt_values(command)
    return not parsed_ok


def _command_starts(line: str) -> list[str]:
    starts = [line]
    current = line
    while True:
        match = re.search(r"\srun\s", current, re.IGNORECASE)
        if not match:
            break
        prefix = current[: match.start()]
        if not prefix.strip().lstrip("/").lower().startswith("execute "):
            break
        current = current[match.end() :]
        starts.append(current)
    return starts


def _split_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    current: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(text):
        ch = text[index]
        if quote is not None:
            if ch == "\\":
                current.append(ch)
                if index + 1 < len(text):
                    current.append(text[index + 1])
                    index += 1
            elif ch == quote:
                quote = None
            else:
                current.append(ch)
        elif ch in "'\"":
            quote = ch
        elif ch.isspace():
            if current:
                tokens.append("".join(current))
                current = []
        else:
            current.append(ch)
        index += 1
    if current:
        tokens.append("".join(current))
    return tokens


def _emit_index_events(index: ReferenceIndex) -> None:
    emit_event(
        stage="reference_index",
        extra={
            "entry_count": len(index.by_object),
            "locked_value_count": len(index.locked_values),
            "unresolved": len(index.unresolved),
            "ambiguous": len(index.ambiguous),
            "unknown": len(index.unknown),
        },
    )
    for value in sorted(index.locked_values)[:40]:
        emit_event(stage="reference_lock", source=value, policy=POLICY_IMMUTABLE_MATCH)
    for item in index.unresolved[:20]:
        emit_event(stage="reference_unresolved", extra=item)
    for item in index.ambiguous[:20]:
        emit_event(stage="reference_ambiguous", extra=item)


