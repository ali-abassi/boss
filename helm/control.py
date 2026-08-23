"""Durable control-plane primitives: CAS records, redaction, controls and inspection."""
from __future__ import annotations
import re
from pathlib import Path
from typing import Callable
from .paths import home
from .util import HelmError, locked, now, read_json, write_json

SCHEMA_VERSION = 3
_SECRET_WORDS = ("token", "secret", "password", "passwd", "authorization", "apikey", "credential", "cookie", "privatekey", "accesskey", "refreshkey")
_SECRET_VALUES = [
    re.compile(r"-----BEGIN [^-]*(?:PRIVATE KEY|CERTIFICATE)-----.*?-----END [^-]*(?:PRIVATE KEY|CERTIFICATE)-----", re.I | re.S),
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\b(?:sk|ghp|github_pat|xox[baprs])[-_][A-Za-z0-9_-]{8,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)(https?://)([^/@\s:]+):([^/@\s]+)@"),
    re.compile(r"(?i)((?:token|secret|password|passwd|api[-_]?key|authorization|credential)\s*[=:]\s*)[^\s&]+"),
    re.compile(r"(?i)([?&](?:token|secret|password|api[-_]?key|access_token)=)[^&#\s]+"),
]


def _secret_key(key: object) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
    return normalized not in ("tokens", "tokencount") and any(word in normalized for word in _SECRET_WORDS)


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
        return {k: ("[REDACTED]" if _secret_key(k) else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if not isinstance(value, str):
        return value
    out = value
    for pattern in _SECRET_VALUES:
        if pattern.pattern.startswith("(?i)(https?"):
            out = pattern.sub(lambda m: m.group(1) + "[REDACTED]@", out)
        else:
            out = pattern.sub(lambda m: (m.group(1) if m.lastindex else "") + "[REDACTED]", out)
    return out


def request(work_id: str, action: str, value=None) -> dict:
    allowed = {"steer", "pause", "resume", "away", "interrupt", "recover"}
    if action not in allowed:
        raise HelmError(f"unknown control '{action}'")
    def mutate(item):
        controls = item.setdefault("controls", {"paused": False, "away": False, "pending": []})
        event = {"id": f"{int(__import__('time').time_ns())}", "at": now(), "action": action,
                 "value": str(value).strip() if value is not None else None, "state": "pending"}
        controls.setdefault("events", []).append(event)
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
            controls.setdefault("pending", []).append({"id": event["id"], "at": event["at"], "text": str(value).strip()})
            item["reviews"] = []
            if item.get("status") in ("ready", "pr-open"):
                item["status"] = "queued"; item["phase"] = "revision"
        item.setdefault("history", []).append({"at": now(), "from": item["status"], "to": item["status"], "note": f"control: {action}"})
    return cas_update(work_id, mutate)


def consume(work_id: str, event_ids: list[str], state: str = "consumed") -> dict:
    """Acknowledge controls exactly once after they reach the worker/agent boundary."""
    wanted = set(event_ids)
    def mutate(item):
        controls = item.setdefault("controls", {})
        controls["pending"] = [p for p in controls.get("pending", []) if p.get("id") not in wanted]
        for event in controls.get("events", []):
            if event.get("id") in wanted and event.get("state") == "pending":
                event["state"] = state; event["consumed_at"] = now()
    return cas_update(work_id, mutate)


def _allow(record: dict, keys: tuple[str, ...]) -> dict:
    return {key: record[key] for key in keys if key in record}


def inspection(item: dict, recent_output: str = "") -> dict:
    raw_session = item.get("session") or {}
    session = _allow(raw_session, ("agent_name", "agent_session_id", "pane_id", "tab_id", "workspace_id",
                                   "agent_status", "status", "model", "thinking", "resolved_model",
                                   "resolved_thinking", "created", "reconnected", "liveness_validated_at"))
    reviews = []
    for review in item.get("reviews", []):
        safe = _allow(review, ("verdict", "notes", "sha", "role", "fresh", "valid", "at", "invalidated_at"))
        reviewer = review.get("reviewer") or {}
        safe["reviewer"] = _allow(reviewer, ("kind", "identity", "agent_name", "agent_session_id"))
        reviews.append(safe)
    tests = [_allow(test, ("at", "ok", "base_sha", "head_sha", "complete", "run_dir"))
             for test in item.get("verification", [])]
    run = (item.get("runs") or [{}])[-1]
    data = {
        "id": item["id"], "phase": item.get("phase", item.get("status")), "state": item.get("status"),
        "last_activity": item.get("updated"), "branch": item.get("branch"),
        "sha": item.get("head_sha") or (item.get("checkpoint") or {}).get("sha"),
        "changed_scope": item.get("changed_scope", []), "declared_scope": item.get("scope", {}).get("paths", []),
        "tests": tests, "reviews": reviews,
        "blockers": item.get("ask") or item.get("blockers", []), "model": item.get("model_decision") or item.get("dispatch"),
        "tokens": run.get("tokens"), "cost": run.get("cost"),
        "usage_evidence": {"available": run.get("tokens") is not None or run.get("cost") is not None,
                           "source": "Herdr agent metadata" if session else "runner ledger"},
        "activity": item.get("activity", {}), "budgets": item.get("budgets", {}),
        "session": session,
        "controls": item.get("controls", {}), "rigor": item.get("rigor"),
        "recent_output": recent_output[-12000:],
    }
    return redact(data)
