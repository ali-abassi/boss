try:
    import _gitenv  # noqa: F401
except ImportError:
    from tests import _gitenv  # noqa: F401
import json, os, shutil, subprocess, tempfile, unittest
from pathlib import Path


class ControlPlaneTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()); os.environ["HELM_HOME"] = str(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_scope_intersection_is_conservative_and_disjoint_prefixes_parallelize(self):
        from helm import scope
        self.assertTrue(scope.overlap(["src/*.py"], ["src/a*"]))
        self.assertTrue(scope.overlap(["unknown"], ["docs/**"]))
        self.assertTrue(scope.overlap([".github/workflows/ci.yml"], ["src/**"]))
        self.assertFalse(scope.overlap(["src/api/**"], ["src/web/**"]))
        self.assertTrue(scope.claim("p", "one", ["src/api/**"], "a", os.getpid()))
        self.assertTrue(scope.claim("p", "two", ["src/web/**"], "b", os.getpid()))
        self.assertFalse(scope.claim("p", "three", ["src/a*"], "c", os.getpid()))

    def test_cas_rejects_stale_writer_and_redacts_every_nested_surface(self):
        from helm import control
        path = self.root / "work" / "x" / "item.json"; path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"id": "x", "revision": 0, "status": "queued", "history": []}))
        control.cas_update("x", lambda item: item.update(phase="plan"), expected_revision=0)
        with self.assertRaises(SystemExit):
            control.cas_update("x", lambda item: item.update(phase="bad"), expected_revision=0)
        redacted = control.redact({"history": ["Authorization: Bearer abcdefghijk"],
                                   "blocker": "api_key=super-secret-value", "token": "raw"})
        self.assertNotIn("abcdefghijk", json.dumps(redacted))
        self.assertNotIn("super-secret-value", json.dumps(redacted))
        self.assertEqual(redacted["token"], "[REDACTED]")
        hostile = {"accessToken": "raw", "out": "https://ali:pass@example.test/x eyJabcdefghijk.abcdefghijk.abcdefghijk\n"
                   "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----"}
        clean = json.dumps(control.redact(hostile))
        for secret in ("raw", "ali:pass", "eyJabcdefghijk", "BEGIN PRIVATE KEY"):
            self.assertNotIn(secret, clean)

    def test_controls_are_durable_consumable_and_inspection_has_every_field(self):
        from helm import control
        path = self.root / "work" / "x" / "item.json"; path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"id": "x", "revision": 0, "status": "running", "phase": "implementing",
                                    "branch": "firstmate/x", "updated": "now", "controls": {"pending": []},
                                    "runs": [], "verification": [], "reviews": [], "scope": {"paths": ["src/**"]}}))
        item = control.request("x", "steer", "use the safe parser")
        event = item["controls"]["events"][-1]
        self.assertEqual(event["state"], "pending"); self.assertEqual(len(item["controls"]["pending"]), 1)
        item = control.consume("x", [event["id"]])
        self.assertEqual(item["controls"]["events"][-1]["state"], "consumed")
        self.assertEqual(item["controls"]["pending"], [])
        for action, value in (("pause", None), ("resume", None), ("away", True), ("interrupt", None), ("recover", None)):
            item = control.request("x", action, value)
            item = control.consume("x", [item["controls"]["events"][-1]["id"]])
        self.assertEqual([e["action"] for e in item["controls"]["events"]],
                         ["steer", "pause", "resume", "away", "interrupt", "recover"])
        fields = control.inspection(item, "recent")
        for key in ("phase", "state", "last_activity", "branch", "sha", "changed_scope", "tests", "reviews",
                    "blockers", "model", "tokens", "cost", "usage_evidence", "recent_output", "controls", "rigor"):
            self.assertIn(key, fields)

    def test_stale_scope_claim_is_recovered_atomically(self):
        from helm import scope
        (self.root / "scope-claims.json").write_text(json.dumps({"version": 1, "claims": [
            {"project": "p", "work_id": "dead", "paths": ["src/**"], "pid": 999999}]}))
        self.assertTrue(scope.claim("p", "live", ["src/api/**"], "worker", os.getpid()))
        claims = json.loads((self.root / "scope-claims.json").read_text())["claims"]
        self.assertEqual([c["work_id"] for c in claims], ["live"])

    def test_model_and_thinking_metadata_fail_closed(self):
        from helm import dispatch, herdr
        good = {"agent_session_id": "s1", "pane_id": "p1", "agent_status": "idle",
                "model": "openai-codex/gpt-5.6-sol", "thinking": "high"}
        herdr.validate_agent(good, "openai-codex/gpt-5.6-sol", "high")
        for bad in ({**good, "thinking": "low"}, {k: v for k, v in good.items() if k != "model"}):
            with self.assertRaises(SystemExit):
                herdr.validate_agent(bad, "openai-codex/gpt-5.6-sol", "high")
        old = os.environ.get("HELM_AVAILABLE_MODELS")
        os.environ["HELM_AVAILABLE_MODELS"] = "openai-codex/another-model"
        try:
            with self.assertRaises(SystemExit):
                dispatch.assert_available({"models": {"implement": "openai-codex/gpt-5.6-sol"}})
        finally:
            if old is None: os.environ.pop("HELM_AVAILABLE_MODELS", None)
            else: os.environ["HELM_AVAILABLE_MODELS"] = old

    def test_pi_session_record_attests_model_thinking_identity_and_usage(self):
        from helm import herdr
        session = self.root / "session.jsonl"
        session.write_text("\n".join(json.dumps(event) for event in [
            {"type": "session", "id": "real-session"},
            {"type": "model_change", "provider": "openai-codex", "modelId": "gpt-5.6-sol"},
            {"type": "thinking_level_change", "thinkingLevel": "high"},
            {"type": "message", "message": {"role": "assistant", "usage": {
                "totalTokens": 120, "cost": {"total": 0.25}}}},
        ]) + "\n")
        agent = {"pane_id": "p1", "agent_status": "idle", "agent_session_path": str(session)}
        herdr.validate_agent(agent, "openai-codex/gpt-5.6-sol", "high")
        self.assertEqual(herdr.usage(agent), {"tokens": 120, "cost": 0.25})
        with self.assertRaises(SystemExit):
            herdr.validate_agent({**agent, "agent_session_id": "lie"}, "openai-codex/gpt-5.6-sol", "high")

    def test_latest_base_integration_and_exact_worktree_signature(self):
        from helm import worktree
        repo = self.root / "repo"; repo.mkdir()
        def g(*args): return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()
        g("init", "-q", "-b", "main"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
        (repo / "base.txt").write_text("one\n"); g("add", "-A"); g("commit", "-qm", "base")
        project = {"id": "p", "path": str(repo), "base": "main"}
        wt = worktree.create(project, "x")
        (wt / "item.txt").write_text("x\n"); subprocess.run(["git", "-C", str(wt), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(wt), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "item"], check=True)
        (repo / "new.txt").write_text("new\n"); g("add", "-A"); g("commit", "-qm", "new base")
        result = worktree.integrate_latest(project, wt)
        self.assertEqual(result["base_sha"], g("rev-parse", "main")); self.assertTrue(worktree.base_is_ancestor(project, wt))
        clean = worktree.signature(wt); (wt / "untracked.txt").write_text("mutation")
        self.assertNotEqual(clean, worktree.signature(wt))

    def test_rigor_routes_and_escalates_explainably(self):
        from helm import rigor
        scout = rigor.route({"kind": "scout", "text": "why", "scope": {"paths": ["unknown"]}})
        self.assertEqual(scout["level"], "scout")
        high = rigor.route({"kind": "ship", "text": "change auth permissions", "scope": {"paths": ["src/auth.py"]}})
        self.assertEqual(high["level"], "high-risk")
        quick = rigor.route({"kind": "ship", "text": "fix README typo", "scope": {"paths": ["README.md"]}})
        self.assertEqual(quick["level"], "quick")
        escalated = rigor.escalate(quick, verification_failed=True)
        self.assertEqual(escalated["level"], "high-risk"); self.assertIn("verification", escalated["rationale"])

    def test_review_parser_requires_real_evidence(self):
        from helm.work import _json_verdict, _headless_reviews
        self.assertEqual(_json_verdict('noise {"verdict":"accept","notes":"ok"}')["verdict"], "accept")
        with self.assertRaises(SystemExit):
            _json_verdict("looks good")
        run = self.root / "run"; run.mkdir()
        (run / "review_correctness.json").write_text(json.dumps({"verdict": "accept", "notes": "ok"}))
        item = {"dispatch": {"graph": "direct-pr"}}
        self.assertEqual(_headless_reviews(item, {"run_dir": str(run)}, "a" * 40), [])
        (run / "review_correctness.json").write_text(json.dumps(
            {"verdict": "accept", "notes": "ok", "sha": "a" * 40}))
        self.assertEqual(_headless_reviews(item, {"run_dir": str(run)}, "a" * 40)[0]["sha"], "a" * 40)


if __name__ == "__main__":
    unittest.main()
