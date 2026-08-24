"""Explainable deterministic rigor routing and escalation."""
from __future__ import annotations
import re
from . import scope

LEVELS = ("scout", "quick", "standard", "high-risk")
RANK = {v: i for i, v in enumerate(LEVELS)}
# Each canonical root is matched with a small set of inflections: plural, -ing, -ed, -ize, etc.
# Compound false positives (e.g. "authoritative", "concurrent", "deletable") are filtered by
# the _SAFE_OVERRIDE denylist which wins over the matcher.
_HIGH_ROOTS = (
    "auth", "security", "permission", "credential", "migration", "migrat", "database",
    "billing", "payment", "production", "produc", "workflow", "lock",
    "delete", "delet", "destructive", "destroy", "deploy", "rollout", "rollback", "payout",
    "invoice", "refund", "tax", "kyc", "pii", "gdpr", "encrypt", "decrypt",
    "secret", "token", "session", "csrf", "xss", "ssrf", "rce",
    "authenticat", "authentic", "authoriz", "destruct",
    "concurrency",  # only the *noun* form, not the adjective "concurrent"
)
_HIGH = re.compile(
    # The root is matched as-is, then optionally add an inflection. Bounded to whole words.
    # We include both the bare stem and the e-form so inflections work for both shapes.
    r"(?i)\b(?:" + "|".join(_HIGH_ROOTS) + r")"
    r"(?:e|s|es|ed|er|ing|ings|ize|ise|ized|ised|ization|isation|ation|ations|al|ally|ates|ating|ated|ions|ion)?\b"
)
# Compound words that look like a high-risk root but are not. Each entry is a whole-word
# pattern; any match here wins over the risk regex. Apply after the high-risk matcher.
_SAFE_OVERRIDE = re.compile(
    r"(?i)\b(?:"
    r"author(?:itative|ity|ship|ed|ing|ize|ization)?|"
    r"authentic(?:ate|ated|ating|ation|ator|y)?|"
    r"production(?:ize|izing|ization|izer|alize|alization)?|"
    r"concurrent(?:ly)?|"
    r"deletable|"
    r"credentialed|"
    r"permissions[- ]?table|"
    r"workflowy"
    r")\b"
)
_QUICK = re.compile(r"(?i)\b(?:typo|copy[- ]edit|comment|readme|docs?[- ]only|changelog only|fmt only|lint only)\b")


def _risk_hit(text: str) -> str | None:
    """Return the matched phrase (lowercased) or None. Filters false positives."""
    for m in _HIGH.finditer(text or ""):
        matched = m.group(0).lower()
        # If the matched text is followed by characters that make it a safe compound
        # (e.g. "authoritative"), skip. We rely on a tighter lookback via the override
        # regex applied to the full text instead.
        if _SAFE_OVERRIDE.search(matched):
            continue
        return matched
    return None


def route(item: dict) -> dict:
    if item.get("kind") == "scout":
        return {"level": "scout", "rationale": "read-only investigation"}
    text = item.get("text", "") or ""
    paths = scope.normalize((item.get("scope") or {}).get("paths"))
    labels = set(item.get("labels") or [])
    sensitive = paths != ["unknown"] and scope.is_global(paths)
    hit = _risk_hit(text)
    if "high-risk" in labels or sensitive or hit:
        why = "sensitive/global scope" if sensitive else f"risk term '{hit}'" if hit else "high-risk override"
        return {"level": "high-risk", "rationale": why}
    if "quick" in labels or (_QUICK.search(text) and paths != ["unknown"]):
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
