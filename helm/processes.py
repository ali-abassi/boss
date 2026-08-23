"""Exact process identities for worker lifecycle; PIDs alone are never authority."""
from __future__ import annotations

import hashlib
import os
import subprocess
from typing import Any

from .util import now

VERSION = 1


def _field(pid: int, name: str) -> str | None:
    try:
        result = subprocess.run(["ps", "-p", str(pid), "-o", f"{name}="], text=True,
                                capture_output=True, stdin=subprocess.DEVNULL, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def exists(pid: Any) -> str:
    try:
        value = int(pid)
        if value <= 0:
            return "unknown"
        os.kill(value, 0)
        return "present"
    except ProcessLookupError:
        return "absent"
    except PermissionError:
        return "unknown"
    except (OSError, TypeError, ValueError):
        return "unknown"


def capture(pid: int, owner: str) -> dict | None:
    """Capture the already-exec'd daemon. Failure means it must not be registered."""
    start, command = _field(pid, "lstart"), _field(pid, "command")
    if not start or not command:
        return None
    try:
        pgid = os.getpgid(pid)
    except OSError:
        return None
    return {"version": VERSION, "kind": "firstmate-worker", "pid": int(pid), "pgid": int(pgid),
            "owner": owner, "start_sha256": _digest(start), "command_sha256": _digest(command),
            "registered_at": now()}


def probe(record: object) -> dict:
    """Return live only for an exact captured identity; legacy integers are untrusted."""
    if not isinstance(record, dict):
        state = exists(record)
        return {"state": "dead" if state == "absent" else "untrusted", "pid": record,
                "reason": "legacy PID has no process-start/command identity"}
    pid = record.get("pid")
    presence = exists(pid)
    if presence == "absent":
        return {"state": "dead", "pid": pid, "reason": "captured PID is positively absent"}
    if presence != "present":
        return {"state": "unknown", "pid": pid, "reason": "process presence could not be proven"}
    if (record.get("version") != VERSION or record.get("kind") != "firstmate-worker"
            or not record.get("start_sha256") or not record.get("command_sha256")):
        return {"state": "untrusted", "pid": pid, "reason": "worker identity record is incomplete"}
    start, command = _field(int(pid), "lstart"), _field(int(pid), "command")
    if not start or not command:
        return {"state": "unknown", "pid": pid, "reason": "live process metadata is unavailable"}
    try:
        pgid = os.getpgid(int(pid))
    except OSError:
        return {"state": "unknown", "pid": pid, "reason": "process group is unavailable"}
    if (_digest(start) != record.get("start_sha256") or _digest(command) != record.get("command_sha256")
            or int(record.get("pgid", -1)) != pgid):
        return {"state": "reused", "pid": pid, "reason": "PID now belongs to a different process identity"}
    return {"state": "live", "pid": int(pid), "pgid": pgid, "owner": record.get("owner"),
            "reason": "exact process start, command, and group identity match"}


def liveness(record: object | None, legacy_pid: object | None = None) -> dict:
    """Conservative execution-owner liveness for leases and claims.

    New records carry an exact process identity.  A legacy bare PID can prove
    only positive absence: a present PID may have been reused and is therefore
    unknown, never live authority.
    """
    if isinstance(record, dict):
        evidence = probe(record)
        if evidence.get("state") == "live":
            return evidence
        if evidence.get("state") == "dead":
            return evidence
        return {**evidence, "state": "unknown"}
    pid = legacy_pid if legacy_pid is not None else record
    presence = exists(pid)
    if presence == "absent":
        return {"state": "dead", "pid": pid, "reason": "legacy PID is positively absent"}
    return {"state": "unknown", "pid": pid,
            "reason": "legacy/live PID has no exact process-start identity"}
