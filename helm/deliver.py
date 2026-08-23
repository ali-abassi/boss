"""Delivery after a green graph, and captain-confirmed promotion. Authority is enforced here."""
from __future__ import annotations
from pathlib import Path
from . import worktree
from .util import git, sh, HelmError, log


def after_success(it: dict, project: dict, wt: Path) -> None:
    from .work import transition
    head = git(wt, "rev-parse", "HEAD")
    it["head_sha"] = head
    # Herdr-backed reviewed modes require genuine fresh reviewer records bound to
    # exactly the post-integration commit. Test/headless adapters never fabricate them.
    if it.get("session") and it.get("dispatch", {}).get("graph") in ("direct-pr", "no-mistakes"):
        required = 2 if it["dispatch"]["graph"] == "no-mistakes" else 1
        reviews = it.get("reviews") or []
        if len(reviews) < required or any(r.get("verdict") != "accept" or r.get("sha") != head or not r.get("reviewer") for r in reviews):
            raise HelmError("independent approval is missing or not bound to the exact delivery SHA")
    mode = project["mode"]
    has_origin = bool(git(project["path"], "remote", "get-url", "origin", check=False))
    if mode == "local-only" or project["authority"] < 2 or not has_origin:
        note = f"branch {it['branch']} ready in {wt}"
        if mode != "local-only" and project["authority"] >= 2 and not has_origin:
            note += " (no origin remote: PR not possible)"
        elif mode != "local-only" and project["authority"] < 2:
            note += " (authority < 2: PR not opened)"
        transition(it, "ready", note)
        return
    url = open_pr(it, project, wt)
    it["pr_url"] = url
    transition(it, "pr-open", url)


def open_pr(it: dict, project: dict, wt: Path) -> str:
    git(wt, "push", "--force-with-lease", "-u", "origin", it["branch"])
    title = it["text"].strip().splitlines()[0][:70]
    body = f"helm work item `{it['id']}`\n\n{it['text']}\n\nGraph: {it['dispatch']['graph']} · rule: {it['dispatch']['rule']}\n"
    r = sh(["gh", "pr", "create", "--head", it["branch"], "--base", project["base"],
            "--title", title, "--body", body], cwd=wt, check=False)
    if r.returncode != 0:
        raise HelmError(f"gh pr create failed: {r.stderr.strip()}")
    return r.stdout.strip().splitlines()[-1]


def promote(it: dict, project: dict, confirm: bool) -> str:
    """Captain's explicit word, every time. Never runs from the daemon."""
    from .work import transition
    if not confirm:
        raise HelmError("promotion needs --confirm (the captain's explicit word)")
    if project["authority"] < 3:
        raise HelmError(f"project '{project['id']}' authority {project['authority']} < 3: raise it with "
                        f"`helm set {project['id']} --authority 3` if you really want helm merging")
    if it["status"] not in ("ready", "pr-open"):
        raise HelmError(f"{it['id']} is {it['status']}; only ready/pr-open items promote")
    repo = Path(project["path"])
    wt = worktree.worktree_root() / project["id"] / it["id"]
    if wt.exists():
        current = git(wt, "rev-parse", "HEAD")
        if it.get("head_sha") and current != it["head_sha"]:
            raise HelmError("branch mutated after verification; approval is invalid")
        if it.get("reviews") and any(r.get("sha") != current for r in it["reviews"]):
            raise HelmError("review approval is not bound to the current commit SHA")
        base_now = git(repo, "rev-parse", project["base"])
        reviewed_base = ((it.get("verification") or [{}])[-1]).get("base_sha")
        if reviewed_base and reviewed_base != base_now:
            raise HelmError("configured base moved after verification; recover the item to integrate and review again")
    if it["status"] == "pr-open" and it.get("pr_url"):
        r = sh(["gh", "pr", "merge", it["pr_url"], "--merge", "--delete-branch"], cwd=repo, check=False)
        if r.returncode != 0:
            raise HelmError(f"gh pr merge failed: {r.stderr.strip()}")
        worktree.remove(project, it["id"], delete_branch=True)
        transition(it, "merged", it["pr_url"])
        if it.get("session"):
            from . import herdr
            herdr.close_agent_tab(it["session"])
        return it["pr_url"]
    # local fast-forward into the captain's checkout: only when it is clean and on base.
    if git(repo, "status", "--porcelain", "--untracked-files=no"):
        raise HelmError(f"{repo} has uncommitted changes; commit or stash them first")
    current = git(repo, "symbolic-ref", "--short", "HEAD", check=False)
    if current != project["base"]:
        raise HelmError(f"{repo} is on '{current}', not base '{project['base']}'")
    from .util import GIT_OPTS
    r = sh(["git", *GIT_OPTS, "-C", str(repo), "merge", "--ff-only", it["branch"]], check=False)
    if r.returncode != 0:
        raise HelmError(f"fast-forward refused (base moved?): {r.stderr.strip()} — re-queue with `helm respond`")
    head = git(repo, "rev-parse", "--short", "HEAD")
    worktree.remove(project, it["id"], delete_branch=True)
    transition(it, "merged", f"fast-forwarded {project['base']} to {head}")
    if it.get("session"):
        from . import herdr
        herdr.close_agent_tab(it["session"])
    log(f"{it['id']}: merged into {project['base']} @ {head}")
    return head
