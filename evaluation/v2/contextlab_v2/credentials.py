"""Credential presence and redaction for the fixed paid gateway."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping
from typing import Any


OPENROUTER_ENV = "OPENROUTER_API_KEY"
KEYCHAIN_SERVICE = "contextlab-openrouter"
_TOKEN_PATTERN = re.compile(r"sk-or-v1-[A-Za-z0-9_-]+")


class CredentialError(RuntimeError):
    """The process cannot safely obtain or handle the OpenRouter credential."""


def require_runtime_credential(environment: Mapping[str, str] | None = None) -> str:
    environment = os.environ if environment is None else environment
    value = environment.get(OPENROUTER_ENV, "")
    if not value:
        raise CredentialError(f"{OPENROUTER_ENV} is not set")
    if not value.startswith("sk-or-v1-") or len(value) < 24:
        raise CredentialError(f"{OPENROUTER_ENV} does not have the expected OpenRouter form")
    return value


def redact(value: Any, *, known_secret: str | None = None) -> Any:
    """Recursively redact OpenRouter-looking tokens and one exact in-memory secret."""
    if isinstance(value, str):
        redacted = value.replace(known_secret, "[REDACTED]") if known_secret else value
        return _TOKEN_PATTERN.sub("[REDACTED]", redacted)
    if isinstance(value, list):
        return [redact(item, known_secret=known_secret) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item, known_secret=known_secret) for item in value)
    if isinstance(value, dict):
        return {key: redact(item, known_secret=known_secret) for key, item in value.items()}
    return value


def keychain_entry_present(*, account: str | None = None) -> bool:
    """Check item presence without requesting or displaying its password."""
    account = account or os.environ.get("USER")
    if not account:
        return False
    result = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0
