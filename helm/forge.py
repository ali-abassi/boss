"""GitHub lifecycle evidence bound to repository, PR, base, and exact head SHA."""
from __future__ import annotations
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse, quote
from .paths import home
from .util import HelmError, locked, now, git_config, project_sh

CLASSIFICATIONS = ("checks-pending", "checks-failed", "checks-green", "merged", "closed",
                   "changed-head", "moved-base", "unknown")
PASSING_CONCLUSIONS = {"success", "neutral", "skipped"}
PENDING_STATUSES = {"queued", "in_progress", "waiting", "requested", "pending"}
FAILING_CONCLUSIONS = {"failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale", "error"}
DEFAULT_INTERVAL_SECONDS = 60


def _run(args: list[str], cwd: Path | str, timeout: int = 20) -> subprocess.CompletedProcess:
    try:
        return project_sh(args, cwd=cwd, check=False, timeout=timeout, network=True)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(args, 124, "", f"{type(exc).__name__}: {exc}")


def _github_repo(remote: str) -> str | None:
    remote = remote.strip()
    match = re.match(r"^git@github\.com:([^/]+/[^/]+?)(?:\.git)?$", remote)
    if not match:
        parsed = urlparse(remote)
        if parsed.hostname != "github.com": return None
        match = re.match(r"^/([^/]+/[^/]+?)(?:\.git)?/?$", parsed.path)
    if not match: return None
    value = match.group(1).removesuffix(".git")
    return value if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value) else None


def _pull(url: str) -> tuple[str, int] | None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "github.com": return None
    match = re.fullmatch(r"/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/(\d+)/?", parsed.path)
    return (f"{match.group(1)}/{match.group(2)}", int(match.group(3))) if match else None


def _api(repo_path: Path, endpoint: str) -> tuple[dict | None, dict | None]:
    if not shutil.which("gh"):
        return None, {"kind": "unavailable", "reason": "GitHub CLI is not installed"}
    result = _run(["gh", "api", "--method", "GET", endpoint], repo_path)
    if result.returncode:
        message = (result.stderr or result.stdout).strip()[-1000:]
        from .control import redact
        message = redact(message)
        lower = message.lower()
        kind = "rate-limited" if "rate limit" in lower or "http 429" in lower else "outage-or-auth"
        return None, {"kind": kind, "reason": message or f"gh api exited {result.returncode}"}
    try: value = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None, {"kind": "invalid-response", "reason": "GitHub returned non-JSON evidence"}
    if not isinstance(value, dict):
        return None, {"kind": "invalid-response", "reason": "GitHub returned a non-object response"}
    return value, None


def _unknown(reason: str, expected: dict, **extra) -> dict:
    return {"provider": "github", "classification": "unknown", "reason": reason,
            "evidence_complete": False, "expected": expected, "observed_at": now(), **extra}


def inspect(item: dict, project: dict) -> dict:
    """Read GitHub only. No state write and no merge side effect."""
    expected = {
        "repository": None, "pr_url": item.get("pr_url"), "head_sha": item.get("head_sha"),
        "base_ref": project.get("base"),
        "base_sha": ((item.get("verification") or [{}])[-1]).get("base_sha"),
    }
    pull = _pull(str(item.get("pr_url") or ""))
    if not pull:
        return _unknown("PR URL is not a canonical github.com pull request", expected)
    pr_repo, number = pull
    origin = git_config(project["path"], "remote.origin.url")
    origin_repo = _github_repo(origin)
    expected["repository"] = origin_repo
    if not origin_repo:
        return _unknown("origin is not a recognized GitHub repository", expected, pr_number=number)
    if pr_repo.lower() != origin_repo.lower():
        return _unknown("PR repository does not match the registered origin", expected,
                        observed={"repository": pr_repo}, pr_number=number)
    if not re.fullmatch(r"[0-9a-f]{40}", str(expected["head_sha"] or "")):
        return _unknown("item has no full expected head SHA", expected, pr_number=number)
    if not re.fullmatch(r"[0-9a-f]{40}", str(expected["base_sha"] or "")):
        return _unknown("item has no full reviewed base SHA", expected, pr_number=number)

    pr, failure = _api(Path(project["path"]), f"repos/{origin_repo}/pulls/{number}")
    if failure:
        return _unknown("GitHub PR observation unavailable", expected, pr_number=number, network=failure)
    head = pr.get("head") or {}; base = pr.get("base") or {}
    observed = {
        "repository": ((base.get("repo") or {}).get("full_name")), "head_repository": ((head.get("repo") or {}).get("full_name")),
        "head_ref": head.get("ref"), "head_sha": head.get("sha"), "base_ref": base.get("ref"), "base_sha": base.get("sha"),
        "state": pr.get("state"), "merged": bool(pr.get("merged")), "draft": bool(pr.get("draft")),
        "updated_at": pr.get("updated_at"),
    }
    common = {"provider": "github", "repository": origin_repo, "pr_number": number,
              "pr_url": item.get("pr_url"), "expected": expected, "observed": observed, "observed_at": now()}
    if str(observed["repository"] or "").lower() != origin_repo.lower() or str(observed["head_repository"] or "").lower() != origin_repo.lower():
        return {**common, "classification": "unknown", "reason": "PR repository binding changed",
                "evidence_complete": False}
    if observed["head_sha"] != expected["head_sha"]:
        return {**common, "classification": "changed-head", "reason": "PR head no longer matches the reviewed exact SHA",
                "evidence_complete": True}
    if observed["base_ref"] != expected["base_ref"] or observed["base_sha"] != expected["base_sha"]:
        return {**common, "classification": "moved-base", "reason": "PR base ref or exact reviewed base SHA moved",
                "evidence_complete": True}
    if observed["merged"]:
        return {**common, "classification": "merged", "reason": "GitHub reports the exact-bound PR merged", "evidence_complete": True}
    if str(observed["state"]).lower() == "closed":
        return {**common, "classification": "closed", "reason": "GitHub reports the PR closed without merge", "evidence_complete": True}
    if str(observed["state"]).lower() != "open":
        return {**common, "classification": "unknown", "reason": f"unrecognized PR state {observed['state']!r}",
                "evidence_complete": False}

    checks, check_failure = _api(Path(project["path"]), f"repos/{origin_repo}/commits/{expected['head_sha']}/check-runs?per_page=100")
    statuses, status_failure = _api(Path(project["path"]),
                                    f"repos/{origin_repo}/commits/{expected['head_sha']}/status?per_page=100")
    protection, protection_failure = _api(
        Path(project["path"]),
        f"repos/{origin_repo}/branches/{quote(str(expected['base_ref']), safe='')}/protection/required_status_checks",
    )
    if check_failure or status_failure or protection_failure:
        return {**common, "classification": "unknown", "reason": "exact-SHA check observation unavailable",
                "evidence_complete": False, "network": check_failure or status_failure or protection_failure}
    runs = checks.get("check_runs") or []
    contexts = statuses.get("statuses") or []
    if not isinstance(runs, list) or not isinstance(contexts, list):
        return {**common, "classification": "unknown", "reason": "GitHub check evidence has an invalid shape", "evidence_complete": False}
    check_count = checks.get("total_count")
    status_count = statuses.get("total_count")
    combined_sha = statuses.get("sha")
    combined_state = str(statuses.get("state") or "").lower()
    if (not isinstance(check_count, int) or check_count < 0 or check_count != len(runs)
            or not isinstance(status_count, int) or status_count < 0 or status_count != len(contexts)):
        return {**common, "classification": "unknown",
                "reason": "GitHub returned missing, inconsistent, or incomplete check/status counts",
                "evidence_complete": False}
    if combined_sha != expected["head_sha"] or combined_state not in {"pending", "success", "failure", "error"}:
        return {**common, "classification": "unknown",
                "reason": "combined status response is not bound to the requested exact SHA/state",
                "evidence_complete": False}
    protected_contexts = protection.get("contexts") if isinstance(protection, dict) else None
    protected_checks = protection.get("checks") if isinstance(protection, dict) else None
    if (not isinstance(protection, dict) or protection.get("strict") is not True
            or not isinstance(protected_contexts, list) or not isinstance(protected_checks, list)
            or any(not isinstance(value, str) or not value for value in protected_contexts)
            or any(not isinstance(value, dict) or not isinstance(value.get("context"), str)
                   or not value.get("context")
                   or value.get("app_id") is not None and (
                       not isinstance(value.get("app_id"), int) or isinstance(value.get("app_id"), bool)
                       or value.get("app_id") <= 0)
                   for value in protected_checks)):
        return {**common, "classification": "unknown",
                "reason": "GitHub did not prove a strict required-check policy for the exact base",
                "evidence_complete": False}
    check_bindings = sorted(
        [{"context": value, "app_id": None} for value in protected_contexts]
        + [{"context": value["context"], "app_id": value.get("app_id")} for value in protected_checks],
        key=lambda value: (value["context"], -1 if value["app_id"] is None else int(value["app_id"])),
    )
    # Deduplicate exact policy bindings while retaining app identity where the
    # protected-branch policy specifies it.
    check_bindings = [value for index, value in enumerate(check_bindings)
                      if index == 0 or value != check_bindings[index - 1]]
    if not check_bindings:
        return {**common, "classification": "unknown",
                "reason": "GitHub strict policy has no explicit required checks; absence is not merge authority",
                "evidence_complete": False}
    expected_checks = sorted({value["context"] for value in check_bindings})
    merge_policy = {"strict": True, "required_checks": expected_checks,
                    "required_check_bindings": check_bindings, "base_ref": expected["base_ref"]}
    evidence = {
        "check_runs": [{"name": r.get("name"), "status": r.get("status"), "conclusion": r.get("conclusion"),
                        "head_sha": r.get("head_sha"), "app_id": (r.get("app") or {}).get("id")} for r in runs],
        "statuses": [{"context": s.get("context"), "state": s.get("state"), "sha": s.get("sha")} for s in contexts],
        "merge_policy": merge_policy,
    }
    if (any(not isinstance(r, dict) or r.get("head_sha") != expected["head_sha"] for r in runs)
            or any(not isinstance(s, dict) or s.get("sha") != expected["head_sha"] for s in contexts)):
        return {**common, "classification": "unknown", "reason": "check evidence was not bound to the requested exact SHA",
                "evidence_complete": False, "checks": evidence}
    valid_run_statuses = PENDING_STATUSES | {"completed"}
    malformed_runs = []
    for run in runs:
        status = str(run.get("status") or "").lower()
        conclusion = str(run.get("conclusion") or "").lower()
        app_id = (run.get("app") or {}).get("id")
        if (status not in valid_run_statuses
                or (status == "completed" and conclusion not in PASSING_CONCLUSIONS | FAILING_CONCLUSIONS)
                or (status != "completed" and conclusion)
                or not isinstance(run.get("name"), str) or not run.get("name")
                or (app_id is not None and (not isinstance(app_id, int) or isinstance(app_id, bool) or app_id <= 0))):
            malformed_runs.append(run.get("name"))
    valid_context_states = {"pending", "success", "failure", "error"}
    malformed_contexts = [status.get("context") for status in contexts
                          if (not isinstance(status.get("context"), str) or not status.get("context")
                              or str(status.get("state") or "").lower() not in valid_context_states)]
    if malformed_runs or malformed_contexts:
        return {**common, "classification": "unknown",
                "reason": "GitHub returned malformed or internally inconsistent check states",
                "evidence_complete": False, "checks": evidence}
    conclusions = {str(r.get("conclusion") or "").lower() for r in runs}
    run_statuses = {str(r.get("status") or "").lower() for r in runs}
    context_states = {str(s.get("state") or "").lower() for s in contexts}
    observed_names = {str(r.get("name") or "") for r in runs} | {str(s.get("context") or "") for s in contexts}
    missing_expected = []
    for binding in check_bindings:
        context, app_id = binding["context"], binding["app_id"]
        present = (context in observed_names if app_id is None else
                   any(run.get("name") == context and (run.get("app") or {}).get("id") == app_id for run in runs))
        if not present:
            missing_expected.append(context if app_id is None else f"{context}@app:{app_id}")
    if (conclusions.intersection(FAILING_CONCLUSIONS) or context_states.intersection({"failure", "error"})
            or (contexts and combined_state in {"failure", "error"})):
        classification, reason = "checks-failed", "one or more exact-SHA GitHub checks failed"
    elif (missing_expected or observed["draft"] or run_statuses.intersection(PENDING_STATUSES) or "" in conclusions
          or "pending" in context_states or (contexts and combined_state == "pending")):
        classification, reason = ("checks-pending",
                                  "required exact-SHA checks have not all materialized: " + ", ".join(missing_expected)
                                  if missing_expected else "required exact-SHA GitHub checks are pending")
    elif not runs and not contexts:
        classification, reason = "unknown", "GitHub returned no check or status evidence; absence is not green"
    elif (all(c in PASSING_CONCLUSIONS for c in conclusions) and context_states <= {"success"}
          and (not contexts or combined_state == "success")):
        classification, reason = "checks-green", "all observed exact-SHA GitHub checks are green"
    else:
        classification, reason = "unknown", "GitHub check states were not safely classifiable"
    return {**common, "classification": classification, "reason": reason,
            "evidence_complete": classification != "unknown", "checks": evidence}


def _meaning(observation: dict) -> str:
    stable = {k: v for k, v in observation.items() if k != "observed_at"}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def monitor_item(work_id: str, *, force: bool = True) -> dict:
    from . import control, registry, supervisor, work
    item = work.load(work_id); project = registry.get(item["project"])
    previous = ((item.get("forge") or {}).get("latest") or {})
    if not force and previous.get("observed_at"):
        try:
            import datetime as dt
            last = dt.datetime.fromisoformat(previous["observed_at"].replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError): last = 0
        try: interval = max(1, int(os.environ.get("HELM_FORGE_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS)))
        except ValueError: interval = DEFAULT_INTERVAL_SECONDS
        if time.time() - last < interval:
            return previous
    observation = inspect(item, project); meaning = _meaning(observation)
    def record(current):
        forge = current.setdefault("forge", {"history": []})
        prior = forge.get("latest") or {}
        if prior.get("fingerprint") != meaning:
            forge.setdefault("history", []).append({**observation, "fingerprint": meaning})
            forge["history"] = forge["history"][-100:]
        forge["latest"] = {**observation, "fingerprint": meaning}
    updated = control.cas_update(work_id, record, expected_revision=int(item.get("revision", 0)))
    supervisor.observe(updated)
    return (updated.get("forge") or {}).get("latest") or observation


def monitor_all() -> list[dict]:
    from . import work
    results = []
    with locked(home() / "forge.lock"):
        for item in work.all_items():
            if item.get("status") == "pr-open" and item.get("pr_url"):
                results.append({"item_id": item["id"], **monitor_item(item["id"], force=False)})
    return results


def require_green(work_id: str) -> dict:
    observation = monitor_item(work_id, force=True)
    policy = ((observation.get("checks") or {}).get("merge_policy") or {})
    if (observation.get("classification") != "checks-green" or not observation.get("evidence_complete")
            or policy.get("strict") is not True or not policy.get("required_checks")):
        raise HelmError(f"GitHub merge refused: {observation.get('classification')} — {observation.get('reason')}")
    return observation
