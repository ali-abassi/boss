"""Herdr adapter.

Presentation calls are best effort. Implementer identity and model-pinned agent calls are
trusted control-plane operations and therefore fail closed instead of inventing sessions.
"""
from __future__ import annotations
import hashlib
import base64
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from .paths import home, REPO
from .util import read_json, write_json, locked, log, now


class UnsettledAgentError(RuntimeError):
    """A control was sent but the live agent never proved it stopped."""


def inside() -> bool:
    return os.environ.get("HERDR_ENV") == "1" and bool(os.environ.get("HERDR_WORKSPACE_ID")) and bool(shutil.which("herdr"))


def _cli(*args: str) -> dict | None:
    session = os.environ.get("HERDR_SESSION")
    # `pane run` and `agent start -- ...` both end in variadic arguments. A
    # trailing global flag can become pane text or an agent flag, so the named
    # session must precede every subcommand.
    cmd = ["herdr", *(["--session", session] if session else []), *args]
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


def wait_shell(pane_id: str, timeout: float = 10.0, *, session: str | None = None,
               binary: str | None = None) -> bool:
    """Wait for a real prompt before sending a pane command; new PTYs can drop early input."""
    binary = binary or shutil.which("herdr")
    if not binary:
        return False
    session = session if session is not None else os.environ.get("HERDR_SESSION")
    deadline = time.monotonic() + timeout
    ansi = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
    while time.monotonic() < deadline:
        prefix = [binary]
        cmd = [*prefix, *(["--session", session] if session else []), "pane", "process-info", "--pane", pane_id]
        try: result = subprocess.run(cmd, text=True, capture_output=True, timeout=2, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired): result = None
        try:
            info = ((json.loads(result.stdout).get("result") or {}).get("process_info")
                    if result and result.returncode == 0 else None)
        except (json.JSONDecodeError, AttributeError):
            info = None
        processes = (info or {}).get("foreground_processes") or []
        shell_pid = (info or {}).get("shell_pid")
        if shell_pid and len(processes) == 1 and processes[0].get("pid") == shell_pid:
            return True
        read_cmd = [*prefix, *(["--session", session] if session else []), "pane", "read", pane_id,
                    "--source", "recent-unwrapped", "--lines", "10"]
        try: read = subprocess.run(read_cmd, text=True, capture_output=True, timeout=2, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired): read = None
        text = ansi.sub("", read.stdout if read and read.returncode == 0 else "").rstrip()
        if text and re.search(r"(?:[$%#>]|❯|➜)\s*$", text):
            return True
        time.sleep(0.05)
    return False


def focused_tab(workspace_id: str) -> str | None:
    """Return the caller's tab without treating an unavailable UI query as truth."""
    listed = _cli("tab", "list", "--workspace", workspace_id)
    tabs = (listed or {}).get("result", {}).get("tabs") or []
    live = next((tab.get("tab_id") for tab in tabs if tab.get("focused") and tab.get("tab_id")), None)
    return live or os.environ.get("HERDR_TAB_ID")


def focus_tab(tab_id: str | None) -> bool:
    """Focus a positively identified tab; Herdr 0.8 only drains pane input when focused."""
    return bool(tab_id) and _cli("tab", "focus", str(tab_id)) is not None


def shell_roundtrip(pane_id: str, timeout: float = 5.0, *, session: str | None = None,
                    binary: str | None = None) -> bool:
    """Prove that a focused pane ran a process and returned to the same shell."""
    binary = binary or shutil.which("herdr")
    if not binary:
        return False
    session = session if session is not None else os.environ.get("HERDR_SESSION")
    args = [binary, *(["--session", session] if session else []),
            "pane", "run", pane_id, "/bin/sleep 0.5"]
    try:
        submitted = subprocess.run(args, text=True, capture_output=True, timeout=2, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if submitted.returncode != 0:
        return False
    deadline = time.monotonic() + timeout
    observed_child = False
    shell_pid = None
    while time.monotonic() < deadline:
        info_args = [binary, *(["--session", session] if session else []),
                     "pane", "process-info", "--pane", pane_id]
        try:
            result = subprocess.run(info_args, text=True, capture_output=True, timeout=2, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            result = None
        try:
            info = ((json.loads(result.stdout).get("result") or {}).get("process_info")
                    if result and result.returncode == 0 else None)
        except (json.JSONDecodeError, AttributeError):
            info = None
        if info:
            shell_pid = shell_pid or info.get("shell_pid")
            processes = info.get("foreground_processes") or []
            if shell_pid and any(process.get("pid") != shell_pid for process in processes):
                observed_child = True
            elif (observed_child and shell_pid and len(processes) == 1 and
                  processes[0].get("pid") == shell_pid):
                return True
        time.sleep(0.05)
    return False


def bind_pi_wrapper(pane_id: str, wrapper: str) -> bool:
    """Bind Herdr's canonical `pi` command to the capability wrapper in this shell.

    Interactive shell startup may rebuild PATH after Herdr applies pane
    environment variables. A pane-local function keeps the canonical Herdr
    agent kind while making the executable boundary explicit and ephemeral.
    The runtime forbidden-write proof remains the final authority.
    """
    path = Path(wrapper).resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        return False
    definition = f"pi() {{ {shlex.quote(str(path))} \"$@\"; }}"
    return (_cli("pane", "run", pane_id, definition) is not None
            and shell_roundtrip(pane_id))


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
DEAD_STATES = {"dead", "exited", "stopped", "failed", "error", "closed", "terminated"}
OPEN_LAUNCH_STATES = {"reserved", "tab-created", "attested", "close-unknown"}


def agent_liveness(target: str) -> dict:
    """Classify a Herdr identity without collapsing transport uncertainty into death."""
    if not inside():
        return {"state": "unknown", "reason": "Herdr is not reachable from this process"}
    session = os.environ.get("HERDR_SESSION")
    cmd = ["herdr", *(["--session", session] if session else []), "agent", "get", target]
    try:
        result = subprocess.run(cmd, text=True, capture_output=True, timeout=20,
                                stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return {"state": "unknown", "reason": "Herdr liveness transport failed"}
    if result.returncode:
        try:
            failure = json.loads(result.stderr or result.stdout)
            code = (failure.get("error") or {}).get("code")
        except (json.JSONDecodeError, AttributeError):
            code = None
        if code == "agent_not_found":
            return {"state": "dead", "status": "absent",
                    "reason": "reachable Herdr positively reports the agent absent"}
        return {"state": "unknown", "reason": f"Herdr liveness probe failed ({code or 'unclassified'})"}
    try:
        out = json.loads(result.stdout) if result.stdout.strip() else {}
    except json.JSONDecodeError:
        return {"state": "unknown", "reason": "Herdr liveness response was invalid"}
    agent = (out.get("result") or {}).get("agent")
    if not isinstance(agent, dict):
        return {"state": "unknown", "reason": "Herdr returned no agent identity"}
    status = agent.get("agent_status") or agent.get("status") or agent.get("state")
    if status in LIVE_STATES and agent.get("pane_id"):
        return {"state": "live", "status": status, "agent": agent}
    if status in DEAD_STATES:
        return {"state": "dead", "status": status, "agent": agent}
    return {"state": "unknown", "status": status, "agent": agent,
            "reason": f"unrecognized Herdr state {status!r}"}


def session_evidence(agent: dict) -> dict:
    """Read authoritative model, thinking and usage from the durable Pi JSONL."""
    raw = agent.get("agent_session_path")
    if not raw:
        return {}
    path = Path(str(raw))
    if not path.is_absolute() or not path.is_file():
        return {}
    evidence = {"agent_session_path": str(path)}
    total_tokens, total_cost = 0, 0.0
    saw_tokens = saw_cost = False
    try:
        with path.open() as handle:
            for line in handle:
                try: event = json.loads(line)
                except (json.JSONDecodeError, UnicodeError): continue
                if event.get("type") == "session" and event.get("id"):
                    evidence["agent_session_id"] = event["id"]
                elif event.get("type") == "model_change":
                    provider, model = event.get("provider"), event.get("modelId")
                    if provider and model: evidence["model"] = f"{provider}/{model}"
                elif event.get("type") == "thinking_level_change" and event.get("thinkingLevel"):
                    evidence["thinking"] = event["thinkingLevel"]
                message = event.get("message") or {}
                usage = message.get("usage") or {}
                if message.get("role") == "assistant" and isinstance(usage.get("totalTokens"), (int, float)):
                    total_tokens += usage["totalTokens"]; saw_tokens = True
                    cost = usage.get("cost") or {}
                    if isinstance(cost.get("total"), (int, float)):
                        total_cost += cost["total"]; saw_cost = True
    except OSError:
        return {}
    if saw_tokens: evidence["tokens"] = total_tokens
    if saw_cost: evidence["cost"] = total_cost
    return evidence


def agent_get(target: str, identity: dict | None = None) -> dict | None:
    """Return only a positively live agent; a stale/dead/unknown record is not reconnectable."""
    evidence = agent_liveness(target)
    if evidence["state"] != "live":
        return None
    agent = {**(identity or {}), **evidence["agent"]}
    return {**agent, **session_evidence(agent), **runtime_activity(agent)}


def _reported_model(agent: dict) -> str | None:
    model = agent.get("model") or (agent.get("metadata") or {}).get("model")
    provider = agent.get("provider") or (agent.get("metadata") or {}).get("provider")
    if model and "/" not in str(model) and provider:
        return f"{provider}/{model}"
    return str(model) if model else None


def _verify_runtime_attestation(agent: dict, model: str, thinking: str) -> dict | None:
    """Verify evidence emitted by the actual Pi process and bind it to the Herdr pane."""
    raw_path = agent.get("attestation_path")
    if not raw_path:
        return None
    from .util import HelmError
    path = Path(str(raw_path))
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 64_000:
            raise HelmError("Pi runtime attestation is missing, linked, or oversized")
        raw = path.read_bytes()
        data = json.loads(raw)
    except HelmError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError) as exc:
        raise HelmError(f"Pi runtime attestation is unreadable: {type(exc).__name__}")
    expected = {
        "schema": 1,
        "nonce": agent.get("attestation_nonce"),
        "session_id": agent.get("agent_session_id"),
        "model": model,
        "thinking": thinking,
        "cwd": agent.get("agent_cwd"),
    }
    for key, value in expected.items():
        if value is None or data.get(key) != value:
            raise HelmError(f"Pi runtime attestation disagrees on {key}")
    from . import sandbox
    sandbox_evidence = data.get("sandbox") or {}
    if (sandbox_evidence.get("required") is not True
            or sandbox_evidence.get("probe_blocked") is not True
            or sandbox_evidence.get("profile_sha256") != agent.get("sandbox_profile_sha256")
            or sandbox_evidence.get("tool_profile_sha256") != agent.get("sandbox_tool_profile_sha256")
            or not sandbox.verify_record(agent) or not sandbox.verify_tool_record(agent)):
        raise HelmError("Pi runtime did not prove the pinned macOS write sandbox")
    try:
        public_der = base64.b64decode(str(data.get("event_public_key") or ""), validate=True)
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.hazmat.primitives.serialization import load_der_public_key
        public_key = load_der_public_key(public_der)
        if not isinstance(public_key, Ed25519PublicKey):
            raise ValueError("not Ed25519")
    except (ValueError, TypeError, ImportError) as exc:
        raise HelmError("Pi runtime attestation has no verifiable Ed25519 event key") from exc
    try:
        pid = int(data.get("pid"))
        session_dir = Path(str(data.get("session_dir"))).resolve()
        session_file = Path(str(data.get("session_file"))).resolve()
    except (TypeError, ValueError, OSError):
        raise HelmError("Pi runtime attestation contains invalid process/session paths")
    if pid <= 0 or session_file.parent != session_dir or not session_file.name.endswith(f"_{data['session_id']}.jsonl"):
        raise HelmError("Pi runtime attestation contains an escaped or invalid session file")
    events_path = Path(str(data.get("events_file"))).resolve()
    expected_events = Path(str(agent.get("agent_events_path"))).resolve()
    if events_path != expected_events or events_path.parent != path.parent or events_path.is_symlink() or not events_path.is_file():
        raise HelmError("Pi runtime event ledger is missing or escaped its attestation directory")
    path_stat, events_stat = path.stat(), events_path.stat()
    if (path_stat.st_uid != os.getuid() or events_stat.st_uid != os.getuid()
            or path_stat.st_mode & 0o077 or events_stat.st_mode & 0o077
            or path_stat.st_nlink != 1 or events_stat.st_nlink != 1):
        raise HelmError("Pi runtime evidence ownership, permissions, or link count is unsafe")
    herdr_evidence = data.get("herdr") or {}
    if herdr_evidence.get("pane_id") and herdr_evidence.get("pane_id") != agent.get("pane_id"):
        raise HelmError("Pi runtime attestation is bound to a different Herdr pane")
    process = _cli("pane", "process-info", "--pane", str(agent.get("pane_id")))
    info = ((process or {}).get("result") or {}).get("process_info") or {}
    foreground = info.get("foreground_processes") or []
    if pid not in {entry.get("pid") for entry in foreground}:
        raise HelmError("Pi runtime attestation PID is not the live Herdr pane process")
    digest = hashlib.sha256(raw).hexdigest()
    if agent.get("attestation_sha256") and agent["attestation_sha256"] != digest:
        raise HelmError("Pi runtime attestation changed after acceptance")
    return {**data, "sha256": digest, "events_inode": events_stat.st_ino,
            "events_device": events_stat.st_dev,
            "event_public_key_sha256": hashlib.sha256(public_der).hexdigest()}


def _runtime_events(agent: dict) -> list[dict]:
    """Read the transcript-free Pi runtime ledger and reject malformed identity events."""
    raw_path = agent.get("agent_events_path")
    if not raw_path:
        return []
    from .util import HelmError
    path = Path(str(raw_path))
    try:
        stat = path.stat()
        if (path.is_symlink() or not path.is_file() or stat.st_size > 16_000_000
                or stat.st_uid != os.getuid() or stat.st_mode & 0o077 or stat.st_nlink != 1
                or (agent.get("agent_events_inode") is not None and stat.st_ino != agent.get("agent_events_inode"))
                or (agent.get("agent_events_device") is not None and stat.st_dev != agent.get("agent_events_device"))):
            raise HelmError("Pi runtime event ledger is missing, linked, or oversized")
        raw = path.read_text()
    except HelmError:
        raise
    except (OSError, UnicodeError) as exc:
        raise HelmError(f"Pi runtime event ledger is unreadable: {type(exc).__name__}")
    try:
        public_der = base64.b64decode(str(agent.get("event_public_key") or ""), validate=True)
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.hazmat.primitives.serialization import load_der_public_key
        public_key = load_der_public_key(public_der)
        if not isinstance(public_key, Ed25519PublicKey):
            raise ValueError("not Ed25519")
    except (ValueError, TypeError, ImportError) as exc:
        raise HelmError("Pi runtime event ledger has no attested Ed25519 key") from exc
    events, latest_input, previous_sha, event_sequence = [], 0, "0" * 64, 0
    lifecycle: dict[int, str] = {}
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            raise HelmError("Pi runtime event ledger contains invalid JSON")
        sequence = event.get("input_sequence")
        event_type = event.get("type")
        event_sequence += 1
        unsigned = {key: value for key, value in event.items() if key not in {"event_sha256", "signature"}}
        canonical = json.dumps(unsigned, ensure_ascii=False, separators=(",", ":")).encode()
        digest = hashlib.sha256(canonical).hexdigest()
        try:
            signature = base64.b64decode(str(event.get("signature") or ""), validate=True)
            public_key.verify(signature, bytes.fromhex(digest))
        except (ValueError, TypeError, InvalidSignature) as exc:
            raise HelmError("Pi runtime event ledger signature is invalid") from exc
        if (event.get("event_sequence") != event_sequence or event.get("previous_sha256") != previous_sha
                or event.get("event_sha256") != digest):
            raise HelmError("Pi runtime event ledger hash chain is invalid")
        previous_sha = digest
        if (event.get("schema") != 1 or event.get("nonce") != agent.get("attestation_nonce") or
                event.get("session_id") != agent.get("agent_session_id") or
                event_type not in {"input", "agent_start", "agent_settled"} or
                not isinstance(sequence, int) or sequence <= 0):
            raise HelmError("Pi runtime event ledger contains invalid or foreign evidence")
        if event_type == "input":
            if (sequence != latest_input + 1 or lifecycle.get(sequence) is not None or
                    not re.fullmatch(r"[0-9a-f]{64}", str(event.get("prompt_sha256") or "")) or
                    not isinstance(event.get("prompt_bytes"), int) or event["prompt_bytes"] < 0):
                raise HelmError("Pi runtime event ledger contains an invalid input sequence")
            latest_input = sequence; lifecycle[sequence] = "input"
        elif sequence > latest_input or lifecycle.get(sequence) is None:
            raise HelmError("Pi runtime event ledger contains lifecycle evidence without an input")
        elif event_type == "agent_start":
            if lifecycle[sequence] != "input":
                raise HelmError("Pi runtime event ledger contains a duplicate or reordered start")
            lifecycle[sequence] = "started"
        else:
            if lifecycle[sequence] != "started":
                raise HelmError("Pi runtime event ledger contains a duplicate or reordered settle")
            lifecycle[sequence] = "settled"
        events.append(event)
    anchor_sequence, anchor_sha = _durable_runtime_anchor(agent)
    if anchor_sequence:
        if (len(events) < anchor_sequence
                or events[anchor_sequence - 1].get("event_sequence") != anchor_sequence
                or events[anchor_sequence - 1].get("event_sha256") != anchor_sha):
            raise HelmError("Pi runtime event ledger rolled back or forked behind its controller-owned anchor")
    return events


def _durable_runtime_anchor(agent: dict) -> tuple[int, str | None]:
    """Read the strongest tail receipt from controller-owned item state."""
    candidates = [agent]
    work_id = agent.get("work_id")
    session_id = agent.get("agent_session_id")
    launch_id = agent.get("launch_id")
    if work_id:
        try:
            from . import ids
            item = read_json(home() / "work" / ids.work(str(work_id)) / "item.json") or {}
            session = item.get("session") or {}
            if session.get("agent_session_id") == session_id:
                candidates.append(session)
            launch = next((entry for entry in item.get("agent_launches") or []
                           if ((launch_id and entry.get("launch_id") == launch_id)
                               or (not launch_id and entry.get("agent_session_id") == session_id))), None)
            if launch: candidates.append(launch)
        except (OSError, ValueError, TypeError):
            pass
    strongest = (0, None)
    by_sequence: dict[int, str] = {}
    for record in candidates:
        sequence = record.get("runtime_anchor_event_sequence")
        digest = record.get("runtime_anchor_event_sha256")
        if sequence is None and digest is None:
            continue
        if (not isinstance(sequence, int) or isinstance(sequence, bool) or sequence <= 0
                or not re.fullmatch(r"[0-9a-f]{64}", str(digest or ""))):
            from .util import HelmError
            raise HelmError("controller-owned runtime anchor is malformed")
        if sequence in by_sequence and by_sequence[sequence] != digest:
            from .util import HelmError
            raise HelmError("controller-owned runtime anchors conflict")
        by_sequence[sequence] = str(digest)
        if sequence > strongest[0]: strongest = (sequence, str(digest))
    return strongest


def runtime_activity(agent: dict) -> dict:
    """Summarize input/settled evidence without reading or persisting prompt text."""
    if not agent.get("agent_events_path"):
        return {}
    events = _runtime_events(agent)
    inputs = [event for event in events if event["type"] == "input"]
    if not inputs:
        return {"runtime_turn_pending": False, "runtime_input_sequence": 0}
    latest = inputs[-1]["input_sequence"]
    settled = any(event["type"] == "agent_settled" and event["input_sequence"] >= latest for event in events)
    return {"runtime_turn_pending": not settled, "runtime_input_sequence": latest,
            "runtime_last_input_at": inputs[-1].get("at"),
            "runtime_anchor_event_sequence": int(events[-1]["event_sequence"]),
            "runtime_anchor_event_sha256": events[-1]["event_sha256"]}


def record_runtime_anchor(agent: dict) -> dict:
    """Monotonically pin the signed ledger tail outside the worker sandbox."""
    activity = runtime_activity(agent)
    sequence = activity.get("runtime_anchor_event_sequence")
    digest = activity.get("runtime_anchor_event_sha256")
    work_id = agent.get("work_id")
    if not sequence or not digest or not work_id:
        return {**agent, **activity}
    from . import control, ids
    if not (home() / "work" / ids.work(str(work_id)) / "item.json").is_file():
        return {**agent, **activity}
    session_id, launch_id = agent.get("agent_session_id"), agent.get("launch_id")
    def pin(item):
        targets = []
        session = item.get("session") or {}
        if session.get("agent_session_id") == session_id:
            targets.append(session)
        launch = next((entry for entry in item.get("agent_launches") or []
                       if ((launch_id and entry.get("launch_id") == launch_id)
                           or (not launch_id and entry.get("agent_session_id") == session_id))), None)
        if launch: targets.append(launch)
        if not targets:
            from .util import HelmError
            raise HelmError("runtime anchor has no matching durable agent identity")
        for target in targets:
            prior_sequence = int(target.get("runtime_anchor_event_sequence") or 0)
            prior_sha = target.get("runtime_anchor_event_sha256")
            if prior_sequence > sequence or (prior_sequence == sequence and prior_sha not in (None, digest)):
                from .util import HelmError
                raise HelmError("runtime anchor attempted to move backward or fork")
            if sequence > prior_sequence:
                target["runtime_anchor_event_sequence"] = int(sequence)
                target["runtime_anchor_event_sha256"] = str(digest)
                target["runtime_anchor_recorded_at"] = now()
    control.cas_update(str(work_id), pin)
    return {**agent, **activity}


def _runtime_input(agent: dict, text: str, after_sequence: int) -> dict | None:
    # Herdr submits Pi's editor buffer, and Pi deliberately trims outer
    # whitespace before emitting its input event. Hash the same canonical
    # value for both the live submission and crash reconciliation.
    text = str(text).strip()
    if not text:
        return None
    wanted = hashlib.sha256(text.encode()).hexdigest()
    for event in _runtime_events(agent):
        if (event["type"] == "input" and event["input_sequence"] > after_sequence and
                event.get("prompt_sha256") == wanted and event.get("prompt_bytes") == len(text.encode()) and
                event.get("model") == agent.get("model") and event.get("thinking") == agent.get("thinking")):
            return event
    return None


def accepted_input(agent: dict, text: str, after_sequence: int) -> dict | None:
    """Public crash-reconciliation lookup for a hash-only accepted Pi input."""
    return _runtime_input(agent, text, after_sequence)


def _runtime_settled(agent: dict, sequence: int) -> bool:
    return any(event["type"] == "agent_settled" and event["input_sequence"] == sequence
               for event in _runtime_events(agent))


def validate_agent(agent: dict, model: str, thinking: str, *, require_durable: bool = False) -> None:
    """Fail closed when Herdr cannot attest the frozen model and thinking level."""
    from .util import HelmError
    runtime = _verify_runtime_attestation(agent, model, thinking)
    evidence = session_evidence(agent)
    actual_model = _reported_model({**agent, **evidence})
    actual_thinking = evidence.get("thinking") or agent.get("thinking") or agent.get("thinking_level") or (agent.get("metadata") or {}).get("thinking")
    if actual_model is None or actual_thinking is None:
        raise HelmError("Herdr agent omitted model/thinking metadata; refusing an unverifiable session")
    if actual_model != model or str(actual_thinking) != thinking:
        raise HelmError(f"agent drift: resolved {model} thinking={thinking}, got {actual_model} thinking={actual_thinking}")
    if not (agent.get("agent_session_id") or evidence.get("agent_session_id")):
        raise HelmError("Herdr agent omitted its durable session identity")
    if agent.get("agent_session_id") and evidence.get("agent_session_id") and agent["agent_session_id"] != evidence["agent_session_id"]:
        raise HelmError("Herdr session identity disagrees with the durable Pi session record")
    if not runtime and not evidence.get("agent_session_id"):
        raise HelmError("agent identity has neither Pi runtime attestation nor durable session evidence")
    if require_durable:
        if not (evidence.get("agent_session_id") and _reported_model(evidence) and evidence.get("thinking")):
            raise HelmError("Pi did not materialize durable session evidence after the agent turn")


def usage(agent: dict | None) -> dict:
    """Return only trustworthy numeric usage; never expose Herdr display dictionaries."""
    if not agent:
        return {}
    evidence = session_evidence(agent)
    out = {}
    for key in ("tokens", "cost"):
        value = evidence.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool): out[key] = value
    return out


def progress_marker(agent: dict | None) -> str:
    """Hash observable Pi-owned file growth without reading transcript text."""
    values = []
    for key in ("agent_session_path", "agent_events_path"):
        raw = (agent or {}).get(key)
        if not raw:
            values.append((key, None)); continue
        try:
            stat = Path(str(raw)).stat()
            values.append((key, stat.st_size, stat.st_mtime_ns, stat.st_ino))
        except OSError:
            values.append((key, "missing"))
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()


def _durable_item(item: dict) -> bool:
    from . import ids
    work_id = str(item.get("id") or "")
    return bool(ids.WORK_PATTERN.fullmatch(work_id) and (home() / "work" / work_id / "item.json").is_file())


def _pending_launch(item: dict, *, reviewer: bool) -> dict | None:
    role = "reviewer" if reviewer else "implementer"
    for launch in reversed(item.get("agent_launches") or []):
        if launch.get("role") == role and launch.get("state") in OPEN_LAUNCH_STATES:
            current_id = (item.get("session") or {}).get("agent_session_id")
            if not reviewer and launch.get("agent_session_id") == current_id:
                continue
            return launch
    return None


def _update_item_copy(item: dict, updated: dict) -> None:
    item.clear(); item.update(updated)


def _reserve_recovery(item: dict, session_id: str) -> None:
    """Consume the launch edge, not the recovery grant, before external effects."""
    from . import control
    authorization = item.get("recovery_authorized") or {}
    if (authorization.get("consumed_at") or authorization.get("started_at") or
            authorization.get("session_id") != session_id):
        from .util import HelmError
        raise HelmError("implementer is dead; explicit `helm recover` authorization is required before replacement")
    if _durable_item(item):
        def reserve(current):
            current_auth = current.get("recovery_authorized") or {}
            if (current_auth.get("consumed_at") or current_auth.get("started_at") or
                    current_auth.get("session_id") != session_id):
                from .util import HelmError
                raise HelmError("recovery authorization was already consumed or changed")
            current_auth["started_at"] = now(); current["recovery_authorized"] = current_auth
        _update_item_copy(item, control.cas_update(item["id"], reserve))
    else:
        authorization["started_at"] = now(); item["recovery_authorized"] = authorization


def _reserve_launch(item: dict, record: dict) -> None:
    if not _durable_item(item):
        return
    from . import control
    def reserve(current):
        conflict = _pending_launch(current, reviewer=record["role"] == "reviewer")
        if conflict:
            from .util import HelmError
            raise HelmError(f"unreconciled {record['role']} launch {conflict.get('launch_id')} blocks a duplicate")
        current.setdefault("agent_launches", []).append(record)
        current["agent_launches"] = current["agent_launches"][-100:]
    _update_item_copy(item, control.cas_update(item["id"], reserve))


def _update_launch(item: dict, launch_id: str, **changes) -> None:
    if not _durable_item(item):
        return
    from . import control
    def mutate(current):
        launch = next((entry for entry in current.get("agent_launches", [])
                       if entry.get("launch_id") == launch_id), None)
        if not launch:
            from .util import HelmError
            raise HelmError("agent launch journal entry disappeared")
        launch.update(changes)
    _update_item_copy(item, control.cas_update(item["id"], mutate))


def _finalize_launch(item: dict, launch_id: str, session: dict, *, reviewer: bool) -> None:
    if not _durable_item(item):
        return
    from . import control
    def finalize(current):
        launch = next((entry for entry in current.get("agent_launches", [])
                       if entry.get("launch_id") == launch_id), None)
        if not launch or launch.get("state") not in {"reserved", "tab-created", "attested"}:
            from .util import HelmError
            raise HelmError("agent launch journal changed before identity finalization")
        launch.update(state="attested", attested_at=now(), tab_id=session.get("tab_id"),
                      pane_id=session.get("pane_id"), workspace_id=session.get("workspace_id"),
                      attestation_sha256=session.get("attestation_sha256"),
                      attested_pid=session.get("attested_pid"),
                      agent_session_path=session.get("agent_session_path"),
                      agent_events_path=session.get("agent_events_path"),
                      agent_events_inode=session.get("agent_events_inode"),
                      agent_events_device=session.get("agent_events_device"),
                      event_public_key=session.get("event_public_key"),
                      event_public_key_sha256=session.get("event_public_key_sha256"),
                      sandbox_profile=session.get("sandbox_profile"),
                      sandbox_profile_sha256=session.get("sandbox_profile_sha256"),
                      sandbox_tool_profile=session.get("sandbox_tool_profile"),
                      sandbox_tool_profile_sha256=session.get("sandbox_tool_profile_sha256"),
                      sandbox_probe=session.get("sandbox_probe"),
                      sandbox_verified=session.get("sandbox_verified"),
                      resolved_model=session.get("resolved_model") or session.get("model"),
                      resolved_thinking=session.get("resolved_thinking") or session.get("thinking"))
        if reviewer:
            return
        prior = current.get("session") or {}
        replacement_for = launch.get("replacement_for")
        if prior and prior.get("agent_session_id") != session.get("agent_session_id"):
            if replacement_for != prior.get("agent_session_id"):
                from .util import HelmError
                raise HelmError("session changed while replacement was launching")
            current.setdefault("session_history", []).append(
                {**prior, "lost_at": now(), "recovered_from_checkpoint": True})
        authorization = current.get("recovery_authorized") or {}
        if replacement_for:
            if (authorization.get("session_id") != replacement_for or authorization.get("consumed_at") or
                    not authorization.get("started_at")):
                from .util import HelmError
                raise HelmError("recovery authorization changed before replacement identity was durable")
            authorization.update(consumed_at=now(), replacement_session_id=session.get("agent_session_id"))
            current["recovery_authorized"] = authorization
            for prior_launch in current.get("agent_launches", []):
                if (prior_launch is not launch and prior_launch.get("role") == "implementer"
                        and prior_launch.get("agent_session_id") == replacement_for
                        and prior_launch.get("state") in OPEN_LAUNCH_STATES):
                    prior_launch.update(state="superseded", superseded_at=now(),
                                        replacement_session_id=session.get("agent_session_id"))
        current["session"] = session
    _update_item_copy(item, control.cas_update(item["id"], finalize))


def finish_launch(session: dict, state: str = "closed") -> None:
    """Close a journal entry after an explicitly controller-owned lifecycle action."""
    work_id, launch_id = session.get("work_id"), session.get("launch_id")
    if not work_id or not launch_id:
        return
    from . import ids
    item_file = home() / "work" / ids.work(work_id) / "item.json"
    if not item_file.is_file():
        return
    from . import control
    def finish(item):
        launch = next((entry for entry in item.get("agent_launches", [])
                       if entry.get("launch_id") == launch_id), None)
        if launch and launch.get("state") in OPEN_LAUNCH_STATES:
            launch.update(state=state, finished_at=now())
    control.cas_update(str(work_id), finish)


def _reconnect_reserved_launch(item: dict, launch: dict, model: str, thinking: str) -> dict:
    """Recover only the exact attested launch; uncertainty never creates another agent."""
    from .util import HelmError
    if launch.get("model") != model or launch.get("thinking") != thinking:
        raise HelmError("reserved agent launch disagrees with the frozen model/thinking decision")
    target = str(launch.get("agent_name") or "")
    liveness = agent_liveness(target) if target else {"state": "unknown", "reason": "launch has no agent name"}
    if liveness.get("state") == "dead":
        _reserve_recovery(item, str(launch.get("agent_session_id") or ""))
        tab_id = launch.get("tab_id")
        if tab_id and _tab_matches(launch) and not close_tab(str(tab_id)):
            raise UnsettledAgentError("positively absent agent still has an exact tab that could not be closed")
        _update_launch(item, str(launch.get("launch_id")), state="dead-confirmed", reconciled_at=now())
        return {}
    if liveness.get("state") != "live":
        raise HelmError("reserved Herdr launch liveness is unknown; refusing a duplicate agent")
    agent = {**launch, **liveness["agent"], "agent_cwd": launch.get("cwd"),
             "attestation_nonce": launch.get("attestation_nonce")}
    runtime = _verify_runtime_attestation(agent, model, thinking)
    if not runtime:
        raise HelmError("reserved live Pi agent emitted no runtime attestation")
    agent.update(agent_session_path=runtime["session_file"], model=runtime["model"], thinking=runtime["thinking"],
                 attested_pid=runtime["pid"], attestation_sha256=runtime["sha256"],
                 runtime_attested_at=runtime["attested_at"], reconnected=True,
                 liveness_validated_at=now(), resolved_model=model, resolved_thinking=thinking,
                 work_id=item.get("id"), launch_id=launch.get("launch_id"))
    agent.update(session_evidence(agent)); agent.update(runtime_activity(agent))
    validate_agent(agent, model, thinking)
    _finalize_launch(item, str(launch.get("launch_id")), agent, reviewer=False)
    return agent


def agent_read(target: str, lines: int = 120) -> str:
    session = os.environ.get("HERDR_SESSION")
    cmd = ["herdr", *(["--session", session] if session else []),
           "agent", "read", target, "--source", "recent-unwrapped", "--lines", str(lines)]
    try:
        r = subprocess.run(cmd, text=True, capture_output=True, timeout=20, stdin=subprocess.DEVNULL)
        return r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def ensure_agent(item: dict, cwd: Path, model: str, thinking: str, *, reviewer: bool = False,
                 agent_env: dict[str, str] | None = None) -> dict:
    """Reconnect a live identity; start a replacement only from one-use recovery authority."""
    if not inside():
        from .util import HelmError
        raise HelmError("persistent execution requires Herdr (start with pi-firstmate); no fallback session was fabricated")
    if _durable_item(item):
        # Reviewer finalization and live controls use independent CAS writes.
        # Always reason from the latest journal rather than a caller's stale
        # in-memory copy before deciding whether another process may start.
        from . import ids
        latest = read_json(home() / "work" / ids.work(item["id"]) / "item.json")
        if latest:
            _update_item_copy(item, latest)
    existing = item.get("session") or {}
    replacement_for = None
    if not reviewer and existing.get("agent_name"):
        liveness = agent_liveness(existing["agent_name"])
        live = ({**existing, **liveness["agent"],
                 **session_evidence({**existing, **liveness["agent"]}),
                 **runtime_activity({**existing, **liveness["agent"]})}
                if liveness.get("state") == "live" else None)
        if live:
            validate_agent(live, model, thinking)
            if existing.get("agent_session_id") and live.get("agent_session_id") != existing["agent_session_id"]:
                from .util import HelmError
                raise HelmError("Herdr agent name now refers to a different session; refusing identity drift")
        if live:
            return {**existing, **live, "reconnected": True, "liveness_validated_at": now()}
        from .util import HelmError
        if liveness.get("state") != "dead":
            raise HelmError("existing Herdr session liveness is unknown; refusing an automatic replacement")
        replacement_for = str(existing.get("agent_session_id"))
        _reserve_recovery(item, replacement_for)
    elif _durable_item(item):
        pending = _pending_launch(item, reviewer=reviewer)
        if pending:
            if reviewer:
                from .util import HelmError
                raise HelmError(f"unreconciled reviewer launch {pending.get('launch_id')} blocks a fresh reviewer")
            recovered = _reconnect_reserved_launch(item, pending, model, thinking)
            if recovered:
                return recovered
            replacement_for = str(pending.get("agent_session_id"))
    import re
    base = re.sub(r"[^a-z0-9_-]", "-", item["id"].lower())[-24:].lstrip("-") or "item"
    name = (("review-" + secrets.token_hex(3)) if reviewer else "impl-" + base)[:32]
    launch_id = str(uuid.uuid4())
    nonce = secrets.token_hex(24)
    from . import ids
    state_dir = home() / "work" / ids.work(item["id"])
    session_dir = state_dir / "agent-sessions" / name / launch_id
    attestation_dir = state_dir / "agent-attestations" / f"{name}-{nonce}"
    attestation_path = attestation_dir / "attestation.json"
    events_path = attestation_dir / "events.jsonl"
    from . import sandbox
    exact_writes = [value for value in str((agent_env or {}).get("HELM_AGENT_ALLOWED_WRITES") or "").splitlines()
                    if value]
    sandbox_record = sandbox.prepare(
        work_id=item["id"], launch_id=launch_id, cwd=cwd, session_dir=session_dir,
        attestation_dir=attestation_dir,
        writable_worktree=(not reviewer and item.get("kind") != "scout"),
        exact_writes=exact_writes,
        pi_source_dir=Path(os.environ.get("PI_CODING_AGENT_DIR") or home() / "pi"),
    )
    label = ("review " if reviewer else "work ") + item["project"] + f" · {launch_id[:8]}"
    launch_record = {
        "schema": 1, "launch_id": launch_id, "agent_session_id": launch_id,
        "agent_name": name, "role": "reviewer" if reviewer else "implementer",
        "state": "reserved", "reserved_at": now(), "cwd": str(cwd.resolve()),
        "model": model, "thinking": thinking, "label": label,
        "session_dir": str(session_dir), "attestation_path": str(attestation_path),
        "agent_events_path": str(events_path), "attestation_nonce": nonce,
        "sandbox_profile": sandbox_record["profile"],
        "sandbox_profile_sha256": sandbox_record["profile_sha256"],
        "sandbox_tool_profile": sandbox_record["tool_profile"],
        "sandbox_tool_profile_sha256": sandbox_record["tool_profile_sha256"],
        "sandbox_probe": sandbox_record["probe"],
        "sandbox_pi_dir": sandbox_record["pi_dir"],
        "sandbox_pi_config_files": sandbox_record["pi_config_files"],
        "replacement_for": replacement_for,
    }
    _reserve_launch(item, launch_record)
    session_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    attestation_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    pane_env = {key: value for key, value in (agent_env or {}).items()
                if (key == "GIT_CONFIG_COUNT" or key.startswith("GIT_CONFIG_KEY_")
                    or key.startswith("GIT_CONFIG_VALUE_") or key == "HELM_AGENT_ALLOWED_WRITES")}
    pane_env["GIT_OPTIONAL_LOCKS"] = "0"
    tab = open_tab(label, "", cwd,
                   restore_focus=False, extra_env={**pane_env,
                       "HELM_AGENT_ATTESTATION": str(attestation_path),
                       "HELM_AGENT_EVENTS": str(events_path),
                       "HELM_AGENT_NONCE": nonce,
                       "HELM_AGENT_ROOT": str(cwd.resolve()),
                       "HELM_SANDBOX_REQUIRED": "1",
                       "HELM_SANDBOX_PROFILE": sandbox_record["profile"],
                       "HELM_SANDBOX_PROFILE_SHA256": sandbox_record["profile_sha256"],
                       "HELM_TOOL_SANDBOX_PROFILE": sandbox_record["tool_profile"],
                       "HELM_TOOL_SANDBOX_PROFILE_SHA256": sandbox_record["tool_profile_sha256"],
                       "HELM_TOOL_RUNNER": sandbox_record["tool_runner"],
                       "HELM_SANDBOX_PROBE": sandbox_record["probe"],
                       "HELM_REAL_PI": sandbox_record["real_pi"],
                       "PI_CODING_AGENT_DIR": sandbox_record["pi_dir"],
                       "TMPDIR": sandbox_record["tmpdir"],
                       "PATH": sandbox_record["wrapper_dir"] + os.pathsep + os.environ.get("PATH", ""),
                   })
    if not tab:
        from .util import HelmError
        raise HelmError("Herdr could not create an agent tab")
    return_tab = tab.pop("return_tab_id", None)
    try:
        _update_launch(item, launch_id, state="tab-created", tab_id=tab["tab_id"],
                       pane_id=tab["pane_id"], workspace_id=tab.get("workspace_id"),
                       herdr_session=tab.get("herdr_session"), tab_created_at=now())
    except BaseException:
        close_tab(tab["tab_id"]); focus_tab(return_tab)
        raise
    # The new tab remains focused through agent start. Herdr 0.8 accepts pane
    # input while unfocused but does not drain it, so restoring focus earlier
    # can leave an apparently successful yet empty worker tab.
    if not shell_roundtrip(tab["pane_id"]):
        close_tab(tab["tab_id"]); focus_tab(return_tab)
        from .util import HelmError
        raise HelmError("Herdr agent tab did not complete an interactive shell round trip")
    if not bind_pi_wrapper(tab["pane_id"], str(Path(sandbox_record["wrapper_dir"]) / "pi")):
        close_tab(tab["tab_id"]); focus_tab(return_tab)
        from .util import HelmError
        raise HelmError("Herdr agent tab could not bind the managed Pi capability wrapper")
    args = ["agent", "start", name, "--kind", "pi", "--pane", tab["pane_id"], "--timeout", "60000",
            "--", "--approve", "--model", model, "--thinking", thinking,
            "--session-id", launch_id, "--session-dir", str(session_dir),
            "--extension", str(REPO / "helm" / "pi_attest.ts")]
    try:
        agent = _result(cli_required(*args), "agent")
        # The extension runs inside the actual Pi process before Herdr reports
        # readiness. Read it independently; launch argv is never identity proof.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not attestation_path.is_file():
            time.sleep(0.1)
        agent = {**agent, "agent_name": agent.get("name") or name,
                 "agent_session_id": launch_id, "agent_session_path": str(session_dir / f"pending_{launch_id}.jsonl"),
                 "agent_cwd": str(cwd.resolve()), "attestation_path": str(attestation_path),
                 "agent_events_path": str(events_path), "attestation_nonce": nonce,
                 "sandbox_profile": sandbox_record["profile"],
                 "sandbox_profile_sha256": sandbox_record["profile_sha256"],
                 "sandbox_tool_profile": sandbox_record["tool_profile"],
                 "sandbox_tool_profile_sha256": sandbox_record["tool_profile_sha256"],
                 "sandbox_probe": sandbox_record["probe"],
                 "sandbox_pi_dir": sandbox_record["pi_dir"],
                 "sandbox_pi_config_files": sandbox_record["pi_config_files"],
                 "work_id": item.get("id"), "launch_id": launch_id,
                 "role": "reviewer" if reviewer else "implementer"}
        runtime = _verify_runtime_attestation(agent, model, thinking)
        if not runtime:
            from .util import HelmError
            raise HelmError("Pi emitted no runtime attestation")
        agent.update(agent_session_path=runtime["session_file"], model=runtime["model"],
                     thinking=runtime["thinking"], attested_pid=runtime["pid"],
                     attestation_sha256=runtime["sha256"], runtime_attested_at=runtime["attested_at"],
                     sandbox_verified=True, event_public_key=runtime["event_public_key"],
                     event_public_key_sha256=runtime["event_public_key_sha256"])
        agent.update(agent_events_inode=runtime["events_inode"], agent_events_device=runtime["events_device"])
        validate_agent(agent, model, thinking)
        session = {**tab, **agent, "agent_name": agent.get("name") or name, "created": now(),
                   "reconnected": False, "liveness_validated_at": now(), "resolved_model": model,
                   "resolved_thinking": thinking}
        _finalize_launch(item, launch_id, session, reviewer=reviewer)
    except BaseException as exc:
        closed = close_tab(tab["tab_id"])
        if closed:
            try:
                _update_launch(item, launch_id, state="closed-after-launch-failure", reconciled_at=now())
            except BaseException:
                raise UnsettledAgentError(
                    "agent tab closed after launch failure but the durable launch journal could not be finalized"
                ) from exc
            raise
        raise UnsettledAgentError(
            "agent launch failed and its exact Herdr tab could not be proven closed"
        ) from exc
    finally:
        focus_tab(return_tab)
    # The planned ID remains explicitly `reserved` until the actual Pi process
    # attests it. Only this finalized Herdr/Pi identity is returned as a session.
    return session


def prompt_agent(target: str, text: str, timeout: int, identity: dict | None = None) -> dict:
    submitted = prompt_agent_async(target, text, identity)
    sequence = submitted.get("_runtime_input_sequence")
    if identity and sequence:
        agent, finding = _wait_runtime_monitored(target, timeout, lambda: None, submitted, sequence)
        if finding:
            from .util import HelmError
            raise HelmError("unexpected control finding while waiting for agent")
        return agent
    return wait_agent(target, timeout)


def prompt_agent_async(target: str, text: str, identity: dict | None = None) -> dict:
    """Prove input submission, then restore the captain without waiting for completion."""
    from .util import HelmError
    text = str(text).strip()
    if not text:
        raise HelmError("refusing to submit an empty agent prompt")
    before = agent_liveness(target)
    if before.get("state") != "live":
        raise HelmError("cannot submit input without a positively live Herdr agent")
    before_agent = before["agent"]
    before_seq = before_agent.get("state_change_seq")
    before_status = before_agent.get("agent_status") or before_agent.get("status")
    baseline = (runtime_activity(identity or {}).get("runtime_input_sequence", 0)
                if identity and identity.get("agent_events_path") else 0)
    require_runtime = bool(identity and identity.get("agent_events_path"))
    workspace = os.environ.get("HERDR_WORKSPACE_ID")
    return_tab = focused_tab(workspace) if workspace else os.environ.get("HERDR_TAB_ID")
    cli_required("agent", "focus", target)
    external_attempted = False
    try:
        # Once this call begins, a transport/response/evidence failure cannot
        # prove that Pi did not accept the text. Managed items must retain
        # their collision claim until explicit recovery resolves that state.
        external_attempted = True
        submitted = _result(cli_required("agent", "prompt", target, text), "agent")
        latest = submitted
        delivered = False
        runtime_event = None
        deadline = time.monotonic() + 0.75
        while time.monotonic() < deadline:
            if require_runtime:
                runtime_event = _runtime_input(identity or {}, text, baseline)
                if runtime_event:
                    delivered = True; break
            probe = agent_liveness(target)
            if probe.get("state") == "live":
                latest = probe["agent"]
                seq = latest.get("state_change_seq")
                status = latest.get("agent_status") or latest.get("status")
                if ((before_seq is not None and seq is not None and seq != before_seq) or
                        (before_status == "idle" and status == "working")):
                    if not require_runtime:
                        delivered = True; break
            time.sleep(0.05)
        if not delivered:
            # Herdr 0.8 can report agent_prompted after populating Pi's editor
            # without submitting it. Enter is safe after a proven unchanged
            # state: for a working agent it queues the follow-up; for idle it
            # starts the requested turn.
            cli_required("agent", "send-keys", target, "enter")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if require_runtime:
                    runtime_event = _runtime_input(identity or {}, text, baseline)
                    if runtime_event:
                        delivered = True; break
                probe = agent_liveness(target)
                if probe.get("state") == "live":
                    latest = probe["agent"]
                    seq = latest.get("state_change_seq")
                    status = latest.get("agent_status") or latest.get("status")
                    if (not require_runtime and ((before_seq is not None and seq is not None and seq != before_seq) or
                                                 (before_status == "idle" and status == "working"))):
                        delivered = True; break
                time.sleep(0.05)
            if not delivered:
                raise UnsettledAgentError(
                    "Herdr prompt outcome is unknown: Pi emitted no matching input evidence"
                )
        result = {**(identity or {}), **latest,
                  **({"_runtime_input_sequence": runtime_event["input_sequence"], "runtime_turn_pending": True}
                     if runtime_event else {})}
        return record_runtime_anchor(result) if runtime_event else result
    except UnsettledAgentError:
        raise
    except BaseException as exc:
        if require_runtime and external_attempted:
            raise UnsettledAgentError(
                f"Herdr prompt outcome is unknown after submission began: {type(exc).__name__}"
            ) from exc
        raise
    finally:
        focus_tab(return_tab)


def wait_agent(target: str, timeout: int) -> dict:
    return _result(cli_required("agent", "wait", target, "--timeout", str(timeout * 1000)), "agent")


def _wait_runtime_monitored_inner(target: str, timeout: int, monitor, identity: dict,
                                  sequence: int) -> tuple[dict, object | None]:
    """Wait on Pi's transcript-free settled event while preserving live controls."""
    deadline = time.monotonic() + timeout
    finding = None
    while time.monotonic() < deadline:
        # Steering accepted during this turn creates a later durable input.
        # Follow the newest accepted sequence so the caller cannot verify or
        # deliver before queued captain guidance has actually settled.
        sequence = max(sequence, int(runtime_activity(identity).get("runtime_input_sequence", 0)))
        if _runtime_settled(identity, sequence):
            live = agent_get(target, identity)
            if not live:
                raise HelmError("Pi settled but Herdr liveness became unknown")
            return record_runtime_anchor(live), None
        finding = monitor()
        if finding:
            interrupt_agent(target)
            break
        time.sleep(0.2)
    if not finding:
        interrupt_agent(target)
        finding = {"control": "timeout", "reason": "Pi turn reached its bounded wait"}
    settle_deadline = time.monotonic() + 30
    while time.monotonic() < settle_deadline:
        sequence = max(sequence, int(runtime_activity(identity).get("runtime_input_sequence", 0)))
        if _runtime_settled(identity, sequence):
            break
        time.sleep(0.2)
    if not _runtime_settled(identity, sequence):
        raise UnsettledAgentError("agent did not settle after cooperative interrupt; scope must remain held")
    identity = record_runtime_anchor(identity)
    if isinstance(finding, dict) and finding.get("control") in ("budget", "timeout"):
        # The interrupted turn is settled. Another checkpoint prompt would be
        # a new model turn beyond the configured cap.
        finding["agent_checkpoint"] = "settled-without-extra-model-turn"
    elif isinstance(finding, dict) and finding.get("control") in ("pause", "interrupt"):
        if not _runtime_settled(identity, sequence):
            finding["agent_checkpoint"] = "unavailable"
        else:
            try:
                prompt_agent(target,
                    "Checkpoint the current work now. Preserve every useful change, commit safe progress when possible, "
                    "summarize remaining work and blockers, then stop and wait for resume.", 120, identity)
            except BaseException:
                if runtime_activity(identity).get("runtime_turn_pending"):
                    raise UnsettledAgentError("checkpoint turn did not settle; scope must remain held")
                finding["agent_checkpoint"] = "unavailable"
            else:
                finding["agent_checkpoint"] = "complete"
    return agent_get(target, identity) or {**identity, **(agent_liveness(target).get("agent") or {})}, finding


def _wait_runtime_monitored(target: str, timeout: int, monitor, identity: dict,
                            sequence: int) -> tuple[dict, object | None]:
    """Every failure after accepted input is settlement-unknown unless proven otherwise."""
    try:
        return _wait_runtime_monitored_inner(target, timeout, monitor, identity, sequence)
    except UnsettledAgentError:
        raise
    except BaseException as exc:
        try:
            interrupt_agent(target)
        except BaseException:
            pass
        raise UnsettledAgentError(
            f"Pi turn settlement became unprovable: {type(exc).__name__}; scope must remain held"
        ) from exc


def wait_agent_monitored(target: str, timeout: int, monitor,
                         identity: dict | None = None) -> tuple[dict, object | None]:
    """Reconnect to an already-working turn without giving up live controls."""
    if identity and identity.get("agent_events_path"):
        activity = runtime_activity(identity)
        sequence = activity.get("runtime_input_sequence")
        if sequence and activity.get("runtime_turn_pending"):
            return _wait_runtime_monitored(target, timeout, monitor, identity, sequence)
    session = os.environ.get("HERDR_SESSION")
    cmd = ["herdr", *(["--session", session] if session else []),
           "agent", "wait", target, "--timeout", str(timeout * 1000)]
    proc = subprocess.Popen(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
    finding = None
    timed_out = False
    deadline = time.monotonic() + timeout + 10
    while proc.poll() is None:
        finding = monitor()
        timed_out = time.monotonic() >= deadline
        if finding or timed_out:
            interrupt_agent(target); break
        time.sleep(0.2)
    try: stdout, stderr = proc.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        proc.terminate(); stdout, stderr = proc.communicate(timeout=5)
    if finding or timed_out:
        finding = finding or {"control": "timeout", "reason": "Herdr turn reached its bounded wait"}
        try:
            settled = wait_agent(target, 30)
            if isinstance(finding, dict) and finding.get("control") in ("budget", "timeout"):
                finding["agent_checkpoint"] = "settled-without-extra-model-turn"
            else:
                prompt_agent(target,
                    "Checkpoint the current work now. Preserve every useful change, commit safe progress when possible, "
                    "summarize remaining work and blockers, then stop and wait for resume.", 120, identity)
        except BaseException as exc:
            raise UnsettledAgentError("legacy Herdr agent did not prove a settled checkpoint") from exc
        else: finding.setdefault("agent_checkpoint", "complete")
        return agent_get(target, identity) or settled or {}, finding
    if proc.returncode != 0:
        raise UnsettledAgentError(
            f"Herdr agent wait ended without positive settlement: {(stderr or stdout).strip()[-500:]}"
        )
    try: return _result(json.loads(stdout), "agent"), None
    except (json.JSONDecodeError, TypeError):
        raise UnsettledAgentError("Herdr agent wait returned no real settled identity")


def prompt_agent_monitored(target: str, text: str, timeout: int, monitor,
                           identity: dict | None = None) -> tuple[dict, object | None]:
    """Wait for a prompt while polling a safety monitor; cooperatively interrupt on escape."""
    submitted = prompt_agent_async(target, text, identity)
    sequence = submitted.get("_runtime_input_sequence")
    if identity and sequence:
        return _wait_runtime_monitored(target, timeout, monitor, submitted, sequence)
    return wait_agent_monitored(target, timeout, monitor, identity)


def steer_agent(target: str, text: str, timeout: int = 300,
                identity: dict | None = None) -> dict:
    return prompt_agent_async(target, "STEERING FROM THE CAPTAIN:\n" + text, identity)


def interrupt_agent(target: str) -> None:
    workspace = os.environ.get("HERDR_WORKSPACE_ID")
    return_tab = focused_tab(workspace) if workspace else os.environ.get("HERDR_TAB_ID")
    cli_required("agent", "focus", target)
    try:
        cli_required("agent", "send-keys", target, "esc")
    finally:
        focus_tab(return_tab)


def close_agent_tab(session: dict) -> bool:
    if session.get("agent_name"):
        try:
            liveness = agent_liveness(session["agent_name"])
            if liveness.get("state") == "dead":
                # A positively absent agent can still leave an empty owned tab.
                # Close that tab only when the durable session/workspace/label
                # identity still matches; a reused ID is somebody else's tab.
                if session.get("tab_id") and _tab_matches(session) and not close_tab(session["tab_id"]):
                    return False
                finish_launch(session, "dead-confirmed")
                return True
            if liveness.get("state") != "live":
                return False
            live = {**session, **(liveness.get("agent") or {})}
            live.update(session_evidence(live)); activity = runtime_activity(live); live.update(activity)
            proven = bool(live.get("pane_id") == session.get("pane_id")
                          and live.get("tab_id") == session.get("tab_id")
                          and live.get("agent_session_id") == session.get("agent_session_id")
                          and activity.get("runtime_turn_pending") is False)
            if not proven:
                return False
        except BaseException:
            return False
    else:
        # Agent tabs must carry an exact agent identity. A label alone is not
        # settlement proof and may refer to a reused live tab.
        return False
    if session.get("tab_id") and close_tab(session["tab_id"]):
        finish_launch(session)
        return True
    return False


def _tab_matches(record: dict) -> bool:
    tab_id, workspace = record.get("tab_id"), record.get("workspace_id") or os.environ.get("HERDR_WORKSPACE_ID")
    if not tab_id or not workspace:
        return False
    recorded_session = record.get("herdr_session")
    current_session = os.environ.get("HERDR_SESSION")
    if recorded_session and recorded_session != current_session:
        return False
    recorded_workspace = record.get("workspace_id")
    current_workspace = os.environ.get("HERDR_WORKSPACE_ID")
    if recorded_workspace and current_workspace and recorded_workspace != current_workspace:
        return False
    result = _cli("tab", "list", "--workspace", str(workspace))
    tabs = ((result or {}).get("result") or {}).get("tabs") or []
    current = next((tab for tab in tabs if tab.get("tab_id") == tab_id), None)
    if not current:
        return False
    return bool(record.get("label") and current.get("label") == record.get("label"))


def open_tab(label: str, command: str, cwd: Path | None = None, *, restore_focus: bool = True,
             extra_env: dict[str, str] | None = None) -> dict | None:
    """Create a tab beside the captain and run `command` in it. Returns {tab_id, pane_id}."""
    workspace = os.environ["HERDR_WORKSPACE_ID"]
    return_tab = focused_tab(workspace)
    pane_environment: dict[str, str] = {}
    for k in PASS_ENV:
        v = os.environ.get(k) or (str(home()) if k == "HELM_HOME" else None)
        if v:
            pane_environment[k] = v
    for key, value in sorted((extra_env or {}).items()):
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or "\x00" in value:
            from .util import HelmError
            raise HelmError("invalid Herdr pane environment request")
        # A launch-specific capability must replace, never duplicate, an
        # inherited variable. Herdr deliberately keeps the first duplicate.
        pane_environment[key] = value
    env_args = [part for key, value in sorted(pane_environment.items())
                for part in ("--env", f"{key}={value}")]
    out = _cli("tab", "create", "--workspace", workspace,
               "--cwd", str(cwd or REPO), "--label", label, "--no-focus", *env_args)
    if not out:
        return None
    tab = (out.get("result") or {}).get("tab", {}).get("tab_id")
    pane = (out.get("result") or {}).get("root_pane", {}).get("pane_id")
    if not (tab and pane):
        return None
    # Herdr 0.8 queues pane input in a --no-focus tab. Focus is therefore part
    # of command delivery, not presentation: do it before reporting success.
    if not focus_tab(tab) or not wait_shell(pane):
        close_tab(tab)
        focus_tab(return_tab)
        return None
    if command and _cli("pane", "run", pane, command) is None:
        close_tab(tab)
        focus_tab(return_tab)
        return None
    if restore_focus:
        # Once pane run has written the command and Enter to the focused PTY,
        # it continues independently when the captain regains focus.
        time.sleep(0.05)
        focus_tab(return_tab)
    log(f"herdr: opened tab {tab} '{label}'", console=False)
    return {"tab_id": tab, "pane_id": pane, "label": label, "workspace_id": workspace,
            "herdr_session": os.environ.get("HERDR_SESSION"), "return_tab_id": return_tab}


def close_tab(tab_id: str) -> bool:
    closed = _cli("tab", "close", tab_id) is not None
    log(f"herdr: closed tab {tab_id}", console=False)
    return closed


def notify(title: str, body: str = "") -> None:
    if shutil.which("herdr") and os.environ.get("HERDR_ENV") == "1":
        _cli("notification", "show", title, *(["--body", body] if body else []))


# ------------------------------------------------------------------ durable tab registry

def _state_path() -> Path: return home() / "herdr.json"


def remember(kind: str, rec: dict) -> None:
    with locked(home() / "herdr.lock"):
        st = read_json(_state_path(), {"tabs": []})
        st["tabs"].append({"kind": kind, "herdr_session": os.environ.get("HERDR_SESSION"), **rec})
        write_json(_state_path(), st)


def forget(tab_id: str) -> None:
    with locked(home() / "herdr.lock"):
        st = read_json(_state_path(), {"tabs": []})
        st["tabs"] = [t for t in st["tabs"] if t.get("tab_id") != tab_id]
        write_json(_state_path(), st)


def close_all(kind: str | None = None) -> int:
    with locked(home() / "herdr.lock"):
        if not _state_path().is_file():
            return 0
        st = read_json(_state_path(), {"tabs": []})
        keep, n = [], 0
        for t in st["tabs"]:
            if kind and t.get("kind") != kind:
                keep.append(t); continue
            if not _tab_matches(t):
                keep.append(t); continue
            if close_tab(t["tab_id"]): n += 1
            else: keep.append(t)
        st["tabs"] = keep
        write_json(_state_path(), st)
        return n
