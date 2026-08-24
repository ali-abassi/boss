"""Render a graph template for one attempt and drive `piw` (the only runner)."""
from __future__ import annotations
import json
import os
import shlex
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from string import Template


class _T(Template):
    delimiter = "@"  # shell $VARS and $(...) pass through untouched

from .paths import GRAPHS, REPO
from .util import BossError, log
from . import modes


VENDORED_PIW = REPO / "vendor" / "pi-graph" / "bin" / "piw"
TEST_PIW = REPO / "tests" / "fake_piw.py"


def deterministic_test_mode() -> bool:
    """Recognize only the checked-in inert fixture; arbitrary harness injection is forbidden."""
    raw = os.environ.get("BOSS_PIW")
    if not raw:
        return False
    try:
        return Path(raw).expanduser().resolve() == TEST_PIW.resolve()
    except OSError:
        return False


def piw_bin() -> str:
    """Return the runner; only the repository's inert fixture may override it."""
    if os.environ.get("BOSS_PIW") and not deterministic_test_mode():
        raise BossError("BOSS_PIW may select only BOSS's checked-in deterministic test fixture")
    return str(TEST_PIW if deterministic_test_mode() else VENDORED_PIW)


def render(graph: str, dest_dir: Path, *, cwd: Path, branch: str, project: dict,
           models: dict, thinking: dict, timeout: int) -> Path:
    graph = modes.normalize(graph)
    src = GRAPHS / f"{graph}.yaml"
    if not src.exists():
        raise BossError(f"graph template not found: {src}")
    protected = project.get("protected_paths") or []
    vars_ = {
        "CWD": str(cwd), "BRANCH": branch, "BASE": project["base"],
        "TEST_CMD": project.get("test_cmd") or "true",
        "PROTECTED": ", ".join(protected) or "(none)",
        "PROTECTED_ARGS": " ".join(shlex.quote(p) for p in protected),
        "BOSS_REPO": str(REPO), "TIMEOUT": str(timeout),
    }
    for phase, model in models.items():
        vars_[f"MODEL_{phase.upper()}"] = model
    for phase, level in thinking.items():
        vars_[f"THINKING_{phase.upper()}"] = level
    text = _T(src.read_text()).substitute(vars_)
    dest_dir.mkdir(parents=True, exist_ok=True)
    steps = dest_dir / "steps.yaml"
    steps.write_text(text)
    return steps


def validate(steps: Path) -> None:
    r = subprocess.run([piw_bin(), "validate", str(steps)], text=True, capture_output=True)
    if r.returncode != 0:
        raise BossError(f"piw validate failed:\n{r.stdout}{r.stderr}")


def run(steps: Path, brief: Path, timeout: int, env: dict | None = None, monitor=None) -> dict:
    cmd = [piw_bin(), "run", str(steps), "--input-file", str(brief), "--json",
           "--no-cache", "--timeout", str(timeout)]
    log(f"exec {' '.join(shlex.quote(c) for c in cmd)}")
    # stdin MUST be closed: `pi -p` blocks forever on an open inherited pipe (fine in a
    # terminal, fatal under a daemon). Own process group so a timeout kills pi too.
    finding = None
    # Files avoid pipe back-pressure while the supervisor polls controls and scope.
    with tempfile.TemporaryFile(mode="w+") as stdout_file, tempfile.TemporaryFile(mode="w+") as stderr_file:
        proc = subprocess.Popen(cmd, text=True, stdout=stdout_file, stderr=stderr_file,
                                stdin=subprocess.DEVNULL, cwd=str(steps.parent), start_new_session=True, env=env)
        deadline = time.monotonic() + timeout + 120
        while proc.poll() is None:
            finding = monitor() if monitor else None
            if finding or time.monotonic() >= deadline:
                os.killpg(proc.pid, signal.SIGTERM)
                try: proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL); proc.wait()
                break
            time.sleep(0.2)
        stdout_file.seek(0); stderr_file.seek(0)
        stdout, stderr = stdout_file.read(), stderr_file.read()
    if finding:
        return {"ok": False, "failed_ids": ([] if isinstance(finding, dict) and finding.get("control") else ["scope-escape"]),
                "run_dir": "", "exit_code": proc.returncode, **(finding if isinstance(finding, dict) else {"scope_escape": finding}),
                "error": "headless runner stopped at a safe supervisor boundary; worktree preserved"}
    if time.monotonic() >= deadline and proc.returncode:
        stderr = (stderr or "") + f"\nbossctl: killed piw after {timeout + 120}s"
    r = subprocess.CompletedProcess(cmd, proc.returncode, stdout or "", stderr or "")
    summary = None
    for line in reversed(r.stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                summary = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
    if summary is None:
        summary = {"ok": False, "failed_ids": ["<piw>"], "run_dir": "",
                   "error": (r.stderr or r.stdout)[-4000:] or f"piw exit {r.returncode}"}
    summary["exit_code"] = r.returncode
    return summary


def failure_notes(summary: dict, limit: int = 3000) -> str:
    """Tail of evidence for each failed node, fed into the next attempt's brief."""
    parts = []
    if summary.get("error"):
        parts.append(f"runner error: {summary['error'][-limit:]}")
    run_dir = Path(summary.get("run_dir") or "")
    for sid in summary.get("failed_ids") or []:
        if sid.startswith("<"):
            continue
        chunk = []
        if run_dir.is_dir():
            for f in sorted(run_dir.glob(f"{sid}*")):
                if f.is_file():
                    try:
                        chunk.append(f"--- {f.name} ---\n{f.read_text()[-limit:]}")
                    except OSError:
                        pass
        parts.append(f"failed node `{sid}`:\n" + ("\n".join(chunk) or "(no evidence files)"))
    return "\n\n".join(parts)


def probe_model(model: str, timeout: int = 60, env: dict | None = None) -> tuple[bool, str]:
    """One-word live call through pi with piw's exact flags. Proves auth + model id."""
    cmd = ["pi", "-p", "--mode", "json", "--no-session", "--no-approve", "--offline",
           "--no-extensions", "--no-skills", "--no-prompt-templates", "--no-themes", "--no-tools",
           "--model", model, "reply with the single word pong"]
    try:
        r = subprocess.run(cmd, text=True, capture_output=True, stdin=subprocess.DEVNULL,
                           timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return False, f"no answer in {timeout}s"
    except FileNotFoundError:
        return False, "pi not on PATH"
    err, got_text, actual = "", False, None
    for line in r.stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        m = ev.get("message") or {}
        if ev.get("type") == "message_end" and m.get("role") == "assistant":
            if m.get("errorMessage"):
                err = m["errorMessage"].split(";")[0][:160]
            actual = f"{m.get('provider')}/{m.get('model')}"
            if any(c.get("type") == "text" and c.get("text") for c in m.get("content", [])):
                got_text = True
    if err:
        return False, err
    if not got_text:
        return False, f"no assistant text (exit {r.returncode}) {r.stderr[-200:]}"
    if actual != model:
        return False, f"model drift: got {actual}"
    return True, "pong"
