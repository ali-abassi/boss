from __future__ import annotations
import argparse
import copy
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from . import registry, dispatch, work, deliver, worktree, herdr, board, control, scope, gates, supervisor, processes, __version__
from .paths import home, dispatch_file, authority_lock
from .util import BossError, locked, log, now, private_mkdir, write_json


def out(obj, as_json: bool, text: str | None = None):
    if as_json:
        print(json.dumps(obj, indent=2, sort_keys=True))
    elif text is not None:
        print(text)


def cmd_add(a):
    p = registry.add(a.path, a.id, a.mode, a.authority, a.test, a.protected.split(",") if a.protected else [], a.base, a.gate)
    out(p, a.json, f"registered {p['id']}  mode {p['mode']} · authority {p['authority']} · base {p['base']}\n"
                   f"  test: {p['test_cmd'] or '(none found)'}")
    if not p["test_cmd"]:
        print(f"  no test command detected — changes will not be verified. Set one: bossctl set {p['id']} --test \"…\"", file=sys.stderr)
    elif a.test is None:
        print(f"  (detected; override with: bossctl set {p['id']} --test \"…\")", file=sys.stderr)


# ---------------------------------------------------------------- daemon lifecycle

def _pid_file(): return home() / "daemon.pid"
def _pid_lock(): return home() / "daemon.lock"

def _daemon_records_unlocked() -> list[object]:
    try:
        raw = _pid_file().read_text().strip()
        value = json.loads(raw)
        return value if isinstance(value, list) else [value]
    except (OSError, ValueError, json.JSONDecodeError):
        return []


def _daemon_pids_unlocked() -> list[int]:
    return [probe["pid"] for record in _daemon_records_unlocked()
            if (probe := processes.probe(record)).get("state") == "live"]


def daemon_pids() -> list[int]:
    with locked(_pid_lock()):
        return _daemon_pids_unlocked()

def daemon_pid():
    pids = daemon_pids()
    return pids[0] if pids else None


def cmd_up(a):
    home().mkdir(parents=True, exist_ok=True, mode=0o700)
    if herdr.inside():
        return _up_herdr(a)
    return _up_background(a)


def _up_background(a, *, quiet: bool = False):
    with locked(_pid_lock()):
        records = _daemon_records_unlocked()
        existing = [probe["pid"] for record in records
                    if (probe := processes.probe(record)).get("state") == "live"]
        desired = max(1, a.workers)
        if len(existing) >= desired:
            if a.json: out({"pids": existing}, True)
            elif not quiet: print(f"{len(existing)} worker{'s' if len(existing) != 1 else ''} already running")
            return
        pids = list(existing)
        with open(home() / "daemon.log", "ab") as logf:
            for n in range(len(existing) + 1, desired + 1):
                owner = f"worker-{n}"
                # This process is already running under bin/bossctl's verified
                # runtime. Reuse that exact interpreter and module entry point
                # so the PID's command is stable before identity capture.
                p = subprocess.Popen([sys.executable, "-m", "bossctl", "daemon", "--owner", owner,
                                      "--interval", str(a.interval)],
                                     stdin=subprocess.DEVNULL, stdout=logf, stderr=logf,
                                     start_new_session=True, env=os.environ)
                identity = None
                for _ in range(20):
                    identity = processes.capture(p.pid, owner)
                    if identity and p.poll() is None:
                        break
                    time.sleep(0.025)
                if not identity or p.poll() is not None:
                    if p.poll() is None:
                        p.terminate()
                        try: p.wait(timeout=5)
                        except subprocess.TimeoutExpired: p.kill(); p.wait()
                    raise BossError("worker started without a provable process identity; it was stopped")
                records.append(identity); pids.append(p.pid)
                write_json(_pid_file(), records)
    if a.json: out({"pids": pids}, True)
    elif not quiet: print(f"{len(pids)} worker{'s' if len(pids) != 1 else ''} running")


def _up_herdr(a):
    """Herdr shows real task agents; schedulers stay invisible in the background."""
    # Legacy ops/worker tabs are closed only when session/workspace/label
    # still prove exact ownership. A reused ID is preserved for doctor rather
    # than closing somebody else's live tab.
    herdr.close_all("board")
    herdr.close_all("worker")
    return _up_background(a, quiet=True)


def cmd_down(a):
    closed = herdr.close_all() if (home() / "herdr.json").exists() else 0
    with locked(_pid_lock()):
        records = _daemon_records_unlocked()
        signalled, remaining = [], []
        for record in records:
            evidence = processes.probe(record)
            if evidence.get("state") == "live":
                try:
                    os.killpg(evidence["pgid"], signal.SIGTERM); signalled.append(record)
                except (OSError, ProcessLookupError):
                    remaining.append(record)
            elif evidence.get("state") == "dead":
                continue
            else:
                remaining.append(record)
        deadline = time.monotonic() + 10
        pending = list(signalled)
        while pending and time.monotonic() < deadline:
            pending = [record for record in pending if processes.probe(record).get("state") != "dead"]
            if pending: time.sleep(0.05)
        stopped = [int(record.get("pid")) for record in signalled if record not in pending]
        remaining.extend(pending)
        if remaining: write_json(_pid_file(), remaining)
        else: _pid_file().unlink(missing_ok=True)
        if remaining:
            raise BossError(f"shutdown could not prove {len(remaining)} worker identity/identities stopped; state was retained for doctor")
        if not stopped:
            out({"stopped": False, "tabs_closed": closed}, a.json, f"closed {closed} herdr tabs" if closed else "workers not running")
            return
    out({"stopped": True, "pids": stopped, "untrusted_records": 0}, a.json,
        f"stopped {len(stopped)} workers; interrupted work remains preserved for doctor/recover")


def cmd_status(a):
    items = work.all_items()
    counts = {}
    for i in items:
        counts[i["status"]] = counts.get(i["status"], 0) + 1
    pid = daemon_pid()
    from .util import read_json
    tabs = read_json(home() / "herdr.json", {"tabs": []})["tabs"]
    data = {"workers": pid, "herdr_tabs": tabs, "projects": len(registry.load()["projects"]), "items": counts,
            "supervisor": supervisor.summary()}
    if a.json:
        return out(data, True)
    print(board.render(pid))


HARNESS = {
    "pi": ["pi", "--approve"],
    "claude": ["claude", "--dangerously-skip-permissions"],
    "codex": ["codex", "--full-auto"],
}


# A default, not a cage: every model the login provides stays selectable (/model), and the
# team's per-step models live in dispatch.json, which the boss can change by asking.
PI_HOME_SETTINGS = {"defaultProvider": "openai-codex", "defaultModel": "gpt-5.6-sol",
                    "defaultThinkingLevel": "high", "quietStartup": True}


def pi_home() -> Path:
    """The BOSS's own Pi config dir. Nothing from the boss's Pi is inherited."""
    return home() / "pi"


def _isolated_pi_home() -> Path:
    dst = pi_home()
    private_mkdir(dst)
    settings = dst / "settings.json"
    current = json.loads(settings.read_text()) if settings.exists() else {}
    if "defaultModel" not in current:                      # seed once; the boss's later choices stick
        current.update(PI_HOME_SETTINGS)
        write_json(settings, current)
    readme = dst / "README"
    if not readme.exists():
        descriptor = os.open(readme, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write("BOSS's private Pi home — managed by bossctl. Log in here with `bossctl setup`.\n")
    return dst


def codex_ready() -> bool:
    if not shutil.which("pi"):
        return False
    r = subprocess.run(["pi", "auth", "check", "--provider", "openai-codex"], text=True, capture_output=True,
                       env={**os.environ, "PI_CODING_AGENT_DIR": str(_isolated_pi_home())}, stdin=subprocess.DEVNULL)
    return r.stdout.strip() == "ready"


def cmd_setup(a):
    """Own config, own login: connect the BOSS to the Codex subscription."""
    dst = _isolated_pi_home()
    dispatch.load()
    if not codex_ready() and not getattr(a, "fresh", False):
        # The boss has very likely logged Pi into Codex already: reuse that, don't ask twice.
        src = Path(os.environ.get("BOSS_IMPORT_PI_DIR", "~/.pi/agent")).expanduser() / "auth.json"
        try:
            creds = json.loads(src.read_text()).get("openai-codex")
        except (OSError, json.JSONDecodeError):
            creds = None
        if creds:
            auth = dst / "auth.json"
            current = json.loads(auth.read_text()) if auth.exists() else {}
            current["openai-codex"] = creds
            auth.write_text(json.dumps(current, indent=2) + "\n"); auth.chmod(0o600)
            print(f"  ◆ reusing your Codex login from {src.parent}", file=sys.stderr)
        elif a.import_login:
            raise BossError(f"no openai-codex login found in {src}")
    if codex_ready():
        out({"ready": True, "pi_home": str(dst)}, a.json, f"✓ connected to the Codex subscription · pi home {dst}")
        return
    if a.json or not sys.stdin.isatty():
        raise BossError("not connected to Codex yet — run `pi-boss setup` in a terminal")
    print("\n  ◆ one-time setup — connect the BOSS to your Codex subscription\n"
          "     Pi will open. Type  /login  and choose  OpenAI Codex , finish in the browser, then  /exit\n", file=sys.stderr)
    subprocess.run(["pi"], env={**os.environ, "PI_CODING_AGENT_DIR": str(dst)}, cwd=str(dst))
    if codex_ready():
        out({"ready": True, "pi_home": str(dst)}, False, f"✓ connected to the Codex subscription · pi home {dst}")
    else:
        raise BossError("still not connected to Codex — run `pi-boss setup` again")


def cmd_launch(a):
    """Outside Herdr, create/attach the one named persistent BOSS session."""
    if herdr.inside():
        return cmd_boss(argparse.Namespace(harness=a.harness, workers=a.workers))
    binary = shutil.which("herdr")
    if not binary:
        raise BossError("Herdr is required for persistent BOSS sessions; no non-persistent worker fallback is fabricated")
    session = a.session
    def call(*args):
        return subprocess.run([binary, "--session", session, *args], text=True, capture_output=True, stdin=subprocess.DEVNULL)
    probe = call("workspace", "list")
    if probe.returncode and "server_not_running" in (probe.stderr or probe.stdout):
        home().mkdir(parents=True, exist_ok=True, mode=0o700)
        logf = open(home() / "herdr-server.log", "ab")
        subprocess.Popen([binary, "--session", session, "server"], stdin=subprocess.DEVNULL,
                         stdout=logf, stderr=logf, start_new_session=True)
        deadline = time.time() + 10
        while time.time() < deadline:
            time.sleep(0.1); probe = call("workspace", "list")
            if probe.returncode == 0: break
    def hc(*args):
        r = probe if args == ("workspace", "list") else call(*args)
        if r.returncode:
            raise BossError(f"Herdr session '{session}' unavailable: {(r.stderr or r.stdout).strip()[:400]}")
        try: return json.loads(r.stdout) if r.stdout.strip() else {}
        except json.JSONDecodeError: raise BossError("Herdr returned an invalid session response")
    listed = (hc("workspace", "list").get("result") or {}).get("workspaces") or []
    created_workspace = False
    if listed:
        workspace_id = listed[0]["workspace_id"]
    else:
        created_workspace = True
        made = hc("workspace", "create", "--cwd", str(Path(__file__).resolve().parents[1]), "--label", "BOSS", "--no-focus",
                  "--env", f"BOSS_HOME={home()}", "--env", f"PI_CODING_AGENT_DIR={pi_home()}")
        workspace_id = (made.get("result") or {}).get("workspace", {}).get("workspace_id")
    if not workspace_id:
        raise BossError("Herdr did not return a workspace identity; nothing was launched")
    tabs = (hc("tab", "list", "--workspace", workspace_id).get("result") or {}).get("tabs") or []
    coo_tab = next((t for t in tabs if t.get("label") == "◆ BOSS"), None)
    pane = None
    if coo_tab:
        created_coo = False
        panes = (hc("pane", "list").get("result") or {}).get("panes") or []
        pane_info = next((p for p in panes if p.get("tab_id") == coo_tab.get("tab_id")), None)
        pane = (pane_info or {}).get("pane_id")
        running = (pane_info or {}).get("agent") == "pi" or (pane_info or {}).get("agent_status") in ("idle", "working")
    else:
        created_coo = True
        running = False
        made = hc("tab", "create", "--workspace", workspace_id, "--cwd", str(Path(__file__).resolve().parents[1]),
                  "--label", "◆ BOSS", "--no-focus", "--env", f"BOSS_HOME={home()}",
                  "--env", f"PI_CODING_AGENT_DIR={pi_home()}")
        pane = (made.get("result") or {}).get("root_pane", {}).get("pane_id")
        coo_tab = (made.get("result") or {}).get("tab", {})
        if not pane: raise BossError("Herdr did not return a pane identity; nothing was launched")
        # Some Herdr versions create a default shell tab with a new workspace.
        # Once the real BOSS tab exists, close those initial placeholders.
        if created_workspace:
            new_tab = coo_tab.get("tab_id")
            for old in tabs:
                if old.get("tab_id") and old.get("tab_id") != new_tab:
                    hc("tab", "close", old["tab_id"])
    if not pane:
        raise BossError("The BOSS tab has no pane; close it and run pi-boss again")
    if not running:
        if not herdr.wait_shell(pane, session=session, binary=binary):
            if created_coo and coo_tab.get("tab_id"):
                hc("tab", "close", coo_tab["tab_id"])
            raise BossError("The BOSS shell did not become ready; no command was sent")
        command = shlex.quote(str(Path(__file__).resolve().parents[1] / "bin" / "pi-boss"))
        if a.harness != "pi": command += " " + shlex.quote(a.harness)
        # Herdr 0.8 queues input for --no-focus tabs. Focusing before pane run
        # is required command delivery; attaching later is not soon enough.
        if coo_tab.get("tab_id"):
            hc("tab", "focus", coo_tab["tab_id"])
        hc("pane", "run", pane, command)
        deadline = time.time() + 8
        while time.time() < deadline:
            state = (hc("pane", "get", pane).get("result") or {}).get("pane", {})
            if state.get("agent") == "pi" or state.get("agent_status") in ("idle", "working"):
                running = True
                break
            time.sleep(0.1)
        if not running:
            raise BossError("Pi did not start in the BOSS tab; the session was not attached")
    if coo_tab.get("tab_id"):
        hc("tab", "focus", coo_tab["tab_id"])
    os.execv(binary, [binary, "session", "attach", session])


def cmd_boss(a):
    """One command: workers up, banner, then the liaison in front of you."""
    cmd = HARNESS.get(a.harness) or [a.harness]
    if not shutil.which(cmd[0]):
        hint = {"pi": "install Pi:  npm install -g @earendil-works/pi-coding-agent",
                "claude": "install Claude Code:  npm install -g @anthropic-ai/claude-code",
                "codex": "install Codex:  npm install -g @openai/codex"}.get(cmd[0], "")
        raise BossError(f"{cmd[0]} is not installed. {hint}".strip())
    from .util import read_json
    # Always pass through the Herdr startup path so upgrades clean up the old
    # permanent ops/worker tabs even when background schedulers are alive.
    if herdr.inside() or not daemon_pid():
        cmd_up(argparse.Namespace(json=False, interval=20, workers=a.workers))
    if a.harness == "pi":
        if not codex_ready():
            cmd_setup(argparse.Namespace(json=False, import_login=False))
    else:
        print(f"BOSS: {' '.join(cmd)}", file=sys.stderr)
    repo = Path(__file__).resolve().parents[1]
    os.chdir(repo)
    os.execvp(cmd[0], cmd)


def cmd_watch(a):
    board.watch(a.interval, once=a.once)


def cmd_tail(a):
    """Follow a work item's live run log (used by per-task herdr tabs)."""
    d = work.item_dir(a.id)
    print(f"{a.id} — waiting for the run to start…")
    deadline = time.time() + 120
    logf = None
    while time.time() < deadline and not logf:
        runs = sorted((d / "runs").glob("*/log.md")) if (d / "runs").exists() else []
        logf = runs[-1] if runs else None
        if not logf:
            time.sleep(1)
    if not logf:
        print("no run started within 120s"); return
    print(f"following {logf}\n")
    os.execvp("tail", ["tail", "-n", "+1", "-F", str(logf)])


def cmd_set(a):
    p = registry.set_fields(a.id, mode=a.mode, authority=a.authority, test_cmd=a.test, base=a.base, gate=a.gate,
                            protected_paths=a.protected.split(",") if a.protected else None)
    out(p, a.json, f"{p['id']}: {p['mode']} authority {p['authority']} base {p['base']}")


def cmd_projects(a):
    ps = registry.load()["projects"]
    if a.json:
        return out(ps, True)
    items = work.all_items()
    for p in ps.values():
        open_ = [i for i in items if i["project"] == p["id"] and i["status"] in work.OPEN]
        print(f"{p['id']:<20} {p['mode']:<12} auth {p['authority']}  open {len(open_):<3} {p['path']}")


def _node_budget_args(token_specs=None, cost_specs=None, second_specs=None) -> dict:
    result = {}
    for key, specs, converter in (("tokens", token_specs or [], int),
                                  ("cost", cost_specs or [], float),
                                  ("seconds", second_specs or [], int)):
        for spec in specs:
            node, separator, raw = str(spec).partition("=")
            if not separator or not node or not raw:
                raise BossError(f"--node-max-{key} requires NODE=LIMIT")
            if node not in work.BUDGET_NODES:
                raise BossError(f"unknown budget node '{node}'; choose from {', '.join(work.BUDGET_NODES)}")
            if key in result.setdefault(node, {}):
                raise BossError(f"duplicate {key} budget for node '{node}'")
            try:
                value = converter(raw)
            except ValueError:
                raise BossError(f"{node} {key} budget must be numeric") from None
            result[node][key] = value
    return work.validate_node_budgets(result)


def cmd_task(a):
    text = Path(a.file).read_text() if a.file else " ".join(a.text)
    if not text.strip():
        raise BossError("empty task")
    node_budgets = _node_budget_args(a.node_max_tokens, a.node_max_cost, a.node_max_seconds)
    it = work.create(a.project, text, a.kind, a.labels.split(",") if a.labels else [], a.max_attempts,
                     a.scope.split(",") if a.scope else None, a.model, a.thinking,
                     a.max_tokens, a.max_cost, a.max_seconds, node_budgets)
    out(it, a.json, f"{it['id']} queued → {it['rigor']['level']} rigor · graph {it['dispatch']['graph']} (rule {it['dispatch']['rule']})")


def cmd_inspect(a):
    it = work.load(a.id)
    project = registry.get(it["project"])
    wt = worktree.worktree_root() / project["id"] / it["id"]
    verified = ((it.get("verification") or [{}])[-1]).get("fingerprint")
    if wt.exists() and verified and verified != worktree.signature(wt):
        def invalidate(item):
            for review in item.get("reviews", []):
                review["valid"] = False; review["invalidated_at"] = now()
            item["phase"] = "integrity-invalid"
            if item["status"] in ("ready", "pr-open"):
                item["status"] = "paused"
                item["ask"] = {"question": "The reviewed worktree mutated. Recover and rerun verification?",
                               "context": "Exact-SHA approval was invalidated."}
        it = control.cas_update(a.id, invalidate)
    recent = ""
    session = it.get("session") or {}
    if session.get("agent_name") and herdr.inside():
        recent = herdr.agent_read(session["agent_name"], a.lines)
    if not recent and it.get("runs"):
        run_dir = Path(it["runs"][-1].get("run_dir") or "")
        chunks = []
        if run_dir.is_dir():
            for path in sorted(p for p in run_dir.iterdir() if p.is_file() and p.suffix in (".md", ".txt", ".stdout", ".stderr", ".json"))[-6:]:
                try: chunks.append(path.read_text()[-4000:])
                except (OSError, UnicodeError): pass
        recent = "\n".join(chunks)
    data = control.inspection(it, recent)
    out(data, a.json, json.dumps(data, indent=2))


def cmd_control(a):
    it = work.load(a.id)
    if a.action in ("resume", "recover"):
        if exhausted := work.budget_blockers(it):
            raise BossError(f"{a.action} refused with exhausted budget or unproven usage (" + ", ".join(exhausted) +
                            "); raise it explicitly with `bossctl budget` first")
    if (a.action == "resume" and
            (it.get("phase") == "recovery-required" or it.get("recovery_claim_token"))):
        raise BossError("resume refused: unknown agent settlement retains a recovery claim; inspect it and use explicit `bossctl recover`")
    session = it.get("session") or {}
    target = session.get("agent_name")
    value = " ".join(getattr(a, "value", []) or [])
    it = control.request(a.id, a.action, value if a.action == "steer" else None,
                         request_id=getattr(a, "request_id", None))
    deduplicated = it.pop("_control_deduplicated", False)
    events = it.get("controls", {}).get("events") or []
    event_id = (next((event.get("id") for event in events if event.get("id") == getattr(a, "request_id", None)), None)
                if getattr(a, "request_id", None) else (events[-1].get("id") if events else None))
    delivered = a.action not in ("steer", "pause", "interrupt")
    if a.action == "steer" and target and it["status"] == "running" and herdr.inside() and not deduplicated:
        # Non-blocking submission: never mistake the completion of an already
        # active turn for acknowledgement of this steering message.
        delivered = work.deliver_steering(a.id, event_id, value, session, f"cli:{os.getpid()}")
    # Running pause/interrupt is consumed by the runner monitor, which performs
    # interrupt -> settle -> checkpoint. Marking it paused here would race that
    # sequence and could release the item before its checkpoint exists.
    if a.action in ("pause", "interrupt"):
        was_queued = it["status"] == "queued"
        def paused(x):
            if x["status"] == "queued":
                x["controls"]["paused"] = True; x["phase"] = "paused"; x["status"] = "paused"
        it = control.cas_update(a.id, paused)
        delivered = was_queued
    elif a.action in ("resume", "recover") and it["status"] in ("paused", "failed"):
        def resumed(x):
            x.update(status="queued", phase="queued")
            x["controls"]["paused"] = False; x["controls"]["recovery_requested"] = False
            if a.action == "recover":
                x["attempts"] = 0; x["ask"] = None
                unresolved_launch = next((launch for launch in reversed(x.get("agent_launches") or [])
                                          if launch.get("role") == "implementer" and launch.get("state") in
                                          {"reserved", "tab-created", "attested"}), {})
                x["recovery_authorized"] = {"at": now(), "request_id": event_id,
                                            # The newest unresolved launch edge is the thing
                                            # being superseded.  Falling back to the finalized
                                            # session is correct only when no open edge exists.
                                            "session_id": (unresolved_launch.get("agent_session_id")
                                                           or (x.get("session") or {}).get("agent_session_id"))}
        it = control.cas_update(a.id, resumed)
    if delivered and event_id and a.action != "steer":
        it = control.consume(a.id, [event_id])
    out(control.redact(it), a.json, f"{a.id}: {a.action} recorded")


def cmd_budget(a):
    updates = {"tokens": a.tokens, "cost": a.cost, "seconds": a.seconds}
    node_updates = _node_budget_args(a.node_max_tokens, a.node_max_cost, a.node_max_seconds)
    if all(value is None for value in updates.values()) and not node_updates:
        raise BossError("budget requires at least one item or per-node limit")
    if any(value is not None and not work.valid_budget_value(value) for value in updates.values()):
        raise BossError("budget limits must be positive")
    def mutate(item):
        if item.get("status") not in ("paused", "needs-you", "failed"):
            raise BossError("budget changes require a paused/needs-you/failed item")
        before = dict(item.get("budgets") or {})
        before_nodes = copy.deepcopy(item.get("node_budgets") or {})
        item.setdefault("budgets", {}).update({key: value for key, value in updates.items() if value is not None})
        merged_nodes = copy.deepcopy(item.get("node_budgets") or {})
        for node, limits in node_updates.items():
            merged_nodes.setdefault(node, {}).update(limits)
        item["node_budgets"] = work.validate_node_budgets(merged_nodes)
        historical = bool(item.get("attempts") or item.get("runs") or item.get("session") or item.get("agent_launches"))
        usage = item.setdefault("node_usage", {})
        for node in node_updates:
            if node not in usage:
                if historical:
                    raise BossError(f"cannot add a retroactive {node} budget after model/runtime history; usage is unattributable")
                usage[node] = work._new_node_usage()
        item.setdefault("history", []).append({"at": now(), "from": item.get("status"), "to": item.get("status"),
                                               "note": f"boss updated budget from {before}/{before_nodes} to {item['budgets']}/{item['node_budgets']}"})
    item = control.cas_update(a.id, mutate)
    out(control.redact(item), a.json, f"{a.id}: budget updated; use `bossctl resume {a.id}` when ready")


def cmd_scope(a):
    paths = scope.normalize(a.paths.split(","))
    def mutate(it):
        if (it.get("promotion") or {}).get("state") in {"armed", "external-requested", "merge-observed", "cleanup-pending"}:
            raise BossError("scope change refused while exact-SHA promotion requires reconciliation")
        if (it.get("pr_delivery") or {}).get("state") in {"armed", "push-requested", "push-confirmed", "pr-create-requested"}:
            raise BossError("scope change refused while exact-SHA PR delivery requires reconciliation")
        if it["status"] == "running": raise BossError("cannot replace scope while running; pause first")
        project = registry.get(it["project"])
        global_claim = scope.is_global(paths) or any(scope.overlap(paths, [p]) for p in project.get("protected_paths", []))
        it["scope"] = {"paths": paths, "claim": "global" if global_claim else "paths"}
        it.setdefault("controls", {})["paused"] = False; it["reviews"] = []
        if it["status"] in ("paused", "needs-you", "ready", "pr-open"): it["status"] = "queued"; it["phase"] = "queued"
    it = control.cas_update(a.id, mutate)
    out(it, a.json, f"{a.id}: declared scope {', '.join(paths)}")


def cmd_work(a):
    items = work.all_items()
    if not a.all:
        items = [i for i in items if i["status"] in work.OPEN]
    if a.json:
        return out(control.redact(items), True)
    for i in items:
        extra = i.get("pr_url") or (i.get("ask") or {}).get("question", "")[:60] or ""
        print(f"{i['id']:<44} {i['status']:<10} {i['kind']:<5} a{i['attempts']}/{i['max_attempts']} {extra}")


def cmd_show(a):
    it = control.redact(work.load(a.id))
    if a.json:
        return out(it, True)
    print(json.dumps({k: v for k, v in it.items() if k not in ("history",)}, indent=2))
    for h in it.get("history", []):
        print(f"  {h['at']} {h['from']} → {h['to']}  {h['note']}")
    rep = work.item_dir(it["id"]) / "report.md"
    if rep.exists():
        print(f"\nreport: {rep}")


def cmd_inbox(a):
    items = [i for i in work.all_items() if i["status"] in ("needs-you", "failed", "ready", "pr-open")]
    if a.json:
        return out(control.redact(items), True)
    emit = lambda text: print(board.ascii_text(text) if board.plain() else text)
    if not items:
        emit("nothing needs you")
    for i in items:
        title = i["text"].splitlines()[0][:60]
        if i["status"] == "needs-you":
            emit(f"[question]  {i['project']}: {title}\n            {(i.get('ask') or {}).get('question')}")
            if a.hints: emit(f"            → bossctl respond {i['id']} \"…\"")
        elif i["status"] == "failed":
            last = (i["failure_notes"] or [{}])[-1].get("notes", "")[:300].replace("\n", " ")
            emit(f"[failed]    {i['project']}: {title}\n            {last}")
            if a.hints: emit(f"            → bossctl respond {i['id']} \"guidance\"  |  bossctl retry {i['id']}")
        else:
            what = i.get("pr_url") or f"branch {i['branch']}"
            emit(f"[{i['status']}]{' ' * max(1, 11 - len(i['status']) - 2)}{i['project']}: {title}\n            {what} — say \"merge it\" to promote")
            if a.hints: emit(f"            → bossctl promote {i['id']} --confirm")


def cmd_respond(a):
    it = work.respond(a.id, " ".join(a.guidance))
    out(it, a.json, f"{it['id']} requeued with guidance")


def cmd_retry(a):
    it = work.retry(a.id)
    out(it, a.json, f"{it['id']} requeued")


def cmd_cancel(a):
    it = work.cancel(a.id, discard=a.discard)
    receipt = ((it.get("cancellation") or {}).get("quarantine_result") or {}).get("path")
    note = f"{it['id']} cancelled" + (f"; work preserved at {receipt}" if receipt else "; no worktree existed")
    out(it, a.json, note)


def cmd_promote(a):
    it = work.load(a.id)
    p = registry.get(it["project"])
    result = deliver.promote(it, p, a.confirm)
    verb = "merged" if result["state"] == "merged" else "merge requested; awaiting exact GitHub evidence"
    out(work.load(a.id), a.json, f"{it['id']} {verb}: {result['ref']}")


def cmd_run_once(a):
    it = work.claim_next(a.owner)
    if not it:
        supervisor.scan()
        out({"claimed": None}, a.json, "nothing queued")
        return
    it = work.execute(it, timeout=a.timeout)
    out(it, a.json, f"{it['id']}: {it['status']}")


def cmd_daemon(a):
    log(f"daemon start interval={a.interval}s")
    stopped = threading.Event()
    def supervise_forever():
        while not stopped.is_set():
            try:
                supervisor.scan()
            except BaseException as exc:
                log(f"supervisor scan unavailable: {getattr(exc, 'msg', None) or exc!r}")
            try:
                from . import forge
                forge.monitor_all()
            except BaseException as exc:
                log(f"GitHub monitor unavailable: {getattr(exc, 'msg', None) or exc!r}")
            stopped.wait(max(1, a.interval))
    watcher = threading.Thread(target=supervise_forever,
                               name=f"boss-supervisor:{a.owner}", daemon=True)
    watcher.start()
    idle = 0
    try:
        while True:
            it = work.claim_next(a.owner)
            if it:
                idle = 0
                try:
                    work.execute(it, timeout=a.timeout)
                except KeyboardInterrupt:
                    raise
                except BaseException as e:  # execution state is already preserved; keep the loop alive
                    log(f"{it['id']}: executor error {e!r}")
                continue
            idle += 1
            if a.once_idle and idle >= a.once_idle:
                log("daemon: queue drained, exiting")
                return
            time.sleep(a.interval)
    finally:
        stopped.set(); watcher.join(timeout=max(2, a.interval + 1))


def cmd_supervise(a):
    observations = supervisor.scan(probe_agents=not a.no_herdr)
    out(observations, a.json, "\n".join(f"{o['item_id']}: {o['classification']} — {o['reason']}" for o in observations) or "no work items")


def cmd_wakes(a):
    if a.ack:
        count = supervisor.acknowledge(a.ack.split(","), a.consumer)
        return out({"acknowledged": count}, a.json, f"acknowledged {count} wake(s)")
    if a.release:
        count = supervisor.release(a.release.split(","), a.consumer)
        return out({"released": count}, a.json, f"released {count} wake claim(s)")
    if a.sending:
        count = supervisor.mark_sending(a.sending.split(","), a.consumer)
        return out({"sending": count}, a.json, f"marked {count} wake send(s) in progress")
    if a.sent:
        count = supervisor.mark_sent(a.sent.split(","), a.consumer)
        return out({"sent": count}, a.json, f"marked {count} wake send(s) durable")
    if a.renew:
        count = supervisor.renew(a.renew.split(","), a.consumer)
        return out({"renewed": count}, a.json, f"renewed {count} wake receipt(s)")
    events = supervisor.claim(a.consumer, limit=a.limit) if a.claim else supervisor.pending()
    out(events, a.json, "\n".join(f"[{e['classification']}] {e['item_id']} — {e['reason']}" for e in events) or "no pending wakes")


def cmd_wait(a):
    item = supervisor.declare_wait(a.id, a.duration, " ".join(a.reason))
    wait = item.get("declared_wait")
    out(item, a.json, f"{a.id}: " + (f"waiting until {wait['until']}" if wait else "declared wait cleared"))


def cmd_forge(a):
    from . import forge
    observations = forge.monitor_all() if a.all else [{"item_id": a.id, **forge.monitor_item(a.id, force=True)}]
    out(observations, a.json, "\n".join(f"{o['item_id']}: {o['classification']} — {o['reason']}" for o in observations) or "no open GitHub PRs")


def cmd_gate_status(a):
    item = work.load(a.id); project = registry.get(item["project"])
    result = gates.inspect_item(item, project)
    out(result, a.json, f"{a.id}: {result.get('classification')} — {result.get('reason')}")
    return 0 if result.get("classification") in {"checks-passed", "needs-you", "running"} else 1


def cmd_gate_reconcile(a):
    with locked(authority_lock()):
        result = gates.reconcile(a.id)
    observation = (result.get("external_gate") or {}).get("last_observation") or {}
    out(control.redact(result), a.json,
        f"{a.id}: {observation.get('classification')} — {observation.get('reason')}")


def cmd_gate_respond(a):
    findings = [value.strip() for value in (a.findings or "").split(",") if value.strip()]
    with locked(authority_lock()):
        result = gates.respond(a.id, a.action, findings, a.instructions)
    observation = (result.get("external_gate") or {}).get("last_observation") or {}
    out(control.redact(result), a.json,
        f"{a.id}: {observation.get('classification')} — {observation.get('reason')}")


def cmd_memory(a):
    from . import memory
    args = a.args
    if a.scope == "operational":
        if a.action == "list": result = memory.list_operational()
        elif a.action == "get" and len(args) == 1: result = memory.get_operational(args[0])
        elif a.action == "set" and len(args) >= 2: result = memory.set_operational(args[0], " ".join(args[1:]))
        elif a.action == "remove" and len(args) == 1: result = memory.remove_operational(args[0], confirm=a.confirm)
        else: raise BossError("usage: bossctl memory operational list|get KEY|set KEY VALUE|remove KEY --confirm")
    else:
        if a.action == "list" and len(args) == 1: result = memory.project_requests(args[0])
        elif a.action == "set" and len(args) >= 3: result = memory.request_project(args[0], "set", args[1], " ".join(args[2:]))
        elif a.action == "remove" and len(args) == 2: result = memory.request_project(args[0], "remove", args[1], confirm=a.confirm)
        else: raise BossError("usage: bossctl memory project list PROJECT|set PROJECT KEY VALUE|remove PROJECT KEY --confirm")
    out(result, a.json, json.dumps(control.redact(result), indent=2))


def cmd_away_mode(a):
    result = supervisor.away_status() if a.state == "status" else supervisor.set_away(a.state == "on")
    out(result, a.json, ("away mode on" if result.get("enabled") else "away mode off") +
        f" · {result.get('pending_wakes', 0)} durable wake(s) preserved")


def cmd_dispatch(a):
    cfg = dispatch.load()
    if a.set:
        from .util import write_json
        for spec in a.set:
            phase, _, model = spec.partition("=")
            if phase not in dispatch.PHASES or "/" not in model:
                raise BossError(f"use --set PHASE=provider/model with PHASE in {dispatch.PHASES}")
            cfg["models"][phase] = model
        write_json(dispatch_file(), cfg)
        print("models now: " + ", ".join(f"{k}={v}" for k, v in cfg["models"].items()))
        return
    if a.json:
        return out(cfg, True)
    print(f"dispatch file: {dispatch_file()}")
    print("defaults:", json.dumps(cfg["models"], indent=2))
    for r in cfg["rules"]:
        print(f"  rule {r.get('name'):<14} kind={r.get('kind','*'):<5} labels={r.get('labels',[])} graph={r.get('graph','<mode>')} models={r.get('models',{})}")


def cmd_doctor(a):
    from . import doctor
    if a.repair:
        result = doctor.repair(confirm=a.confirm, network=not a.offline,
                               auth_source=a.auth_source, auth_provider=a.auth_provider,
                               model_specs=a.model, test_specs=a.test, fetch_projects=a.fetch)
        return out(result, a.json, f"doctor repair: {len(result['applied'])} applied · {len(result['skipped'])} skipped\n" + doctor.render(result["post_audit"]))
    report = doctor.audit(network=not a.offline, probe_models=a.probe)
    out(report, a.json, doctor.render(report))
    return 0 if report["healthy"] else 1


def _main(argv=None):
    ap = argparse.ArgumentParser(prog="bossctl", description="deterministic software operations across repositories")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    def S(name, fn, help_):
        p = sub.add_parser(name, help=help_); p.set_defaults(fn=fn); p.add_argument("--json", action="store_true"); return p

    p = S("add", cmd_add, "register a repo"); p.add_argument("path"); p.add_argument("--id")
    p.add_argument("--mode", default="local-only", choices=registry.ACCEPTED_MODES); p.add_argument("--authority", type=int, default=1)
    p.add_argument("--gate", default="native", choices=gates.PROVIDERS,
                   help="native or the externally installed, pinned macOS no-mistakes adapter")
    p.add_argument("--test", help="test command (the verify gate); auto-detected when omitted")
    p.add_argument("--protected", help="comma-separated globs the agent may not change"); p.add_argument("--base")
    p = S("set", cmd_set, "change a project's posture"); p.add_argument("id"); p.add_argument("--mode", choices=registry.ACCEPTED_MODES)
    p.add_argument("--gate", choices=gates.PROVIDERS)
    p.add_argument("--authority", type=int); p.add_argument("--test"); p.add_argument("--protected"); p.add_argument("--base")
    S("projects", cmd_projects, "list projects")
    p = S("task", cmd_task, "queue a ship or scout task"); p.add_argument("project"); p.add_argument("text", nargs="*")
    p.add_argument("--file"); p.add_argument("--kind", default="ship", choices=("ship", "scout"))
    p.add_argument("--labels", help="comma-separated dispatch labels, e.g. cheap,hard"); p.add_argument("--max-attempts", type=int, default=3)
    p.add_argument("--scope", help="comma-separated declared path/glob claims; unknown serializes")
    p.add_argument("--model", help="boss override for implement/scout provider/model"); p.add_argument("--thinking", choices=("off","minimal","low","medium","high","xhigh"))
    p.add_argument("--max-tokens", type=int); p.add_argument("--max-cost", type=float); p.add_argument("--max-seconds", type=int)
    p.add_argument("--node-max-tokens", action="append", default=[], metavar="NODE=LIMIT")
    p.add_argument("--node-max-cost", action="append", default=[], metavar="NODE=LIMIT")
    p.add_argument("--node-max-seconds", action="append", default=[], metavar="NODE=LIMIT")
    p = S("work", cmd_work, "list work items"); p.add_argument("--all", action="store_true")
    p = S("show", cmd_show, "show one item with history"); p.add_argument("id")
    p = S("inspect", cmd_inspect, "secret-safe live item inspection"); p.add_argument("id"); p.add_argument("--lines", type=int, default=120)
    for action in ("steer", "pause", "resume", "interrupt", "recover"):
        p = S(action, cmd_control, f"{action} a persistent item"); p.set_defaults(action=action); p.add_argument("id"); p.add_argument("value", nargs="*")
        p.add_argument("--request-id", help="idempotency key for retried/racing control delivery")
    p = S("budget", cmd_budget, "explicitly update a paused item's cumulative limits")
    p.add_argument("id"); p.add_argument("--tokens", type=int); p.add_argument("--cost", type=float); p.add_argument("--seconds", type=int)
    p.add_argument("--node-max-tokens", action="append", default=[], metavar="NODE=LIMIT")
    p.add_argument("--node-max-cost", action="append", default=[], metavar="NODE=LIMIT")
    p.add_argument("--node-max-seconds", action="append", default=[], metavar="NODE=LIMIT")
    p = S("scope", cmd_scope, "replace a paused item's declared scope"); p.add_argument("id"); p.add_argument("paths")
    p = S("inbox", cmd_inbox, "what needs the boss"); p.add_argument("--hints", action="store_true", help="show the bossctl commands (for the BOSS)")
    p = S("respond", cmd_respond, "answer a question / give guidance, requeue"); p.add_argument("id"); p.add_argument("guidance", nargs="+")
    p = S("retry", cmd_retry, "requeue a failed item"); p.add_argument("id")
    p = S("cancel", cmd_cancel, "cancel an item"); p.add_argument("id"); p.add_argument("--discard", action="store_true")
    p = S("promote", cmd_promote, "merge a ready/pr-open item (boss's word)"); p.add_argument("id"); p.add_argument("--confirm", action="store_true")
    p = S("run-once", cmd_run_once, "claim and execute one queued item"); p.add_argument("--owner", default="cli"); p.add_argument("--timeout", type=int, default=3600)
    p = S("daemon", cmd_daemon, "execute forever"); p.add_argument("--owner", default="daemon"); p.add_argument("--interval", type=int, default=20)
    p.add_argument("--timeout", type=int, default=3600); p.add_argument("--once-idle", type=int, default=0, help="exit after N idle polls (tests)")
    p = S("supervise", cmd_supervise, "run one zero-token supervisor scan"); p.add_argument("--no-herdr", action="store_true", help="do not probe live Herdr identities")
    p = S("wakes", cmd_wakes, "inspect or claim durable supervisor wakes")
    p.add_argument("--claim", action="store_true"); p.add_argument("--consumer", default="cli"); p.add_argument("--limit", type=int, default=20)
    p.add_argument("--ack", help="comma-separated claimed wake ids"); p.add_argument("--release", help="comma-separated claimed wake ids")
    p.add_argument("--sending", help="durably mark IDs before Pi send begins")
    p.add_argument("--sent", help="durably mark IDs after Pi accepted the message")
    p.add_argument("--renew", help="renew IDs while their queued Pi turn is pending")
    p = S("wait", cmd_wait, "declare when an unchanged item should resurface")
    p.add_argument("id"); p.add_argument("duration", help="30s, 20m, 1h30m, or clear"); p.add_argument("reason", nargs="*")
    p = S("forge", cmd_forge, "observe GitHub PR lifecycle at the exact reviewed SHA")
    group = p.add_mutually_exclusive_group(required=True); group.add_argument("--all", action="store_true"); group.add_argument("id", nargs="?")
    p = S("gate-status", cmd_gate_status, "inspect one journaled no-mistakes transaction read-only")
    p.add_argument("id")
    p = S("gate-reconcile", cmd_gate_reconcile, "record authoritative no-mistakes SQLite evidence without replay")
    p.add_argument("id")
    p = S("gate-respond", cmd_gate_respond, "answer one exact observed no-mistakes gate")
    p.add_argument("id"); p.add_argument("--action", required=True, choices=("approve", "fix", "skip"))
    p.add_argument("--findings", help="comma-separated exact finding IDs; required for fix")
    p.add_argument("--instructions", help="boss guidance passed only with action=fix")
    p = S("memory", cmd_memory, "explicit operational memory or reviewed project knowledge")
    p.add_argument("scope", choices=("operational", "project")); p.add_argument("action", choices=("list", "get", "set", "remove"))
    p.add_argument("args", nargs="*"); p.add_argument("--confirm", action="store_true")
    p = S("away-mode", cmd_away_mode, "gated unattended supervision without merge authority")
    p.add_argument("state", choices=("on", "off", "status"), default="status", nargs="?")
    p = S("dispatch", cmd_dispatch, "show dispatch table"); p.add_argument("--set", action="append", metavar="PHASE=provider/model", help="change a step's default model")
    p = S("setup", cmd_setup, "connect the BOSS to the Codex subscription (own config, own login)")
    p.add_argument("--import-login", action="store_true", help="(default behaviour) reuse the Codex login from your Pi")
    p.add_argument("--fresh", action="store_true", help="ignore any existing Pi login and log in interactively")
    p = S("up", cmd_up, "start workers (herdr tabs when inside herdr, else background)"); p.add_argument("--interval", type=int, default=20)
    p.add_argument("--workers", type=int, default=2, help="worker tabs to open inside herdr")
    p = S("watch", cmd_watch, "live ops board"); p.add_argument("--interval", type=float, default=2.0); p.add_argument("--once", action="store_true")
    p = S("tail", cmd_tail, "follow one item's run log"); p.add_argument("id")
    S("down", cmd_down, "stop background workers")
    S("status", cmd_status, "workers, projects, queue at a glance")
    p = S("boss", cmd_boss, "start workers and open the liaison (pi by default)")
    p.add_argument("harness", nargs="?", default="pi", help="pi | claude | codex | any command")
    p.add_argument("--workers", type=int, default=2)
    p = S("launch", cmd_launch, "create/attach the persistent Herdr BOSS session")
    p.add_argument("--session", default="boss"); p.add_argument("--workers", type=int, default=2)
    p.add_argument("--harness", default="pi")
    p = S("doctor", cmd_doctor, "read-only control-plane audit and confirmed safe reconciliation")
    p.add_argument("--probe", action="store_true", help="live 1-word call per model (explicitly spends a few tokens)")
    p.add_argument("--offline", action="store_true", help="skip read-only network freshness/auth probes")
    p.add_argument("--repair", action="store_true", help="apply the freshly audited non-destructive repair plan")
    p.add_argument("--confirm", action="store_true", help="explicitly authorize --repair mutations")
    p.add_argument("--auth-source", help="explicit auth.json or Pi config directory to copy one provider from")
    p.add_argument("--auth-provider", default="openai-codex", help="provider key used with --auth-source")
    p.add_argument("--model", action="append", metavar="PHASE=PROVIDER/MODEL",
                   help="explicitly reconcile one dispatch model against the offline inventory")
    p.add_argument("--test", action="append", metavar="PROJECT=COMMAND",
                   help="explicitly replace one project test command after syntax validation")
    p.add_argument("--fetch", action="append", metavar="PROJECT",
                   help="fetch only the exact configured origin/base ref; never merge or update the checkout")
    a = ap.parse_args(argv)
    os.environ["PI_CODING_AGENT_DIR"] = str(pi_home())
    try:
        return a.fn(a)
    except BossError as e:
        return e.code
    except subprocess.CalledProcessError as e:
        print(f"bossctl: command failed: {' '.join(e.cmd)}\n{e.stderr}", file=sys.stderr)
        return 1


def main(argv=None):
    """Run one CLI command with private creation defaults and restore the caller."""
    previous_umask = os.umask(0o077)
    try:
        return _main(argv)
    finally:
        os.umask(previous_umask)
