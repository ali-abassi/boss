"""Validation-provider routing.

``high-assurance`` is BOSS's native workflow. ``no-mistakes`` is a
separate optional product and is reachable only through the pinned external
transaction adapter in :mod:`bossctl.no_mistakes`.
"""
from __future__ import annotations

from pathlib import Path

from . import no_mistakes
from .util import BossError


NO_MISTAKES_VERSION = no_mistakes.VERSION
NO_MISTAKES_TAG_SHA = no_mistakes.TAG_SHA
NO_MISTAKES_BUILD_SHA = no_mistakes.BUILD_SHA
PROVIDERS = ("native", "no-mistakes")


def no_mistakes_status(repo: Path | str | None = None, **kwargs) -> dict:
    return no_mistakes.status(repo, **kwargs)


def status(provider: str, repo: Path | str | None = None) -> dict:
    if provider == "native":
        return {"provider": "native", "ready": True,
                "reason": "built-in exact-SHA safety pipeline"}
    if provider == "no-mistakes":
        return no_mistakes_status(repo)
    return {"provider": provider, "ready": False, "reason": "unknown gate provider"}


def require_execution(provider: str, repo: Path | str) -> dict:
    """Require a currently attested provider capability; never submit here."""
    evidence = status(provider, repo)
    if not evidence.get("ready"):
        raise BossError(f"gate provider {provider} is unavailable: {evidence.get('reason', 'unverified')}")
    return evidence


def start_or_reconcile(item: dict, project: dict, wt: Path) -> dict:
    if project.get("gate", "native") != "no-mistakes":
        return item
    return no_mistakes.start_or_reconcile(item, project, wt)


def has_open_transaction(item: dict) -> bool:
    return no_mistakes.has_open_transaction(item)


def reconcile(work_id: str) -> dict:
    return no_mistakes.reconcile(work_id)


def respond(work_id: str, action: str, findings: list[str] | None = None,
            instructions: str | None = None) -> dict:
    return no_mistakes.respond(work_id, action, findings, instructions)


def inspect_item(item: dict, project: dict) -> dict:
    return no_mistakes.inspect(item, project)


def require_receipt(provider: str, item: dict, project: dict) -> dict:
    if provider == "native":
        return {"provider": "native", "ready": True}
    if provider == "no-mistakes":
        return no_mistakes.require_receipt(item, project)
    raise BossError(f"unknown gate provider {provider}")
