"""Durable control-plane primitives: CAS records, redaction, controls and inspection."""
from __future__ import annotations
import hashlib
import re
import secrets
import time
from pathlib import Path
from typing import Callable
from .paths import home
from .util import HelmError, locked, now, read_json, write_json
from . import ids

SCHEMA_VERSION = 3
_SECRET_WORDS = ("token", "secret", "password", "passwd", "authorization", "apikey", "credential", "cookie", "privatekey", "accesskey", "refreshkey")
_SECRET_VALUES = [
    re.compile(r"-----BEGIN [^-]*(?:PRIVATE KEY|CERTIFICATE)-----.*?-----END [^-]*(?:PRIVATE KEY|CERTIFICATE)-----", re.I | re.S),
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\b(?:sk|ghp|github_pat|xox[baprs])[-_][A-Za-z0-9_-]{8,}"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)(https?://)([^/@\s:]+):([^/@\s]+)@"),
    re.compile(r"(?i)((?:token|secret|password|passwd|api[-_]?key|authorization|credential)\s*[=:]\s*)[^\s&]+"),
    re.compile(r"(?i)([?&](?:token|secret|password|api[-_]?key|access_token)=)[^&#\s]+"),
]


def _secret_key(key: object) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
    usage_keys = {
        "tokens", "tokencount", "implementertokens", "reviewertokens", "attempttokens", "maxtokens",
        "inputtokens", "outputtokens", "cachedtokens", "tokensevidencecomplete",
        "implementertokenscomplete", "reviewertokenscomplete",
    }
    return normalized not in usage_keys and any(word in normalized for word in _SECRET_WORDS)


def item_lock(work_id: str) -> Path:
    return home() / "work" / ids.work(work_id) / "item.lock"


def cas_update(work_id: str, mutate: Callable[[dict], None], expected_revision: int | None = None) -> dict:
    """Atomically mutate one item. Optional revision makes stale writers fail closed."""
    path = home() / "work" / ids.work(work_id) / "item.json"
    with locked(item_lock(work_id)):
        item = read_json(path)
        if not item:
            raise HelmError(f"unknown work item '{work_id}'")
        if item.get("id") != ids.work(work_id):
            raise HelmError("work item identity does not match its durable state directory")
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


def request(work_id: str, action: str, value=None, request_id: str | None = None) -> dict:
    allowed = {"steer", "pause", "resume", "away", "interrupt", "recover"}
    if action not in allowed:
        raise HelmError(f"unknown control '{action}'")
    if request_id is not None and not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", request_id):
        raise HelmError("request id must be 1-128 safe identifier characters")
    event_id = request_id or f"{int(__import__('time').time_ns())}-{secrets.token_hex(4)}"
    created = [False]
    def mutate(item):
        promotion = item.get("promotion") or {}
        if promotion.get("state") in {
            "armed", "external-requested", "merge-observed", "cleanup-pending"
        }:
            raise HelmError(f"{action} refused while an exact-SHA promotion is armed; reconcile promotion first")
        pr_delivery = item.get("pr_delivery") or {}
        if pr_delivery.get("state") in {
            "armed", "push-requested", "push-confirmed", "pr-create-requested"
        }:
            raise HelmError(f"{action} refused while exact-SHA PR delivery is armed; reconcile delivery first")
        external_gate = item.get("external_gate") or {}
        if external_gate.get("state") in {
            "armed", "request-started", "command-returned", "running", "needs-decision",
            "response-armed", "response-requested", "unknown",
        }:
            raise HelmError(f"{action} refused while a no-mistakes transaction requires reconciliation")
        controls = item.setdefault("controls", {"paused": False, "away": False, "pending": []})
        existing = next((event for event in controls.get("events", []) if event.get("id") == event_id), None)
        if existing:
            expected_value = str(value).strip() if value is not None else None
            if existing.get("action") != action or existing.get("value") != expected_value:
                raise HelmError(f"control request id '{event_id}' was already used for different content")
            return
        event = {"id": event_id, "at": now(), "action": action,
                 "value": str(value).strip() if value is not None else None, "state": "pending"}
        created[0] = True
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
    result = cas_update(work_id, mutate)
    result["_control_deduplicated"] = not created[0]  # transient; never written to the item record
    return result


def consume(work_id: str, event_ids: list[str], state: str = "consumed") -> dict:
    """Acknowledge controls exactly once after they reach the worker/agent boundary."""
    wanted = set(event_ids)
    def mutate(item):
        controls = item.setdefault("controls", {})
        consumed = set()
        for event in controls.get("events", []):
            if event.get("id") in wanted and event.get("state") == "pending":
                event["state"] = state; event["consumed_at"] = now()
                consumed.add(event["id"])
        controls["pending"] = [p for p in controls.get("pending", []) if p.get("id") not in consumed]
    return cas_update(work_id, mutate)


def begin_delivery(work_id: str, event_id: str, owner: str, baseline_sequence: int,
                   *, lease_seconds: int = 60) -> dict:
    """CAS-reserve one external steering side effect, with crash reconciliation data."""
    acquired = [False]
    epoch = time.time()
    def mutate(item):
        controls = item.setdefault("controls", {})
        event = next((value for value in controls.get("events", []) if value.get("id") == event_id), None)
        if not event or event.get("action") != "steer":
            raise HelmError(f"unknown steering event '{event_id}'")
        if event.get("state") in ("delivered", "consumed"):
            return
        unresolved = [value for value in controls.get("events", [])
                      if value.get("action") == "steer" and value.get("state") in ("pending", "delivering")]
        # One Pi session has one ordered input stream. Only the oldest
        # unresolved control may reserve it; otherwise two identical controls
        # can both correlate to one hash/sequence receipt.
        if not unresolved or unresolved[0] is not event:
            return
        if any(value is not event and value.get("state") == "delivering" for value in unresolved):
            return
        until = float(event.get("delivery_until_epoch") or 0)
        if event.get("state") == "delivering" and until > epoch:
            return
        event.update(state="delivering", delivery_owner=owner,
                     delivery_until_epoch=epoch + max(1, lease_seconds),
                     delivery_started_at=event.get("delivery_started_at") or now(),
                     delivery_baseline_sequence=int(event.get("delivery_baseline_sequence", baseline_sequence)),
                     value_sha256=hashlib.sha256(str(event.get("value") or "").encode()).hexdigest())
        acquired[0] = True
    result = cas_update(work_id, mutate)
    result["_delivery_acquired"] = acquired[0]
    return result


def complete_delivery(work_id: str, event_id: str, owner: str, input_sequence: int) -> dict:
    def mutate(item):
        controls = item.setdefault("controls", {})
        event = next((value for value in controls.get("events", []) if value.get("id") == event_id), None)
        if not event or event.get("state") != "delivering" or event.get("delivery_owner") != owner:
            raise HelmError("steering delivery ownership changed before acknowledgement")
        event.update(state="delivered", delivered_at=now(), input_sequence=int(input_sequence))
        event.pop("delivery_until_epoch", None)
        controls["pending"] = [value for value in controls.get("pending", []) if value.get("id") != event_id]
    return cas_update(work_id, mutate)


def abandon_delivery(work_id: str, event_id: str, owner: str) -> dict:
    """Release only an unacknowledged reservation; accepted Pi input is reconciled on retry."""
    def mutate(item):
        event = next((value for value in item.setdefault("controls", {}).get("events", [])
                      if value.get("id") == event_id), None)
        if event and event.get("state") == "delivering" and event.get("delivery_owner") == owner:
            event.update(state="pending", delivery_released_at=now())
            event.pop("delivery_owner", None); event.pop("delivery_until_epoch", None)
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
    usage = item.get("usage") or {}
    data = {
        "id": item["id"], "phase": item.get("phase", item.get("status")), "state": item.get("status"),
        "last_activity": item.get("updated"), "branch": item.get("branch"),
        "sha": item.get("head_sha") or (item.get("checkpoint") or {}).get("sha"),
        "changed_scope": item.get("changed_scope", []), "declared_scope": item.get("scope", {}).get("paths", []),
        "tests": tests, "reviews": reviews,
        "blockers": item.get("ask") or item.get("blockers", []), "model": item.get("model_decision") or item.get("dispatch"),
        "tokens": usage.get("tokens", run.get("tokens")), "cost": usage.get("cost", run.get("cost")),
        "usage_evidence": {"available": usage.get("tokens") is not None or usage.get("cost") is not None,
                           "source": "Herdr agent metadata" if session else "runner ledger"},
        "activity": item.get("activity", {}), "budgets": item.get("budgets", {}), "usage": usage,
        "session": session,
        "controls": item.get("controls", {}), "rigor": item.get("rigor"),
        "recent_output": recent_output[-12000:],
    }
    return redact(data)
