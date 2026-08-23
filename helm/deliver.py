"""Delivery and captain-authorized, exact-SHA promotion transactions."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from pathlib import Path

from . import modes, worktree
from .paths import authority_lock
from .util import HelmError, git, git_config, locked, log, now, sh

_OPEN_PROMOTION = {"armed", "external-requested", "merge-observed", "cleanup-pending"}
_OPEN_PR_DELIVERY = {"armed", "push-requested", "push-confirmed", "pr-create-requested"}


def _required_review_roles(item: dict) -> list[str]:
    graph = (item.get("dispatch") or {}).get("graph")
    if modes.high_assurance(graph):
        return ["correctness", "adversarial"]
    if graph == "direct-pr":
        return ["correctness"]
    return []


def _full_sha(value: object) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{40}", str(value or "")))


def _verification(item: dict) -> dict:
    record = (item.get("verification") or [{}])[-1]
    head = item.get("head_sha")
    if not (_full_sha(head) and record.get("ok") is True and record.get("complete") is True):
        raise HelmError("promotion refused: complete successful final verification is missing")
    if record.get("head_sha") != head or not _full_sha(record.get("base_sha")):
        raise HelmError("promotion refused: verification is not bound to full exact head/base SHAs")
    fingerprint = record.get("fingerprint")
    if not isinstance(fingerprint, dict) or fingerprint.get("sha") != head or fingerprint.get("dirty") != []:
        raise HelmError("promotion refused: clean exact-SHA worktree fingerprint is missing")
    return record


def _reviews(item: dict, verification: dict) -> None:
    required = _required_review_roles(item)
    reviews = item.get("reviews") or []
    if len(reviews) != len(required):
        raise HelmError(f"promotion refused: expected {len(required)} exact-SHA review(s), found {len(reviews)}")
    by_role = {review.get("role"): review for review in reviews}
    if set(by_role) != set(required):
        raise HelmError("promotion refused: required independent reviewer roles are incomplete or duplicated")
    for role in required:
        review = by_role[role]
        if (review.get("verdict") != "accept" or review.get("sha") != item.get("head_sha")
                or review.get("fresh") is not True or review.get("valid") is not True
                or not review.get("reviewer")):
            raise HelmError(f"promotion refused: {role} approval is not fresh and exact-SHA-bound")
        review_base = review.get("base_sha")
        if review_base != verification.get("base_sha"):
            raise HelmError(f"promotion refused: {role} approval used a different base SHA")


def _worktree_path(item: dict, project: dict) -> Path:
    expected = (worktree.worktree_root() / project["id"] / item["id"]).resolve()
    recorded = Path(item.get("worktree") or expected).expanduser().resolve()
    if recorded != expected:
        raise HelmError("promotion refused: recorded worktree escapes the item's owned path")
    return expected


def _validate(item: dict, project: dict, *, require_worktree: bool = True) -> tuple[dict, Path]:
    from . import gates

    if item.get("project") != project.get("id"):
        raise HelmError("promotion refused: item/project binding changed")
    if item.get("status") not in ("ready", "pr-open"):
        raise HelmError(f"{item['id']} is {item.get('status')}; only ready/pr-open items promote")
    gates.require_execution(project.get("gate", "native"), project["path"])
    gates.require_receipt(project.get("gate", "native"), item, project)
    verification = _verification(item)
    _reviews(item, verification)
    controls = item.get("controls") or {}
    unresolved = [event for event in controls.get("events", [])
                  if event.get("state") in ("pending", "delivering")]
    if unresolved or controls.get("paused") or controls.get("pause_requested") or controls.get("interrupt_requested"):
        raise HelmError("promotion refused: unresolved captain control must be reconciled first")

    wt = _worktree_path(item, project)
    if require_worktree and not wt.is_dir():
        raise HelmError("promotion refused: verified worktree is missing; only an armed transaction may reconcile it")
    if wt.is_dir():
        if not worktree.is_clean(wt):
            raise HelmError("worktree mutated after verification; approval is invalid")
        current = git(wt, "rev-parse", "HEAD", check=False)
        if current != item["head_sha"]:
            raise HelmError("branch mutated after verification; approval is invalid")
        if worktree.signature(wt) != verification["fingerprint"]:
            raise HelmError("worktree fingerprint changed after verification; approval is invalid")
        branch_sha = git(project["path"], "rev-parse", "--verify", item["branch"], check=False)
        if branch_sha != item["head_sha"]:
            raise HelmError("promotion refused: item branch no longer names the reviewed exact SHA")
        ancestry = sh(["git", "-C", str(wt), "merge-base", "--is-ancestor",
                       verification["base_sha"], item["head_sha"]], check=False)
        if ancestry.returncode:
            raise HelmError("promotion refused: reviewed base is not an ancestor of the exact head SHA")
    base_now = git(project["path"], "rev-parse", project["base"], check=False)
    if base_now != verification["base_sha"]:
        raise HelmError("configured base moved after verification; recover the item to integrate and review again")
    return verification, wt


def after_success(it: dict, project: dict, wt: Path) -> None:
    from .work import transition

    head = git(wt, "rev-parse", "HEAD")
    it["head_sha"] = head
    if not worktree.is_clean(wt):
        raise HelmError("delivery refused: worktree has uncommitted or untracked mutations")
    required = _required_review_roles(it)
    reviews = it.get("reviews") or []
    reviewed_base = ((it.get("verification") or [{}])[-1]).get("base_sha")
    if len(reviews) != len(required) or any(
        r.get("verdict") != "accept" or r.get("sha") != head or r.get("valid") is not True
        or r.get("base_sha") != reviewed_base or r.get("fresh") is not True or not r.get("reviewer")
        for r in reviews
    ):
        raise HelmError("independent approval is missing or not bound to the exact delivery SHA")
    if project.get("gate", "native") == "no-mistakes":
        from . import gates
        completed = gates.start_or_reconcile(it, project, wt)
        it.clear(); it.update(completed)
        return
    mode = project["mode"]
    has_origin = bool(git_config(project["path"], "remote.origin.url"))
    if mode == "local-only" or project["authority"] < 2 or not has_origin:
        note = f"branch {it['branch']} ready in {wt}"
        if mode != "local-only" and project["authority"] >= 2 and not has_origin:
            note += " (no origin remote: PR not possible)"
        elif mode != "local-only" and project["authority"] < 2:
            note += " (authority < 2: PR not opened)"
        transition(it, "ready", note)
        return
    completed = resume_pr_delivery(it, project, wt)
    it.clear(); it.update(completed)


def has_resumable_pr_delivery(item: dict) -> bool:
    delivery = item.get("pr_delivery") or {}
    state = delivery.get("state")
    if state in _OPEN_PR_DELIVERY:
        return True
    # A complete receipt is resumable only across the narrow crash window
    # before the item transition. Steering a ready/PR item clears reviews and
    # must start new work, never resurrect an older PR receipt.
    exact_reviews = item.get("reviews") or []
    return (state == "complete" and item.get("status") != "pr-open"
            and delivery.get("head_sha") == item.get("head_sha")
            and bool(exact_reviews)
            and all(review.get("sha") == item.get("head_sha") and review.get("valid") is True
                    for review in exact_reviews))


def _pr_policy(project: dict) -> dict:
    return {key: project.get(key) for key in
            ("id", "path", "mode", "authority", "base", "test_cmd", "protected_paths", "gate")}


def _remote_head(project: dict, branch: str, origin: str | None = None) -> str | None:
    """Observe one exact remote ref; command failure is uncertainty, never absence."""
    ref = f"refs/heads/{branch}"
    explicit_origin = origin or git_config(project["path"], "remote.origin.url")
    if not explicit_origin:
        raise HelmError("remote branch observation has no configured origin")
    result = sh(["git", "-C", str(project["path"]), "ls-remote", "--heads",
                 explicit_origin, ref], check=False, network=True)
    if result.returncode:
        raise HelmError("remote branch observation unavailable; PR delivery remains journaled")
    lines = [line.split() for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        return None
    if (len(lines) != 1 or len(lines[0]) != 2 or lines[0][1] != ref
            or not _full_sha(lines[0][0])):
        raise HelmError("remote branch observation was ambiguous or malformed")
    return lines[0][0]


def _query_pr(project: dict, delivery: dict) -> dict | None:
    """Return only one exact repository/base/head-bound PR from authoritative JSON."""
    args = ["gh", "pr", "list", "--repo", delivery["repository"], "--state", "all",
            "--head", delivery["branch"], "--limit", "100", "--json",
            "url,number,state,headRefName,headRefOid,baseRefName"]
    result = sh(args, cwd=project["path"], check=False, network=True)
    if result.returncode:
        raise HelmError("GitHub PR observation unavailable; delivery remains unresolved")
    try:
        rows = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise HelmError("GitHub PR observation returned invalid JSON") from exc
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise HelmError("GitHub PR observation returned an invalid shape")
    related = [row for row in rows if row.get("headRefName") == delivery["branch"]]
    exact = [row for row in related if row.get("headRefOid") == delivery["head_sha"]
             and row.get("baseRefName") == delivery["base_ref"]]
    if any(row not in exact for row in related):
        raise HelmError("a GitHub PR for the item branch has a changed head or base; refusing to infer identity")
    if len(exact) > 1:
        raise HelmError("multiple exact GitHub PRs matched one delivery journal")
    if not exact:
        return None
    row = exact[0]
    url = str(row.get("url") or "")
    from .forge import _pull
    parsed = _pull(url)
    state = str(row.get("state") or "").upper()
    if not parsed or parsed[0].lower() != str(delivery["repository"]).lower():
        raise HelmError("GitHub returned a non-canonical or foreign PR URL")
    if state != "OPEN":
        raise HelmError(f"the exact delivery PR is {state or 'UNKNOWN'}; captain reconciliation is required")
    return {"url": url, "number": row.get("number"), "state": state,
            "head_sha": row.get("headRefOid"), "base_ref": row.get("baseRefName")}


def _validate_pr_side_effect(item: dict, project: dict, delivery: dict, wt: Path) -> None:
    """Revalidate every frozen input immediately before a new external mutation."""
    from . import gates, work
    item = work.load(item["id"])
    active = item.get("pr_delivery") or {}
    if (active.get("id") != delivery.get("id") or active.get("state") not in _OPEN_PR_DELIVERY
            or item.get("status") != "running" or item.get("head_sha") != delivery.get("head_sha")):
        raise HelmError("item or PR delivery identity changed before the external side effect")
    verification = _verification(item)
    _reviews(item, verification)
    if (verification.get("base_sha") != delivery.get("base_sha")
            or verification.get("fingerprint") != delivery.get("worktree_fingerprint")):
        raise HelmError("verification evidence changed after PR delivery armed")
    controls = item.get("controls") or {}
    unresolved = [event for event in controls.get("events", [])
                  if event.get("state") in ("pending", "delivering")]
    if (unresolved or controls.get("paused") or controls.get("pause_requested")
            or controls.get("interrupt_requested")):
        raise HelmError("PR delivery refused: unresolved captain control must be reconciled first")
    if (_pr_policy(project) != delivery.get("project_policy")
            or delivery.get("repository_path") != str(Path(project["path"]).resolve())
            or delivery.get("base_ref") != project.get("base")
            or delivery.get("gate_provider") != project.get("gate", "native")):
        raise HelmError("registered project policy changed after PR delivery armed")
    gates.require_execution(project.get("gate", "native"), project["path"])
    if git_config(project["path"], "remote.origin.url") != delivery.get("origin"):
        raise HelmError("origin changed after PR delivery armed")
    if git(project["path"], "rev-parse", project["base"], check=False) != delivery.get("base_sha"):
        raise HelmError("base moved after review; PR delivery cannot create a new side effect")
    if not wt.is_dir() or worktree.signature(wt) != delivery.get("worktree_fingerprint"):
        raise HelmError("worktree changed after review; PR delivery remains preserved")
    if (git(wt, "rev-parse", "HEAD", check=False) != delivery.get("head_sha")
            or git(project["path"], "rev-parse", "--verify", delivery["branch"], check=False)
            != delivery.get("head_sha")):
        raise HelmError("item branch changed after PR delivery armed")


def _pr_update(work_id: str, intent_id: str, expected_states: set[str], **changes) -> dict:
    from . import control, work
    item = work.load(work_id)
    def mutate(current):
        active = current.get("pr_delivery") or {}
        if active.get("id") != intent_id or active.get("state") not in expected_states:
            raise HelmError("PR delivery journal changed during reconciliation")
        active.update(changes)
    return control.cas_update(work_id, mutate, expected_revision=int(item.get("revision", 0)))


def _complete_pr_delivery(item: dict, receipt: dict) -> dict:
    delivery = item.get("pr_delivery") or {}
    return _pr_update(item["id"], delivery["id"], _OPEN_PR_DELIVERY,
                      state="complete", completed_at=now(), pr_receipt=receipt,
                      pr_url=receipt["url"])


def _arm_pr_delivery(item: dict, project: dict, wt: Path) -> dict:
    from . import control, forge
    verification = _verification(item)
    _reviews(item, verification)
    origin = git_config(project["path"], "remote.origin.url")
    repository = forge._github_repo(origin)
    if not repository:
        raise HelmError("origin is not a recognized GitHub repository; PR delivery refused")
    if worktree.signature(wt) != verification["fingerprint"]:
        raise HelmError("worktree changed before PR delivery could arm")
    intent_id = secrets.token_hex(16)
    safe_text = str(control.redact(item["text"]))
    title = safe_text.strip().splitlines()[0][:70]
    body = (f"helm work item `{item['id']}`\n\n{safe_text}\n\n"
            f"Graph: {item['dispatch']['graph']} · rule: {item['dispatch']['rule']}\n")
    expected_revision = int(item.get("revision", 0))
    def arm(current):
        if current.get("status") not in {"running", "failed", "queued"} or current.get("head_sha") != item.get("head_sha"):
            raise HelmError("item changed before PR delivery could arm")
        prior = current.get("pr_delivery") or {}
        if prior.get("state") in _OPEN_PR_DELIVERY:
            raise HelmError("an earlier PR delivery requires reconciliation")
        current["pr_delivery"] = {
            "id": intent_id, "state": "armed", "armed_at": now(), "project_id": project["id"],
            "repository_path": str(Path(project["path"]).resolve()), "repository": repository,
            "origin": origin, "base_ref": project["base"], "base_sha": verification["base_sha"],
            "head_sha": item["head_sha"], "branch": item["branch"],
            "worktree_path": str(wt.resolve()), "worktree_fingerprint": verification["fingerprint"],
            "gate_provider": project.get("gate", "native"), "project_policy": _pr_policy(project),
            "title": title, "body": body, "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
        }
    return control.cas_update(item["id"], arm, expected_revision=expected_revision)


def _open_pr_locked(it: dict, project: dict, wt: Path) -> str:
    """Create/reconcile a PR through a durable exact-identity transaction."""
    from . import work
    item = work.load(it["id"])
    delivery = item.get("pr_delivery") or {}
    if delivery.get("state") == "complete":
        receipt = delivery.get("pr_receipt") or {}
        if not receipt.get("url"):
            raise HelmError("completed PR delivery has no exact receipt")
        return receipt["url"]
    if delivery.get("state") not in _OPEN_PR_DELIVERY:
        if project.get("authority", 0) < 2 or project.get("mode") == "local-only":
            raise HelmError("PR delivery refused: current registered authority does not permit opening a PR")
        item = _arm_pr_delivery(item, project, wt)
        delivery = item["pr_delivery"]
    if (delivery.get("project_id") != project.get("id")
            or delivery.get("repository_path") != str(Path(project["path"]).resolve())
            or delivery.get("worktree_path") != str(wt.resolve())):
        raise HelmError("PR delivery journal no longer matches its project/worktree")

    # Observation is always safe, including after policy/base drift. Finding the
    # exact PR reconciles an earlier uncertain create without another mutation.
    receipt = _query_pr(project, delivery)
    if receipt:
        completed = _complete_pr_delivery(item, receipt)
        return completed["pr_delivery"]["pr_receipt"]["url"]

    state = delivery["state"]
    remote_head = _remote_head(project, delivery["branch"], delivery["origin"])
    if remote_head not in (None, delivery["head_sha"]):
        raise HelmError("remote item branch changed; refusing to overwrite it")
    if state == "armed":
        _validate_pr_side_effect(item, project, delivery, wt)
        item = _pr_update(item["id"], delivery["id"], {"armed"}, state="push-requested",
                          push_requested_at=now())
        delivery = item["pr_delivery"]; state = delivery["state"]
    if state == "push-requested" and remote_head is None:
        _validate_pr_side_effect(item, project, delivery, wt)
        result = sh(["git", "-C", str(wt), "push", "--porcelain", delivery["origin"],
                     f"{delivery['head_sha']}:refs/heads/{delivery['branch']}"],
                    check=False, network=True)
        if result.returncode:
            raise HelmError("exact-SHA non-force push failed; PR delivery remains journaled")
        remote_head = _remote_head(project, delivery["branch"], delivery["origin"])
    if state == "push-requested":
        if remote_head != delivery["head_sha"]:
            raise HelmError("remote did not prove the journaled exact SHA after push")
        item = _pr_update(item["id"], delivery["id"], {"push-requested"},
                          state="push-confirmed", push_confirmed_at=now(), remote_head=remote_head)
        delivery = item["pr_delivery"]; state = delivery["state"]

    receipt = _query_pr(project, delivery)
    if receipt:
        completed = _complete_pr_delivery(item, receipt)
        return completed["pr_delivery"]["pr_receipt"]["url"]
    create_now = False
    if state == "push-confirmed":
        _validate_pr_side_effect(item, project, delivery, wt)
        item = _pr_update(item["id"], delivery["id"], {"push-confirmed"},
                          state="pr-create-requested", pr_create_requested_at=now())
        delivery = item["pr_delivery"]; state = delivery["state"]
        create_now = True
    if state != "pr-create-requested":
        raise HelmError("PR delivery reached an unknown journal phase")
    if not create_now:
        # The durable request may already have reached GitHub. An empty lookup
        # during outage/eventual consistency is uncertainty, not permission to
        # issue a second create. A later reconciliation may observe the PR.
        raise HelmError("PR create was already requested but no exact PR is currently observable; refusing automatic replay")
    _validate_pr_side_effect(item, project, delivery, wt)
    result = sh(["gh", "pr", "create", "--repo", delivery["repository"],
                 "--head", delivery["branch"], "--base", delivery["base_ref"],
                 "--title", delivery["title"], "--body", delivery["body"]],
                cwd=wt, check=False, network=True)
    # Never trust command stdout as identity. The exact authoritative lookup is
    # the sole receipt, whether the create returned success, an existing-PR
    # error, or the controller died after GitHub accepted it.
    receipt = _query_pr(project, delivery)
    if not receipt:
        reason = "GitHub did not expose an exact PR after create"
        if result.returncode:
            reason += f" (command exit {result.returncode})"
        raise HelmError(reason + "; delivery remains journaled")
    completed = _complete_pr_delivery(item, receipt)
    return completed["pr_delivery"]["pr_receipt"]["url"]


def open_pr(it: dict, project: dict, wt: Path) -> str:
    """Serialize every PR mutation against registry, away, and recovery policy."""
    from . import registry

    with locked(authority_lock()):
        current = registry.get(it["project"])
        if (project.get("id") != current.get("id")
                or Path(str(project.get("path", ""))).expanduser().resolve()
                != Path(current["path"]).expanduser().resolve()):
            raise HelmError("PR delivery refused: caller project binding is stale or mismatched")
        return _open_pr_locked(it, current, wt)


def resume_pr_delivery(it: dict, project: dict, wt: Path | None = None) -> dict:
    """Finish PR delivery without starting another implementer/model turn."""
    from . import work
    wt = wt or _worktree_path(it, project)
    url = open_pr(it, project, wt)
    latest = work.load(it["id"])
    if latest.get("status") != "pr-open":
        latest["pr_url"] = url; latest["phase"] = "pr-open"
        work.transition(latest, "pr-open", url)
    return latest


def _arm(item: dict, project: dict, kind: str, verification: dict,
         forge_evidence: dict | None = None) -> dict:
    from . import control

    intent_id = secrets.token_hex(16)
    expected = int(item.get("revision", 0))
    def mutate(current):
        if current.get("status") != item.get("status") or current.get("head_sha") != item.get("head_sha"):
            raise HelmError("promotion inputs changed before the transaction could arm")
        prior = current.get("promotion") or {}
        if prior.get("state") in _OPEN_PROMOTION:
            raise HelmError("an earlier promotion transaction requires reconciliation")
        current["promotion"] = {
            "id": intent_id, "kind": kind, "state": "armed", "armed_at": now(),
            "project_id": project["id"], "repository_path": str(Path(project["path"]).resolve()),
            "base_ref": project["base"], "base_sha": verification["base_sha"],
            "gate_provider": project.get("gate", "native"),
            "project_policy": {key: project.get(key) for key in
                               ("mode", "authority", "base", "test_cmd", "protected_paths", "gate")},
            "head_sha": item["head_sha"], "branch": item["branch"], "pr_url": item.get("pr_url"),
            "verification_fingerprint": verification["fingerprint"],
            "github_merge_policy": (((forge_evidence or {}).get("checks") or {}).get("merge_policy")
                                    if kind == "github" else None),
            "github_observation_fingerprint": (forge_evidence or {}).get("fingerprint"),
        }
    return control.cas_update(item["id"], mutate, expected_revision=expected)


def _mark_requested(item: dict) -> dict:
    from . import control

    intent_id = (item.get("promotion") or {}).get("id")
    def mutate(current):
        promotion = current.get("promotion") or {}
        if promotion.get("id") != intent_id or promotion.get("state") != "armed":
            raise HelmError("promotion transaction changed before its side effect")
        promotion.update(state="external-requested", requested_at=now())
    return control.cas_update(item["id"], mutate, expected_revision=int(item.get("revision", 0)))


def _record_attempt(work_id: str, intent_id: str, observation: dict | None,
                    command_result=None) -> dict:
    from . import control, work

    item = work.load(work_id)
    def mutate(current):
        promotion = current.get("promotion") or {}
        if promotion.get("id") != intent_id or promotion.get("state") not in _OPEN_PROMOTION:
            raise HelmError("promotion transaction changed during reconciliation")
        attempt = {"at": now()}
        if command_result is not None:
            from .control import redact
            attempt.update(command_exit=command_result.returncode,
                           command_stderr=redact((command_result.stderr or "")[-1000:]))
        if observation is not None:
            attempt.update(observed_classification=observation.get("classification"),
                           observation_fingerprint=observation.get("fingerprint"))
        promotion.setdefault("attempts", []).append(attempt)
        promotion["attempts"] = promotion["attempts"][-20:]
    return control.cas_update(work_id, mutate, expected_revision=int(item.get("revision", 0)))


def _mark_merged(item: dict, note: str, evidence: dict) -> dict:
    from . import control

    intent = item.get("promotion") or {}
    intent_id = intent.get("id"); head = intent.get("head_sha")
    def mutate(current):
        promotion = current.get("promotion") or {}
        if promotion.get("id") != intent_id or promotion.get("head_sha") != head:
            raise HelmError("promotion transaction identity changed before merge recording")
        before = current.get("status")
        current.setdefault("history", []).append({"at": now(), "from": before, "to": "merged", "note": note})
        current["status"] = "merged"; current["phase"] = "merged"
        current["activity"] = {"last": now(), "state": "merged"}
        promotion.update(state="cleanup-pending", merge_observed_at=now(), merge_evidence=evidence)
    return control.cas_update(item["id"], mutate, expected_revision=int(item.get("revision", 0)))


def _cleanup(item: dict, project: dict) -> dict:
    from . import control, herdr, work

    promotion = item.get("promotion") or {}
    intent_id = promotion.get("id")
    wt = _worktree_path(item, project)

    def phase(name: str, **details) -> dict:
        latest = work.load(item["id"])
        def mutate(current):
            active = current.get("promotion") or {}
            if active.get("id") != intent_id or current.get("status") != "merged":
                raise HelmError("promotion cleanup identity changed")
            active.update(cleanup_phase=name, cleanup_phase_at=now(), **details)
        return control.cas_update(item["id"], mutate, expected_revision=int(latest.get("revision", 0)))

    def blocked(reason: str) -> None:
        phase("blocked", cleanup_blocked_at=now(), cleanup_blocked_reason=reason)

    cleanup_phase = promotion.get("cleanup_phase")
    if cleanup_phase not in {"agent-quiesced", "worktree-removal-requested", "worktree-removed"}:
        phase("agent-quiescing")
        current = work.load(item["id"])
        if current.get("session") and not herdr.close_agent_tab(current["session"]):
            blocked("implementer settlement/tab closure is unproven")
            raise HelmError("merge is proven, but cleanup was refused because the implementer is not positively quiesced")
        phase("agent-quiesced")

    item = work.load(item["id"]); promotion = item.get("promotion") or {}
    cleanup_phase = promotion.get("cleanup_phase")
    expected_fingerprint = promotion.get("verification_fingerprint")
    branch_locations = worktree.branch_worktrees(project, item["branch"])
    foreign_locations = [path for path in branch_locations if path != wt.resolve()]
    if foreign_locations:
        blocked("the item branch is attached to a different worktree")
        raise HelmError("merge is proven, but cleanup found the branch in another worktree; preserving it")
    if wt.exists():
        actual = worktree.signature(wt)
        if (actual.get("sha") != promotion.get("head_sha") or actual.get("dirty") != []
                or actual != expected_fingerprint):
            blocked("worktree changed after promotion armed")
            raise HelmError("merge is proven, but cleanup was refused because unlanded work appeared after approval; worktree preserved")
    elif cleanup_phase != "worktree-removal-requested" and cleanup_phase != "worktree-removed":
        blocked("verified worktree disappeared before a removal intent was durable")
        raise HelmError("merge is proven, but the worktree disappeared before cleanup could journal removal")

    if cleanup_phase != "worktree-removed":
        phase("worktree-removal-requested", expected_fingerprint=expected_fingerprint)
        worktree.remove(project, item["id"], delete_branch=False, expected=expected_fingerprint)
        # A concurrent checkout/mutation never becomes successful cleanup.
        if worktree.branch_worktrees(project, item["branch"]):
            blocked("item branch remained attached after clean worktree removal")
            raise HelmError("clean worktree removal could not be proven; branch and state were preserved")
        current_branch = git(project["path"], "rev-parse", "--verify", item["branch"], check=False)
        if current_branch != promotion.get("head_sha"):
            blocked("item branch changed during worktree cleanup")
            raise HelmError("item branch changed during cleanup; the ref was preserved")
        phase("worktree-removed", branch_retained=True, retained_branch_sha=current_branch)

    # Retaining the already-merged exact branch is deliberate recovery evidence.
    # Automatic ref deletion has an unavoidable race with an external worktree
    # checkout; doctor can later offer an explicitly confirmed reconciliation.
    latest = work.load(item["id"])
    def complete(current):
        active = current.get("promotion") or {}
        if (active.get("id") != intent_id or current.get("status") != "merged"
                or active.get("cleanup_phase") != "worktree-removed"):
            raise HelmError("promotion cleanup identity changed")
        active.update(state="complete", cleanup_phase="complete", completed_at=now(),
                      branch_retained=True)
    return control.cas_update(item["id"], complete, expected_revision=int(latest.get("revision", 0)))


def _reconcile(item: dict, project: dict) -> dict | None:
    """Resolve only a previously armed exact transaction from authoritative state."""
    from . import forge, work

    promotion = item.get("promotion") or {}
    if promotion.get("state") not in _OPEN_PROMOTION:
        return None
    if (promotion.get("project_id") != project.get("id")
            or promotion.get("repository_path") != str(Path(project["path"]).resolve())
            or promotion.get("head_sha") != item.get("head_sha")):
        raise HelmError("promotion journal no longer matches the registered item/project")
    if promotion.get("state") == "cleanup-pending" and item.get("status") == "merged":
        completed = _cleanup(item, project)
        return {"state": "merged", "ref": promotion.get("pr_url") or promotion.get("head_sha"), "item": completed}
    if promotion.get("kind") == "github":
        observation = forge.monitor_item(item["id"], force=True)
        latest = work.load(item["id"])
        if (observation.get("classification") == "merged" and observation.get("evidence_complete")
                and (observation.get("expected") or {}).get("head_sha") == promotion.get("head_sha")):
            merged = _mark_merged(latest, str(promotion.get("pr_url")), observation)
            completed = _cleanup(merged, project)
            return {"state": "merged", "ref": promotion.get("pr_url"), "item": completed}
        return {"state": "unresolved", "observation": observation, "item": latest}
    if promotion.get("kind") == "local":
        base_head = git(project["path"], "rev-parse", promotion.get("base_ref") or project["base"], check=False)
        if base_head == promotion.get("head_sha"):
            merged = _mark_merged(item, f"exact SHA {base_head}",
                                  {"provider": "git", "base_ref": promotion.get("base_ref"), "head_sha": base_head})
            completed = _cleanup(merged, project)
            return {"state": "merged", "ref": base_head, "item": completed}
        return {"state": "unresolved", "observation": {"classification": "not-merged", "base_sha": base_head}, "item": item}
    raise HelmError("promotion journal has an unknown kind")


def promote(it: dict, project: dict, confirm: bool) -> dict:
    """Captain's explicit word, serialized and journaled; never called by the daemon."""
    from . import forge, registry, supervisor, work

    if not confirm:
        raise HelmError("promotion needs --confirm (the captain's explicit word)")
    with locked(authority_lock()):
        if supervisor.away_status().get("enabled"):
            raise HelmError("promotion refused while away mode is enabled; return first")
        item = work.load(it["id"])
        project = registry.get(item["project"])
        if project["authority"] < 3:
            raise HelmError(f"project '{project['id']}' authority {project['authority']} < 3: raise it with "
                            f"`helm set {project['id']} --authority 3` if you really want helm merging")

        reconciled = _reconcile(item, project)
        if reconciled and reconciled["state"] == "merged":
            return reconciled
        if reconciled:
            item = reconciled["item"]
            promotion = item.get("promotion") or {}
            from . import gates
            # Observation/reconciliation of an already-completed side effect is
            # always allowed. Any *new* irreversible request must revalidate the
            # current fail-closed gate and the exact policy that armed it.
            if promotion.get("base_ref") != project.get("base"):
                raise HelmError("promotion remains unresolved and the configured base ref changed")
            if promotion.get("gate_provider") != project.get("gate", "native"):
                raise HelmError("promotion remains unresolved and the fail-closed gate provider changed")
            armed_policy = promotion.get("project_policy")
            current_policy = {key: project.get(key) for key in
                              ("mode", "authority", "base", "test_cmd", "protected_paths", "gate")}
            if armed_policy is not None and armed_policy != current_policy:
                raise HelmError("promotion remains unresolved and the registered project policy changed")
            gates.require_execution(project.get("gate", "native"), project["path"])
            if promotion.get("state") == "armed":
                item = _mark_requested(item); promotion = item["promotion"]
            elif promotion.get("state") != "external-requested":
                raise HelmError("armed promotion is not merged and cannot be replayed safely")
            observation = reconciled.get("observation") or {}
            if promotion.get("kind") == "github":
                if observation.get("classification") != "checks-green" or not observation.get("evidence_complete"):
                    raise HelmError(f"promotion remains unresolved: {observation.get('classification')} — {observation.get('reason', '')}")
            elif (promotion.get("kind") != "local" or observation.get("classification") != "not-merged"
                  or observation.get("base_sha") != promotion.get("base_sha")):
                raise HelmError("local promotion outcome is unresolved; exact base evidence changed")
        else:
            verification, _ = _validate(item, project)
            kind = "github" if item.get("status") == "pr-open" and item.get("pr_url") else "local"
            evidence = None
            if kind == "github":
                evidence = forge.require_green(item["id"])
                item = work.load(item["id"])
                if (evidence.get("expected") or {}).get("head_sha") != item.get("head_sha"):
                    raise HelmError("GitHub observation no longer matches the item head SHA")
                verification, _ = _validate(item, project)
            else:
                repo = Path(project["path"])
                if git(repo, "status", "--porcelain", "--untracked-files=all"):
                    raise HelmError(f"{repo} has uncommitted changes; commit or stash them before arming promotion")
                current = git(repo, "symbolic-ref", "--short", "HEAD", check=False)
                if current != project["base"]:
                    raise HelmError(f"{repo} is on '{current}', not base '{project['base']}'")
            item = _arm(item, project, kind, verification, evidence)
            item = _mark_requested(item)
            promotion = item["promotion"]

        # The arm journal is not standing authority. Re-read and revalidate the
        # entire project/gate/worktree policy immediately before every new
        # merge request, including the first call after arming.
        project = registry.get(item["project"])
        current_policy = {key: project.get(key) for key in
                          ("mode", "authority", "base", "test_cmd", "protected_paths", "gate")}
        if (project.get("authority", 0) < 3
                or promotion.get("gate_provider") != project.get("gate", "native")
                or promotion.get("project_policy") != current_policy):
            raise HelmError("promotion authority or project/gate policy changed after arming")
        _validate(item, project)

        if promotion["kind"] == "github":
            immediate = forge.require_green(item["id"])
            immediate_policy = ((immediate.get("checks") or {}).get("merge_policy") or {})
            if (not promotion.get("github_merge_policy")
                    or immediate_policy != promotion.get("github_merge_policy")
                    or (immediate.get("expected") or {}).get("base_sha") != promotion.get("base_sha")
                    or (immediate.get("expected") or {}).get("head_sha") != promotion.get("head_sha")):
                raise HelmError("GitHub merge policy/base/head evidence changed after promotion armed")
            result = sh(["gh", "pr", "merge", promotion["pr_url"], "--merge",
                         "--match-head-commit", promotion["head_sha"]], cwd=project["path"],
                        check=False, network=True)
            observation = forge.monitor_item(item["id"], force=True)
            latest = _record_attempt(item["id"], promotion["id"], observation, result)
            if (observation.get("classification") == "merged" and observation.get("evidence_complete")
                    and (observation.get("expected") or {}).get("head_sha") == promotion["head_sha"]):
                merged = _mark_merged(latest, promotion["pr_url"], observation)
                completed = _cleanup(merged, project)
                return {"state": "merged", "ref": promotion["pr_url"], "item": completed}
            if result.returncode:
                raise HelmError(f"GitHub merge outcome is not proven ({observation.get('classification')}): "
                                f"{(result.stderr or result.stdout).strip()[-500:]}")
            return {"state": "merge-requested", "ref": promotion["pr_url"], "item": latest,
                    "observation": observation}

        repo = Path(project["path"])
        if git(repo, "status", "--porcelain", "--untracked-files=all"):
            raise HelmError(f"{repo} has uncommitted changes; exact local promotion remains armed")
        current = git(repo, "symbolic-ref", "--short", "HEAD", check=False)
        if current != project["base"]:
            raise HelmError(f"{repo} is on '{current}', not base '{project['base']}'")
        result = sh(["git", "-C", str(repo), "merge", "--ff-only", promotion["head_sha"]], check=False)
        base_after = git(repo, "rev-parse", project["base"], check=False)
        latest = _record_attempt(item["id"], promotion["id"],
                                 {"classification": "merged" if base_after == promotion["head_sha"] else "not-merged",
                                  "base_sha": base_after}, result)
        if base_after != promotion["head_sha"]:
            raise HelmError(f"fast-forward outcome is not the reviewed exact SHA: {(result.stderr or result.stdout).strip()[-500:]}")
        merged = _mark_merged(latest, f"fast-forwarded {project['base']} to {base_after}",
                              {"provider": "git", "base_ref": project["base"], "head_sha": base_after})
        completed = _cleanup(merged, project)
        log(f"{item['id']}: merged into {project['base']} @ {base_after}")
        return {"state": "merged", "ref": base_after, "item": completed}
