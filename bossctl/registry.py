"""Project registry: ~/.boss/projects.json. Mode + authority are the boss's standing posture."""
from __future__ import annotations
import re
from pathlib import Path
from .paths import projects_file, authority_lock
from .util import read_json, write_json, BossError, locked
from . import modes, gates, ids

MODES = modes.MODES
ACCEPTED_MODES = modes.ACCEPTED_MODES
# Authority: what bossctl may do on its own for this project. Only a human raises it.
AUTHORITY = {
    0: "observe      — scout tasks only",
    1: "build        — ship tasks build+verify in a worktree; nothing leaves the machine",
    2: "open-pr      — may push a branch and open a PR",
    3: "merge        — `bossctl promote --confirm` may merge/fast-forward",
}


def available(project: dict) -> bool:
    """A registered repository is usable only while its path is still a Git checkout."""
    path = Path(str(project.get("path") or ""))
    return bool(project.get("path")) and path.is_dir() and (path / ".git").exists()


def load(*, check_paths: bool = False) -> dict:
    """The registry. `check_paths` stats each project to derive `available`.

    The stat is opt-in because `load` runs inside the daemon poll, the 2s board
    redraw, and per-candidate queue claiming: a project on an unresponsive network
    mount must not be able to block those. Callers that render or gate on
    availability ask for it; `require_available` derives it for one project instead.
    """
    try:
        data = read_json(projects_file(), {"projects": {}})
    except (OSError, ValueError) as exc:
        raise BossError(f"project registry is unreadable: {projects_file()} ({exc}); "
                        "run `pi-boss doctor --repair --confirm`") from None
    if not isinstance(data, dict) or not isinstance(data.get("projects", {}), dict):
        raise BossError(f"project registry is malformed: {projects_file()}; run `pi-boss doctor --repair --confirm`")
    data.setdefault("projects", {})
    if not all(isinstance(value, dict) for value in data["projects"].values()):
        raise BossError(f"project registry has a malformed entry: {projects_file()}; run `pi-boss doctor --repair --confirm`")
    for project in data["projects"].values():
        project["mode"] = modes.normalize(project.get("mode"))
        project.setdefault("gate", "native")
        if check_paths:
            project["available"] = available(project)
    return data


def save(data: dict) -> None:
    # `available` is derived from the filesystem on every load; never persist it.
    write_json(projects_file(), {**data, "projects": {
        key: {k: v for k, v in value.items() if k != "available"} for key, value in data["projects"].items()}})


def get(project_id: str) -> dict:
    project_id = ids.project(project_id)
    p = load()["projects"].get(project_id)
    if not p:
        raise BossError(f"unknown project '{project_id}' (see `bossctl projects`)")
    if p.get("id") != project_id:
        raise BossError("project identity does not match its registry key")
    return p


def require_available(project: dict) -> dict:
    """Refuse new work for a project whose checkout is gone; the registration is kept."""
    if not project.get("available", available(project)):
        raise BossError(f"project '{project['id']}' is registered at {project.get('path')} but that path is "
                        "no longer a Git checkout; restore it or re-register with `bossctl add`")
    return project


def add(path: str, project_id: str | None, mode: str, authority: int, test_cmd: str | None,
        protected: list[str], base: str | None, gate: str = "native") -> dict:
    repo = Path(path).expanduser().resolve()
    if not (repo / ".git").exists():
        raise BossError(f"{repo} is not a git repository")
    if mode not in ACCEPTED_MODES:
        raise BossError(f"mode must be one of {MODES}")
    mode = modes.normalize(mode)
    if authority not in AUTHORITY:
        raise BossError(f"authority must be 0-3")
    if gate not in gates.PROVIDERS:
        raise BossError(f"gate must be one of {gates.PROVIDERS}")
    pid = ids.project(project_id or re.sub(r"[^a-z0-9-]+", "-", repo.name.lower()).strip("-"))
    from . import detect
    if not base:
        base = detect.base_branch(repo)
    if test_cmd is None:
        test_cmd = detect.test_command(repo)
    entry = {
        "id": pid, "path": str(repo), "mode": mode, "authority": authority,
        "base": base, "test_cmd": test_cmd or "",
        "protected_paths": protected or [".github/workflows/*", ".boss/*", "bossctl.json"],
        "gate": gate,
    }
    with locked(authority_lock()):
        data = load()
        if pid in data["projects"]:
            raise BossError(f"project id '{pid}' is already registered; use `bossctl set` to update it")
        duplicate = next((key for key, value in data["projects"].items()
                          if Path(value.get("path", "")).expanduser().resolve() == repo), None)
        if duplicate:
            raise BossError(f"repository is already registered as project '{duplicate}'")
        data["projects"][pid] = entry
        save(data)
    return entry


def set_fields(project_id: str, **fields) -> dict:
    project_id = ids.project(project_id)
    with locked(authority_lock()):
        data = load()
        p = data["projects"].get(project_id) or {}
        if not p:
            raise BossError(f"unknown project '{project_id}'")
        for k, v in fields.items():
            if v is None:
                continue
            if k == "mode":
                if v not in ACCEPTED_MODES:
                    raise BossError(f"mode must be one of {MODES}")
                v = modes.normalize(v)
            if k == "authority" and v not in AUTHORITY:
                raise BossError("authority must be 0-3")
            if k == "gate" and v not in gates.PROVIDERS:
                raise BossError(f"gate must be one of {gates.PROVIDERS}")
            p[k] = v
        save(data)
        return p
