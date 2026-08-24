"""Dispatch is data, not prose: the first matching rule decides graph + model pins.

A rule matches on kind (ship|scout), project id regex, and required labels.
Nothing here asks a model anything.
"""
from __future__ import annotations
import os
import re
import shutil
import subprocess
from .paths import dispatch_file
from .util import read_json, write_json, BossError
from . import graphs, modes

PHASES = ("plan", "implement", "review_correctness", "review_adversarial", "scout")

DEFAULT = {
    "version": 1,
    # Opinionated: everything runs on the Codex subscription. Add other providers
    # (log in with /login inside `pi-boss`) and point phases at them here.
    "models": {
        "plan": "openai-codex/gpt-5.6-sol",
        "implement": "openai-codex/gpt-5.6-sol",
        "review_correctness": "openai-codex/gpt-5.6-sol",
        "review_adversarial": "openai-codex/gpt-5.6-sol",
        "scout": "openai-codex/gpt-5.6-sol",
    },
    "thinking": {"plan": "high", "implement": "high", "review_correctness": "high",
                 "review_adversarial": "high", "scout": "high"},
    "rules": [
        {"name": "scout", "kind": "scout", "graph": "scout"},
        {"name": "cheap", "kind": "ship", "labels": ["cheap"],
         "models": {"implement": "openai-codex/gpt-5.4-mini"}, "thinking": {"implement": "low"}},
        {"name": "hard", "kind": "ship", "labels": ["hard"],
         "thinking": {"plan": "high", "implement": "high", "review_adversarial": "high"}},
        {"name": "default-ship", "kind": "ship"},
    ],
}


def load() -> dict:
    data = read_json(dispatch_file())
    if data is None:
        write_json(dispatch_file(), DEFAULT)
        return DEFAULT
    # An older file keeps its choices but inherits defaults for phases it never mentions.
    data["models"] = {**DEFAULT["models"], **(data.get("models") or {})}
    data["thinking"] = {**DEFAULT["thinking"], **(data.get("thinking") or {})}
    return data


def resolve(item: dict, project: dict) -> dict:
    """Resolve and explain exact model pins; overrides are deterministic and auditable."""
    cfg = load()
    labels = set(item.get("labels") or [])
    for rule in cfg.get("rules", []):
        if rule.get("kind") and rule["kind"] != item["kind"]:
            continue
        if rule.get("project") and not re.fullmatch(rule["project"], project["id"]):
            continue
        if not set(rule.get("labels") or []) <= labels:
            continue
        graph = modes.normalize(rule.get("graph") or (project["mode"] if item["kind"] == "ship" else "scout"))
        level = (item.get("rigor") or {}).get("level")
        if item.get("memory_request") or "project-memory" in labels:
            # Project knowledge becomes a normal reviewed project change. A
            # custom dispatch rule may tune its model, but may never downgrade
            # the review/authority path that protects AGENTS.md.
            graph = modes.HIGH_ASSURANCE
        elif item["kind"] == "ship" and not rule.get("graph"):
            if level == "high-risk": graph = modes.HIGH_ASSURANCE
            elif level == "quick": graph = "local-only"
        models = {**cfg.get("models", {}), **rule.get("models", {}), **(item.get("model_overrides") or {})}
        thinking = {**cfg.get("thinking", {}), **rule.get("thinking", {}), **(item.get("thinking_overrides") or {})}
        missing = [p for p in PHASES if p not in models or p not in thinking]
        if missing:
            raise BossError(f"dispatch rule '{rule.get('name')}' leaves phases without a model/thinking level: {missing}")
        bad = {p: thinking[p] for p in PHASES if thinking[p] not in ("off", "minimal", "low", "medium", "high", "xhigh")}
        if bad:
            raise BossError(f"invalid thinking levels: {bad}")
        override = sorted((item.get("model_overrides") or {}).keys())
        rationale = (f"boss override for {', '.join(override)}; " if override else "") + f"deterministic rule {rule.get('name', '?')}"
        return {"rule": rule.get("name", "?"), "graph": graph, "models": models, "thinking": thinking,
                "rationale": rationale, "resolved": True}
    raise BossError(f"no dispatch rule matches kind={item['kind']} labels={sorted(labels)}")


def assert_available(decision: dict) -> None:
    """Validate against an authoritative configured inventory when one is supplied."""
    raw = os.environ.get("BOSS_AVAILABLE_MODELS")
    if raw is None and graphs.deterministic_test_mode():
        return  # deterministic test runner supplies model evidence in its fixture
    if raw is None:
        if not shutil.which("pi"):
            raise BossError("cannot validate resolved models: pi is unavailable")
        result = subprocess.run(["pi", "--list-models"], text=True, capture_output=True,
                                stdin=subprocess.DEVNULL, timeout=30)
        if result.returncode:
            raise BossError("cannot validate resolved models: `pi --list-models` failed")
        available = {f"{parts[0]}/{parts[1]}" for line in result.stdout.splitlines()
                     if len(parts := line.split()) >= 2}
    else:
        available = {m.strip() for m in raw.split(",") if m.strip()}
    missing = sorted(set(decision.get("models", {}).values()) - available)
    if missing:
        raise BossError("resolved model unavailable: " + ", ".join(missing) + "; refusing silent substitution")
