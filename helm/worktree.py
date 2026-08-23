"""One persistent git worktree per item. Never touches the captain's checkout branch."""
from __future__ import annotations
import hashlib
import json
import os
import re
import secrets
import shutil
import stat as statmod
import subprocess
import time
from pathlib import Path
from .paths import worktree_root, home
from .util import (git, git_raw_bytes, locked, now, private_mkdir, sh,
                   write_json, HelmError)
from . import ids


RECOVERY_VERSION = 1
RECOVERY_STATES = {"planned", "source-preserved", "registered",
                   "payload-restored", "complete"}
_SHA_PATTERN = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_TX_PATTERN = re.compile(r"[0-9a-f]{32}")
_MAX_MANIFEST_ENTRIES = 1_000_000
_MAX_MANIFEST_BYTES = 100 * 1024 * 1024 * 1024


def branch_name(work_id: str) -> str:
    return f"firstmate/{ids.work(work_id)}"


def _hash_fields(digest, *fields: bytes) -> None:
    for field in fields:
        digest.update(len(field).to_bytes(8, "big")); digest.update(field)


def _stable_stat(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns)


def _xattrs(path: Path) -> list[tuple[bytes, bytes]]:
    if not hasattr(os, "listxattr"):
        return []
    try:
        names = sorted(os.listxattr(path, follow_symlinks=False), key=os.fsencode)
        return [(os.fsencode(name), os.getxattr(path, name, follow_symlinks=False))
                for name in names]
    except (OSError, NotImplementedError) as exc:
        raise HelmError(f"worktree metadata cannot be preserved safely: {type(exc).__name__}")


def _tree_manifest(path: Path, *, contents: bool) -> dict:
    """Hash exact payload bytes/types/modes/links without following symlinks."""
    root = Path(path)
    digest = hashlib.sha256(b"firstmate-worktree-manifest-v1\0")
    totals = {"entries": 0, "bytes": 0}

    def visit(candidate: Path, relative: bytes) -> None:
        try:
            before = os.lstat(candidate)
        except OSError as exc:
            raise HelmError(f"worktree payload changed during inspection: {type(exc).__name__}")
        if before.st_uid != os.getuid():
            raise HelmError("worktree payload contains content not owned by the current user")
        totals["entries"] += 1
        if totals["entries"] > _MAX_MANIFEST_ENTRIES:
            raise HelmError("worktree payload exceeds the bounded recovery manifest entry limit")
        mode = statmod.S_IMODE(before.st_mode).to_bytes(4, "big")
        if statmod.S_ISDIR(before.st_mode):
            _hash_fields(digest, relative, b"directory", mode)
            for name, value in _xattrs(candidate):
                _hash_fields(digest, b"xattr", name, value)
            try:
                with os.scandir(candidate) as scan:
                    entries = sorted(scan, key=lambda value: os.fsencode(value.name))
                for entry in entries:
                    child_relative = (relative + b"/" if relative else b"") + os.fsencode(entry.name)
                    visit(candidate / entry.name, child_relative)
            except OSError as exc:
                raise HelmError(f"worktree directory cannot be inspected: {type(exc).__name__}")
        elif statmod.S_ISREG(before.st_mode):
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(candidate, flags)
            except OSError as exc:
                raise HelmError(f"worktree file cannot be opened safely: {type(exc).__name__}")
            file_digest = hashlib.sha256()
            try:
                opened = os.fstat(descriptor)
                if _stable_stat(opened) != _stable_stat(before) or not statmod.S_ISREG(opened.st_mode):
                    raise HelmError("worktree file identity changed during inspection")
                while True:
                    chunk = os.read(descriptor, 1024 * 1024)
                    if not chunk: break
                    file_digest.update(chunk); totals["bytes"] += len(chunk)
                    if totals["bytes"] > _MAX_MANIFEST_BYTES:
                        raise HelmError("worktree payload exceeds the bounded recovery byte limit")
                after = os.fstat(descriptor)
                if _stable_stat(after) != _stable_stat(opened):
                    raise HelmError("worktree file changed during inspection")
            finally:
                os.close(descriptor)
            _hash_fields(digest, relative, b"file", mode,
                         before.st_size.to_bytes(8, "big"), file_digest.digest())
            for name, value in _xattrs(candidate):
                _hash_fields(digest, b"xattr", name, value)
        elif statmod.S_ISLNK(before.st_mode):
            try:
                target = os.fsencode(os.readlink(candidate))
            except OSError as exc:
                raise HelmError(f"worktree link cannot be inspected: {type(exc).__name__}")
            _hash_fields(digest, relative, b"symlink", mode, target)
            for name, value in _xattrs(candidate):
                _hash_fields(digest, b"xattr", name, value)
        else:
            raise HelmError("worktree payload contains a socket, device, FIFO, or other unsupported object")
        try:
            after_path = os.lstat(candidate)
        except OSError:
            raise HelmError("worktree payload disappeared during inspection")
        if _stable_stat(after_path) != _stable_stat(before):
            raise HelmError("worktree payload changed during inspection")

    try:
        root_before = os.lstat(root)
    except OSError as exc:
        raise HelmError(f"worktree root cannot be inspected: {type(exc).__name__}")
    if root_before.st_uid != os.getuid() or (contents and not statmod.S_ISDIR(root_before.st_mode)):
        raise HelmError("worktree manifest root is not an owned object of the required kind")
    if contents:
        try:
            with os.scandir(root) as scan:
                roots = sorted(scan, key=lambda value: os.fsencode(value.name))
            for entry in roots:
                if entry.name == ".git":
                    continue
                visit(root / entry.name, os.fsencode(entry.name))
        except OSError as exc:
            raise HelmError(f"worktree root cannot be enumerated: {type(exc).__name__}")
    else:
        visit(root, b".")
    try:
        root_after = os.lstat(root)
    except OSError:
        raise HelmError("worktree root disappeared during inspection")
    if _stable_stat(root_after) != _stable_stat(root_before):
        raise HelmError("worktree root changed during inspection")
    return {"version": 1, "sha256": digest.hexdigest(), **totals}


def payload_manifest(path: Path) -> dict:
    return _tree_manifest(path, contents=True)


def entry_manifest(path: Path) -> dict:
    return _tree_manifest(path, contents=False)


def _lexists(path: Path) -> bool:
    return os.path.lexists(os.fspath(path))


def _owned_directory(path: Path) -> bool:
    try: info = os.lstat(path)
    except OSError: return False
    return (statmod.S_ISDIR(info.st_mode) and not statmod.S_ISLNK(info.st_mode)
            and info.st_uid == os.getuid() and not info.st_mode & 0o022)


def _owned_state_chain(path: Path, *, require_leaf: bool) -> bool:
    base = home()
    try: relative = path.relative_to(base)
    except ValueError: return False
    cursor = base
    for component in relative.parts:
        cursor /= component
        if not _lexists(cursor):
            return not require_leaf
        if not _owned_directory(cursor): return False
    return not require_leaf or _owned_directory(path)


def canonical_path_safety(project_id: str, work_id: str, *, allow_missing_leaf: bool = False) -> tuple[bool, str]:
    """Validate every controller-owned component lexically, never through a link."""
    project_id = ids.project(project_id); work_id = ids.work(work_id)
    canonical = worktree_root() / project_id / work_id
    for directory in (worktree_root(), canonical.parent):
        if not _owned_directory(directory):
            return False, f"controller-owned worktree component is missing, linked, unowned, or writable by another user: {directory}"
    if not _lexists(canonical):
        return (True, "canonical leaf is absent") if allow_missing_leaf else (False, "canonical worktree is absent")
    if not _owned_directory(canonical):
        return False, "canonical worktree is linked, unowned, non-directory, or writable by another user"
    return True, "canonical worktree path is owner-controlled"


def root_path_safety() -> tuple[bool, str]:
    root = worktree_root()
    if not _lexists(root): return True, "worktree root is not initialized"
    if not _owned_directory(root):
        return False, "controller worktree root is linked, unowned, non-directory, or writable by another user"
    return True, "controller worktree root is owner-controlled"


def _read_gitfile(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise HelmError(f"detached worktree Git file is unsafe: {type(exc).__name__}")
    try:
        info = os.fstat(descriptor)
        if (not statmod.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o022 or info.st_size > 4096):
            raise HelmError("detached worktree Git file is not an owned integrity file")
        value = os.read(descriptor, 4097)
        if len(value) > 4096:
            raise HelmError("detached worktree Git file is unexpectedly large")
        return value
    finally:
        os.close(descriptor)


def detached_registration_evidence(project: dict, work_id: str, path: Path) -> dict:
    """Prove a canonical worktree remains intact after its Git admin entry vanished."""
    project_id = ids.project(project["id"]); work_id = ids.work(work_id)
    canonical = worktree_root() / project_id / work_id
    supplied = Path(os.path.abspath(os.path.expanduser(os.fspath(path))))
    if supplied != canonical:
        raise HelmError("detached worktree path is not the exact controller-owned canonical path")
    safe, reason = canonical_path_safety(project_id, work_id)
    if not safe: raise HelmError(reason)
    gitfile = canonical / ".git"
    raw = _read_gitfile(gitfile)
    if not raw.startswith(b"gitdir: ") or raw.count(b"\n") > 1 or b"\0" in raw:
        raise HelmError("detached worktree Git file is malformed")
    raw_target = os.fsdecode(raw[len(b"gitdir: "):].rstrip(b"\n"))
    if not raw_target:
        raise HelmError("detached worktree Git file has no admin target")
    target = Path(raw_target)
    if not target.is_absolute(): target = gitfile.parent / target
    target = Path(os.path.abspath(os.fspath(target)))
    repo = Path(project["path"]).expanduser().resolve(strict=True)
    common_raw = git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    common = Path(common_raw).expanduser().resolve(strict=True)
    if target.parent.resolve(strict=False) != (common / "worktrees").resolve(strict=False):
        raise HelmError("detached worktree Git file does not target this project's worktree metadata")
    if _lexists(target):
        raise HelmError("worktree admin metadata still exists; registration loss is not proven")
    branch = branch_name(work_id); ref = f"refs/heads/{branch}"
    sha = git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}", check=False)
    if not _SHA_PATTERN.fullmatch(sha):
        raise HelmError("detached worktree recovery branch is missing or not a commit")
    registered = branch_worktrees(project, branch)
    if registered:
        raise HelmError("the recovery branch is already registered to another worktree")
    root_info = os.lstat(canonical)
    source_manifest = entry_manifest(canonical)
    payload = payload_manifest(canonical)
    return {
        "version": 1, "project_id": project_id, "work_id": work_id,
        "canonical": str(canonical), "branch": branch, "branch_ref": ref,
        "branch_sha": sha, "missing_gitdir": str(target),
        "gitfile_sha256": hashlib.sha256(raw).hexdigest(),
        "source_inode": root_info.st_ino, "source_device": root_info.st_dev,
        "source_manifest": source_manifest, "payload_manifest": payload,
    }


def recovery_journal_path(project_id: str, work_id: str) -> Path:
    return home() / "recovery" / "worktrees" / ids.project(project_id) / f"{ids.work(work_id)}.json"


def _read_journal_bytes(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try: descriptor = os.open(path, flags)
    except FileNotFoundError: raise
    except OSError as exc: raise HelmError(f"worktree recovery journal is unsafe: {type(exc).__name__}")
    try:
        info = os.fstat(descriptor)
        if (not statmod.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o022 or info.st_size > 2_000_000):
            raise HelmError("worktree recovery journal is not an owned bounded integrity file")
        chunks, remaining = [], info.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk: raise HelmError("worktree recovery journal was truncated during inspection")
            chunks.append(chunk); remaining -= len(chunk)
        if os.read(descriptor, 1): raise HelmError("worktree recovery journal grew during inspection")
        after = os.fstat(descriptor)
        if _stable_stat(after) != _stable_stat(info):
            raise HelmError("worktree recovery journal changed during inspection")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _journal_digest(path: Path) -> str | None:
    try: return hashlib.sha256(_read_journal_bytes(path)).hexdigest()
    except FileNotFoundError: return None


def _valid_manifest(value) -> bool:
    return (isinstance(value, dict) and value.get("version") == 1
            and isinstance(value.get("sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", value["sha256"]) is not None
            and isinstance(value.get("entries"), int) and not isinstance(value.get("entries"), bool)
            and 0 <= value["entries"] <= _MAX_MANIFEST_ENTRIES
            and isinstance(value.get("bytes"), int) and not isinstance(value.get("bytes"), bool)
            and 0 <= value["bytes"] <= _MAX_MANIFEST_BYTES)


def _recovery_paths(project_id: str, work_id: str, tx_id: str) -> dict[str, Path]:
    ids.project(project_id); ids.work(work_id)
    if not _TX_PATTERN.fullmatch(str(tx_id)):
        raise HelmError("worktree recovery transaction identity is invalid")
    root = home() / "quarantine" / "worktree-recovery" / project_id / f"{work_id}-{tx_id}"
    return {"root": root, "source": root / "source", "stage": root / "stage",
            "scratch": root / "scratch", "canonical": worktree_root() / project_id / work_id}


def _validate_transaction(value: object, project_id: str, work_id: str) -> str | None:
    if not isinstance(value, dict): return "current transaction is not an object"
    if not _TX_PATTERN.fullmatch(str(value.get("id") or "")): return "transaction id is invalid"
    if value.get("state") not in RECOVERY_STATES: return "transaction state is invalid"
    if value.get("project_id") != project_id or value.get("work_id") != work_id:
        return "transaction ownership does not match its journal"
    if value.get("branch") != branch_name(work_id) or value.get("branch_ref") != f"refs/heads/{branch_name(work_id)}":
        return "transaction branch binding is invalid"
    if not _SHA_PATTERN.fullmatch(str(value.get("branch_sha") or "")):
        return "transaction branch SHA is invalid"
    if not re.fullmatch(r"[0-9a-f]{64}", str(value.get("gitfile_sha256") or "")):
        return "transaction Git-file receipt is invalid"
    if not _valid_manifest(value.get("source_manifest")) or not _valid_manifest(value.get("payload_manifest")):
        return "transaction manifest receipt is invalid"
    for key in ("source_inode", "source_device", "item_revision"):
        if not isinstance(value.get(key), int) or isinstance(value.get(key), bool) or value[key] < 0:
            return f"transaction {key} is invalid"
    paths = _recovery_paths(project_id, work_id, value["id"])
    if value.get("canonical") != str(paths["canonical"]): return "transaction canonical path is invalid"
    return None


def registration_recovery_status(project_id: str, work_id: str) -> dict:
    """Read one recovery journal without creating or normalizing any state."""
    project_id = ids.project(project_id); work_id = ids.work(work_id)
    path = recovery_journal_path(project_id, work_id)
    try:
        raw = _read_journal_bytes(path); value = json.loads(raw)
    except FileNotFoundError:
        return {"state": "none", "path": str(path), "digest": None}
    except HelmError as exc:
        return {"state": "corrupt", "path": str(path), "digest": None,
                "reason": exc.msg}
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {"state": "corrupt", "path": str(path), "digest": hashlib.sha256(raw).hexdigest(),
                "reason": f"journal is unreadable: {type(exc).__name__}"}
    if (not isinstance(value, dict) or value.get("version") != RECOVERY_VERSION
            or value.get("project_id") != project_id or value.get("work_id") != work_id
            or not isinstance(value.get("history", []), list)
            or len(value.get("history", [])) > 20):
        return {"state": "corrupt", "path": str(path), "digest": hashlib.sha256(raw).hexdigest(),
                "reason": "journal envelope is invalid"}
    current = value.get("current")
    error = _validate_transaction(current, project_id, work_id)
    if error:
        return {"state": "corrupt", "path": str(path), "digest": hashlib.sha256(raw).hexdigest(),
                "reason": error}
    paths = _recovery_paths(project_id, work_id, current["id"])
    return {"state": current["state"], "path": str(path),
            "digest": hashlib.sha256(raw).hexdigest(), "record": value,
            "transaction": current, "paths": {key: str(value) for key, value in paths.items()}}


def _write_phase(path: Path, record: dict, state: str) -> None:
    if state not in RECOVERY_STATES: raise HelmError("invalid worktree recovery phase")
    record["current"]["state"] = state; record["current"]["updated"] = now()
    write_json(path, record)


def _recovery_checkpoint(_phase: str, _transaction: dict) -> None:
    """Deterministic crash-injection seam; production intentionally does nothing."""


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try: os.fsync(descriptor)
    finally: os.close(descriptor)


def _fsync_payload(path: Path) -> None:
    info = os.lstat(path)
    if statmod.S_ISLNK(info.st_mode): return
    if statmod.S_ISREG(info.st_mode):
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                             | getattr(os, "O_NOFOLLOW", 0))
        try: os.fsync(descriptor)
        finally: os.close(descriptor)
        return
    if not statmod.S_ISDIR(info.st_mode):
        raise HelmError("recovery copy produced an unsupported filesystem object")
    with os.scandir(path) as entries:
        children = [path / entry.name for entry in entries]
    for child in children: _fsync_payload(child)
    _fsync_directory(path)


def _copy_entry(source: Path, destination: Path) -> None:
    info = os.lstat(source)
    if statmod.S_ISDIR(info.st_mode):
        shutil.copytree(source, destination, symlinks=True, copy_function=shutil.copy2)
    elif statmod.S_ISREG(info.st_mode):
        shutil.copy2(source, destination, follow_symlinks=False)
    elif statmod.S_ISLNK(info.st_mode):
        os.symlink(os.readlink(source), destination,
                   target_is_directory=source.is_dir())
        try: shutil.copystat(source, destination, follow_symlinks=False)
        except NotImplementedError: pass
    else:
        raise HelmError("recovery source contains an unsupported filesystem object")
    _fsync_payload(destination)


def _restore_payload(source: Path, stage: Path, scratch: Path, tx_id: str) -> None:
    private_mkdir(scratch)
    with os.scandir(source) as source_scan:
        source_entries = sorted((entry.name for entry in source_scan if entry.name != ".git"),
                                key=os.fsencode)
    with os.scandir(stage) as stage_scan:
        stage_entries = {entry.name for entry in stage_scan if entry.name != ".git"}
    unexpected = stage_entries.difference(source_entries)
    if unexpected:
        raise HelmError("recovery staging worktree contains unexpected content; preserving it for inspection")
    for index, name in enumerate(source_entries):
        source_entry, target = source / name, stage / name
        expected = entry_manifest(source_entry)
        if _lexists(target):
            if entry_manifest(target) != expected:
                raise HelmError("recovery staging content changed; preserving every copy for inspection")
            continue
        inflight = scratch / f"inflight-{index:08d}-{tx_id}"
        if _lexists(inflight) and entry_manifest(inflight) != expected:
            partial = scratch / f"partial-{index:08d}-{time.time_ns()}"
            os.replace(inflight, partial); _fsync_directory(scratch)
        if not _lexists(inflight):
            _copy_entry(source_entry, inflight)
        if entry_manifest(inflight) != expected:
            raise HelmError("recovery copy could not be verified; the original remains preserved")
        os.replace(inflight, target)
        _fsync_directory(stage); _fsync_directory(scratch)


def _exact_ref(project: dict, transaction: dict) -> bool:
    repo = Path(project["path"])
    actual = git(repo, "rev-parse", "--verify", f"{transaction['branch_ref']}^{{commit}}", check=False)
    return actual == transaction["branch_sha"]


def _assert_attached(project: dict, transaction: dict, path: Path) -> None:
    branch = git(path, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    head = git(path, "rev-parse", "--verify", "HEAD", check=False)
    registered = branch_worktrees(project, transaction["branch"])
    if branch != transaction["branch"] or head != transaction["branch_sha"] or registered != [path.resolve()]:
        raise HelmError("worktree recovery registration or exact branch SHA changed")


def _matches_transaction_evidence(actual: dict, transaction: dict) -> bool:
    keys = ("project_id", "work_id", "canonical", "branch", "branch_ref", "branch_sha",
            "missing_gitdir", "gitfile_sha256", "source_inode", "source_device",
            "source_manifest", "payload_manifest")
    return all(actual.get(key) == transaction.get(key) for key in keys)


def begin_registration_recovery(project: dict, item: dict, evidence: dict,
                                *, expected_journal_digest: str | None) -> dict:
    """Start, durably journal, and run one explicitly confirmed reconstruction."""
    project_id = ids.project(project["id"]); work_id = ids.work(item["id"])
    journal = recovery_journal_path(project_id, work_id)
    with locked(home() / "recovery" / "worktrees.lock"):
        if _journal_digest(journal) != expected_journal_digest:
            raise HelmError("worktree recovery journal changed before confirmation")
        status = registration_recovery_status(project_id, work_id)
        if status["state"] == "corrupt": raise HelmError(status["reason"])
        if status["state"] not in {"none", "complete"}:
            raise HelmError("an incomplete worktree recovery must be resumed, not replaced")
        actual = detached_registration_evidence(project, work_id, Path(evidence["canonical"]))
        if actual != evidence:
            raise HelmError("detached worktree evidence changed before confirmation")
        tx_id = secrets.token_hex(16)
        transaction = {**actual, "id": tx_id, "state": "planned", "created": now(),
                       "updated": now(), "item_revision": int(item.get("revision", 0)),
                       "item_status": item.get("status")}
        history = [] if status["state"] == "none" else [*status["record"].get("history", []),
                                                         status["transaction"]][-20:]
        record = {"version": RECOVERY_VERSION, "project_id": project_id,
                  "work_id": work_id, "current": transaction, "history": history}
        write_json(journal, record)
        _recovery_checkpoint("journaled", transaction)
        return _resume_registration_locked(project, record, journal)


def resume_registration_recovery(project: dict, work_id: str,
                                 *, expected_journal_digest: str) -> dict:
    project_id = ids.project(project["id"]); work_id = ids.work(work_id)
    journal = recovery_journal_path(project_id, work_id)
    with locked(home() / "recovery" / "worktrees.lock"):
        if _journal_digest(journal) != expected_journal_digest:
            raise HelmError("worktree recovery journal changed before confirmation")
        status = registration_recovery_status(project_id, work_id)
        if status["state"] in {"none", "corrupt"}:
            raise HelmError(status.get("reason", "worktree recovery journal is absent"))
        return _resume_registration_locked(project, status["record"], journal)


def _resume_registration_locked(project: dict, record: dict, journal: Path) -> dict:
    transaction = record["current"]
    project_id, work_id = transaction["project_id"], transaction["work_id"]
    paths = _recovery_paths(project_id, work_id, transaction["id"])
    canonical, source, stage, scratch = (paths[key] for key in ("canonical", "source", "stage", "scratch"))
    canonical_safe, canonical_reason = canonical_path_safety(
        project_id, work_id, allow_missing_leaf=True)
    if not canonical_safe: raise HelmError(canonical_reason)
    for candidate in (paths["root"], source, stage, scratch):
        if _lexists(candidate) and not _owned_state_chain(candidate, require_leaf=True):
            raise HelmError("recovery path is linked, unowned, or writable by another user")
    if not _exact_ref(project, transaction):
        raise HelmError("recovery branch moved; every preserved copy was left untouched")

    # A crash after the final Git move but before its journal write is inferred
    # only from exact attachment, exact SHA, exact payload, and retained source.
    if canonical.is_dir() and source.is_dir():
        try:
            _assert_attached(project, transaction, canonical)
        except HelmError:
            pass
        else:
            if (entry_manifest(source) != transaction["source_manifest"]
                    or payload_manifest(canonical) != transaction["payload_manifest"]):
                raise HelmError("reattached worktree or preserved source changed; recovery stopped")
            _write_phase(journal, record, "complete")
            return {"state": "complete", "canonical": str(canonical),
                    "preserved_source": str(source), "branch_sha": transaction["branch_sha"],
                    "staging_metadata": "original Git index metadata was absent and remains unrecoverable"}

    if not source.is_dir():
        if not canonical.is_dir():
            raise HelmError("both canonical and preserved recovery source are absent")
        actual = detached_registration_evidence(project, work_id, canonical)
        if not _matches_transaction_evidence(actual, transaction):
            raise HelmError("detached worktree changed after recovery was journaled")
        private_mkdir(paths["root"])
        if any(_lexists(paths[key]) for key in ("source", "stage", "scratch")):
            raise HelmError("recovery destination collision; canonical worktree remains untouched")
        os.replace(canonical, source)
        _fsync_directory(source.parent); _fsync_directory(canonical.parent)
    if (entry_manifest(source) != transaction["source_manifest"]
            or os.lstat(source).st_ino != transaction["source_inode"]
            or os.lstat(source).st_dev != transaction["source_device"]):
        raise HelmError("preserved recovery source failed its exact manifest or inode receipt")
    _write_phase(journal, record, "source-preserved")
    _recovery_checkpoint("source-preserved", transaction)

    if not stage.is_dir():
        if _lexists(stage): raise HelmError("recovery staging path is not a directory")
        if branch_worktrees(project, transaction["branch"]):
            raise HelmError("recovery branch became registered elsewhere")
        result = sh(["git", "-C", str(project["path"]), "worktree", "add", "--no-checkout",
                     "--quiet", str(stage), transaction["branch"]], check=False,
                    write_paths=(stage.parent,))
        if result.returncode:
            raise HelmError("Git refused the preserved worktree reconstruction")
    _assert_attached(project, transaction, stage)
    read_tree = sh(["git", "-C", str(stage), "read-tree", transaction["branch_sha"]],
                   check=False, write_paths=(stage,))
    if read_tree.returncode or not _exact_ref(project, transaction):
        raise HelmError("exact recovery index could not be reconstructed without moving the branch")
    _write_phase(journal, record, "registered")
    _recovery_checkpoint("registered", transaction)

    _restore_payload(source, stage, scratch, transaction["id"])
    if payload_manifest(stage) != transaction["payload_manifest"]:
        raise HelmError("reconstructed worktree payload does not match the preserved source")
    read_tree = sh(["git", "-C", str(stage), "read-tree", transaction["branch_sha"]],
                   check=False, write_paths=(stage,))
    if read_tree.returncode:
        raise HelmError("reconstructed worktree index could not be verified")
    _assert_attached(project, transaction, stage)
    _write_phase(journal, record, "payload-restored")
    _recovery_checkpoint("payload-restored", transaction)

    if _lexists(canonical):
        raise HelmError("canonical worktree path was repopulated during recovery; every copy was preserved")
    if entry_manifest(source) != transaction["source_manifest"] or not _exact_ref(project, transaction):
        raise HelmError("source or branch changed before final reattachment")
    moved = sh(["git", "-C", str(project["path"]), "worktree", "move", str(stage), str(canonical)],
               check=False, write_paths=(stage.parent, canonical.parent))
    if moved.returncode:
        raise HelmError("Git could not atomically reattach the reconstructed worktree")
    _assert_attached(project, transaction, canonical)
    if payload_manifest(canonical) != transaction["payload_manifest"]:
        raise HelmError("reattached worktree payload changed during the final move")
    _recovery_checkpoint("reattached", transaction)
    _write_phase(journal, record, "complete")
    _recovery_checkpoint("complete", transaction)
    return {"state": "complete", "canonical": str(canonical),
            "preserved_source": str(source), "branch_sha": transaction["branch_sha"],
            "staging_metadata": "original Git index metadata was absent and remains unrecoverable"}


def create(project: dict, work_id: str) -> Path:
    ids.project(project["id"]); work_id = ids.work(work_id)
    repo = Path(project["path"])
    wt = worktree_root() / project["id"] / work_id
    for candidate in (worktree_root(), wt.parent, wt):
        if _lexists(candidate) and not _owned_state_chain(candidate, require_leaf=True):
            raise HelmError("persistent worktree path is linked, unowned, or writable by another user")
    if wt.exists():
        # A work item owns one branch/worktree through questions, steering and revisions.
        # Validate that it is still attached instead of destructively recreating it.
        if (wt / ".git").exists() and git(wt, "rev-parse", "--abbrev-ref", "HEAD", check=False) == branch_name(work_id):
            link_deps(repo, wt)
            return wt
        raise HelmError(f"persistent worktree {wt} exists but is not attached to {branch_name(work_id)}")
    private_mkdir(wt.parent)
    base = project["base"]
    if git(repo, "rev-parse", "--verify", "--quiet", base, check=False) == "":
        raise HelmError(f"base ref '{base}' not found in {repo}")
    br = branch_name(work_id)
    if git(repo, "rev-parse", "--verify", "--quiet", br, check=False) == "":
        git(repo, "branch", br, base)
    result = sh(["git", "-C", str(repo), "worktree", "add", "--quiet", str(wt), br],
                check=False, write_paths=(wt.parent,))
    if result.returncode:
        raise HelmError(f"could not create persistent worktree: {result.stderr.strip()[-1000:]}")
    link_deps(repo, wt)
    return wt


def git_env(wt: Path, exclude_file: Path, base_env: dict | None = None) -> dict:
    """Environment for everything that runs git inside the worktree.

    Linked dependency trees are symlinks, and a `.gitignore` line like `node_modules/`
    matches directories only, so `git add -A` would commit the links. Rather than write
    into the project's `.git/info/exclude`, hand git an extra excludes file through the
    documented GIT_CONFIG_* environment for this run only.
    """
    env = dict(base_env if base_env is not None else os.environ)
    names = [n for n in DEP_DIRS if (wt / n).is_symlink()]
    exclude_file.write_text("".join(f"/{n}\n" for n in names) + "/.helm-ask.json\n")
    count = int(env.get("GIT_CONFIG_COUNT", "0") or 0)
    for key, value in (("core.excludesFile", str(exclude_file)), ("core.fsmonitor", "false"), ("core.untrackedCache", "false")):
        env[f"GIT_CONFIG_KEY_{count}"] = key
        env[f"GIT_CONFIG_VALUE_{count}"] = value
        count += 1
    env["GIT_CONFIG_COUNT"] = str(count)
    return env


DEP_DIRS = ("node_modules", ".venv", "venv", "vendor", ".tox", "target")


def checkpoint_pathspecs(wt: Path) -> list[str]:
    """Exact pathspecs for a controller checkpoint, independent of Git config."""
    excluded = [name for name in DEP_DIRS if (wt / name).is_symlink()]
    return [".", ":(top,exclude).helm-ask.json",
            *(f":(top,exclude){name}" for name in excluded)]


def link_deps(repo: Path, wt: Path) -> list[str]:
    """Symlink git-ignored dependency trees from the checkout into the worktree.

    A fresh worktree has only tracked files, so `.venv/bin/python -m pytest` or `npm test`
    would fail for want of installed packages. Linking keeps verification offline and fast.
    Only directories with no tracked files are linked (a venv ignores itself from inside, so
    `git check-ignore` is not a reliable test); `git_env` keeps the links out of commits.
    """
    linked = []
    for name in DEP_DIRS:
        src, dst = repo / name, wt / name
        if not src.is_dir() or dst.exists():
            continue
        tracked = git(repo, "ls-files", "--", name, check=False)
        if tracked.strip():          # tracked content comes from git, never from a link
            continue
        dst.symlink_to(src, target_is_directory=True)
        linked.append(name)
    return linked


def quarantine(project: dict, work_id: str, *, reason: str,
               destination: Path | None = None) -> dict:
    """Move a worktree intact into recoverable controller-owned quarantine."""
    ids.project(project["id"]); work_id = ids.work(work_id)
    wt = worktree_root() / project["id"] / work_id
    for candidate in (worktree_root(), wt.parent, wt):
        if _lexists(candidate) and not _owned_state_chain(candidate, require_leaf=True):
            raise HelmError("worktree quarantine target is linked, unowned, or writable by another user")
    destination = destination or (home() / "quarantine" / "worktrees" / project["id"] /
                                  f"{work_id}-{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns()}")
    destination = destination.resolve()
    allowed_root = (home() / "quarantine" / "worktrees" / project["id"]).resolve()
    if destination.parent != allowed_root or not destination.name.startswith(work_id + "-"):
        raise HelmError("invalid worktree quarantine destination")
    if destination.exists():
        if wt.exists():
            raise HelmError("both the owned worktree and its quarantine destination exist; preserving both")
        return {"quarantined": True, "path": str(destination), "reason": reason,
                "signature": signature(destination), "reconciled": True}
    if not wt.exists():
        return {"quarantined": False, "reason": "worktree already absent"}
    private_mkdir(destination.parent)
    result = sh(["git", "-C", str(project["path"]), "worktree", "move", str(wt), str(destination)],
                check=False, write_paths=(wt.parent, destination.parent))
    if result.returncode or not destination.is_dir() or wt.exists():
        raise HelmError("worktree quarantine failed; original work remains preserved")
    return {"quarantined": True, "path": str(destination), "reason": reason,
            "signature": signature(destination)}


def remove(project: dict, work_id: str, delete_branch: bool = False,
           *, expected: dict | None = None) -> dict:
    """Remove only an exact clean worktree; never force-delete or rmtree it."""
    ids.project(project["id"]); work_id = ids.work(work_id)
    repo = Path(project["path"])
    wt = worktree_root() / project["id"] / work_id
    for candidate in (worktree_root(), wt.parent, wt):
        if _lexists(candidate) and not _owned_state_chain(candidate, require_leaf=True):
            raise HelmError("worktree cleanup target is linked, unowned, or writable by another user")
    if wt.exists():
        actual = signature(wt)
        if expected is not None and actual != expected:
            raise HelmError("worktree changed before cleanup; preserving it")
        if actual.get("dirty"):
            raise HelmError("worktree is dirty; refusing destructive cleanup")
        result = sh(["git", "-C", str(repo), "worktree", "remove", str(wt)], check=False,
                    write_paths=(wt.parent,))
        if result.returncode or wt.exists():
            raise HelmError("Git could not prove a clean worktree removal; work was preserved")
    branch_deleted = False
    if delete_branch:
        ref = f"refs/heads/{branch_name(work_id)}"
        actual_sha = git(repo, "rev-parse", "--verify", ref, check=False)
        expected_sha = (expected or {}).get("sha")
        if actual_sha and expected_sha and actual_sha != expected_sha:
            raise HelmError("item branch changed before cleanup; preserving the ref")
        if actual_sha and expected_sha:
            result = sh(["git", "-C", str(repo), "update-ref", "-d", ref, expected_sha], check=False)
            if result.returncode:
                raise HelmError("atomic branch cleanup refused because the ref changed")
            branch_deleted = git(repo, "rev-parse", "--verify", ref, check=False) == ""
        elif actual_sha:
            raise HelmError("branch cleanup requires an exact expected SHA")
    return {"removed": not wt.exists(), "branch_deleted": branch_deleted}


def branch_worktrees(project: dict, branch: str) -> list[Path]:
    """Return Git's registered worktree paths for one exact local branch."""
    target = f"refs/heads/{branch}"
    raw = git_raw_bytes(project["path"], "worktree", "list", "--porcelain", "-z")
    paths = []
    for block in raw.split(b"\0\0"):
        fields: dict[bytes, bytes] = {}
        for record in (value for value in block.split(b"\0") if value):
            key, separator, value = record.partition(b" ")
            if not separator:
                continue
            fields[key] = value
        branch = os.fsdecode(fields.get(b"branch", b""))
        path = os.fsdecode(fields.get(b"worktree", b""))
        if branch == target and path:
            paths.append(Path(path).expanduser().resolve())
    return paths


def integrate_latest(project: dict, wt: Path) -> dict:
    """Rebase a clean item branch onto the latest configured base, without data loss."""
    base_sha = git(project["path"], "rev-parse", project["base"])
    before = git(wt, "rev-parse", "HEAD")
    if status_paths(wt):
        return {"at": now(), "base_sha": base_sha, "before_sha": before, "head_sha": before,
                "deferred": True, "reason": "uncommitted checkpoint preserved"}
    if sh(["git", "-C", str(wt), "merge-base", "--is-ancestor", base_sha, "HEAD"], check=False).returncode:
        r = sh(["git", "-C", str(wt), "rebase", base_sha], check=False)
        if r.returncode:
            sh(["git", "-C", str(wt), "rebase", "--abort"], check=False)
            raise HelmError(f"latest-base integration conflicted: {r.stderr.strip()[-500:]}")
    return {"at": now(), "base_sha": base_sha, "before_sha": before, "head_sha": git(wt, "rev-parse", "HEAD"),
            "deferred": False}


def base_is_ancestor(project: dict, wt: Path) -> bool:
    base_sha = git(project["path"], "rev-parse", project["base"])
    return sh(["git", "-C", str(wt), "merge-base", "--is-ancestor", base_sha, "HEAD"], check=False).returncode == 0


def status_paths(wt: Path, *, allow_ask: bool = False) -> list[str]:
    """Return every tracked or untracked mutation, excluding only managed dependency links."""
    out = []
    try:
        raw = git_raw_bytes(wt, "status", "--porcelain=v1", "-z",
                            "--untracked-files=all", "--ignored=matching")
    except (OSError, subprocess.SubprocessError) as exc:
        raise HelmError(f"could not inspect exact worktree paths: {type(exc).__name__}")
    records = raw.split(b"\0")
    index = 0
    while index < len(records):
        record = records[index]; index += 1
        if not record:
            continue
        if len(record) < 3 or record[2:3] != b" ":
            raise HelmError("Git returned malformed NUL-delimited status evidence")
        status = record[:2]
        paths = [os.fsdecode(record[3:])]
        # Porcelain -z emits the second exact pathname as a separate record for
        # a rename/copy.  Retain both source and destination so moving a
        # protected path cannot make it disappear from the mechanical gate.
        if b"R" in status or b"C" in status:
            if index >= len(records) or not records[index]:
                raise HelmError("Git returned an incomplete rename status record")
            paths.append(os.fsdecode(records[index])); index += 1
        for path in paths:
            if not path:
                continue
            top = path.split("/", 1)[0]
            if top in DEP_DIRS and (wt / top).is_symlink():
                continue
            if allow_ask and path == ".helm-ask.json":
                continue
            out.append(path)
    return sorted(set(out))


def signature(wt: Path) -> dict:
    return {"sha": git(wt, "rev-parse", "HEAD"), "dirty": status_paths(wt)}


def has_commits(project: dict, wt: Path) -> bool:
    return git(wt, "rev-list", "--count", f"{project['base']}..HEAD") not in ("", "0")


def is_clean(wt: Path) -> bool:
    return not status_paths(wt)


def changed_files(project: dict, wt: Path) -> list[str]:
    # --no-renames deliberately reports both sides as delete/add, avoiding a
    # rename record whose old protected pathname could otherwise be hidden.
    raw = git_raw_bytes(wt, "diff", "--no-ext-diff", "--no-textconv", "--no-renames",
                        "--name-only", "-z", f"{project['base']}...HEAD")
    return sorted({os.fsdecode(value) for value in raw.split(b"\0") if value})
