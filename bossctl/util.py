from __future__ import annotations
import datetime as _dt
import fcntl
import json
import os
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any


def now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def private_mkdir(path: Path) -> None:
    """Create every missing directory component owner-only; never chmod existing paths."""
    path = Path(path)
    missing = []
    cursor = path
    while not os.path.lexists(cursor):
        missing.append(cursor)
        parent = cursor.parent
        if parent == cursor:
            break
        cursor = parent
    if os.path.lexists(cursor):
        info = os.lstat(cursor)
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise NotADirectoryError(str(cursor))
    for directory in reversed(missing):
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            info = os.lstat(directory)
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise NotADirectoryError(str(directory))
        else:
            # An unusually restrictive inherited umask may remove owner bits.
            # Tighten only the directory this call positively created.
            os.chmod(directory, 0o700)


def write_json(path: Path, data: Any) -> None:
    """Durably replace one private JSON record without sharing a temp pathname."""
    private_mkdir(path.parent)
    # Python's encoder otherwise writes NaN/Infinity tokens even though they
    # are not JSON.  Durable control state must never manufacture a value that
    # makes numeric safety comparisons fail open.
    payload = (json.dumps(data, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fd = -1
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        # The file fsync makes its contents durable; the directory fsync makes
        # the replacement durable across a controller/host crash.
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def locked(path: Path):
    """Exclusive advisory lock; released on process death."""
    private_mkdir(path.parent)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def sh(args: list[str], cwd: Path | str | None = None, check: bool = True,
       env: dict | None = None, timeout: int | None = None, *, text: bool = True,
       network: bool = False, write_paths: tuple[Path | str, ...] = (),
       read_paths: tuple[Path | str, ...] = ()) -> subprocess.CompletedProcess:
    if args and Path(args[0]).name == "git":
        from .git_boundary import run_git
        return run_git(args, cwd=cwd, check=check, text=text, env=env, timeout=timeout,
                       network=network, write_paths=write_paths, read_paths=read_paths)
    if args and Path(args[0]).name == "gh":
        if cwd is None:
            raise BossError("GitHub CLI commands require an explicit project directory")
        from .git_boundary import run_project
        return run_project(args, cwd=cwd, check=check, text=text, env=env, timeout=timeout,
                           network=network, write_paths=write_paths)
    return subprocess.run(args, cwd=str(cwd) if cwd else None, check=check, text=text,
                          capture_output=True, env=env, timeout=timeout)


def project_sh(args: list[str], cwd: Path | str, check: bool = True,
               env: dict | None = None, timeout: int | None = None, *, text: bool = True,
               network: bool = False,
               write_paths: tuple[Path | str, ...] = ()) -> subprocess.CompletedProcess:
    from .git_boundary import run_project
    return run_project(args, cwd=cwd, check=check, text=text, env=env, timeout=timeout,
                       network=network, write_paths=write_paths)


# Git options bossctl always applies to its own git calls. A global `core.fsmonitor=true`
# starts a watcher daemon per repository and `git worktree add` can block forever on it
# for a fresh worktree; bossctl's worktrees are short-lived, so the watcher buys nothing.
GIT_OPTS = ["-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false"]


def git_raw(repo: Path | str, *args: str, check: bool = True) -> str:
    """Return Git stdout verbatim for formats where leading bytes are data."""
    # Supervisor reads (especially live scope polling) must never contend with an
    # implementer's commit by taking Git's optional index refresh lock.
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    return sh(["git", *GIT_OPTS, "-C", str(repo), *args], check=check, env=env).stdout


def git_raw_bytes(repo: Path | str, *args: str, check: bool = True) -> bytes:
    """Return exact Git bytes for NUL-delimited pathname formats."""
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    return sh(["git", *GIT_OPTS, "-C", str(repo), *args], check=check,
              env=env, text=False).stdout


def git(repo: Path | str, *args: str, check: bool = True) -> str:
    return git_raw(repo, *args, check=check).strip()


def git_config(repo: Path | str, key: str) -> str:
    from .git_boundary import local_config_value
    return local_config_value(repo, key)


def log(msg: str, *, console: bool = True) -> None:
    from .paths import log_file
    line = f"{now()} {msg}"
    try:
        private_mkdir(log_file().parent)
        flags = (os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
                 | getattr(os, "O_NOFOLLOW", 0))
        fd = os.open(log_file(), flags, 0o600)
        with os.fdopen(fd, "a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    if console:
        print(line, file=sys.stderr)


class BossError(SystemExit):
    def __init__(self, msg: str, code: int = 1):
        super().__init__(code)
        self.msg = msg
        print(f"bossctl: {msg}", file=sys.stderr)
