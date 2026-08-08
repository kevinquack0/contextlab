"""Safe corpus boundary used by every ContextLab v2 adapter."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


PROTECTED_COMPONENT = "evaluation_only_do_not_index"


class ProtectedDataError(PermissionError):
    """Raised when code attempts to load evaluator-only or sealed data."""


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class CorpusBoundary:
    """Resolve and load only files inside one approved corpus root."""

    corpus_root: Path
    protected_roots: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        lexical_parts = self.corpus_root.parts
        resolved_root = self.corpus_root.resolve()
        if PROTECTED_COMPONENT in lexical_parts or PROTECTED_COMPONENT in resolved_root.parts:
            raise ProtectedDataError(f"protected path cannot be a corpus root: {self.corpus_root}")
        for protected in self.protected_roots:
            if _is_relative_to(resolved_root, protected.resolve()):
                raise ProtectedDataError(f"corpus root is inside protected data: {self.corpus_root}")

    def validate(self, path: Path) -> Path:
        lexical = path if path.is_absolute() else self.corpus_root / path
        if PROTECTED_COMPONENT in lexical.parts:
            raise ProtectedDataError(f"protected data path rejected: {path}")
        root = self.corpus_root.resolve()
        resolved = lexical.resolve()
        if not _is_relative_to(resolved, root):
            raise ProtectedDataError(f"path escapes approved corpus root: {path}")
        for protected in self.protected_roots:
            if _is_relative_to(resolved, protected.resolve()):
                raise ProtectedDataError(f"protected data path rejected: {path}")
        if PROTECTED_COMPONENT in resolved.parts:
            raise ProtectedDataError(f"protected data path rejected: {path}")
        return resolved

    def load_text(self, path: Path, *, encoding: str = "utf-8") -> str:
        return self.validate(path).read_text(encoding=encoding)

    def discover(self, patterns: Iterable[str] = ("*.md",)) -> list[Path]:
        files: list[Path] = []
        for pattern in patterns:
            for path in self.corpus_root.rglob(pattern):
                if path.is_file() or path.is_symlink():
                    files.append(self.validate(path))
        return sorted(set(files))
