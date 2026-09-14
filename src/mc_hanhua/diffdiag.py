"""Read-only NBT text comparison and same-name structure-file diagnosis.

These helpers never rewrite game files. They report:

- input English / extracted / AI-success / still-English / actually written
  changes for NBT display units;
- colliding same-basename structure files such as multiple ``graves_open.nbt``.

A translated structure *source* must never be described as an already-generated
world update: generated/<namespace>/structures/*.nbt is a template, not a
populated region.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from .events import redact_text
from .models import ScanResult, TextUnit
from .utils import looks_like_chinese_translation

STRUCTURE_NAME_HINTS = ("/structure/", "/structures/", "generated/")


def _has_latin(text: str) -> bool:
    return any(ch.isascii() and ch.isalpha() for ch in text)


def compare_nbt_text(
    scan_result: ScanResult,
    units: list[TextUnit],
    root: Path | None = None,
) -> dict[str, Any]:
    """Summarise input vs written NBT display text. Read-only."""

    nbt_units = [unit for unit in units if unit.format == "nbt-text"]
    extracted = len(nbt_units)
    input_english = sum(1 for unit in nbt_units if _has_latin(unit.source_text))
    ai_success = sum(
        1
        for unit in nbt_units
        if unit.final_target not in (None, unit.source_text) and looks_like_chinese_translation(unit.final_target or "")
    )
    still_english = sum(
        1
        for unit in nbt_units
        if (unit.final_target is None or unit.final_target == unit.source_text) and _has_latin(unit.source_text)
    )
    writeback_changed = sum(1 for unit in nbt_units if unit.metadata.get("writeback_status") == "changed")
    writeback_failed = sum(1 for unit in nbt_units if unit.metadata.get("writeback_status") == "failed")
    samples: list[dict[str, Any]] = []
    for unit in nbt_units[:30]:
        samples.append(
            {
                "file": unit.file_path,
                "locator": redact_text(unit.locator, 200),
                "source": redact_text(unit.source_text),
                "final": redact_text(unit.final_target or ""),
                "writeback": unit.metadata.get("writeback_status") or "",
                "policy": unit.policy,
            }
        )
    note = (
        "结构源文件（generated/*/structures 或 data/*/structure）的译文只改模板，"
        "不表示已生成世界或已放置的结构实例被更新。"
    )
    return {
        "extracted": extracted,
        "input_english": input_english,
        "ai_success": ai_success,
        "still_english": still_english,
        "writeback_changed": writeback_changed,
        "writeback_failed": writeback_failed,
        "filtered_nbt": {
            reason: scan_result.filtered_counts.get(reason, 0)
            for reason in (
                "author_name",
                "entity_name_preserved",
                "mechanism_locked",
                "reference_locked",
                "unknown_mechanism_field",
                "nbt_parse_error",
            )
            if scan_result.filtered_counts.get(reason)
        },
        "samples": samples,
        "note": note,
    }


def diagnose_structure_conflicts(root: Path) -> list[dict[str, Any]]:
    """List same-basename structure files. Does not modify anything."""

    groups: dict[str, list[str]] = defaultdict(list)
    if not root.exists():
        return []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() != ".nbt":
            continue
        rel = path.relative_to(root).as_posix()
        lowered = f"/{rel.lower()}"
        if not any(hint in lowered for hint in STRUCTURE_NAME_HINTS) and "generated/" not in rel.lower():
            continue
        groups[path.name.lower()].append(rel)
    conflicts: list[dict[str, Any]] = []
    for name, paths in sorted(groups.items()):
        if len(paths) < 2:
            continue
        conflicts.append(
            {
                "name": name,
                "count": len(paths),
                "paths": sorted(paths),
                "note": (
                    "同名结构模板同时存在于多个路径；翻译结构源不会更新已生成世界里的同名实例。"
                    if name == "graves_open.nbt" or "grave" in name
                    else "同名结构模板同时存在于多个路径；这是只读诊断，不改变游戏机制。"
                ),
            }
        )
    return conflicts


def unit_locator_json(unit: TextUnit) -> dict[str, Any] | None:
    try:
        parsed = json.loads(unit.locator)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None
