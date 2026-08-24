"""Inside Herdr the fleet is visible without a redundant board tab."""
try:
    import _gitenv  # noqa: F401  (git hygiene for temp repos)
except ImportError:
    from tests import _gitenv  # noqa: F401
import hashlib, json, os, subprocess, sys, tempfile, unittest, uuid
from pathlib import Path
from unittest import mock
from tests.fake_helm import fake_sandbox_status

REPO = Path(__file__).resolve().parents[1]
HELM = [sys.executable, str(REPO / "tests" / "fake_helm.py")]


class HerdrTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()); self.home = self.tmp / "home"; self.bin = self.tmp / "bin"; self.bin.mkdir()
        (self.bin / "herdr").symlink_to(REPO / "tests" / "fake_herdr.py")
        self.log = self.tmp / "herdr-calls.jsonl"; self.log.touch()
        self.proj = self.tmp / "proj"; self.proj.mkdir()
        g = lambda *a: subprocess.run(["git", "-C", str(self.proj), *a], check=True, capture_output=True)
        g("init", "-q", "-b", "main"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
        (self.proj / "README.md").write_text("x"); g("add", "-A"); g("commit", "-qm", "init")
        self.pi_home = self.home / "pi"; self.pi_home.mkdir(parents=True)
        (self.pi_home / "settings.json").write_text("{}\n")
        self.env = {**os.environ, "HELM_HOME": str(self.home), "HELM_PIW": str(REPO / "tests" / "fake_piw.py"),
                    "PI_CODING_AGENT_DIR": str(self.pi_home),
                    "PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_HERDR_LOG": str(self.log),
                    "HERDR_ENV": "1", "HERDR_WORKSPACE_ID": "w1", "HERDR_SESSION": "pi-x", "HERDR_PANE_ID": "w1:p1"}
        sandbox_patch = mock.patch("helm.sandbox.available", side_effect=fake_sandbox_status)
        sandbox_patch.start(); self.addCleanup(sandbox_patch.stop)

    def helm(self, *args, check=True):
        r = subprocess.run(HELM + list(args), env=self.env, text=True, capture_output=True)
        if check and r.returncode != 0:
            self.fail(f"helm {' '.join(args)} failed ({r.returncode}):\n{r.stdout}\n{r.stderr}")
        return r

    def calls(self):
        calls = [json.loads(l) for l in self.log.read_text().splitlines() if l.strip()]
        return [call[2:] if call[:1] == ["--session"] else call for call in calls]

    def raw_calls(self):
        return [json.loads(l) for l in self.log.read_text().splitlines() if l.strip()]

    def test_fake_verification_runner_refuses_shell_and_preserves_timeout_contract(self):
        profile = self.tmp / "fake-profile.sb"; profile.write_text("(version 1)\n")
        scratch = self.tmp / "fake-scratch"; scratch.mkdir()
        receipt = hashlib.sha256(profile.read_bytes()).hexdigest()
        runner = str(REPO / "tests" / "fake_firstmate_tool.py")
        marker = self.tmp / "must-not-exist"
        refused = subprocess.run(
            [runner, str(profile), receipt, str(scratch), "1", f"touch {marker}"],
            text=True, capture_output=True,
        )
        self.assertEqual(refused.returncode, 126, refused.stderr)
        self.assertFalse(marker.exists())
        timed = subprocess.run(
            [runner, str(profile), receipt, str(scratch), "1", "sleep 2"],
            text=True, capture_output=True,
        )
        self.assertEqual(timed.returncode, 124, timed.stderr)

    def test_up_readies_scheduler_without_empty_herdr_tabs(self):
        out = self.helm("up", "--workers", "3").stdout
        self.assertEqual(out, "")
        creates = [c for c in self.calls() if c[:2] == ["tab", "create"]]
        self.assertEqual(creates, [])
        records = json.loads((self.home / "daemon.pid").read_text())
        self.assertEqual(len(records), 3)
        self.assertTrue(all(record["kind"] == "firstmate-worker" and record.get("start_sha256")
                            and record.get("command_sha256") for record in records))
        self.assertEqual(self.helm("up").stdout, "")                         # idempotent and quiet
        self.assertIn("crew      ready", self.helm("status").stdout)
        self.helm("down")
        closes = [c for c in self.calls() if c[:2] == ["tab", "close"]]
        self.assertEqual(closes, [])
        self.assertFalse((self.home / "herdr.json").exists())

    def test_startup_never_closes_a_reused_stale_tab_id(self):
        old = os.environ.copy(); os.environ.update(self.env)
        try:
            from helm import cli, herdr
            self.home.mkdir(parents=True, exist_ok=True)
            (self.home / "herdr.json").write_text(json.dumps({"tabs": [
                {"kind": "worker", "tab_id": "w1:t-reused", "workspace_id": "w1",
                 "herdr_session": "pi-x", "label": "old worker"}
            ]}))
            with mock.patch.object(herdr, "_tab_matches", return_value=False), \
                 mock.patch.object(herdr, "close_tab") as close, \
                 mock.patch.object(cli, "_up_background", return_value=None):
                cli._up_herdr(type("Args", (), {})())
            close.assert_not_called()
            stored = json.loads((self.home / "herdr.json").read_text())
            self.assertEqual(stored["tabs"][0]["tab_id"], "w1:t-reused")
        finally:
            os.environ.clear(); os.environ.update(old)

    def test_each_running_task_gets_a_tab_then_it_closes_and_captain_is_notified(self):
        self.helm("add", str(self.proj), "--id", "p", "--test", "true", "--mode", "local-only")
        it = json.loads(self.helm("task", "p", "add a thing", "--json").stdout)
        self.helm("run-once")
        calls = self.calls()
        creates = [c for c in calls if c[:2] == ["tab", "create"]]
        self.assertEqual(len(creates), 1)
        self.assertTrue(creates[0][creates[0].index("--label") + 1].startswith("⚙ p: add a thing"))
        run = [c for c in calls if c[:2] == ["pane", "run"]][0]
        self.assertIn(f"tail {it['id']}", run[3])
        self.assertTrue(any(c[:2] == ["tab", "close"] for c in calls), "task tab must close when the task ends")
        notes = [c for c in calls if c[:2] == ["notification", "show"]]
        self.assertEqual(notes[0][2], "p: ready")
        self.assertEqual(json.loads((self.home / "herdr.json").read_text())["tabs"], [])

    def test_pi_firstmate_outside_herdr_launches_named_session_without_recursion(self):
        env = {k: v for k, v in self.env.items() if not k.startswith("HERDR_")}
        env["FAKE_HERDR_LOG"] = str(self.log)
        r = subprocess.run([str(REPO / "bin" / "pi-firstmate")], env=env, text=True, capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.calls()
        self.assertIn(["--session", "firstmate", "workspace", "list"], self.raw_calls())
        run = next(c for c in calls if c[:2] == ["pane", "run"])
        self.assertIn("pi-firstmate", run[-1])
        focus = next(c for c in calls if c[:2] == ["tab", "focus"])
        self.assertLess(calls.index(focus), calls.index(run), "the no-focus tab must be focused before command delivery")
        self.assertEqual(calls[-1], ["session", "attach", "firstmate"])

    def test_new_workspace_closes_the_default_empty_shell_tab(self):
        env = {k: v for k, v in self.env.items() if not k.startswith("HERDR_")}
        env.update(FAKE_HERDR_LOG=str(self.log), FAKE_HERDR_NEW_WORKSPACE="1")
        r = subprocess.run([str(REPO / "bin" / "pi-firstmate")], env=env, text=True, capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.calls()
        self.assertTrue(any(c[:2] == ["workspace", "create"] for c in calls))
        self.assertIn(["tab", "close", "w1:t-default"], calls)

    def test_existing_shell_only_firstmate_tab_is_restarted_and_focused(self):
        env = {k: v for k, v in self.env.items() if not k.startswith("HERDR_")}
        env.update(FAKE_HERDR_LOG=str(self.log), FAKE_HERDR_EXISTING_DEAD_MATE="1")
        r = subprocess.run([str(REPO / "bin" / "pi-firstmate")], env=env, text=True, capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.calls()
        run = ["pane", "run", "w1:p-mate", str(REPO / "bin" / "pi-firstmate")]
        focus = ["tab", "focus", "w1:t-mate"]
        self.assertIn(run, calls)
        self.assertLess(calls.index(focus), calls.index(run))
        self.assertIn(["tab", "focus", "w1:t-mate"], calls)
        self.assertEqual(calls[-1], ["session", "attach", "firstmate"])

    def test_agent_tab_is_focused_through_start_then_captain_is_restored(self):
        old = os.environ.copy(); os.environ.update(self.env, HERDR_TAB_ID="w1:t-captain")
        try:
            from helm import herdr
            item = {"id": "p-focused", "project": "p", "session": None}
            herdr.ensure_agent(item, self.proj, "openai-codex/gpt-5.6-sol", "high")
            calls = self.calls()
            created_index, created = next((i, c) for i, c in enumerate(calls) if c[:2] == ["tab", "create"])
            new_tab = f"w1:t{created_index + 1}"
            focus_new = next(c for c in calls if c[:3] == ["tab", "focus", new_tab])
            start = next(c for c in calls if c[:2] == ["agent", "start"])
            wrapper = next(c for c in calls if c[:3] == ["pane", "run", start[start.index("--pane") + 1]]
                           and "pi()" in c[-1])
            restore = next(c for c in calls if c[:3] == ["tab", "focus", "w1:t-captain"])
            self.assertLess(calls.index(focus_new), calls.index(start))
            self.assertLess(calls.index(wrapper), calls.index(start))
            self.assertIn(str(REPO / "libexec" / "pi"), wrapper[-1])
            self.assertGreater(calls.index(restore), calls.index(start))
        finally:
            os.environ.clear(); os.environ.update(old)

    def test_launch_environment_replaces_inherited_pi_home_without_duplicates(self):
        old = os.environ.copy(); os.environ.update(self.env)
        try:
            from helm import herdr
            private = str(self.home / "work" / "item" / "private-pi")
            with mock.patch.object(herdr, "focused_tab", return_value="w1:t-captain"), \
                 mock.patch.object(herdr, "focus_tab", return_value=True), \
                 mock.patch.object(herdr, "wait_shell", return_value=True):
                self.assertTrue(herdr.open_tab("private", "", self.proj,
                                               extra_env={"PI_CODING_AGENT_DIR": private}))
            create = next(call for call in self.calls() if call[:2] == ["tab", "create"])
            values = [create[index + 1] for index, value in enumerate(create) if value == "--env"]
            pi_values = [value for value in values if value.startswith("PI_CODING_AGENT_DIR=")]
            self.assertEqual(pi_values, [f"PI_CODING_AGENT_DIR={private}"])
        finally:
            os.environ.clear(); os.environ.update(old)

    def test_real_implementer_identity_reconnects_and_reviewers_are_fresh(self):
        old = os.environ.copy(); os.environ.update(self.env)
        try:
            from helm import herdr
            item = {"id": "p-item", "project": "p", "session": None}
            first = herdr.ensure_agent(item, self.proj, "openai-codex/gpt-5.6-sol", "high")
            uuid.UUID(first["agent_session_id"])
            self.assertTrue(first.get("runtime_attested_at"))
            item["session"] = first
            again = herdr.ensure_agent(item, self.proj, "openai-codex/gpt-5.6-sol", "high")
            self.assertEqual(again["agent_session_id"], first["agent_session_id"])
            self.assertTrue(again["reconnected"])
            a = herdr.ensure_agent(item, self.proj, "openai-codex/gpt-5.6-sol", "high", reviewer=True)
            b = herdr.ensure_agent(item, self.proj, "openai-codex/gpt-5.6-sol", "high", reviewer=True)
            self.assertNotEqual(a["agent_session_id"], b["agent_session_id"])
            self.assertNotEqual(a["agent_session_id"], first["agent_session_id"])
        finally:
            os.environ.clear(); os.environ.update(old)

    def test_runtime_attestation_and_hash_only_input_ledger_fail_closed_on_tampering(self):
        old = os.environ.copy(); os.environ.update(self.env)
        try:
            from helm import herdr
            item = {"id": "attested", "project": "p", "session": None}
            session = herdr.ensure_agent(item, self.proj, "openai-codex/gpt-5.6-sol", "high")
            with self.assertRaises(SystemExit):
                herdr.validate_agent(session, "openai-codex/gpt-5.6-sol", "high", require_durable=True)
            prompt = "\n  sensitive prompt text must never enter the runtime ledger  \n"
            herdr.prompt_agent(session["agent_name"], prompt, 30, session)
            events_path = Path(session["agent_events_path"])
            self.assertNotIn(prompt.strip(), events_path.read_text())
            events = herdr._runtime_events(session)
            self.assertEqual([event["type"] for event in events], ["input", "agent_start", "agent_settled"])
            self.assertEqual(events[0]["prompt_bytes"], len(prompt.strip().encode()))
            self.assertEqual(herdr.accepted_input(session, prompt, 0)["input_sequence"], 1)
            live = herdr.agent_get(session["agent_name"], session)
            herdr.validate_agent(live, "openai-codex/gpt-5.6-sol", "high", require_durable=True)

            attestation = Path(session["attestation_path"])
            data = json.loads(attestation.read_text()); data["session_file"] = "/tmp/escaped-session.jsonl"
            attestation.write_text(json.dumps(data))
            with self.assertRaises(SystemExit):
                herdr.validate_agent(session, "openai-codex/gpt-5.6-sol", "high")

            with events_path.open("a") as handle:
                handle.write(json.dumps({"schema": 1, "nonce": "foreign", "type": "agent_settled",
                                         "input_sequence": 99, "session_id": session["agent_session_id"]}) + "\n")
            with self.assertRaises(SystemExit):
                herdr.runtime_activity(session)
        finally:
            os.environ.clear(); os.environ.update(old)

    def test_controller_owned_anchor_rejects_same_inode_empty_and_valid_prefix_rollback(self):
        old = os.environ.copy(); os.environ.update(self.env)
        try:
            from helm import herdr
            from helm.util import write_json
            work_id = "anchored-ledger"
            directory = self.home / "work" / work_id; directory.mkdir(parents=True)
            item = {"id": work_id, "project": "p", "revision": 0, "status": "running",
                    "phase": "implementing", "session": None, "agent_launches": [],
                    "controls": {"pending": [], "events": []}, "history": []}
            write_json(directory / "item.json", item)
            session = herdr.ensure_agent(item, self.proj, "openai-codex/gpt-5.6-sol", "high")
            herdr.prompt_agent(session["agent_name"], "anchor this exact turn", 30, session)
            stored = json.loads((directory / "item.json").read_text())
            launch = next(value for value in stored["agent_launches"]
                          if value["launch_id"] == session["launch_id"])
            self.assertGreaterEqual(launch["runtime_anchor_event_sequence"], 3)
            path = Path(session["agent_events_path"]); inode = path.stat().st_ino
            full = path.read_text(); lines = full.splitlines(keepends=True)

            path.write_text("")
            self.assertEqual(path.stat().st_ino, inode)
            with self.assertRaises(SystemExit):
                herdr.runtime_activity(session)

            path.write_text(full)
            self.assertFalse(herdr.runtime_activity(session)["runtime_turn_pending"])
            path.write_text(lines[0])
            self.assertEqual(path.stat().st_ino, inode)
            with self.assertRaises(SystemExit):
                herdr.runtime_activity(session)
        finally:
            os.environ.clear(); os.environ.update(old)

    def test_runtime_ledger_correlates_queued_steering_with_the_turn_that_settles(self):
        old = os.environ.copy(); os.environ.update(self.env)
        try:
            from helm import herdr
            from tests._event_signing import sign_chain
            session = herdr.ensure_agent({"id": "queued-steer", "project": "p", "session": None}, self.proj,
                                         "openai-codex/gpt-5.6-sol", "high")
            path = Path(session["agent_events_path"])
            base = {"schema": 1, "nonce": session["attestation_nonce"],
                    "session_id": session["agent_session_id"], "at": "2026-01-01T00:00:00Z"}
            def event(kind, sequence, **extra):
                return {**base, "type": kind, "input_sequence": sequence, **extra}
            first = event("input", 1, prompt_sha256="1" * 64, prompt_bytes=3,
                          model=session["model"], thinking=session["thinking"])
            second = event("input", 2, prompt_sha256="2" * 64, prompt_bytes=4,
                           model=session["model"], thinking=session["thinking"])
            queued = sign_chain([first, event("agent_start", 1), second,
                                 event("agent_settled", 1), event("agent_start", 2)])
            path.write_text("".join(json.dumps(value) + "\n" for value in queued))
            self.assertEqual(herdr.runtime_activity(session)["runtime_input_sequence"], 2)
            self.assertTrue(herdr.runtime_activity(session)["runtime_turn_pending"])
            with path.open("a") as handle:
                settled = sign_chain([event("agent_settled", 2)], queued[-1]["event_sha256"], 6)[0]
                handle.write(json.dumps(settled) + "\n")
            self.assertFalse(herdr.runtime_activity(session)["runtime_turn_pending"])
            with path.open("a") as handle:
                duplicate = sign_chain([event("agent_settled", 2)], settled["event_sha256"], 7)[0]
                handle.write(json.dumps(duplicate) + "\n")
            with self.assertRaises(SystemExit):
                herdr.runtime_activity(session)
        finally:
            os.environ.clear(); os.environ.update(old)

    def test_missing_input_evidence_after_prompt_submission_is_unsettled(self):
        from helm import herdr
        live = {"agent_status": "idle", "state_change_seq": 7, "pane_id": "w1:p2"}
        identity = {"agent_events_path": "/tmp/events", "model": "openai-codex/gpt-5.6-sol",
                    "thinking": "high"}
        def cli(*args):
            if args[:2] == ("agent", "prompt"):
                return {"result": {"agent": live}}
            return {}
        with mock.patch.object(herdr, "agent_liveness", return_value={"state": "live", "agent": live}), \
             mock.patch.object(herdr, "exact_agent_liveness",
                               return_value={"state": "live", "identity_verified": True}), \
             mock.patch.object(herdr, "runtime_activity", return_value={"runtime_input_sequence": 0}), \
             mock.patch.object(herdr, "_runtime_input", return_value=None), \
             mock.patch.object(herdr, "focused_tab", return_value=None), \
             mock.patch.object(herdr, "focus_tab", return_value=True), \
             mock.patch.object(herdr, "cli_required", side_effect=cli), \
             mock.patch.object(herdr.time, "monotonic", side_effect=[0, 1, 2, 8]):
            with self.assertRaises(herdr.UnsettledAgentError):
                herdr.prompt_agent_async("agent", "work", identity)

    def test_dead_agent_requires_one_use_recovery_authorization_before_checkpoint_fallback(self):
        old = os.environ.copy(); os.environ.update(self.env)
        try:
            from helm import herdr
            item = {"id": "p-dead", "project": "p", "session": None}
            first = herdr.ensure_agent(item, self.proj, "openai-codex/gpt-5.6-sol", "high")
            item["session"] = first
            os.environ["FAKE_HERDR_AGENT_STATUS"] = "dead"
            with self.assertRaises(SystemExit):
                herdr.ensure_agent(item, self.proj, "openai-codex/gpt-5.6-sol", "high")
            item["recovery_authorized"] = {"session_id": first["agent_session_id"], "at": "now"}
            replacement = herdr.ensure_agent(item, self.proj, "openai-codex/gpt-5.6-sol", "high")
            self.assertFalse(replacement["reconnected"])
            self.assertNotEqual(replacement["agent_session_id"], first["agent_session_id"])
            with self.assertRaises(SystemExit):
                herdr.ensure_agent(item, self.proj, "openai-codex/gpt-5.6-sol", "high")
        finally:
            os.environ.clear(); os.environ.update(old)

    def test_explicit_recovery_supersedes_a_crash_reserved_absent_launch(self):
        old = os.environ.copy(); os.environ.update(self.env)
        try:
            from helm import herdr
            work_id = "p-reserved"
            directory = self.home / "work" / work_id; directory.mkdir(parents=True)
            launch_id = str(uuid.uuid4())
            launch = {"schema": 1, "launch_id": launch_id, "agent_session_id": launch_id,
                      "agent_name": "impl-reserved", "role": "implementer", "state": "reserved",
                      "cwd": str(self.proj.resolve()), "model": "openai-codex/gpt-5.6-sol",
                      "thinking": "high", "label": f"work p · {launch_id[:8]}",
                      "session_dir": str(directory / "agent-sessions" / "impl-reserved"),
                      "attestation_path": str(directory / "agent-attestations" / "old.json"),
                      "agent_events_path": str(directory / "agent-attestations" / "old.events.jsonl"),
                      "attestation_nonce": "old", "replacement_for": None}
            item = {"id": work_id, "project": "p", "revision": 0, "status": "queued", "session": None,
                    "agent_launches": [launch], "recovery_authorized": {"session_id": launch_id, "at": "now"}}
            (directory / "item.json").write_text(json.dumps(item))
            session = herdr.ensure_agent(item, self.proj, "openai-codex/gpt-5.6-sol", "high")
            stored = json.loads((directory / "item.json").read_text())
            old_launch = next(value for value in stored["agent_launches"] if value["launch_id"] == launch_id)
            self.assertEqual(old_launch["state"], "dead-confirmed")
            self.assertNotEqual(session["agent_session_id"], launch_id)
        finally:
            os.environ.clear(); os.environ.update(old)

    def test_live_steering_is_submitted_without_waiting_on_active_turn(self):
        old = os.environ.copy(); os.environ.update(self.env, HERDR_TAB_ID="w1:t-captain")
        try:
            from helm import herdr
            session = herdr.ensure_agent({"id": "steer-x", "project": "p", "session": None}, self.proj,
                                         "openai-codex/gpt-5.6-sol", "high")
            target = session["agent_name"]
            herdr.steer_agent(target, "narrow the parser", identity=session)
            calls = self.calls()
            prompt = [c for c in calls if c[:2] == ["agent", "prompt"]][-1]
            self.assertNotIn("--wait", prompt)
            self.assertIn("narrow the parser", prompt[3])
            prompt_index = max(i for i, call in enumerate(calls) if call is prompt)
            focus_index = max(i for i, call in enumerate(calls[:prompt_index])
                              if call[:3] == ["agent", "focus", target])
            restore_index = max(i for i, call in enumerate(calls)
                                if call[:3] == ["tab", "focus", "w1:t-captain"])
            self.assertLess(focus_index, prompt_index)
            self.assertGreater(restore_index, prompt_index)
        finally:
            os.environ.clear(); os.environ.update(old)

    def test_ship_runs_in_real_persistent_agent_and_reviews_bind_exact_sha(self):
        env = {**self.env, "FAKE_HERDR_EXECUTE": "1",
               "HELM_AVAILABLE_MODELS": "openai-codex/gpt-5.6-sol"}
        env.pop("HELM_PIW", None)
        r = subprocess.run(HELM + ["add", str(self.proj), "--id", "persistent", "--test", "true",
                                  "--mode", "high-assurance"], env=env, text=True, capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        task = subprocess.run(HELM + ["task", "persistent", "change it", "--json"], env=env,
                              text=True, capture_output=True)
        self.assertEqual(task.returncode, 0, task.stderr); item = json.loads(task.stdout)
        run = subprocess.run(HELM + ["run-once"], env=env, text=True, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        shown = json.loads(subprocess.run(HELM + ["show", item["id"], "--json"], env=env,
                                          text=True, capture_output=True, check=True).stdout)
        self.assertEqual(shown["status"], "ready", shown)
        uuid.UUID(shown["session"]["agent_session_id"])
        self.assertTrue(shown["session"].get("runtime_attested_at"))
        self.assertEqual(len(shown["reviews"]), 2)
        self.assertTrue(all(review["sha"] == shown["head_sha"] for review in shown["reviews"]))
        starts = [call for call in self.calls() if call[:2] == ["agent", "start"]]
        self.assertEqual(len(starts), 3, "one persistent implementer plus two fresh reviewers")

    def test_reviewer_usage_is_cumulative_and_exhaustion_stops_before_next_reviewer(self):
        self.env.pop("HELM_PIW", None)
        self.env["HELM_AVAILABLE_MODELS"] = "openai-codex/gpt-5.6-sol"
        self.helm("add", str(self.proj), "--id", "p", "--test", "true", "--mode", "high-assurance")
        item = json.loads(self.helm("task", "p", "bounded reviews", "--max-tokens", "15", "--json").stdout)
        self.env["FAKE_HERDR_EXECUTE"] = "1"
        self.helm("run-once")
        shown = json.loads(self.helm("show", item["id"], "--json").stdout)
        self.assertEqual(shown["status"], "paused", shown)
        self.assertEqual(shown["usage"]["tokens"], 20)
        self.assertEqual(shown["usage"]["implementer_tokens"], 10)
        self.assertEqual(shown["usage"]["reviewer_tokens"], 10)
        self.assertEqual([review["role"] for review in shown["reviews"]], ["correctness"])
        starts = [call for call in self.calls() if call[:2] == ["agent", "start"]]
        self.assertEqual(len(starts), 2, "adversarial reviewer must not start after the aggregate cap")

    def test_per_node_reviewer_budget_is_receipted_and_stops_later_nodes(self):
        self.env.pop("HELM_PIW", None)
        self.env["HELM_AVAILABLE_MODELS"] = "openai-codex/gpt-5.6-sol"
        self.helm("add", str(self.proj), "--id", "node-cap", "--test", "true", "--mode", "high-assurance")
        item = json.loads(self.helm("task", "node-cap", "bounded correctness review",
                                    "--node-max-tokens", "review_correctness=5", "--json").stdout)
        self.env["FAKE_HERDR_EXECUTE"] = "1"
        self.helm("run-once")
        shown = json.loads(self.helm("show", item["id"], "--json").stdout)
        self.assertEqual(shown["status"], "paused", shown)
        self.assertEqual(shown["node_usage"]["implement"]["tokens"], 10)
        self.assertEqual(shown["node_usage"]["review_correctness"]["tokens"], 10)
        self.assertIn("review_correctness tokens budget exceeded", shown["ask"]["context"])
        starts = [call for call in self.calls() if call[:2] == ["agent", "start"]]
        self.assertEqual(len(starts), 2, "adversarial reviewer must not start after the node cap")

    def test_verify_node_seconds_cap_times_out_and_never_starts_a_reviewer(self):
        self.env.pop("HELM_PIW", None)
        self.env["HELM_AVAILABLE_MODELS"] = "openai-codex/gpt-5.6-sol"
        self.helm("add", str(self.proj), "--id", "verify-cap", "--test", "sleep 3",
                  "--mode", "high-assurance")
        item = json.loads(self.helm("task", "verify-cap", "bounded verify",
                                    "--node-max-seconds", "verify=1", "--json").stdout)
        self.env["FAKE_HERDR_EXECUTE"] = "1"
        self.helm("run-once")
        shown = json.loads(self.helm("show", item["id"], "--json").stdout)
        self.assertEqual(shown["status"], "paused", shown)
        self.assertEqual(shown["runs"][-1]["failed_ids"], ["verify"])
        self.assertGreaterEqual(shown["node_usage"]["verify"]["seconds"], 0.9)
        starts = [call for call in self.calls() if call[:2] == ["agent", "start"]]
        self.assertEqual(len(starts), 1)

    def test_elapsed_budget_consumed_by_verification_never_starts_a_reviewer(self):
        self.env.pop("HELM_PIW", None)
        self.env["HELM_AVAILABLE_MODELS"] = "openai-codex/gpt-5.6-sol"
        self.helm("add", str(self.proj), "--id", "timed", "--test", "sleep 5",
                  "--mode", "high-assurance")
        item = json.loads(self.helm("task", "timed", "bounded verification",
                                    "--max-seconds", "3", "--json").stdout)
        self.env["FAKE_HERDR_EXECUTE"] = "1"
        self.helm("run-once")
        shown = json.loads(self.helm("show", item["id"], "--json").stdout)
        starts = [call for call in self.calls() if call[:2] == ["agent", "start"]]
        self.assertEqual(len(starts), 1, "no reviewer may start after verification consumes the deadline")
        self.assertIn(shown["status"], {"queued", "paused"}, shown)
        recorded = (shown.get("runs") or [])[-1]
        # Host load may consume the last bounded second before the verifier is
        # allowed to start. Both outcomes are the same fail-closed invariant:
        # either the bounded verifier times out or the budget gate stops first.
        self.assertTrue(recorded.get("failed_ids") == ["verify"] or
                        (recorded.get("failed_ids") == [] and recorded.get("control") == "budget"
                         and "seconds" in str(recorded.get("budget_exceeded"))),
                        shown)

    def test_successful_verification_that_consumes_deadline_stops_before_reviewer_creation(self):
        old = os.environ.copy(); os.environ.update(self.env)
        os.environ.pop("HELM_PIW", None)
        os.environ["HELM_AVAILABLE_MODELS"] = "openai-codex/gpt-5.6-sol"
        try:
            from helm import work, worktree
            from helm.util import write_json
            work_id = "deadline-review"
            project = {"id": "p", "path": str(self.proj), "base": "main", "mode": "high-assurance",
                       "authority": 1, "test_cmd": "true", "protected_paths": [], "gate": "native"}
            wt = worktree.create(project, work_id)
            (wt / "change.txt").write_text("change\n")
            subprocess.run(["git", "-C", str(wt), "add", "-A"], check=True)
            subprocess.run(["git", "-C", str(wt), "commit", "-qm", "change"], check=True)
            models = {phase: "openai-codex/gpt-5.6-sol"
                      for phase in ("plan", "implement", "review_correctness", "review_adversarial", "scout")}
            thinking = {phase: "high" for phase in models}
            item = {"id": work_id, "project": "p", "kind": "ship", "text": "bounded", "status": "running",
                    "phase": "implementing", "revision": 0, "created": "2026-01-01T00:00:00Z",
                    "updated": "2026-01-01T00:00:00Z", "branch": f"firstmate/{work_id}",
                    "worktree": str(wt), "history": [], "runs": [], "reviews": [], "verification": [],
                    "agent_launches": [], "session": None, "checkpoint": None, "changed_scope": [],
                    "controls": {"paused": False, "pending": [], "events": []}, "attempts": 0,
                    "scope": {"paths": ["unknown"], "claim": "global"},
                    "dispatch": {"graph": "high-assurance", "rule": "test", "models": models, "thinking": thinking},
                    "model_decision": {"models": models, "thinking": thinking},
                    "budgets": {"tokens": None, "cost": None, "seconds": 1},
                    "usage": {"tokens": 0, "cost": 0.0, "seconds": 0.0,
                              "implementer_tokens": 0, "implementer_cost": 0.0,
                              "reviewer_tokens": 0, "reviewer_cost": 0.0,
                              "tokens_evidence_complete": True, "cost_evidence_complete": True}}
            directory = self.home / "work" / work_id; directory.mkdir(parents=True)
            write_json(directory / "item.json", item)
            brief = directory / "brief.md"; brief.write_text("brief")
            session = {"agent_name": "impl", "agent_session_id": "session-1", "model": models["implement"],
                       "thinking": "high", "pane_id": "w1:p9", "tab_id": "w1:t9"}
            clock = [0.0]
            def verified(**_kwargs):
                clock[0] = 1.1
                return subprocess.CompletedProcess(["verify"], 0, "ok", "")
            with mock.patch("helm.work.time.monotonic", side_effect=lambda: clock[0]), \
                 mock.patch("helm.herdr.ensure_agent", return_value=session) as ensure, \
                 mock.patch("helm.work._model_drift"), \
                 mock.patch("helm.herdr.progress_marker", return_value="marker"), \
                 mock.patch("helm.herdr.runtime_activity", return_value={"runtime_input_sequence": 0}), \
                 mock.patch("helm.herdr.prompt_agent_monitored", return_value=(session, None)), \
                 mock.patch("helm.herdr.agent_get", return_value=session), \
                 mock.patch("helm.herdr.usage", return_value={"tokens": 1, "cost": 0.01}), \
                 mock.patch("helm.herdr.agent_read", return_value="done"), \
                 mock.patch("helm.sandbox.run_verification", side_effect=verified):
                result = work._persistent_execute(item, project, wt, brief, 10, {},
                                                  attempt_started=0.0, deadline=1.0)
            self.assertFalse(result["ok"])
            self.assertEqual(result["control"], "budget")
            self.assertEqual(result["agent_checkpoint"], "no-review-turn-started")
            self.assertEqual(ensure.call_count, 1, "reviewer creation is forbidden after deadline")
        finally:
            os.environ.clear(); os.environ.update(old)

    def test_live_pending_or_unknown_session_is_never_closed_by_label_fallback(self):
        from helm import herdr
        session = {"agent_name": "impl", "agent_session_id": "session-1", "pane_id": "w1:p7",
                   "tab_id": "w1:t7", "workspace_id": "w1", "label": "work p"}
        live = {"state": "live", "agent": {"pane_id": "w1:p7", "tab_id": "w1:t7",
                                               "agent_session_id": "session-1"}}
        with mock.patch.object(herdr, "agent_liveness", return_value=live), \
             mock.patch.object(herdr, "exact_agent_liveness",
                               return_value={"state": "live", "identity_verified": True}), \
             mock.patch.object(herdr, "session_evidence", return_value={}), \
             mock.patch.object(herdr, "runtime_activity", return_value={"runtime_turn_pending": True}), \
             mock.patch.object(herdr, "_tab_matches", return_value=True), \
             mock.patch.object(herdr, "close_tab") as close:
            self.assertFalse(herdr.close_agent_tab(session))
            close.assert_not_called()
        reused = {"state": "live", "agent": {"pane_id": "w1:p7", "tab_id": "w1:t7",
                                                "agent_session_id": "replacement"}}
        with mock.patch.object(herdr, "agent_liveness", return_value=reused), \
             mock.patch.object(herdr, "_tab_matches", return_value=True), \
             mock.patch.object(herdr, "close_tab") as close:
            self.assertFalse(herdr.close_agent_tab(session))
            close.assert_not_called()
        with mock.patch.object(herdr, "agent_liveness", return_value={"state": "unknown"}), \
             mock.patch.object(herdr, "_tab_matches", return_value=True), \
             mock.patch.object(herdr, "close_tab") as close:
            self.assertFalse(herdr.close_agent_tab(session))
            close.assert_not_called()

    def test_exact_liveness_rejects_reused_pane_or_different_session_uuid(self):
        from helm import herdr
        from helm.util import HelmError
        identity = {"agent_name": "impl", "agent_session_id": "durable-session",
                    "pane_id": "w1:p7", "workspace_id": "w1", "tab_id": "w1:t7",
                    "resolved_model": "openai-codex/gpt-5.6-sol", "resolved_thinking": "high",
                    "agent_cwd": str(self.proj),
                    "attested_process_identity": {"pid": 123, "start_sha256": "a" * 64}}
        different = {"state": "live", "agent": {"name": "impl", "pane_id": "w1:p7",
                     "workspace_id": "w1", "tab_id": "w1:t7", "agent_session_id": "replacement"}}
        self.assertEqual(herdr.exact_agent_liveness(identity, different)["state"], "unknown")

        # Herdr may omit the Pi UUID.  In that case the signed attestation and
        # current pane PID are mandatory; old evidence from a reused pane fails.
        omitted = {"state": "live", "agent": {"name": "impl", "pane_id": "w1:p7",
                   "workspace_id": "w1", "tab_id": "w1:t7"}}
        with mock.patch.object(herdr, "_verify_runtime_attestation",
                               side_effect=HelmError("attested PID is no longer in this pane")):
            result = herdr.exact_agent_liveness(identity, omitted, process_info={})
        self.assertEqual(result["state"], "unknown")
        self.assertIn("attested PID", result["reason"])
        with mock.patch.object(herdr, "_verify_runtime_attestation",
                               return_value={"pid": 123, "process_identity": identity["attested_process_identity"]}), \
             mock.patch.object(herdr, "session_evidence",
                               return_value={"agent_session_id": "durable-session"}):
            result = herdr.exact_agent_liveness(identity, omitted, process_info={})
        self.assertEqual(result["state"], "live")
        self.assertTrue(result["identity_verified"])

    def test_same_pid_with_different_birth_identity_cannot_be_steered_interrupted_or_closed(self):
        old = os.environ.copy(); os.environ.update(self.env)
        try:
            from helm import herdr, processes
            session = herdr.ensure_agent({"id": "pid-reuse", "project": "p", "session": None},
                                         self.proj, "openai-codex/gpt-5.6-sol", "high")
            herdr.prompt_agent(session["agent_name"], "materialize durable identity", 30, session)
            live = herdr.agent_liveness(session["agent_name"])
            real_field = processes._field
            def reused_field(pid, name):
                return "Thu Jan  1 00:00:00 1970" if name == "lstart" else real_field(pid, name)
            before = self.calls()
            with mock.patch.object(processes, "_field", side_effect=reused_field):
                exact = herdr.exact_agent_liveness(session, live)
                self.assertEqual(exact["state"], "unknown")
                self.assertIn("different process identity", exact["reason"])
                with self.assertRaises(SystemExit):
                    herdr.steer_agent(session["agent_name"], "do not deliver", identity=session)
                with self.assertRaises(SystemExit):
                    herdr.interrupt_agent(session["agent_name"], session)
                self.assertFalse(herdr.close_agent_tab(session))
            after = self.calls()[len(before):]
            self.assertFalse(any(call[:2] in (["agent", "prompt"], ["agent", "send-keys"],
                                              ["tab", "close"]) for call in after), after)
        finally:
            os.environ.clear(); os.environ.update(old)

    def test_manual_pause_settles_without_an_unbudgeted_checkpoint_turn(self):
        from helm import herdr
        identity = {"agent_name": "impl", "agent_session_id": "session-1"}
        with mock.patch.object(herdr, "runtime_activity", return_value={"runtime_input_sequence": 1}), \
             mock.patch.object(herdr, "_runtime_settled", side_effect=[False, True, True]), \
             mock.patch.object(herdr, "interrupt_agent") as interrupt, \
             mock.patch.object(herdr, "record_runtime_anchor", side_effect=lambda value: value), \
             mock.patch.object(herdr, "agent_get", return_value=identity), \
             mock.patch.object(herdr, "prompt_agent") as prompt:
            _, finding = herdr._wait_runtime_monitored_inner(
                "impl", 5, lambda: {"control": "pause"}, identity, 1)
        interrupt.assert_called_once_with("impl", identity)
        prompt.assert_not_called()
        self.assertEqual(finding["agent_checkpoint"], "settled-without-extra-model-turn")

    def test_reviewer_loss_never_becomes_approval_or_delivery(self):
        env = {**self.env, "FAKE_HERDR_EXECUTE": "1", "FAKE_HERDR_REVIEWER_LOSS": "1",
               "HELM_AVAILABLE_MODELS": "openai-codex/gpt-5.6-sol"}
        env.pop("HELM_PIW", None)
        self.assertEqual(subprocess.run(HELM + ["add", str(self.proj), "--id", "loss", "--test", "true",
                                                "--mode", "high-assurance"], env=env, text=True, capture_output=True).returncode, 0)
        task = json.loads(subprocess.run(HELM + ["task", "loss", "change it", "--json"], env=env,
                                         text=True, capture_output=True, check=True).stdout)
        run = subprocess.run(HELM + ["run-once"], env=env, text=True, capture_output=True)
        self.assertNotEqual(run.returncode, 0)
        shown = json.loads(subprocess.run(HELM + ["show", task["id"], "--json"], env=env,
                                          text=True, capture_output=True, check=True).stdout)
        self.assertEqual(shown["status"], "paused")
        self.assertEqual(shown["phase"], "recovery-required")
        self.assertIn("did not prove it stopped", shown["ask"]["question"])
        self.assertEqual(shown["reviews"], [])
        self.assertTrue((self.home / "worktrees" / "loss" / task["id"]).exists())
        reviewer = next(launch for launch in shown["agent_launches"] if launch["role"] == "reviewer")
        self.assertEqual(reviewer["state"], "attested")
        claims = json.loads((self.home / "scope-claims.json").read_text())["claims"]
        held = next(claim for claim in claims if claim["work_id"] == task["id"])
        self.assertTrue(held["held_for_recovery"])
        self.assertFalse(any(call[:2] == ["tab", "close"] and reviewer["tab_id"] in call
                             for call in self.calls()), "unsettled reviewer tab must not be killed automatically")

    def test_ask_notifies_captain(self):
        self.helm("add", str(self.proj), "--id", "p", "--test", "true", "--mode", "local-only")
        self.helm("task", "p", "thing [fake:ask]")
        self.helm("run-once")
        notes = [c for c in self.calls() if c[:2] == ["notification", "show"]]
        self.assertEqual(notes[0][2], "p needs you"); self.assertIn("Which auth provider?", notes[0][4])

    def test_herdr_failure_never_fails_the_task(self):
        (self.bin / "herdr").unlink(); (self.bin / "herdr").write_text("#!/bin/sh\nexit 3\n"); (self.bin / "herdr").chmod(0o755)
        self.helm("add", str(self.proj), "--id", "p", "--test", "true", "--mode", "local-only")
        it = json.loads(self.helm("task", "p", "thing", "--json").stdout)
        self.helm("run-once")
        self.assertEqual(json.loads(self.helm("show", it["id"], "--json").stdout)["status"], "ready")

    def test_banner_and_watch_once(self):
        out = self.helm("watch", "--once").stdout
        self.assertIn("F I R S T   M A T E", out); self.assertIn("crew      stopped", out); self.assertIn("none yet", out)


if __name__ == "__main__":
    unittest.main()
