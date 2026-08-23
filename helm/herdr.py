"""Herdr adapter.

Presentation calls are best effort. Implementer identity and model-pinned agent calls are
trusted control-plane operations and therefore fail closed instead of inventing sessions.
"""
from __future__ import annotations
import json
import os
import secrets
import shutil
import subprocess
import time
from pathlib import Path
from .paths import home, REPO
from .util import read_json, write_json, log, now


def inside() -> bool:
    return os.environ.get("HERDR_ENV") == "1" and bool(os.environ.get("HERDR_WORKSPACE_ID")) and bool(shutil.which("herdr"))


def _cli(*args: str) -> dict | None:
    cmd = ["herdr", *args]
    session = os.environ.get("HERDR_SESSION")
    if session:
        cmd += ["--session", session]
    timeout = 20.0
    if "--timeout" in args:
        try: timeout = max(timeout, int(args[args.index("--timeout") + 1]) / 1000 + 10)
        except (ValueError, IndexError): pass
    try:
        r = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"herdr: {' '.join(args[:2])} failed: {e!r}")
        return None
    if r.returncode != 0:
        log(f"herdr: {' '.join(args[:2])} exit {r.returncode}: {r.stderr.strip()[:200]}")
        return None
    try:
        return json.loads(r.stdout) if r.stdout.strip() else {}
    except json.JSONDecodeError:
        return {}


PASS_ENV = ("HELM_HOME", "HELM_PIW", "PI_CODING_AGENT_DIR")


def cli_required(*args: str) -> dict:
    out = _cli(*args)
    if out is None:
        from .util import HelmError
        raise HelmError(f"Herdr operation failed: {' '.join(args[:3])}")
    return out


def _result(out: dict, key: str) -> dict:
    value = (out.get("result") or {}).get(key)
    if not isinstance(value, dict):
        from .util import HelmError
        raise HelmError(f"Herdr response did not contain a real {key} identity")
    return value


LIVE_STATES = {"idle", "done", "working", "blocked"}


def agent_get(target: str) -> dict | None:
    """Return only a positively live agent; a stale/dead/unknown record is not reconnectable."""
    out = _cli("agent", "get", target)
    if not out:
        return None
    agent = _result(out, "agent")
    state = agent.get("agent_status") or agent.get("status") or agent.get("state")
    if state not in LIVE_STATES or not agent.get("pane_id"):
        return None
    return agent


def _reported_model(agent: dict) -> str | None:
    model = agent.get("model") or (agent.get("metadata") or {}).get("model")
    provider = agent.get("provider") or (agent.get("metadata") or {}).get("provider")
    if model and "/" not in str(model) and provider:
        return f"{provider}/{model}"
    return str(model) if model else None


def validate_agent(agent: dict, model: str, thinking: str) -> None:
    """Fail closed when Herdr cannot attest the frozen model and thinking level."""
    from .util import HelmError
    actual_model = _reported_model(agent)
    actual_thinking = agent.get("thinking") or agent.get("thinking_level") or (agent.get("metadata") or {}).get("thinking")
    if actual_model is None or actual_thinking is None:
        raise HelmError("Herdr agent omitted model/thinking metadata; refusing an unverifiable session")
    if actual_model != model or str(actual_thinking) != thinking:
        raise HelmError(f"agent drift: resolved {model} thinking={thinking}, got {actual_model} thinking={actual_thinking}")
    if not agent.get("agent_session_id"):
        raise HelmError("Herdr agent omitted its durable session identity")


def agent_read(target: str, lines: int = 120) -> str:
    cmd = ["herdr", "agent", "read", target, "--source", "recent-unwrapped", "--lines", str(lines)]
    session = os.environ.get("HERDR_SESSION")
    if session:
        cmd += ["--session", session]
    try:
        r = subprocess.run(cmd, text=True, capture_output=True, timeout=20, stdin=subprocess.DEVNULL)
        return r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def ensure_agent(item: dict, cwd: Path, model: str, thinking: str, *, reviewer: bool = False) -> dict:
    """Reconnect a named live agent, or start a real one in a new Herdr tab."""
    if not inside():
        from .util import HelmError
        raise HelmError("persistent execution requires Herdr (start with pi-firstmate); no fallback session was fabricated")
    existing = item.get("session") or {}
    if not reviewer and existing.get("agent_name"):
        live = agent_get(existing["agent_name"])
        if live:
            validate_agent(live, model, thinking)
            if existing.get("agent_session_id") and live.get("agent_session_id") != existing["agent_session_id"]:
                from .util import HelmError
                raise HelmError("Herdr agent name now refers to a different session; refusing identity drift")
        if live:
            return {**existing, **live, "reconnected": True, "liveness_validated_at": now()}
    import re
    base = re.sub(r"[^a-z0-9_-]", "-", item["id"].lower())[-24:].lstrip("-") or "item"
    name = (("review-" + secrets.token_hex(3)) if reviewer else "impl-" + base)[:32]
    tab = open_tab(("review " if reviewer else "work ") + item["project"], "true", cwd)
    if not tab:
        from .util import HelmError
        raise HelmError("Herdr could not create an agent tab")
    # pane run above submitted `true`; wait briefly for the shell prompt before agent start.
    time.sleep(0.05)
    args = ["agent", "start", name, "--kind", "pi", "--pane", tab["pane_id"], "--timeout", "60000",
            "--", "--model", model, "--thinking", thinking]
    agent = _result(cli_required(*args), "agent")
    try:
        validate_agent(agent, model, thinking)
    except BaseException:
        close_tab(tab["tab_id"])
        raise
    # Persist only identities returned by Herdr; no locally generated id is treated as one.
    return {**tab, **agent, "agent_name": agent.get("name") or name, "created": now(),
            "reconnected": False, "liveness_validated_at": now(), "resolved_model": model,
            "resolved_thinking": thinking}


def prompt_agent(target: str, text: str, timeout: int) -> dict:
    return _result(cli_required("agent", "prompt", target, text, "--wait", "--timeout", str(timeout * 1000)), "agent")


def wait_agent(target: str, timeout: int) -> dict:
    return _result(cli_required("agent", "wait", target, "--timeout", str(timeout * 1000)), "agent")


def prompt_agent_monitored(target: str, text: str, timeout: int, monitor) -> tuple[dict, object | None]:
    """Wait for a prompt while polling a safety monitor; cooperatively interrupt on escape."""
    cmd = ["herdr", "agent", "prompt", target, text, "--wait", "--timeout", str(timeout * 1000)]
    if os.environ.get("HERDR_SESSION"):
        cmd += ["--session", os.environ["HERDR_SESSION"]]
    proc = subprocess.Popen(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
    finding = None
    deadline = time.monotonic() + timeout + 10
    while proc.poll() is None:
        finding = monitor()
        if finding:
            interrupt_agent(target)
            break
        if time.monotonic() >= deadline:
            interrupt_agent(target)
            proc.terminate()
            break
        time.sleep(0.2)
    try:
        stdout, stderr = proc.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        proc.terminate(); stdout, stderr = proc.communicate(timeout=5)
    if proc.returncode != 0 and not finding:
        from .util import HelmError
        raise HelmError(f"Herdr agent prompt failed: {(stderr or stdout).strip()[-500:]}")
    if finding:
        return agent_get(target) or {}, finding
    try:
        return _result(json.loads(stdout), "agent"), None
    except (json.JSONDecodeError, TypeError):
        from .util import HelmError
        raise HelmError("Herdr agent prompt returned no real agent identity")


def steer_agent(target: str, text: str, timeout: int = 300) -> dict:
    return prompt_agent(target, "STEERING FROM THE CAPTAIN:\n" + text, timeout)


def interrupt_agent(target: str) -> None:
    cli_required("agent", "send-keys", target, "esc")


def close_agent_tab(session: dict) -> None:
    if session.get("tab_id"):
        close_tab(session["tab_id"])


def open_tab(label: str, command: str, cwd: Path | None = None) -> dict | None:
    """Create a tab beside the captain and run `command` in it. Returns {tab_id, pane_id}."""
    env_args = []
    for k in PASS_ENV:
        v = os.environ.get(k) or (str(home()) if k == "HELM_HOME" else None)
        if v:
            env_args += ["--env", f"{k}={v}"]
    out = _cli("tab", "create", "--workspace", os.environ["HERDR_WORKSPACE_ID"],
               "--cwd", str(cwd or REPO), "--label", label, "--no-focus", *env_args)
    if not out:
        return None
    tab = (out.get("result") or {}).get("tab", {}).get("tab_id")
    pane = (out.get("result") or {}).get("root_pane", {}).get("pane_id")
    if not (tab and pane):
        return None
    _cli("pane", "run", pane, command)
    log(f"herdr: opened tab {tab} '{label}'")
    return {"tab_id": tab, "pane_id": pane, "label": label}


def close_tab(tab_id: str) -> None:
    _cli("tab", "close", tab_id)
    log(f"herdr: closed tab {tab_id}")


def notify(title: str, body: str = "") -> None:
    if shutil.which("herdr") and os.environ.get("HERDR_ENV") == "1":
        _cli("notification", "show", title, *(["--body", body] if body else []))


# ------------------------------------------------------------------ durable tab registry

def _state_path() -> Path: return home() / "herdr.json"


def remember(kind: str, rec: dict) -> None:
    st = read_json(_state_path(), {"tabs": []})
    st["tabs"].append({"kind": kind, **rec})
    write_json(_state_path(), st)


def forget(tab_id: str) -> None:
    st = read_json(_state_path(), {"tabs": []})
    st["tabs"] = [t for t in st["tabs"] if t.get("tab_id") != tab_id]
    write_json(_state_path(), st)


def close_all(kind: str | None = None) -> int:
    st = read_json(_state_path(), {"tabs": []})
    keep, n = [], 0
    for t in st["tabs"]:
        if kind and t.get("kind") != kind:
            keep.append(t); continue
        close_tab(t["tab_id"]); n += 1
    st["tabs"] = keep
    write_json(_state_path(), st)
    return n
