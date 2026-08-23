#!/usr/bin/env python3
"""Stand-in for the `herdr` CLI: records every call to $FAKE_HERDR_LOG and answers like herdr."""
import json, os, re, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ["FAKE_HERDR_LOG"], "a") as fh:
    fh.write(json.dumps(args) + "\n")
n = sum(1 for _ in open(os.environ["FAKE_HERDR_LOG"]))
if args[:1] == ["--session"]:
    args = args[2:]
if args[:2] == ["tab", "create"]:
    print(json.dumps({"result": {"tab": {"tab_id": f"w1:t{n}", "label": args[args.index('--label') + 1]},
                                 "root_pane": {"pane_id": f"w1:p{n}"}, "type": "tab_created"}}))
elif args[:2] == ["agent", "start"]:
    name, pane = args[2], args[args.index("--pane") + 1]
    model = args[args.index("--model") + 1] if "--model" in args else None
    thinking = args[args.index("--thinking") + 1] if "--thinking" in args else None
    print(json.dumps({"result": {"agent": {"name": name, "pane_id": pane, "tab_id": pane.replace(":p", ":t"),
                                                "workspace_id": "w1", "agent_status": "idle",
                                                "agent_session_id": f"real-{name}-{pane}", "model": model, "thinking": thinking}}}))
elif args[:2] == ["agent", "get"]:
    target = args[2]; starts = []
    for line in open(os.environ["FAKE_HERDR_LOG"]):
        call = json.loads(line)
        if call[:3] == ["agent", "start", target]: starts.append(call)
    if not starts: sys.exit(1)
    call = starts[-1]; pane = call[call.index("--pane") + 1]
    model = call[call.index("--model") + 1] if "--model" in call else None
    thinking = call[call.index("--thinking") + 1] if "--thinking" in call else None
    status = os.environ.get("FAKE_HERDR_AGENT_STATUS", "idle")
    print(json.dumps({"result": {"agent": {"name": target, "pane_id": pane, "tab_id": pane.replace(":p", ":t"),
                                                "workspace_id": "w1", "agent_status": status,
                                                "agent_session_id": f"real-{target}-{pane}", "model": model, "thinking": thinking}}}))
elif args[:2] in (["agent", "prompt"], ["agent", "wait"]):
    starts = []
    for line in open(os.environ["FAKE_HERDR_LOG"]):
        call = json.loads(line)
        if call[:3] == ["agent", "start", args[2]]: starts.append(call)
    call = starts[-1] if starts else []
    model = call[call.index("--model") + 1] if "--model" in call else None
    thinking = call[call.index("--thinking") + 1] if "--thinking" in call else None
    if args[:2] == ["agent", "prompt"] and os.environ.get("FAKE_HERDR_EXECUTE") == "1":
        prompt = args[3]
        pane = call[call.index("--pane") + 1] if call else ""
        cwd = None
        for index, line in enumerate(open(os.environ["FAKE_HERDR_LOG"]), 1):
            prior = json.loads(line)
            if prior[:2] == ["tab", "create"] and f"w1:p{index}" == pane and "--cwd" in prior:
                cwd = Path(prior[prior.index("--cwd") + 1])
        if prompt.startswith("Continue work on the persistent branch") and cwd:
            (cwd / "herdr-change.txt").write_text("real persistent agent change\n")
            subprocess.run(["git", "-C", str(cwd), "add", "-A"], check=True)
            subprocess.run(["git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t",
                            "commit", "-qm", "herdr: persistent change"], check=True)
        if "independent" in prompt and "reviewer" in prompt:
            sha = re.search(r"Review commit ([0-9a-f]{40})", prompt).group(1)
            output = re.search(r"to (/.+?\.pending\.json)", prompt).group(1)
            Path(output).write_text(json.dumps({"verdict": "accept", "notes": "fake independent review", "sha": sha}))
    print(json.dumps({"result": {"agent": {"name": args[2], "pane_id": "w1:p-agent", "workspace_id": "w1",
                                                "agent_status": "idle", "agent_session_id": f"real-{args[2]}-w1:p-agent",
                                                "model": model, "thinking": thinking}}}))
elif args[:2] == ["agent", "read"]:
    print("fake durable agent output")
elif args[:2] == ["workspace", "list"]:
    workspaces = [] if os.environ.get("FAKE_HERDR_NEW_WORKSPACE") == "1" else [{"workspace_id": "w1", "label": "First Mate"}]
    print(json.dumps({"result": {"workspaces": workspaces}}))
elif args[:2] == ["workspace", "create"]:
    print(json.dumps({"result": {"workspace": {"workspace_id": "w1"},
                                 "tab": {"tab_id": "w1:t-default"},
                                 "root_pane": {"pane_id": "w1:p-default"}}}))
elif args[:2] == ["tab", "list"]:
    if os.environ.get("FAKE_HERDR_NEW_WORKSPACE") == "1":
        tabs = [{"tab_id": "w1:t-default", "label": "Shell"}]
    elif os.environ.get("FAKE_HERDR_EXISTING_DEAD_MATE") == "1":
        tabs = [{"tab_id": "w1:t-mate", "label": "⚓ First Mate"}]
    else:
        tabs = []
    print(json.dumps({"result": {"tabs": tabs}}))
elif args[:2] == ["pane", "list"]:
    print(json.dumps({"result": {"panes": [{"pane_id": "w1:p-mate", "tab_id": "w1:t-mate",
                                               "agent_status": "unknown"}]}}))
elif args[:2] == ["pane", "get"]:
    print(json.dumps({"result": {"pane": {"pane_id": args[2], "agent": "pi", "agent_status": "idle"}}}))
elif args[:2] in (["tab", "close"], ["tab", "focus"], ["pane", "run"], ["notification", "show"], ["agent", "send-keys"]):
    print(json.dumps({"result": {"type": "ok"}}))
else:
    print(json.dumps({"result": {}}))
