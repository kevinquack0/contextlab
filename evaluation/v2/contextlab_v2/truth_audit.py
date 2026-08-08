"""Detect false claims that the v1 Claude Opus review was human review."""

from __future__ import annotations

import re
from pathlib import Path


FORBIDDEN_PATTERNS = (
    re.compile(r"\bhuman[- ]audited\b", re.IGNORECASE),
    re.compile(r"\bhuman audit\b", re.IGNORECASE),
    re.compile(r"\bauditoria humana\b", re.IGNORECASE),
    re.compile(r"\brevisão humana\b", re.IGNORECASE),
    re.compile(
        r"\btodas\s+(?:as\s+)?notas\s+(?:foram|são)\s+revisadas\s+individualmente"
        r"(?:\s+pelo\s+autor)?\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bavaliação cega e auditada\b", re.IGNORECASE),
    re.compile(r"\bhumano\s*\(Opus\)", re.IGNORECASE),
    re.compile(r"\bhuman\s*\(Opus\)\s+audit\b", re.IGNORECASE),
)


def publication_paths(root: Path) -> list[Path]:
    candidates = [root / "README.md", root / "evaluation" / "README.md"]
    candidates.extend((root / "results" / "final").glob("*.md"))
    candidates.extend((root / "docs" / "tcc_draft").glob("*.md"))
    candidates.extend((root / "docs" / "handoff").glob("*.md"))
    return sorted(path for path in candidates if path.is_file())


def audit_truth_language(root: Path) -> list[str]:
    findings: list[str] = []
    for path in publication_paths(root):
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for pattern in FORBIDDEN_PATTERNS:
                if pattern.search(line):
                    relative = path.relative_to(root)
                    findings.append(f"{relative}:{line_number}: {pattern.pattern}")
                    break
    return findings
