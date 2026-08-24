"""macOS capability boundary for Herdr-started Pi sessions.

The Pi extension remains useful UX/early rejection, but it is not the security
boundary.  Every managed Pi process is launched through sandbox-exec and proves
the boundary from inside session_start before it can receive a model turn.
"""
from __future__ import annotations

import hashlib
import os
import platform
import re
import secrets
import shutil
import subprocess
import signal
from pathlib import Path

from .paths import REPO, home
from .util import BossError, sh


def _canonical(path: Path | str) -> Path:
    return Path(path).expanduser().resolve()


def _sbpl(value: Path | str) -> str:
    raw = str(_canonical(value))
    return '"' + raw.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _rules(kind: str, paths: list[Path]) -> list[str]:
    return [f"({kind} {_sbpl(path)})" for path in sorted(set(paths))]


def _restricted_profile(*, writable: list[Path], exact_writes: list[Path],
                        readable_home: list[Path], deny_network: bool,
                        protected_writes: list[Path] | None = None,
                        exact_reads: list[Path] | None = None,
                        allowed_exec: list[Path] | None = None,
                        deny_signal: bool = True,
                        deny_security_services: bool = True) -> str:
    write_rules = _rules("subpath", writable) + _rules("literal", exact_writes)
    # `subpath` excludes the directory vnode itself.  Permit each declared
    # readable root literally as well so tools can stat/chdir it without
    # gaining any access to sibling contents.
    read_rules = (_rules("subpath", readable_home) + _rules("literal", readable_home)
                  + _rules("literal", exact_reads or []))
    lines = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*",
        "  (require-not",
        "    (require-any",
        *[f"      {rule}" for rule in write_rules],
        "    )",
        "  )",
        ")",
    ]
    for protected in sorted(set(protected_writes or [])):
        # Linked worktrees use a literal `.git` file; ordinary repositories use
        # a directory. Deny both forms even though their parent is writable.
        lines += [f"(deny file-write* (literal {_sbpl(protected)}))",
                  f"(deny file-write* (subpath {_sbpl(protected)}))"]
    if allowed_exec is not None:
        exec_rules = _rules("literal", allowed_exec)
        lines += [
            "(deny process-exec",
            "  (require-not",
            "    (require-any",
            *[f"      {rule}" for rule in exec_rules],
            "    )",
            "  )",
            ")",
        ]
    # Candidate tools do not need any other user's-home or BOSS state. System
    # libraries/binaries remain readable, while private config, credentials,
    # sibling worktrees, sessions, and control-plane ledgers do not.
    private_roots = {_canonical(Path.home()), _canonical(home())}
    for candidate in (Path("/tmp"), Path("/private/tmp"), Path("/var/tmp"),
                      Path(os.environ.get("TMPDIR") or "/private/tmp")):
        if candidate.exists():
            private_roots.add(_canonical(candidate))
    for root in sorted(private_roots):
        lines += [
            "(deny file-read*",
            "  (require-all",
            f"    (subpath {_sbpl(root)})",
            "    (require-not",
            "      (require-any",
            *[f"        {rule}" for rule in read_rules],
            "      )",
            "    )",
            "  )",
            ")",
        ]
    if deny_network:
        lines += ["(deny network*)"]
    # Do not let candidate code signal unrelated host processes. The
    # controller remains outside this nested profile and can still time out the
    # whole process group.
    if deny_signal:
        lines += ["(deny signal)"]
    if deny_security_services:
        # Prevent indirect access to login/keychain material through the
        # Security framework even when a payload hides `security` in a script.
        lines += [
            "(deny mach-lookup",
            '  (global-name "com.apple.securityd")',
            '  (global-name "com.apple.securityd.xpc")',
            '  (global-name "com.apple.SecurityServer")',
            '  (global-name "com.apple.security.agent")',
            ")",
        ]
    lines += [""]
    return "\n".join(lines)


def _read_capabilities(cwd: Path) -> list[Path]:
    allowed = [cwd]
    for name in ("node_modules", ".venv", "venv", "vendor", ".tox", "target"):
        candidate = cwd / name
        if candidate.exists():
            allowed.append(_canonical(candidate))
    # Git inspection in a linked worktree resolves into the registered
    # repository's common metadata. It is read-only in the nested profile.
    for flag in ("--git-dir", "--git-common-dir"):
        try:
            result = sh(["git", "-C", str(cwd), "rev-parse", flag], timeout=5,
                        check=False)
            if result.returncode == 0 and result.stdout.strip():
                value = Path(result.stdout.strip())
                allowed.append(_canonical(value if value.is_absolute() else cwd / value))
        except (OSError, subprocess.TimeoutExpired):
            pass
    return sorted(set(allowed))


def available() -> dict:
    binary = shutil.which("sandbox-exec")
    wrapper = REPO / "libexec" / "pi"
    tool_runner = REPO / "libexec" / "boss-tool"
    real_pi = shutil.which("pi")
    prerequisites = bool(platform.system() == "Darwin" and binary == "/usr/bin/sandbox-exec"
                         and wrapper.is_file() and os.access(wrapper, os.X_OK)
                         and tool_runner.is_file() and os.access(tool_runner, os.X_OK)
                         and real_pi and _canonical(real_pi) != wrapper.resolve())
    reaper_ready = False
    reaper_detail = "process fingerprint reaper was not checked"
    if prerequisites:
        try:
            check = subprocess.run([str(tool_runner), "--capability-check"], text=True,
                                   capture_output=True, stdin=subprocess.DEVNULL, timeout=5)
            reaper_ready = check.returncode == 0
            reaper_detail = (check.stdout if reaper_ready else check.stderr).strip() or (
                "process fingerprint reaper available" if reaper_ready else "process fingerprint reaper failed")
        except (OSError, subprocess.TimeoutExpired) as exc:
            reaper_detail = f"process fingerprint reaper unavailable: {type(exc).__name__}"
    ready = prerequisites and reaper_ready
    return {"ready": ready, "platform": platform.system(), "sandbox_exec": binary,
            "wrapper": str(wrapper), "tool_runner": str(tool_runner), "real_pi": real_pi,
            "reaper_ready": reaper_ready, "reaper_detail": reaper_detail,
            "reason": ("macOS sandbox and descendant reaper available" if ready else
                       "managed Herdr agents require executable libexec/pi, real pi, /usr/bin/sandbox-exec, "
                       "and Darwin process-fingerprint reaping")}


def prepare(*, work_id: str, launch_id: str, cwd: Path, session_dir: Path,
            attestation_dir: Path, writable_worktree: bool,
            exact_writes: list[str] | None = None,
            pi_source_dir: Path | None = None) -> dict:
    status = available()
    if not status["ready"]:
        raise BossError(status["reason"])
    cwd = _canonical(cwd); session_dir = _canonical(session_dir)
    attestation_dir = _canonical(attestation_dir)
    item_root = _canonical(home() / "work" / work_id)
    for controlled in (session_dir, attestation_dir):
        try: controlled.relative_to(item_root)
        except ValueError:
            raise BossError("sandbox-owned session evidence escaped the item state directory")
    sandbox_dir = item_root / "agent-sandboxes" / launch_id
    tmp_dir = sandbox_dir / "tmp"
    pi_dir = sandbox_dir / "pi"
    probe_dir = item_root / "sandbox-probes"
    sandbox_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    tmp_dir.mkdir(mode=0o700); pi_dir.mkdir(mode=0o700)
    probe_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    probe = probe_dir / f"forbidden-{launch_id}"

    # Pi takes advisory locks and may refresh its OAuth record at startup. It
    # must never receive write authority over BOSS's shared Pi home, so
    # each durable launch gets a minimal private copy inside its capability
    # root. Session/review evidence lives elsewhere and is never copied.
    source = _canonical(pi_source_dir or home() / "pi")
    if not source.is_dir() or source.is_symlink():
        raise BossError("managed Herdr agents require a real isolated Pi configuration directory")
    copied: list[str] = []
    for name in ("auth.json", "settings.json", "models.json", "models-store.json"):
        candidate = source / name
        if not candidate.exists():
            continue
        if not candidate.is_file() or candidate.is_symlink() or candidate.stat().st_uid != os.getuid():
            raise BossError(f"refusing unsafe Pi configuration source: {name}")
        destination = pi_dir / name
        shutil.copyfile(candidate, destination)
        destination.chmod(0o600)
        copied.append(name)
    if not copied:
        raise BossError("managed Herdr agents found no usable Pi configuration files")

    subpaths = [session_dir, attestation_dir, tmp_dir, pi_dir]
    if writable_worktree:
        subpaths.append(cwd)
    literals = [Path("/dev/null"), Path("/dev/tty")]
    for raw in exact_writes or []:
        target = _canonical(raw)
        if target == cwd or cwd in target.parents:
            if not writable_worktree:
                literals.append(target)
        else:
            # A reviewer may write only its one controller-designated verdict;
            # arbitrary extra paths never become capabilities.
            try: target.relative_to(item_root)
            except ValueError:
                raise BossError("sandbox exact-write capability escaped item state")
            literals.append(target)
    requirements = [f"(subpath {_sbpl(path)})" for path in sorted(set(subpaths))]
    requirements += [f"(literal {_sbpl(path)})" for path in sorted(set(literals))]
    profile = "\n".join([
        "(version 1)",
        "(allow default)",
        "(deny file-write*",
        "  (require-not",
        "    (require-any",
        *[f"      {rule}" for rule in requirements],
        "    )",
        "  )",
        ")",
        f"(deny file-write* (literal {_sbpl(cwd / '.git')}))",
        f"(deny file-write* (subpath {_sbpl(cwd / '.git')}))",
        "",
    ])
    profile_path = sandbox_dir / "profile.sb"
    profile_path.write_text(profile); profile_path.chmod(0o600)
    digest = hashlib.sha256(profile.encode()).hexdigest()
    # Pi itself needs provider network access, but every model-invoked shell is
    # nested in this stricter profile. This removes network, host credentials,
    # unrelated writes, and process signalling from candidate-controlled code.
    tool_profile = _restricted_profile(
        writable=([cwd] if writable_worktree else []) + [tmp_dir],
        exact_writes=[Path(value) for value in exact_writes or []]
                     + [Path("/dev/null"), Path("/dev/tty")],
        readable_home=_read_capabilities(cwd) + [tmp_dir]
                      + [Path(value).parent for value in exact_writes or []],
        deny_network=True,
        protected_writes=[cwd / ".git"],
    )
    tool_profile_path = sandbox_dir / "tool-profile.sb"
    tool_profile_path.write_text(tool_profile); tool_profile_path.chmod(0o600)
    return {"profile": str(profile_path), "profile_sha256": digest,
            "tool_profile": str(tool_profile_path),
            "tool_profile_sha256": hashlib.sha256(tool_profile.encode()).hexdigest(),
            "tmpdir": str(tmp_dir), "probe": str(probe),
            "pi_dir": str(pi_dir), "pi_config_files": copied,
            "wrapper_dir": str((REPO / "libexec").resolve()), "real_pi": status["real_pi"],
            "tool_runner": status["tool_runner"],
            "writable_worktree": writable_worktree,
            "exact_writes": [str(_canonical(path)) for path in exact_writes or []]}


def verify_record(record: dict) -> bool:
    """Verify the immutable launch-side profile receipt, not agent assertions."""
    raw = record.get("sandbox_profile")
    expected = record.get("sandbox_profile_sha256")
    if not raw or not re.fullmatch(r"[0-9a-f]{64}", str(expected or "")):
        return False
    path = Path(str(raw))
    try:
        return (path.is_file() and not path.is_symlink() and path.stat().st_uid == os.getuid()
                and not path.stat().st_mode & 0o077
                and hashlib.sha256(path.read_bytes()).hexdigest() == expected)
    except OSError:
        return False


def verify_tool_record(record: dict) -> bool:
    raw = record.get("sandbox_tool_profile") or record.get("tool_profile")
    expected = record.get("sandbox_tool_profile_sha256") or record.get("tool_profile_sha256")
    if not raw or not re.fullmatch(r"[0-9a-f]{64}", str(expected or "")):
        return False
    path = Path(str(raw))
    try:
        return (path.is_file() and not path.is_symlink() and path.stat().st_uid == os.getuid()
                and not path.stat().st_mode & 0o077
                and hashlib.sha256(path.read_bytes()).hexdigest() == expected)
    except OSError:
        return False


def run_verification(*, work_id: str, phase: str, cwd: Path, command: str,
                     timeout: int, env: dict | None = None) -> subprocess.CompletedProcess:
    """Run candidate-controlled verification with no host/network authority."""
    status = available()
    if not status["ready"]:
        raise BossError("candidate verification requires the pinned macOS sandbox; refusing an unsandboxed test")
    cwd = _canonical(cwd); item_root = _canonical(home() / "work" / work_id)
    sandbox_dir = item_root / "verification-sandboxes" / f"{phase}-{secrets.token_hex(8)}"
    scratch = sandbox_dir / "scratch"
    sandbox_dir.mkdir(parents=True, exist_ok=False, mode=0o700); scratch.mkdir(mode=0o700)
    profile = _restricted_profile(
        writable=[cwd, scratch], exact_writes=[Path("/dev/null"), Path("/dev/tty")],
        readable_home=_read_capabilities(cwd) + [scratch], deny_network=True,
        protected_writes=[cwd / ".git"],
    )
    profile_path = sandbox_dir / "profile.sb"
    profile_path.write_text(profile); profile_path.chmod(0o600)
    source = env or os.environ
    safe_env = {
        "PATH": source.get("PATH", "/usr/bin:/bin"), "HOME": str(scratch), "TMPDIR": str(scratch),
        "LANG": source.get("LANG", "C.UTF-8"), "LC_ALL": source.get("LC_ALL", ""),
        "GIT_OPTIONAL_LOCKS": "0",
    }
    for key, value in source.items():
        if key == "GIT_CONFIG_COUNT" or key.startswith("GIT_CONFIG_KEY_") or key.startswith("GIT_CONFIG_VALUE_"):
            safe_env[key] = value
    runner = Path(str(status["tool_runner"]))
    args = [str(runner), str(profile_path), hashlib.sha256(profile.encode()).hexdigest(),
            str(scratch), str(max(1, int(timeout))), command]
    process = subprocess.Popen(args, cwd=str(cwd), text=True, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                               env=safe_env, start_new_session=True)
    try:
        # The runner owns a bounded fail-closed descendant sweep after the
        # command deadline. Give that proof time to finish before considering
        # the controller itself wedged.
        stdout, stderr = process.communicate(timeout=max(1, int(timeout)) + 15)
    except subprocess.TimeoutExpired:
        process.send_signal(signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=12)
        except subprocess.TimeoutExpired:
            process.kill(); stdout, stderr = process.communicate()
        return subprocess.CompletedProcess(args, 124, stdout or "", stderr or "verification runner timed out")
    return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
