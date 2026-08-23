"""Read-only control-plane audit and explicitly confirmed safe reconciliation."""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import stat as statmod
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote, urlparse
from . import control, dispatch, gates, modes, scope, processes, ids, registry, worktree, work
from .paths import (GRAPHS, dispatch_file, home, projects_file, supervisor_file,
                    wakes_file, work_root, worktree_root, supervisor_lock, authority_lock)
from .util import HelmError, git_config, locked, now, read_json, sh, write_json

VERSION = 1
STATUSES = ("ok", "warning", "error", "unknown")
MAX_RECORDS = 10_000


def _run(args: list[str], *, cwd: Path | str | None = None, timeout: int = 10,
         env: dict[str, str] | None = None, network: bool = False,
         write_paths: tuple[Path | str, ...] = (),
         read_paths: tuple[Path | str, ...] = ()) -> subprocess.CompletedProcess:
    try:
        return sh(args, cwd=cwd, check=False, timeout=timeout,
                  env={**os.environ, **(env or {}), "GIT_OPTIONAL_LOCKS": "0"},
                  network=network, write_paths=write_paths, read_paths=read_paths)
    except (OSError, subprocess.TimeoutExpired, HelmError) as exc:
        detail = exc.msg if isinstance(exc, HelmError) else f"{type(exc).__name__}: {exc}"
        return subprocess.CompletedProcess(args, 124, "", detail)


def _json(path: Path, default):
    try:
        return json.loads(path.read_text()), None
    except FileNotFoundError:
        return default, None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, type(exc).__name__


def _owner_state(record: dict | None, pid=None) -> str:
    return processes.liveness(record, pid).get("state", "unknown")


def _git(repo: Path, *args: str, timeout: int = 10, network: bool = False,
         write_paths: tuple[Path | str, ...] = (),
         read_paths: tuple[Path | str, ...] = ()) -> subprocess.CompletedProcess:
    return _run(["git", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
                "-C", str(repo), *args], timeout=timeout, network=network,
                write_paths=write_paths, read_paths=read_paths)


def _local_remote_read_paths(origin: str) -> tuple[Path, ...]:
    """Return a read-only capability only for an explicit absolute file remote."""
    parsed = urlparse(origin)
    if parsed.scheme == "file" and parsed.hostname in (None, "", "localhost"):
        candidate = Path(unquote(parsed.path))
    elif not parsed.scheme and Path(origin).is_absolute():
        candidate = Path(origin)
    else:
        return ()
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return ()
    return (resolved,) if resolved.is_dir() else ()


def _herdr_agent(name: str, identity: dict | None = None) -> dict:
    if not name:
        return {"state": "unknown", "reason": "Herdr agent identity is missing"}
    binary = shutil.which("herdr")
    if not binary:
        return {"state": "unknown", "reason": "Herdr is not reachable from doctor"}
    session = os.environ.get("HERDR_SESSION") or "firstmate"
    args = [binary, *(["--session", session] if session else []), "agent", "get", name]
    result = _run(args, timeout=10)
    if result.returncode:
        try:
            failure = json.loads(result.stderr or result.stdout)
            code = ((failure.get("error") or {}).get("code") if isinstance(failure, dict) else None)
        except (json.JSONDecodeError, AttributeError):
            code = None
        if code == "agent_not_found":
            return {"state": "dead", "reason": "Herdr positively reports the agent absent"}
        return {"state": "unknown", "reason": "Herdr agent probe failed"}
    try: agent = (json.loads(result.stdout).get("result") or {}).get("agent")
    except (json.JSONDecodeError, AttributeError): agent = None
    if not isinstance(agent, dict): return {"state": "unknown", "reason": "Herdr omitted agent evidence"}
    status = agent.get("agent_status") or agent.get("status") or agent.get("state")
    if status in {"idle", "done", "working", "blocked"} and agent.get("pane_id"):
        evidence = {"state": "live", "status": status, "agent": agent}
        if identity is not None:
            process = _run([binary, *(['--session', session] if session else []),
                            "pane", "process-info", "--pane", str(agent["pane_id"])], timeout=10)
            try:
                process_info = (((json.loads(process.stdout).get("result") or {}).get("process_info"))
                                if process.returncode == 0 else None)
            except (json.JSONDecodeError, AttributeError):
                process_info = None
            from . import herdr
            exact = herdr.exact_agent_liveness(identity, evidence,
                                               process_info=process_info if isinstance(process_info, dict) else {})
            return {**exact, "pane_id": agent.get("pane_id"),
                    "workspace_id": agent.get("workspace_id")}
        return {**evidence, "pane_id": agent.get("pane_id"),
                "workspace_id": agent.get("workspace_id")}
    if status in {"dead", "exited", "stopped", "failed", "error", "closed", "terminated"}:
        return {"state": "dead", "status": status}
    return {"state": "unknown", "status": status, "reason": "unrecognized Herdr state"}


def _worktree_recovery_quiescence(item: dict) -> tuple[bool, dict]:
    """Require positive absence/death for every execution identity before moving work."""
    status = item.get("status")
    if status not in {"paused", "needs-you", "ready", "pr-open", "failed"}:
        return False, {"reason": f"item status {status!r} is not quiescent"}
    if item.get("lease"):
        return False, {"reason": "item still has an execution lease"}
    session = item.get("session") or {}
    session_state = (_herdr_agent(session.get("agent_name"), session).get("state")
                     if session.get("agent_name") else "none")
    if session_state not in {"none", "dead"}:
        return False, {"reason": f"implementer session liveness is {session_state}"}
    launch_states = []
    for launch in item.get("agent_launches") or []:
        if launch.get("state") not in {"reserved", "tab-created", "attested"}:
            continue
        state = (_herdr_agent(launch.get("agent_name"), launch).get("state")
                 if launch.get("agent_name") else "unknown")
        launch_states.append({"launch_id": launch.get("launch_id"), "state": state})
    if any(value["state"] != "dead" for value in launch_states):
        return False, {"reason": "one or more active launch identities are live or unknown",
                       "launches": launch_states}
    return True, {"session_state": session_state, "launches": launch_states}


def _live_tabs() -> tuple[dict[str, dict] | None, str | None]:
    binary = shutil.which("herdr")
    if not binary:
        return None, "Herdr is not reachable from doctor"
    session = os.environ.get("HERDR_SESSION") or "firstmate"
    workspace = os.environ.get("HERDR_WORKSPACE_ID")
    workspaces = [workspace] if workspace else []
    if not workspaces:
        listed = _run([binary, "--session", session, "workspace", "list"])
        if listed.returncode: return None, "Herdr session/workspace probe failed"
        try:
            workspaces = [str(value.get("workspace_id")) for value in
                          ((json.loads(listed.stdout).get("result") or {}).get("workspaces") or [])
                          if value.get("workspace_id")]
        except (json.JSONDecodeError, AttributeError):
            return None, "Herdr returned invalid workspace evidence"
    live: dict[str, dict] = {}
    for workspace_id in workspaces:
        result = _run([binary, "--session", session, "tab", "list", "--workspace", workspace_id])
        if result.returncode: return None, "Herdr tab probe failed"
        try: tabs = (json.loads(result.stdout).get("result") or {}).get("tabs") or []
        except (json.JSONDecodeError, AttributeError): return None, "Herdr returned invalid tab evidence"
        for tab in tabs:
            if tab.get("tab_id"):
                live[str(tab["tab_id"])] = {**tab, "workspace_id": workspace_id, "herdr_session": session}
    return live, None


def _digest(path: Path) -> str | None:
    try: return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError: return None


def _owned_json_nofollow(path: Path, *, max_bytes: int = 1_000_000) -> dict:
    """Read one captain-selected JSON file without a symlink/swap window."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        try:
            stat = os.fstat(fd)
            if stat.st_uid != os.getuid() or not 0 < stat.st_size <= max_bytes:
                raise HelmError("authentication source ownership or size is unsafe")
            chunks, remaining = [], max_bytes + 1
            while remaining:
                chunk = os.read(fd, min(65_536, remaining))
                if not chunk: break
                chunks.append(chunk); remaining -= len(chunk)
        finally:
            os.close(fd)
        value = json.loads(b"".join(chunks))
    except HelmError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HelmError(f"authentication source is unreadable: {type(exc).__name__}")
    if not isinstance(value, dict):
        raise HelmError("authentication source must be a JSON object")
    return value


@contextmanager
def _isolated_pi_probe(*, include_auth: bool = False):
    """Give observational Pi commands a disposable copy, never the live home."""
    with tempfile.TemporaryDirectory(prefix="firstmate-doctor-pi-") as raw:
        root = Path(raw); pi_dir = root / "pi"; tmp_dir = root / "tmp"
        pi_dir.mkdir(mode=0o700); tmp_dir.mkdir(mode=0o700)
        names = ["settings.json", "models.json", "models-store.json"]
        if include_auth: names.append("auth.json")
        source = home() / "pi"
        for name in names:
            candidate = source / name
            try: stat = candidate.stat()
            except OSError: continue
            if (candidate.is_symlink() or not candidate.is_file() or stat.st_uid != os.getuid()
                    or stat.st_size > 5_000_000):
                continue
            destination = pi_dir / name
            shutil.copyfile(candidate, destination); destination.chmod(0o600)
        yield {**os.environ, "PI_CODING_AGENT_DIR": str(pi_dir), "HOME": str(root),
               "XDG_CONFIG_HOME": str(root / "config"), "XDG_CACHE_HOME": str(root / "cache"),
               "TMPDIR": str(tmp_dir)}


def _offline_model_inventory() -> tuple[set[str], str | None]:
    raw = os.environ.get("HELM_AVAILABLE_MODELS")
    if raw is not None:
        return {value.strip() for value in raw.split(",") if value.strip()}, None
    if not shutil.which("pi"):
        return set(), "pi unavailable"
    # Subscription providers expose their model inventory only when their
    # provider record is present, even in --offline mode.  Copy that record
    # into the disposable owner-only home; never point the probe at the live
    # Pi directory and never make a model call.
    with _isolated_pi_probe(include_auth=True) as env:
        listing = _run(["pi", "--offline", "--list-models"], timeout=30, env=env)
    available = {f"{parts[0]}/{parts[1]}" for line in listing.stdout.splitlines()
                 if len(parts := line.split()) >= 2}
    return (available, None) if listing.returncode == 0 else (set(), "pi --list-models failed")


def _live_model_probe(model: str) -> tuple[bool, str]:
    from . import graphs
    with _isolated_pi_probe(include_auth=True) as env:
        return graphs.probe_model(model, env=env)


def _project_data() -> tuple[dict, str | None]:
    data, error = _json(projects_file(), {"projects": {}})
    if error or not isinstance(data, dict) or not isinstance(data.get("projects"), dict):
        return {"projects": {}}, error or "invalid schema"
    for key, project in data["projects"].items():
        if (not isinstance(key, str) or not ids.PROJECT_PATTERN.fullmatch(key)
                or not isinstance(project, dict) or project.get("id") != key
                or not isinstance(project.get("path"), str) or not project.get("path")
                or modes.normalize(project.get("mode")) not in modes.MODES
                or not isinstance(project.get("authority"), int) or isinstance(project.get("authority"), bool)
                or project.get("authority") not in range(4)
                or not isinstance(project.get("base"), str) or not project.get("base")
                or not isinstance(project.get("test_cmd"), str)
                or not isinstance(project.get("protected_paths"), list)
                or not all(isinstance(value, str) for value in project.get("protected_paths", []))
                or project.get("gate", "native") not in gates.PROVIDERS):
            return {"projects": {}}, f"invalid project entry {key!r}"
        project["mode"] = modes.normalize(project.get("mode")); project.setdefault("gate", "native")
    return data, None


def _dispatch_data() -> tuple[dict, str | None]:
    data, error = _json(dispatch_file(), dispatch.DEFAULT)
    if error or not isinstance(data, dict): return dispatch.DEFAULT, error or "invalid schema"
    return {**data, "models": {**dispatch.DEFAULT["models"], **(data.get("models") or {})},
            "thinking": {**dispatch.DEFAULT["thinking"], **(data.get("thinking") or {})}}, None


def _claims_schema(value: object) -> str | None:
    if not isinstance(value, dict) or value.get("version") != 1:
        return "invalid claims version/schema"
    claims = value.get("claims")
    if not isinstance(claims, list) or len(claims) > MAX_RECORDS:
        return "claims must be a bounded list"
    seen = set()
    for claim in claims:
        if (not isinstance(claim, dict) or not isinstance(claim.get("work_id"), str)
                or not isinstance(claim.get("project"), str)
                or not isinstance(claim.get("paths"), list)
                or not all(isinstance(path, str) for path in claim.get("paths", []))):
            return "claim entry is malformed"
        if claim["work_id"] in seen:
            return "claim work identities must be unique"
        seen.add(claim["work_id"])
        if claim.get("process_identity") is not None and not isinstance(claim.get("process_identity"), dict):
            return "claim process identity is malformed"
        if (claim.get("claim_token") is not None
                and not isinstance(claim.get("claim_token"), str)):
            return "claim ownership token is malformed"
    return None


def _tabs_schema(value: object) -> str | None:
    if not isinstance(value, dict) or not isinstance(value.get("tabs"), list) or len(value["tabs"]) > MAX_RECORDS:
        return "tabs must be a bounded object/list"
    seen = set()
    for tab in value["tabs"]:
        if (not isinstance(tab, dict) or not isinstance(tab.get("tab_id"), str)
                or tab.get("kind") is not None and not isinstance(tab.get("kind"), str)
                or tab.get("label") is not None and not isinstance(tab.get("label"), str)
                or tab.get("workspace_id") is not None and not isinstance(tab.get("workspace_id"), str)
                or tab.get("herdr_session") is not None and not isinstance(tab.get("herdr_session"), str)):
            return "tab entry is malformed"
        identity = (tab.get("herdr_session"), tab.get("workspace_id"), tab["tab_id"])
        if identity in seen: return "tab identities must be unique"
        seen.add(identity)
    return None


def _item_schema(value: object, directory_name: str) -> str | None:
    if not isinstance(value, dict) or value.get("id") != directory_name or not ids.WORK_PATTERN.fullmatch(directory_name):
        return "item identity does not match its safe state directory"
    statuses = {"queued", "running", "paused", "needs-you", "ready", "pr-open", "done",
                "failed", "merged", "cancelled", "cancelling"}
    if (value.get("status") not in statuses or not isinstance(value.get("project"), str)
            or not ids.PROJECT_PATTERN.fullmatch(value["project"])
            or not isinstance(value.get("revision", 0), int) or isinstance(value.get("revision", 0), bool)
            or not isinstance(value.get("branch", f"firstmate/{directory_name}"), str)
            or not isinstance(value.get("worktree", ""), str)):
        return "item core fields are malformed"
    bounded_lists = ("history", "runs", "reviews", "verification", "agent_launches", "failure_notes")
    for key in bounded_lists:
        records = value.get(key, [])
        if not isinstance(records, list) or len(records) > MAX_RECORDS or any(not isinstance(record, dict) for record in records):
            return f"item {key} must be a bounded object list"
    controls = value.get("controls", {})
    if not isinstance(controls, dict): return "item controls must be an object"
    for key in ("events", "pending"):
        records = controls.get(key, [])
        if not isinstance(records, list) or len(records) > MAX_RECORDS or any(not isinstance(record, dict) for record in records):
            return f"item controls.{key} must be a bounded object list"
    event_ids = [event.get("id") for event in controls.get("events", [])]
    if any(not isinstance(event_id, str) for event_id in event_ids) or len(event_ids) != len(set(event_ids)):
        return "item control event identities are missing or duplicated"
    for key in ("lease", "session", "checkpoint", "budgets", "usage", "node_budgets", "node_usage",
                "promotion", "pr_delivery", "external_gate", "cancellation"):
        if value.get(key) is not None and not isinstance(value.get(key), dict):
            return f"item {key} must be an object or null"
    if error := work.budget_state_error(value):
        return error
    lease = value.get("lease") or {}
    if lease and (not isinstance(lease.get("pid"), int) or isinstance(lease.get("pid"), bool)
                  or lease.get("process_identity") is not None and not isinstance(lease.get("process_identity"), dict)
                  or lease.get("claim_token") is not None and not isinstance(lease.get("claim_token"), str)):
        return "item lease ownership is malformed"
    launches = value.get("agent_launches") or []
    launch_ids = []
    for launch in launches:
        launch_id = launch.get("launch_id")
        if (not isinstance(launch_id, str) or not isinstance(launch.get("state"), str)
                or launch.get("role") not in {"implementer", "reviewer"}
                or launch.get("agent_name") is not None and not isinstance(launch.get("agent_name"), str)
                or launch.get("attested_process_identity") is not None
                and not isinstance(launch.get("attested_process_identity"), dict)):
            return "item launch journal is malformed"
        launch_ids.append(launch_id)
    if len(launch_ids) != len(set(launch_ids)): return "item launch identities are duplicated"
    return None


def audit(*, network: bool = True, probe_models: bool = False) -> dict:
    """Inspect without creating, deleting, normalizing, locking, or rewriting state."""
    checks, repairs = [], []
    def add(check_id: str, status: str, summary: str, **details):
        if status not in STATUSES: raise ValueError(status)
        checks.append({"id": check_id, "status": status, "summary": summary, **details})
    def repair(action: str, target, reason: str, **evidence):
        repairs.append({"action": action, "target": target, "reason": reason, **evidence})

    for binary in ("git", "pi", "gh", "herdr"):
        found = shutil.which(binary)
        add(f"binary:{binary}", "ok" if found else ("warning" if binary in ("gh",) else "error"),
            found or "not on PATH")
    from . import sandbox
    sandbox_status = sandbox.available()
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey  # noqa: F401
        signing_ready = True
    except ImportError:
        signing_ready = False
    add("boundary:macos-sandbox", "ok" if sandbox_status.get("ready") else "error",
        sandbox_status.get("reason", "sandbox unavailable"), evidence=sandbox_status)
    add("boundary:signed-ledger", "ok" if signing_ready else "error",
        "Ed25519 runtime ledger verification available" if signing_ready else
        "Python cryptography is unavailable; managed runtime evidence fails closed")
    from . import graphs
    runner = _run([graphs.piw_bin(), "schema", "--json"], timeout=20)
    add("runner:pi-graph", "ok" if runner.returncode == 0 else "error",
        graphs.piw_bin() if runner.returncode == 0 else "bundled runner schema probe failed")
    external_gate = gates.no_mistakes_status()
    add("gate:no-mistakes", "ok" if external_gate.get("ready") else "warning",
        external_gate.get("reason", "unverified optional adapter"),
        evidence={k: v for k, v in external_gate.items() if k not in ("reason", "binary")})

    state_home = home()
    try:
        state_stat = state_home.stat()
    except FileNotFoundError:
        add("state:home-permissions", "ok", "state home is not initialized yet; new homes are private")
    except OSError as exc:
        add("state:home-permissions", "error", f"state home cannot be inspected: {type(exc).__name__}")
    else:
        private = state_home.is_dir() and state_stat.st_uid == os.getuid() and not (state_stat.st_mode & 0o077)
        if private:
            add("state:home-permissions", "ok", "state home is owner-only")
        elif not state_home.is_dir() or state_stat.st_uid != os.getuid():
            add("state:home-permissions", "error", "state home is not an owned directory; no repair was inferred")
        else:
            add("state:home-permissions", "error", "state home permits group/other access; controller records remain preserved")
            repair("tighten-state-home-permissions", str(state_home),
                   "set the owned controller state directory to mode 0700",
                   inode=state_stat.st_ino, device=state_stat.st_dev,
                   prior_mode=state_stat.st_mode & 0o777)

    # Inspect controller-owned records without traversing project worktrees.
    # Existing modes are evidence only until a confirmed repair revalidates the
    # exact inode; normal controller operation never chmods them implicitly.
    permission_targets: list[Path] = []
    if state_home.is_dir():
        try:
            for candidate in state_home.iterdir():
                if (candidate.name.endswith((".json", ".lock", ".log", ".pid"))
                        or candidate.name in {"projects.json", "dispatch.json"}):
                    permission_targets.append(candidate)
            for private_root in (state_home / "work", state_home / "pi", state_home / "recovery"):
                if not private_root.exists():
                    continue
                for raw_root, directories, filenames in os.walk(private_root, followlinks=False):
                    root_path = Path(raw_root); permission_targets.append(root_path)
                    # Record symlinked directories as unsafe, but never enter them.
                    for name in list(directories):
                        candidate = root_path / name
                        if candidate.is_symlink():
                            permission_targets.append(candidate); directories.remove(name)
                        # Historical graph runs may contain ordinary Git object
                        # stores.  They are preserved project evidence, not
                        # controller records: chmodding them would be an
                        # over-broad repair and can strip executable hook bits.
                        elif name == ".git":
                            directories.remove(name)
                    filenames = [name for name in filenames if name != ".git"]
                    permission_targets.extend(root_path / name for name in filenames)
        except OSError as exc:
            add("state:record-permissions", "error",
                f"controller record permissions could not be enumerated: {type(exc).__name__}")
            permission_targets = []
    permission_unsafe, permission_loose = [], []
    for path in sorted(set(permission_targets)):
        try:
            info = path.lstat()
        except OSError:
            permission_unsafe.append(str(path)); continue
        kind = ("directory" if statmod.S_ISDIR(info.st_mode) else
                "file" if statmod.S_ISREG(info.st_mode) else None)
        if path.is_symlink() or kind is None or info.st_uid != os.getuid():
            permission_unsafe.append(str(path)); continue
        required = 0o700 if kind == "directory" else 0o600
        actual = info.st_mode & 0o777
        if actual != required:
            permission_loose.append(str(path))
            repair("tighten-state-record-permissions", str(path),
                   f"set the owned controller {kind} to mode {required:04o}",
                   inode=info.st_ino, device=info.st_dev, prior_mode=actual,
                   required_mode=required, kind=kind)
    if not any(check["id"] == "state:record-permissions" for check in checks):
        if permission_unsafe:
            add("state:record-permissions", "error",
                f"{len(permission_unsafe)} controller record path(s) are unowned, linked, or not regular",
                paths=permission_unsafe[:20])
        elif permission_loose:
            add("state:record-permissions", "error",
                f"{len(permission_loose)} controller record path(s) are not owner-only",
                paths=permission_loose[:20])
        else:
            add("state:record-permissions", "ok", "controller state records are owner-only")

    projects, project_error = _project_data()
    if project_error:
        add("state:projects", "error", f"projects.json is unreadable: {project_error}")
    else:
        add("state:projects", "ok", f"{len(projects['projects'])} registered project(s)")
    cfg, dispatch_error = _dispatch_data()
    add("state:dispatch", "error" if dispatch_error else "ok",
        f"dispatch.json is unreadable: {dispatch_error}" if dispatch_error else
        ("using shipped defaults (file not initialized)" if not dispatch_file().exists() else "dispatch schema readable"))

    raw_items, item_by_id = [], {}
    if work_root().is_dir():
        for directory in sorted(work_root().iterdir()):
            if not directory.is_dir(): continue
            value, error = _json(directory / "item.json", None)
            schema_error = None if error else _item_schema(value, directory.name)
            if error or schema_error:
                add(f"state:item:{directory.name}", "error",
                    f"item record unreadable: {error or schema_error}")
                continue
            raw_items.append(value); item_by_id[value["id"]] = value
    add("state:items", "ok" if all(c["status"] != "error" for c in checks if c["id"].startswith("state:item:")) else "error",
        f"{len(raw_items)} readable item(s)")

    state_specs = [
        ("supervisor", supervisor_file(), {"version": 1, "observations": {}, "away": {"enabled": False}}, True),
        ("wakes", wakes_file(), {"version": 1, "next_id": 1, "events": []}, True),
        ("claims", home() / "scope-claims.json", {"version": 1, "claims": []}, False),
        ("tabs", home() / "herdr.json", {"tabs": []}, True),
    ]
    parsed = {}
    for name, path, default, quarantine_safe in state_specs:
        value, error = _json(path, default)
        if not error:
            if name in ("supervisor", "wakes"):
                from . import supervisor
                error = (supervisor.validate_state(value) if name == "supervisor"
                         else supervisor.validate_queue(value))
            elif name == "claims": error = _claims_schema(value)
            elif name == "tabs": error = _tabs_schema(value)
        parsed[name] = value if not error else default
        add(f"state:{name}", "error" if error else ("warning" if not path.exists() and name == "supervisor" else "ok"),
            f"{path.name} unreadable: {error}" if error else ("not initialized yet" if not path.exists() else "schema readable"))
        if error and quarantine_safe:
            repair("quarantine-state", str(path), "preserve corrupt file and rebuild only derived/local registry state",
                   digest=_digest(path))
    memory_path = home() / "memory.json"
    if memory_path.exists():
        try:
            from . import memory
            entries = memory.list_operational()
        except (Exception, HelmError) as exc:
            add("state:memory", "error", f"memory.json is unreadable or malformed: {type(exc).__name__}")
        else:
            add("state:memory", "ok", f"{len(entries)} explicit operational memory entr{'y' if len(entries) == 1 else 'ies'}")
    else:
        add("state:memory", "ok", "not initialized; no operational memory stored")
    queue_events = ((parsed.get("wakes") or {}).get("events", [])
                    if isinstance(parsed.get("wakes"), dict) else [])
    for event in queue_events:
        delivery = event.get("delivery_state", "pending")
        if not event.get("acknowledged") and delivery in {"sending", "sent"}:
            add(f"wake-receipt:{event.get('id')}", "unknown",
                "wake send began but turn-start acknowledgement is absent; it will not be replayed automatically",
                item_id=event.get("item_id"), consumer=event.get("claimed_by"), delivery_state=delivery)

    # Worker PIDs are read without daemon_pids(), which intentionally cleans state.
    pid_path = home() / "daemon.pid"; pid_value, pid_error = _json(pid_path, [])
    if not pid_error and not isinstance(pid_value, (list, int, dict)):
        pid_error = "invalid schema"
    if pid_error:
        add("workers:pid-file", "error", f"daemon.pid unreadable: {pid_error}")
        repair("quarantine-state", str(pid_path), "preserve malformed PID registry and rebuild it empty",
               digest=_digest(pid_path))
        pids = []
    else:
        pids = pid_value if isinstance(pid_value, list) else ([pid_value] if pid_value not in (None, "") else [])
        add("workers:pid-file", "ok", f"{len(pids)} registered worker PID(s)")
    for record in pids:
        evidence = processes.probe(record); state = evidence["state"]; pid = evidence.get("pid")
        add(f"worker:{pid}", "ok" if state == "live" else "error" if state == "dead" else "unknown",
            f"worker PID {pid} is {state}: {evidence.get('reason')}", identity_version=record.get("version") if isinstance(record, dict) else None)
        if state == "dead":
            repair("prune-worker-pid", record, "registered worker identity is positively absent",
                   registry_digest=_digest(pid_path))

    live_tabs, tab_error = _live_tabs()
    for tab in (parsed.get("tabs") or {}).get("tabs", []) if isinstance(parsed.get("tabs"), dict) else []:
        tab_id = tab.get("tab_id")
        if live_tabs is None:
            add(f"tab:{tab_id}", "unknown", tab_error or "tab liveness unknown")
        elif tab_id in live_tabs:
            observed = live_tabs[tab_id]
            exact = (bool(tab.get("label")) and observed.get("label") == tab.get("label")
                     and (not tab.get("workspace_id") or observed.get("workspace_id") == tab.get("workspace_id"))
                     and (not tab.get("herdr_session") or observed.get("herdr_session") == tab.get("herdr_session")))
            add(f"tab:{tab_id}", "ok" if exact else "error",
                "registered Herdr tab identity matches" if exact else "tab ID exists but belongs to a different identity")
            if not exact:
                repair("forget-unowned-tab", tab_id,
                       "remove only the stale local registry record; never close the reused live tab",
                       expected_label=tab.get("label"), observed_label=observed.get("label"))
        else:
            add(f"tab:{tab_id}", "error", "registered Herdr tab is positively absent")
            repair("forget-dead-tab", tab_id, "remove only the stale local tab registry entry")

    claims = (parsed.get("claims") or {}).get("claims", []) if isinstance(parsed.get("claims"), dict) else []
    for claim in claims:
        work_id, pid = claim.get("work_id"), claim.get("pid")
        if (not ids.WORK_PATTERN.fullmatch(str(work_id or ""))
                or not ids.PROJECT_PATTERN.fullmatch(str(claim.get("project") or ""))):
            add(f"claim:{work_id}", "error", "claim contains an unsafe project/work identity; no path was inspected")
            continue
        state = ("held" if claim.get("held_for_recovery") else
                 _owner_state(claim.get("process_identity"), pid))
        add(f"claim:{work_id}", "ok" if state in ("live", "held") else "error" if state == "dead" else "unknown",
            f"scope claim is {state}", paths=claim.get("paths"))
        if state == "dead":
            if work_id in item_by_id:
                repair("hold-stale-claim", work_id, "retain collision protection while removing reliance on a dead process identity",
                       pid=pid, process_identity=claim.get("process_identity"),
                       claim_token=claim.get("claim_token"))
            else:
                project = projects["projects"].get(claim.get("project"), {})
                wt = worktree_root() / str(claim.get("project")) / str(work_id)
                branch = _git(Path(project.get("path", "/nonexistent")), "show-ref", "--verify", "--quiet",
                              f"refs/heads/firstmate/{work_id}").returncode == 0 if project else False
                if not wt.exists() and not branch:
                    repair("remove-orphan-claim", work_id, "claim has no item, worktree, or recovery branch",
                           pid=pid, process_identity=claim.get("process_identity"),
                           claim_token=claim.get("claim_token"))

    # Conservatively flag overlapping claims even when one is stale/held.
    for index, left in enumerate(claims):
        for right in claims[index + 1:]:
            if left.get("project") == right.get("project") and scope.overlap(left.get("paths") or ["unknown"], right.get("paths") or ["unknown"]):
                add(f"claim-collision:{left.get('work_id')}:{right.get('work_id')}", "error",
                    "overlapping durable scope claims", left=left.get("paths"), right=right.get("paths"))

    for item in raw_items:
        work_id, status = item["id"], item.get("status")
        lease = item.get("lease") or {}
        lease_state = _owner_state(lease.get("process_identity"), lease.get("pid")) if lease else "missing"
        session = item.get("session") or {}; agent = _herdr_agent(session["agent_name"], session) if session.get("agent_name") else {"state": "none"}
        if status == "running":
            severity = "ok" if lease_state == "live" else "error" if lease_state == "dead" and agent["state"] in ("dead", "none") else "unknown"
            add(f"lease:{work_id}", severity, f"running lease is {lease_state}; session is {agent['state']}")
            if lease_state == "dead" and agent["state"] in ("dead", "none"):
                repair("quarantine-dead-item", work_id, "pause dead execution while preserving session history, branch, and worktree",
                       pid=lease.get("pid"), agent_state=agent["state"], agent_name=session.get("agent_name"),
                       process_identity=lease.get("process_identity"), claim_token=lease.get("claim_token"),
                       revision=int(item.get("revision", 0)))
        elif lease:
            add(f"lease:{work_id}", "error", f"non-running item {status} still has a lease")
        if session.get("agent_name"):
            add(f"session:{work_id}", "ok" if agent["state"] == "live" else "error" if agent["state"] == "dead" else "unknown",
                f"persistent session is {agent['state']}", agent_name=session.get("agent_name"))

        for launch in item.get("agent_launches") or []:
            if launch.get("state") not in {"reserved", "tab-created", "attested"}:
                continue
            launch_id, role, name = launch.get("launch_id"), launch.get("role"), launch.get("agent_name")
            launch_agent = _herdr_agent(name, launch) if name else {"state": "unknown", "reason": "agent name missing"}
            exact_live = (launch_agent.get("state") == "live"
                          and launch_agent.get("identity_verified") is True)
            if exact_live and launch.get("state") == "attested":
                boundary = sandbox.verify_record(launch) and sandbox.verify_tool_record(launch)
                signing = (isinstance(launch.get("event_public_key"), str)
                           and isinstance(launch.get("event_public_key_sha256"), str))
                if boundary and signing and launch.get("sandbox_verified") is True:
                    launch_status, launch_note = "ok", "attested launch has matching live identity and capability receipts"
                else:
                    launch_status, launch_note = "error", "live launch is missing sandbox or signed-ledger receipts"
            elif launch_agent.get("state") == "dead":
                launch_status, launch_note = "error", "launch is positively dead and remains journaled"
                if role == "reviewer":
                    repair("close-dead-reviewer-launch", {"work_id": work_id, "launch_id": launch_id},
                           "mark only the positively dead reviewer launch reconciled so a retry can review again",
                           revision=int(item.get("revision", 0)), agent_name=name)
            else:
                launch_status, launch_note = "unknown", "launch identity/finalization is incomplete or unproven"
            add(f"launch:{work_id}:{launch_id}", launch_status, launch_note,
                role=role, state=launch.get("state"), agent_state=launch_agent.get("state"))

        project_id = str(item.get("project"))
        project = projects["projects"].get(project_id) or {}
        expected_wt = worktree_root() / project_id / work_id
        supplied_wt = Path(item.get("worktree") or expected_wt).expanduser()
        wt = Path(os.path.abspath(os.fspath(supplied_wt)))
        if wt != expected_wt:
            add(f"worktree:{work_id}", "error", "recorded worktree escapes the owned state path; target was not inspected")
            continue
        path_safe, path_reason = worktree.canonical_path_safety(
            project_id, work_id, allow_missing_leaf=True)
        if not path_safe:
            add(f"worktree:{work_id}", "error", f"{path_reason}; target was not inspected")
            continue
        recovery = worktree.registration_recovery_status(project_id, work_id)
        quiescent, quiescence = False, {"reason": "not evaluated"}
        incomplete_recovery = recovery.get("state") not in {"none", "complete", "corrupt"}
        if recovery.get("state") == "corrupt":
            add(f"worktree-recovery:{work_id}", "error",
                f"recovery journal is corrupt and was preserved: {recovery.get('reason')}")
        elif incomplete_recovery:
            quiescent, quiescence = _worktree_recovery_quiescence(item)
            add(f"worktree-recovery:{work_id}", "error",
                f"confirmed reconstruction stopped at durable phase {recovery['state']}",
                source=recovery.get("paths", {}).get("source"),
                quiescence=quiescence)
            if quiescent and project:
                repair("resume-worktree-registration",
                       {"project_id": project_id, "work_id": work_id},
                       "resume only the exact journaled reconstruction; all source copies remain preserved",
                       journal_digest=recovery.get("digest"), revision=int(item.get("revision", 0)))
        elif recovery.get("state") == "complete":
            add(f"worktree-recovery:{work_id}", "warning",
                "registration was reconstructed; the exact original remains retained for manual custody",
                source=recovery.get("paths", {}).get("source"),
                branch_sha=(recovery.get("transaction") or {}).get("branch_sha"))
        if wt.exists() and not incomplete_recovery:
            branch = _git(wt, "rev-parse", "--abbrev-ref", "HEAD")
            dirty = _git(wt, "status", "--porcelain", "--untracked-files=all")
            expected = f"firstmate/{work_id}"
            if branch.returncode or branch.stdout.strip() != expected:
                add(f"worktree:{work_id}", "error", f"worktree is not attached to {expected}")
                if project and recovery.get("state") != "corrupt":
                    quiescent, quiescence = _worktree_recovery_quiescence(item)
                    try:
                        evidence = worktree.detached_registration_evidence(project, work_id, wt)
                    except (OSError, HelmError) as exc:
                        add(f"worktree-recovery-candidate:{work_id}", "error",
                            f"safe reconstruction evidence is unavailable: {getattr(exc, 'msg', type(exc).__name__)}")
                    else:
                        if quiescent:
                            repair("rebuild-worktree-registration",
                                   {"project_id": project_id, "work_id": work_id},
                                   "preserve the exact detached source, rebuild at the exact branch SHA, and retain the source",
                                   evidence=evidence, journal_digest=recovery.get("digest"),
                                   revision=int(item.get("revision", 0)))
                        else:
                            add(f"worktree-recovery-candidate:{work_id}", "unknown",
                                "registration loss is proven, but execution is live or unknown; no repair was inferred",
                                quiescence=quiescence)
            else:
                add(f"worktree:{work_id}", "warning" if dirty.stdout.strip() else "ok",
                    "worktree has preserved uncommitted changes" if dirty.stdout.strip() else "worktree attachment is clean")
        elif status in ("running", "paused", "needs-you", "ready", "pr-open") or incomplete_recovery:
            add(f"worktree:{work_id}", "error", f"open item {status} has no worktree")

    # Detect worktrees that have no item, without deleting them.
    root_safe, root_reason = worktree.root_path_safety()
    if not root_safe:
        add("worktree-root", "error", f"{root_reason}; orphan targets were not traversed")
    elif worktree_root().is_dir():
        for project_dir in worktree_root().iterdir():
            try: project_stat = project_dir.lstat()
            except OSError:
                add(f"worktree-project:{project_dir.name}", "error",
                    "worktree project directory changed during inspection"); continue
            if (not statmod.S_ISDIR(project_stat.st_mode) or project_dir.is_symlink()
                    or project_stat.st_uid != os.getuid() or project_stat.st_mode & 0o022):
                add(f"worktree-project:{project_dir.name}", "error",
                    "worktree project path is linked, unowned, or writable by another user; it was not traversed")
                continue
            for wt in project_dir.iterdir():
                try: wt_stat = wt.lstat()
                except OSError:
                    add(f"worktree-orphan:{project_dir.name}:{wt.name}", "error",
                        "worktree entry changed during inspection"); continue
                safe_directory = (statmod.S_ISDIR(wt_stat.st_mode) and not wt.is_symlink()
                                  and wt_stat.st_uid == os.getuid() and not wt_stat.st_mode & 0o022)
                if safe_directory and wt.name not in item_by_id:
                    add(f"worktree-orphan:{project_dir.name}:{wt.name}", "warning",
                        "unowned worktree is preserved; confirmed repair can move it intact to quarantine")
                    if (ids.PROJECT_PATTERN.fullmatch(project_dir.name) and ids.WORK_PATTERN.fullmatch(wt.name)
                            and project_dir.name in projects.get("projects", {})):
                        repair("quarantine-orphan-worktree",
                               {"project_id": project_dir.name, "work_id": wt.name, "path": str(wt)},
                               "move the exact orphan intact to controller quarantine; retain its branch and files",
                               inode=wt_stat.st_ino, device=wt_stat.st_dev)
                elif not safe_directory:
                    add(f"worktree-orphan:{project_dir.name}:{wt.name}", "error",
                        "worktree entry is linked, unowned, or writable by another user; it was preserved")

    for project_id, project in projects["projects"].items():
        if (not ids.PROJECT_PATTERN.fullmatch(str(project_id)) or project.get("id") != project_id):
            add(f"project:{project_id}", "error", "project identity is unsafe or does not match its registry key")
            continue
        path = Path(str(project.get("path", "")))
        if not path.exists() or _git(path, "rev-parse", "--git-dir").returncode:
            add(f"project:{project_id}", "error", f"repository unavailable at {path}"); continue
        base = str(project.get("base") or "")
        local = _git(path, "rev-parse", "--verify", base)
        if local.returncode:
            add(f"project:{project_id}:base", "error", f"base ref {base!r} is missing")
        else:
            dirty = _git(path, "status", "--porcelain", "--untracked-files=all")
            add(f"project:{project_id}:checkout", "warning" if dirty.stdout.strip() else "ok",
                "captain checkout is dirty (preserved)" if dirty.stdout.strip() else "captain checkout is clean")
        test_cmd = str(project.get("test_cmd") or "")
        syntax = _run(["bash", "-n", "-c", test_cmd]) if test_cmd else None
        add(f"project:{project_id}:test", "ok" if syntax and syntax.returncode == 0 else "error",
            "test command is defined and shell-valid" if syntax and syntax.returncode == 0 else "test command is missing or invalid",
            command=test_cmd or None, executed=False)
        gate = gates.status(project.get("gate", "native"), path)
        add(f"project:{project_id}:gate", "ok" if gate.get("ready") else "error", gate.get("reason", "unverified"),
            provider=gate.get("provider"), evidence={k: v for k, v in gate.items() if k not in ("reason", "binary")})
        origin = git_config(path, "remote.origin.url")
        if not origin:
            add(f"project:{project_id}:freshness", "warning", "no origin remote; freshness cannot be compared")
        elif not network:
            add(f"project:{project_id}:freshness", "unknown", "network freshness skipped by --offline")
        else:
            remote = _git(path, "ls-remote", "--heads", origin, f"refs/heads/{base}",
                          timeout=15, network=True,
                          read_paths=_local_remote_read_paths(origin))
            lines = remote.stdout.splitlines() if remote.returncode == 0 else []
            remote_sha = lines[0].split()[0] if lines else None
            if not remote_sha:
                add(f"project:{project_id}:freshness", "unknown", "remote base unavailable; outage/auth/rate limit is not success")
            elif local.returncode == 0 and remote_sha == local.stdout.strip():
                add(f"project:{project_id}:freshness", "ok", "configured base matches the exact remote head", sha=remote_sha)
            else:
                add(f"project:{project_id}:freshness", "warning", "configured base differs from the exact remote head",
                    local=local.stdout.strip() or None, remote=remote_sha)

    auth, auth_error = _json(home() / "pi" / "auth.json", {})
    has_codex = not auth_error and isinstance(auth, dict) and isinstance(auth.get("openai-codex"), dict)
    add("auth:codex", "ok" if has_codex else "error", "Codex credential record present (secret not inspected)" if has_codex else "Codex credential record missing or unreadable")
    gh = shutil.which("gh")
    if gh and network:
        gh_status = _run([gh, "auth", "status"], cwd=home(), timeout=15, network=True)
        add("auth:github", "ok" if gh_status.returncode == 0 else "unknown",
            "GitHub CLI authentication accepted" if gh_status.returncode == 0 else "GitHub auth unavailable; not treated as success")
    else:
        add("auth:github", "unknown", "GitHub auth not probed")

    wanted = set((cfg.get("models") or {}).values())
    available, inventory_error = _offline_model_inventory()
    for model in sorted(wanted):
        if inventory_error:
            add(f"model:{model}", "unknown", inventory_error)
        elif model not in available:
            add(f"model:{model}", "error", "resolved model is absent from the authoritative inventory")
        elif probe_models:
            good, detail = _live_model_probe(model)
            add(f"model:{model}", "ok" if good else "error", detail)
        else:
            add(f"model:{model}", "ok", "listed (no token-spending live probe requested)")
    for graph in ("high-assurance", "direct-pr", "local-only", "scout"):
        add(f"graph:{graph}", "ok" if (GRAPHS / f"{graph}.yaml").is_file() else "error", "template present" if (GRAPHS / f"{graph}.yaml").is_file() else "template missing")

    errors = sum(c["status"] == "error" for c in checks)
    warnings = sum(c["status"] in ("warning", "unknown") for c in checks)
    return {"version": VERSION, "generated": now(), "read_only": True, "healthy": errors == 0,
            "summary": {"ok": sum(c["status"] == "ok" for c in checks), "errors": errors, "warnings_or_unknown": warnings},
            "checks": checks, "repairs": repairs}


def _quarantine(path: Path, default) -> str:
    stamp = f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns()}"
    backup = path.with_name(path.name + f".quarantine-{stamp}")
    if path.exists(): os.replace(path, backup)
    write_json(path, default)
    return str(backup)


def repair(*, confirm: bool, network: bool = True, auth_source: str | None = None,
           auth_provider: str = "openai-codex", model_specs: list[str] | None = None,
           test_specs: list[str] | None = None, fetch_projects: list[str] | None = None) -> dict:
    """Apply only the safe plan from a fresh audit; never delete work or relaunch."""
    report = audit(network=network)
    if not confirm:
        raise HelmError("repair plan is read-only until explicitly confirmed with `pi-firstmate doctor --repair --confirm`")
    applied, skipped = [], []
    with locked(home() / "doctor-repair.lock"):
        # Quarantine positively dead execution before exchanging its stale
        # claim. This lets the claim token bind to the paused recovery record
        # without invalidating the item's audited revision mid-plan.
        planned = sorted(enumerate(report["repairs"]),
                         key=lambda pair: (0 if pair[1]["action"] == "quarantine-dead-item" else 1,
                                           pair[0]))
        for _, action in planned:
            name, target = action["action"], action["target"]
            if name == "tighten-state-home-permissions":
                path = Path(str(target)).resolve()
                try: stat = path.stat()
                except OSError:
                    skipped.append({**action, "skip": "state home disappeared"}); continue
                if (path != home() or not path.is_dir() or stat.st_uid != os.getuid()
                        or stat.st_ino != action.get("inode") or stat.st_dev != action.get("device")
                        or (stat.st_mode & 0o777) != action.get("prior_mode")):
                    skipped.append({**action, "skip": "state-home ownership or mode evidence changed"}); continue
                path.chmod(0o700); applied.append(action)
            elif name == "tighten-state-record-permissions":
                path = Path(str(target))
                try:
                    relative = path.relative_to(home())
                except ValueError:
                    skipped.append({**action, "skip": "record target escaped the state home"}); continue
                direct_record = (len(relative.parts) == 1 and
                                 (relative.name.endswith((".json", ".lock", ".log", ".pid"))
                                  or relative.name in {"projects.json", "dispatch.json"}))
                nested_record = bool(relative.parts and relative.parts[0] in {"work", "pi", "recovery"}
                                     and ".git" not in relative.parts)
                if not (direct_record or nested_record):
                    skipped.append({**action, "skip": "record target is outside controller-owned state"}); continue
                flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
                try:
                    descriptor = os.open(path, flags)
                except OSError:
                    skipped.append({**action, "skip": "record disappeared or became unsafe"}); continue
                try:
                    current = os.fstat(descriptor)
                    expected_kind = (statmod.S_ISDIR(current.st_mode) if action.get("kind") == "directory"
                                     else statmod.S_ISREG(current.st_mode))
                    if (current.st_uid != os.getuid() or current.st_ino != action.get("inode")
                            or current.st_dev != action.get("device")
                            or (current.st_mode & 0o777) != action.get("prior_mode")
                            or not expected_kind or action.get("required_mode") not in {0o600, 0o700}):
                        skipped.append({**action, "skip": "record ownership, inode, kind, or mode evidence changed"})
                        continue
                    os.fchmod(descriptor, int(action["required_mode"])); applied.append(action)
                finally:
                    os.close(descriptor)
            elif name == "prune-worker-pid":
                with locked(home() / "daemon.lock"):
                    value, error = _json(home() / "daemon.pid", [])
                    pids = value if isinstance(value, list) else [value]
                    if (not error and action.get("registry_digest") == _digest(home() / "daemon.pid")
                            and processes.probe(target).get("state") == "dead" and target in pids):
                        write_json(home() / "daemon.pid", [p for p in pids if p != target]); applied.append(action)
                    else: skipped.append({**action, "skip": "evidence changed"})
            elif name == "close-dead-reviewer-launch":
                work_id, launch_id = str(target.get("work_id")), str(target.get("launch_id"))
                current_item = read_json(home() / "work" / work_id / "item.json") or {}
                current_launch = next((entry for entry in current_item.get("agent_launches", [])
                                       if entry.get("launch_id") == launch_id), {})
                if _herdr_agent(action.get("agent_name"), current_launch).get("state") != "dead":
                    skipped.append({**action, "skip": "reviewer liveness evidence changed"}); continue
                def close_launch(item):
                    launch = next((entry for entry in item.get("agent_launches", [])
                                   if entry.get("launch_id") == launch_id), None)
                    if (not launch or launch.get("role") != "reviewer"
                            or launch.get("state") not in {"reserved", "tab-created", "attested"}
                            or launch.get("agent_name") != action.get("agent_name")):
                        raise HelmError("reviewer launch evidence changed during repair")
                    launch.update(state="dead-confirmed", reconciled_at=now())
                try: control.cas_update(work_id, close_launch, expected_revision=action.get("revision"))
                except HelmError: skipped.append({**action, "skip": "evidence changed"})
                else: applied.append(action)
            elif name == "quarantine-dead-item":
                work_id, expected_pid = str(target), action.get("pid")
                expected_identity = action.get("process_identity")
                expected_agent_name, expected_agent_state = action.get("agent_name"), action.get("agent_state")
                current_item = read_json(home() / "work" / work_id / "item.json") or {}
                current_session = current_item.get("session") or {}
                current_agent_state = (_herdr_agent(expected_agent_name, current_session).get("state")
                                       if expected_agent_name else "none")
                if current_agent_state != expected_agent_state:
                    skipped.append({**action, "skip": "agent liveness evidence changed"}); continue
                # A dead controller never reached the attempt-finalization write.
                # Reconcile durable Pi receipts and conservatively count the
                # open lease's elapsed wall time before authorizing recovery.
                # The harvest is receipt-keyed, so a crash here is replay-safe.
                lease_before = current_item.get("lease") or {}
                elapsed = 0.0
                try:
                    started_epoch = dt.datetime.fromisoformat(
                        str(lease_before.get("started")).replace("Z", "+00:00")).timestamp()
                    elapsed = max(0.0, time.time() - started_epoch)
                except (TypeError, ValueError, OverflowError):
                    pass
                try:
                    from . import work as work_items
                    current_item = work_items._harvest_usage(work_id, elapsed)
                except (Exception, HelmError):
                    skipped.append({**action, "skip": "dead execution usage could not be reconciled"})
                    continue
                current_session = current_item.get("session") or {}
                current_agent_state = (_herdr_agent(expected_agent_name, current_session).get("state")
                                       if expected_agent_name else "none")
                if current_agent_state != expected_agent_state:
                    skipped.append({**action, "skip": "agent liveness evidence changed after usage reconciliation"})
                    continue
                reconciled_revision = current_item.get("revision")
                def pause(item):
                    lease = item.get("lease") or {}
                    current_name = (item.get("session") or {}).get("agent_name")
                    if (item.get("status") != "running" or lease.get("pid") != expected_pid or
                            lease.get("process_identity") != expected_identity or
                            current_name != expected_agent_name or
                            _owner_state(expected_identity, expected_pid) != "dead"):
                        raise HelmError("dead-item evidence changed during repair")
                    if item.get("session"):
                        item.setdefault("session_history", []).append({**item["session"], "quarantined_at": now(), "reason": "positively dead"})
                    item["recovery_claim_token"] = lease.get("claim_token")
                    item.pop("lease", None); item["phase"] = "recovery-required"; item["status"] = "paused"
                    item.setdefault("controls", {})["paused"] = True
                    item["ask"] = {"question": "Execution died. Inspect the preserved worktree and explicitly recover when ready.",
                                   "context": "Doctor quarantined positive death evidence; nothing was relaunched or discarded."}
                    item.setdefault("history", []).append({"at": now(), "from": "running", "to": "paused", "note": "confirmed doctor quarantine; work preserved"})
                try: control.cas_update(work_id, pause, expected_revision=reconciled_revision)
                except HelmError: skipped.append({**action, "skip": "evidence changed"})
                else: applied.append(action)
            elif name in ("hold-stale-claim", "remove-orphan-claim"):
                path = home() / "scope-claims.json"
                with locked(home() / "scope-claims.lock"):
                    data, error = _json(path, {"version": 1, "claims": []})
                    match = next((c for c in (data or {}).get("claims", []) if c.get("work_id") == target), None) if not error else None
                    if (not match or match.get("process_identity") != action.get("process_identity")
                            or match.get("claim_token") != action.get("claim_token")
                            or _owner_state(match.get("process_identity"), match.get("pid")) != "dead"):
                        skipped.append({**action, "skip": "evidence changed"}); continue
                    if name == "hold-stale-claim":
                        replacement_token = match.get("claim_token") or secrets.token_hex(24)
                        if not match.get("claim_token"):
                            # Legacy claims had no ownership token. Bind a new
                            # unguessable recovery token into controller item
                            # state before making the claim exchangeable. A
                            # crash between these writes remains fail-closed:
                            # the old held claim still blocks all workers.
                            try:
                                def bind_legacy_token(item):
                                    lease = item.get("lease") or {}
                                    if (item.get("status") == "running"
                                            and lease.get("pid") == action.get("pid")
                                            and lease.get("process_identity") == action.get("process_identity")
                                            and lease.get("claim_token") in (None, "")):
                                        # The dead-item quarantine action runs
                                        # later in the same confirmed plan and
                                        # will carry this token into recovery.
                                        lease["claim_token"] = replacement_token
                                    elif (item.get("status") != "running"
                                          and item.get("recovery_claim_token") in (None, "")):
                                        item["recovery_claim_token"] = replacement_token
                                        item["status"] = "paused"; item["phase"] = "recovery-required"
                                        item.setdefault("controls", {})["paused"] = True
                                    else:
                                        raise HelmError("legacy recovery item evidence changed")
                                control.cas_update(str(target), bind_legacy_token)
                            except HelmError:
                                skipped.append({**action, "skip": "legacy recovery item evidence changed"}); continue
                        match.update(pid=None, process_identity=None, owner=f"recovery:{target}",
                                     claim_token=replacement_token,
                                     held_for_recovery=True, reconciled=now())
                    else:
                        wt = worktree_root() / str(match.get("project")) / str(target)
                        projects, _ = _project_data(); project = projects.get("projects", {}).get(match.get("project"), {})
                        branch = (_git(Path(project["path"]), "show-ref", "--verify", "--quiet",
                                       f"refs/heads/firstmate/{target}").returncode == 0) if project.get("path") else False
                        if wt.exists() or (work_root() / str(target) / "item.json").exists() or branch:
                            skipped.append({**action, "skip": "recovery evidence now exists"}); continue
                        data["claims"] = [c for c in data["claims"] if c is not match]
                    write_json(path, data); applied.append(action)
            elif name in ("forget-dead-tab", "forget-unowned-tab"):
                live, _ = _live_tabs(); path = home() / "herdr.json"
                with locked(home() / "herdr.lock"):
                    data, error = _json(path, {"tabs": []})
                    record = next((entry for entry in (data or {}).get("tabs", [])
                                   if entry.get("tab_id") == target), None) if not error else None
                    absent = live is not None and target not in live
                    reused = (live is not None and target in live and record is not None
                              and live[target].get("label") != record.get("label"))
                    if record and ((name == "forget-dead-tab" and absent)
                                   or (name == "forget-unowned-tab" and reused)):
                        data["tabs"] = [t for t in data.get("tabs", []) if t.get("tab_id") != target]
                        write_json(path, data); applied.append(action)
                    else: skipped.append({**action, "skip": "tab liveness is no longer positive"})
            elif name == "quarantine-state":
                path = Path(str(target)).resolve()
                allowed = {supervisor_file(), wakes_file(), home() / "herdr.json", home() / "daemon.pid"}
                defaults = {supervisor_file(): {"version": 1, "observations": {}, "away": {"enabled": False}},
                            wakes_file(): {"version": 1, "next_id": 1, "events": []},
                            home() / "herdr.json": {"tabs": []}, home() / "daemon.pid": []}
                lock_path = (supervisor_lock() if path in {supervisor_file(), wakes_file()} else
                             home() / "herdr.lock" if path == home() / "herdr.json" else
                             home() / "daemon.lock")
                with locked(lock_path):
                    if path in allowed and action.get("digest") is not None and _digest(path) == action.get("digest"):
                        applied.append({**action, "backup": _quarantine(path, defaults[path])})
                    else: skipped.append({**action, "skip": "target is not repairable or its evidence changed"})
            elif name in {"rebuild-worktree-registration", "resume-worktree-registration"}:
                project_id, work_id = str(target.get("project_id")), str(target.get("work_id"))
                try:
                    ids.project(project_id); ids.work(work_id)
                except HelmError:
                    skipped.append({**action, "skip": "unsafe recovery identity"}); continue
                projects_now, project_error = _project_data()
                project = (projects_now.get("projects", {}).get(project_id)
                           if not project_error else None)
                if not project:
                    skipped.append({**action, "skip": "registered project evidence is unavailable"}); continue
                with locked(control.item_lock(work_id)):
                    item, item_error = _json(work_root() / work_id / "item.json", None)
                    schema_error = None if item_error else _item_schema(item, work_id)
                    if (item_error or schema_error or item.get("project") != project_id
                            or int(item.get("revision", -1)) != action.get("revision")):
                        skipped.append({**action, "skip": "item identity or revision changed"}); continue
                    quiescent, quiescence = _worktree_recovery_quiescence(item)
                    if not quiescent:
                        skipped.append({**action, "skip": quiescence.get("reason", "execution is not quiescent")}); continue
                    try:
                        if name == "rebuild-worktree-registration":
                            result = worktree.begin_registration_recovery(
                                project, item, action.get("evidence") or {},
                                expected_journal_digest=action.get("journal_digest"))
                        else:
                            result = worktree.resume_registration_recovery(
                                project, work_id,
                                expected_journal_digest=str(action.get("journal_digest") or ""))
                    except (OSError, HelmError) as exc:
                        skipped.append({**action, "skip": f"safe reconstruction stopped: {getattr(exc, 'msg', type(exc).__name__)}"})
                    else:
                        applied.append({**action, "result": result})
            elif name == "quarantine-orphan-worktree":
                project_id, work_id = str(target.get("project_id")), str(target.get("work_id"))
                path = worktree_root() / project_id / work_id
                with locked(home() / "claim.lock"):
                    try: stat = path.stat()
                    except OSError:
                        skipped.append({**action, "skip": "orphan disappeared"}); continue
                    safe, _reason = worktree.canonical_path_safety(project_id, work_id)
                    target_path = Path(os.path.abspath(os.path.expanduser(str(target.get("path")))))
                    if (path != target_path or not safe
                            or stat.st_ino != action.get("inode") or stat.st_dev != action.get("device")
                            or (work_root() / work_id / "item.json").exists()):
                        skipped.append({**action, "skip": "orphan ownership evidence changed"}); continue
                    try:
                        result = worktree.quarantine(registry.get(project_id), work_id,
                                                     reason="confirmed doctor orphan reconciliation")
                    except (Exception, HelmError) as exc:
                        skipped.append({**action, "skip": f"intact quarantine unavailable: {type(exc).__name__}"})
                    else:
                        applied.append({**action, "result": result})
        # The following repairs are never inferred. They require an explicit
        # captain-supplied target in addition to --repair --confirm.
        if auth_source:
            provider = str(auth_provider or "")
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", provider):
                raise HelmError("--auth-provider is invalid")
            source = Path(auth_source).expanduser()
            if source.is_symlink():
                raise HelmError("authentication source symlinks are refused")
            source_file = source / "auth.json" if source.is_dir() else source
            source_data = _owned_json_nofollow(source_file)
            if not isinstance(source_data.get(provider), dict):
                raise HelmError("authentication source has no safe matching provider record")
            destination = home() / "pi" / "auth.json"
            pi_dir = destination.parent
            if pi_dir.exists() and (pi_dir.is_symlink() or not pi_dir.is_dir() or pi_dir.resolve().parent != home()):
                raise HelmError("destination Pi configuration directory is unsafe")
            pi_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            with locked(pi_dir / "auth.lock"):
                current, error = _json(destination, {})
                if error or not isinstance(current, dict) or destination.is_symlink():
                    raise HelmError("destination authentication record is malformed; refusing overwrite")
                current[provider] = source_data[provider]
                write_json(destination, current); destination.chmod(0o600)
            applied.append({"action": "reconcile-authentication", "provider": provider,
                            "source": str(source_file), "destination": str(destination)})
        for spec in model_specs or []:
            phase, separator, model = str(spec).partition("=")
            if not separator or phase not in dispatch.PHASES or not re.fullmatch(r"[^/\s]+/[^\s]+", model):
                raise HelmError(f"invalid --model {spec!r}; use PHASE=provider/model")
            available, inventory_error = _offline_model_inventory()
            if inventory_error:
                raise HelmError(f"model inventory is unavailable ({inventory_error}); reconciliation failed closed")
            if model not in available:
                raise HelmError(f"replacement model {model!r} is not in the authoritative inventory")
            with locked(authority_lock()):
                cfg = dispatch.load(); prior = cfg["models"].get(phase); cfg["models"][phase] = model
                write_json(dispatch_file(), cfg)
            applied.append({"action": "reconcile-model", "phase": phase, "from": prior, "to": model})
        for spec in test_specs or []:
            project_id, separator, command = str(spec).partition("=")
            if not separator or not command.strip():
                raise HelmError(f"invalid --test {spec!r}; use PROJECT=COMMAND")
            ids.project(project_id)
            syntax = _run(["bash", "-n", "-c", command])
            if syntax.returncode:
                raise HelmError(f"test command for {project_id} is not shell-valid")
            before = registry.get(project_id).get("test_cmd")
            registry.set_fields(project_id, test_cmd=command)
            applied.append({"action": "reconcile-test-command", "project": project_id,
                            "from": before, "to": command, "executed": False})
        for project_id in fetch_projects or []:
            with locked(authority_lock()):
                project = registry.get(project_id); repo = Path(project["path"])
                base = str(project.get("base") or "")
                if _run(["git", "check-ref-format", "--branch", base]).returncode:
                    raise HelmError(f"project {project_id} has an unsafe base ref")
                before_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
                before_status = _git(repo, "status", "--porcelain", "--untracked-files=all").stdout
                origin = git_config(repo, "remote.origin.url")
                if not origin:
                    raise HelmError(f"project {project_id} has no integrity-checked origin URL")
                result = _run(["git", "-C", str(repo), "fetch", "--no-tags",
                               "--no-write-fetch-head", origin,
                               f"refs/heads/{base}:refs/remotes/origin/{base}"],
                              timeout=120, network=True, write_paths=(repo,),
                              read_paths=_local_remote_read_paths(origin))
                after_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
                after_status = _git(repo, "status", "--porcelain", "--untracked-files=all").stdout
                if result.returncode:
                    raise HelmError(f"fetch for {project_id} failed; network uncertainty was not treated as freshness")
                if after_head != before_head or after_status != before_status:
                    raise HelmError(f"fetch for {project_id} changed the checkout unexpectedly; inspect it manually")
                remote = _git(repo, "rev-parse", f"refs/remotes/origin/{base}")
                if remote.returncode:
                    raise HelmError(f"fetch for {project_id} produced no exact remote-base receipt")
                applied.append({"action": "reconcile-clone-freshness", "project": project_id,
                                "remote_ref": f"refs/remotes/origin/{base}", "remote_sha": remote.stdout.strip(),
                                "checkout_head": before_head, "checkout_preserved": True})
        # Rebuild only derived observations/wakes. Unknown liveness stays unknown;
        # claims, sessions, items, branches and worktrees remain untouched.
        try:
            from . import supervisor
            supervisor.scan(probe_agents=False)
        except (Exception, HelmError):
            pass
    return {"confirmed": True, "destructive": False, "applied": applied, "skipped": skipped,
            "post_audit": audit(network=network)}


def render(report: dict) -> str:
    lines = [f"doctor: {'healthy' if report['healthy'] else 'attention required'} · read-only",
             f"  {report['summary']['ok']} ok · {report['summary']['errors']} errors · {report['summary']['warnings_or_unknown']} warnings/unknown"]
    for check in report["checks"]:
        icon = {"ok": "ok ", "warning": "WARN", "error": "FAIL", "unknown": "????"}[check["status"]]
        lines.append(f"{icon} {check['id']} — {check['summary']}")
    if report.get("repairs"):
        lines += ["", f"repair plan: {len(report['repairs'])} explicitly confirmable non-destructive action(s)",
                  "  inspect JSON: pi-firstmate doctor --json",
                  "  apply:        pi-firstmate doctor --repair --confirm"]
    return "\n".join(lines)
