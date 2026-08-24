"""Zero-token, durable supervision and wake delivery.

This module only reads process/Herdr/item evidence and writes its own ledger.  It
does not prompt a model, kill or start a process, alter a work item, release a
claim, discard a worktree, or merge anything.
"""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import os
import time
from pathlib import Path
from .paths import supervisor_file, wakes_file, supervisor_lock, authority_lock
from .util import BossError, locked, now, write_json
from . import processes

VERSION = 1
CLASSIFICATIONS = ("healthy", "waiting", "needs-you", "stale", "wedged", "dead", "unknown")
ACTIONABLE = {"needs-you", "stale", "wedged", "dead", "unknown"}
TERMINAL = {"done", "merged", "cancelled"}
ATTENTION = {"needs-you", "failed", "ready", "pr-open"}
DEFAULT_STALE_SECONDS = 300
DEFAULT_QUEUE_STALE_SECONDS = 900
DEFAULT_WEDGE_OBSERVATIONS = 3
DEFAULT_WEDGE_RESURFACE_SECONDS = 1800
CLAIM_SECONDS = 60
MAX_RECORDS = 10_000
DELIVERY_STATES = {None, "claimed", "sending", "sent"}


def _strict_json(path: Path, default: dict) -> dict:
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return default
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BossError(f"supervisor state is unreadable at {path}: {type(exc).__name__}; run `pi-boss doctor`")
    if not isinstance(value, dict):
        raise BossError(f"supervisor state at {path} is not an object; run `pi-boss doctor`")
    return value


def validate_state(value: object) -> str | None:
    if not isinstance(value, dict) or value.get("version") != VERSION:
        return "unsupported state version/schema"
    observations = value.get("observations")
    away = value.get("away", {"enabled": False})
    if not isinstance(observations, dict) or len(observations) > MAX_RECORDS:
        return "observations must be a bounded object"
    if not isinstance(away, dict) or not isinstance(away.get("enabled", False), bool):
        return "away state is malformed"
    for key, record in observations.items():
        if not isinstance(key, str) or not key or not isinstance(record, dict):
            return "observation entries must be named objects"
        classification = record.get("classification")
        if classification is not None and classification not in CLASSIFICATIONS:
            return "observation classification is invalid"
        if record.get("unchanged_observations") is not None and (
                not isinstance(record.get("unchanged_observations"), int)
                or record["unchanged_observations"] < 0):
            return "observation counter is invalid"
    return None


def validate_queue(value: object) -> str | None:
    if not isinstance(value, dict) or value.get("version") != VERSION:
        return "unsupported wake version/schema"
    events, next_id = value.get("events"), value.get("next_id")
    if not isinstance(events, list) or len(events) > MAX_RECORDS:
        return "wake events must be a bounded list"
    if not isinstance(next_id, int) or isinstance(next_id, bool) or next_id < 1:
        return "wake next_id must be a positive integer"
    seen_ids, seen_keys = set(), set()
    for event in events:
        if not isinstance(event, dict):
            return "every wake event must be an object"
        required = ("id", "key", "item_id", "classification", "reason", "acknowledged")
        if any(key not in event for key in required):
            return "wake event is missing required fields"
        if (not isinstance(event["id"], str) or not event["id"].isdigit()
                or not isinstance(event["key"], str) or not event["key"]
                or not isinstance(event["item_id"], str) or not event["item_id"]
                or event["classification"] not in CLASSIFICATIONS
                or not isinstance(event["reason"], str)
                or not isinstance(event["acknowledged"], bool)):
            return "wake event field types are invalid"
        if event["id"] in seen_ids or event["key"] in seen_keys:
            return "wake event IDs/keys must be unique"
        seen_ids.add(event["id"]); seen_keys.add(event["key"])
        if event.get("delivery_state") not in DELIVERY_STATES:
            return "wake delivery state is invalid"
        if event.get("claimed_by") is not None and not isinstance(event.get("claimed_by"), str):
            return "wake consumer identity is invalid"
        if event.get("claim_until") is not None and _epoch(event.get("claim_until")) is None:
            return "wake claim expiry is invalid"
    if seen_ids and next_id <= max(int(value) for value in seen_ids):
        return "wake next_id must be greater than every durable event id"
    return None


def _state() -> dict:
    value = _strict_json(supervisor_file(), {"version": VERSION, "observations": {}, "away": {"enabled": False}})
    if error := validate_state(value):
        raise BossError(f"malformed supervisor state ({error}); run `pi-boss doctor`")
    value.setdefault("away", {"enabled": False})
    return value


def _queue() -> dict:
    value = _strict_json(wakes_file(), {"version": VERSION, "next_id": 1, "events": []})
    if error := validate_queue(value):
        raise BossError(f"malformed wake queue ({error}); run `pi-boss doctor`")
    return value


def _epoch(value) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


def _iso(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _lease_liveness(lease: dict | None) -> str:
    lease = lease or {}
    return processes.liveness(lease.get("process_identity"), lease.get("pid")).get("state", "unknown")


def _fingerprint(item: dict) -> str:
    session = item.get("session") or {}
    launch = next((entry for entry in reversed(item.get("agent_launches") or [])
                   if entry.get("role") == "implementer" and entry.get("state") in
                   {"reserved", "tab-created", "attested"}), {})
    wait = item.get("declared_wait") or {}
    meaningful = {
        "status": item.get("status"), "phase": item.get("phase"),
        "activity": (item.get("activity") or {}).get("last"),
        "lease": {k: (item.get("lease") or {}).get(k) for k in
                  ("pid", "owner", "started", "process_identity", "claim_token")},
        "session": {k: session.get(k) for k in ("agent_name", "agent_session_id", "pane_id")},
        "launch": {k: launch.get(k) for k in ("launch_id", "agent_session_id", "agent_name", "pane_id", "state")},
        "head": item.get("head_sha"), "ask": item.get("ask"), "wait": wait,
        "pr": item.get("pr_url"), "attempts": item.get("attempts"),
        "forge": {k: ((item.get("forge") or {}).get("latest") or {}).get(k)
                  for k in ("classification", "fingerprint", "repository", "pr_number")},
    }
    return hashlib.sha256(json.dumps(meaningful, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _wake_fingerprint(item: dict, observation: dict, item_fingerprint: str) -> str:
    """Deduplicate wake meaning separately from heartbeat/wedge observation state."""
    if observation["classification"] in ("stale", "wedged"):
        return item_fingerprint
    latest_forge = ((item.get("forge") or {}).get("latest") or {})
    meaningful = {
        "status": item.get("status"), "phase": item.get("phase"), "classification": observation["classification"],
        "ask": item.get("ask"), "head": item.get("head_sha"), "pr": item.get("pr_url"),
        "lease_pid": (item.get("lease") or {}).get("pid"),
        "session_id": (item.get("session") or {}).get("agent_session_id"),
        "launch_id": next((entry.get("launch_id") for entry in reversed(item.get("agent_launches") or [])
                           if entry.get("role") == "implementer" and entry.get("state") in
                           {"reserved", "tab-created", "attested"}), None),
        "wait_until": (item.get("declared_wait") or {}).get("until"),
        "forge": {k: latest_forge.get(k) for k in ("classification", "fingerprint", "repository", "pr_number")},
        "lease_liveness": observation.get("lease_liveness"), "agent_liveness": observation.get("agent_liveness"),
    }
    return hashlib.sha256(json.dumps(meaningful, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _threshold(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def classify(item: dict, *, at: float | None = None, agent: dict | None = None) -> dict:
    """Pure deterministic classification when ``agent`` evidence is supplied."""
    at = time.time() if at is None else at
    status = item.get("status")
    activity_at = _epoch((item.get("activity") or {}).get("last")) or _epoch(item.get("updated")) or _epoch(item.get("created"))
    age = max(0, at - activity_at) if activity_at is not None else None
    wait = item.get("declared_wait") or {}
    wait_until = _epoch(wait.get("until"))

    if status in TERMINAL:
        return {"classification": "healthy", "reason": f"terminal state {status}", "age_seconds": age}
    if status in ATTENTION or (status == "paused" and item.get("ask")):
        return {"classification": "needs-you", "reason": f"item state {status} requires boss action", "age_seconds": age}

    if status == "running":
        lease_state = _lease_liveness(item.get("lease"))
        session = item.get("session") or {}
        launch = next((entry for entry in reversed(item.get("agent_launches") or [])
                       if entry.get("role") == "implementer" and entry.get("state") in
                       {"reserved", "tab-created", "attested"}), {})
        has_agent_identity = bool(session.get("agent_name") or launch.get("agent_name"))
        agent_state = (agent or {}).get("state") if has_agent_identity else None
        if has_agent_identity:
            if agent_state == "dead":
                return {"classification": "dead", "reason": "Herdr positively reports the implementer/launch dead",
                        "age_seconds": age, "lease_liveness": lease_state, "agent_liveness": agent_state}
            if agent_state != "live":
                return {"classification": "unknown", "reason": (agent or {}).get("reason", "implementer launch liveness is unproven"),
                        "age_seconds": age, "lease_liveness": lease_state, "agent_liveness": agent_state or "unknown"}
            if lease_state == "dead":
                return {"classification": "unknown", "reason": "runner is dead but Herdr implementer is live; manual reconciliation required",
                        "age_seconds": age, "lease_liveness": lease_state, "agent_liveness": agent_state}
        elif lease_state == "dead":
            return {"classification": "dead", "reason": "runner PID is positively absent", "age_seconds": age,
                    "lease_liveness": lease_state, "agent_liveness": "not-applicable"}
        if lease_state != "live":
            return {"classification": "unknown", "reason": "runner liveness is unproven", "age_seconds": age,
                    "lease_liveness": lease_state, "agent_liveness": agent_state or "not-applicable"}
        if wait_until and at < wait_until:
            return {"classification": "waiting", "reason": f"declared wait until {_iso(wait_until)}", "age_seconds": age}
        if wait_until and at >= wait_until:
            return {"classification": "needs-you", "reason": f"declared wait elapsed at {_iso(wait_until)}", "age_seconds": age,
                    "declared_wait_elapsed": True}
        if age is None:
            return {"classification": "unknown", "reason": "activity timestamp is missing", "age_seconds": None}
        if age >= _threshold("BOSS_SUPERVISOR_STALE_SECONDS", DEFAULT_STALE_SECONDS):
            return {"classification": "stale", "reason": f"no durable activity for {int(age)}s", "age_seconds": age}
        return {"classification": "healthy", "reason": "runner and implementer evidence are live", "age_seconds": age}

    if status in ("queued", "paused"):
        if wait_until and at < wait_until:
            return {"classification": "waiting", "reason": f"declared wait until {_iso(wait_until)}", "age_seconds": age}
        if wait_until and at >= wait_until:
            return {"classification": "needs-you", "reason": f"declared wait elapsed at {_iso(wait_until)}", "age_seconds": age,
                    "declared_wait_elapsed": True}
        if status == "queued" and age is not None and age >= _threshold("BOSS_SUPERVISOR_QUEUE_STALE_SECONDS", DEFAULT_QUEUE_STALE_SECONDS):
            return {"classification": "stale", "reason": f"queued without progress for {int(age)}s", "age_seconds": age}
        return {"classification": "waiting", "reason": f"item is {status}", "age_seconds": age}

    return {"classification": "unknown", "reason": f"unrecognized item state {status!r}", "age_seconds": age}


def _agent_evidence(item: dict) -> dict | None:
    identity = item.get("session") or {}
    if not identity.get("agent_name"):
        identity = next((entry for entry in reversed(item.get("agent_launches") or [])
                         if entry.get("role") == "implementer" and entry.get("state") in
                         {"reserved", "tab-created", "attested"}), {})
    target = identity.get("agent_name")
    if not target:
        return None
    from . import herdr
    evidence = herdr.agent_liveness(target)
    return herdr.exact_agent_liveness(identity, evidence)


def _enqueue(queue: dict, item: dict, observation: dict, key: str, epoch: float) -> bool:
    if any(e.get("key") == key for e in queue["events"]):
        return False
    # Reclaim only the bounded acknowledged audit tail. Pending or uncertain
    # delivery is never dropped to make room for a new wake.
    acknowledged = [e for e in queue["events"] if e.get("acknowledged")]
    if len(acknowledged) > 500:
        drop = {id(e) for e in acknowledged[:-500]}
        queue["events"] = [e for e in queue["events"] if id(e) not in drop]
    if len(queue["events"]) >= MAX_RECORDS:
        raise BossError("wake queue reached its durable bound; no pending decision was discarded")
    event_id = str(queue["next_id"]); queue["next_id"] += 1
    queue["events"].append({
        "id": event_id, "key": key, "item_id": item.get("id"), "project": item.get("project"),
        "classification": observation["classification"], "reason": observation["reason"],
        "created": _iso(epoch), "acknowledged": False,
    })
    return True


def _observe_locked(items: list[dict], state: dict, queue: dict, epoch: float, probe_agents: bool) -> list[dict]:
    results = []
    wedge_after = _threshold("BOSS_SUPERVISOR_WEDGE_OBSERVATIONS", DEFAULT_WEDGE_OBSERVATIONS)
    resurface = _threshold("BOSS_SUPERVISOR_WEDGE_RESURFACE_SECONDS", DEFAULT_WEDGE_RESURFACE_SECONDS)
    for item in items:
        work_id = item.get("id")
        if not work_id:
            continue
        fp = _fingerprint(item)
        prior = state["observations"].get(work_id) or {}
        evidence = _agent_evidence(item) if probe_agents and item.get("status") == "running" else None
        base = classify(item, at=epoch, agent=evidence)
        unchanged = prior.get("fingerprint") == fp and prior.get("base_classification") == base["classification"]
        count = int(prior.get("unchanged_observations", 0)) + 1 if unchanged else 1
        first_seen = prior.get("first_seen") if unchanged else _iso(epoch)
        classification = base["classification"]
        if classification == "stale" and count >= wedge_after:
            classification = "wedged"
            base = {**base, "classification": classification,
                    "reason": f"unchanged stale evidence repeated {count} times: {base['reason']}"}
        record = {**base, "fingerprint": fp, "base_classification": ("stale" if classification == "wedged" else base["classification"]),
                  "unchanged_observations": count, "first_seen": first_seen, "last_seen": _iso(epoch)}
        # Terminal records have no future supervision decision and otherwise
        # accumulate forever in a long-running ops. Remove their old record;
        # pending wake receipts remain untouched and visible.
        if item.get("status") in TERMINAL:
            state["observations"].pop(work_id, None)
        else:
            if work_id not in state["observations"] and len(state["observations"]) >= MAX_RECORDS:
                raise BossError("supervisor observation ledger reached its bound without dropping live state")
            state["observations"][work_id] = record
        if classification in ACTIONABLE:
            bucket = 0
            if classification == "wedged":
                first_epoch = _epoch(prior.get("wedged_since")) if unchanged else None
                first_epoch = first_epoch or epoch
                record["wedged_since"] = _iso(first_epoch)
                bucket = int(max(0, epoch - first_epoch) // resurface)
            wake_fp = _wake_fingerprint(item, record, fp)
            key = f"{work_id}:{classification}:{wake_fp}:{bucket}"
            if _enqueue(queue, item, record, key, epoch):
                record["last_wake_key"] = key; record["last_wake_at"] = _iso(epoch)
        results.append({"item_id": work_id, **record})
    state["last_scan"] = _iso(epoch)
    return results


def observe(item: dict, *, at: float | None = None, probe_agent: bool = True) -> dict:
    """Record one item transition immediately; suitable for the mutation path."""
    epoch = time.time() if at is None else at
    with locked(supervisor_lock()):
        state, queue = _state(), _queue()
        result = _observe_locked([item], state, queue, epoch, probe_agent)[0]
        write_json(supervisor_file(), state); write_json(wakes_file(), queue)
        return result


def scan(*, at: float | None = None, probe_agents: bool = True) -> list[dict]:
    """Watchdog scan for elapsed waits and wedges. It performs no recovery."""
    from . import work
    epoch = time.time() if at is None else at
    with locked(supervisor_lock()):
        state, queue = _state(), _queue()
        result = _observe_locked(work.all_items(), state, queue, epoch, probe_agents)
        write_json(supervisor_file(), state); write_json(wakes_file(), queue)
        return result


def pending() -> list[dict]:
    with locked(supervisor_lock()):
        queue = _queue()
        return [dict(e) for e in queue["events"] if not e.get("acknowledged")]


def claim(consumer: str, *, limit: int = 20, at: float | None = None) -> list[dict]:
    if not consumer.strip():
        raise BossError("wake consumer identity is required")
    epoch = time.time() if at is None else at
    with locked(supervisor_lock()):
        state, queue = _state(), _queue()
        if (state.get("away") or {}).get("enabled"):
            return []
        selected = []
        for event in queue["events"]:
            if event.get("acknowledged"):
                continue
            # Once a Pi send may have begun, automatic replay is forbidden.
            # A crash leaves a durable uncertain receipt for the boss/doctor
            # instead of silently queuing a duplicate model turn.
            if event.get("delivery_state") in ("sending", "sent"):
                continue
            until = _epoch(event.get("claim_until")) or 0
            # An active lease is already delivery in progress, including for
            # the same consumer. Returning it again makes an 8s UI poll start
            # duplicate model turns before the first turn can acknowledge it.
            if event.get("claimed_by") and until > epoch:
                continue
            event["claimed_by"] = consumer; event["claim_until"] = _iso(epoch + CLAIM_SECONDS)
            event["delivery_state"] = "claimed"
            selected.append(dict(event))
            if len(selected) >= max(1, limit):
                break
        if selected:
            write_json(wakes_file(), queue)
        return selected


def _delivery_update(ids: list[str], consumer: str, state: str, *, at: float | None = None) -> int:
    if state not in ("sending", "sent"):
        raise BossError("invalid wake delivery transition")
    wanted = {str(value) for value in ids}; count = 0
    epoch = time.time() if at is None else at
    with locked(supervisor_lock()):
        queue = _queue()
        for event in queue["events"]:
            if (event.get("id") in wanted and not event.get("acknowledged")
                    and event.get("claimed_by") == consumer):
                prior = event.get("delivery_state")
                if ((state == "sending" and prior == "claimed")
                        or (state == "sent" and prior in ("sending", "sent"))):
                    event["delivery_state"] = state
                    event[f"{state}_at"] = _iso(epoch)
                    event["claim_until"] = _iso(epoch + CLAIM_SECONDS)
                    count += 1
        write_json(wakes_file(), queue)
    return count


def mark_sending(ids: list[str], consumer: str) -> int:
    return _delivery_update(ids, consumer, "sending")


def mark_sent(ids: list[str], consumer: str) -> int:
    return _delivery_update(ids, consumer, "sent")


def renew(ids: list[str], consumer: str, *, at: float | None = None) -> int:
    wanted = {str(value) for value in ids}; count = 0
    epoch = time.time() if at is None else at
    with locked(supervisor_lock()):
        queue = _queue()
        for event in queue["events"]:
            if (event.get("id") in wanted and not event.get("acknowledged")
                    and event.get("claimed_by") == consumer
                    and event.get("delivery_state") in ("claimed", "sending", "sent")):
                event["claim_until"] = _iso(epoch + CLAIM_SECONDS); count += 1
        write_json(wakes_file(), queue)
    return count


def acknowledge(ids: list[str], consumer: str) -> int:
    wanted = {str(v) for v in ids}
    count = 0
    with locked(supervisor_lock()):
        queue = _queue()
        for event in queue["events"]:
            if event.get("id") in wanted and not event.get("acknowledged") and event.get("claimed_by") == consumer:
                event["acknowledged"] = True; event["acknowledged_at"] = now()
                event["delivery_state"] = "sent"; count += 1
        write_json(wakes_file(), queue)
    return count


def release(ids: list[str], consumer: str) -> int:
    wanted = {str(v) for v in ids}; count = 0
    with locked(supervisor_lock()):
        queue = _queue()
        for event in queue["events"]:
            if (event.get("id") in wanted and not event.get("acknowledged")
                    and event.get("claimed_by") == consumer
                    and event.get("delivery_state") == "claimed"):
                event.pop("claimed_by", None); event.pop("claim_until", None); count += 1
                event.pop("delivery_state", None)
        write_json(wakes_file(), queue)
    return count


def summary() -> dict:
    """Read-only status for UI/doctor; malformed state stays explicitly unknown."""
    try:
        state = _state(); queue = _queue()
    except BossError as exc:
        return {"healthy": False, "error": exc.msg, "pending_wakes": None, "away": None}
    counts = {}
    for value in state["observations"].values():
        name = value.get("classification", "unknown")
        counts[name] = counts.get(name, 0) + 1
    return {"healthy": True, "last_scan": state.get("last_scan"), "classifications": counts,
            "pending_wakes": sum(not e.get("acknowledged") for e in queue["events"]),
            "away": bool((state.get("away") or {}).get("enabled"))}


def away_status() -> dict:
    try:
        state = _state(); queue = _queue()
    except BossError as exc:
        return {"enabled": None, "healthy": False, "error": exc.msg}
    away = dict(state.get("away") or {"enabled": False})
    return {**away, "healthy": True,
            "pending_wakes": sum(not event.get("acknowledged") for event in queue["events"])}


def set_away(enabled: bool) -> dict:
    """Enable only after deterministic supervision/recovery preflight passes.

    When turning off, the gate_evidence recorded at enable time is re-verified
    against the live world so we cannot accidentally accept a state that drifted
    while unattended.
    """
    # Promotion holds this same lock. Keeping it for the full preflight closes
    # the gap where a merge could arm between validation and the durable bit.
    with locked(authority_lock()):
        if not enabled:
            with locked(supervisor_lock()):
                state = _state()
                # Re-verify the gate_evidence recorded at enable time. If anything in the
                # environment that contributed to the gate has changed (worker identity
                # drifted, doctor summary changed), refuse to silently re-enter normal mode.
                prior = (state.get("away") or {})
                recorded = prior.get("gate_evidence")
                recorded_workers = prior.get("live_workers") or []
                recorded_doctor = prior.get("doctor_summary")
                refusal: str | None = None
                if recorded:
                    # Cheap, deterministic re-verify: re-run the doctor summary; if the
                    # recorded summary no longer matches, refuse. We deliberately do NOT
                    # re-run the gate_evidence hash because that would clobber it on
                    # normal background churn; the doctor summary is the durable bit.
                    try:
                        from . import doctor as _doc
                        live = _doc.audit(network=False, probe_models=False)
                        if live.get("summary") != recorded_doctor:
                            refusal = f"doctor summary changed while away ({recorded_doctor} -> {live.get('summary')})"
                    except Exception as exc:
                        refusal = f"doctor re-verify failed: {exc}"
                if refusal:
                    raise BossError(f"away-mode off refused: {refusal}; reconcile via `pi-boss doctor` and re-run")
                state["away"] = {**(state.get("away") or {}), "enabled": False, "disabled_at": now()}
                write_json(supervisor_file(), state)
            return away_status()

        observations = scan(probe_agents=True)
        blocking = [o for o in observations if o.get("classification") in ACTIONABLE]
        if blocking:
            names = ", ".join(f"{o['item_id']}={o['classification']}" for o in blocking[:8])
            raise BossError(f"away mode refused: resolve supervisor attention first ({names})")
        from . import work
        unresolved_transactions = []
        for item in work.all_items():
            if (item.get("promotion") or {}).get("state") in {"armed", "external-requested", "merge-observed", "cleanup-pending"}:
                unresolved_transactions.append(f"{item['id']}:promotion")
            if (item.get("pr_delivery") or {}).get("state") in {"armed", "push-requested", "push-confirmed", "pr-create-requested"}:
                unresolved_transactions.append(f"{item['id']}:pr-delivery")
            if (item.get("cancellation") or {}).get("state") in {"armed", "quiescing", "filesystem-requested", "filesystem-complete"}:
                unresolved_transactions.append(f"{item['id']}:cancellation")
            if (item.get("external_gate") or {}).get("state") in {
                "armed", "request-started", "command-returned", "running", "needs-decision",
                "response-armed", "response-requested", "unknown",
            }:
                unresolved_transactions.append(f"{item['id']}:no-mistakes")
        if unresolved_transactions:
            raise BossError("away mode refused: reconcile open transaction(s) first (" +
                            ", ".join(unresolved_transactions[:8]) + ")")
        from . import doctor
        report = doctor.audit(network=False, probe_models=False)
        if not report.get("healthy"):
            errors = [c["id"] for c in report.get("checks", []) if c.get("status") == "error"]
            raise BossError("away mode refused: doctor recovery gate failed (" + ", ".join(errors[:8]) + ")")
        live_workers = [c for c in report.get("checks", []) if c.get("id", "").startswith("worker:") and c.get("status") == "ok"]
        if not live_workers:
            raise BossError("away mode refused: no positively live worker is registered")
        gate_evidence = hashlib.sha256(json.dumps({"observations": [(o["item_id"], o["classification"], o["fingerprint"]) for o in observations],
                                                   "doctor": report.get("summary"), "workers": [c["id"] for c in live_workers]},
                                                  sort_keys=True).encode()).hexdigest()
        with locked(supervisor_lock()):
            state, queue = _state(), _queue()
            pending_count = sum(not event.get("acknowledged") for event in queue["events"])
            if pending_count:
                raise BossError(f"away mode refused: {pending_count} pending decision wake(s) must be handled first")
            state["away"] = {"enabled": True, "enabled_at": now(), "gate_evidence": gate_evidence,
                             "doctor_summary": report.get("summary"), "live_workers": [c["id"] for c in live_workers]}
            write_json(supervisor_file(), state)
    return away_status()


def parse_duration(value: str) -> int:
    import re
    match = re.fullmatch(r"(?i)(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", value.strip())
    if not match or not any(match.groups()):
        raise BossError("duration must look like 30s, 20m, 1h, or 1h30m")
    seconds = int(match.group(1) or 0) * 3600 + int(match.group(2) or 0) * 60 + int(match.group(3) or 0)
    if seconds <= 0:
        raise BossError("duration must be positive")
    return seconds


def declare_wait(work_id: str, duration: str | None, reason: str = "") -> dict:
    from . import control
    if duration in (None, "clear"):
        def clear(item): item.pop("declared_wait", None)
        item = control.cas_update(work_id, clear)
    else:
        seconds = parse_duration(duration); epoch = time.time()
        def set_wait(item):
            item["declared_wait"] = {"declared_at": _iso(epoch), "until": _iso(epoch + seconds),
                                     "reason": reason.strip() or "boss-declared wait"}
        item = control.cas_update(work_id, set_wait)
    observe(item)
    return item
