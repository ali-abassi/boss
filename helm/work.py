"""Work items: durable JSON records under ~/.helm/work/<id>/ with an explicit state machine.

queued → running → ready | pr-open | needs-you | done | failed
needs-you --respond--> queued      failed --retry--> queued
ready/pr-open --promote--> merged
"""
from __future__ import annotations
import fnmatch
import json
import os
import secrets
import shutil
import time
from pathlib import Path
from . import dispatch, graphs, registry, worktree, deliver, herdr, scope, rigor, control
from .paths import work_root, home
from .util import read_json, write_json, locked, now, log, HelmError, git, sh

ACTIVE = ("running",)
OPEN = ("queued", "running", "paused", "needs-you", "ready", "pr-open")


def item_dir(work_id: str) -> Path: return work_root() / work_id
def item_path(work_id: str) -> Path: return item_dir(work_id) / "item.json"


def _hydrate(it: dict) -> dict:
    """Read-compatible migration for version-1 records; the next CAS write persists it."""
    it.setdefault("schema_version", control.SCHEMA_VERSION); it.setdefault("revision", 0)
    it.setdefault("phase", it.get("status", "queued")); it.setdefault("scope", {"paths": ["unknown"], "claim": "global"})
    it.setdefault("controls", {"paused": False, "away": False, "pending": []})
    for key, default in (("session", None), ("checkpoint", None), ("reviews", []), ("verification", []), ("changed_scope", [])):
        it.setdefault(key, default)
    it.setdefault("activity", {"last": it.get("updated"), "state": it.get("phase")})
    it.setdefault("budgets", {"tokens": None, "cost": None, "seconds": None})
    if not it.get("model_decision") and it.get("dispatch"):
        it["model_decision"] = {"models": it["dispatch"].get("models", {}), "thinking": it["dispatch"].get("thinking", {}),
                                "rationale": "migrated pinned dispatch", "resolved_at": it.get("created")}
    return it


def load(work_id: str) -> dict:
    it = read_json(item_path(work_id))
    if not it:
        raise HelmError(f"unknown work item '{work_id}'")
    return _hydrate(it)


def save(it: dict) -> None:
    """CAS-save an item. Stale writers fail instead of erasing concurrent controls."""
    expected = int(it.get("revision", 0))
    def replace(current):
        if int(current.get("revision", 0)) != expected:
            raise HelmError(f"stale work item revision {expected}; current revision is {current.get('revision', 0)}")
        current.clear(); current.update(it)
    updated = control.cas_update(it["id"], replace, expected)
    it.clear(); it.update(updated)


def transition(it: dict, status: str, note: str = "") -> None:
    it.setdefault("history", []).append({"at": now(), "from": it["status"], "to": status, "note": note})
    it["status"] = status
    save(it)
    log(f"{it['id']}: {status}" + (f" — {note}" if note else ""))


def all_items() -> list[dict]:
    out = []
    if work_root().is_dir():
        for d in sorted(work_root().iterdir()):
            it = read_json(d / "item.json")
            if it:
                out.append(_hydrate(it))
    return sorted(out, key=lambda i: i["created"])


def create(project_id: str, text: str, kind: str = "ship", labels: list[str] | None = None,
           max_attempts: int = 3, declared_scope: list[str] | None = None,
           model: str | None = None, thinking: str | None = None,
           max_tokens: int | None = None, max_cost: float | None = None, max_seconds: int | None = None) -> dict:
    project = registry.get(project_id)
    if kind not in ("ship", "scout"):
        raise HelmError("kind must be ship or scout")
    if model and "/" not in model:
        raise HelmError("model override must be provider/model")
    if any(v is not None and v <= 0 for v in (max_tokens, max_cost, max_seconds)):
        raise HelmError("budgets must be positive")
    available = {m.strip() for m in os.environ.get("HELM_AVAILABLE_MODELS", "").split(",") if m.strip()}
    if model and available and model not in available:
        raise HelmError(f"resolved model {model} is unavailable; refusing silent substitution")
    if kind == "ship" and project["authority"] < 1:
        raise HelmError(f"project '{project_id}' has authority 0 (observe): only scout tasks allowed")
    wid = f"{project_id}-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"
    declared = scope.normalize(declared_scope)
    global_claim = scope.is_global(declared) or any(scope.overlap(declared, [p]) for p in project.get("protected_paths", []))
    it = {
        "id": wid, "project": project_id, "kind": kind, "text": text.strip(),
        "labels": sorted(set(labels or [])), "status": "queued", "attempts": 0,
        "max_attempts": max_attempts, "created": now(), "updated": now(),
        "guidance": [], "failure_notes": [], "runs": [], "history": [],
        "schema_version": control.SCHEMA_VERSION, "revision": 0,
        "branch": worktree.branch_name(wid), "worktree": str(worktree.worktree_root() / project_id / wid),
        "pr_url": None, "ask": None, "dispatch": None, "phase": "queued",
        "scope": {"paths": declared, "claim": "global" if global_claim else "paths"},
        "controls": {"paused": False, "away": False, "pending": []}, "session": None,
        "checkpoint": None, "reviews": [], "verification": [], "changed_scope": [],
        "activity": {"last": now(), "state": "queued"},
        "budgets": {"tokens": max_tokens, "cost": max_cost, "seconds": max_seconds},
        "model_overrides": ({("scout" if kind == "scout" else "implement"): model} if model else {}),
        "thinking_overrides": ({("scout" if kind == "scout" else "implement"): thinking} if thinking else {}),
    }
    it["rigor"] = rigor.route(it)
    if global_claim and declared != ["unknown"] and it["rigor"]["level"] != "high-risk":
        it["rigor"] = {"level": "high-risk", "rationale": "project-sensitive declared scope"}
    it["dispatch"] = dispatch.resolve(it, project)   # pinned before execution
    dispatch.assert_available(it["dispatch"])
    it["model_decision"] = {"models": it["dispatch"]["models"], "thinking": it["dispatch"]["thinking"],
                            "rationale": it["dispatch"]["rationale"], "resolved_at": now()}
    write_json(item_path(wid), it)
    log(f"{wid}: queued ({kind}, rule={it['dispatch']['rule']}, graph={it['dispatch']['graph']})")
    return it


def brief_text(it: dict, project: dict) -> str:
    lines = [f"# helm {it['kind']} brief — {it['id']}", "",
             f"Project: {project['id']} ({project['path']})",
             f"Mode: {project['mode']} · authority {project['authority']} · base {project['base']}",
             f"Attempt: {it['attempts'] + 1} of {it['max_attempts']}", "", "## Task", "", it["text"], ""]
    if it["guidance"]:
        lines += ["## Captain guidance (authoritative answers to earlier questions)", ""]
        lines += [f"- {g['at']}: {g['text']}" for g in it["guidance"]] + [""]
    pending = (it.get("controls") or {}).get("pending", [])
    if pending:
        lines += ["## Captain steering (apply during this run)", ""]
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


def claim_next(owner: str) -> dict | None:
    """Claim the oldest queued item whose durable scope is mechanically disjoint."""
    with locked(home() / "claim.lock"):
        items = all_items()
        for it in items:                      # a dead owner's lease is not a running item
            if it["status"] == "running" and not _pid_alive((it.get("lease") or {}).get("pid")):
                it.pop("lease", None)
                transition(it, "queued", "stale lease (owner died); requeued without burning an attempt")
        for it in items:
            if it["status"] == "queued" and not (it.get("controls") or {}).get("paused"):
                declared = (it.get("scope") or {}).get("paths") or ["unknown"]
                paths = ["global"] if (it.get("scope") or {}).get("claim") == "global" else declared
                if not scope.claim(it["project"], it["id"], paths, owner, os.getpid()):
                    continue
                it["lease"] = {"owner": owner, "started": now(), "pid": os.getpid(), "scope": paths}
                it["phase"] = "implementing"
                try:
                    transition(it, "running", f"leased by {owner}")
                except BaseException:
                    scope.release(it["id"])
                    raise
                return it
    return None


def execute(it: dict, timeout: int = 3600) -> dict:
    """Run one attempt. Claims are always released and crashes retain branch/checkpoint."""
    try:
        return _execute(it, timeout)
    except BaseException as e:          # includes HelmError (a SystemExit) and KeyboardInterrupt
        it = load(it["id"])
        if it["status"] == "running":
            it.pop("lease", None); it["phase"] = "failed"
            transition(it, "failed", f"attempt crashed: {getattr(e, 'msg', None) or e!r}")
        raise
    finally:
        scope.release(it["id"])


def _model_drift(expected: str, thinking: str, agent: dict) -> None:
    herdr.validate_agent(agent, expected, thinking)


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
        raise HelmError("reviewer produced no parseable verdict; approval was not inferred")
    return found[-1]


def _persistent_execute(it: dict, project: dict, wt: Path, brief: Path, timeout: int) -> dict:
    """Use one real reconnectable Herdr implementer and fresh independent reviewers."""
    decision = it["model_decision"]
    model = decision["models"]["scout" if it["kind"] == "scout" else "implement"]
    thinking = decision["thinking"]["scout" if it["kind"] == "scout" else "implement"]
    dispatch.assert_available(it["dispatch"])
    session = herdr.ensure_agent(it, wt, model, thinking)
    _model_drift(model, thinking, session)
    current = load(it["id"])
    previous = current.get("session")
    recovered_checkpoint = current.get("checkpoint") or {}
    if previous and not session.get("reconnected"):
        current.setdefault("session_history", []).append({**previous, "lost_at": now(), "recovered_from_checkpoint": True})
    current["session"] = session; current["phase"] = "investigating" if it["kind"] == "scout" else "implementing"
    current["checkpoint"] = {"sha": git(wt, "rev-parse", "HEAD"), "at": now(),
                             "phase": current["phase"], "prior": recovered_checkpoint,
                             "scope": current.get("changed_scope", []),
                             "recovery": "live-reconnect" if session.get("reconnected") else "checkpoint-fallback"}
    save(current); it = current
    checkpoint = recovered_checkpoint or it.get("checkpoint") or {}
    if it["kind"] == "scout":
        prompt = ("Investigate this repository read-only. Do not modify files or commit. Return a concise Markdown report with file/line evidence.\n\n"
                  + brief.read_text())
    else:
        prompt = (f"Continue work on the persistent branch {it['branch']} in {wt}. Run `{project.get('test_cmd') or 'true'}` and commit all intended changes. "
                  f"Never touch protected paths: {project.get('protected_paths')}. If a decision is required, write .helm-ask.json and stop. "
                  f"Checkpoint SHA before this turn: {checkpoint.get('sha')}.\n\n" + brief.read_text())
    pending = list((it.get("controls") or {}).get("pending", []))
    baseline_changed = set(worktree.changed_files(project, wt))
    monitor_started = time.monotonic(); last_heartbeat = [0.0]
    def live_escape():
        live_item = load(it["id"])
        controls = live_item.get("controls") or {}
        elapsed = time.monotonic() - monitor_started
        if elapsed - last_heartbeat[0] >= 5:
            def beat(item): item["activity"] = {"last": now(), "state": "working", "elapsed_seconds": int(elapsed)}
            control.cas_update(it["id"], beat); last_heartbeat[0] = elapsed
        if (live_item.get("budgets") or {}).get("seconds") and elapsed >= live_item["budgets"]["seconds"]:
            return {"control": "budget", "reason": "time budget reached"}
        if controls.get("pause_requested") or controls.get("interrupt_requested"):
            return {"control": "interrupt" if controls.get("interrupt_requested") else "pause"}
        changed = set(worktree.changed_files(project, wt))
        for line in git(wt, "status", "--porcelain", "--untracked-files=all", check=False).splitlines():
            path = line[3:].split(" -> ")[-1]
            if path and path.split("/", 1)[0] not in (*worktree.DEP_DIRS, ".helm-ask.json"):
                changed.add(path)
        changed = sorted(changed - baseline_changed)
        sensitive = [p for p in changed if any(fnmatch.fnmatch(p, pattern) for pattern in project.get("protected_paths", []))]
        return sorted(set(sensitive + scope.escaped((it.get("scope") or {}).get("paths"), changed)))
    if session.get("reconnected") and (session.get("agent_status") or session.get("state")) == "working":
        agent, escaped_live = herdr.wait_agent(session["agent_name"], timeout), None
    else:
        agent, escaped_live = herdr.prompt_agent_monitored(session["agent_name"], prompt, timeout, live_escape)
        if pending:
            control.consume(it["id"], [p["id"] for p in pending], "delivered")
    agent = herdr.agent_get(session["agent_name"]) or agent
    _model_drift(model, thinking, agent)
    if isinstance(escaped_live, dict) and escaped_live.get("control"):
        run_dir = item_dir(it["id"]) / "runs" / f"herdr-{int(time.time() * 1000)}"
        run_dir.mkdir(parents=True, exist_ok=False)
        return {"ok": False, "run_dir": str(run_dir), "failed_ids": [], "control": escaped_live["control"],
                "budget_exceeded": escaped_live.get("reason"), "error": "cooperative control checkpoint"}
    if escaped_live:
        run_dir = item_dir(it["id"]) / "runs" / f"herdr-{int(time.time() * 1000)}"
        run_dir.mkdir(parents=True, exist_ok=False)
        protected_live = [p for p in escaped_live if any(fnmatch.fnmatch(p, pattern) for pattern in project.get("protected_paths", []))]
        if protected_live:
            return {"ok": False, "run_dir": str(run_dir), "failed_ids": ["protected"],
                    "error": "live protected-path change interrupted: " + ", ".join(protected_live), "changed": escaped_live}
        return {"ok": False, "run_dir": str(run_dir), "failed_ids": ["scope-escape"], "scope_escape": escaped_live,
                "error": "live scope escape interrupted: " + ", ".join(escaped_live)}
    output = herdr.agent_read(session["agent_name"], 240)
    run_dir = item_dir(it["id"]) / "runs" / f"herdr-{int(time.time() * 1000)}"
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "implementer.md").write_text(output)
    if it["kind"] == "scout":
        (item_dir(it["id"]) / "report.md").write_text(output)
        return {"ok": True, "run_dir": str(run_dir), "failed_ids": [], "tokens": agent.get("tokens"), "cost": agent.get("cost"), "reviews": []}
    if (wt / ".helm-ask.json").exists():
        return {"ok": False, "run_dir": str(run_dir), "failed_ids": ["question"], "tokens": agent.get("tokens"), "cost": agent.get("cost")}
    if not worktree.has_commits(project, wt):
        return {"ok": False, "run_dir": str(run_dir), "failed_ids": ["implement"], "error": "implementer produced no commit"}
    changed = worktree.changed_files(project, wt)
    protected = [p for p in changed if any(fnmatch.fnmatch(p, pattern) for pattern in project.get("protected_paths", []))]
    if protected:
        return {"ok": False, "run_dir": str(run_dir), "failed_ids": ["protected"],
                "error": "protected paths changed: " + ", ".join(protected), "changed": changed}
    escaped = scope.escaped((it.get("scope") or {}).get("paths"), changed)
    if escaped:
        herdr.interrupt_agent(session["agent_name"])
        return {"ok": False, "run_dir": str(run_dir), "failed_ids": ["scope-escape"], "scope_escape": escaped,
                "error": "changed files escaped declared scope: " + ", ".join(escaped)}
    # Integrate the latest configured local base before final verification and review.
    base_sha = git(project["path"], "rev-parse", project["base"])
    if git(wt, "merge-base", base_sha, "HEAD", check=False) != base_sha:
        r = sh(["git", "-C", str(wt), "rebase", base_sha], check=False)
        if r.returncode != 0:
            sh(["git", "-C", str(wt), "rebase", "--abort"], check=False)
            return {"ok": False, "run_dir": str(run_dir), "failed_ids": ["base-integration"], "error": r.stderr[-2000:]}
    verify = sh(["bash", "-c", project.get("test_cmd") or "true"], cwd=wt, check=False, timeout=timeout)
    (run_dir / "verify.md").write_text(verify.stdout + verify.stderr)
    if verify.returncode:
        return {"ok": False, "run_dir": str(run_dir), "failed_ids": ["verify"], "error": (verify.stdout + verify.stderr)[-3000:]}
    sha = git(wt, "rev-parse", "HEAD")
    reviews = []
    if it["dispatch"]["graph"] in ("direct-pr", "no-mistakes"):
        diff = git(wt, "diff", f"{base_sha}...{sha}")[-200000:]
        roles = ["correctness"] + (["adversarial"] if it["dispatch"]["graph"] == "no-mistakes" else [])
        for role in roles:
            phase = "review_" + role
            reviewer = herdr.ensure_agent(it, wt, decision["models"][phase], decision["thinking"][phase], reviewer=True)
            try:
                _model_drift(decision["models"][phase], decision["thinking"][phase], reviewer)
                verdict_file = run_dir / f"review_{role}.pending.json"
                herdr.prompt_agent(reviewer["agent_name"],
                    f"You are an independent {role} reviewer. Review commit {sha}. Do not modify the repository. "
                    "Write genuine JSON {\"verdict\":\"accept\" or \"reject\",\"notes\":\"...\"} to "
                    f"{verdict_file}, then reply with that path.\n\n" + brief.read_text() + "\n\nDIFF:\n" + diff, timeout)
                live_reviewer = herdr.agent_get(reviewer["agent_name"])
                if not live_reviewer:
                    raise HelmError("reviewer session disappeared before its verdict was captured")
                _model_drift(decision["models"][phase], decision["thinking"][phase], live_reviewer)
                evidence = herdr.agent_read(reviewer["agent_name"], 240)
                try:
                    verdict = json.loads(verdict_file.read_text())
                except (OSError, json.JSONDecodeError):
                    verdict = _json_verdict(evidence)
                if verdict.get("verdict") not in ("accept", "reject"):
                    raise HelmError("reviewer evidence has no valid verdict")
                rec = {**verdict, "sha": sha, "role": role, "reviewer": reviewer, "fresh": True,
                       "valid": True, "evidence": evidence[-12000:], "at": now()}
                reviews.append(rec); (run_dir / f"review_{role}.json").write_text(json.dumps(rec, indent=2))
                if verdict["verdict"] != "accept":
                    return {"ok": False, "run_dir": str(run_dir), "failed_ids": [phase], "reviews": reviews}
            finally:
                herdr.close_agent_tab(reviewer)
    return {"ok": True, "run_dir": str(run_dir), "failed_ids": [], "tokens": agent.get("tokens"), "cost": agent.get("cost"),
            "reviews": reviews, "sha": sha, "base_sha": base_sha, "changed": changed}


def _headless_reviews(it: dict, summary: dict, sha: str) -> list[dict]:
    """Bind only parseable pi-graph reviewer evidence to the exact post-run SHA."""
    run_dir = Path(summary.get("run_dir") or "")
    phases = ["review_correctness"] + (["review_adversarial"] if it["dispatch"]["graph"] == "no-mistakes" else [])
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
        reviews.append({**verdict, "sha": sha, "role": phase.removeprefix("review_"),
                        "reviewer": {"kind": "pi-graph", "identity": f"{run_dir}:{phase}"},
                        "fresh": True, "valid": True, "at": now(), "evidence": evidence[-12000:]})
    return reviews


def _apply_safety_pipeline(it: dict, project: dict, wt: Path, summary: dict, initial: dict, timeout: int) -> dict:
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
    if not worktree.base_is_ancestor(project, wt):
        return {**summary, "ok": False, "failed_ids": ["base-moved"],
                "error": "configured base moved during execution; full pipeline must rerun"}
    verify = sh(["bash", "-c", project.get("test_cmd") or "true"], cwd=wt, check=False, timeout=timeout)
    run_dir = Path(summary.get("run_dir") or item_dir(it["id"]))
    if run_dir.is_dir():
        (run_dir / "helm-final-verify.md").write_text(verify.stdout + verify.stderr)
    if verify.returncode:
        return {**summary, "ok": False, "failed_ids": ["verify"],
                "error": (verify.stdout + verify.stderr)[-3000:]}
    dirty = worktree.status_paths(wt)
    if dirty:
        return {**summary, "ok": False, "failed_ids": ["dirty-worktree"],
                "error": "tests mutated the worktree: " + ", ".join(dirty)}
    sha = git(wt, "rev-parse", "HEAD")
    base_sha = git(project["path"], "rev-parse", project["base"])
    reviews = summary.get("reviews") or []
    if it["dispatch"]["graph"] in ("direct-pr", "no-mistakes") and not reviews:
        reviews = _headless_reviews(it, summary, sha)
    required = 2 if it["dispatch"]["graph"] == "no-mistakes" else 1 if it["dispatch"]["graph"] == "direct-pr" else 0
    if len(reviews) != required or any(r.get("verdict") != "accept" or r.get("sha") != sha for r in reviews):
        return {**summary, "ok": False, "failed_ids": ["exact-sha-review"],
                "error": "fresh accepting reviewer evidence is not bound to the exact final SHA", "reviews": reviews}
    return {**summary, "sha": sha, "base_sha": base_sha, "changed": changed, "reviews": reviews,
            "fingerprint": worktree.signature(wt), "final_verify": True}


def _execute(it: dict, timeout: int) -> dict:
    started_monotonic = time.monotonic()
    if (it.get("budgets") or {}).get("seconds"):
        timeout = min(timeout, int(it["budgets"]["seconds"]))
    project = registry.get(it["project"])
    d = item_dir(it["id"])
    wt = worktree.create(project, it["id"])
    initial = worktree.signature(wt)
    it["integration"] = worktree.integrate_latest(project, wt)
    it["reviews"] = []
    it["activity"] = {"last": now(), "state": "integrated"}
    save(it)
    initial = worktree.signature(wt)
    brief = d / "brief.md"
    brief.write_text(brief_text(it, project))
    pinned = it.get("dispatch") or dispatch.resolve(it, project)
    fresh = dispatch.resolve(it, project)
    if fresh["models"] != pinned["models"] or fresh["thinking"] != pinned["thinking"]:
        raise HelmError("model/thinking configuration drifted after resolution; retry with an explicit override")
    dp = pinned
    dispatch.assert_available(dp)
    steps = graphs.render(dp["graph"], d, cwd=wt, branch=it["branch"], project=project,
                          models=dp["models"], thinking=dp["thinking"], timeout=timeout)
    graphs.validate(steps)
    env = worktree.git_env(wt, d / "gitexclude")
    if herdr.inside() and not os.environ.get("HELM_PIW"):
        summary = _persistent_execute(it, project, wt, brief, timeout)
    else:
        # Deterministic runner fallback is retained for tests and explicit headless use;
        # it is never represented as a Herdr agent session. A tail tab remains a display only.
        tab = None
        if herdr.inside():
            import shlex
            tab = herdr.open_tab(f"⚙ {project['id']}: {it['text'].splitlines()[0][:28]}",
                                 f"{shlex.quote(str(Path(__file__).resolve().parents[1] / 'bin' / 'helm'))} tail {it['id']}")
            if tab: herdr.remember("task", {**tab, "item": it["id"]})
        try:
            summary = graphs.run(steps, brief, timeout + 60, env=env)
            pending_ids = [event["id"] for event in (it.get("controls") or {}).get("pending", [])]
            if pending_ids:
                control.consume(it["id"], pending_ids, "delivered")
        finally:
            if tab:
                herdr.close_tab(tab["tab_id"]); herdr.forget(tab["tab_id"])
    summary = _apply_safety_pipeline(it, project, wt, summary, initial, timeout)
    elapsed = time.monotonic() - started_monotonic
    budgets = it.get("budgets") or {}
    exceeded = []
    for key, actual in (("tokens", summary.get("tokens")), ("cost", summary.get("cost")), ("seconds", elapsed)):
        limit = budgets.get(key)
        if limit is not None and actual is None:
            exceeded.append(f"{key} evidence unavailable")
        elif limit is not None and actual > limit:
            exceeded.append(f"{key} {actual:g}>{limit:g}")
    if exceeded:
        summary = {**summary, "ok": False, "failed_ids": [], "control": "budget",
                   "budget_exceeded": "; ".join(exceeded), "error": "budget threshold reached"}
    it = load(it["id"])
    it["attempts"] += 1
    it["runs"].append({"attempt": it["attempts"], "at": now(), "ok": bool(summary.get("ok")),
                       "run_dir": summary.get("run_dir"), "failed_ids": summary.get("failed_ids"),
                       "tokens": summary.get("tokens"), "cost": summary.get("cost"), "sha": summary.get("sha")})
    it["reviews"] = summary.get("reviews") or it.get("reviews", [])
    it["head_sha"] = summary.get("sha") or (git(wt, "rev-parse", "HEAD") if wt.exists() else None)
    it["changed_scope"] = summary.get("changed") or (worktree.changed_files(project, wt) if wt.exists() else [])
    it["checkpoint"] = {"sha": it["head_sha"], "at": now(), "phase": "verified" if summary.get("ok") else "checkpoint",
                        "changed_scope": it["changed_scope"], "worktree": worktree.signature(wt) if wt.exists() else None}
    it["activity"] = {"last": now(), "state": "verified" if summary.get("ok") else "attention"}
    failed_ids = set(summary.get("failed_ids") or [])
    it["rigor"] = rigor.escalate(it.get("rigor") or {}, changed=it["changed_scope"],
                                 verification_failed=bool(failed_ids.intersection({"verify", "protected", "review_correctness", "review_adversarial"})),
                                 scope_escaped=bool(summary.get("scope_escape")))
    if not summary.get("ok") and (it.get("rigor") or {}).get("level") == "high-risk":
        it["dispatch"]["graph"] = "no-mistakes"
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

    ask_file = wt / ".helm-ask.json"
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
        target_graph = "no-mistakes" if it["rigor"]["level"] == "high-risk" else project["mode"]
        if it["dispatch"]["graph"] != target_graph:
            it["dispatch"]["graph"] = target_graph; it["reviews"] = []; it["phase"] = "rigor-escalation"
            transition(it, "queued", f"observed evidence escalated rigor to {it['rigor']['level']}; rerunning stronger gates")
            return it

    if summary.get("ok"):
        if it["kind"] == "scout":
            src = Path(summary.get("run_dir") or "") / "report.md"
            if src.is_file():
                shutil.copy(src, d / "report.md")
            worktree.remove(project, it["id"], delete_branch=True)
            if it.get("session"): herdr.close_agent_tab(it["session"])
            it["phase"] = "done"
            transition(it, "done", f"report at {d / 'report.md'}")
            return it
        if not worktree.has_commits(project, wt):
            transition(it, "failed", "graph passed but produced no commits")
            return it
        it["phase"] = "merge-ready"
        deliver.after_success(it, project, wt)
        herdr.notify(f"{project['id']}: {it['status']}", it["text"].splitlines()[0][:120])
        return it

    notes = graphs.failure_notes(summary) or str(summary.get("error") or "unknown execution failure")
    signature = __import__("hashlib").sha256(("|".join(summary.get("failed_ids") or []) + "\n" + notes).encode()).hexdigest()
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
        raise HelmError(f"{work_id} is {it['status']}, not needs-you/failed")
    it["guidance"].append({"at": now(), "text": guidance.strip(), "question": (it.get("ask") or {}).get("question")})
    it["ask"] = None
    ask_file = worktree.worktree_root() / it["project"] / it["id"] / ".helm-ask.json"
    ask_file.unlink(missing_ok=True)
    if it["status"] == "failed":
        it["attempts"] = 0
    it.setdefault("controls", {})["paused"] = False
    it["phase"] = "queued"
    transition(it, "queued", "captain responded; persistent session queued with guidance")
    return it


def retry(work_id: str) -> dict:
    it = load(work_id)
    if it["status"] != "failed":
        raise HelmError(f"{work_id} is {it['status']}, not failed")
    it["attempts"] = 0
    transition(it, "queued", "manual retry")
    return it


def cancel(work_id: str) -> dict:
    it = load(work_id)
    if it["status"] == "running":
        raise HelmError("cannot cancel a running item; wait for the attempt to end")
    project = registry.get(it["project"])
    wt = worktree.worktree_root() / project["id"] / it["id"]
    if wt.exists() and (worktree.has_commits(project, wt) or worktree.status_paths(wt)):
        raise HelmError(f"{work_id} has unlanded commits or edits on {it['branch']}; explicit --discard authorization is required")
    worktree.remove(project, it["id"], delete_branch=True)
    if it.get("session"): herdr.close_agent_tab(it["session"])
    transition(it, "cancelled", "")
    return it
