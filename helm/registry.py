"""Project registry: ~/.helm/projects.json. Mode + authority are the captain's standing posture."""
from __future__ import annotations
import re
from pathlib import Path
from .paths import projects_file, authority_lock
from .util import read_json, write_json, HelmError, locked
from . import modes, gates, ids

MODES = modes.MODES
ACCEPTED_MODES = modes.ACCEPTED_MODES
# Authority: what helm may do on its own for this project. Only a human raises it.
AUTHORITY = {
    0: "observe      — scout tasks only",
    1: "build        — ship tasks build+verify in a worktree; nothing leaves the machine",
    2: "open-pr      — may push a branch and open a PR",
    3: "merge        — `helm promote --confirm` may merge/fast-forward",
}
MIN_AUTHORITY = {"scout": 0, "build": 1, "open-pr": 2, "merge": 3}


def load() -> dict:
    data = read_json(projects_file(), {"projects": {}})
    for project in data.get("projects", {}).values():
        project["mode"] = modes.normalize(project.get("mode"))
        project.setdefault("gate", "native")
    return data


def save(data: dict) -> None:
    write_json(projects_file(), data)


def get(project_id: str) -> dict:
    project_id = ids.project(project_id)
    p = load()["projects"].get(project_id)
    if not p:
        raise HelmError(f"unknown project '{project_id}' (see `helm projects`)")
    if p.get("id") != project_id:
        raise HelmError("project identity does not match its registry key")
    return p


def add(path: str, project_id: str | None, mode: str, authority: int, test_cmd: str | None,
        protected: list[str], base: str | None, gate: str = "native") -> dict:
    repo = Path(path).expanduser().resolve()
    if not (repo / ".git").exists():
        raise HelmError(f"{repo} is not a git repository")
    if mode not in ACCEPTED_MODES:
        raise HelmError(f"mode must be one of {MODES}")
    mode = modes.normalize(mode)
    if authority not in AUTHORITY:
        raise HelmError(f"authority must be 0-3")
    if gate not in gates.PROVIDERS:
        raise HelmError(f"gate must be one of {gates.PROVIDERS}")
    pid = ids.project(project_id or re.sub(r"[^a-z0-9-]+", "-", repo.name.lower()).strip("-"))
    from . import detect
    if not base:
        base = detect.base_branch(repo)
    if test_cmd is None:
        test_cmd = detect.test_command(repo)
    entry = {
        "id": pid, "path": str(repo), "mode": mode, "authority": authority,
        "base": base, "test_cmd": test_cmd or "",
        "protected_paths": protected or [".github/workflows/*", ".helm/*", "helm.json"],
        "gate": gate,
    }
    with locked(authority_lock()):
        data = load()
        if pid in data["projects"]:
            raise HelmError(f"project id '{pid}' is already registered; use `helm set` to update it")
        duplicate = next((key for key, value in data["projects"].items()
                          if Path(value.get("path", "")).expanduser().resolve() == repo), None)
        if duplicate:
            raise HelmError(f"repository is already registered as project '{duplicate}'")
        data["projects"][pid] = entry
        save(data)
    return entry


def set_fields(project_id: str, **fields) -> dict:
    project_id = ids.project(project_id)
    with locked(authority_lock()):
        data = load()
        p = data["projects"].get(project_id) or {}
        if not p:
            raise HelmError(f"unknown project '{project_id}'")
        for k, v in fields.items():
            if v is None:
                continue
            if k == "mode":
                if v not in ACCEPTED_MODES:
                    raise HelmError(f"mode must be one of {MODES}")
                v = modes.normalize(v)
            if k == "authority" and v not in AUTHORITY:
                raise HelmError("authority must be 0-3")
            if k == "gate" and v not in gates.PROVIDERS:
                raise HelmError(f"gate must be one of {gates.PROVIDERS}")
            p[k] = v
        save(data)
        return p
