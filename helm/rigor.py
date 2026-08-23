"""Explainable deterministic rigor routing and escalation."""
from __future__ import annotations
import re
from . import scope

LEVELS = ("scout", "quick", "standard", "high-risk")
RANK = {v: i for i, v in enumerate(LEVELS)}
_HIGH = re.compile(r"(?i)\b(auth|security|permission|credential|migration|database|billing|payment|production|workflow|lock|concurr|delete|destructive)\b")
_QUICK = re.compile(r"(?i)\b(typo|copy|comment|readme|docs? only)\b")


def route(item: dict) -> dict:
    if item.get("kind") == "scout":
        return {"level": "scout", "rationale": "read-only investigation"}
    paths = scope.normalize((item.get("scope") or {}).get("paths"))
    labels = set(item.get("labels") or [])
    sensitive = paths != ["unknown"] and scope.is_global(paths)
    if "high-risk" in labels or sensitive or _HIGH.search(item.get("text", "")):
        why = "sensitive/global scope" if sensitive else "risk terms or high-risk override"
        return {"level": "high-risk", "rationale": why}
    if "quick" in labels or (_QUICK.search(item.get("text", "")) and paths != ["unknown"]):
        return {"level": "quick", "rationale": "small documentation/copy scope"}
    return {"level": "standard", "rationale": "default verified implementation"}


def escalate(current: dict, *, changed: list[str] | None = None, verification_failed: bool = False,
             scope_escaped: bool = False) -> dict:
    changed = changed or []
    target, reasons = current.get("level", "standard"), []
    if scope_escaped or verification_failed:
        target = "high-risk"; reasons.append("scope escape" if scope_escaped else "verification failure")
    elif any(scope.is_global([p]) for p in changed) and RANK[target] < RANK["high-risk"]:
        target = "high-risk"; reasons.append("sensitive path observed")
    elif len(changed) > 10 and RANK[target] < RANK["standard"]:
        target = "standard"; reasons.append("observed scope expanded")
    if target == current.get("level"):
        return current
    return {"level": target, "rationale": "; ".join(reasons), "escalated_from": current.get("level")}
