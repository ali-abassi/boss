#!/usr/bin/env python3
"""Bounded command runner for the inert fake-Herdr integration tests only.

The production runner proves a Darwin sandbox fingerprint.  Cross-platform
controller tests need only its exit-code and timeout contract; the real
sandbox/reaper behavior is exercised separately by ``test_sandbox`` on macOS.
"""
from __future__ import annotations

import hashlib
import re
import sys
import time
from pathlib import Path


def fail(message: str) -> None:
    print(f"fake-firstmate-tool: {message}", file=sys.stderr)
    raise SystemExit(126)


if len(sys.argv) != 6:
    fail("expected PROFILE PROFILE_SHA256 SCRATCH TIMEOUT COMMAND")

profile = Path(sys.argv[1])
expected = sys.argv[2]
scratch = Path(sys.argv[3])
try:
    timeout = max(1, min(3600, int(sys.argv[4])))
except ValueError:
    fail("invalid timeout")
command = sys.argv[5]

if not re.fullmatch(r"[0-9a-f]{64}", expected):
    fail("invalid profile receipt")
try:
    profile_bytes = profile.read_bytes()
except OSError as exc:
    fail(f"profile unavailable: {type(exc).__name__}")
if (profile.is_symlink() or not profile.is_file()
        or hashlib.sha256(profile_bytes).hexdigest() != expected):
    fail("profile receipt does not match")
if scratch.is_symlink() or not scratch.is_dir():
    fail("scratch directory is unsafe")

# The fake-Herdr fixtures use only these two verification shapes.  Do not run
# an arbitrary shell here: even a test-only process group cannot contain a
# hostile double-fork plus setsid() portably.  Unknown commands fail before any
# child or side effect exists.
if command == "true":
    raise SystemExit(0)
sleep_match = re.fullmatch(r"sleep ([0-9]+(?:\.[0-9]+)?)", command)
if not sleep_match:
    fail("unsupported fake verification command")
duration = float(sleep_match.group(1))
time.sleep(min(duration, float(timeout)))
raise SystemExit(124 if duration > timeout else 0)
