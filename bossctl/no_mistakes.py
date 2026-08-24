"""Pinned, fail-closed adapter for the optional real no-mistakes product.

The adapter deliberately treats the no-mistakes CLI as an external transaction:
BOSS journals intent before invoking it, reads authoritative receipts from
the product's SQLite database, and never replays an uncertain request.  It does
not install, initialize, update, abort, sync, reset, or otherwise repair the
external product.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import sqlite3
import stat
import subprocess
from contextlib import closing
from pathlib import Path
from urllib.parse import unquote, urlparse, urlsplit, urlunsplit

from .util import BossError, git, git_config, now


VERSION = "1.57.1"
TAG_SHA = "a6f64fcdb4e82c0ddbbd9f01ed91e97dd233d42d"
BUILD_SHA = TAG_SHA[:7]
MACOS_TEAM_ID = "9T2J7MNUP9"
MACOS_IDENTIFIER = "com.kunchenguid.no-mistakes"

# Hashes of the signed executable inside the official v1.57.1 release archives.
# The corresponding archive digests are retained as provenance documentation;
# an installed executable is checked against the executable hash, not confused
# with the tarball hash.
MACOS_RELEASES = {
    "x86_64": {
        "archive_sha256": "9854dd5ac35fc0add1246193872a567de0a92fbf2b508dc9ee45b7cd8b9c6a2e",
        "binary_sha256": "5147d0abd3856697b816e3c25f4ac0cac00d1eccca1bcb6c7c1d791170f0fb7f",
    },
    "arm64": {
        "archive_sha256": "ac006a1a48c3eeaca63c8b2e33486eca215683fe2528783590444dc3754cfdc6",
        "binary_sha256": "66c0ee4630c918ff846ef41de252deba20de5264b7b1ddd42b92551f2eef0427",
    },
}

REQUIRED_REPO_COLUMNS = {
    "id", "working_path", "upstream_url", "fork_url", "default_branch",
}
REQUIRED_RUN_COLUMNS = {
    "id", "repo_id", "branch", "head_sha", "base_sha", "submitted_head_sha",
    "no_mistakes_version", "no_mistakes_build_sha", "review_approved_head_sha",
    "status", "pr_url", "pr_state", "ci_ready_at", "ci_ready_no_ci",
    "last_pushed_sha", "push_target_kind", "push_target_fingerprint",
    "push_ref", "push_active", "terminal_head_verified_at", "error",
    "awaiting_agent_since", "created_at", "updated_at",
}
REQUIRED_STEP_COLUMNS = {
    "id", "run_id", "step_name", "step_order", "status", "findings_json",
}
OPEN_STEP_STATES = {"awaiting_approval", "fix_review"}
TERMINAL_FAILURES = {"failed", "cancelled", "ci_monitor_interrupted"}
OPEN_TRANSACTION_STATES = {
    "armed", "request-started", "command-returned", "running", "needs-decision",
    "response-armed", "response-requested", "unknown",
}


class AdapterError(Exception):
    """Read-side adapter uncertainty that must not print or mutate state."""


def _full_sha(value: object) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{40}", str(value or "")))


def _target_fingerprint(raw: str) -> str:
    """Reproduce pinned v1.57.1 branchsync.TargetFingerprint exactly for normal Git URLs."""
    value = str(raw or "").strip()
    parsed = urlsplit(value)
    if parsed.scheme:
        scheme, netloc = parsed.scheme, parsed.netloc
        if scheme.lower() in {"http", "https"}:
            scheme = scheme.lower()
            host = (parsed.hostname or "").lower()
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            try:
                port = parsed.port
            except ValueError as exc:
                raise AdapterError("no-mistakes push target URL has an invalid port") from exc
            netloc = host + (f":{port}" if port is not None else "")
        value = urlunsplit((scheme, netloc, parsed.path, parsed.query, ""))
    return hashlib.sha256(value.rstrip("/").encode()).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _owned_regular(path: Path, *, executable: bool = False) -> tuple[bool, str]:
    if path.is_symlink():
        return False, f"{path} is a symbolic link"
    try:
        info = path.stat()
    except OSError as exc:
        return False, f"cannot stat {path}: {type(exc).__name__}"
    if not stat.S_ISREG(info.st_mode):
        return False, f"{path} is not a regular file"
    if info.st_uid not in {os.getuid(), 0}:
        return False, f"{path} is owned by uid {info.st_uid}, not the current user or root"
    if info.st_mode & 0o022:
        return False, f"{path} is group/world writable"
    if executable and not info.st_mode & 0o111:
        return False, f"{path} is not executable"
    return True, ""


def _command(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, stdin=subprocess.DEVNULL, text=True,
                          capture_output=True, **kwargs)


def _binary_attestation(binary: str | Path) -> dict:
    candidate = Path(binary).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        return {"verified": False, "reason": f"binary resolution failed: {type(exc).__name__}"}
    safe, reason = _owned_regular(resolved, executable=True)
    if not safe:
        return {"verified": False, "binary": str(resolved), "reason": reason}
    if platform.system() != "Darwin":
        return {"verified": False, "binary": str(resolved),
                "reason": "the pinned adapter currently attests only signed macOS release binaries"}
    machine = platform.machine().lower()
    machine = "x86_64" if machine in {"amd64", "x86_64"} else machine
    release = MACOS_RELEASES.get(machine)
    if not release:
        return {"verified": False, "binary": str(resolved),
                "reason": f"unsupported macOS architecture {machine!r}"}
    binary_hash = _sha256(resolved)
    if binary_hash != release["binary_sha256"]:
        return {"verified": False, "binary": str(resolved), "sha256": binary_hash,
                "reason": "binary SHA-256 does not match the pinned official release executable"}

    try:
        version = _command([str(resolved), "--version"])
    except OSError as exc:
        return {"verified": False, "binary": str(resolved), "sha256": binary_hash,
                "reason": f"binary version probe failed: {type(exc).__name__}"}
    version_text = (version.stdout + " " + version.stderr).strip()
    match = re.fullmatch(
        rf"no-mistakes version v{re.escape(VERSION)} \(({re.escape(BUILD_SHA)})\) "
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", version_text,
    )
    if version.returncode or not match:
        return {"verified": False, "binary": str(resolved), "sha256": binary_hash,
                "version_output": version_text[:500],
                "reason": "binary version/build output does not match the pinned release"}

    try:
        verify = _command(["/usr/bin/codesign", "--verify", "--strict", "--verbose=2", str(resolved)])
        details = _command(["/usr/bin/codesign", "-dvvv", str(resolved)])
        requirement = _command(["/usr/bin/codesign", "-d", "-r-", str(resolved)])
        arch = _command(["/usr/bin/lipo", "-archs", str(resolved)])
    except OSError as exc:
        return {"verified": False, "binary": str(resolved), "sha256": binary_hash,
                "version": VERSION, "build_sha": BUILD_SHA,
                "reason": f"macOS signature tool failed: {type(exc).__name__}"}
    signature = details.stdout + details.stderr
    designated = requirement.stdout + requirement.stderr
    timestamp = re.search(r"(?m)^Timestamp=(.+)$", signature)
    signature_ok = (
        verify.returncode == 0 and details.returncode == 0 and requirement.returncode == 0
        and arch.returncode == 0 and arch.stdout.strip() == machine
        and f"Identifier={MACOS_IDENTIFIER}" in signature
        and f"TeamIdentifier={MACOS_TEAM_ID}" in signature
        and "Authority=Developer ID Application:" in signature
        and re.search(r"flags=0x[0-9a-f]+\([^)]*runtime[^)]*\)", signature, re.I)
        and timestamp and timestamp.group(1).strip().lower() != "none"
        and "anchor apple generic" in designated
        and f'identifier "{MACOS_IDENTIFIER}"' in designated
        and re.search(rf"leaf\[subject\.OU\]\s*=\s*\"?{MACOS_TEAM_ID}\"?", designated)
        and "cdhash H" not in designated
        and "adhoc" not in signature.lower()
    )
    if not signature_ok:
        return {"verified": False, "binary": str(resolved), "sha256": binary_hash,
                "version": VERSION, "build_sha": BUILD_SHA,
                "reason": "Developer ID signature, runtime, timestamp, identifier, team, or architecture was not proven"}
    info = resolved.stat()
    return {
        "verified": True, "binary": str(resolved), "sha256": binary_hash,
        "device": info.st_dev, "inode": info.st_ino, "size": info.st_size,
        "version": VERSION, "build_sha": BUILD_SHA, "tag_sha": TAG_SHA,
        "team_id": MACOS_TEAM_ID, "identifier": MACOS_IDENTIFIER,
        "architecture": machine, "archive_sha256": release["archive_sha256"],
        "reason": "pinned release hash, embedded build, and Developer ID identity verified",
    }


def _home(override: Path | str | None = None) -> Path:
    raw = str(override) if override is not None else os.environ.get("NM_HOME", "")
    path = Path(raw).expanduser() if raw else Path.home() / ".no-mistakes"
    if not path.is_absolute():
        raise AdapterError("NM_HOME must be absolute for a pinned no-mistakes adapter")
    return path.resolve()


def _db_connect(path: Path) -> sqlite3.Connection:
    safe, reason = _owned_regular(path)
    if not safe:
        raise AdapterError(f"no-mistakes state database is unsafe: {reason}")
    uri = path.as_uri() + "?mode=ro"
    connection = None
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=2)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("BEGIN")
        return connection
    except sqlite3.Error as exc:
        if connection is not None:
            connection.close()
        raise AdapterError(f"cannot open no-mistakes state database read-only: {exc}") from exc


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in db.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error as exc:
        raise AdapterError(f"cannot inspect no-mistakes {table} schema: {exc}") from exc


def _schema(db: sqlite3.Connection) -> None:
    requirements = {
        "repos": REQUIRED_REPO_COLUMNS,
        "runs": REQUIRED_RUN_COLUMNS,
        "step_results": REQUIRED_STEP_COLUMNS,
    }
    for table, required in requirements.items():
        missing = required - _columns(db, table)
        if missing:
            raise AdapterError(f"no-mistakes database schema lacks {table} columns: {', '.join(sorted(missing))}")


def _local_path(value: str) -> Path | None:
    parsed = urlparse(value)
    if parsed.scheme == "file" and not parsed.netloc:
        return Path(unquote(parsed.path)).expanduser().resolve()
    if not parsed.scheme and value:
        return Path(value).expanduser().resolve()
    return None


def _repo_binding(repo: Path | str, nm_home: Path) -> dict:
    repo_path = Path(repo).expanduser().resolve()
    db_path = nm_home / "state.sqlite"
    if not nm_home.is_dir():
        raise AdapterError(f"no-mistakes home is absent: {nm_home}")
    root_info = nm_home.stat()
    if root_info.st_uid not in {os.getuid(), 0} or root_info.st_mode & 0o022:
        raise AdapterError("no-mistakes home is not owner/root controlled")
    with closing(_db_connect(db_path)) as db:
        _schema(db)
        rows = list(db.execute(
            "SELECT id, working_path, upstream_url, COALESCE(fork_url, '') AS fork_url, default_branch "
            "FROM repos WHERE working_path = ?", (str(repo_path),),
        ))
    if len(rows) != 1:
        raise AdapterError("repository is not uniquely initialized in the pinned no-mistakes database")
    row = dict(rows[0])
    origin = git_config(repo_path, "remote.origin.url")
    gate_remote = git_config(repo_path, "remote.no-mistakes.url")
    if not origin:
        raise AdapterError("repository has no origin URL to bind the external gate")
    if row["upstream_url"].strip() != origin.strip():
        raise AdapterError("no-mistakes upstream URL does not match the registered repository origin")
    # BOSS's GitHub lifecycle currently binds head and base to one exact
    # repository. A distinct fork target would violate that invariant.
    if row["fork_url"].strip() not in {"", origin.strip()}:
        raise AdapterError("no-mistakes fork target differs from origin; this adapter requires one exact GitHub repository")
    expected_gate = (nm_home / "repos" / f"{row['id']}.git").resolve()
    actual_gate = _local_path(gate_remote)
    if actual_gate != expected_gate:
        raise AdapterError("remote.no-mistakes.url is not the gate repository bound by the pinned database")
    push_target = row["fork_url"].strip() or origin
    return {
        "repo_id": row["id"], "working_path": str(repo_path), "origin": origin,
        "default_branch": row["default_branch"], "fork_url": row["fork_url"],
        "push_target_kind": "fork" if row["fork_url"].strip() else "upstream",
        "push_target_fingerprint": _target_fingerprint(push_target),
        "gate_remote": str(expected_gate), "db_path": str(db_path), "nm_home": str(nm_home),
    }


def status(repo: Path | str | None = None, *, binary: Path | str | None = None,
           nm_home: Path | str | None = None) -> dict:
    found = str(binary) if binary is not None else shutil.which("no-mistakes")
    result = {
        "provider": "no-mistakes", "required_version": VERSION,
        "required_tag_sha": TAG_SHA, "required_build_sha": BUILD_SHA,
        "binary": found, "installed": bool(found), "version": None,
        "initialized": False, "binary_provenance_verified": False,
        "repository_binding_verified": False, "adapter_verified": False,
        "ready": False, "reason": "no-mistakes is not installed",
    }
    if not found:
        return result
    attestation = _binary_attestation(found)
    result["binary_attestation"] = attestation
    result["version"] = attestation.get("version")
    result["binary_provenance_verified"] = attestation.get("verified") is True
    if not result["binary_provenance_verified"]:
        result["reason"] = str(attestation.get("reason") or "binary provenance was not verified")
        return result
    if repo is None:
        result["reason"] = "pinned binary verified; a repository is required to verify initialization"
        return result
    try:
        binding = _repo_binding(repo, _home(nm_home))
    except (AdapterError, OSError, sqlite3.Error, BossError) as exc:
        result["reason"] = str(getattr(exc, "msg", None) or exc)
        return result
    result.update(initialized=True, repository_binding_verified=True,
                  adapter_verified=True, ready=True, binding=binding,
                  reason="pinned binary, repository initialization, and exact receipt schema verified")
    return result


def _row(db: sqlite3.Connection, run_id: str) -> dict | None:
    names = sorted(REQUIRED_RUN_COLUMNS)
    row = db.execute(f"SELECT {', '.join(names)} FROM runs WHERE id = ?", (run_id,)).fetchone()
    return dict(row) if row else None


def _candidate_runs(db: sqlite3.Connection, tx: dict) -> list[dict]:
    names = sorted(REQUIRED_RUN_COLUMNS)
    rows = db.execute(
        f"SELECT {', '.join(names)} FROM runs "
        "WHERE repo_id = ? AND branch = ? AND submitted_head_sha = ? "
        "ORDER BY created_at DESC, id DESC",
        (tx["repo_id"], tx["branch"], tx["submitted_head_sha"]),
    )
    prior = set(tx.get("prior_run_ids") or [])
    return [dict(row) for row in rows if row["id"] not in prior]


def _active_gate(db: sqlite3.Connection, run_id: str) -> dict | None:
    rows = list(db.execute(
        "SELECT id, step_name, step_order, status, findings_json FROM step_results "
        "WHERE run_id = ? AND status IN ('awaiting_approval', 'fix_review') "
        "ORDER BY step_order, id", (run_id,),
    ))
    if len(rows) > 1:
        raise AdapterError("no-mistakes exposed multiple simultaneous approval gates")
    if not rows:
        return None
    gate = dict(rows[0])
    raw = gate.pop("findings_json", None)
    try:
        parsed = json.loads(raw) if raw else {"items": []}
    except json.JSONDecodeError as exc:
        raise AdapterError("no-mistakes gate findings are not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise AdapterError("no-mistakes gate findings have an invalid shape")
    items = parsed.get("items") or []
    if not isinstance(items, list) or any(not isinstance(value, dict) for value in items):
        raise AdapterError("no-mistakes gate findings have an invalid item list")
    gate["findings"] = parsed
    gate["fingerprint"] = hashlib.sha256((raw or "").encode()).hexdigest()
    return gate


def _receipt_classification(item: dict, project: dict, tx: dict, run: dict,
                            gate: dict | None) -> dict:
    exact = {
        "repo_id": run.get("repo_id") == tx.get("repo_id"),
        "branch": run.get("branch") == tx.get("branch"),
        "submitted_head_sha": run.get("submitted_head_sha") == tx.get("submitted_head_sha"),
        "base_sha": run.get("base_sha") == tx.get("base_sha"),
        "version": run.get("no_mistakes_version") in {VERSION, f"v{VERSION}"},
        "build_sha": run.get("no_mistakes_build_sha") == BUILD_SHA,
    }
    if not all(exact.values()):
        return {"classification": "invalid", "reason": "external run identity/provenance does not match the journal", "exact": exact}
    if gate:
        return {"classification": "needs-you", "reason": f"no-mistakes is waiting at {gate['step_name']}",
                "exact": exact, "gate": gate}
    if run.get("status") in TERMINAL_FAILURES:
        return {"classification": "failed", "reason": f"no-mistakes ended as {run.get('status')}",
                "exact": exact, "error": run.get("error")}
    final_head = run.get("head_sha")
    if not _full_sha(final_head):
        return {"classification": "unknown", "reason": "external run has no full current head SHA", "exact": exact}
    if final_head != tx.get("submitted_head_sha"):
        return {"classification": "changed-head",
                "reason": "no-mistakes produced a different head; explicit custody sync and fresh BOSS verification are required",
                "exact": exact, "observed_head_sha": final_head}
    reviewed = run.get("review_approved_head_sha") == final_head
    pushed = run.get("last_pushed_sha") == final_head and not bool(run.get("push_active"))
    ci_ready = run.get("ci_ready_at") is not None
    pr_url = str(run.get("pr_url") or "")
    from .forge import _github_repo, _pull
    pull = _pull(pr_url)
    origin_repo = _github_repo(str(tx.get("origin") or ""))
    canonical_pr = bool(pull and origin_repo and pull[0].lower() == origin_repo.lower())
    current_base = git(project["path"], "rev-parse", project["base"], check=False)
    base_stable = current_base == tx.get("base_sha")
    checks = {"reviewed": reviewed, "pushed": pushed, "ci_ready": ci_ready,
              "canonical_pr": canonical_pr, "base_stable": base_stable,
              "pr_open": str(run.get("pr_state") or "").lower() == "open",
              "push_target_kind": run.get("push_target_kind") == tx.get("push_target_kind"),
              "push_target_fingerprint": run.get("push_target_fingerprint") == tx.get("push_target_fingerprint"),
              "push_ref": run.get("push_ref") == f"refs/heads/{tx.get('branch')}"}
    if all(checks.values()) and run.get("status") in {"running", "completed"}:
        return {"classification": "checks-passed", "reason": "pinned exact-SHA no-mistakes receipt is complete",
                "exact": exact, "checks": checks, "pr_url": pr_url, "head_sha": final_head}
    if run.get("status") not in {"pending", "running", "completed"}:
        return {"classification": "unknown", "reason": f"unrecognized no-mistakes status {run.get('status')!r}",
                "exact": exact, "checks": checks}
    return {"classification": "running", "reason": "no-mistakes has not produced a complete exact-SHA receipt",
            "exact": exact, "checks": checks}


def inspect(item: dict, project: dict) -> dict:
    tx = item.get("external_gate") or {}
    if tx.get("provider") != "no-mistakes" or not tx.get("id"):
        return {"provider": "no-mistakes", "classification": "absent",
                "reason": "item has no journaled no-mistakes transaction"}
    try:
        _attestation_still_matches(tx)
        binding = _repo_binding(project["path"], _home(tx.get("nm_home")))
        if (binding.get("repo_id") != tx.get("repo_id") or binding.get("origin") != tx.get("origin")
                or binding.get("db_path") != tx.get("db_path")):
            raise AdapterError("no-mistakes repository binding changed after the transaction armed")
        db_path = Path(str(tx["db_path"]))
        with closing(_db_connect(db_path)) as db:
            _schema(db)
            run = _row(db, str(tx.get("run_id"))) if tx.get("run_id") else None
            candidates = [] if run else _candidate_runs(db, tx)
            if not run and len(candidates) == 1:
                run = candidates[0]
            elif not run and len(candidates) > 1:
                return {"provider": "no-mistakes", "classification": "ambiguous",
                        "reason": "multiple new external runs match the exact submitted head",
                        "candidate_run_ids": [value["id"] for value in candidates]}
            if not run:
                return {"provider": "no-mistakes", "classification": "unknown",
                        "reason": "no authoritative external run is observable; the request will not be replayed"}
            gate = _active_gate(db, run["id"])
    except (BossError, AdapterError, OSError, sqlite3.Error, KeyError) as exc:
        return {"provider": "no-mistakes", "classification": "unknown",
                "reason": str(getattr(exc, "msg", None) or exc)}
    result = _receipt_classification(item, project, tx, run, gate)
    return {"provider": "no-mistakes", "run_id": run["id"], "receipt": run, **result}


def _command_env(nm_home: Path) -> dict[str, str]:
    allowed = {
        "HOME", "PATH", "TMPDIR", "LANG", "LC_ALL", "TERM", "COLORTERM", "NO_COLOR",
        "SSH_AUTH_SOCK", "GH_TOKEN", "GITHUB_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY", "GEMINI_API_KEY", "PI_CODING_AGENT_DIR",
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    }
    env = {key: value for key, value in os.environ.items() if key in allowed}
    env["NM_HOME"] = str(nm_home)
    env.setdefault("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    return env


def _open_private(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(descriptor, "wb")


def invoke(binary: str, args: list[str], *, cwd: Path, nm_home: Path,
           stdout_path: Path, stderr_path: Path, started=None) -> subprocess.CompletedProcess:
    """Run one blocking AXI drive call without a timeout or automatic signal."""
    with _open_private(stdout_path) as stdout, _open_private(stderr_path) as stderr:
        process = subprocess.Popen([binary, "axi", *args], cwd=cwd, env=_command_env(nm_home),
                                   stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                   start_new_session=True)
        callback_error = None
        if started:
            try:
                started(process.pid)
            except BaseException as exc:
                # Once the external process exists, do not abandon it merely
                # because process-identity journaling failed. Wait for it to
                # settle, then propagate uncertainty to the caller.
                callback_error = exc
        returncode = process.wait()
        if callback_error is not None:
            raise callback_error
    return subprocess.CompletedProcess([binary, "axi", *args], returncode, "", "")


def has_open_transaction(item: dict) -> bool:
    tx = item.get("external_gate") or {}
    return tx.get("provider") == "no-mistakes" and tx.get("state") in OPEN_TRANSACTION_STATES


def _project_policy(project: dict) -> dict:
    return {key: project.get(key) for key in (
        "id", "path", "mode", "authority", "base", "test_cmd", "protected_paths", "gate"
    )}


def _prior_run_ids(db_path: Path, binding: dict, branch: str, head_sha: str) -> list[str]:
    with closing(_db_connect(db_path)) as db:
        _schema(db)
        rows = db.execute(
            "SELECT id FROM runs WHERE repo_id = ? AND branch = ? AND submitted_head_sha = ? "
            "ORDER BY created_at DESC, id DESC",
            (binding["repo_id"], branch, head_sha),
        )
        return [str(row[0]) for row in rows]


def _validate_item(item: dict, project: dict, wt: Path, capability: dict) -> dict:
    from . import deliver, modes, worktree

    if project.get("gate") != "no-mistakes":
        raise BossError("item is not configured for the no-mistakes provider")
    if item.get("project") != project.get("id") or item.get("status") != "running":
        raise BossError("item/project state does not permit a no-mistakes submission")
    if project.get("authority", 0) < 2 or project.get("mode") == modes.LOCAL_ONLY:
        raise BossError("no-mistakes can push/open a PR and therefore requires non-local mode with authority >= 2")
    if item.get("kind") != "ship":
        raise BossError("no-mistakes execution is available only for ship items")
    if (any(value is not None for value in (item.get("budgets") or {}).values())
            or bool(item.get("node_budgets"))):
        raise BossError("no-mistakes cannot prove BOSS token/cost/time caps; configured budgets fail closed")
    binding = capability.get("binding") or {}
    if binding.get("default_branch") != project.get("base"):
        raise BossError("no-mistakes default branch does not match the registered BOSS base")
    verification = deliver._verification(item)
    deliver._reviews(item, verification)
    head_sha = item.get("head_sha")
    base_sha = verification.get("base_sha")
    if not (_full_sha(head_sha) and _full_sha(base_sha) and verification.get("ok") is True
            and verification.get("complete") is True and verification.get("head_sha") == head_sha):
        raise BossError("no-mistakes submission requires complete exact-SHA BOSS verification")
    if git(wt, "rev-parse", "HEAD", check=False) != head_sha or not worktree.is_clean(wt):
        raise BossError("no-mistakes submission worktree is not clean at the reviewed exact SHA")
    if git(project["path"], "rev-parse", project["base"], check=False) != base_sha:
        raise BossError("registered base moved before no-mistakes submission")
    if worktree.signature(wt) != verification.get("fingerprint"):
        raise BossError("worktree fingerprint changed before no-mistakes submission")
    return verification


def arm(item: dict, project: dict, wt: Path) -> dict:
    """Durably bind all inputs before an external AXI run can begin."""
    import secrets
    from . import control

    capability = status(project["path"])
    if not capability.get("ready"):
        raise BossError(f"gate provider no-mistakes is unavailable: {capability.get('reason', 'unverified')}")
    verification = _validate_item(item, project, wt, capability)
    binding = capability["binding"]
    binary = capability["binary_attestation"]
    intent = str(control.redact(str(item.get("text") or ""))).strip()
    if not intent:
        raise BossError("no-mistakes submission requires the original task intent")
    transaction_id = secrets.token_hex(16)
    try:
        prior = _prior_run_ids(Path(binding["db_path"]), binding, item["branch"], item["head_sha"])
    except AdapterError as exc:
        raise BossError(str(exc)) from exc
    expected = int(item.get("revision", 0))

    def mutate(current):
        if current.get("status") != "running" or current.get("head_sha") != item.get("head_sha"):
            raise BossError("item changed before the external gate could arm")
        previous = current.get("external_gate") or {}
        if previous.get("state") in OPEN_TRANSACTION_STATES:
            raise BossError("an earlier no-mistakes transaction requires reconciliation")
        if previous:
            current.setdefault("external_gate_history", []).append(previous)
            current["external_gate_history"] = current["external_gate_history"][-20:]
        current.pop("pr_delivery", None)
        current["pr_url"] = None
        current["external_gate"] = {
            "id": transaction_id, "provider": "no-mistakes", "state": "armed",
            "armed_at": now(), "project_id": project["id"],
            "repository_path": str(Path(project["path"]).resolve()),
            "project_policy": _project_policy(project),
            "worktree_path": str(wt.resolve()), "worktree_fingerprint": verification["fingerprint"],
            "branch": item["branch"], "base_ref": project["base"],
            "base_sha": verification["base_sha"], "submitted_head_sha": item["head_sha"],
            "intent": intent, "intent_sha256": hashlib.sha256(intent.encode()).hexdigest(),
            "repo_id": binding["repo_id"], "origin": binding["origin"],
            "push_target_kind": binding["push_target_kind"],
            "push_target_fingerprint": binding["push_target_fingerprint"],
            "gate_remote": binding["gate_remote"], "nm_home": binding["nm_home"],
            "db_path": binding["db_path"], "binary": binary["binary"],
            "binary_attestation": binary, "prior_run_ids": prior, "responses": [],
        }
        current["phase"] = "external-gate-armed"

    return control.cas_update(item["id"], mutate, expected_revision=expected)


def _attestation_still_matches(tx: dict) -> None:
    expected = tx.get("binary_attestation") or {}
    current = _binary_attestation(str(tx.get("binary") or ""))
    if (current.get("verified") is not True or expected.get("verified") is not True
            or current.get("sha256") != expected.get("sha256")
            or current.get("sha256") not in {value["binary_sha256"] for value in MACOS_RELEASES.values()}
            or current.get("build_sha") != BUILD_SHA):
        raise AdapterError("journaled no-mistakes binary provenance is no longer valid")


def _validate_before_command(item: dict, project: dict, *, allow_needs_you: bool = False) -> tuple[dict, Path]:
    from . import registry, worktree

    tx = item.get("external_gate") or {}
    current_project = registry.get(item["project"])
    if _project_policy(current_project) != tx.get("project_policy") or _project_policy(project) != _project_policy(current_project):
        raise BossError("project policy changed after the external gate armed")
    allowed_statuses = {"running", "needs-you"} if allow_needs_you else {"running"}
    if item.get("status") not in allowed_statuses:
        raise BossError("item state no longer permits the external gate command")
    controls = item.get("controls") or {}
    unresolved = [event for event in controls.get("events", [])
                  if event.get("state") in {"pending", "delivering"}]
    if unresolved or controls.get("pause_requested") or controls.get("interrupt_requested"):
        raise BossError("unresolved boss control prevents an external gate command")
    wt = Path(str(tx.get("worktree_path") or "")).resolve()
    expected_wt = Path(str(item.get("worktree") or wt)).resolve()
    if wt != expected_wt or not wt.is_dir():
        raise BossError("journaled external-gate worktree is missing or changed")
    if git(wt, "rev-parse", "HEAD", check=False) != tx.get("submitted_head_sha") or not worktree.is_clean(wt):
        raise BossError("external-gate worktree changed after arming")
    if worktree.signature(wt) != tx.get("worktree_fingerprint"):
        raise BossError("external-gate worktree fingerprint changed after arming")
    if git(project["path"], "rev-parse", project["base"], check=False) != tx.get("base_sha"):
        raise BossError("registered base moved after the external gate armed")
    try:
        _attestation_still_matches(tx)
    except AdapterError as exc:
        raise BossError(str(exc)) from exc
    return tx, wt


def _update(work_id: str, transaction_id: str, expected_states: set[str], **changes) -> dict:
    from . import control, work

    item = work.load(work_id)
    def mutate(current):
        tx = current.get("external_gate") or {}
        if tx.get("id") != transaction_id or tx.get("state") not in expected_states:
            raise BossError("external gate journal changed during reconciliation")
        tx.update(changes)
    return control.cas_update(work_id, mutate, expected_revision=int(item.get("revision", 0)))


def _tail(path: Path, limit: int = 4000) -> str:
    from .control import redact
    try:
        with path.open("rb") as source:
            source.seek(0, os.SEEK_END)
            size = source.tell(); source.seek(max(0, size - limit))
            value = source.read(limit).decode("utf-8", "replace")
    except OSError as exc:
        return f"<{type(exc).__name__} reading command evidence>"
    return str(redact(value))


def _invoke_journaled(item: dict, project: dict, args: list[str], *, response_id: str | None = None) -> dict:
    from . import control, processes, work

    tx, wt = _validate_before_command(item, project, allow_needs_you=bool(response_id))
    transaction_id = tx["id"]
    evidence_dir = work.item_dir(item["id"]) / "external-gate"
    stem = response_id or "run"
    stdout_path = evidence_dir / f"{transaction_id}-{stem}.stdout"
    stderr_path = evidence_dir / f"{transaction_id}-{stem}.stderr"
    requested_state = "response-requested" if response_id else "request-started"
    expected_state = {"response-armed"} if response_id else {"armed"}
    current = work.load(item["id"])
    def mark_requested(value):
        active = value.get("external_gate") or {}
        if active.get("id") != transaction_id or active.get("state") not in expected_state:
            raise BossError("external gate journal changed before the CLI request could start")
        active.update(state=requested_state, requested_at=now(),
                      stdout_path=str(stdout_path), stderr_path=str(stderr_path))
        if response_id:
            response = next((entry for entry in active.get("responses") or []
                             if entry.get("id") == response_id), None)
            if not response or response.get("state") != "armed":
                raise BossError("external gate response journal changed before the CLI request could start")
            response.update(state="requested", requested_at=now())
    item = control.cas_update(item["id"], mark_requested,
                              expected_revision=int(current.get("revision", 0)))

    def started(pid: int) -> None:
        identity = processes.capture(pid, f"no-mistakes:{item['id']}")
        current = work.load(item["id"])
        def record(value):
            active = value.get("external_gate") or {}
            if active.get("id") != transaction_id or active.get("state") != requested_state:
                raise BossError("external gate journal changed after CLI start")
            active["cli_process"] = {"pid": pid, "identity": identity, "started_at": now()}
        control.cas_update(item["id"], record, expected_revision=int(current.get("revision", 0)))

    completed = invoke(tx["binary"], args, cwd=wt, nm_home=Path(tx["nm_home"]),
                       stdout_path=stdout_path, stderr_path=stderr_path, started=started)
    current = work.load(item["id"])
    def record_result(value):
        active = value.get("external_gate") or {}
        if active.get("id") != transaction_id or active.get("state") != requested_state:
            raise BossError("external gate journal changed before CLI result recording")
        active.update(state="command-returned", command_returned_at=now(),
                      command_exit=completed.returncode,
                      stdout_sha256=_sha256(stdout_path), stderr_sha256=_sha256(stderr_path),
                      stdout_tail=_tail(stdout_path), stderr_tail=_tail(stderr_path))
        if response_id:
            for response in active.get("responses") or []:
                if response.get("id") == response_id:
                    response.update(state="command-returned", command_exit=completed.returncode,
                                    returned_at=now())
                    break
    return control.cas_update(item["id"], record_result,
                              expected_revision=int(current.get("revision", 0)))


def _ask_for(result: dict) -> dict:
    classification = result.get("classification")
    if classification == "needs-you":
        gate = result.get("gate") or {}
        return {
            "question": f"no-mistakes is waiting for your decision at {gate.get('step_name', 'a gate')}.",
            "context": result.get("reason"), "provider": "no-mistakes",
            "run_id": result.get("run_id"), "gate": gate,
            "actions": ["approve", "fix", "skip"],
        }
    return {
        "question": "The no-mistakes transaction needs explicit reconciliation.",
        "context": result.get("reason"), "provider": "no-mistakes",
        "run_id": result.get("run_id"), "classification": classification,
    }


def reconcile(work_id: str) -> dict:
    """Reconcile only from durable external evidence; never issue a command."""
    from . import control, registry, work

    item = work.load(work_id); project = registry.get(item["project"])
    tx = item.get("external_gate") or {}
    if tx.get("provider") != "no-mistakes":
        raise BossError("item has no no-mistakes transaction to reconcile")
    result = inspect(item, project)
    transaction_id = tx.get("id")
    current = work.load(work_id)
    previous_response = ((current.get("external_gate") or {}).get("responses") or [])[-1:]
    if (previous_response and previous_response[0].get("state") in {"requested", "command-returned"}
            and result.get("classification") == "needs-you"
            and (result.get("gate") or {}).get("fingerprint") == previous_response[0].get("gate_fingerprint")):
        result = {**result, "classification": "unknown",
                  "reason": "the response request returned but the same external gate remains; it will not be replayed"}

    def record(value):
        active = value.get("external_gate") or {}
        if active.get("id") != transaction_id:
            raise BossError("external gate identity changed during receipt reconciliation")
        if result.get("run_id"):
            if active.get("run_id") not in {None, result["run_id"]}:
                raise BossError("external gate run identity changed during reconciliation")
            active["run_id"] = result["run_id"]
        active["last_observation"] = result
        active["observed_at"] = now()
        classification = result.get("classification")
        active["state"] = ({"checks-passed": "complete", "needs-you": "needs-decision"}
                           .get(classification, "running" if classification == "running" else "unknown"))
        responses = active.get("responses") or []
        if responses and responses[-1].get("state") in {"requested", "command-returned"} and classification != "unknown":
            responses[-1]["state"] = "observed"
            responses[-1]["observed_at"] = now()
        if classification == "checks-passed":
            value["pr_url"] = result["pr_url"]
            value["pr_delivery"] = {
                "id": active["id"], "state": "complete", "provider": "no-mistakes",
                "completed_at": now(), "head_sha": active["submitted_head_sha"],
                "base_sha": active["base_sha"], "branch": active["branch"],
                "gate_provider": "no-mistakes", "pr_url": result["pr_url"],
                "pr_receipt": result,
            }
            value["ask"] = None
            value.setdefault("controls", {})["paused"] = False
            value.setdefault("history", []).append({"at": now(), "from": value.get("status"),
                                                       "to": "pr-open", "note": result["pr_url"]})
            value["status"] = "pr-open"; value["phase"] = "pr-open"
            value["activity"] = {"last": now(), "state": "pr-open"}
        else:
            value["ask"] = _ask_for(result)
            value.setdefault("controls", {})["paused"] = True
            if value.get("status") != "needs-you":
                value.setdefault("history", []).append({"at": now(), "from": value.get("status"),
                                                           "to": "needs-you", "note": result.get("reason", "")[:500]})
            value["status"] = "needs-you"; value["phase"] = "external-gate-decision"
            value["activity"] = {"last": now(), "state": "needs-you"}

    updated = control.cas_update(work_id, record, expected_revision=int(current.get("revision", 0)))
    try:
        from . import supervisor
        supervisor.observe(updated)
    except BaseException:
        pass
    return updated


def start_or_reconcile(item: dict, project: dict, wt: Path) -> dict:
    """Start once from ``armed``; every other state is observation-only."""
    current = item
    tx = current.get("external_gate") or {}
    verification = (current.get("verification") or [{}])[-1]
    matches_current = (
        tx.get("provider") == "no-mistakes"
        and tx.get("submitted_head_sha") == current.get("head_sha")
        and tx.get("base_sha") == verification.get("base_sha")
        and tx.get("worktree_fingerprint") == verification.get("fingerprint")
    )
    if tx.get("provider") == "no-mistakes" and not matches_current and tx.get("state") in OPEN_TRANSACTION_STATES:
        raise BossError("an unresolved no-mistakes transaction is bound to an older item revision")
    if not matches_current:
        current = arm(current, project, wt); tx = current["external_gate"]
    if tx.get("state") == "complete":
        return reconcile(current["id"])
    if tx.get("state") != "armed":
        return reconcile(current["id"])
    current = _invoke_journaled(current, project, ["run", "--intent", tx["intent"]])
    return reconcile(current["id"])


def respond(work_id: str, action: str, findings: list[str] | None = None,
            instructions: str | None = None) -> dict:
    """Journal and issue one explicit response to the exact observed gate."""
    import secrets
    from . import control, registry, work

    if action not in {"approve", "fix", "skip"}:
        raise BossError("external gate action must be approve, fix, or skip")
    findings = [value.strip() for value in (findings or []) if value.strip()]
    item = work.load(work_id); project = registry.get(item["project"])
    tx = item.get("external_gate") or {}
    if item.get("status") != "needs-you" or tx.get("state") != "needs-decision":
        raise BossError("item is not waiting at a reconciled no-mistakes decision gate")
    observation = inspect(item, project)
    gate = observation.get("gate") or {}
    if observation.get("classification") != "needs-you" or not gate.get("fingerprint"):
        raise BossError("the journaled external gate is no longer authoritatively observable")
    available = {str(value.get("id")) for value in (gate.get("findings") or {}).get("items", [])
                 if value.get("id") is not None}
    if action == "fix" and (not findings or not set(findings) <= available):
        raise BossError("fix requires a non-empty subset of the exact observed finding IDs")
    if action != "fix" and (findings or instructions):
        raise BossError("finding IDs and instructions are valid only with action=fix")
    response_id = secrets.token_hex(16)
    expected = int(item.get("revision", 0))
    def arm_response(current):
        active = current.get("external_gate") or {}
        if active.get("id") != tx.get("id") or active.get("state") != "needs-decision":
            raise BossError("external gate changed before the response could arm")
        response = {
            "id": response_id, "state": "armed", "armed_at": now(), "action": action,
            "findings": findings, "instructions": str(control.redact(instructions or "")),
            "gate_step_id": gate.get("id"), "gate_fingerprint": gate["fingerprint"],
        }
        active.setdefault("responses", []).append(response)
        active["state"] = "response-armed"
    item = control.cas_update(work_id, arm_response, expected_revision=expected)
    # Pin the exact observed step. Omitting --step would let an external race
    # answer whichever gate happened to be current when the CLI connected.
    args = ["respond", "--step", str(gate.get("step_name") or ""), "--action", action]
    if findings:
        args += ["--findings", ",".join(findings)]
    if instructions:
        args += ["--instructions", instructions]
    item = _invoke_journaled(item, project, args, response_id=response_id)
    return reconcile(item["id"])


def require_receipt(item: dict, project: dict) -> dict:
    result = inspect(item, project)
    if result.get("classification") != "checks-passed":
        raise BossError(f"no-mistakes exact-SHA receipt is not complete: {result.get('reason', 'unknown')}")
    tx = item.get("external_gate") or {}
    if (tx.get("state") != "complete" or item.get("head_sha") != result.get("head_sha")
            or item.get("pr_url") != result.get("pr_url")):
        raise BossError("no-mistakes receipt is not durably bound to the current item")
    return result
