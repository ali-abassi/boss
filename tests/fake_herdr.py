#!/usr/bin/env python3
"""Stand-in for the `herdr` CLI: records every call to $FAKE_HERDR_LOG and answers like herdr."""
import json, os, sys
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
    print(json.dumps({"result": {"agent": {"name": args[2], "pane_id": "w1:p-agent", "workspace_id": "w1",
                                                "agent_status": "idle", "agent_session_id": f"real-{args[2]}-w1:p-agent",
                                                "model": model, "thinking": thinking}}}))
elif args[:2] == ["workspace", "list"]:
    print(json.dumps({"result": {"workspaces": [{"workspace_id": "w1", "label": "First Mate"}]}}))
elif args[:2] == ["tab", "list"]:
    print(json.dumps({"result": {"tabs": []}}))
elif args[:2] in (["tab", "close"], ["pane", "run"], ["notification", "show"], ["agent", "send-keys"]):
    print(json.dumps({"result": {"type": "ok"}}))
else:
    print(json.dumps({"result": {}}))
