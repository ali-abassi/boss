"""Identifiers that are safe as single state-directory and Git-ref components."""
from __future__ import annotations

import re

from .util import BossError

PROJECT_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62})\Z")
WORK_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,127})\Z")


def project(value: object) -> str:
    text = str(value or "")
    if not PROJECT_PATTERN.fullmatch(text):
        raise BossError("project id must be 1-63 lowercase letters, digits, or hyphens and start alphanumeric")
    return text


def work(value: object) -> str:
    text = str(value or "")
    if not WORK_PATTERN.fullmatch(text):
        raise BossError("work id is not a safe state-path component")
    return text
