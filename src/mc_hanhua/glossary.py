from __future__ import annotations

import csv
import json
import re
from pathlib import Path

from .models import GlossaryEntry

BUILTIN_ENTRIES = [
    GlossaryEntry("Overworld", "主世界", "Minecraft dimension", 10),
    GlossaryEntry("Nether", "下界", "Minecraft dimension", 10),
    GlossaryEntry("End", "末地", "Minecraft dimension", 10),
    GlossaryEntry("Creeper", "苦力怕", "Minecraft mob", 10),
    GlossaryEntry("Zombie", "僵尸", "Minecraft mob", 10),
    GlossaryEntry("Skeleton", "骷髅", "Minecraft mob", 10),
    GlossaryEntry("Redstone", "红石", "Minecraft system", 10),
    GlossaryEntry("Copper", "铜", "Minecraft material", 20),
    GlossaryEntry("Quest", "任务", "Quest system", 20),
    GlossaryEntry("Reward", "奖励", "Quest system", 20),
]


class Glossary:
    def __init__(self, entries: list[GlossaryEntry] | None = None) -> None:
        self.entries = sorted(entries or [], key=lambda entry: entry.priority)
        # Entries are sorted by priority ascending, so for a duplicated source
        # the LATER entry (higher priority) wins in the exact map. This is the
        # observable precedence: user glossary > existing zh_cn pairs > builtin.
        self.exact = {entry.source.lower(): entry.target for entry in self.entries}

    @classmethod
    def builtin(cls) -> Glossary:
        return cls(BUILTIN_ENTRIES)

    @classmethod
    def from_file(cls, path: Path) -> Glossary:
        suffix = path.suffix.lower()
        entries: list[GlossaryEntry] = []
        if suffix == ".json":
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                data = [{"source": key, "target": value} for key, value in data.items()]
            for item in data:
                entries.append(
                    GlossaryEntry(
                        source=str(item["source"]),
                        target=str(item["target"]),
                        note=str(item.get("note", "")),
                        priority=int(item.get("priority", 100)),
                    )
                )
        elif suffix in {".csv", ".tsv"}:
            delimiter = "\t" if suffix == ".tsv" else ","
            with path.open("r", encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f, delimiter=delimiter):
                    entries.append(
                        GlossaryEntry(
                            source=row["source"],
                            target=row["target"],
                            note=row.get("note", ""),
                            priority=int(row.get("priority") or 100),
                        )
                    )
        else:
            raise ValueError(f"Unsupported glossary format: {path}")
        return cls(entries)

    def merge(self, other: Glossary) -> Glossary:
        return Glossary([*self.entries, *other.entries])

    def exact_match(self, text: str) -> str | None:
        return self.exact.get(text.strip().lower())

    def has_entry(self, source: str) -> bool:
        """True when the exact (case-insensitive) source is a glossary term."""

        return source.strip().lower() in self.exact

    def mask_terms(self, text: str) -> tuple[str, dict[str, str]]:
        """Replace highest-priority term occurrences with immutable {termN} tokens.

        The tokens match PLACEHOLDER_RE so every downstream guard (placeholder
        preservation, structural token validation) treats them like mandatory
        markers: the model cannot drop, duplicate or rewrite them unnoticed.
        Overlapping terms resolve by priority at each position: the highest
        priority entry that starts at a position wins.
        """

        mapping: dict[str, str] = {}
        if not text or not self.entries:
            return text, mapping
        branches: list[tuple[GlossaryEntry, str]] = []
        for entry in reversed(self.entries):
            source = entry.source
            if not source or len(source) < 2:
                continue
            if source == "End" and "dimension" in (entry.note or "").lower():
                # Dimension End must not mask ordinary narrative "end".
                continue
            if re.fullmatch(r"[A-Za-z0-9 ]+", source):
                pattern = rf"\b{re.escape(source)}\b"
            else:
                pattern = re.escape(source)
            branches.append((entry, pattern))
        if not branches:
            return text, mapping
        # Highest priority wins at each position; ties resolve to the longest
        # source so a more specific phrase is not shadowed by its prefix.
        # Sort before compiling: match.lastindex indexes the pattern's groups.
        branches.sort(key=lambda item: (-item[0].priority, -len(item[0].source)))
        combined = re.compile("|".join(f"({pattern})" for _entry, pattern in branches))
        pieces: list[str] = []
        cursor = 0
        index = 0
        for match in combined.finditer(text):
            pieces.append(text[cursor : match.start()])
            entry = branches[match.lastindex - 1][0]
            token = f"{{term{index}}}"
            while token in text:
                index += 1
                token = f"{{term{index}}}"
            pieces.append(token)
            mapping[token] = entry.target
            cursor = match.end()
            index += 1
        if not mapping:
            return text, mapping
        pieces.append(text[cursor:])
        return "".join(pieces), mapping

    @staticmethod
    def restore_terms(text: str, mapping: dict[str, str]) -> str:
        """Substitute the immutable tokens back to their glossary targets."""

        restored = text
        for token, target in mapping.items():
            restored = restored.replace(token, target)
        return restored

    def apply_terms(self, text: str) -> str:
        translated = text
        for entry in self.entries:
            if re.fullmatch(r"[A-Za-z0-9 ]+", entry.source):
                pattern = re.compile(rf"\b{re.escape(entry.source)}\b")
                translated = pattern.sub(entry.target, translated)
            else:
                translated = translated.replace(entry.source, entry.target)
        return translated
