"""Work items: durable JSON records under ~/.boss/work/<id>/ with an explicit state machine.

queued → running → ready | pr-open | needs-you | done | failed
needs-you --respond--> queued      failed --retry--> queued
ready/pr-open --promote--> merged
"""
from __future__ import annotations
import copy
import datetime as dt
import fnmatch
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import time
from pathlib import Path
from . import dispatch, graphs, registry, worktree, deliver, herdr, scope, rigor, control, modes, gates, ids, processes, sandbox
from .paths import work_root, home, authority_lock
from .util import read_json, write_json, locked, now, log, BossError, git, sh

ACTIVE = ("running",)
OPEN = ("queued", "running", "paused", "needs-you", "ready", "pr-open")
MAX_REVIEW_DIFF_BYTES = 200_000
MODEL_BUDGET_NODES = ("implement", "scout", "review_correctness", "review_adversarial")
BUDGET_NODES = (*MODEL_BUDGET_NODES, "verify")
MAX_BUDGET_RECEIPTS = 10_000


def valid_budget_value(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)) and float(value) > 0)


def _valid_usage_value(key: str, value) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    numeric = float(value)
    return (math.isfinite(numeric) and numeric >= 0
            and (key != "tokens" or numeric.is_integer()))


def _remaining_timeout(deadline: float | None, cap: int) -> int:
    if deadline is None:
        return max(1, int(cap))
    remaining = deadline - time.monotonic()
    return 0 if remaining < 1.0 else max(1, min(int(cap), int(remaining)))


def validate_node_budgets(value: dict | None) -> dict:
    """Normalize the explicit runtime-node budget contract."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise BossError("node budgets must be an object keyed by runtime node")
    normalized = {}
    for node, raw in value.items():
        if node not in BUDGET_NODES:
            raise BossError(f"unknown budget node '{node}'; choose from {', '.join(BUDGET_NODES)}")
        if not isinstance(raw, dict):
            raise BossError(f"node budget '{node}' must be an object")
        unknown = set(raw) - {"tokens", "cost", "seconds"}
        if unknown:
            raise BossError(f"node budget '{node}' has unknown fields: {', '.join(sorted(unknown))}")
        limits = {key: raw.get(key) for key in ("tokens", "cost", "seconds")}
        for key, limit in limits.items():
            if (limit is not None and (not valid_budget_value(limit)
                                       or key == "tokens" and not float(limit).is_integer())):
                raise BossError(f"{node} {key} budget must be positive")
        if node == "verify" and any(limits[key] is not None for key in ("tokens", "cost")):
            raise BossError("verify is a non-model node; only its seconds budget is meaningful")
        if any(limit is not None for limit in limits.values()):
            normalized[node] = limits
    return normalized


def budget_state_error(it: object) -> str | None:
    """Validate the complete persisted budget contract without mutating it.

    This is the single authority used by item loading, CAS writes, the runtime
    pre-turn gate, and doctor.  A malformed number or evidence flag must never
    reach a comparison where NaN/None could turn a safety check into False.
    """
    if not isinstance(it, dict):
        return "item budget state is not an object"
    historical = bool(it.get("attempts") or it.get("runs") or it.get("session")
                      or it.get("agent_launches"))

    raw_budgets = it.get("budgets", {})
    if raw_budgets is None:
        raw_budgets = {}
    if not isinstance(raw_budgets, dict) or set(raw_budgets) - {"tokens", "cost", "seconds"}:
        return "item budgets are malformed"
    budgets = {key: raw_budgets.get(key) for key in ("tokens", "cost", "seconds")}
    for key, limit in budgets.items():
        if (limit is not None and (not valid_budget_value(limit)
                                   or key == "tokens" and not float(limit).is_integer())):
            return f"item budget {key} is malformed"

    raw_usage = it.get("usage", {})
    if raw_usage is None:
        raw_usage = {}
    if not isinstance(raw_usage, dict):
        return "item usage is malformed"
    for key in ("tokens", "cost", "seconds", "implementer_tokens", "implementer_cost",
                "reviewer_tokens", "reviewer_cost"):
        if key in raw_usage and not _valid_usage_value("tokens" if key.endswith("tokens") else
                                                       "cost" if key.endswith("cost") else key,
                                                       raw_usage[key]):
            return f"item usage {key} is malformed"
    for key in ("tokens", "cost"):
        flag = f"{key}_evidence_complete"
        complete = raw_usage.get(flag)
        if flag in raw_usage and not isinstance(complete, bool):
            return f"item usage {key}_evidence_complete is malformed"

    seconds_receipts = raw_usage.get("seconds_receipts", {})
    if (not isinstance(seconds_receipts, dict) or len(seconds_receipts) > MAX_BUDGET_RECEIPTS
            or any(not isinstance(receipt_id, str) or not receipt_id
                   or not _valid_usage_value("seconds", measured)
                   for receipt_id, measured in seconds_receipts.items())):
        return "item usage seconds receipts are malformed"
    try:
        receipt_seconds_floor = math.fsum(float(measured) for measured in seconds_receipts.values())
    except OverflowError:
        return "item usage seconds receipt total is not finite"
    if not math.isfinite(receipt_seconds_floor):
        return "item usage seconds receipt total is not finite"
    reported_seconds = float(raw_usage.get("seconds", 0.0))
    # Each receipt is a cumulative maximum for one execution identity and the
    # scalar advances by its positive delta. Legacy/success-path accounting may
    # make the scalar larger, never smaller. Allow only floating summation noise.
    tolerance = max(1e-9, abs(receipt_seconds_floor) * 1e-12)
    if reported_seconds + tolerance < receipt_seconds_floor:
        return "item usage seconds receipt totals exceed reported seconds"

    receipts = raw_usage.get("receipts", {})
    if (not isinstance(receipts, dict) or len(receipts) > MAX_BUDGET_RECEIPTS
            or any(not isinstance(session_id, str) or not session_id or len(session_id) > 256
                   or not isinstance(receipt, dict)
                   for session_id, receipt in receipts.items())):
        return "item usage receipts are malformed"
    for session_id, receipt in receipts.items():
        if receipt.get("role") not in {"implementer", "reviewer", "legacy"}:
            return f"item usage receipt {session_id} role is malformed"
        for key in ("tokens", "cost"):
            measured = receipt.get(key)
            if measured is not None and not _valid_usage_value(key, measured):
                return f"item usage receipt {session_id} {key} is malformed"
            available = receipt.get(f"{key}_available")
            if available is not None and not isinstance(available, bool):
                return f"item usage receipt {session_id} {key} availability is malformed"
            if available is True and measured is None:
                return f"item usage receipt {session_id} {key} is missing"
    if receipts:
        implementers = [receipt for receipt in receipts.values() if receipt.get("role") == "implementer"]
        reviewers = [receipt for receipt in receipts.values() if receipt.get("role") == "reviewer"]
        legacy = [receipt for receipt in receipts.values() if receipt.get("role") == "legacy"]
        derived = {
            "implementer_tokens": sum(int(receipt.get("tokens") or 0) for receipt in implementers),
            "implementer_cost": sum(float(receipt.get("cost") or 0.0) for receipt in implementers),
            "reviewer_tokens": sum(int(receipt.get("tokens") or 0) for receipt in reviewers),
            "reviewer_cost": sum(float(receipt.get("cost") or 0.0) for receipt in reviewers),
        }
        derived["tokens"] = (derived["implementer_tokens"] + derived["reviewer_tokens"]
                             + sum(int(receipt.get("tokens") or 0) for receipt in legacy))
        derived["cost"] = (derived["implementer_cost"] + derived["reviewer_cost"]
                           + sum(float(receipt.get("cost") or 0.0) for receipt in legacy))
        for key, expected in derived.items():
            actual = raw_usage.get(key, 0)
            agrees = (actual == expected if key.endswith("tokens")
                      else math.isclose(float(actual), float(expected), rel_tol=1e-12, abs_tol=1e-12))
            if not agrees:
                return f"item usage receipt totals disagree on {key}"
        for key in ("tokens", "cost"):
            derived_complete = all(receipt.get(f"{key}_available") is True
                                   for receipt in receipts.values())
            if raw_usage.get(f"{key}_evidence_complete") is not derived_complete:
                return f"item usage receipt evidence disagrees on {key}"

    try:
        node_budgets = validate_node_budgets(it.get("node_budgets"))
    except BossError as exc:
        return f"item node budgets are malformed: {exc.msg}"
    raw_node_usage = it.get("node_usage", {})
    if raw_node_usage is None:
        raw_node_usage = {}
    if (not isinstance(raw_node_usage, dict) or len(raw_node_usage) > len(BUDGET_NODES)
            or set(raw_node_usage) - set(BUDGET_NODES)):
        return "item node usage contains an unknown or malformed runtime node"
    for node, usage in raw_node_usage.items():
        if not isinstance(usage, dict):
            return f"item node usage {node} is malformed"
        for key in ("tokens", "cost", "seconds"):
            if key in usage and not _valid_usage_value(key, usage[key]):
                return f"item node usage {node}.{key} is malformed"
        node_receipts = usage.get("receipts", {})
        if (not isinstance(node_receipts, dict) or len(node_receipts) > MAX_BUDGET_RECEIPTS
                or any(not isinstance(session_id, str) or not session_id or len(session_id) > 256
                       or not isinstance(receipt, dict)
                       for session_id, receipt in node_receipts.items())):
            return f"item node usage {node} receipts are malformed"
        for session_id, receipt in node_receipts.items():
            for key in ("tokens", "cost"):
                measured = receipt.get(key)
                if measured is not None and not _valid_usage_value(key, measured):
                    return f"item node usage {node} receipt {key} is malformed"
                available = receipt.get(f"{key}_available")
                if available is not None and not isinstance(available, bool):
                    return f"item node usage {node} receipt {key} availability is malformed"
                if available is True and measured is None:
                    return f"item node usage {node} receipt {key} is missing"
        if node_receipts:
            receipt_tokens = sum(int(receipt.get("tokens") or 0) for receipt in node_receipts.values())
            receipt_cost = sum(float(receipt.get("cost") or 0.0) for receipt in node_receipts.values())
            if (usage.get("tokens", 0) != receipt_tokens
                    or not math.isclose(float(usage.get("cost", 0.0)), receipt_cost,
                                        rel_tol=1e-12, abs_tol=1e-12)):
                return f"item node usage {node} receipt totals disagree"
        for key in ("tokens", "cost", "seconds"):
            flag = f"{key}_evidence_complete"
            complete = usage.get(flag)
            if flag in usage and not isinstance(complete, bool):
                return f"item node usage {node}.{key}_evidence_complete is malformed"
        for key in ("tokens", "cost"):
            if node_receipts and usage.get(f"{key}_evidence_complete") is True \
                    and not all(receipt.get(f"{key}_available") is True
                                for receipt in node_receipts.values()):
                return f"item node usage {node} receipt evidence disagrees on {key}"
    if historical:
        for node, limits in node_budgets.items():
            if node not in raw_node_usage and any(limit is not None for limit in limits.values()):
                return f"item node usage {node} evidence is incomplete"
    return None


def validate_budget_state(it: object) -> None:
    if error := budget_state_error(it):
        raise BossError(error)


def _new_node_usage(*, complete: bool = True) -> dict:
    return {"tokens": 0, "cost": 0.0, "seconds": 0.0,
            "tokens_evidence_complete": complete, "cost_evidence_complete": complete,
            "seconds_evidence_complete": complete, "receipts": {}}


def _node_usage_snapshot(it: dict) -> dict:
    raw = it.get("node_usage") or {}
    if not isinstance(raw, dict):
        raw = {}
    result = copy.deepcopy(raw)
    historical = bool(it.get("attempts") or it.get("runs") or it.get("session") or it.get("agent_launches"))
    for node in (it.get("node_budgets") or {}):
        result.setdefault(node, _new_node_usage(complete=not historical))
    return result


def _node_state(node_usage: dict, node: str, *, complete: bool = True) -> dict:
    state = node_usage.setdefault(node, _new_node_usage(complete=complete))
    state.setdefault("tokens", 0); state.setdefault("cost", 0.0); state.setdefault("seconds", 0.0)
    state.setdefault("tokens_evidence_complete", complete)
    state.setdefault("cost_evidence_complete", complete)
    state.setdefault("seconds_evidence_complete", complete)
    state.setdefault("receipts", {})
    return state


def _record_node_session(node_usage: dict, node: str, identity: dict, evidence: dict,
                         *, started: bool) -> None:
    """Monotonically attribute one node's cumulative Pi session receipt."""
    state = _node_state(node_usage, node)
    session_id = str(identity.get("agent_session_id") or "")
    if not session_id:
        if started:
            state["tokens_evidence_complete"] = False
            state["cost_evidence_complete"] = False
        return
    has_evidence = any(_valid_usage_value(key, evidence.get(key)) for key in ("tokens", "cost"))
    if not started and not has_evidence:
        return
    receipt = state["receipts"].setdefault(session_id, {})
    for key in ("tokens", "cost"):
        value = evidence.get(key)
        if _valid_usage_value(key, value):
            receipt[key] = max(float(receipt.get(key) or 0), float(value))
            if key == "tokens":
                receipt[key] = int(receipt[key])
            receipt[f"{key}_available"] = True
        elif started:
            receipt[f"{key}_available"] = False
    receipt["observed_at"] = now()
    receipts = list(state["receipts"].values())
    state["tokens"] = sum(int(entry.get("tokens") or 0) for entry in receipts)
    state["cost"] = sum(float(entry.get("cost") or 0.0) for entry in receipts)
    if receipts:
        state["tokens_evidence_complete"] = all(entry.get("tokens_available") is True for entry in receipts)
        state["cost_evidence_complete"] = all(entry.get("cost_available") is True for entry in receipts)


def _record_node_seconds(node_usage: dict, node: str, seconds: float, *, complete: bool = True) -> None:
    state = _node_state(node_usage, node)
    state["seconds"] = float(state.get("seconds") or 0.0) + max(0.0, float(seconds))
    state["seconds_evidence_complete"] = state.get("seconds_evidence_complete") is True and complete
    state["seconds_accounted_at"] = now()


def _persist_node_usage(work_id: str, node_usage: dict) -> dict:
    """Merge one runner's node ledger without overwriting concurrent controls."""
    snapshot = copy.deepcopy(node_usage)
    return control.cas_update(work_id, lambda item: item.update(node_usage=snapshot))


def _missing_settled_usage(budgets: dict, evidence: dict, settled_sequence: int,
                           label: str) -> list[str]:
    """Usage is unavailable only after Pi proves at least one turn settled."""
    if settled_sequence <= 0:
        return []
    return [f"{key} usage evidence {label}" for key in ("tokens", "cost")
            if budgets.get(key) is not None and key not in evidence]


def _node_budget_findings(node_budgets: dict, node_usage: dict, node: str, *,
                          exhausted: bool) -> list[str]:
    limits = node_budgets.get(node) or {}
    usage = node_usage.get(node) or {}
    findings = []
    for key in ("tokens", "cost", "seconds"):
        limit = limits.get(key)
        if limit is None:
            continue
        if usage.get(f"{key}_evidence_complete") is not True:
            findings.append(f"{node} {key} usage evidence is incomplete")
            continue
        actual = usage.get(key)
        if not isinstance(actual, (int, float)) or isinstance(actual, bool):
            findings.append(f"{node} {key} usage evidence is unavailable")
        elif (float(actual) >= float(limit) if exhausted else float(actual) > float(limit)):
            word = "exhausted" if exhausted else "exceeded"
            findings.append(f"{node} {key} budget {word} ({actual:g}/{limit:g})")
        elif exhausted and key == "seconds" and float(limit) - float(actual) < 1.0:
            findings.append(f"{node} seconds budget has less than one bounded second remaining ({actual:g}/{limit:g})")
    return findings


def node_budget_blockers(it: dict, node: str | None = None, *, node_usage: dict | None = None) -> list[str]:
    budgets = validate_node_budgets(it.get("node_budgets"))
    usage = node_usage if node_usage is not None else _node_usage_snapshot(it)
    nodes = [node] if node else list(budgets)
    return [finding for name in nodes for finding in _node_budget_findings(budgets, usage, name, exhausted=True)]


def _node_timeout(it: dict, node_usage: dict, node: str, deadline: float | None, cap: int) -> int:
    bounded = _remaining_timeout(deadline, cap)
    limit = ((it.get("node_budgets") or {}).get(node) or {}).get("seconds")
    if limit is None:
        return bounded
    state = node_usage.get(node) or {}
    if state.get("seconds_evidence_complete") is not True:
        return 0
    remaining = float(limit) - float(state.get("seconds") or 0.0)
    if remaining < 1.0:
        return 0
    return min(bounded, max(1, int(remaining)))


def _policy_snapshot(project: dict) -> dict:
    return {key: project.get(key) for key in (
        "id", "path", "mode", "authority", "base", "test_cmd", "protected_paths", "gate"
    )}


def item_dir(work_id: str) -> Path: return work_root() / ids.work(work_id)
def item_path(work_id: str) -> Path: return item_dir(work_id) / "item.json"


def _hydrate(it: dict) -> dict:
    """Read-compatible migration for version-1 records; the next CAS write persists it."""
    it.setdefault("schema_version", control.SCHEMA_VERSION); it.setdefault("revision", 0)
    it.setdefault("phase", it.get("status", "queued")); it.setdefault("scope", {"paths": ["unknown"], "claim": "global"})
    it.setdefault("controls", {"paused": False, "pending": []})
    for key, default in (("session", None), ("checkpoint", None), ("reviews", []), ("verification", []),
                         ("changed_scope", []), ("agent_launches", [])):
        it.setdefault(key, default)
    it.setdefault("activity", {"last": it.get("updated"), "state": it.get("phase")})
    it.setdefault("budgets", {"tokens": None, "cost": None, "seconds": None})
    if not isinstance(it.get("budgets"), dict):
        raise BossError("item budgets are malformed")
    it["node_budgets"] = validate_node_budgets(it.get("node_budgets"))
    it.setdefault("usage", {"tokens": 0, "cost": 0.0, "seconds": 0.0,
                            "implementer_tokens": 0, "implementer_cost": 0.0,
                            "reviewer_tokens": 0, "reviewer_cost": 0.0})
    if not isinstance(it.get("usage"), dict):
        raise BossError("item usage is malformed")
    usage = it["usage"]
    historical_turn = bool(it.get("attempts") or it.get("runs") or it.get("session")
                           or it.get("agent_launches"))
    # A migrated record with model-turn history and no explicit metering receipt
    # is unknown, never a trustworthy zero. Brand-new items have proven zero use.
    usage.setdefault("tokens_evidence_complete", not historical_turn)
    usage.setdefault("cost_evidence_complete", not historical_turn)
    if it.get("node_usage") is not None and not isinstance(it.get("node_usage"), dict):
        raise BossError("item node usage is malformed")
    it["node_usage"] = _node_usage_snapshot(it)
    if not it.get("model_decision") and it.get("dispatch"):
        it["model_decision"] = {"models": it["dispatch"].get("models", {}), "thinking": it["dispatch"].get("thinking", {}),
                                "rationale": "migrated pinned dispatch", "resolved_at": it.get("created")}
    if it.get("dispatch"):
        it["dispatch"]["graph"] = modes.normalize(it["dispatch"].get("graph"))
    validate_budget_state(it)
    return it


def load(work_id: str) -> dict:
    work_id = ids.work(work_id)
    it = read_json(item_path(work_id))
    if not it:
        raise BossError(f"unknown work item '{work_id}'")
    if it.get("id") != work_id:
        raise BossError("work item identity does not match its durable state directory")
    return _hydrate(it)


def save(it: dict) -> None:
    """CAS-save an item. Stale writers fail instead of erasing concurrent controls."""
    validate_budget_state(it)
    expected = int(it.get("revision", 0))
    def replace(current):
        if int(current.get("revision", 0)) != expected:
            raise BossError(f"stale work item revision {expected}; current revision is {current.get('revision', 0)}")
        current.clear(); current.update(it)
    updated = control.cas_update(it["id"], replace, expected)
    it.clear(); it.update(updated)


def transition(it: dict, status: str, note: str = "") -> None:
    it.setdefault("history", []).append({"at": now(), "from": it["status"], "to": status, "note": note})
    it["status"] = status
    canonical_phase = {"running": "implementing", "ready": "merge-ready", "pr-open": "pr-open",
                       "failed": "failed", "done": "done", "merged": "merged",
                       "cancelled": "cancelled"}.get(status)
    if canonical_phase:
        it["phase"] = canonical_phase
    it["activity"] = {"last": now(), "state": it.get("phase") or status}
    save(it)
    log(f"{it['id']}: {status}" + (f" — {note}" if note else ""))
    try:
        from . import supervisor
        supervisor.observe(it)
    except (Exception, BossError) as exc:
        # Item state is already durable. Never roll it back or fake a wake; the
        # read-only doctor will expose a damaged supervisor ledger.
        log(f"{it['id']}: supervisor observation unavailable: {getattr(exc, 'msg', None) or exc!r}")


def all_items() -> list[dict]:
    out = []
    if work_root().is_dir():
        for d in sorted(work_root().iterdir()):
            it = read_json(d / "item.json")
            if it:
                if not ids.WORK_PATTERN.fullmatch(d.name) or d.name != it.get("id"):
                    continue
                out.append(_hydrate(it))
    return sorted(out, key=lambda i: i["created"])


def print_summary(it: dict, args) -> None:
    """Compact, tail-friendly item summary for terminal states. Honors --json."""
    if getattr(args, "json", False):
        import json as _json
        print(_json.dumps({"id": it.get("id"), "status": it.get("status"),
                           "project": it.get("project"), "summary": True}))
        return
    status = it.get("status", "unknown")
    text = (it.get("text") or "").splitlines()[0] if it.get("text") else ""
    print(f"{it.get('id', '?')} — status={status}  project={it.get('project', '?')}")
    if text:
        print(f"  {text[:120]}")
    notes = (it.get("failure_notes") or {}).get("notes") if it.get("failure_notes") else None
    if notes:
        print(f"  notes: {notes[:200]}")


def create(project_id: str, text: str, kind: str = "ship", labels: list[str] | None = None,
           max_attempts: int = 3, declared_scope: list[str] | None = None,
           model: str | None = None, thinking: str | None = None,
           max_tokens: int | None = None, max_cost: float | None = None, max_seconds: int | None = None,
           node_budgets: dict | None = None,
           memory_request: dict | None = None) -> dict:
    project = registry.get(project_id)
    gates.require_execution(project.get("gate", "native"), project["path"])
    if kind not in ("ship", "scout"):
        raise BossError("kind must be ship or scout")
    if model and "/" not in model:
        raise BossError("model override must be provider/model")
    if any(v is not None and not valid_budget_value(v) for v in (max_tokens, max_cost, max_seconds)):
        raise BossError("budgets must be positive")
    node_budgets = validate_node_budgets(node_budgets)
    if project.get("gate") == "no-mistakes" and kind == "ship":
        if project.get("authority", 0) < 2 or project.get("mode") == modes.LOCAL_ONLY:
            raise BossError("no-mistakes can push/open a PR and requires non-local mode with authority >= 2")
        if any(value is not None for value in (max_tokens, max_cost, max_seconds)) or node_budgets:
            raise BossError("no-mistakes cannot prove BOSS token/cost/time caps; configured budgets fail closed")
    available = {m.strip() for m in os.environ.get("BOSS_AVAILABLE_MODELS", "").split(",") if m.strip()}
    if model and available and model not in available:
        raise BossError(f"resolved model {model} is unavailable; refusing silent substitution")
    if kind == "ship" and project["authority"] < 1:
        raise BossError(f"project '{project_id}' has authority 0 (observe): only scout tasks allowed")
    if kind == "ship" and not (project.get("test_cmd") or "").strip():
        raise BossError(
            f"project '{project_id}' has no test command; ship tasks require one. "
            f"Set it with `bossctl set {project_id} --test <cmd>` or use kind=scout for inspection."
        )
    wid = f"{project_id}-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"
    declared = scope.normalize(declared_scope)
    # `scope.overlap` treats either sensitive operand as globally serializing,
    # which is correct for two durable claims but wrong for this policy test:
    # the mere existence of a protected pattern would make every disjoint file
    # look protected. Here we need the underlying conservative glob intersection.
    protected_overlap = any(scope.patterns_overlap(path, protected)
                            for path in declared for protected in project.get("protected_paths", []))
    global_claim = scope.is_global(declared) or protected_overlap
    it = {
        "id": wid, "project": project_id, "kind": kind, "text": text.strip(),
        "labels": sorted(set(labels or [])), "status": "queued", "attempts": 0,
        "max_attempts": max_attempts, "created": now(), "updated": now(),
        "guidance": [], "failure_notes": [], "runs": [], "history": [],
        "schema_version": control.SCHEMA_VERSION, "revision": 0,
        "branch": worktree.branch_name(wid), "worktree": str(worktree.worktree_root() / project_id / wid),
        "pr_url": None, "ask": None, "dispatch": None, "phase": "queued",
        "scope": {"paths": declared, "claim": "global" if global_claim else "paths"},
        "controls": {"paused": False, "pending": []}, "session": None, "agent_launches": [],
        "checkpoint": None, "reviews": [], "verification": [], "changed_scope": [],
        "activity": {"last": now(), "state": "queued"},
        "budgets": {"tokens": max_tokens, "cost": max_cost, "seconds": max_seconds},
        "node_budgets": node_budgets,
        "node_usage": {node: _new_node_usage() for node in node_budgets},
        "usage": {"tokens": 0, "cost": 0.0, "seconds": 0.0,
                  "implementer_tokens": 0, "implementer_cost": 0.0,
                  "reviewer_tokens": 0, "reviewer_cost": 0.0,
                  "tokens_evidence_complete": True, "cost_evidence_complete": True},
        "model_overrides": ({("scout" if kind == "scout" else "implement"): model} if model else {}),
        "thinking_overrides": ({("scout" if kind == "scout" else "implement"): thinking} if thinking else {}),
        "memory_request": memory_request,
    }
    it["rigor"] = rigor.route(it)
    if global_claim and declared != ["unknown"] and it["rigor"]["level"] != "high-risk":
        it["rigor"] = {"level": "high-risk", "rationale": "project-sensitive declared scope"}
    it["dispatch"] = dispatch.resolve(it, project)   # pinned before execution
    dispatch.assert_available(it["dispatch"])
    it["model_decision"] = {"models": it["dispatch"]["models"], "thinking": it["dispatch"]["thinking"],
                            "rationale": it["dispatch"]["rationale"], "resolved_at": now()}
    write_json(item_path(wid), it)
    try:
        from . import supervisor
        supervisor.observe(it)
    except (Exception, BossError) as exc:
        log(f"{wid}: supervisor observation unavailable: {getattr(exc, 'msg', None) or exc!r}")
    log(f"{wid}: queued ({kind}, rule={it['dispatch']['rule']}, graph={it['dispatch']['graph']})")
    return it


def brief_text(it: dict, project: dict) -> str:
    lines = [f"# bossctl {it['kind']} brief — {it['id']}", "",
             f"Project: {project['id']} ({project['path']})",
             f"Mode: {project['mode']} · authority {project['authority']} · base {project['base']}",
             f"Attempt: {it['attempts'] + 1} of {it['max_attempts']}", "", "## Task", "", it["text"], ""]
    if it["guidance"]:
        lines += ["## Boss guidance (authoritative answers to earlier questions)", ""]
        lines += [f"- {g['at']}: {g['text']}" for g in it["guidance"]] + [""]
    pending = (it.get("controls") or {}).get("pending", [])
    if pending:
        lines += ["## Boss steering (apply during this run)", ""]
        lines += [f"- {event['at']}: {event['text']}" for event in pending] + [""]
    if it["failure_notes"]:
        lines += ["## Earlier attempts failed — do not repeat these mistakes", ""]
        for n in it["failure_notes"][-2:]:
            lines += [f"### attempt {n['attempt']}", "", "```", n["notes"], "```", ""]
    return "\n".join(lines)


def _pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def budget_blockers(it: dict) -> list[str]:
    """Return fail-closed cumulative budget blockers before any new model turn."""
    if error := budget_state_error(it):
        return [f"budget state is malformed: {error}"]
    usage, budgets = it.get("usage") or {}, it.get("budgets") or {}
    blockers = []
    for key in ("tokens", "cost", "seconds"):
        limit = budgets.get(key)
        if limit is None:
            continue
        if key in ("tokens", "cost") and usage.get(f"{key}_evidence_complete") is not True:
            blockers.append(f"{key} usage evidence is incomplete")
            continue
        actual = usage.get(key)
        if not _valid_usage_value(key, actual):
            blockers.append(f"{key} usage evidence is unavailable")
        elif float(actual) >= float(limit):
            blockers.append(f"{key} budget exhausted ({actual:g}/{limit:g})")
        elif key == "seconds" and float(limit) - float(actual) < 1.0:
            blockers.append(f"seconds budget has less than one bounded second remaining ({actual:g}/{limit:g})")
    return blockers + node_budget_blockers(it)


def _harvest_usage(work_id: str, elapsed: float = 0.0) -> dict:
    """Persist monotonic per-session receipts on every exceptional exit."""
    current = load(work_id)
    identities = []
    implement_node = "scout" if current.get("kind") == "scout" else "implement"
    if isinstance(current.get("session"), dict):
        identities.append(("implementer", current["session"].get("node") or implement_node, current["session"]))
    for historical in current.get("session_history") or []:
        if isinstance(historical, dict):
            identities.append(("implementer", historical.get("node") or implement_node, historical))
    for launch in current.get("agent_launches") or []:
        # Closed reviewers are still billable model sessions. In particular,
        # an invalid verdict/model-drift error may close a settled reviewer
        # before the outer exception path harvests its receipt.
        if (launch.get("role") in {"implementer", "reviewer"}
                and launch.get("agent_session_id") and launch.get("agent_session_path")):
            fallback = implement_node if launch["role"] == "implementer" else None
            identities.append((launch["role"], launch.get("node") or fallback, launch))

    def mutate(item):
        usage = item.setdefault("usage", {})
        receipts = usage.setdefault("receipts", {})
        prior_implementer_tokens = int(usage.get("implementer_tokens") or 0)
        prior_implementer_cost = float(usage.get("implementer_cost") or 0.0)
        prior_reviewer_tokens = int(usage.get("reviewer_tokens") or 0)
        prior_reviewer_cost = float(usage.get("reviewer_cost") or 0.0)
        prior_session = usage.get("implementer_session_id")
        if prior_session and str(prior_session) not in receipts and (prior_implementer_tokens or prior_implementer_cost):
            receipts[str(prior_session)] = {
                "role": "implementer", "tokens": prior_implementer_tokens,
                "cost": prior_implementer_cost,
                "tokens_available": True, "cost_available": True,
            }
        seen = set()
        node_usage = _node_usage_snapshot(item)
        for role, node, identity in identities:
            session_id = str(identity.get("agent_session_id") or "")
            if not session_id or session_id in seen:
                continue
            seen.add(session_id)
            evidence = herdr.usage(identity)
            try:
                started = int(herdr.runtime_activity(identity).get("runtime_input_sequence") or 0) > 0
            except BaseException:
                started = True
            if node in BUDGET_NODES:
                _record_node_session(node_usage, node, identity, evidence, started=started)
            elif role == "reviewer" and started:
                for review_node in ("review_correctness", "review_adversarial"):
                    if review_node in (item.get("node_budgets") or {}):
                        state = _node_state(node_usage, review_node, complete=False)
                        state["tokens_evidence_complete"] = False
                        state["cost_evidence_complete"] = False
            receipt = receipts.setdefault(session_id, {"role": role})
            receipt["role"] = role
            for key in ("tokens", "cost"):
                value = evidence.get(key)
                if _valid_usage_value(key, value):
                    receipt[key] = max(float(receipt.get(key) or 0), float(value))
                    if key == "tokens": receipt[key] = int(receipt[key])
                    receipt[f"{key}_available"] = True
                elif started:
                    receipt[f"{key}_available"] = False
            receipt["harvested_at"] = now()
        measured_implementer_tokens = sum(int(r.get("tokens") or 0) for key, r in receipts.items()
                                          if r.get("role") == "implementer" and key != "legacy-implementer")
        measured_implementer_cost = sum(float(r.get("cost") or 0.0) for key, r in receipts.items()
                                        if r.get("role") == "implementer" and key != "legacy-implementer")
        measured_reviewer_tokens = sum(int(r.get("tokens") or 0) for key, r in receipts.items()
                                       if r.get("role") == "reviewer" and key != "legacy-reviewers")
        measured_reviewer_cost = sum(float(r.get("cost") or 0.0) for key, r in receipts.items()
                                     if r.get("role") == "reviewer" and key != "legacy-reviewers")
        # Older scalar-only records cannot identify which reviewer sessions
        # they covered. Preserve their lower bound without claiming complete
        # evidence; a configured budget will therefore fail closed.
        residual_tokens = max(0, prior_reviewer_tokens - measured_reviewer_tokens)
        residual_cost = max(0.0, prior_reviewer_cost - measured_reviewer_cost)
        if residual_tokens or residual_cost:
            receipts["legacy-reviewers"] = {
                "role": "reviewer", "tokens": residual_tokens, "cost": residual_cost,
                "tokens_available": False, "cost_available": False,
                "harvested_at": now(), "unattributed_legacy": True,
            }
        elif receipts.get("legacy-reviewers", {}).get("unattributed_legacy"):
            receipts.pop("legacy-reviewers", None)
        residual_implementer_tokens = max(0, prior_implementer_tokens - measured_implementer_tokens)
        residual_implementer_cost = max(0.0, prior_implementer_cost - measured_implementer_cost)
        if residual_implementer_tokens or residual_implementer_cost:
            receipts["legacy-implementer"] = {
                "role": "implementer", "tokens": residual_implementer_tokens,
                "cost": residual_implementer_cost, "tokens_available": False,
                "cost_available": False, "harvested_at": now(), "unattributed_legacy": True,
            }
        elif receipts.get("legacy-implementer", {}).get("unattributed_legacy"):
            receipts.pop("legacy-implementer", None)
        token_receipts = [r for r in receipts.values() if r.get("role") != "legacy"]
        legacy_receipts = [r for r in receipts.values() if r.get("role") == "legacy"]
        implementer = [r for r in token_receipts if r.get("role") == "implementer"]
        reviewers = [r for r in token_receipts if r.get("role") == "reviewer"]
        usage["implementer_tokens"] = sum(int(r.get("tokens") or 0) for r in implementer)
        usage["implementer_cost"] = sum(float(r.get("cost") or 0.0) for r in implementer)
        usage["reviewer_tokens"] = sum(int(r.get("tokens") or 0) for r in reviewers)
        usage["reviewer_cost"] = sum(float(r.get("cost") or 0.0) for r in reviewers)
        usage["tokens"] = (sum(int(r.get("tokens") or 0) for r in legacy_receipts) + usage["implementer_tokens"]
                           + usage["reviewer_tokens"])
        usage["cost"] = (sum(float(r.get("cost") or 0.0) for r in legacy_receipts) + usage["implementer_cost"]
                         + usage["reviewer_cost"])
        active = list(receipts.values())
        usage["tokens_evidence_complete"] = all(r.get("tokens_available") is True for r in active)
        usage["cost_evidence_complete"] = all(r.get("cost_available") is True for r in active)
        bounded_elapsed = max(0.0, float(elapsed)) if math.isfinite(float(elapsed)) else 0.0
        if bounded_elapsed:
            lease = item.get("lease") or {}
            receipt_identity = {
                "claim_token": lease.get("claim_token"),
                "process_identity": lease.get("process_identity"),
                "pid": lease.get("pid"),
                "started": lease.get("started"),
            }
            receipt_key = hashlib.sha256(json.dumps(receipt_identity, sort_keys=True,
                                                    separators=(",", ":")).encode()).hexdigest()
            seconds_receipts = usage.setdefault("seconds_receipts", {})
            previous_raw = seconds_receipts.get(receipt_key, 0.0)
            seconds_raw = usage.get("seconds", 0.0)
            if (not _valid_usage_value("seconds", previous_raw)
                    or not _valid_usage_value("seconds", seconds_raw)):
                raise BossError("usage seconds receipt is malformed; run pi-boss doctor")
            previous_elapsed = float(previous_raw)
            usage["seconds"] = (float(seconds_raw)
                                + max(0.0, bounded_elapsed - previous_elapsed))
            seconds_receipts[receipt_key] = max(previous_elapsed, bounded_elapsed)
        activity = item.get("activity") or {}
        active_node = activity.get("node")
        if active_node in BUDGET_NODES and bounded_elapsed > 0:
            raw_started = activity.get("node_started_at")
            complete = True
            try:
                started_epoch = dt.datetime.fromisoformat(str(raw_started).replace("Z", "+00:00")).timestamp()
                state = _node_state(node_usage, active_node)
                raw_accounted = state.get("seconds_accounted_at")
                accounted_epoch = (dt.datetime.fromisoformat(str(raw_accounted).replace("Z", "+00:00")).timestamp()
                                   if raw_accounted else started_epoch)
                current_epoch = time.time()
                if started_epoch > current_epoch + 1 or accounted_epoch > current_epoch + 1:
                    raise ValueError("node accounting clock moved backwards")
                anchor = max(started_epoch, accounted_epoch)
                node_elapsed = min(bounded_elapsed, max(0.0, current_epoch - anchor))
            except (TypeError, ValueError, OverflowError):
                node_elapsed, complete = bounded_elapsed, False
            _record_node_seconds(node_usage, active_node, node_elapsed, complete=complete)
        elif bounded_elapsed > 0 and item.get("node_budgets"):
            for node, limits in item["node_budgets"].items():
                if (limits or {}).get("seconds") is not None:
                    _node_state(node_usage, node)["seconds_evidence_complete"] = False
        item["node_usage"] = node_usage
    return control.cas_update(work_id, mutate)


def claim_next(owner: str) -> dict | None:
    """Claim the oldest queued item whose durable scope is mechanically disjoint."""
    with locked(home() / "claim.lock"):
        items = all_items()
        for it in items:
            if it["status"] == "queued" and not (it.get("controls") or {}).get("paused"):
                declared = (it.get("scope") or {}).get("paths") or ["unknown"]
                paths = ["global"] if (it.get("scope") or {}).get("claim") == "global" else declared
                identity = processes.capture(os.getpid(), owner)
                if not identity:
                    raise BossError("worker cannot capture its exact process identity; refusing a lease")
                recovery_token = it.get("recovery_claim_token")
                claim_token = scope.claim(it["project"], it["id"], paths, owner, os.getpid(), identity,
                                          replace_token=recovery_token)
                if not claim_token:
                    continue
                it.pop("recovery_claim_token", None)
                it["lease"] = {"owner": owner, "started": now(), "pid": os.getpid(), "scope": paths,
                               "process_identity": identity, "claim_token": claim_token,
                               "recovery_attempt": bool(recovery_token),
                               "replaced_recovery_claim_token": recovery_token}
                it["phase"] = "implementing"
                try:
                    transition(it, "running", f"leased by {owner}")
                except BaseException:
                    if recovery_token:
                        # The hold was exchanged before the item CAS. Never
                        # delete collision protection merely because that CAS
                        # or the runner died in this narrow window. Pin the new
                        # token back onto the item when possible; otherwise the
                        # still-held claim remains visible to doctor.
                        scope.hold(it["id"], claim_token,
                                   "recovery lease transition did not durably complete")
                        try:
                            def preserve(current):
                                if current.get("recovery_claim_token") != recovery_token:
                                    raise BossError("recovery ownership changed during failed lease transition")
                                current["recovery_claim_token"] = claim_token
                                current.pop("lease", None); current["status"] = "paused"
                                current["phase"] = "recovery-required"
                                current.setdefault("controls", {})["paused"] = True
                            control.cas_update(it["id"], preserve)
                        except BaseException:
                            pass
                    else:
                        scope.release(it["id"], claim_token)
                    raise
                return it
    return None


def execute(it: dict, timeout: int = 3600) -> dict:
    """Run one attempt; retain the claim only while an unsettled agent may still mutate."""
    hold_claim = False
    claim_token = (it.get("lease") or {}).get("claim_token")
    recovery_attempt = bool((it.get("lease") or {}).get("recovery_attempt"))
    started = time.monotonic()
    def retain_recovery(reason: str, question: str) -> dict:
        nonlocal hold_claim
        hold_claim = True
        current = load(it["id"])
        current["recovery_claim_token"] = claim_token
        current.pop("lease", None); current["phase"] = "recovery-required"
        current.setdefault("controls", {})["paused"] = True
        current["ask"] = {"question": question, "context": reason}
        transition(current, "paused", "recovery attempt stopped; exact scope retained")
        if not scope.hold(current["id"], claim_token, reason):
            raise BossError("recovery stopped and exact scope ownership could not be retained")
        return current
    try:
        current = load(it["id"])
        if gates.has_open_transaction(current):
            project = registry.get(current["project"])
            tx = current.get("external_gate") or {}
            wt = Path(str(tx.get("worktree_path") or current.get("worktree") or ""))
            return gates.start_or_reconcile(current, project, wt)
        if deliver.has_resumable_pr_delivery(current):
            project = registry.get(current["project"])
            return deliver.resume_pr_delivery(current, project)
        blockers = budget_blockers(current)
        if blockers:
            if recovery_attempt:
                return retain_recovery("; ".join(blockers),
                                       "Recovery cannot start another model turn until its budget is raised or reconciled.")
            current = load(it["id"])
            current.pop("lease", None); current["phase"] = "paused"
            current.setdefault("controls", {})["paused"] = True
            current["ask"] = {"question": "The item cannot start another model turn until its budget is raised or reconciled.",
                              "context": "; ".join(blockers)}
            transition(current, "paused", "budget gate stopped execution before agent creation")
            return current
        return _execute(it, timeout)
    except herdr.UnsettledAgentError as e:
        try: _harvest_usage(it["id"], time.monotonic() - started)
        except BaseException: pass
        retain_recovery(str(e), "The agent did not prove it stopped. Inspect the live Herdr tab before recovery.")
        raise
    except BaseException as e:          # includes BossError (a SystemExit) and KeyboardInterrupt
        try: _harvest_usage(it["id"], time.monotonic() - started)
        except BaseException: pass
        if recovery_attempt:
            retain_recovery(str(getattr(e, "msg", None) or e),
                            "Recovery did not complete. Inspect the preserved agent/worktree evidence before trying again.")
            raise
        it = load(it["id"])
        if it["status"] == "running":
            it.pop("lease", None); it["phase"] = "failed"
            transition(it, "failed", f"attempt crashed: {getattr(e, 'msg', None) or e!r}")
        raise
    finally:
        if not hold_claim:
            scope.release(it["id"], claim_token)


def _model_drift(expected: str, thinking: str, agent: dict, *, require_durable: bool = True) -> None:
    herdr.validate_agent(agent, expected, thinking, require_durable=require_durable)


def _mark_activity(work_id: str, phase: str, *, node: str | None = None) -> dict:
    def mark(item):
        item["phase"] = phase
        item["activity"] = {"last": now(), "state": phase}
        if node:
            item["activity"].update(node=node, node_started_at=now())
    return control.cas_update(work_id, mark)


def _json_verdict(text: str) -> dict:
    decoder = json.JSONDecoder()
    found = []
    for pos, char in enumerate(text):
        if char == "{":
            try:
                value, _ = decoder.raw_decode(text[pos:])
                if isinstance(value, dict) and value.get("verdict") in ("accept", "reject"):
                    found.append(value)
            except json.JSONDecodeError:
                pass
    if not found:
        raise BossError("reviewer produced no parseable verdict; approval was not inferred")
    return found[-1]


def _failure_signature(summary: dict, notes: str) -> str:
    """Deduplicate failure meaning without volatile run/session/reviewer evidence."""
    import hashlib
    text = str(summary.get("error") or notes)
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    text = re.sub(r"\b\d{4}-\d\d-\d\d[T ][0-9:.+-]+Z?\b", "<time>", text)
    text = re.sub(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", "<uuid>", text, flags=re.I)
    text = re.sub(r"\b[0-9a-f]{40,64}\b", "<sha>", text, flags=re.I)
    text = re.sub(r"(?:^|\s)/(?:[^\s:]+/)*(?:runs|agent-sessions|agent-attestations)/[^\s:]+", " <run>", text)
    text = re.sub(r"\b(?:pid|process|review-[a-z0-9_-]+)\s*[=: ]\s*\d{3,}\b", "<volatile-id>", text, flags=re.I)
    text = " ".join(text.split())[:4000]
    stable = {"failed_ids": sorted(set(summary.get("failed_ids") or [])),
              "scope_escape": sorted(set(summary.get("scope_escape") or [])), "meaning": text}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def deliver_steering(work_id: str, event_id: str, text: str, session: dict, owner: str) -> bool:
    """Deliver one steering event with a CAS lease and Pi-ledger crash reconciliation."""
    baseline = int(herdr.runtime_activity(session).get("runtime_input_sequence", 0))
    reserved = control.begin_delivery(work_id, event_id, owner, baseline)
    if not reserved.pop("_delivery_acquired", False):
        return False
    event = next(value for value in reserved.get("controls", {}).get("events", []) if value.get("id") == event_id)
    baseline = int(event.get("delivery_baseline_sequence", baseline))
    accepted = herdr.accepted_input(session, "STEERING FROM THE BOSS:\n" + text, baseline)
    if not accepted:
        try:
            submitted = herdr.steer_agent(session["agent_name"], text, identity=session)
        except BaseException:
            # Herdr may fail after Pi accepted input. Leave the bounded lease in
            # place; the next owner checks the hash-only ledger before retrying.
            raise
        sequence = submitted.get("_runtime_input_sequence")
        accepted = ({"input_sequence": sequence} if sequence else
                    herdr.accepted_input(session, "STEERING FROM THE BOSS:\n" + text, baseline))
    if not accepted or not accepted.get("input_sequence"):
        raise BossError("Pi accepted no durable steering input; delivery remains reserved for reconciliation")
    herdr.record_runtime_anchor(session)
    control.complete_delivery(work_id, event_id, owner, int(accepted["input_sequence"]))
    return True


def _persistent_execute(it: dict, project: dict, wt: Path, brief: Path, timeout: int,
                        agent_env: dict[str, str], *, attempt_started: float,
                        deadline: float | None) -> dict:
    """Use one real reconnectable Herdr implementer and fresh independent reviewers."""
    decision = it["model_decision"]
    implement_node = "scout" if it["kind"] == "scout" else "implement"
    model = decision["models"][implement_node]
    thinking = decision["thinking"][implement_node]
    dispatch.assert_available(it["dispatch"])
    session = herdr.ensure_agent(it, wt, model, thinking, agent_env=agent_env, node=implement_node)
    _model_drift(model, thinking, session, require_durable=False)
    current = load(it["id"])
    previous = current.get("session")
    recovered_checkpoint = current.get("checkpoint") or {}
    same_session = bool(previous and previous.get("agent_session_id") == session.get("agent_session_id"))
    if previous and not same_session and not session.get("reconnected"):
        current.setdefault("session_history", []).append({**previous, "lost_at": now(), "recovered_from_checkpoint": True})
        authorization = current.get("recovery_authorized") or {}
        if authorization.get("session_id") == previous.get("agent_session_id") and not authorization.get("consumed_at"):
            authorization.update(consumed_at=now(), replacement_session_id=session.get("agent_session_id"))
            current["recovery_authorized"] = authorization
    elif not same_session:
        authorization = current.get("recovery_authorized") or {}
        if authorization and not authorization.get("consumed_at"):
            authorization.update(consumed_at=now(), outcome="live-reconnect" if session.get("reconnected") else "initial-session")
            current["recovery_authorized"] = authorization
    prior_activity = dict(current.get("activity") or {})
    current["session"] = session; current["phase"] = "investigating" if it["kind"] == "scout" else "implementing"
    current["activity"] = {"last": now(), "heartbeat": now(), "state": current["phase"],
                           "node": implement_node, "node_started_at": now(),
                           "progress_marker": herdr.progress_marker(session)}
    current["checkpoint"] = {"sha": git(wt, "rev-parse", "HEAD"), "at": now(),
                             "phase": current["phase"], "prior": recovered_checkpoint,
                             "scope": current.get("changed_scope", []),
                             "recovery": "live-reconnect" if session.get("reconnected") else "checkpoint-fallback"}
    save(current); it = current
    prior_usage = it.get("usage") or {}
    node_usage = _node_usage_snapshot(it)
    session_id = session.get("agent_session_id")
    def aggregate_usage(implementer: dict, reviewer_tokens: int = 0,
                        reviewer_cost: float = 0.0, *, reviewer_tokens_complete: bool = True,
                        reviewer_cost_complete: bool = True) -> dict:
        measured_tokens = int(implementer.get("tokens") or 0)
        measured_cost = float(implementer.get("cost") or 0.0)
        if prior_usage.get("implementer_session_id") == session_id:
            implementer_tokens = max(int(prior_usage.get("implementer_tokens") or 0), measured_tokens)
            implementer_cost = max(float(prior_usage.get("implementer_cost") or 0.0), measured_cost)
        else:
            implementer_tokens = int(prior_usage.get("implementer_tokens") or 0) + measured_tokens
            implementer_cost = float(prior_usage.get("implementer_cost") or 0.0) + measured_cost
        all_reviewer_tokens = int(prior_usage.get("reviewer_tokens") or 0) + reviewer_tokens
        all_reviewer_cost = float(prior_usage.get("reviewer_cost") or 0.0) + reviewer_cost
        return {"tokens": implementer_tokens + all_reviewer_tokens,
                "cost": implementer_cost + all_reviewer_cost,
                "implementer_tokens": implementer_tokens, "implementer_cost": implementer_cost,
                "reviewer_tokens": all_reviewer_tokens, "reviewer_cost": all_reviewer_cost,
                "implementer_session_id": session_id, "usage_kind": "session-cumulative",
                "tokens_evidence_complete": ("tokens" in implementer and reviewer_tokens_complete),
                "cost_evidence_complete": ("cost" in implementer and reviewer_cost_complete),
                "node_usage": copy.deepcopy(node_usage)}

    initial_evidence = herdr.usage(session)
    try:
        initial_activity = herdr.runtime_activity(session)
        settled_turns = int(initial_activity.get("runtime_settled_sequence") or 0)
    except BaseException:
        settled_turns = 1
    _record_node_session(node_usage, implement_node, session, initial_evidence, started=settled_turns > 0)
    if session.get("reconnected") and (session.get("runtime_turn_pending") or
                                       (session.get("agent_status") or session.get("state")) == "working"):
        raw_started = (prior_activity.get("node_started_at") if prior_activity.get("node") == implement_node
                       else _node_state(node_usage, implement_node).get("seconds_accounted_at"))
        try:
            started_epoch = dt.datetime.fromisoformat(str(raw_started).replace("Z", "+00:00")).timestamp()
            _record_node_seconds(node_usage, implement_node, max(0.0, time.time() - started_epoch))
        except (TypeError, ValueError):
            _node_state(node_usage, implement_node)["seconds_evidence_complete"] = False
    missing_initial = _missing_settled_usage(
        it.get("budgets") or {}, initial_evidence, settled_turns,
        "unavailable for the existing session")
    initial_usage = aggregate_usage(initial_evidence)
    missing_initial += node_budget_blockers(it, implement_node, node_usage=node_usage)
    it["node_usage"] = copy.deepcopy(node_usage)
    save(it)
    if missing_initial:
        run_dir = item_dir(it["id"]) / "runs" / f"herdr-{int(time.time() * 1000)}"
        run_dir.mkdir(parents=True, exist_ok=False)
        return {"ok": False, "run_dir": str(run_dir), "failed_ids": [], "control": "budget",
                "budget_exceeded": "; ".join(missing_initial), "agent_checkpoint": "no-model-turn-started",
                "error": "usage evidence is fail-closed before prompt", "reviews": [], **initial_usage}
    exhausted = [f"{key} budget already exhausted ({initial_usage[key]:g}/{limit:g})"
                 for key in ("tokens", "cost")
                 if (limit := (it.get("budgets") or {}).get(key)) is not None and initial_usage[key] >= limit]
    if exhausted:
        run_dir = item_dir(it["id"]) / "runs" / f"herdr-{int(time.time() * 1000)}"
        run_dir.mkdir(parents=True, exist_ok=False)
        return {"ok": False, "run_dir": str(run_dir), "failed_ids": [], "control": "budget",
                "budget_exceeded": "; ".join(exhausted), "agent_checkpoint": "no-model-turn-started",
                "error": "budget threshold reached before prompt", "reviews": [], **initial_usage}
    checkpoint = recovered_checkpoint or it.get("checkpoint") or {}
    turn_head = git(wt, "rev-parse", "HEAD")
    if it["kind"] == "scout":
        prompt = ("Investigate this repository read-only. Do not modify files or commit. Return a concise Markdown report with file/line evidence.\n\n"
                  + brief.read_text())
    else:
        prompt = (f"Continue work in the owned worktree {wt}. Run `{project.get('test_cmd') or 'true'}`. "
                  "Edit files, but do not run git add/commit/rebase: the controller will checkpoint only after validating your scope. "
                  f"Never touch protected paths: {project.get('protected_paths')}. If a decision is required, write .boss-ask.json and stop. "
                  f"Checkpoint SHA before this turn: {checkpoint.get('sha')}.\n\n" + brief.read_text())
    event_states = {event.get("id"): event.get("state") for event in (it.get("controls") or {}).get("events", [])}
    pending = [event for event in (it.get("controls") or {}).get("pending", [])
               if event_states.get(event.get("id"), "pending") == "pending"]
    baseline_changed = set(worktree.changed_files(project, wt))
    monitor_started = time.monotonic(); last_heartbeat = [0.0]
    reconnecting_active = session.get("reconnected") and (
        session.get("runtime_turn_pending") or
        (session.get("agent_status") or session.get("state")) == "working"
    )
    delivered_controls: set[str] = set() if reconnecting_active else {p["id"] for p in pending}
    def live_escape():
        live_item = load(it["id"])
        controls = live_item.get("controls") or {}
        elapsed = time.monotonic() - monitor_started
        # Steering submitted while this turn is active reaches the durable agent
        # immediately and is acknowledged exactly once only after Herdr accepts it.
        for event in controls.get("pending", []):
            event_id = event.get("id")
            if event_id and event_id not in delivered_controls:
                if deliver_steering(it["id"], event_id, event["text"], session,
                                    f"runner:{os.getpid()}"):
                    delivered_controls.add(event_id)
        if elapsed - last_heartbeat[0] >= 5:
            live_agent = herdr.agent_get(session["agent_name"], session)
            measured = herdr.usage(live_agent)
            live_settled = int((live_agent or {}).get("runtime_settled_sequence") or 0)
            marker = herdr.progress_marker(live_agent or session)
            def beat(item):
                activity = item.setdefault("activity", {})
                activity.update(heartbeat=now(), state="working", elapsed_seconds=int(elapsed), **measured)
                if activity.get("progress_marker") != marker:
                    activity.update(last=now(), progress_marker=marker)
            control.cas_update(it["id"], beat); last_heartbeat[0] = elapsed
            total_usage = aggregate_usage(measured)
            missing_live = _missing_settled_usage(
                live_item.get("budgets") or {}, measured, live_settled, "became unavailable")
            if missing_live:
                return {"control": "budget", "reason": "; ".join(missing_live)}
            for key in ("tokens", "cost"):
                limit, actual = (live_item.get("budgets") or {}).get(key), total_usage.get(key)
                if limit is not None and actual is not None and actual >= limit:
                    return {"control": "budget", "reason": f"{key} budget reached ({actual:g}/{limit:g})"}
            live_node_usage = copy.deepcopy(node_usage)
            _record_node_session(live_node_usage, implement_node, session, measured,
                                 started=live_settled > 0)
            _record_node_seconds(live_node_usage, implement_node, elapsed)
            if node_blocked := node_budget_blockers(it, implement_node, node_usage=live_node_usage):
                return {"control": "budget", "reason": "; ".join(node_blocked)}
        elapsed_total = float(prior_usage.get("seconds") or 0.0) + (time.monotonic() - attempt_started)
        if (live_item.get("budgets") or {}).get("seconds") and elapsed_total >= live_item["budgets"]["seconds"]:
            return {"control": "budget", "reason": "time budget reached"}
        if controls.get("pause_requested") or controls.get("interrupt_requested"):
            return {"control": "interrupt" if controls.get("interrupt_requested") else "pause"}
        changed = set(worktree.changed_files(project, wt))
        changed.update(worktree.status_paths(wt, allow_ask=True))
        changed = sorted(changed - baseline_changed)
        sensitive = [p for p in changed if any(fnmatch.fnmatch(p, pattern) for pattern in project.get("protected_paths", []))]
        return sorted(set(sensitive + scope.escaped((it.get("scope") or {}).get("paths"), changed)))
    if reconnecting_active:
        agent, escaped_live = herdr.wait_agent_monitored(
            session["agent_name"], max(1, _node_timeout(it, node_usage, implement_node, deadline, timeout)),
            live_escape, session)
    else:
        turn_timeout = _node_timeout(it, node_usage, implement_node, deadline, timeout)
        if not turn_timeout:
            run_dir = item_dir(it["id"]) / "runs" / f"herdr-{int(time.time() * 1000)}"
            run_dir.mkdir(parents=True, exist_ok=False)
            return {"ok": False, "run_dir": str(run_dir), "failed_ids": [], "control": "budget",
                    "budget_exceeded": "seconds budget has less than one bounded second remaining",
                    "agent_checkpoint": "no-model-turn-started", "error": "time budget reached",
                    "reviews": [], **initial_usage}
        agent, escaped_live = herdr.prompt_agent_monitored(session["agent_name"], prompt, turn_timeout, live_escape, session)
        if pending:
            control.consume(it["id"], [p["id"] for p in pending], "delivered")
    agent = herdr.agent_get(session["agent_name"], session) or {**session, **agent}
    _model_drift(model, thinking, agent)
    turn_evidence = herdr.usage(agent)
    _record_node_session(node_usage, implement_node, session, turn_evidence, started=True)
    _record_node_seconds(node_usage, implement_node, time.monotonic() - monitor_started)
    _persist_node_usage(it["id"], node_usage)
    turn_usage = aggregate_usage(turn_evidence)
    missing_turn = [f"{key} usage evidence unavailable after implementer turn"
                    for key in ("tokens", "cost")
                    if (it.get("budgets") or {}).get(key) is not None and key not in turn_evidence]
    missing_turn += [finding for finding in _node_budget_findings(
        it.get("node_budgets") or {}, node_usage, implement_node, exhausted=False)
        if "evidence" in finding]
    if missing_turn:
        run_dir = item_dir(it["id"]) / "runs" / f"herdr-{int(time.time() * 1000)}"
        run_dir.mkdir(parents=True, exist_ok=False)
        return {"ok": False, "run_dir": str(run_dir), "failed_ids": [], "control": "budget",
                "budget_exceeded": "; ".join(missing_turn), "agent_checkpoint": "turn-settled",
                "error": "usage evidence is fail-closed after prompt", **turn_usage}
    if isinstance(escaped_live, dict) and escaped_live.get("control"):
        run_dir = item_dir(it["id"]) / "runs" / f"herdr-{int(time.time() * 1000)}"
        run_dir.mkdir(parents=True, exist_ok=False)
        return {"ok": False, "run_dir": str(run_dir), "failed_ids": [], "control": escaped_live["control"],
                "budget_exceeded": escaped_live.get("reason"), "agent_checkpoint": escaped_live.get("agent_checkpoint"),
                "error": "cooperative control checkpoint", **turn_usage}
    node_crossed = _node_budget_findings(it.get("node_budgets") or {}, node_usage,
                                         implement_node, exhausted=False)
    if node_crossed:
        run_dir = item_dir(it["id"]) / "runs" / f"herdr-{int(time.time() * 1000)}"
        run_dir.mkdir(parents=True, exist_ok=False)
        return {"ok": False, "run_dir": str(run_dir), "failed_ids": [], "control": "budget",
                "budget_exceeded": "; ".join(node_crossed), "agent_checkpoint": "turn-settled",
                "error": "node budget threshold reached", **turn_usage}
    if escaped_live:
        run_dir = item_dir(it["id"]) / "runs" / f"herdr-{int(time.time() * 1000)}"
        run_dir.mkdir(parents=True, exist_ok=False)
        protected_live = [p for p in escaped_live if any(fnmatch.fnmatch(p, pattern) for pattern in project.get("protected_paths", []))]
        if protected_live:
            return {"ok": False, "run_dir": str(run_dir), "failed_ids": ["protected"],
                    "error": "live protected-path change interrupted: " + ", ".join(protected_live),
                    "changed": escaped_live, **turn_usage}
        return {"ok": False, "run_dir": str(run_dir), "failed_ids": ["scope-escape"], "scope_escape": escaped_live,
                "error": "live scope escape interrupted: " + ", ".join(escaped_live), **turn_usage}
    output = herdr.agent_read(session["agent_name"], 240)
    run_dir = item_dir(it["id"]) / "runs" / f"herdr-{int(time.time() * 1000)}"
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "implementer.md").write_text(output)
    if it["kind"] == "scout":
        (item_dir(it["id"]) / "report.md").write_text(output)
        measured = herdr.usage(agent)
        return {"ok": True, "run_dir": str(run_dir), "failed_ids": [], **aggregate_usage(measured), "reviews": []}
    if git(wt, "rev-parse", "HEAD", check=False) != turn_head:
        return {"ok": False, "run_dir": str(run_dir), "failed_ids": ["sandbox-boundary"],
                "error": "managed implementer changed Git metadata despite the write sandbox; work preserved", **turn_usage}
    dirty_before_checkpoint = worktree.status_paths(wt, allow_ask=True)
    candidate_changed = sorted(set(worktree.changed_files(project, wt) + dirty_before_checkpoint))
    protected_candidate = [p for p in candidate_changed
                           if any(fnmatch.fnmatch(p, pattern) for pattern in project.get("protected_paths", []))]
    escaped_candidate = scope.escaped((it.get("scope") or {}).get("paths"), candidate_changed)
    if protected_candidate:
        return {"ok": False, "run_dir": str(run_dir), "failed_ids": ["protected"],
                "error": "protected paths changed before controller checkpoint: " + ", ".join(protected_candidate),
                "changed": candidate_changed, **turn_usage}
    if escaped_candidate:
        return {"ok": False, "run_dir": str(run_dir), "failed_ids": ["scope-escape"],
                "scope_escape": escaped_candidate,
                "error": "changes escaped declared scope before controller checkpoint: " + ", ".join(escaped_candidate),
                "changed": candidate_changed, **turn_usage}
    if dirty_before_checkpoint:
        add = sh(["git", "-C", str(wt), "add", "-A", "--",
                  *worktree.checkpoint_pathspecs(wt)],
                 check=False, env=agent_env)
        if add.returncode:
            return {"ok": False, "run_dir": str(run_dir), "failed_ids": ["checkpoint"],
                    "error": "controller could not stage the validated worktree: " + add.stderr[-1000:], **turn_usage}
        commit = sh(["git", "-C", str(wt), "-c", "user.name=BOSS Checkpoint",
                     "-c", "user.email=boss@local.invalid", "commit", "-m",
                     f"boss: checkpoint {it['id']}"], check=False, env=agent_env)
        if commit.returncode:
            return {"ok": False, "run_dir": str(run_dir), "failed_ids": ["checkpoint"],
                    "error": "controller could not commit validated changes: " + commit.stderr[-1000:], **turn_usage}
    if (wt / ".boss-ask.json").exists():
        measured = herdr.usage(agent)
        return {"ok": False, "run_dir": str(run_dir), "failed_ids": ["question"], **aggregate_usage(measured)}
    if not worktree.has_commits(project, wt):
        return {"ok": False, "run_dir": str(run_dir), "failed_ids": ["implement"],
                "error": "implementer produced no commit", **turn_usage}
    changed = worktree.changed_files(project, wt)
    protected = [p for p in changed if any(fnmatch.fnmatch(p, pattern) for pattern in project.get("protected_paths", []))]
    if protected:
        return {"ok": False, "run_dir": str(run_dir), "failed_ids": ["protected"],
                "error": "protected paths changed: " + ", ".join(protected), "changed": changed, **turn_usage}
    escaped = scope.escaped((it.get("scope") or {}).get("paths"), changed)
    if escaped:
        return {"ok": False, "run_dir": str(run_dir), "failed_ids": ["scope-escape"], "scope_escape": escaped,
                "error": "changed files escaped declared scope: " + ", ".join(escaped), **turn_usage}
    # Integrate the latest configured local base before final verification and review.
    base_sha = git(project["path"], "rev-parse", project["base"])
    if git(wt, "merge-base", base_sha, "HEAD", check=False) != base_sha:
        r = sh(["git", "-C", str(wt), "rebase", base_sha], check=False)
        if r.returncode != 0:
            sh(["git", "-C", str(wt), "rebase", "--abort"], check=False)
            return {"ok": False, "run_dir": str(run_dir), "failed_ids": ["base-integration"],
                    "error": r.stderr[-2000:], **turn_usage}
    _mark_activity(it["id"], "verifying", node="verify")
    verify_blocked = node_budget_blockers(it, "verify", node_usage=node_usage)
    verify_timeout = _node_timeout(it, node_usage, "verify", deadline, timeout)
    if verify_blocked or not verify_timeout:
        return {"ok": False, "run_dir": str(run_dir), "failed_ids": [], "control": "budget",
                "budget_exceeded": "; ".join(verify_blocked) or "seconds budget reached before verification",
                "agent_checkpoint": "turn-settled", "error": "time budget reached", **turn_usage}
    verify_started = time.monotonic()
    verify = sandbox.run_verification(work_id=it["id"], phase="pre-review", cwd=wt,
                                      command=project.get("test_cmd") or "true",
                                      timeout=verify_timeout, env=agent_env)
    _record_node_seconds(node_usage, "verify", time.monotonic() - verify_started)
    _persist_node_usage(it["id"], node_usage)
    turn_usage = aggregate_usage(herdr.usage(agent))
    (run_dir / "verify.md").write_text(verify.stdout + verify.stderr)
    verify_crossed = _node_budget_findings(it.get("node_budgets") or {}, node_usage,
                                            "verify", exhausted=False)
    if verify.returncode:
        return {"ok": False, "run_dir": str(run_dir), "failed_ids": ["verify"],
                "control": "budget" if verify_crossed else None,
                "budget_exceeded": "; ".join(verify_crossed) if verify_crossed else None,
                "error": (verify.stdout + verify.stderr)[-3000:], **turn_usage}
    if verify_crossed:
        return {"ok": False, "run_dir": str(run_dir), "failed_ids": [], "control": "budget",
                "budget_exceeded": "; ".join(verify_crossed), "agent_checkpoint": "turn-settled",
                "error": "verify node budget threshold reached", **turn_usage}
    if git(project["path"], "rev-parse", project["base"], check=False) != base_sha:
        return {"ok": False, "run_dir": str(run_dir), "failed_ids": ["base-moved"],
                "error": "configured base moved during verification; reviews were not started", **turn_usage}
    sha = git(wt, "rev-parse", "HEAD")
    reviews = []
    implementer_measured = herdr.usage(agent)
    reviewer_tokens = 0
    reviewer_cost = 0.0
    reviewer_tokens_complete = True
    reviewer_cost_complete = True
    if it["dispatch"]["graph"] == "direct-pr" or modes.high_assurance(it["dispatch"]["graph"]):
        diff = git(wt, "diff", f"{base_sha}...{sha}")
        diff_bytes = len(diff.encode())
        if diff_bytes > MAX_REVIEW_DIFF_BYTES:
            return {"ok": False, "run_dir": str(run_dir), "failed_ids": ["review-input-too-large"],
                    "error": f"complete review diff is {diff_bytes} bytes; maximum is {MAX_REVIEW_DIFF_BYTES}; no truncated review was run",
                    **turn_usage}
        roles = ["correctness"] + (["adversarial"] if modes.high_assurance(it["dispatch"]["graph"]) else [])
        for role_index, role in enumerate(roles):
            phase = "review_" + role
            before_review = aggregate_usage(implementer_measured, reviewer_tokens, reviewer_cost,
                                            reviewer_tokens_complete=reviewer_tokens_complete,
                                            reviewer_cost_complete=reviewer_cost_complete)
            blocked = [f"{key} budget reached ({before_review[key]:g}/{limit:g})"
                       for key in ("tokens", "cost")
                       if (limit := (it.get("budgets") or {}).get(key)) is not None and before_review[key] >= limit]
            blocked += [f"{key} usage evidence is incomplete"
                        for key in ("tokens", "cost")
                        if (it.get("budgets") or {}).get(key) is not None
                        and before_review.get(f"{key}_evidence_complete") is not True]
            blocked += node_budget_blockers(it, phase, node_usage=node_usage)
            if not _remaining_timeout(deadline, timeout):
                blocked.append("seconds budget has less than one bounded second remaining")
            if blocked:
                return {"ok": False, "run_dir": str(run_dir), "failed_ids": [], "control": "budget",
                        "budget_exceeded": "; ".join(blocked), "agent_checkpoint": "no-review-turn-started",
                        "error": "budget threshold reached before reviewer", "reviews": reviews, **before_review}
            _mark_activity(it["id"], phase.replace("_", "-"), node=phase)
            verdict_file = run_dir / f"review_{role}.pending.json"
            reviewer_env = {**agent_env, "BOSS_AGENT_ALLOWED_WRITES": str(verdict_file.resolve())}
            reviewer = herdr.ensure_agent(it, wt, decision["models"][phase], decision["thinking"][phase],
                                          reviewer=True, agent_env=reviewer_env, node=phase)
            reviewer_settled = False
            try:
                _model_drift(decision["models"][phase], decision["thinking"][phase], reviewer,
                             require_durable=False)
                review_timeout = _node_timeout(it, node_usage, phase, deadline, timeout)
                if not review_timeout:
                    return {"ok": False, "run_dir": str(run_dir), "failed_ids": [], "control": "budget",
                            "budget_exceeded": "seconds budget reached before reviewer input",
                            "agent_checkpoint": "no-review-turn-started", "error": "time budget reached",
                            "reviews": reviews, **before_review}
                review_started = time.monotonic()
                herdr.prompt_agent(reviewer["agent_name"],
                    f"You are an independent {role} reviewer. Review exact base {base_sha} and commit {sha}. "
                    f"Do not modify the repository. Write genuine JSON "
                    f"{{\"verdict\":\"accept\" or \"reject\",\"notes\":\"...\",\"sha\":\"{sha}\","
                    f"\"base_sha\":\"{base_sha}\"}} to "
                    f"{verdict_file}, then reply with that path.\n\n" + brief.read_text() + "\n\nDIFF:\n" + diff,
                    review_timeout, reviewer)
                reviewer_settled = True
                live_reviewer = herdr.agent_get(reviewer["agent_name"], reviewer)
                if not live_reviewer:
                    raise BossError("reviewer session disappeared before its verdict was captured")
                _model_drift(decision["models"][phase], decision["thinking"][phase], live_reviewer)
                reviewer_usage = herdr.usage(live_reviewer)
                _record_node_session(node_usage, phase, reviewer, reviewer_usage, started=True)
                _record_node_seconds(node_usage, phase, time.monotonic() - review_started)
                _persist_node_usage(it["id"], node_usage)
                reviewer_tokens_complete = reviewer_tokens_complete and "tokens" in reviewer_usage
                reviewer_cost_complete = reviewer_cost_complete and "cost" in reviewer_usage
                reviewer_tokens += int(reviewer_usage.get("tokens") or 0)
                reviewer_cost += float(reviewer_usage.get("cost") or 0.0)
                missing_reviewer = [f"{key} usage evidence unavailable after {role} reviewer"
                                    for key in ("tokens", "cost")
                                    if (it.get("budgets") or {}).get(key) is not None
                                    and key not in reviewer_usage]
                missing_reviewer += [finding for finding in _node_budget_findings(
                    it.get("node_budgets") or {}, node_usage, phase, exhausted=False)
                    if "evidence" in finding]
                if missing_reviewer:
                    return {"ok": False, "run_dir": str(run_dir), "failed_ids": [], "control": "budget",
                            "budget_exceeded": "; ".join(missing_reviewer),
                            "agent_checkpoint": "review-turn-settled",
                            "error": "reviewer usage evidence is fail-closed", "reviews": reviews,
                            **aggregate_usage(implementer_measured, reviewer_tokens, reviewer_cost,
                                              reviewer_tokens_complete=reviewer_tokens_complete,
                                              reviewer_cost_complete=reviewer_cost_complete)}
                evidence = herdr.agent_read(reviewer["agent_name"], 240)
                try:
                    verdict = json.loads(verdict_file.read_text())
                except (OSError, json.JSONDecodeError):
                    verdict = _json_verdict(evidence)
                if (verdict.get("verdict") not in ("accept", "reject") or verdict.get("sha") != sha
                        or verdict.get("base_sha") != base_sha):
                    raise BossError("reviewer evidence has no valid verdict bound to the requested base/head SHAs")
                rec = {**verdict, "sha": sha, "base_sha": base_sha, "role": role, "reviewer": reviewer, "fresh": True,
                       "valid": True, "evidence": evidence[-12000:], "usage": reviewer_usage, "at": now()}
                reviews.append(rec); (run_dir / f"review_{role}.json").write_text(json.dumps(rec, indent=2))
                if verdict["verdict"] != "accept":
                    return {"ok": False, "run_dir": str(run_dir), "failed_ids": [phase], "reviews": reviews,
                            **aggregate_usage(implementer_measured, reviewer_tokens, reviewer_cost,
                                              reviewer_tokens_complete=reviewer_tokens_complete,
                                              reviewer_cost_complete=reviewer_cost_complete)}
                after_review = aggregate_usage(implementer_measured, reviewer_tokens, reviewer_cost,
                                               reviewer_tokens_complete=reviewer_tokens_complete,
                                               reviewer_cost_complete=reviewer_cost_complete)
                node_exceeded = _node_budget_findings(it.get("node_budgets") or {}, node_usage,
                                                       phase, exhausted=False)
                exceeded = [f"{key} budget exceeded ({after_review[key]:g}/{limit:g})"
                            for key in ("tokens", "cost")
                            if (limit := (it.get("budgets") or {}).get(key)) is not None and after_review[key] > limit]
                needed = [f"{key} budget reached ({after_review[key]:g}/{limit:g})"
                          for key in ("tokens", "cost")
                          if role_index + 1 < len(roles)
                          and (limit := (it.get("budgets") or {}).get(key)) is not None and after_review[key] >= limit]
                if exceeded or needed or node_exceeded:
                    return {"ok": False, "run_dir": str(run_dir), "failed_ids": [], "control": "budget",
                            "budget_exceeded": "; ".join(exceeded + needed + node_exceeded),
                            "agent_checkpoint": "review-turn-settled", "error": "reviewer budget threshold reached",
                            "reviews": reviews, **after_review}
            finally:
                # A transport failure after prompt submission cannot prove the
                # reviewer stopped. Keep its exact tab/launch and the item's
                # collision claim for explicit doctor/recovery reconciliation.
                if reviewer_settled:
                    herdr.close_agent_tab(reviewer)
    if git(project["path"], "rev-parse", project["base"], check=False) != base_sha:
        return {"ok": False, "run_dir": str(run_dir), "failed_ids": ["base-moved"], "reviews": reviews,
                "error": "configured base moved after review; all review evidence is invalid",
                **aggregate_usage(implementer_measured, reviewer_tokens, reviewer_cost,
                                  reviewer_tokens_complete=reviewer_tokens_complete,
                                  reviewer_cost_complete=reviewer_cost_complete)}
    measured = aggregate_usage(implementer_measured, reviewer_tokens, reviewer_cost,
                               reviewer_tokens_complete=reviewer_tokens_complete,
                               reviewer_cost_complete=reviewer_cost_complete)
    return {"ok": True, "run_dir": str(run_dir), "failed_ids": [], **measured,
            "reviews": reviews, "sha": sha, "base_sha": base_sha, "changed": changed}


def _headless_reviews(it: dict, summary: dict, sha: str, base_sha: str | None = None) -> list[dict]:
    """Bind only parseable pi-graph evidence to the exact post-run base/head pair."""
    run_dir = Path(summary.get("run_dir") or "")
    phases = ["review_correctness"] + (["review_adversarial"] if modes.high_assurance(it["dispatch"]["graph"]) else [])
    reviews = []
    for phase in phases:
        evidence = ""
        if run_dir.is_dir():
            for path in sorted(run_dir.glob(f"{phase}*")):
                if path.is_file():
                    try: evidence += "\n" + path.read_text()[-12000:]
                    except OSError: pass
        if not evidence:
            return []
        verdict = _json_verdict(evidence)
        if verdict.get("sha") != sha or (base_sha is not None and verdict.get("base_sha") != base_sha):
            return []
        reviews.append({**verdict, "sha": sha, "base_sha": base_sha, "role": phase.removeprefix("review_"),
                        "reviewer": {"kind": "pi-graph", "identity": f"{run_dir}:{phase}"},
                        "fresh": True, "valid": True, "at": now(), "evidence": evidence[-12000:]})
    return reviews


def _apply_safety_pipeline(it: dict, project: dict, wt: Path, summary: dict, initial: dict, timeout: int,
                           *, restricted: bool = True, deadline: float | None = None,
                           verify_env: dict | None = None) -> dict:
    """One post-execution safety pipeline for Herdr, headless, and test runners."""
    if not summary.get("ok"):
        return summary
    if it["kind"] == "scout":
        current = worktree.signature(wt)
        if current != initial:
            return {**summary, "ok": False, "failed_ids": ["scout-read-only"],
                    "error": "scout modified its read-only worktree; preserved for inspection"}
        return summary
    dirty = worktree.status_paths(wt)
    if dirty:
        return {**summary, "ok": False, "failed_ids": ["dirty-worktree"],
                "error": "verification refused uncommitted or untracked mutations: " + ", ".join(dirty)}
    changed = worktree.changed_files(project, wt)
    protected = [p for p in changed if any(fnmatch.fnmatch(p, pattern) for pattern in project.get("protected_paths", []))]
    if protected:
        return {**summary, "ok": False, "failed_ids": ["protected"], "changed": changed,
                "error": "protected paths changed: " + ", ".join(protected)}
    escaped = scope.escaped((it.get("scope") or {}).get("paths"), changed)
    if escaped:
        return {**summary, "ok": False, "failed_ids": ["scope-escape"], "changed": changed,
                "scope_escape": escaped, "error": "changed files escaped declared scope: " + ", ".join(escaped)}
    if it.get("memory_request"):
        from . import memory
        if memory_error := memory.validate_project_change(it, project, wt):
            return {**summary, "ok": False, "failed_ids": ["project-memory"], "changed": changed,
                    "error": memory_error}
    base_sha = git(project["path"], "rev-parse", project["base"])
    if sh(["git", "-C", str(wt), "merge-base", "--is-ancestor", base_sha, "HEAD"], check=False).returncode:
        return {**summary, "ok": False, "failed_ids": ["base-moved"],
                "error": "configured base moved during execution; full pipeline must rerun"}
    node_usage = copy.deepcopy(summary.get("node_usage") or _node_usage_snapshot(it))
    verify_blocked = node_budget_blockers(it, "verify", node_usage=node_usage)
    verify_timeout = _node_timeout(it, node_usage, "verify", deadline, timeout)
    if verify_blocked or not verify_timeout:
        return {**summary, "ok": False, "failed_ids": [], "control": "budget",
                "budget_exceeded": "; ".join(verify_blocked) or "seconds budget reached before final verification",
                "error": "time budget reached"}
    verify_started = time.monotonic()
    verify = (sandbox.run_verification(work_id=it["id"], phase="final", cwd=wt,
                                       command=project.get("test_cmd") or "true",
                                       timeout=verify_timeout, env=verify_env)
              if restricted else
              sh(["bash", "-c", project.get("test_cmd") or "true"], cwd=wt,
                 check=False, timeout=verify_timeout, env=verify_env))
    _record_node_seconds(node_usage, "verify", time.monotonic() - verify_started)
    _persist_node_usage(it["id"], node_usage)
    summary = {**summary, "node_usage": node_usage}
    verify_crossed = _node_budget_findings(it.get("node_budgets") or {}, node_usage,
                                            "verify", exhausted=False)
    run_dir = Path(summary.get("run_dir") or item_dir(it["id"]))
    if run_dir.is_dir():
        (run_dir / "bossctl-final-verify.md").write_text(verify.stdout + verify.stderr)
    if verify.returncode:
        return {**summary, "ok": False, "failed_ids": ["verify"],
                "control": "budget" if verify_crossed else summary.get("control"),
                "budget_exceeded": "; ".join(verify_crossed) if verify_crossed else summary.get("budget_exceeded"),
                "error": (verify.stdout + verify.stderr)[-3000:]}
    if verify_crossed:
        return {**summary, "ok": False, "failed_ids": [], "control": "budget",
                "budget_exceeded": "; ".join(verify_crossed),
                "error": "final verify node budget threshold reached"}
    dirty = worktree.status_paths(wt)
    if dirty:
        return {**summary, "ok": False, "failed_ids": ["dirty-worktree"],
                "error": "tests mutated the worktree: " + ", ".join(dirty)}
    if git(project["path"], "rev-parse", project["base"], check=False) != base_sha:
        return {**summary, "ok": False, "failed_ids": ["base-moved"],
                "error": "configured base moved during final verification; full pipeline must rerun"}
    sha = git(wt, "rev-parse", "HEAD")
    reviews = summary.get("reviews") or []
    if (it["dispatch"]["graph"] == "direct-pr" or modes.high_assurance(it["dispatch"]["graph"])) and not reviews:
        reviews = _headless_reviews(it, summary, sha, base_sha)
    required = 2 if modes.high_assurance(it["dispatch"]["graph"]) else 1 if it["dispatch"]["graph"] == "direct-pr" else 0
    if len(reviews) != required or any(
        r.get("verdict") != "accept" or r.get("sha") != sha or r.get("base_sha") != base_sha
        or r.get("fresh") is not True or r.get("valid") is not True or not r.get("reviewer")
        for r in reviews
    ):
        return {**summary, "ok": False, "failed_ids": ["exact-sha-review"],
                "error": "fresh accepting reviewer evidence is not bound to the exact final SHA", "reviews": reviews}
    return {**summary, "sha": sha, "base_sha": base_sha, "changed": changed, "reviews": reviews,
            "fingerprint": worktree.signature(wt), "final_verify": True}


def _execute(it: dict, timeout: int) -> dict:
    started_monotonic = time.monotonic()
    deadline = None
    if (it.get("budgets") or {}).get("seconds"):
        remaining = float(it["budgets"]["seconds"]) - float((it.get("usage") or {}).get("seconds") or 0.0)
        timeout = min(timeout, max(1, int(remaining)))
        deadline = started_monotonic + max(0.0, remaining)
    project = registry.get(it["project"])
    test_mode = graphs.deterministic_test_mode()
    if not herdr.inside() and not test_mode:
        raise BossError("managed execution requires a real Herdr session; no headless worker identity was fabricated")
    # Recheck the current external gate at execution time. A queued item cannot
    # bypass a later fail-closed project policy change.
    gates.require_execution(project.get("gate", "native"), project["path"])
    policy = _policy_snapshot(project)
    d = item_dir(it["id"])
    it = _mark_activity(it["id"], "integrating")
    wt = worktree.create(project, it["id"])
    initial = worktree.signature(wt)
    integration = worktree.integrate_latest(project, wt)
    def integrated(current):
        if current.get("status") != "running":
            raise BossError("item stopped while its worktree was integrating")
        current["integration"] = integration
        current["reviews"] = []
        current["activity"] = {"last": now(), "state": "integrated"}
    it = control.cas_update(it["id"], integrated)
    initial = worktree.signature(wt)
    brief = d / "brief.md"
    brief.write_text(brief_text(it, project))
    pinned = it.get("dispatch") or dispatch.resolve(it, project)
    fresh = dispatch.resolve(it, project)
    if fresh["models"] != pinned["models"] or fresh["thinking"] != pinned["thinking"]:
        raise BossError("model/thinking configuration drifted after resolution; retry with an explicit override")
    dp = pinned
    dispatch.assert_available(dp)
    steps = graphs.render(dp["graph"], d, cwd=wt, branch=it["branch"], project=project,
                          models=dp["models"], thinking=dp["thinking"], timeout=timeout)
    graphs.validate(steps)
    env = worktree.git_env(wt, d / "gitexclude")
    if test_mode and it.get("node_budgets"):
        run_dir = d / "runs" / f"node-budget-unproven-{int(time.time() * 1000)}"
        run_dir.mkdir(parents=True, exist_ok=False)
        summary = {"ok": False, "run_dir": str(run_dir), "failed_ids": [], "control": "budget",
                   "budget_exceeded": "deterministic graph seam cannot attribute usage to runtime nodes",
                   "agent_checkpoint": "no-model-turn-started",
                   "error": "per-node budgets require the real Herdr-native execution path",
                   "node_usage": _node_usage_snapshot(it)}
    elif herdr.inside() and not test_mode:
        summary = _persistent_execute(it, project, wt, brief, timeout, env,
                                      attempt_started=started_monotonic, deadline=deadline)
    else:
        # The inert checked-in runner is a deterministic test seam only. It is
        # never represented as a Herdr agent session or accepted from an
        # arbitrary BOSS_PIW path.
        tab = None
        if herdr.inside():
            import shlex
            tab = herdr.open_tab(f"⚙ {project['id']}: {it['text'].splitlines()[0][:28]}",
                                 f"{shlex.quote(str(Path(__file__).resolve().parents[1] / 'bin' / 'bossctl'))} tail {it['id']}")
            if tab: herdr.remember("task", {**tab, "item": it["id"]})
        try:
            it = _mark_activity(it["id"], "executing-graph")
            monitor_started = time.monotonic()
            def headless_monitor():
                live = load(it["id"]); controls = live.get("controls") or {}
                elapsed = time.monotonic() - monitor_started
                if controls.get("pause_requested") or controls.get("interrupt_requested"):
                    return {"control": "interrupt" if controls.get("interrupt_requested") else "pause"}
                elapsed_total = float((live.get("usage") or {}).get("seconds") or 0.0) + elapsed
                if (live.get("budgets") or {}).get("seconds") and elapsed_total >= live["budgets"]["seconds"]:
                    return {"control": "budget", "budget_exceeded": "time budget reached"}
                changed_now = worktree.status_paths(wt, allow_ask=True)
                escaped_now = scope.escaped((live.get("scope") or {}).get("paths"), changed_now)
                return {"scope_escape": escaped_now} if escaped_now else None
            summary = graphs.run(steps, brief, timeout + 60, env=env, monitor=headless_monitor)
            pending_ids = [event["id"] for event in (it.get("controls") or {}).get("pending", [])]
            if pending_ids and summary.get("ok"):
                control.consume(it["id"], pending_ids, "delivered")
        finally:
            if tab:
                herdr.close_tab(tab["tab_id"]); herdr.forget(tab["tab_id"])
    it = _mark_activity(it["id"], "final-verification",
                        node="verify" if summary.get("ok") and it["kind"] != "scout" else None)
    current_project = registry.get(it["project"])
    if _policy_snapshot(current_project) != policy:
        summary = {**summary, "ok": False, "failed_ids": ["project-policy-moved"],
                   "error": "registered project policy changed during execution; full pipeline must rerun"}
    else:
        summary = _apply_safety_pipeline(
            it, project, wt, summary, initial, timeout,
            restricted=not test_mode,
            deadline=deadline, verify_env=env)
    elapsed = time.monotonic() - started_monotonic
    previous_usage = it.get("usage") or {}
    if summary.get("usage_kind") == "session-cumulative":
        usage_record = dict(previous_usage)
        usage_record.update({key: summary.get(key, previous_usage.get(key)) for key in (
            "tokens", "cost", "implementer_tokens", "implementer_cost",
            "reviewer_tokens", "reviewer_cost", "implementer_session_id",
            "tokens_evidence_complete", "cost_evidence_complete"
        )})
    else:
        usage_record = dict(previous_usage)
        if summary.get("tokens") is not None:
            usage_record["tokens"] = int(previous_usage.get("tokens") or 0) + int(summary["tokens"])
        if summary.get("cost") is not None:
            usage_record["cost"] = float(previous_usage.get("cost") or 0.0) + float(summary["cost"])
    usage_record["seconds"] = float(previous_usage.get("seconds") or 0.0) + elapsed
    node_usage_record = copy.deepcopy(summary.get("node_usage") or it.get("node_usage") or {})
    attempt_tokens, attempt_cost = summary.get("tokens"), summary.get("cost")
    if attempt_tokens is not None:
        summary["tokens"] = usage_record.get("tokens")
    if attempt_cost is not None:
        summary["cost"] = usage_record.get("cost")
    budgets = it.get("budgets") or {}
    exceeded = []
    for key, actual in (("tokens", summary.get("tokens")), ("cost", summary.get("cost")),
                        ("seconds", usage_record.get("seconds"))):
        limit = budgets.get(key)
        if (limit is not None and key in ("tokens", "cost")
                and usage_record.get(f"{key}_evidence_complete") is not True):
            exceeded.append(f"{key} evidence incomplete")
        elif limit is not None and actual is None:
            exceeded.append(f"{key} evidence unavailable")
        elif limit is not None and actual > limit:
            exceeded.append(f"{key} {actual:g}>{limit:g}")
    for node in (it.get("node_budgets") or {}):
        exceeded.extend(_node_budget_findings(it.get("node_budgets") or {}, node_usage_record,
                                              node, exhausted=False))
    if exceeded:
        # Crossing the wall-clock cap after a bounded operation settles is a
        # reason to stop, not permission to erase the operation's real failure
        # evidence. A successful operation gets the pure budget outcome; a
        # failed verification/review retains its exact failed phase and error.
        failed_before_budget = list(summary.get("failed_ids") or [])
        summary = {**summary, "ok": False, "failed_ids": failed_before_budget,
                   "control": "budget", "budget_exceeded": "; ".join(exceeded),
                   "error": (summary.get("error") if failed_before_budget else "budget threshold reached")}
    it = load(it["id"])
    it["usage"] = usage_record
    it["node_usage"] = node_usage_record
    it["attempts"] += 1
    it["runs"].append({"attempt": it["attempts"], "at": now(), "ok": bool(summary.get("ok")),
                       "run_dir": summary.get("run_dir"), "failed_ids": summary.get("failed_ids"),
                       "control": summary.get("control"), "budget_exceeded": summary.get("budget_exceeded"),
                       "agent_checkpoint": summary.get("agent_checkpoint"),
                       "tokens": summary.get("tokens"), "cost": summary.get("cost"),
                       "attempt_tokens": attempt_tokens, "attempt_cost": attempt_cost,
                       "usage_total": usage_record, "node_usage_total": node_usage_record,
                       "sha": summary.get("sha")})
    it["reviews"] = summary.get("reviews") or it.get("reviews", [])
    it["head_sha"] = summary.get("sha") or (git(wt, "rev-parse", "HEAD") if wt.exists() else None)
    it["changed_scope"] = summary.get("changed") or (worktree.changed_files(project, wt) if wt.exists() else [])
    prior_checkpoint = it.get("checkpoint") or {}
    it["checkpoint"] = {"sha": it["head_sha"], "at": now(), "phase": "verified" if summary.get("ok") else "checkpoint",
                        "changed_scope": it["changed_scope"], "worktree": worktree.signature(wt) if wt.exists() else None,
                        "attempt": it["attempts"], "run_dir": summary.get("run_dir"),
                        "failed_ids": summary.get("failed_ids") or [], "agent_checkpoint": summary.get("agent_checkpoint"),
                        "control": summary.get("control"), "budget_exceeded": summary.get("budget_exceeded"),
                        "usage": {"tokens": summary.get("tokens"), "cost": summary.get("cost")},
                        "remaining_guidance": list((it.get("controls") or {}).get("pending", [])),
                        "previous_sha": prior_checkpoint.get("sha")}
    it["activity"] = {"last": now(), "state": "verified" if summary.get("ok") else "attention"}
    failed_ids = set(summary.get("failed_ids") or [])
    it["rigor"] = rigor.escalate(it.get("rigor") or {}, changed=it["changed_scope"],
                                 verification_failed=bool(failed_ids.intersection({"verify", "protected", "review_correctness", "review_adversarial"})),
                                 scope_escaped=bool(summary.get("scope_escape")))
    if not summary.get("ok") and (it.get("rigor") or {}).get("level") == "high-risk":
        it["dispatch"]["graph"] = modes.HIGH_ASSURANCE
    it.setdefault("verification", []).append({"at": now(), "ok": bool(summary.get("ok")), "run_dir": summary.get("run_dir"),
                                               "base_sha": summary.get("base_sha"), "head_sha": summary.get("sha"),
                                               "fingerprint": summary.get("fingerprint"), "complete": bool(summary.get("final_verify"))})
    it.pop("lease", None)

    if summary.get("control") or (it.get("controls") or {}).get("paused") or (it.get("controls") or {}).get("pause_requested"):
        it["controls"]["paused"] = True; it["controls"]["pause_requested"] = False
        it["controls"]["interrupt_requested"] = False; it["phase"] = "paused"
        for event in it["controls"].get("events", []):
            if event.get("state") == "pending" and event.get("action") in ("pause", "interrupt"):
                event["state"] = "consumed"; event["consumed_at"] = now()
        note = "cooperative checkpoint complete; waiting for resume"
        if summary.get("budget_exceeded"):
            it["ask"] = {"question": "The item reached its configured budget. Increase it or narrow the task?",
                         "context": summary["budget_exceeded"]}
            note = "budget threshold reached; work preserved"
        transition(it, "paused", note)
        return it

    ask_file = wt / ".boss-ask.json"
    if ask_file.exists():
        try:
            it["ask"] = json.loads(ask_file.read_text())
        except json.JSONDecodeError:
            it["ask"] = {"question": ask_file.read_text()[:2000]}
        it["attempts"] -= 1                      # asking is not a failed attempt
        transition(it, "needs-you", it["ask"].get("question", "")[:120])
        herdr.notify(f"{project['id']} needs you", it["ask"].get("question", "")[:160])
        return it

    if summary.get("scope_escape"):
        it["ask"] = {"question": "Changed scope escaped the declared claim; approve a broader scope?",
                     "context": ", ".join(summary["scope_escape"])}
        it["controls"]["paused"] = True; it["phase"] = "scope-escalation"
        transition(it, "needs-you", it["ask"]["question"])
        return it

    if summary.get("ok") and (it.get("rigor") or {}).get("escalated_from"):
        target_graph = modes.HIGH_ASSURANCE if it["rigor"]["level"] == "high-risk" else project["mode"]
        if it["dispatch"]["graph"] != target_graph:
            it["dispatch"]["graph"] = target_graph; it["reviews"] = []; it["phase"] = "rigor-escalation"
            transition(it, "queued", f"observed evidence escalated rigor to {it['rigor']['level']}; rerunning stronger gates")
            return it

    if summary.get("ok"):
        if it["kind"] == "scout":
            src = Path(summary.get("run_dir") or "") / "report.md"
            if src.is_file():
                shutil.copy(src, d / "report.md")
            if it.get("session") and not herdr.close_agent_tab(it["session"]):
                raise herdr.UnsettledAgentError("scout session is not positively quiesced; preserving its worktree")
            clean_signature = worktree.signature(wt)
            worktree.remove(project, it["id"], delete_branch=True, expected=clean_signature)
            it["phase"] = "done"
            transition(it, "done", f"report at {d / 'report.md'}")
            return it
        if not worktree.has_commits(project, wt):
            transition(it, "failed", "graph passed but produced no commits")
            return it
        it["phase"] = "merge-ready"
        it["activity"] = {"last": now(), "state": "delivering"}; it["phase"] = "delivering"; save(it)
        deliver.after_success(it, project, wt)
        herdr.notify(f"{project['id']}: {it['status']}", it["text"].splitlines()[0][:120])
        return it

    notes = graphs.failure_notes(summary) or str(summary.get("error") or "unknown execution failure")
    signature = _failure_signature(summary, notes)
    repeated = bool(it["failure_notes"] and it["failure_notes"][-1].get("signature") == signature)
    it["failure_notes"].append({"attempt": it["attempts"], "notes": notes, "signature": signature})
    it["phase"] = "revision"
    if repeated and it["attempts"] < it["max_attempts"]:
        it["ask"] = {"question": "The same failure repeated without new evidence. What guidance should the worker use?",
                     "context": notes[-1000:]}
        it["controls"]["paused"] = True; it["phase"] = "repeated-failure"
        transition(it, "needs-you", "repeated failure paused; no blind retry")
    elif it["attempts"] < it["max_attempts"]:
        transition(it, "queued", f"attempt {it['attempts']} failed ({','.join(summary.get('failed_ids') or [])}); requeued")
    else:
        transition(it, "failed", f"exhausted {it['max_attempts']} attempts")
        herdr.notify(f"{project['id']}: failed", it["text"].splitlines()[0][:120])
    return it


def respond(work_id: str, guidance: str) -> dict:
    it = load(work_id)
    if it["status"] not in ("needs-you", "failed"):
        raise BossError(f"{work_id} is {it['status']}, not needs-you/failed")
    if blockers := budget_blockers(it):
        raise BossError("response cannot start a new turn with exhausted budget or unproven usage (" + "; ".join(blockers) +
                        "); raise it explicitly with `bossctl budget` first")
    it["guidance"].append({"at": now(), "text": guidance.strip(), "question": (it.get("ask") or {}).get("question")})
    it["ask"] = None
    ask_file = worktree.worktree_root() / it["project"] / it["id"] / ".boss-ask.json"
    ask_file.unlink(missing_ok=True)
    if it["status"] == "failed":
        it["attempts"] = 0
    it.setdefault("controls", {})["paused"] = False
    it["phase"] = "queued"
    transition(it, "queued", "boss responded; persistent session queued with guidance")
    return it


def retry(work_id: str) -> dict:
    it = load(work_id)
    if it["status"] != "failed":
        raise BossError(f"{work_id} is {it['status']}, not failed")
    if blockers := budget_blockers(it):
        raise BossError("retry refused with exhausted budget or unproven usage (" + "; ".join(blockers) +
                        "); raise it explicitly with `bossctl budget` first")
    it["attempts"] = 0
    transition(it, "queued", "manual retry")
    return it


def comment(work_id: str, text: str) -> dict:
    """Append a durable human note to a work item WITHOUT a state transition or requeue.

    This is the in-band collaboration channel: a question or context the boss wants
    the next implementer/reviewer to see, visible in `bossctl show` and picked up in
    the next brief, without forcing `respond` (which requeues) or living only in chat.
    """
    it = load(work_id)
    text = (text or "").strip()
    if not text:
        raise BossError("comment text must not be empty")
    entry = {"at": now(), "from": it["status"], "to": it["status"], "note": f"[comment] {text}"}
    it.setdefault("history", []).append(entry)
    it.setdefault("comments", []).append({"at": entry["at"], "text": text})
    save(it)
    log(f"{it['id']}: comment added")
    return it


_OPEN_CANCELLATION = {"armed", "quiescing", "filesystem-requested", "filesystem-complete"}


def _cancel_sessions(item: dict) -> None:
    """Close only positively identified, settled sessions owned by this item."""
    sessions, seen = [], set()
    for candidate in [item.get("session"), *(item.get("agent_launches") or [])]:
        if not isinstance(candidate, dict):
            continue
        identity = (candidate.get("launch_id"), candidate.get("agent_session_id"), candidate.get("tab_id"))
        if identity in seen:
            continue
        if candidate is not item.get("session") and candidate.get("state") not in herdr.OPEN_LAUNCH_STATES:
            continue
        seen.add(identity); sessions.append(candidate)
    for session in sessions:
        if not herdr.close_agent_tab(session):
            raise BossError("cancellation stopped: an exact item session is live, unsettled, reused, or unreachable")


def _cancel_reconcile(item: dict, project: dict, *, discard: bool) -> dict:
    """Replay a durable, non-destructive cancellation intent."""
    cancellation = item.get("cancellation") or {}
    intent_id = cancellation.get("id")
    if (not intent_id or cancellation.get("state") not in _OPEN_CANCELLATION
            or cancellation.get("project_id") != project.get("id")
            or cancellation.get("repository_path") != str(Path(project["path"]).resolve())
            or cancellation.get("worktree_path") != str((worktree.worktree_root() / project["id"] / item["id"]).resolve())
            or cancellation.get("branch") != item.get("branch")):
        raise BossError("cancellation journal is incomplete or no longer matches the item; work was preserved")
    if bool(cancellation.get("discard_authorized")) != bool(discard):
        flag = " --discard" if cancellation.get("discard_authorized") else ""
        raise BossError(f"cancellation already armed with different authority; repeat `bossctl cancel {item['id']}{flag}`")

    if cancellation["state"] in {"armed", "quiescing"}:
        latest = load(item["id"])
        if latest.get("status") != "cancelling" or (latest.get("cancellation") or {}).get("id") != intent_id:
            raise BossError("cancellation ownership changed; work was preserved")
        def quiescing(current):
            active = current.get("cancellation") or {}
            if active.get("id") != intent_id or current.get("status") != "cancelling":
                raise BossError("cancellation ownership changed before session reconciliation")
            active["state"] = "quiescing"; active["quiescing_at"] = now()
        control.cas_update(item["id"], quiescing, expected_revision=int(latest.get("revision", 0)))
        _cancel_sessions(load(item["id"]))
        latest = load(item["id"])
        def requested(current):
            active = current.get("cancellation") or {}
            if active.get("id") != intent_id or current.get("status") != "cancelling":
                raise BossError("cancellation ownership changed before filesystem reconciliation")
            active["state"] = "filesystem-requested"; active["filesystem_requested_at"] = now()
        item = control.cas_update(item["id"], requested, expected_revision=int(latest.get("revision", 0)))
        cancellation = item["cancellation"]

    if cancellation["state"] == "filesystem-requested":
        wt = Path(cancellation["worktree_path"])
        destination = Path(cancellation["quarantine_path"])
        if wt.exists() and destination.exists():
            raise BossError("cancellation found both owned and quarantined worktrees; preserving both for doctor reconciliation")
        result = worktree.quarantine(project, item["id"], reason="explicit item cancellation",
                                     destination=destination)
        # An absent source is only a successful replay when the exact journaled
        # destination proves our earlier move. Unknown disappearance is never
        # converted into success.
        if not result.get("quarantined") and cancellation.get("worktree_present"):
            raise BossError("journaled worktree disappeared without its quarantine receipt; preserving branch and state")
        latest = load(item["id"])
        def filesystem_complete(current):
            active = current.get("cancellation") or {}
            if active.get("id") != intent_id or active.get("state") != "filesystem-requested":
                raise BossError("cancellation journal changed during filesystem reconciliation")
            active.update(state="filesystem-complete", filesystem_completed_at=now(),
                          quarantine_result=result)
        item = control.cas_update(item["id"], filesystem_complete,
                                  expected_revision=int(latest.get("revision", 0)))
        cancellation = item["cancellation"]

    claim_token = cancellation.get("claim_token")
    claim_released = scope.release(item["id"], claim_token) if claim_token else False
    latest = load(item["id"])
    def complete(current):
        active = current.get("cancellation") or {}
        if active.get("id") != intent_id or active.get("state") != "filesystem-complete":
            raise BossError("cancellation journal changed before completion")
        before = active.get("prior_status") or "unknown"
        current.setdefault("history", []).append({"at": now(), "from": before, "to": "cancelled",
                                                   "note": "work preserved in recoverable quarantine"})
        current["status"] = "cancelled"; current["phase"] = "cancelled"
        current["activity"] = {"last": now(), "state": "cancelled"}
        current.pop("lease", None); current.pop("recovery_claim_token", None)
        active.update(state="complete", completed_at=now(), claim_released=claim_released)
    completed = control.cas_update(item["id"], complete, expected_revision=int(latest.get("revision", 0)))
    try:
        from . import supervisor
        supervisor.observe(completed)
    except BaseException as exc:
        log(f"{item['id']}: supervisor observation unavailable after cancellation: {getattr(exc, 'msg', None) or exc!r}")
    return completed


def cancel(work_id: str, *, discard: bool = False) -> dict:
    """Cancel without deleting work; `--discard` authorizes quarantine of unlanded work."""
    with locked(authority_lock()):
        it = load(work_id)
        project = registry.get(it["project"])
        existing = it.get("cancellation") or {}
        if existing.get("state") == "complete" and it.get("status") == "cancelled":
            return it
        if existing.get("state") in _OPEN_CANCELLATION:
            return _cancel_reconcile(it, project, discard=discard)
        if (it.get("promotion") or {}).get("state") in {"armed", "external-requested", "merge-observed", "cleanup-pending"}:
            raise BossError("cannot cancel while an exact-SHA promotion requires reconciliation")
        if (it.get("pr_delivery") or {}).get("state") in {"armed", "push-requested", "push-confirmed", "pr-create-requested"}:
            raise BossError("cannot cancel while exact-SHA PR delivery requires reconciliation")
        if gates.has_open_transaction(it):
            raise BossError("cannot cancel while a no-mistakes transaction requires explicit reconciliation")
        if it["status"] in {"running", "cancelling"}:
            raise BossError("cannot cancel a running or unjournaled cancelling item; reconcile it first")
        if it["status"] in {"merged", "done", "cancelled"}:
            raise BossError(f"cannot cancel a terminal {it['status']} item")

        expected_path = (worktree.worktree_root() / project["id"] / it["id"]).resolve()
        recorded_path = Path(it.get("worktree") or expected_path).expanduser().resolve()
        if recorded_path != expected_path:
            raise BossError("cancellation refused: recorded worktree escapes the item's owned path")
        wt_present = expected_path.is_dir()
        signature = worktree.signature(expected_path) if wt_present else None
        if wt_present and git(expected_path, "rev-parse", "--abbrev-ref", "HEAD", check=False) != it.get("branch"):
            raise BossError("cancellation refused: owned worktree branch identity changed")
        branch_sha = git(project["path"], "rev-parse", "--verify", it["branch"], check=False)
        base_sha = git(project["path"], "rev-parse", project["base"], check=False)
        unique_commits = bool(branch_sha and sh([
            "git", "-C", str(project["path"]), "merge-base", "--is-ancestor", branch_sha, base_sha
        ], check=False).returncode)
        unlanded = bool((signature or {}).get("dirty") or unique_commits)
        if unlanded and not discard:
            raise BossError(f"{work_id} has unlanded commits or edits on {it['branch']}; explicit --discard authorization is required (work will be quarantined, not deleted)")

        intent_id = secrets.token_hex(16)
        quarantine_path = (home() / "quarantine" / "worktrees" / project["id"] /
                           f"{it['id']}-{intent_id}").resolve()
        claim_token = ((it.get("lease") or {}).get("claim_token") or it.get("recovery_claim_token"))
        prior_status = it["status"]
        expected_revision = int(it.get("revision", 0))
        def arm(current):
            if current.get("status") != prior_status or (current.get("promotion") or {}).get("state") in {
                "armed", "external-requested", "merge-observed", "cleanup-pending"
            }:
                raise BossError("item changed before cancellation could arm; no work was touched")
            current["cancellation"] = {
                "id": intent_id, "state": "armed", "armed_at": now(), "prior_status": prior_status,
                "project_id": project["id"], "repository_path": str(Path(project["path"]).resolve()),
                "worktree_path": str(expected_path), "worktree_present": wt_present,
                "worktree_signature": signature, "quarantine_path": str(quarantine_path),
                "branch": it["branch"], "branch_sha": branch_sha, "base_sha": base_sha,
                "discard_authorized": bool(discard), "unlanded_at_arm": unlanded,
                "claim_token": claim_token,
            }
            current.setdefault("history", []).append({"at": now(), "from": prior_status, "to": "cancelling",
                                                       "note": "non-destructive cancellation armed"})
            current["status"] = "cancelling"; current["phase"] = "cancelling"
        armed = control.cas_update(it["id"], arm, expected_revision=expected_revision)
        return _cancel_reconcile(armed, project, discard=discard)
