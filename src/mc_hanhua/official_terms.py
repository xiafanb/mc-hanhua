from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# source_hash is SHA-256 of canonical UTF-8 JSON:
# {"en_us": <object>, "zh_cn": <object>} with sort_keys=True and separators=(",", ":").
_CANONICAL_SEPARATORS = (",", ":")
_LATIN_TERM_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9' _-]*")
_INFO_UNKNOWN_ROLE = "official_terms_role_unknown"


def bundled_official_terms():
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2])) / "assets" / "official-terms"
    try:
        metadata = json.loads((root / "metadata.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return OfficialTermIndex.from_directory(root, metadata["game_version"])


def _canonical_pair_hash(en_us: object, zh_cn: object) -> str:
    payload = json.dumps({"en_us": en_us, "zh_cn": zh_cn}, ensure_ascii=False, sort_keys=True, separators=_CANONICAL_SEPARATORS)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _boundary_contains(haystack: str, needle: str) -> bool:
    """Whole-token match for Latin terms; literal containment otherwise."""

    if not needle or not haystack:
        return False
    if _LATIN_TERM_RE.fullmatch(needle):
        return re.search(rf"(?<![A-Za-z0-9_]){re.escape(needle)}(?![A-Za-z0-9_])", haystack) is not None
    return needle in haystack


@dataclass(frozen=True, slots=True)
class OfficialTerm:
    key: str
    source: str
    target: str
    game_version: str
    source_kind: str
    source_hash: str


@dataclass(slots=True)
class OfficialTermIndex:
    terms: list[OfficialTerm] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    game_version: str = ""
    source_kind: str = "official"
    source_hash: str = ""
    status: str = "unavailable"

    @property
    def available(self) -> bool:
        return self.status == "available" and self.source_kind == "official" and any(term.source_kind == "official" for term in self.terms)

    @classmethod
    def unavailable(cls, warning: str = "official_terms_unavailable") -> OfficialTermIndex:
        return cls(status="unavailable", warnings=[warning] if warning else [])

    @classmethod
    def from_json_pair(
        cls,
        en_us_path: Path | str,
        zh_cn_path: Path | str,
        game_version: str,
        *,
        source_kind: str = "official",
    ) -> OfficialTermIndex:
        warnings: list[str] = []
        conflicts: list[str] = []
        en_us_path = Path(en_us_path)
        zh_cn_path = Path(zh_cn_path)
        try:
            en_data = json.loads(en_us_path.read_text(encoding="utf-8-sig"))
            zh_data = json.loads(zh_cn_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            warnings.append(f"official_terms_unreadable: {exc}")
            return cls(warnings=warnings, game_version=game_version, source_kind=source_kind, status="unavailable")
        if not isinstance(en_data, dict) or not isinstance(zh_data, dict):
            warnings.append("official_terms_not_object")
            return cls(warnings=warnings, game_version=game_version, source_kind=source_kind, status="unavailable")

        source_hash = _canonical_pair_hash(en_data, zh_data)
        terms: list[OfficialTerm] = []
        seen: dict[tuple[str, str], str] = {}
        keys = set(en_data) | set(zh_data)
        for key in sorted(str(item) for item in keys):
            if key not in en_data or key not in zh_data:
                warnings.append(f"official_terms_key_skipped:{key}")
                continue
            source = en_data[key]
            target = zh_data[key]
            if not isinstance(source, str) or not source.strip() or not isinstance(target, str) or not target.strip():
                warnings.append(f"official_terms_key_skipped:{key}")
                continue
            pair = (key, source)
            previous = seen.get(pair)
            if previous is not None and previous != target:
                conflicts.append(f"official_terms_conflict:{key}")
                continue
            seen[pair] = target
            terms.append(
                OfficialTerm(
                    key=key,
                    source=source,
                    target=target,
                    game_version=game_version,
                    source_kind=source_kind,
                    source_hash=source_hash,
                )
            )
        status = "available" if terms else "unavailable"
        return cls(
            terms=terms,
            warnings=warnings,
            conflicts=conflicts,
            game_version=game_version,
            source_kind=source_kind,
            source_hash=source_hash,
            status=status,
        )

    @classmethod
    def from_directory(cls, dir_path: Path | str, game_version: str) -> OfficialTermIndex:
        directory = Path(dir_path)
        en_us = directory / "en_us.json"
        zh_cn = directory / "zh_cn.json"
        if not en_us.is_file() or not zh_cn.is_file():
            return cls.unavailable("official_terms_unavailable")
        return cls.from_json_pair(en_us, zh_cn, game_version)

    def _official_terms(self) -> list[OfficialTerm]:
        if self.source_kind != "official":
            return []
        return [term for term in self.terms if term.source_kind == "official"]

    def _version_ok(self, term: OfficialTerm, game_version: str | None) -> bool:
        if not game_version:
            return True
        expected = term.game_version or self.game_version
        return not expected or expected == game_version

    def lookup_exact(self, key: str, source_text: str, game_version: str | None = None) -> OfficialTerm | None:
        matches = [
            term
            for term in self._official_terms()
            if term.key == key and term.source == source_text and self._version_ok(term, game_version)
        ]
        if not matches:
            return None
        targets = {term.target for term in matches}
        if len(targets) > 1:
            self.conflicts.append(f"official_terms_conflict:{key}")
            return None
        return matches[0]

    def terms_for_context(self, source_text: str, role: str | None, game_version: str | None = None) -> list[OfficialTerm]:
        if not role or not str(role).strip():
            self.diagnostics.append(_INFO_UNKNOWN_ROLE)
            return []
        role_text = str(role).strip()
        hits: list[OfficialTerm] = []
        for term in self._official_terms():
            if not self._version_ok(term, game_version):
                continue
            prefix = term.key.split(".", 1)[0]
            if prefix != role_text and term.key != role_text and not term.key.startswith(f"{role_text}."):
                continue
            if _boundary_contains(source_text, term.source):
                hits.append(term)
        by_source: dict[str, set[str]] = {}
        for term in hits:
            by_source.setdefault(term.source, set()).add(term.target)
        if any(len(targets) > 1 for targets in by_source.values()):
            self.conflicts.append(f"official_terms_context_conflict:{role_text}")
            return []
        unique: list[OfficialTerm] = []
        seen: set[tuple[str, str, str]] = set()
        for term in hits:
            marker = (term.key, term.source, term.target)
            if marker in seen:
                continue
            seen.add(marker)
            unique.append(term)
        return unique

    def validate_candidate(
        self,
        source_text: str,
        target_text: str,
        *,
        key: str | None = None,
        role: str | None = None,
        game_version: str | None = None,
    ) -> str | None:
        if not self.available:
            return None
        if key:
            term = self.lookup_exact(key, source_text, game_version)
            if term is not None and target_text != term.target:
                return "official_term_conflict"
        if role:
            for term in self.terms_for_context(source_text, role, game_version):
                if term.target not in target_text:
                    return "official_term_conflict"
        return None
