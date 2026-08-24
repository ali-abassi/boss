#!/usr/bin/env python3
"""Bossctl test entry point with a fake-only sandbox capability receipt.

Production never imports this file.  The Herdr integration tests need to build
the same profiles on Linux without claiming that Linux can launch a real
managed agent; their Herdr executable is also the repository's inert fake.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def fake_sandbox_status() -> dict:
    return {
        "ready": True,
        "platform": "test-fake-herdr",
        "sandbox_exec": None,
        "wrapper": str(REPO / "libexec" / "pi"),
        "tool_runner": str(REPO / "tests" / "fake_boss_tool.py"),
        "real_pi": "/usr/bin/true",
        "reaper_ready": True,
        "reaper_detail": "fake Herdr does not execute managed processes",
        "reason": "fake Herdr sandbox capability",
    }


def main() -> int:
    from bossctl import sandbox
    from bossctl.cli import main as bossctl_main

    sandbox.available = fake_sandbox_status
    return bossctl_main() or 0


if __name__ == "__main__":
    raise SystemExit(main())
