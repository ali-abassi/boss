"""Deterministic, advisory-only portfolio planning ledger.

Planning reads canonical work/supervisor records but mutates only planning.json.
The model boundary lives in the Pi extension; this module owns frozen evidence,
cadence, and crash-safe delivery receipts.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
import time
from typing import Any

from . import control, supervisor, work
from .paths import planning_file, planning_lock, supervisor_lock
from .util import BossError, locked, now, write_json

VERSION = 1
MAX_EVENTS = 16
MAX_ITEMS = 512
MAX_WAKES = 128
MAX_SNAPSHOT_BYTES = 262_144
MAX_STATE_BYTES = 5_000_000
MAX_STRING_BYTES = 262_144
MAX_SOURCE_STRING_BYTES = 20_000
MAX_ITEM_FINGERPRINTS = MAX_ITEMS
CLAIM_SECONDS = 120
ACTIVE_SECONDS = 15 * 60
QUIET_SECONDS = 60 * 60
MAX_CADENCE_SECONDS = 6 * 60 * 60
ERROR_BASE_SECONDS = 5 * 60
ERROR_MAX_SECONDS = 60 * 60
CAPTURE_ATTEMPTS = 3
TERMINAL_STATES = {"delivered", "rejected", "cancelled", "deferred", "superseded"}
SAFE_STATES = {"pending", "claimed"}
EVENT_STATES = SAFE_STATES | {"generating"} | TERMINAL_STATES
ATTENTION_STATUSES = {"needs-you", "failed", "ready", "pr-open"}
ACTIVE_STATUSES = {"queued", "running"} | ATTENTION_STATUSES | {"paused"}
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_EVENT_ID = re.compile(r"^pulse-[0-9]{6,}$")
_MODEL_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+@-]{0,255}$")
_USAGE_KEYS = {"input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "total_tokens"}
_ITEM_KEYS = {"source_id", "id", "project", "status", "kind", "phase", "text", "labels",
              "attempts", "max_attempts", "branch", "head_sha", "pr_url", "scope", "ask",
              "latest_failure", "latest_run"}
_RUN_KEYS = {"source_id", "attempt", "ok", "failed_ids", "control", "budget_exceeded",
             "agent_checkpoint", "sha"}
_WAKE_KEYS = {"source_id", "id", "item_id", "classification", "reason"}

_STATE_KEYS = {
    "version", "enabled", "next_id", "next_due", "interval_seconds",
    "unchanged_count", "generation_errors", "last_error", "last_fingerprint",
    "last_item_fingerprints", "last_items", "events",
}
_EVENT_KEYS = {
    "id", "state", "fingerprint", "snapshot", "recap", "created_at",
    "claimed_by", "claim_until", "claimed_at", "generating_at",
    "terminal_at", "reason", "delivery_receipt", "reconciled", "reconciliation_receipt",
}


def _default_state() -> dict:
    return {
        "version": VERSION,
        "enabled": False,
        "next_id": 1,
        "next_due": None,
        "interval_seconds": ACTIVE_SECONDS,
        "unchanged_count": 0,
        "generation_errors": 0,
        "last_error": None,
        "last_fingerprint": None,
        "last_item_fingerprints": {},
        "last_items": [],
        "events": [],
    }


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def _iso(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError):
        return None


def _bounded_tree(value: Any, *, where: str, max_string_bytes: int = MAX_STRING_BYTES) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return None
    if isinstance(value, float):
        return None if math.isfinite(value) else f"{where} contains a non-finite number"
    if isinstance(value, str):
        if len(value.encode("utf-8")) > max_string_bytes:
            return f"{where} contains a string larger than {max_string_bytes} bytes"
        return None
    if isinstance(value, list):
        for index, child in enumerate(value):
            if (error := _bounded_tree(child, where=f"{where}[{index}]", max_string_bytes=max_string_bytes)):
                return error
        return None
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or not key:
                return f"{where} has a non-string or empty key"
            if (error := _bounded_tree(key, where=f"{where} key", max_string_bytes=max_string_bytes)):
                return error
            if (error := _bounded_tree(child, where=f"{where}.{key}", max_string_bytes=max_string_bytes)):
                return error
        return None
    return f"{where} contains unsupported {type(value).__name__} data"


def _validate_snapshot(snapshot: Any) -> str | None:
    if not isinstance(snapshot, dict) or set(snapshot) != {
            "version", "captured_at", "items", "removed_items", "wakes", "changed_item_ids"}:
        return "snapshot schema is invalid"
    if snapshot.get("version") != VERSION or _epoch(snapshot.get("captured_at")) is None:
        return "snapshot version or capture time is invalid"
    items, removed = snapshot.get("items"), snapshot.get("removed_items")
    wakes, changed = snapshot.get("wakes"), snapshot.get("changed_item_ids")
    if not isinstance(items, list) or len(items) > MAX_ITEMS:
        return "snapshot items exceed the bounded limit"
    if not isinstance(removed, list) or len(removed) > MAX_ITEMS:
        return "snapshot removed items exceed the bounded limit"
    if not isinstance(wakes, list) or len(wakes) > MAX_WAKES:
        return "snapshot wakes exceed the bounded limit"
    if not isinstance(changed, list) or len(changed) > MAX_ITEMS or any(not isinstance(v, str) for v in changed):
        return "snapshot changed-item list is invalid"
    source_ids: set[str] = set()
    item_ids: set[str] = set()
    current_item_ids = {item.get("id") for item in items if isinstance(item, dict)}
    for item in items + removed:
        if (not isinstance(item, dict) or set(item) != _ITEM_KEYS
                or not isinstance(item.get("id"), str) or not item["id"]):
            return "snapshot item projection is malformed"
        if item.get("source_id") != f"item:{item['id']}" or item["id"] in item_ids:
            return "snapshot item source identity is invalid or duplicated"
        item_ids.add(item["id"]); source_ids.add(item["source_id"])
        if not isinstance(item.get("labels"), list) or any(not isinstance(label, str) for label in item["labels"]):
            return "snapshot item labels are malformed"
        run = item.get("latest_run")
        if run is not None:
            if (not isinstance(run, dict) or not set(run).issubset(_RUN_KEYS)
                    or {"source_id", "attempt"} - set(run)
                    or not isinstance(run.get("attempt"), int) or isinstance(run.get("attempt"), bool)
                    or run["attempt"] < 0):
                return "snapshot run projection is malformed"
            if run.get("source_id") != f"run:{item['id']}:{run['attempt']}":
                return "snapshot run source identity is invalid"
            if run["source_id"] in source_ids:
                return "snapshot source identity is duplicated"
            source_ids.add(run["source_id"])
    for wake in wakes:
        if not isinstance(wake, dict) or set(wake) != _WAKE_KEYS or not isinstance(wake.get("id"), str):
            return "snapshot wake projection is malformed"
        if wake.get("source_id") != f"event:{wake['id']}" or wake["source_id"] in source_ids:
            return "snapshot wake source identity is invalid or duplicated"
        source_ids.add(wake["source_id"])
    if any(value not in item_ids for value in changed) or changed != sorted(set(changed)):
        return "snapshot changed-item identities are invalid"
    if any(item["id"] in current_item_ids for item in removed):
        return "snapshot removed item is still current"
    if (error := _bounded_tree(snapshot, where="snapshot")):
        return error
    try:
        if len(_canonical(snapshot)) > MAX_SNAPSHOT_BYTES:
            return f"snapshot exceeds {MAX_SNAPSHOT_BYTES} bytes"
    except (TypeError, ValueError, UnicodeError):
        return "snapshot is not canonical JSON"
    return None


def _validate_delivery_receipt(value: Any) -> str | None:
    if not isinstance(value, dict) or set(value) != {"provider", "model", "response_sha256", "usage"}:
        return "delivery receipt schema is invalid"
    if (not isinstance(value.get("provider"), str) or not _MODEL_PART.fullmatch(value["provider"])
            or not isinstance(value.get("model"), str) or not _MODEL_PART.fullmatch(value["model"])):
        return "delivery receipt model/provider is invalid"
    if not isinstance(value.get("response_sha256"), str) or not _HEX64.fullmatch(value["response_sha256"]):
        return "delivery receipt response digest is invalid"
    usage = value.get("usage")
    if not isinstance(usage, dict) or set(usage) != _USAGE_KEYS:
        return "delivery receipt usage schema is invalid"
    if any(not isinstance(usage[key], int) or isinstance(usage[key], bool)
           or not 0 <= usage[key] <= 1_000_000_000_000 for key in _USAGE_KEYS):
        return "delivery receipt usage values must be non-negative integers"
    if usage["total_tokens"] < usage["input_tokens"] + usage["output_tokens"]:
        return "delivery receipt total_tokens is inconsistent"
    return None


def delivery_receipt(provider: str, model: str, response_sha256: str, usage: dict) -> dict:
    value = {"provider": provider, "model": model,
             "response_sha256": response_sha256, "usage": usage}
    if (error := _validate_delivery_receipt(value)):
        raise BossError(error)
    return value


def _validate_reconciliation_receipt(value: Any, expected_outcome: str) -> str | None:
    if not isinstance(value, dict) or set(value) != {"confirmed_outcome", "reason", "confirmed_at"}:
        return "operator reconciliation receipt schema is invalid"
    if value.get("confirmed_outcome") != expected_outcome:
        return "operator reconciliation outcome does not match event state"
    if (not isinstance(value.get("reason"), str) or not value["reason"].strip()
            or len(value["reason"].encode("utf-8")) > 2000):
        return "operator reconciliation reason is invalid"
    if _epoch(value.get("confirmed_at")) is None:
        return "operator reconciliation timestamp is invalid"
    return None


def validate_state(value: Any) -> str | None:
    if not isinstance(value, dict) or set(value) != _STATE_KEYS or value.get("version") != VERSION:
        return "unsupported planning state version/schema"
    if not isinstance(value.get("enabled"), bool):
        return "planning enabled flag is invalid"
    if (not isinstance(value.get("next_id"), int) or isinstance(value.get("next_id"), bool)
            or not 1 <= value["next_id"] <= 1_000_000_000):
        return "planning next_id is invalid"
    if value.get("next_due") is not None and _epoch(value.get("next_due")) is None:
        return "planning next_due is invalid"
    interval = value.get("interval_seconds")
    if not isinstance(interval, int) or isinstance(interval, bool) or not ACTIVE_SECONDS <= interval <= MAX_CADENCE_SECONDS:
        return "planning cadence is out of bounds"
    for field in ("unchanged_count", "generation_errors"):
        if (not isinstance(value.get(field), int) or isinstance(value.get(field), bool)
                or not 0 <= value[field] <= 1_000_000):
            return f"planning {field} is invalid"
    if value.get("last_error") is not None and not isinstance(value.get("last_error"), str):
        return "planning last_error is invalid"
    fingerprint = value.get("last_fingerprint")
    if fingerprint is not None and (not isinstance(fingerprint, str) or not _HEX64.fullmatch(fingerprint)):
        return "planning last_fingerprint is invalid"
    item_fps = value.get("last_item_fingerprints")
    if not isinstance(item_fps, dict) or len(item_fps) > MAX_ITEM_FINGERPRINTS:
        return "planning item fingerprints exceed the bounded limit"
    if any(not isinstance(key, str) or not _HEX64.fullmatch(val or "") for key, val in item_fps.items()):
        return "planning item fingerprints are malformed"
    last_items = value.get("last_items")
    if not isinstance(last_items, list) or len(last_items) > MAX_ITEMS:
        return "planning retained item projections exceed the bounded limit"
    retained_ids = set()
    for item in last_items:
        if (not isinstance(item, dict) or not isinstance(item.get("id"), str)
                or item.get("source_id") != f"item:{item.get('id')}" or item["id"] in retained_ids):
            return "planning retained item projection is malformed"
        retained_ids.add(item["id"])
    if retained_ids != set(item_fps):
        return "planning retained items do not match item fingerprints"
    events = value.get("events")
    if not isinstance(events, list) or len(events) > MAX_EVENTS:
        return "planning event ledger exceeds the bounded limit"
    seen: set[str] = set()
    for event in events:
        if not isinstance(event, dict) or not set(event).issubset(_EVENT_KEYS):
            return "planning event schema is invalid"
        required = {"id", "state", "fingerprint", "snapshot", "recap", "created_at"}
        if not required.issubset(event):
            return "planning event is missing required fields"
        event_id, state = event.get("id"), event.get("state")
        if not isinstance(event_id, str) or not _EVENT_ID.fullmatch(event_id) or event_id in seen:
            return "planning event identity is invalid or duplicated"
        seen.add(event_id)
        if state not in EVENT_STATES or not _HEX64.fullmatch(str(event.get("fingerprint", ""))):
            return "planning event state or fingerprint is invalid"
        if _epoch(event.get("created_at")) is None or not isinstance(event.get("recap"), str):
            return "planning event receipt metadata is invalid"
        if (error := _validate_snapshot(event.get("snapshot"))):
            return f"planning event {event_id}: {error}"
        expected_fingerprint = hashlib.sha256(_canonical({
            "items": event["snapshot"]["items"], "wakes": event["snapshot"]["wakes"]})).hexdigest()
        if event["fingerprint"] != expected_fingerprint:
            return f"planning event {event_id} fingerprint does not match its frozen evidence"
        if state in {"claimed", "generating"} and not isinstance(event.get("claimed_by"), str):
            return f"planning event {event_id} has no consumer receipt"
        if state == "claimed" and _epoch(event.get("claim_until")) is None:
            return f"planning event {event_id} has no valid claim lease"
        if state in TERMINAL_STATES and _epoch(event.get("terminal_at")) is None:
            return f"planning event {event_id} has no terminal receipt"
        reconciliation = event.get("reconciliation_receipt")
        if event.get("reconciled") is not None and event.get("reconciled") is not True:
            return f"planning event {event_id} has an invalid reconciled marker"
        if reconciliation is not None:
            if event.get("reconciled") is not True or state not in {"delivered", "rejected"}:
                return f"planning event {event_id} has reconciliation evidence in an invalid state"
            if (error := _validate_reconciliation_receipt(reconciliation, state)):
                return f"planning event {event_id}: {error}"
        elif event.get("reconciled") is not None:
            return f"planning event {event_id} has no operator reconciliation receipt"
        delivery = event.get("delivery_receipt")
        if state == "delivered":
            if delivery is not None and reconciliation is not None:
                return f"planning event {event_id} has conflicting delivery and reconciliation receipts"
            if delivery is None and reconciliation is None:
                return f"planning event {event_id}: delivered state has no auditable receipt"
            if delivery is not None and (error := _validate_delivery_receipt(delivery)):
                return f"planning event {event_id}: {error}"
        elif delivery is not None:
            return f"planning event {event_id} has a receipt before delivery"
    if seen and value["next_id"] <= max(int(event_id.split("-")[1]) for event_id in seen):
        return "planning next_id must be greater than every event identity"
    if (error := _bounded_tree(value, where="planning state")):
        return error
    try:
        if len(_canonical(value)) > MAX_STATE_BYTES:
            return f"planning state exceeds {MAX_STATE_BYTES} bytes"
    except (TypeError, ValueError, UnicodeError):
        return "planning state is not canonical JSON"
    return None


def _read(*, missing_ok: bool = True) -> dict:
    path = planning_file()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        if missing_ok:
            return _default_state()
        raise BossError("planning is not initialized")
    except OSError as exc:
        raise BossError(f"planning state is unreadable: {type(exc).__name__}; run `pi-boss doctor`")
    if len(raw) > MAX_STATE_BYTES:
        raise BossError(f"planning state exceeds {MAX_STATE_BYTES} bytes; run `pi-boss doctor`")
    try:
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BossError(f"planning state is unreadable: {type(exc).__name__}; run `pi-boss doctor`")
    if (error := validate_state(value)):
        raise BossError(f"planning state is malformed: {error}; run `pi-boss doctor`")
    return value


def _save(state: dict) -> None:
    if (error := validate_state(state)):
        raise BossError(f"refusing malformed planning state: {error}")
    write_json(planning_file(), state)


def _summary(state: dict, *, initialized: bool) -> dict:
    counts = {name: 0 for name in sorted(EVENT_STATES)}
    for event in state["events"]:
        counts[event["state"]] += 1
    generating = [event["id"] for event in state["events"] if event["state"] == "generating"]
    return {
        "healthy": True,
        "initialized": initialized,
        "enabled": state["enabled"],
        "next_due": state["next_due"],
        "interval_seconds": state["interval_seconds"],
        "last_fingerprint": state["last_fingerprint"],
        "last_error": state["last_error"],
        "generation_errors": state["generation_errors"],
        "events": counts,
        "generating": generating,
    }


def summary() -> dict:
    """Read-only status. Missing state is healthy/off and creates no path."""
    initialized = planning_file().exists()
    try:
        return _summary(_read(), initialized=initialized)
    except BossError as exc:
        return {"healthy": False, "initialized": initialized, "enabled": None,
                "error": exc.msg, "generating": None}


def status() -> dict:
    return _summary(_read(), initialized=planning_file().exists())


def enable(*, at: float | None = None) -> dict:
    epoch = time.time() if at is None else at
    with locked(planning_lock()):
        state = _read(); state["enabled"] = True
        state["next_due"] = _iso(epoch)
        _save(state)
        return _summary(state, initialized=True)


def disable(*, reason: str = "planning disabled", at: float | None = None) -> dict:
    if not planning_file().exists():
        return _summary(_default_state(), initialized=False)
    epoch = time.time() if at is None else at
    with locked(planning_lock()):
        state = _read(); state["enabled"] = False; state["next_due"] = None
        cancelled = []
        for event in state["events"]:
            if event["state"] in SAFE_STATES:
                event.update(state="cancelled", terminal_at=_iso(epoch), reason=control.redact(reason))
                event.pop("claim_until", None); cancelled.append(event["id"])
                if state["last_fingerprint"] == event["fingerprint"]:
                    state["last_fingerprint"] = None
        _save(state)
        result = _summary(state, initialized=True)
        result.update(cancelled=cancelled)
        return result


def _small_dict(value: Any, keys: tuple[str, ...]) -> dict | None:
    if not isinstance(value, dict):
        return None
    projected = {key: value.get(key) for key in keys if value.get(key) is not None}
    return control.redact(projected) or None


def _item_projection(item: dict) -> dict:
    item_id = str(item.get("id", ""))
    if not item_id:
        raise BossError("planning capture found an item without a stable identity")
    failure_notes = item.get("failure_notes") or []
    if not isinstance(failure_notes, list):
        raise BossError(f"planning capture found malformed failure evidence for {item_id}")
    runs = item.get("runs") or []
    if not isinstance(runs, list):
        raise BossError(f"planning capture found malformed run evidence for {item_id}")
    latest_run = None
    if runs:
        run = runs[-1]
        if not isinstance(run, dict) or not isinstance(run.get("attempt"), int):
            raise BossError(f"planning capture found a malformed latest run for {item_id}")
        latest_run = {
            "source_id": f"run:{item_id}:{run['attempt']}",
            "attempt": run["attempt"],
            **(_small_dict(run, ("ok", "failed_ids", "control", "budget_exceeded", "agent_checkpoint", "sha")) or {}),
        }
    projection = {
        "source_id": f"item:{item_id}", "id": item_id,
        "project": item.get("project"), "status": item.get("status"),
        "kind": item.get("kind"), "phase": item.get("phase"),
        "text": item.get("text"), "labels": sorted(item.get("labels") or []),
        "attempts": item.get("attempts"), "max_attempts": item.get("max_attempts"),
        "branch": item.get("branch"), "head_sha": item.get("head_sha"),
        "pr_url": item.get("pr_url"), "scope": _small_dict(item.get("scope"), ("paths", "claim")),
        "ask": _small_dict(item.get("ask"), ("question", "context", "options")),
        "latest_failure": _small_dict(failure_notes[-1], ("attempt", "notes", "signature")) if failure_notes else None,
        "latest_run": latest_run,
    }
    return control.redact(projection)


def _wake_projection(event: dict) -> dict:
    event_id = str(event.get("id", ""))
    if not event_id:
        raise BossError("planning capture found a wake without a stable identity")
    return control.redact({
        "source_id": f"event:{event_id}", "id": event_id,
        "item_id": event.get("item_id"), "classification": event.get("classification"),
        "reason": event.get("reason"),
    })


def _capture_semantic() -> dict:
    items = work.all_items(); wakes = supervisor.pending()
    if len(items) > MAX_ITEMS:
        raise BossError(f"planning capture has {len(items)} items; maximum is {MAX_ITEMS}")
    if len(wakes) > MAX_WAKES:
        raise BossError(f"planning capture has {len(wakes)} wakes; maximum is {MAX_WAKES}")
    projected_items = sorted((_item_projection(value) for value in items), key=lambda value: value["id"])
    projected_wakes = sorted((_wake_projection(value) for value in wakes), key=lambda value: value["id"])
    semantic = {"items": projected_items, "wakes": projected_wakes}
    if (error := _bounded_tree(semantic, where="portfolio snapshot",
                               max_string_bytes=MAX_SOURCE_STRING_BYTES)):
        raise BossError(error)
    # Reserve room for capture metadata and changed-item identities.
    if len(_canonical(semantic)) > MAX_SNAPSHOT_BYTES - 4096:
        raise BossError(f"planning snapshot exceeds {MAX_SNAPSHOT_BYTES} bytes")
    return semantic


def capture_snapshot(previous_item_fingerprints: dict[str, str] | None = None,
                     previous_items: list[dict] | None = None,
                     *, captured_at: str | None = None) -> dict:
    """Read stable canonical sources twice per attempt; never mutate those sources."""
    stable = None
    for _ in range(CAPTURE_ATTEMPTS):
        first = _capture_semantic(); second = _capture_semantic()
        if _canonical(first) == _canonical(second):
            stable = second; break
    if stable is None:
        raise BossError(f"planning sources changed during {CAPTURE_ATTEMPTS} bounded capture attempts")
    prior = previous_item_fingerprints or {}
    retained = {item["id"]: item for item in (previous_items or [])}
    item_fps = {item["id"]: hashlib.sha256(_canonical(item)).hexdigest() for item in stable["items"]}
    removed_ids = sorted(set(prior) - set(item_fps))
    if any(item_id not in retained for item_id in removed_ids):
        raise BossError("planning cannot account for a removed item without its retained projection")
    changed = sorted({item_id for item_id, digest in item_fps.items() if prior.get(item_id) != digest}
                     | set(removed_ids))
    snapshot = {"version": VERSION, "captured_at": captured_at or now(),
                "items": stable["items"], "removed_items": [retained[item_id] for item_id in removed_ids],
                "wakes": stable["wakes"], "changed_item_ids": changed}
    if (error := _validate_snapshot(snapshot)):
        raise BossError(f"planning snapshot rejected: {error}")
    return snapshot


def semantic_fingerprint(snapshot: dict) -> str:
    if (error := _validate_snapshot(snapshot)):
        raise BossError(f"cannot fingerprint malformed planning snapshot: {error}")
    semantic = {"items": snapshot["items"], "wakes": snapshot["wakes"]}
    return hashlib.sha256(_canonical(semantic)).hexdigest()


def item_fingerprints(snapshot: dict) -> dict[str, str]:
    return {item["id"]: hashlib.sha256(_canonical(item)).hexdigest() for item in snapshot["items"]}


def source_ids(snapshot: dict) -> list[str]:
    values = []
    for item in snapshot["items"] + snapshot["removed_items"]:
        values.append(item["source_id"])
        if item.get("latest_run"):
            values.append(item["latest_run"]["source_id"])
    values.extend(event["source_id"] for event in snapshot["wakes"])
    return sorted(values)


def build_recap(snapshot: dict) -> str:
    """Build a byte-stable three-tier historical recap from only frozen evidence."""
    if (error := _validate_snapshot(snapshot)):
        raise BossError(f"cannot recap malformed planning snapshot: {error}")
    items = snapshot["items"]; removed_items = snapshot["removed_items"]
    changed = set(snapshot["changed_item_ids"])
    counts: dict[str, int] = {}
    for item in items:
        status_name = str(item.get("status") or "unknown")
        counts[status_name] = counts.get(status_name, 0) + 1
    attention = sum(item.get("status") in ATTENTION_STATUSES for item in items) + len(snapshot["wakes"])
    count_text = ", ".join(f"{key}={counts[key]}" for key in sorted(counts)) or "none"
    lines = [
        "ADVISORY — NO ACTION TAKEN",
        "Historical recap only; canonical current BOSS state always wins.",
        f"Captured: {snapshot['captured_at']}",
        "",
        "Tier 0 — portfolio",
        f"Items: {len(items)} ({count_text}) · changed={len(changed)} · attention={attention} · wakes={len(snapshot['wakes'])}",
        "",
        "Tier 1 — project groups",
    ]
    categories = (
        ("decisions", {"needs-you"}), ("failures", {"failed"}),
        ("ready", {"ready", "pr-open"}), ("active", {"queued", "running"}),
        ("paused", {"paused"}),
    )
    projects = sorted({str(item.get("project") or "(unknown)") for item in items})
    if not projects:
        lines.append("- no projects represented")
    for project in projects:
        project_items = [item for item in items if str(item.get("project") or "(unknown)") == project]
        chunks = []
        for label, statuses in categories:
            ids = [item["source_id"] for item in project_items if item.get("status") in statuses]
            if ids: chunks.append(f"{label}: {', '.join(ids)}")
        lines.append(f"- {project}: " + (" · ".join(chunks) if chunks else "no open operational category"))
    lines += ["", "Tier 2 — open or changed items"]
    open_statuses = set(work.OPEN)
    rows = [item for item in items if item.get("status") in open_statuses or item["id"] in changed]
    if not rows:
        lines.append("- none")
    for item in rows:
        marker = "changed" if item["id"] in changed else "current"
        text = " ".join(str(item.get("text") or "(no request text)").split())
        line = (f"- [{item['source_id']}] {item.get('project') or '(unknown)'} · "
                f"{item.get('status') or 'unknown'} · {marker} · {text}")
        if item.get("latest_run"):
            line += f" · [{item['latest_run']['source_id']}]"
        lines.append(line)
    for item in removed_items:
        text = " ".join(str(item.get("text") or "(no request text)").split())
        line = (f"- [{item['source_id']}] {item.get('project') or '(unknown)'} · removed · changed · {text}")
        if item.get("latest_run"):
            line += f" · [{item['latest_run']['source_id']}]"
        lines.append(line)
    for wake in snapshot["wakes"]:
        reason = " ".join(str(wake.get("reason") or "(no reason)").split())
        lines.append(f"- [{wake['source_id']}] wake · {wake.get('classification') or 'unknown'} · {reason}")
    lines += ["", "Valid source IDs: " + (", ".join(source_ids(snapshot)) or "none")]
    return "\n".join(lines)


def _has_active(snapshot: dict) -> bool:
    return bool(snapshot["wakes"] or any(item.get("status") in ACTIVE_STATUSES for item in snapshot["items"]))


def _prune_for_append(state: dict) -> None:
    while len(state["events"]) >= MAX_EVENTS:
        index = next((i for i, event in enumerate(state["events"]) if event["state"] in TERMINAL_STATES), None)
        if index is None:
            raise BossError(f"planning ledger contains {MAX_EVENTS} nonterminal receipts; reconcile before another pulse")
        state["events"].pop(index)


def _error_delay(error_count: int) -> int:
    return min(ERROR_MAX_SECONDS, ERROR_BASE_SECONDS * (2 ** min(max(error_count - 1, 0), 20)))


def _record_capture_error(message: str, *, epoch: float, force: bool) -> None:
    with locked(planning_lock()):
        state = _read()
        if not state["enabled"] and not force:
            return
        state["generation_errors"] += 1
        delay = _error_delay(state["generation_errors"])
        state["last_error"] = control.redact(message)
        state["next_due"] = _iso(epoch + delay) if state["enabled"] else None
        _save(state)


def tick(*, force: bool = False, at: float | None = None) -> dict:
    """Capture one due semantic change. This never claims or begins generation."""
    epoch = time.time() if at is None else at
    away = supervisor.away_status()
    if not away.get("healthy"):
        raise BossError("planning tick cannot prove away/supervisor state")
    if away.get("enabled"):
        return {"created": False, "reason": "away", "planning": summary()}
    if away.get("pending_wakes"):
        return {"created": False, "reason": "normal-wake-priority", "planning": summary()}
    with locked(planning_lock()):
        state = _read()
        if not state["enabled"] and not force:
            return {"created": False, "reason": "disabled", "planning": _summary(state, initialized=planning_file().exists())}
        due = _epoch(state["next_due"])
        if not force and due is not None and due > epoch:
            return {"created": False, "reason": "not-due", "planning": _summary(state, initialized=True)}
        prior_fps = dict(state["last_item_fingerprints"])
        prior_items = list(state["last_items"])
    try:
        snapshot = capture_snapshot(prior_fps, prior_items, captured_at=_iso(epoch))
        fingerprint = semantic_fingerprint(snapshot); recap = build_recap(snapshot)
    except (BossError, OSError, TypeError, ValueError, UnicodeError) as exc:
        message = getattr(exc, "msg", None) or str(exc)
        _record_capture_error(message, epoch=epoch, force=force)
        raise BossError(f"planning capture failed closed: {message}")
    with locked(planning_lock()):
        state = _read()
        if not state["enabled"] and not force:
            return {"created": False, "reason": "disabled-during-capture", "planning": _summary(state, initialized=True)}
        due = _epoch(state["next_due"])
        if not force and due is not None and due > epoch:
            return {"created": False, "reason": "raced-not-due", "planning": _summary(state, initialized=True)}
        if state["last_fingerprint"] == fingerprint or any(
                event["fingerprint"] == fingerprint and event["state"] in {"pending", "claimed", "generating", "delivered"}
                for event in state["events"]):
            state["unchanged_count"] += 1
            base = ACTIVE_SECONDS if _has_active(snapshot) else QUIET_SECONDS
            state["interval_seconds"] = min(MAX_CADENCE_SECONDS,
                                             max(base, state["interval_seconds"]) * 2)
            state["next_due"] = _iso(epoch + state["interval_seconds"]) if state["enabled"] else None
            state["last_item_fingerprints"] = item_fingerprints(snapshot)
            state["last_items"] = snapshot["items"]
            state["last_fingerprint"] = fingerprint; state["last_error"] = None
            _save(state)
            return {"created": False, "reason": "unchanged", "fingerprint": fingerprint,
                    "planning": _summary(state, initialized=True)}
        _prune_for_append(state)
        event_id = f"pulse-{state['next_id']:06d}"; state["next_id"] += 1
        event = {"id": event_id, "state": "pending", "fingerprint": fingerprint,
                 "snapshot": snapshot, "recap": recap, "created_at": _iso(epoch)}
        state["events"].append(event)
        state["last_fingerprint"] = fingerprint
        state["last_item_fingerprints"] = item_fingerprints(snapshot)
        state["last_items"] = snapshot["items"]
        state["unchanged_count"] = 0; state["last_error"] = None
        state["interval_seconds"] = ACTIVE_SECONDS if _has_active(snapshot) else QUIET_SECONDS
        state["next_due"] = _iso(epoch + state["interval_seconds"]) if state["enabled"] else None
        _save(state)
        return {"created": True, "event": event, "planning": _summary(state, initialized=True)}


def _event(state: dict, event_id: str) -> dict:
    found = next((event for event in state["events"] if event["id"] == event_id), None)
    if found is None:
        raise BossError(f"unknown planning event '{event_id}'")
    return found


def show(event_id: str) -> dict:
    return dict(_event(_read(missing_ok=False), event_id))


def claim(consumer: str, *, event_id: str | None = None, at: float | None = None) -> dict | None:
    if not isinstance(consumer, str) or not consumer.strip():
        raise BossError("planning consumer identity is required")
    epoch = time.time() if at is None else at
    away = supervisor.away_status()
    if not away.get("healthy"):
        raise BossError("planning claim cannot prove away/supervisor state")
    if away.get("enabled") or away.get("pending_wakes"):
        return None
    with locked(planning_lock()):
        state = _read(missing_ok=False)
        candidates = [_event(state, event_id)] if event_id else state["events"]
        selected = None
        for event in candidates:
            if event["state"] == "pending":
                selected = event; break
            if event["state"] == "claimed" and (_epoch(event.get("claim_until")) or 0) <= epoch:
                selected = event; break
        if selected is None:
            return None
        selected.update(state="claimed", claimed_by=consumer.strip(), claimed_at=_iso(epoch),
                        claim_until=_iso(epoch + CLAIM_SECONDS))
        _save(state)
        return dict(selected)


def begin(event_id: str, consumer: str, *, at: float | None = None) -> dict:
    epoch = time.time() if at is None else at
    # Global dual-lock order is supervisor -> planning. Supervisor never imports
    # or acquires planning.lock, and no other planning path holds planning.lock
    # while acquiring supervisor.lock. This closes wake/away insertion between
    # claim and begin without changing supervisor queue semantics.
    with locked(supervisor_lock()):
        supervisor_state, wake_queue = supervisor._state(), supervisor._queue()
        if (supervisor_state.get("away") or {}).get("enabled"):
            raise BossError("planning generation is suppressed while away mode is on")
        if any(not wake.get("acknowledged") for wake in wake_queue["events"]):
            raise BossError("a normal supervisor wake has priority over planning generation")
        with locked(planning_lock()):
            state = _read(missing_ok=False); event = _event(state, event_id)
            if event["state"] != "claimed" or event.get("claimed_by") != consumer:
                raise BossError("planning generation may begin only from this consumer's claimed receipt")
            if (_epoch(event.get("claim_until")) or 0) <= epoch:
                raise BossError("planning claim expired before generation began")
            event["state"] = "generating"; event["generating_at"] = _iso(epoch)
            event.pop("claim_until", None)
            _save(state)
            return dict(event)


def complete(event_id: str, consumer: str, *, provider: str, model: str,
             response_sha256: str, usage: dict, at: float | None = None) -> dict:
    epoch = time.time() if at is None else at
    with locked(planning_lock()):
        state = _read(missing_ok=False); event = _event(state, event_id)
        if event["state"] != "generating" or event.get("claimed_by") != consumer:
            raise BossError("planning delivery can complete only this consumer's generating receipt")
        receipt = delivery_receipt(provider, model, response_sha256, usage)
        event.update(state="delivered", terminal_at=_iso(epoch), delivery_receipt=receipt)
        state["generation_errors"] = 0; state["last_error"] = None
        _save(state)
        return dict(event)


def reject(event_id: str, consumer: str, reason: str, *, at: float | None = None) -> dict:
    epoch = time.time() if at is None else at
    with locked(planning_lock()):
        state = _read(missing_ok=False); event = _event(state, event_id)
        if event["state"] != "generating" or event.get("claimed_by") != consumer:
            raise BossError("planning rejection can complete only this consumer's generating receipt")
        event.update(state="rejected", terminal_at=_iso(epoch), reason=control.redact(reason))
        if state["last_fingerprint"] == event["fingerprint"]:
            state["last_fingerprint"] = None
        state["generation_errors"] += 1
        delay = _error_delay(state["generation_errors"])
        state["last_error"] = control.redact(reason)
        state["next_due"] = _iso(epoch + delay) if state["enabled"] else None
        _save(state)
        return dict(event)


def defer(event_id: str, consumer: str | None, reason: str, *, at: float | None = None) -> dict:
    epoch = time.time() if at is None else at
    with locked(planning_lock()):
        state = _read(missing_ok=False); event = _event(state, event_id)
        if event["state"] not in SAFE_STATES:
            raise BossError("only a safe pending/claimed planning event can be deferred")
        if event["state"] == "claimed" and event.get("claimed_by") != consumer:
            raise BossError("planning event is claimed by another consumer")
        event.update(state="deferred", terminal_at=_iso(epoch), reason=control.redact(reason))
        event.pop("claim_until", None)
        if state["last_fingerprint"] == event["fingerprint"]:
            state["last_fingerprint"] = None
        state["next_due"] = _iso(epoch + state["interval_seconds"]) if state["enabled"] else None
        _save(state)
        return dict(event)


def release(event_id: str, consumer: str) -> dict:
    with locked(planning_lock()):
        state = _read(missing_ok=False); event = _event(state, event_id)
        if event["state"] != "claimed" or event.get("claimed_by") != consumer:
            raise BossError("only this consumer's safe planning claim can be released")
        event["state"] = "pending"
        for key in ("claimed_by", "claimed_at", "claim_until"):
            event.pop(key, None)
        _save(state)
        return dict(event)


def reconcile(event_id: str, outcome: str, *, confirm: bool, reason: str,
              at: float | None = None) -> dict:
    if not confirm:
        raise BossError("planning reconciliation requires --confirm")
    if outcome not in {"delivered", "rejected"}:
        raise BossError("planning reconcile outcome must be delivered or rejected")
    epoch = time.time() if at is None else at
    with locked(planning_lock()):
        state = _read(missing_ok=False); event = _event(state, event_id)
        if event["state"] != "generating":
            raise BossError("only an uncertain generating planning receipt can be reconciled")
        redacted_reason = control.redact(reason)
        if not isinstance(redacted_reason, str) or not redacted_reason.strip():
            raise BossError("planning reconciliation requires a non-empty reason")
        receipt = {"confirmed_outcome": outcome, "reason": redacted_reason, "confirmed_at": _iso(epoch)}
        if (error := _validate_reconciliation_receipt(receipt, outcome)):
            raise BossError(error)
        event.update(state=outcome, terminal_at=_iso(epoch), reason=redacted_reason,
                     reconciled=True, reconciliation_receipt=receipt)
        if outcome == "delivered":
            state["generation_errors"] = 0; state["last_error"] = None
        else:
            state["generation_errors"] += 1; state["last_error"] = control.redact(reason)
            delay = _error_delay(state["generation_errors"])
            state["next_due"] = _iso(epoch + delay) if state["enabled"] else None
        _save(state)
        return dict(event)
