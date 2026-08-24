#!/usr/bin/env python3
"""Stand-in for the `herdr` CLI: records every call to $FAKE_HERDR_LOG and answers like herdr."""
import hashlib, json, os, re, subprocess, sys
from pathlib import Path
try:
    from tests._event_signing import PUBLIC_KEY_B64, sign_chain
except ImportError:
    from _event_signing import PUBLIC_KEY_B64, sign_chain
args = sys.argv[1:]
with open(os.environ["FAKE_HERDR_LOG"], "a") as fh:
    fh.write(json.dumps(args) + "\n")
n = sum(1 for _ in open(os.environ["FAKE_HERDR_LOG"]))
def process_identity(pid, owner):
    field = lambda name: subprocess.run(
        ["ps", "-p", str(pid), "-o", f"{name}="], text=True, capture_output=True, check=True
    ).stdout.strip()
    return {"version": 1, "kind": "boss-pi-agent", "pid": pid,
            "pgid": os.getpgid(pid), "owner": owner,
            "start_sha256": hashlib.sha256(field("lstart").encode()).hexdigest(),
            "command_sha256": hashlib.sha256(field("command").encode()).hexdigest(),
            "registered_at": "2026-01-01T00:00:00Z"}
def normalized(call):
    return call[2:] if call[:1] == ["--session"] else call
if args[:1] == ["--session"]:
    args = args[2:]
if args[:2] == ["tab", "create"]:
    print(json.dumps({"result": {"tab": {"tab_id": f"w1:t{n}", "label": args[args.index('--label') + 1]},
                                 "root_pane": {"pane_id": f"w1:p{n}"}, "type": "tab_created"}}))
elif args[:2] == ["agent", "start"]:
    name, pane = args[2], args[args.index("--pane") + 1]
    model = args[args.index("--model") + 1] if "--model" in args else None
    thinking = args[args.index("--thinking") + 1] if "--thinking" in args else None
    session_id = args[args.index("--session-id") + 1] if "--session-id" in args else None
    session_dir = Path(args[args.index("--session-dir") + 1]) if "--session-dir" in args else None
    pane_env, cwd = {}, None
    for index, line in enumerate(open(os.environ["FAKE_HERDR_LOG"]), 1):
        prior = normalized(json.loads(line))
        if prior[:2] == ["tab", "create"] and f"w1:p{index}" == pane:
            cwd = prior[prior.index("--cwd") + 1] if "--cwd" in prior else None
            for env_index, value in enumerate(prior):
                if value == "--env" and env_index + 1 < len(prior):
                    key, _, env_value = prior[env_index + 1].partition("="); pane_env[key] = env_value
    attestation = pane_env.get("BOSS_AGENT_ATTESTATION")
    events_file = pane_env.get("BOSS_AGENT_EVENTS")
    if attestation and session_id and session_dir and cwd:
        session_file = session_dir / f"2026-01-01T00-00-00-000Z_{session_id}.jsonl"
        agent_pid = os.getppid()
        payload = {"schema": 1, "nonce": pane_env.get("BOSS_AGENT_NONCE"), "pid": agent_pid,
                   "process_identity": process_identity(agent_pid, session_id), "cwd": str(Path(cwd).resolve()),
                   "session_id": session_id, "session_dir": str(session_dir.resolve()),
                   "session_file": str(session_file.resolve()), "events_file": str(Path(events_file).resolve()),
                   "event_public_key": PUBLIC_KEY_B64,
                   "model": model, "thinking": thinking,
                   "sandbox": {"required": pane_env.get("BOSS_SANDBOX_REQUIRED") == "1",
                               "profile_sha256": pane_env.get("BOSS_SANDBOX_PROFILE_SHA256"),
                               "tool_profile_sha256": pane_env.get("BOSS_TOOL_SANDBOX_PROFILE_SHA256"),
                               "probe_blocked": True},
                   "herdr": {"session": os.environ.get("HERDR_SESSION"), "workspace_id": "w1",
                             "tab_id": pane.replace(":p", ":t"), "pane_id": pane},
                   "reason": "startup", "attested_at": "2026-01-01T00:00:00Z"}
        Path(attestation).parent.mkdir(parents=True, exist_ok=True)
        Path(events_file).write_text("")
        Path(attestation).write_text(json.dumps(payload) + "\n")
        Path(events_file).chmod(0o600); Path(attestation).chmod(0o600)
    print(json.dumps({"result": {"agent": {"name": name, "pane_id": pane, "tab_id": pane.replace(":p", ":t"),
                                                "workspace_id": "w1", "agent_status": "idle", "state_change_seq": 1}}}))
elif args[:2] == ["agent", "get"]:
    target = args[2]; starts = []
    for line in open(os.environ["FAKE_HERDR_LOG"]):
        call = normalized(json.loads(line))
        if call[:3] == ["agent", "start", target]: starts.append(call)
    if not starts:
        print(json.dumps({"error": {"code": "agent_not_found", "message": f"agent target {target} not found"}}),
              file=sys.stderr)
        sys.exit(1)
    call = starts[-1]; pane = call[call.index("--pane") + 1]
    status = os.environ.get("FAKE_HERDR_AGENT_STATUS", "idle")
    all_calls = [normalized(json.loads(line)) for line in open(os.environ["FAKE_HERDR_LOG"])]
    start_index = max(i for i, prior in enumerate(all_calls) if prior[:3] == ["agent", "start", target])
    prompt_count = sum(prior[:3] == ["agent", "prompt", target] for prior in all_calls[start_index + 1:])
    print(json.dumps({"result": {"agent": {"name": target, "pane_id": pane, "tab_id": pane.replace(":p", ":t"),
                                                "workspace_id": "w1", "agent_status": status,
                                                "state_change_seq": 1 + prompt_count}}}))
elif args[:2] in (["agent", "prompt"], ["agent", "wait"]):
    if (args[:2] == ["agent", "prompt"] and os.environ.get("FAKE_HERDR_REVIEWER_LOSS") == "1"
            and args[2].startswith("review-")):
        print("reviewer disappeared", file=sys.stderr); sys.exit(3)
    starts = []
    for line in open(os.environ["FAKE_HERDR_LOG"]):
        call = normalized(json.loads(line))
        if call[:3] == ["agent", "start", args[2]]: starts.append(call)
    call = starts[-1] if starts else []
    model = call[call.index("--model") + 1] if "--model" in call else None
    thinking = call[call.index("--thinking") + 1] if "--thinking" in call else None
    session_id = call[call.index("--session-id") + 1] if "--session-id" in call else None
    session_dir = Path(call[call.index("--session-dir") + 1]) if "--session-dir" in call else None
    pane = call[call.index("--pane") + 1] if call else "w1:p-agent"
    pane_env = {}
    for index, line in enumerate(open(os.environ["FAKE_HERDR_LOG"]), 1):
        prior = normalized(json.loads(line))
        if prior[:2] == ["tab", "create"] and f"w1:p{index}" == pane:
            for env_index, value in enumerate(prior):
                if value == "--env" and env_index + 1 < len(prior):
                    key, _, env_value = prior[env_index + 1].partition("="); pane_env[key] = env_value
    events_path = Path(pane_env["BOSS_AGENT_EVENTS"]) if pane_env.get("BOSS_AGENT_EVENTS") else None
    if args[:2] == ["agent", "prompt"] and events_path and session_id:
        prior_events = [json.loads(line) for line in events_path.read_text().splitlines() if line]
        sequence = 1 + max([event.get("input_sequence", 0) for event in prior_events] or [0])
        base_event = {"schema": 1, "nonce": pane_env.get("BOSS_AGENT_NONCE"),
                      "input_sequence": sequence, "session_id": session_id, "at": "2026-01-01T00:00:00Z"}
        # Real Herdr/Pi submits the editor buffer after trimming outer
        # whitespace. Keep the fake faithful so correlation tests catch a
        # controller that hashes a different representation.
        prompt_text = args[3].strip()
        runtime_events = sign_chain([
            {**base_event, "type": "input", "prompt_sha256": hashlib.sha256(prompt_text.encode()).hexdigest(),
             "prompt_bytes": len(prompt_text.encode()), "source": "interactive", "streaming_behavior": None,
             "model": model, "thinking": thinking},
            {**base_event, "type": "agent_start"},
            {**base_event, "type": "agent_settled"},
        ], prior_events[-1]["event_sha256"] if prior_events else "0" * 64,
           int(prior_events[-1]["event_sequence"]) + 1 if prior_events else 1)
        with events_path.open("a") as handle:
            handle.write("".join(json.dumps(event) + "\n" for event in runtime_events))
    if args[:2] == ["agent", "prompt"] and session_id and session_dir:
        session_dir.mkdir(parents=True, exist_ok=True)
        session_file = session_dir / f"2026-01-01T00-00-00-000Z_{session_id}.jsonl"
        if not session_file.exists():
            events = [
                {"type": "session", "id": session_id, "timestamp": "2026-01-01T00:00:00Z"},
                {"type": "model_change", "provider": model.split("/", 1)[0], "modelId": model.split("/", 1)[1]},
                {"type": "thinking_level_change", "thinkingLevel": thinking},
                {"type": "message", "message": {"role": "assistant", "usage": {"totalTokens": 10, "cost": {"total": 0.01}}}},
            ]
            session_file.write_text("".join(json.dumps(event) + "\n" for event in events))
    if args[:2] == ["agent", "prompt"] and os.environ.get("FAKE_HERDR_EXECUTE") == "1":
        prompt = args[3]
        pane = call[call.index("--pane") + 1] if call else ""
        cwd = None
        for index, line in enumerate(open(os.environ["FAKE_HERDR_LOG"]), 1):
            prior = normalized(json.loads(line))
            if prior[:2] == ["tab", "create"] and f"w1:p{index}" == pane and "--cwd" in prior:
                cwd = Path(prior[prior.index("--cwd") + 1])
        if prompt.startswith("Continue work on the persistent branch") and cwd:
            (cwd / "herdr-change.txt").write_text("real persistent agent change\n")
        if prompt.startswith("Continue work in the owned worktree") and cwd:
            (cwd / "herdr-change.txt").write_text("real persistent agent change\n")
        if "independent" in prompt and "reviewer" in prompt:
            exact = re.search(r"Review exact base ([0-9a-f]{40}) and commit ([0-9a-f]{40})", prompt)
            base_sha, sha = exact.group(1), exact.group(2)
            output = re.search(r"to (/.+?\.pending\.json)", prompt).group(1)
            Path(output).write_text(json.dumps({"verdict": "accept", "notes": "fake independent review",
                                                "base_sha": base_sha, "sha": sha}))
    print(json.dumps({"result": {"agent": {"name": args[2], "pane_id": pane, "workspace_id": "w1",
                                                "agent_status": "idle", "state_change_seq": 2}}}))
elif args[:2] == ["agent", "read"]:
    print("fake durable agent output")
elif args[:2] == ["workspace", "list"]:
    workspaces = [] if os.environ.get("FAKE_HERDR_NEW_WORKSPACE") == "1" else [{"workspace_id": "w1", "label": "BOSS"}]
    print(json.dumps({"result": {"workspaces": workspaces}}))
elif args[:2] == ["workspace", "create"]:
    print(json.dumps({"result": {"workspace": {"workspace_id": "w1"},
                                 "tab": {"tab_id": "w1:t-default"},
                                 "root_pane": {"pane_id": "w1:p-default"}}}))
elif args[:2] == ["tab", "list"]:
    if os.environ.get("FAKE_HERDR_NEW_WORKSPACE") == "1":
        tabs = [{"tab_id": "w1:t-default", "label": "Shell"}]
    elif os.environ.get("FAKE_HERDR_EXISTING_DEAD_COO") == "1":
        tabs = [{"tab_id": "w1:t-coo", "label": "◆ BOSS"}]
    else:
        tabs = []
    print(json.dumps({"result": {"tabs": tabs}}))
elif args[:2] == ["pane", "list"]:
    print(json.dumps({"result": {"panes": [{"pane_id": "w1:p-coo", "tab_id": "w1:t-coo",
                                               "agent_status": "unknown"}]}}))
elif args[:2] == ["pane", "get"]:
    print(json.dumps({"result": {"pane": {"pane_id": args[2], "agent": "pi", "agent_status": "idle"}}}))
elif args[:2] == ["pane", "read"]:
    last_run = ""
    for line in open(os.environ["FAKE_HERDR_LOG"]):
        call = normalized(json.loads(line))
        if call[:2] == ["pane", "run"]:
            last_run = " ".join(call[3:])
    print(f"{last_run}\nfake-shell %")
elif args[:2] == ["pane", "process-info"]:
    calls = [normalized(json.loads(line)) for line in open(os.environ["FAKE_HERDR_LOG"])]
    sleep_indexes = [i for i, call in enumerate(calls)
                     if call[:3] == ["pane", "run", args[args.index("--pane") + 1]]
                     and any("/bin/sleep" in part for part in call[3:])]
    probes = (sum(call[:2] == ["pane", "process-info"] for call in calls[sleep_indexes[-1] + 1:])
              if sleep_indexes else 0)
    pane = args[args.index("--pane") + 1]
    agent_started = any(call[:2] == ["agent", "start"] and "--pane" in call
                        and call[call.index("--pane") + 1] == pane for call in calls)
    foreground = ([{"pid": os.getppid(), "name": "node"}] if agent_started else
                  [{"pid": 43, "name": "sleep"}] if sleep_indexes and probes == 1 else
                  [{"pid": 42, "name": "zsh"}])
    print(json.dumps({"result": {"process_info": {"pane_id": args[args.index("--pane") + 1], "shell_pid": 42,
                                                      "foreground_processes": foreground}}}))
elif args[:2] in (["tab", "close"], ["tab", "focus"], ["pane", "run"], ["notification", "show"],
                  ["agent", "send-keys"], ["agent", "focus"]):
    print(json.dumps({"result": {"type": "ok"}}))
else:
    print(json.dumps({"result": {}}))
