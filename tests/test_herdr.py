"""Inside Herdr the fleet is visible without a redundant board tab."""
try:
    import _gitenv  # noqa: F401  (git hygiene for temp repos)
except ImportError:
    from tests import _gitenv  # noqa: F401
import json, os, subprocess, sys, tempfile, unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HELM = [sys.executable, str(REPO / "bin" / "helm")]


class HerdrTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()); self.home = self.tmp / "home"; self.bin = self.tmp / "bin"; self.bin.mkdir()
        (self.bin / "herdr").symlink_to(REPO / "tests" / "fake_herdr.py")
        self.log = self.tmp / "herdr-calls.jsonl"; self.log.touch()
        self.proj = self.tmp / "proj"; self.proj.mkdir()
        g = lambda *a: subprocess.run(["git", "-C", str(self.proj), *a], check=True, capture_output=True)
        g("init", "-q", "-b", "main"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
        (self.proj / "README.md").write_text("x"); g("add", "-A"); g("commit", "-qm", "init")
        self.env = {**os.environ, "HELM_HOME": str(self.home), "HELM_PIW": str(REPO / "tests" / "fake_piw.py"),
                    "PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_HERDR_LOG": str(self.log),
                    "HERDR_ENV": "1", "HERDR_WORKSPACE_ID": "w1", "HERDR_SESSION": "pi-x", "HERDR_PANE_ID": "w1:p1"}

    def helm(self, *args, check=True):
        r = subprocess.run(HELM + list(args), env=self.env, text=True, capture_output=True)
        if check and r.returncode != 0:
            self.fail(f"helm {' '.join(args)} failed ({r.returncode}):\n{r.stdout}\n{r.stderr}")
        return r

    def calls(self):
        return [json.loads(l) for l in self.log.read_text().splitlines() if l.strip()]

    def test_up_runs_workers_in_background_without_empty_herdr_tabs(self):
        out = self.helm("up", "--workers", "3").stdout
        self.assertEqual(out, "")
        creates = [c for c in self.calls() if c[:2] == ["tab", "create"]]
        self.assertEqual(creates, [])
        pids = json.loads((self.home / "daemon.pid").read_text())
        self.assertEqual(len(pids), 3)
        self.assertEqual(self.helm("up").stdout, "")                         # idempotent and quiet
        self.assertIn("background", self.helm("status").stdout)
        self.helm("down")
        closes = [c for c in self.calls() if c[:2] == ["tab", "close"]]
        self.assertEqual(closes, [])
        self.assertFalse((self.home / "herdr.json").exists())

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
        self.assertIn(["--session", "firstmate", "workspace", "list"], calls)
        run = next(c for c in calls if c[2:4] == ["pane", "run"])
        self.assertIn("pi-firstmate", run[-1])
        self.assertEqual(calls[-1], ["session", "attach", "firstmate"])

    def test_new_workspace_closes_the_default_empty_shell_tab(self):
        env = {k: v for k, v in self.env.items() if not k.startswith("HERDR_")}
        env.update(FAKE_HERDR_LOG=str(self.log), FAKE_HERDR_NEW_WORKSPACE="1")
        r = subprocess.run([str(REPO / "bin" / "pi-firstmate")], env=env, text=True, capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.calls()
        self.assertTrue(any(c[2:4] == ["workspace", "create"] for c in calls))
        self.assertIn(["--session", "firstmate", "tab", "close", "w1:t-default"], calls)

    def test_existing_shell_only_firstmate_tab_is_restarted_and_focused(self):
        env = {k: v for k, v in self.env.items() if not k.startswith("HERDR_")}
        env.update(FAKE_HERDR_LOG=str(self.log), FAKE_HERDR_EXISTING_DEAD_MATE="1")
        r = subprocess.run([str(REPO / "bin" / "pi-firstmate")], env=env, text=True, capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.calls()
        self.assertIn(["--session", "firstmate", "pane", "run", "w1:p-mate",
                       str(REPO / "bin" / "pi-firstmate")], calls)
        self.assertIn(["--session", "firstmate", "tab", "focus", "w1:t-mate"], calls)
        self.assertEqual(calls[-1], ["session", "attach", "firstmate"])

    def test_real_implementer_identity_reconnects_and_reviewers_are_fresh(self):
        old = os.environ.copy(); os.environ.update(self.env)
        try:
            from helm import herdr
            item = {"id": "p-item", "project": "p", "session": None}
            first = herdr.ensure_agent(item, self.proj, "openai-codex/gpt-5.6-sol", "high")
            self.assertTrue(first["agent_session_id"].startswith("real-"))
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

    def test_dead_agent_uses_checkpoint_fallback_instead_of_false_reconnect(self):
        old = os.environ.copy(); os.environ.update(self.env)
        try:
            from helm import herdr
            item = {"id": "p-dead", "project": "p", "session": None}
            first = herdr.ensure_agent(item, self.proj, "openai-codex/gpt-5.6-sol", "high")
            item["session"] = first
            os.environ["FAKE_HERDR_AGENT_STATUS"] = "dead"
            replacement = herdr.ensure_agent(item, self.proj, "openai-codex/gpt-5.6-sol", "high")
            self.assertFalse(replacement["reconnected"])
            self.assertNotEqual(replacement["agent_session_id"], first["agent_session_id"])
        finally:
            os.environ.clear(); os.environ.update(old)

    def test_live_steering_is_submitted_without_waiting_on_active_turn(self):
        old = os.environ.copy(); os.environ.update(self.env)
        try:
            from helm import herdr
            herdr.steer_agent("impl-x", "narrow the parser")
            prompt = [c for c in self.calls() if c[:2] == ["agent", "prompt"]][-1]
            self.assertNotIn("--wait", prompt)
            self.assertIn("narrow the parser", prompt[3])
        finally:
            os.environ.clear(); os.environ.update(old)

    def test_ship_runs_in_real_persistent_agent_and_reviews_bind_exact_sha(self):
        env = {**self.env, "FAKE_HERDR_EXECUTE": "1",
               "HELM_AVAILABLE_MODELS": "openai-codex/gpt-5.6-sol"}
        env.pop("HELM_PIW", None)
        r = subprocess.run(HELM + ["add", str(self.proj), "--id", "persistent", "--test", "true",
                                  "--mode", "no-mistakes"], env=env, text=True, capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        task = subprocess.run(HELM + ["task", "persistent", "change it", "--json"], env=env,
                              text=True, capture_output=True)
        self.assertEqual(task.returncode, 0, task.stderr); item = json.loads(task.stdout)
        run = subprocess.run(HELM + ["run-once"], env=env, text=True, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        shown = json.loads(subprocess.run(HELM + ["show", item["id"], "--json"], env=env,
                                          text=True, capture_output=True, check=True).stdout)
        self.assertEqual(shown["status"], "ready", shown)
        self.assertTrue(shown["session"]["agent_session_id"].startswith("real-impl-"))
        self.assertEqual(len(shown["reviews"]), 2)
        self.assertTrue(all(review["sha"] == shown["head_sha"] for review in shown["reviews"]))
        starts = [call for call in self.calls() if call[:2] == ["agent", "start"]]
        self.assertEqual(len(starts), 3, "one persistent implementer plus two fresh reviewers")

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
        self.assertIn("F I R S T   M A T E", out); self.assertIn("workers   stopped", out); self.assertIn("none yet", out)


if __name__ == "__main__":
    unittest.main()
