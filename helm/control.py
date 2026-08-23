"""Durable control-plane primitives: CAS records, redaction, controls and inspection."""
from __future__ import annotations
import copy
import re
from pathlib import Path
from typing import Callable
from .paths import home
from .util import HelmError, locked, now, read_json, write_json

SCHEMA_VERSION = 2
_SECRET_KEYS = re.compile(r"(?i)(^|[_-])(token|secret|password|passwd|authorization|api[-_]?key|credential|cookie|access|refresh)($|[_-])")
_SECRET_VALUES = [
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\b(?:sk|ghp|github_pat|xox[baprs])[-_][A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)((?:token|secret|password|api[-_]?key)\s*[=:]\s*)\S+"),
]


def item_lock(work_id: str) -> Path:
    return home() / "work" / work_id / "item.lock"


def cas_update(work_id: str, mutate: Callable[[dict], None], expected_revision: int | None = None) -> dict:
    """Atomically mutate one item. Optional revision makes stale writers fail closed."""
    path = home() / "work" / work_id / "item.json"
    with locked(item_lock(work_id)):
        item = read_json(path)
        if not item:
            raise HelmError(f"unknown work item '{work_id}'")
        revision = int(item.get("revision", 0))
        if expected_revision is not None and revision != expected_revision:
            raise HelmError(f"stale work item revision {expected_revision}; current revision is {revision}")
        mutate(item)
        item["schema_version"] = SCHEMA_VERSION
        item["revision"] = revision + 1
        item["updated"] = now()
        write_json(path, item)
        return item


def redact(value):
    """Recursively redact every captain-visible field, including dict keys and history."""
    if isinstance(value, dict):
        return {k: ("[REDACTED]" if _SECRET_KEYS.search(str(k)) else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if not isinstance(value, str):
        return value
    out = value
    for pattern in _SECRET_VALUES:
        out = pattern.sub(lambda m: (m.group(1) if m.lastindex else "") + "[REDACTED]", out)
    return out


def request(work_id: str, action: str, value=None) -> dict:
    allowed = {"steer", "pause", "resume", "away", "interrupt", "recover"}
    if action not in allowed:
        raise HelmError(f"unknown control '{action}'")
    def mutate(item):
        controls = item.setdefault("controls", {"paused": False, "away": False, "pending": []})
        if action == "pause":
            controls["pause_requested"] = True
        elif action == "resume":
            controls["paused"] = False; controls["pause_requested"] = False
        elif action == "away":
            controls["away"] = bool(value)
        elif action == "interrupt":
            controls["interrupt_requested"] = True
        elif action == "recover":
            controls["recovery_requested"] = True
        elif action == "steer":
            if not str(value or "").strip():
                raise HelmError("steer requires guidance")
            controls.setdefault("pending", []).append({"at": now(), "text": str(value).strip()})
            item["reviews"] = []
            if item.get("status") in ("ready", "pr-open"):
                item["status"] = "queued"; item["phase"] = "revision"
        item.setdefault("history", []).append({"at": now(), "from": item["status"], "to": item["status"], "note": f"control: {action}"})
    return cas_update(work_id, mutate)


def inspection(item: dict, recent_output: str = "") -> dict:
    session = item.get("session") or {}
    run = (item.get("runs") or [{}])[-1]
    data = {
        "id": item["id"], "phase": item.get("phase", item.get("status")), "state": item.get("status"),
        "last_activity": item.get("updated"), "branch": item.get("branch"),
        "sha": item.get("head_sha") or (item.get("checkpoint") or {}).get("sha"),
        "changed_scope": item.get("changed_scope", []), "declared_scope": item.get("scope", {}).get("paths", []),
        "tests": item.get("verification", []), "reviews": item.get("reviews", []),
        "blockers": item.get("ask") or item.get("blockers", []), "model": item.get("model_decision") or item.get("dispatch"),
        "tokens": run.get("tokens"), "cost": run.get("cost"),
        "usage_evidence": {"available": run.get("tokens") is not None or run.get("cost") is not None,
                           "source": "Herdr agent metadata" if session else "runner ledger"},
        "session": session,
        "controls": item.get("controls", {}), "rigor": item.get("rigor"),
        "recent_output": recent_output[-12000:],
    }
    return redact(data)
