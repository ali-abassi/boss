"""Least-authority execution boundary for controller-owned project commands.

Model tools have their own nested sandbox, but the controller also invokes Git
after inspecting candidate repositories.  Git normally consumes executable
configuration from the repository, the user's config, and inherited ``GIT_*``
variables.  That would turn hooks, filters, diff/merge drivers, transports, or
credential helpers into controller-authority subprocesses.

This module gives every controller Git invocation one fail-closed boundary:

* an absolute, ownership-checked system Git binary;
* an explicit minimal environment with configuration isolated at /dev/null;
* hooks disabled and remote protocols denied unless the caller explicitly
  requests a network operation;
* repository-selected helpers absent (tracked attributes cannot execute a
  driver when no driver configuration is loaded); and
* on macOS, a sandbox which denies offline network and writes outside the
  named repository/Git metadata/scratch capabilities.

Network callers must pass an explicit URL rather than a remote name.  Only
file, HTTPS, and SSH transports can be enabled, and the repository cannot
replace the SSH command, credential helper, or URL through configuration.
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Iterable


_SAFE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
_CONTROL_BYTES = re.compile(r"[\x00-\x1f\x7f]")
_UNSAFE_CONFIG_PATTERNS = tuple(re.compile(value) for value in (
    r"^include(?:if\..+)?\.path$",
    r"^(?:alias|filter|credential|difftool|mergetool|pager|url|http|https|tar|man)\.",
    r"^core\.(?:hookspath|fsmonitor|sshcommand|gitproxy|askpass|editor|pager|"
    r"alternaterefscommand|worktree)$",
    r"^diff\.(?:external|[^.]+\.(?:command|textconv|cachetextconv))$",
    r"^merge\.[^.]+\.(?:driver|recursive)$",
    r"^(?:gpg(?:\.[^.]+)?\.program|sequence\.editor|interactive\.difffilter)$",
    r"^remote\.[^.]+\.(?:vcs|uploadpack|receivepack|proxy|exec|promisor|partialclonefilter)$",
    r"^branch\.[^.]+\.mergeoptions$",
    r"^submodule\.[^.]+\.update$",
    r"^protocol\.",
    r"^uploadpack\.packobjectshook$",
))


def _fail(message: str):
    # Import lazily: util delegates here, and util owns BossError.
    from .util import BossError
    raise BossError(message)


def _trusted_executable(candidates: Iterable[str | None], name: str) -> str:
    for raw in candidates:
        if not raw:
            continue
        path = Path(raw).expanduser()
        try:
            resolved = path.resolve(strict=True)
            info = resolved.stat()
        except OSError:
            continue
        if (not stat.S_ISREG(info.st_mode) or not os.access(resolved, os.X_OK)
                or info.st_uid not in (0, os.getuid()) or info.st_mode & 0o022):
            continue
        return str(resolved)
    _fail(f"no ownership-safe {name} executable is available")


def _optional_trusted_executable(raw: Path | str | None) -> str | None:
    if not raw:
        return None
    try:
        resolved = Path(raw).expanduser().resolve(strict=True)
        info = resolved.stat()
    except OSError:
        return None
    if (not stat.S_ISREG(info.st_mode) or not os.access(resolved, os.X_OK)
            or info.st_uid not in (0, os.getuid()) or info.st_mode & 0o022):
        return None
    return str(resolved)


def git_binary() -> str:
    # Prefer the operator-selected absolute Git when it passes the ownership and
    # mode checks.  On macOS /usr/bin/git is an xcrun shim which attempts to
    # write a cache outside a narrow sandbox on every call; a validated
    # Homebrew Git is a real binary and stays inside the declared capability.
    # /usr/bin/git remains the immutable fallback and the usual Linux path.
    return _trusted_executable((shutil.which("git"), "/usr/bin/git"), "Git")


def _config_file_entries(path: Path) -> list[tuple[str, str]]:
    """Parse one integrity-checked Git config as data, never following includes."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return []
    except OSError:
        _fail("repository Git config cannot be inspected safely")
    if (not stat.S_ISREG(info.st_mode) or path.is_symlink()
            or info.st_uid != os.getuid() or info.st_mode & 0o022):
        _fail("repository Git config is not an owner-controlled regular file")
    scratch = Path(tempfile.mkdtemp(prefix="boss-config-")); scratch.chmod(0o700)
    try:
        env = _minimal_env(scratch=scratch, network=False)
        result = subprocess.run(
            [git_binary(), "config", "--file", str(path), "--no-includes",
             "--null", "--list"], capture_output=True, stdin=subprocess.DEVNULL,
            env=env, timeout=10,
        )
        if result.returncode:
            _fail("repository Git config is malformed or unreadable")
        entries = []
        for record in result.stdout.split(b"\0"):
            if not record:
                continue
            raw_key, separator, raw_value = record.partition(b"\n")
            if not separator:
                _fail("repository Git config returned a malformed record")
            try:
                key = raw_key.decode("utf-8").lower()
                value = raw_value.decode("utf-8")
            except UnicodeDecodeError:
                _fail("repository Git config is not UTF-8")
            if _CONTROL_BYTES.search(key):
                _fail("repository Git config key contains control bytes")
            entries.append((key, value))
        return entries
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _git_layout(repo: Path, *, scratch: Path) -> tuple[Path, Path] | None:
    """Resolve worktree-specific and common metadata with a non-mutating builtin."""
    env = _minimal_env(scratch=scratch, network=False)
    resolved = []
    for flag in ("--absolute-git-dir", "--git-common-dir"):
        result = subprocess.run(
            [git_binary(), "-c", "core.hooksPath=/dev/null", "-C", str(repo),
             "rev-parse", flag], text=True, capture_output=True,
            stdin=subprocess.DEVNULL, env=env, timeout=10,
        )
        if result.returncode or not result.stdout.strip():
            return None
        value = Path(result.stdout.strip())
        resolved.append((value if value.is_absolute() else repo / value).resolve())
    git_dir, common = resolved
    for directory in (git_dir, common):
        try:
            info = directory.stat()
        except OSError:
            _fail("repository Git metadata cannot be inspected safely")
        if not directory.is_dir() or info.st_uid != os.getuid() or info.st_mode & 0o022:
            _fail("repository Git metadata is not owner-controlled")
    return git_dir, common


def _audit_project_config(repo: Path, *, scratch: Path) -> tuple[Path, Path] | None:
    """Fail before Git can consume any local executable/transport configuration."""
    layout = _git_layout(repo, scratch=scratch)
    if layout is None:
        return None
    git_dir, common = layout
    paths = [common / "config"]
    if git_dir != common:
        paths.append(git_dir / "config.worktree")
    else:
        paths.append(common / "config.worktree")
    for path in paths:
        for key, _value in _config_file_entries(path):
            if any(pattern.match(key) for pattern in _UNSAFE_CONFIG_PATTERNS):
                _fail(f"repository Git config contains controller-unsafe key {key!r}")
    return layout


def _minimal_env(*, scratch: Path, network: bool, inherited: dict | None = None,
                 expose_user_home: bool = False) -> dict[str, str]:
    source = inherited or os.environ
    path_parts = [str(Path(git_binary()).parent), _SAFE_PATH]
    env = {
        "PATH": ":".join(path_parts),
        "HOME": str(Path.home() if expose_user_home else scratch),
        "XDG_CONFIG_HOME": str(Path.home() / ".config" if expose_user_home else scratch / "config"),
        "XDG_CACHE_HOME": str(scratch / "cache"),
        "TMPDIR": str(scratch),
        "LANG": source.get("LANG", "C.UTF-8"),
        "LC_ALL": source.get("LC_ALL", ""),
        # GIT_CONFIG is the documented single-file mode.  Command-line -c
        # entries below still apply, but system/global/local/worktree config and
        # every inherited GIT_CONFIG_COUNT entry are absent.
        "GIT_CONFIG": "/dev/null",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/usr/bin/false",
        "SSH_ASKPASS": "/usr/bin/false",
        "GIT_PAGER": "cat",
        "PAGER": "cat",
    }
    if network:
        # A direct executable avoids GIT_SSH_COMMAND's shell interpretation.
        # The generated scratch-home config supplies non-interactive strict
        # host/key behavior without consuming ~/.ssh/config or ProxyCommand.
        env["GIT_SSH"] = "/usr/bin/ssh"
        env["GIT_SSH_VARIANT"] = "ssh"
        env["GIT_PROTOCOL_FROM_USER"] = "0"
    return env


def _safe_options(*, network: bool, env: dict[str, str]) -> list[str]:
    options = [
        "-c", "core.hooksPath=/dev/null",
        "-c", "core.attributesFile=/dev/null",
        "-c", "core.excludesFile=/dev/null",
        "-c", "core.fsmonitor=false",
        "-c", "core.untrackedCache=false",
        "-c", "commit.gpgSign=false",
        "-c", "tag.gpgSign=false",
        "-c", "maintenance.auto=false",
        "-c", "gc.auto=0",
        "-c", "submodule.recurse=false",
        "-c", "fetch.recurseSubmodules=false",
        "-c", "push.recurseSubmodules=no",
        "-c", "status.submoduleSummary=false",
        "-c", "diff.ignoreSubmodules=all",
        "-c", "protocol.allow=never",
        "-c", "protocol.file.allow=always",
        "-c", f"protocol.https.allow={'always' if network else 'never'}",
        "-c", f"protocol.ssh.allow={'always' if network else 'never'}",
        "-c", "protocol.http.allow=never",
        "-c", "protocol.git.allow=never",
        # Clear any built-in/system credential sequence before selecting only
        # a controller-owned helper for deliberate network operations.
        "-c", "credential.helper=",
    ]
    if network and platform.system() == "Darwin":
        # This helper is a trusted Git installation binary and needs no shell.
        # If the keychain has no credential, Git fails non-interactively.
        options += ["-c", "credential.helper=osxkeychain"]
    return options


def _git_config_environment(env: dict[str, str], *, network: bool) -> None:
    """Apply the same safe -c set to Git processes spawned by a trusted CLI."""
    options = _safe_options(network=network, env=env)
    pairs = [(options[index + 1].split("=", 1)[0], options[index + 1].split("=", 1)[1])
             for index in range(0, len(options), 2)]
    env["GIT_CONFIG_COUNT"] = str(len(pairs))
    for index, (key, value) in enumerate(pairs):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value


def _prepare_ssh_home(scratch: Path) -> None:
    """Copy only default key material into a generated strict SSH home."""
    target = scratch / ".ssh"
    target.mkdir(mode=0o700)
    config = target / "config"
    config.write_text(
        "Host *\n"
        "  BatchMode yes\n"
        "  StrictHostKeyChecking yes\n"
        "  ClearAllForwardings yes\n"
        "  ForwardAgent no\n"
    )
    config.chmod(0o600)
    source = Path.home() / ".ssh"
    try:
        source_info = source.stat()
    except OSError:
        return
    if (not source.is_dir() or source.is_symlink() or source_info.st_uid != os.getuid()
            or source_info.st_mode & 0o022):
        return
    names = (
        "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "id_xmss",
        "id_ecdsa_sk", "id_ed25519_sk", "known_hosts", "known_hosts2",
    )
    for name in names:
        candidate = source / name
        try:
            info = candidate.lstat()
        except OSError:
            continue
        is_host_file = name.startswith("known_hosts")
        if (not stat.S_ISREG(info.st_mode) or candidate.is_symlink()
                or info.st_uid != os.getuid() or info.st_mode & 0o022
                or (not is_host_file and info.st_mode & 0o077)):
            continue
        destination = target / name
        shutil.copyfile(candidate, destination)
        destination.chmod(0o600)


def _git_exec_path(scratch: Path) -> Path | None:
    env = _minimal_env(scratch=scratch, network=False)
    result = subprocess.run([git_binary(), "--exec-path"], text=True, capture_output=True,
                            stdin=subprocess.DEVNULL, env=env, cwd="/", timeout=10)
    if result.returncode or not result.stdout.strip():
        return None
    try:
        path = Path(result.stdout.strip()).resolve(strict=True)
    except OSError:
        return None
    return path if path.is_dir() else None


def _allowed_git_executables(*, scratch: Path, network: bool,
                             allow_local_transport_shell: bool = False) -> list[Path]:
    allowed = [Path(git_binary())]
    if not network:
        return allowed
    if allow_local_transport_shell:
        for raw in ("/bin/sh", "/bin/bash"):
            shell = _optional_trusted_executable(raw)
            if shell:
                allowed.append(Path(shell))
    ssh = _optional_trusted_executable("/usr/bin/ssh")
    if ssh:
        allowed.append(Path(ssh))
    exec_path = _git_exec_path(scratch)
    if exec_path:
        for name in ("git-remote-http", "git-remote-https", "git-upload-pack",
                     "git-receive-pack", "git-credential-osxkeychain"):
            helper = _optional_trusted_executable(exec_path / name)
            if helper:
                allowed.append(Path(helper))
    return sorted(set(allowed))


def _repo_from_args(args: list[str], cwd: Path | str | None) -> Path | None:
    for index, value in enumerate(args[:-1]):
        if value == "-C":
            try:
                return Path(args[index + 1]).expanduser().resolve()
            except OSError:
                return None
    if cwd:
        try:
            return Path(cwd).expanduser().resolve()
        except OSError:
            return None
    return None


def _common_dir(repo: Path, *, scratch: Path) -> Path | None:
    env = _minimal_env(scratch=scratch, network=False)
    result = subprocess.run(
        [git_binary(), "-c", "core.hooksPath=/dev/null", "-C", str(repo),
         "rev-parse", "--git-common-dir"],
        text=True, capture_output=True, stdin=subprocess.DEVNULL,
        env=env, timeout=10,
    )
    if result.returncode or not result.stdout.strip():
        return None
    value = Path(result.stdout.strip())
    return (value if value.is_absolute() else repo / value).resolve()


def _profile(*, repo: Path | None, common: Path | None, scratch: Path,
             network: bool, write_paths: Iterable[Path | str],
             read_paths: Iterable[Path | str] = (),
             repo_writable: bool = True,
             user_config: bool = False,
             allowed_exec: Iterable[Path | str] = ()) -> str | None:
    if platform.system() != "Darwin" or not Path("/usr/bin/sandbox-exec").is_file():
        return None
    from .sandbox import _restricted_profile

    readable = [scratch]
    writable = [scratch]
    exact_writes = [Path("/dev/null"), Path("/dev/tty")]
    exact_reads: list[Path] = []
    if repo is not None:
        readable.append(repo)
        if repo_writable:
            writable.append(repo)
    if common is not None:
        readable.append(common)
        if repo_writable:
            writable.append(common)
    for raw in write_paths:
        lexical = Path(os.path.abspath(os.path.expanduser(str(raw))))
        path = lexical.resolve()
        readable.append(path); writable.append(path)
        # Git's safe_create_leading_directories() issues mkdir(2) for each
        # already-existing ancestor before it reaches a new worktree path.
        # A literal capability lets that fixed Git operation observe EEXIST
        # without granting writes to any sibling content under the ancestor.
        for spelling in {lexical, path}:
            parent = spelling
            while parent != parent.parent:
                if parent.exists():
                    exact_writes.append(parent)
                    exact_reads.append(parent)
                parent = parent.parent
    for raw in read_paths:
        readable.append(Path(raw).expanduser().resolve())
    if user_config:
        # Exact controller-selected credential/config roots only.  Repository
        # data never expands this list.
        for candidate in (
            Path.home() / ".config" / "gh",
            Path.home() / "Library" / "Application Support" / "gh",
        ):
            if candidate.exists():
                readable.append(candidate.resolve())
    # Git validates worktree paths by walking to the filesystem root.  Permit
    # stat/chdir on only those exact ancestor vnodes; their children remain
    # unreadable unless separately listed above.
    for capability in readable:
        parent = capability.resolve()
        while parent != parent.parent:
            if parent.exists():
                exact_reads.append(parent)
            parent = parent.parent
    return _restricted_profile(
        writable=writable,
        exact_writes=exact_writes,
        readable_home=readable,
        exact_reads=exact_reads,
        allowed_exec=[Path(value) for value in allowed_exec],
        deny_network=not network,
        deny_signal=True,
        deny_security_services=not network,
    )


def run_git(args: list[str], *, cwd: Path | str | None = None, check: bool = True,
            text: bool = True, env: dict | None = None, timeout: int | None = None,
            network: bool = False, write_paths: Iterable[Path | str] = (),
            read_paths: Iterable[Path | str] = ()) -> subprocess.CompletedProcess:
    """Run one Git command with no repository/ambient executable config."""
    scratch = Path(tempfile.mkdtemp(prefix="boss-controller-git-"))
    scratch.chmod(0o700)
    try:
        write_paths = tuple(write_paths)
        read_paths = tuple(read_paths)
        if network:
            _prepare_ssh_home(scratch)
        safe_env = _minimal_env(scratch=scratch, network=network, inherited=env)
        repo = _repo_from_args(args, cwd)
        layout = (_audit_project_config(repo, scratch=scratch)
                  if repo and repo.exists() else None)
        for readable in read_paths:
            candidate = Path(readable).expanduser().resolve()
            if candidate.is_dir():
                _audit_project_config(candidate, scratch=scratch)
        common = layout[1] if layout else None
        command = [git_binary(), *_safe_options(network=network, env=safe_env), *args[1:]]
        allowed_exec = _allowed_git_executables(
            scratch=scratch, network=network,
            allow_local_transport_shell=bool(network and read_paths))
        profile = _profile(repo=repo, common=common, scratch=scratch,
                           network=network, write_paths=write_paths,
                           read_paths=read_paths, allowed_exec=allowed_exec)
        if profile is not None:
            command = ["/usr/bin/sandbox-exec", "-p", profile, *command]
        # Commands with no repository capability run from the immutable
        # filesystem root.  Running from the private scratch directory makes
        # Git's repository discovery stat every private ancestor needlessly.
        return subprocess.run(
            command, cwd=str(cwd) if cwd else "/", check=check, text=text,
            capture_output=True, stdin=subprocess.DEVNULL, env=safe_env,
            timeout=timeout if timeout is not None else (120 if network else 300),
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def run_project(args: list[str], *, cwd: Path | str, check: bool = True,
                text: bool = True, env: dict | None = None, timeout: int | None = None,
                network: bool = False,
                write_paths: Iterable[Path | str] = ()) -> subprocess.CompletedProcess:
    """Run an explicit trusted project CLI with sanitized child-Git behavior."""
    if not args:
        _fail("empty project command")
    scratch = Path(tempfile.mkdtemp(prefix="boss-controller-project-")); scratch.chmod(0o700)
    try:
        safe_env = _minimal_env(scratch=scratch, network=network, inherited=env,
                                expose_user_home=True)
        _git_config_environment(safe_env, network=network)
        resolved = _trusted_executable((shutil.which(args[0], path=(env or os.environ).get("PATH")),),
                                       Path(args[0]).name)
        repo = Path(cwd).expanduser().resolve()
        layout = _audit_project_config(repo, scratch=scratch) if repo.exists() else None
        common = layout[1] if layout else None
        command = [resolved, *args[1:]]
        allowed_exec = [Path(resolved), *_allowed_git_executables(
            scratch=scratch, network=network)]
        profile = _profile(repo=repo, common=common, scratch=scratch,
                           network=network, write_paths=write_paths,
                           repo_writable=False, user_config=True,
                           allowed_exec=allowed_exec)
        if profile is not None:
            command = ["/usr/bin/sandbox-exec", "-p", profile, *command]
        return subprocess.run(
            command, cwd=str(repo), check=check, text=text, capture_output=True,
            stdin=subprocess.DEVNULL, env=safe_env,
            timeout=timeout if timeout is not None else (120 if network else 300),
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def local_config_values(repo: Path | str, key: str) -> list[str]:
    """Read one exact common-config key without following includes.

    The values are data only; callers still decide whether a URL or other
    value is acceptable.  The config file itself must be a private-integrity
    file owned by the current user and must not be a symlink.
    """
    root = Path(repo).expanduser().resolve()
    scratch = Path(tempfile.mkdtemp(prefix="boss-git-config-")); scratch.chmod(0o700)
    try:
        common = _common_dir(root, scratch=scratch)
        if common is None:
            return []
        config = common / "config"
        try:
            info = config.lstat()
        except OSError:
            return []
        if (not stat.S_ISREG(info.st_mode) or config.is_symlink()
                or info.st_uid != os.getuid() or info.st_mode & 0o022):
            _fail("repository Git config is not an owner-controlled regular file")
        env = _minimal_env(scratch=scratch, network=False)
        # --file is a single explicit source; --no-includes makes even an
        # include directive inert.
        result = subprocess.run(
            [git_binary(), "config", "--file", str(config), "--no-includes",
             "--null", "--get-all", key],
            capture_output=True, stdin=subprocess.DEVNULL, env=env, timeout=10,
        )
        if result.returncode not in (0, 1):
            _fail(f"could not read repository Git config key {key}")
        values = []
        for raw in result.stdout.split(b"\0"):
            if not raw:
                continue
            try:
                value = raw.decode("utf-8")
            except UnicodeDecodeError:
                _fail(f"repository Git config key {key} is not UTF-8")
            if _CONTROL_BYTES.search(value):
                _fail(f"repository Git config key {key} contains control bytes")
            values.append(value)
        return values
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def local_config_value(repo: Path | str, key: str) -> str:
    values = local_config_values(repo, key)
    if not values:
        return ""
    if len(values) != 1:
        _fail(f"repository Git config key {key} is ambiguous")
    return values[0]
