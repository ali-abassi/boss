"""Atomic, crash-recoverable same-repository scope claims."""
from __future__ import annotations
import fnmatch
import os
import secrets
from .paths import home
from .util import locked, now, read_json, write_json
from . import processes

SENSITIVE = (".github/workflows/*", ".helm/*", "helm.json", ".gitmodules", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "Cargo.lock")
_GLOBAL = {"*", "**", "**/*", ".", "unknown", "global"}


def normalize(paths) -> list[str]:
    if isinstance(paths, str):
        paths = paths.split(",")
    out = []
    for raw in paths or []:
        # Git path evidence is POSIX byte-preserving text. A backslash is a
        # legal filename character there, not a directory separator; rewriting
        # it could bind a claim to a different path than the one reviewed.
        p = str(raw).strip()
        if p.startswith("./"):
            p = p[2:]
        if p:
            out.append(p)
    return sorted(set(out)) or ["unknown"]


def is_global(paths: list[str]) -> bool:
    return any(p in _GLOBAL or any(fnmatch.fnmatch(p, s) or fnmatch.fnmatch(s, p) for s in SENSITIVE) for p in paths)


def _fixed_components(pattern: str) -> tuple[list[str], bool]:
    parts, fixed = pattern.split("/"), []
    for part in parts:
        if any(c in part for c in "*?["):
            return fixed, False
        fixed.append(part)
    return fixed, True


def patterns_overlap(a: str, b: str) -> bool:
    """Conservative glob intersection: prove disjoint only via incompatible fixed components."""
    if a in _GLOBAL or b in _GLOBAL:
        return True
    if fnmatch.fnmatch(a, b) or fnmatch.fnmatch(b, a):
        return True
    ca, exact_a = _fixed_components(a); cb, exact_b = _fixed_components(b)
    for x, y in zip(ca, cb):
        if x != y:
            return False
    # A complete literal cannot intersect a distinct path below/above it; otherwise a
    # wildcard/partial component may still intersect and must serialize.
    if exact_a and exact_b:
        return a == b
    return True


def overlap(a: list[str], b: list[str]) -> bool:
    if is_global(a) or is_global(b):
        return True
    return any(patterns_overlap(x, y) for x in a for y in b)


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0); return True
    except (OSError, TypeError, ValueError):
        return False


def claim(project: str, work_id: str, paths: list[str], owner: str, pid: int | None = None,
          process_identity: dict | None = None, *, replace_token: str | None = None) -> str | None:
    """Acquire a scope and return its unguessable ownership token.

    The token is required for release/hold so an old runner can never remove a
    newer runner's claim for the same work item.
    """
    path = home() / "scope-claims.json"
    paths = normalize(paths)
    pid = pid or os.getpid()
    if process_identity is None and pid == os.getpid():
        process_identity = processes.capture(pid, owner)
    token = secrets.token_hex(24)
    with locked(home() / "scope-claims.lock"):
        data = read_json(path, {"version": 1, "claims": []})
        # A dead PID does not authorize forgetting ownership: the worktree may
        # contain unlanded changes. Confirmed doctor reconciliation removes a
        # stale claim only into a durable recovery hold after inspecting it.
        same = next((c for c in data["claims"] if c.get("work_id") == work_id), None)
        if same:
            # A queued/stale runner must not overwrite an active or recovery
            # claim merely because it knows the item id.  Only the exact token
            # recorded on a recovery-required item can exchange its hold for a
            # new runner lease.
            if (not replace_token or not same.get("held_for_recovery")
                    or not secrets.compare_digest(str(same.get("claim_token") or ""), str(replace_token))):
                return None
        elif replace_token:
            # The item expected to exchange a recovery hold, but the hold has
            # disappeared or changed.  Continuing would lose collision proof.
            return None
        for c in data["claims"]:
            if c["work_id"] != work_id and c["project"] == project and overlap(paths, c["paths"]):
                return None
        data["claims"] = [c for c in data["claims"] if c.get("work_id") != work_id]
        data["claims"].append({"project": project, "work_id": work_id, "paths": paths, "owner": owner,
                               "pid": pid, "process_identity": process_identity,
                               "claim_token": token, "claimed": now()})
        write_json(path, data)
        return token


def release(work_id: str, claim_token: str | None) -> bool:
    path = home() / "scope-claims.json"
    if not claim_token:
        return False
    with locked(home() / "scope-claims.lock"):
        data = read_json(path, {"version": 1, "claims": []})
        match = next((c for c in data["claims"] if c.get("work_id") == work_id), None)
        if not match or not secrets.compare_digest(str(match.get("claim_token") or ""), str(claim_token)):
            return False
        data["claims"] = [c for c in data["claims"] if c is not match]
        write_json(path, data)
        return True


def hold(work_id: str, claim_token: str | None, reason: str) -> bool:
    """Retain collision protection when a live external actor may still mutate."""
    path = home() / "scope-claims.json"
    if not claim_token:
        return False
    with locked(home() / "scope-claims.lock"):
        data = read_json(path, {"version": 1, "claims": []})
        claim = next((entry for entry in data["claims"] if entry.get("work_id") == work_id), None)
        if claim and secrets.compare_digest(str(claim.get("claim_token") or ""), str(claim_token)):
            claim.update(pid=None, process_identity=None, owner=f"recovery:{work_id}", held_for_recovery=True,
                         hold_reason=reason, reconciled=now())
            write_json(path, data)
            return True
        return False


def escaped(declared: list[str], changed: list[str]) -> list[str]:
    declared = normalize(declared)
    if is_global(declared):
        return []
    return sorted(p for p in changed if not any(fnmatch.fnmatch(p, pattern) for pattern in declared))
