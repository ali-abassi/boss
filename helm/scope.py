"""Atomic, crash-recoverable same-repository scope claims."""
from __future__ import annotations
import fnmatch
import os
import re
from pathlib import PurePosixPath
from .paths import home
from .util import locked, now, read_json, write_json

SENSITIVE = (".github/workflows/*", ".helm/*", "helm.json", ".gitmodules", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "Cargo.lock")
_GLOBAL = {"*", "**", "**/*", ".", "unknown", "global"}


def normalize(paths) -> list[str]:
    if isinstance(paths, str):
        paths = paths.split(",")
    out = []
    for raw in paths or []:
        p = str(raw).strip().replace("\\", "/")
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


def claim(project: str, work_id: str, paths: list[str], owner: str, pid: int | None = None) -> bool:
    path = home() / "scope-claims.json"
    paths = normalize(paths)
    with locked(home() / "scope-claims.lock"):
        data = read_json(path, {"version": 1, "claims": []})
        data["claims"] = [c for c in data["claims"] if c.get("work_id") == work_id or _alive(c.get("pid"))]
        for c in data["claims"]:
            if c["work_id"] != work_id and c["project"] == project and overlap(paths, c["paths"]):
                write_json(path, data)
                return False
        data["claims"] = [c for c in data["claims"] if c.get("work_id") != work_id]
        data["claims"].append({"project": project, "work_id": work_id, "paths": paths, "owner": owner,
                               "pid": pid or os.getpid(), "claimed": now()})
        write_json(path, data)
        return True


def release(work_id: str) -> None:
    path = home() / "scope-claims.json"
    with locked(home() / "scope-claims.lock"):
        data = read_json(path, {"version": 1, "claims": []})
        data["claims"] = [c for c in data["claims"] if c.get("work_id") != work_id]
        write_json(path, data)


def escaped(declared: list[str], changed: list[str]) -> list[str]:
    declared = normalize(declared)
    if is_global(declared):
        return []
    return sorted(p for p in changed if not any(fnmatch.fnmatch(p, pattern) for pattern in declared))
