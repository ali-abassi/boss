"""Canonical delivery-mode names and read compatibility for legacy records."""
from __future__ import annotations

HIGH_ASSURANCE = "high-assurance"
DIRECT_PR = "direct-pr"
LOCAL_ONLY = "local-only"
LEGACY_HIGH_ASSURANCE = "no-mistakes"

MODES = (HIGH_ASSURANCE, DIRECT_PR, LOCAL_ONLY)
ACCEPTED_MODES = (*MODES, LEGACY_HIGH_ASSURANCE)


def normalize(value: str | None) -> str | None:
    return HIGH_ASSURANCE if value == LEGACY_HIGH_ASSURANCE else value


def high_assurance(value: str | None) -> bool:
    return normalize(value) == HIGH_ASSURANCE
