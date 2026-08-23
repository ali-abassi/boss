try:
    import _gitenv  # noqa: F401
except ImportError:
    from tests import _gitenv  # noqa: F401
import concurrent.futures, json, multiprocessing, os, shutil, subprocess, tempfile, unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]


def racing_control(home: str, work_id: str) -> None:
    os.environ["HELM_HOME"] = home
    from helm import control
    control.request(work_id, "steer", "same guidance", request_id="network-retry-1")


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
        usage = control.redact({"tokens_evidence_complete": True, "reviewer_tokens": 12})
        self.assertEqual(usage, {"tokens_evidence_complete": True, "reviewer_tokens": 12})
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
        for action, value in (("pause", None), ("resume", None), ("interrupt", None), ("recover", None)):
            item = control.request("x", action, value)
            item = control.consume("x", [item["controls"]["events"][-1]["id"]])
        self.assertEqual([e["action"] for e in item["controls"]["events"]],
                         ["steer", "pause", "resume", "interrupt", "recover"])
        with self.assertRaises(SystemExit):
            control.request("x", "away", True)
        fields = control.inspection(item, "recent")
        for key in ("phase", "state", "last_activity", "branch", "sha", "changed_scope", "tests", "reviews",
                    "blockers", "model", "tokens", "cost", "usage_evidence", "node_budgets", "node_usage",
                    "recent_output", "controls", "rigor"):
            self.assertIn(key, fields)

    def test_duplicate_and_racing_controls_are_idempotent_and_never_lost(self):
        from helm import control
        path = self.root / "work" / "race" / "item.json"; path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"id": "race", "revision": 0, "status": "running", "phase": "implementing",
                                    "controls": {"pending": [], "events": []}, "history": [], "reviews": []}))
        processes = [multiprocessing.Process(target=racing_control, args=(str(self.root), "race")) for _ in range(8)]
        for process in processes: process.start()
        for process in processes:
            process.join(10); self.assertEqual(process.exitcode, 0)
        item = json.loads(path.read_text())
        self.assertEqual([e["id"] for e in item["controls"]["events"]], ["network-retry-1"])
        self.assertEqual(len(item["controls"]["pending"]), 1)
        with self.assertRaises(SystemExit):
            control.request("race", "steer", "different guidance", request_id="network-retry-1")

    def test_steering_crash_after_acceptance_reconciles_without_a_second_send(self):
        from helm import control, work
        path = self.root / "work" / "steer" / "item.json"; path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"id": "steer", "revision": 0, "status": "running",
                                    "controls": {"pending": [], "events": []}, "history": [], "reviews": []}))
        item = control.request("steer", "steer", "one exact message", request_id="steer-1")
        event_id = item["controls"]["events"][0]["id"]
        sent = []
        def accepted_then_transport_error(*_args, **_kwargs):
            sent.append("accepted")
            raise RuntimeError("transport died after Pi accepted input")
        with mock.patch("helm.herdr.runtime_activity", return_value={"runtime_input_sequence": 0}), \
             mock.patch("helm.herdr.accepted_input", return_value=None), \
             mock.patch("helm.herdr.steer_agent", side_effect=accepted_then_transport_error):
            with self.assertRaises(RuntimeError):
                work.deliver_steering("steer", event_id, "one exact message", {"agent_name": "impl"}, "owner-a")
        control.cas_update("steer", lambda current: current["controls"]["events"][0].update(delivery_until_epoch=0))
        with mock.patch("helm.herdr.runtime_activity", return_value={"runtime_input_sequence": 1}), \
             mock.patch("helm.herdr.accepted_input", return_value={"input_sequence": 1}), \
             mock.patch("helm.herdr.steer_agent") as resend:
            self.assertTrue(work.deliver_steering("steer", event_id, "one exact message",
                                                  {"agent_name": "impl"}, "owner-b"))
        self.assertEqual(sent, ["accepted"]); resend.assert_not_called()
        stored = json.loads(path.read_text())
        self.assertEqual(stored["controls"]["events"][0]["state"], "delivered")

    def test_cli_worker_steering_race_has_one_external_sender(self):
        from helm import control, work
        path = self.root / "work" / "steer-race" / "item.json"; path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"id": "steer-race", "revision": 0, "status": "running",
                                    "controls": {"pending": [], "events": []}, "history": [], "reviews": []}))
        item = control.request("steer-race", "steer", "race once", request_id="race-once")
        event_id = item["controls"]["events"][0]["id"]
        with mock.patch("helm.herdr.runtime_activity", return_value={"runtime_input_sequence": 0}), \
             mock.patch("helm.herdr.accepted_input", return_value=None), \
             mock.patch("helm.herdr.steer_agent", return_value={"_runtime_input_sequence": 1}) as sender:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda owner: work.deliver_steering(
                    "steer-race", event_id, "race once", {"agent_name": "impl"}, owner),
                    ("cli", "worker")))
        self.assertEqual(sender.call_count, 1)
        self.assertEqual(sorted(results), [False, True])

    def test_distinct_same_content_controls_get_distinct_ordered_pi_inputs(self):
        from helm import control, work
        path = self.root / "work" / "same-text" / "item.json"; path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"id": "same-text", "revision": 0, "status": "running",
                                    "controls": {"pending": [], "events": []}, "history": [], "reviews": []}))
        first = control.request("same-text", "steer", "do this", request_id="first")
        second = control.request("same-text", "steer", "do this", request_id="second")
        session = {"agent_name": "impl"}
        with mock.patch("helm.herdr.runtime_activity", return_value={"runtime_input_sequence": 0}), \
             mock.patch("helm.herdr.accepted_input", return_value=None), \
             mock.patch("helm.herdr.steer_agent", return_value={"_runtime_input_sequence": 1}) as sender:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(work.deliver_steering, "same-text", event_id, "do this", session, owner)
                           for event_id, owner in ((first["controls"]["events"][0]["id"], "one"),
                                                   (second["controls"]["events"][1]["id"], "two"))]
                results = [future.result() for future in futures]
        self.assertEqual(sender.call_count, 1)
        self.assertEqual(sorted(results), [False, True])
        with mock.patch("helm.herdr.runtime_activity", return_value={"runtime_input_sequence": 1}), \
             mock.patch("helm.herdr.accepted_input", return_value=None), \
             mock.patch("helm.herdr.steer_agent", return_value={"_runtime_input_sequence": 2}) as sender2:
            self.assertTrue(work.deliver_steering("same-text", "second", "do this", session, "two-retry"))
        self.assertEqual(sender2.call_count, 1)
        events = json.loads(path.read_text())["controls"]["events"]
        self.assertEqual([event.get("input_sequence") for event in events], [1, 2])

    def test_stale_scope_claim_stays_blocking_until_confirmed_reconciliation(self):
        from helm import scope
        (self.root / "scope-claims.json").write_text(json.dumps({"version": 1, "claims": [
            {"project": "p", "work_id": "dead", "paths": ["src/**"], "pid": 999999}]}))
        self.assertFalse(scope.claim("p", "live", ["src/api/**"], "worker", os.getpid()))
        claims = json.loads((self.root / "scope-claims.json").read_text())["claims"]
        self.assertEqual([c["work_id"] for c in claims], ["dead"])

    def test_unsettled_interrupt_pauses_item_and_retains_collision_claim(self):
        from helm import herdr, scope, work
        path = self.root / "work" / "p-unsettled" / "item.json"; path.parent.mkdir(parents=True)
        item = {"id": "p-unsettled", "project": "p", "status": "running", "phase": "implementing",
                "revision": 0, "created": "2026-01-01T00:00:00Z", "updated": "2026-01-01T00:00:00Z",
                "lease": {"pid": os.getpid(), "owner": "test"}, "controls": {"paused": False},
                "history": [], "attempts": 0, "session": {"agent_name": "impl"}}
        path.write_text(json.dumps(item))
        claim_token = scope.claim("p", "p-unsettled", ["src/**"], "test", os.getpid())
        self.assertTrue(claim_token)
        item["lease"]["claim_token"] = claim_token
        path.write_text(json.dumps(item))
        with mock.patch("helm.work._execute", side_effect=herdr.UnsettledAgentError("still editing")):
            with self.assertRaises(herdr.UnsettledAgentError): work.execute(item)
        stored = json.loads(path.read_text())
        claim = json.loads((self.root / "scope-claims.json").read_text())["claims"][0]
        self.assertEqual(stored["status"], "paused"); self.assertEqual(stored["phase"], "recovery-required")
        self.assertTrue(claim["held_for_recovery"]); self.assertIsNone(claim["pid"])

    def test_exhausted_seconds_and_unproven_historical_usage_never_start_a_turn(self):
        from helm import work
        path = self.root / "work" / "bounded" / "item.json"; path.parent.mkdir(parents=True)
        path.write_text(json.dumps({
            "id": "bounded", "project": "p", "status": "running", "phase": "implementing",
            "revision": 0, "created": "2026-01-01T00:00:00Z", "updated": "2026-01-01T00:00:00Z",
            "lease": {"pid": os.getpid(), "owner": "test"}, "controls": {"paused": False},
            "history": [], "attempts": 1, "runs": [{"attempt": 1}],
            "budgets": {"tokens": 100, "cost": None, "seconds": 10},
            "usage": {"tokens": 0, "cost": 0.0, "seconds": 9.5},
        }))
        with mock.patch("helm.work._execute", side_effect=AssertionError("no model turn")):
            stopped = work.execute(work.load("bounded"))
        self.assertEqual(stopped["status"], "paused")
        self.assertIn("tokens usage evidence is incomplete", stopped["ask"]["context"])
        self.assertIn("less than one bounded second", stopped["ask"]["context"])

    def test_unsettled_session_usage_is_harvested_before_scope_hold(self):
        from helm import herdr, scope, work
        path = self.root / "work" / "metered" / "item.json"; path.parent.mkdir(parents=True)
        token = scope.claim("p", "metered", ["src/**"], "test", os.getpid())
        session = {"agent_name": "impl", "agent_session_id": "session-1",
                   "agent_events_path": str(self.root / "events.jsonl")}
        item = {"id": "metered", "project": "p", "status": "running", "phase": "implementing",
                "revision": 0, "created": "2026-01-01T00:00:00Z", "updated": "2026-01-01T00:00:00Z",
                "lease": {"pid": os.getpid(), "owner": "test", "claim_token": token},
                "controls": {"paused": False}, "history": [], "attempts": 0, "runs": [],
                "session": session, "agent_launches": [], "budgets": {"tokens": 200, "cost": 2, "seconds": 60},
                "usage": {"tokens": 0, "cost": 0.0, "seconds": 0.0,
                          "tokens_evidence_complete": True, "cost_evidence_complete": True}}
        path.write_text(json.dumps(item))
        with mock.patch("helm.work._execute", side_effect=herdr.UnsettledAgentError("unknown settle")), \
             mock.patch("helm.herdr.session_evidence", return_value={"tokens": 123, "cost": 0.75}), \
             mock.patch("helm.herdr.runtime_activity", return_value={"runtime_input_sequence": 1}):
            with self.assertRaises(herdr.UnsettledAgentError):
                work.execute(work.load("metered"))
        stored = work.load("metered")
        self.assertEqual(stored["usage"]["tokens"], 123)
        self.assertEqual(stored["usage"]["cost"], 0.75)
        self.assertTrue(stored["usage"]["tokens_evidence_complete"])

    def test_closed_reviewer_usage_remains_billable_and_complete_only_with_receipts(self):
        from helm import work
        path = self.root / "work" / "closed-review" / "item.json"; path.parent.mkdir(parents=True)
        path.write_text(json.dumps({
            "id": "closed-review", "project": "p", "status": "failed", "phase": "failed",
            "revision": 0, "created": "2026-01-01T00:00:00Z", "updated": "2026-01-01T00:00:00Z",
            "controls": {"pending": [], "events": []}, "history": [], "attempts": 1, "runs": [],
            "session": None, "agent_launches": [{
                "launch_id": "review-launch", "role": "reviewer", "state": "closed",
                "agent_session_id": "review-session", "agent_session_path": str(self.root / "review.jsonl"),
            }],
            "usage": {"tokens": 0, "cost": 0.0, "seconds": 0.0,
                      "reviewer_tokens": 0, "reviewer_cost": 0.0,
                      "tokens_evidence_complete": True, "cost_evidence_complete": True},
        }))
        with mock.patch("helm.herdr.usage", return_value={"tokens": 1234, "cost": 0.25}) as usage, \
             mock.patch("helm.herdr.runtime_activity", return_value={"runtime_input_sequence": 1}):
            stored = work._harvest_usage("closed-review")
        usage.assert_called_once()
        self.assertEqual(stored["usage"]["reviewer_tokens"], 1234)
        self.assertEqual(stored["usage"]["reviewer_cost"], 0.25)
        self.assertEqual(stored["usage"]["receipts"]["review-session"]["role"], "reviewer")
        self.assertTrue(stored["usage"]["tokens_evidence_complete"])

    def test_only_explicit_recover_can_exchange_and_retain_a_recovery_hold(self):
        from helm import scope, work
        from helm.util import HelmError
        path = self.root / "work" / "recover-held" / "item.json"; path.parent.mkdir(parents=True)
        old = scope.claim("p", "recover-held", ["src/**"], "old", os.getpid())
        self.assertTrue(old); self.assertTrue(scope.hold("recover-held", old, "unknown settlement"))
        path.write_text(json.dumps({
            "id": "recover-held", "project": "p", "status": "paused", "phase": "recovery-required",
            "revision": 0, "created": "2026-01-01T00:00:00Z", "updated": "2026-01-01T00:00:00Z",
            "controls": {"paused": True, "pending": [], "events": []}, "history": [], "attempts": 1,
            "runs": [], "session": {"agent_name": "impl", "agent_session_id": "session-old"},
            "agent_launches": [], "scope": {"paths": ["src/**"], "claim": "paths"},
            "recovery_claim_token": old, "budgets": {"tokens": None, "cost": None, "seconds": None},
            "usage": {"tokens": 0, "cost": 0.0, "seconds": 0.0,
                      "tokens_evidence_complete": True, "cost_evidence_complete": True},
        }))
        ordinary = subprocess.run([str(REPO / "bin" / "helm"), "resume", "recover-held"],
                                  env={**os.environ}, text=True, capture_output=True)
        self.assertNotEqual(ordinary.returncode, 0)
        self.assertIn("use explicit `helm recover`", ordinary.stderr)
        held = json.loads((self.root / "scope-claims.json").read_text())["claims"][0]
        self.assertEqual(held["claim_token"], old); self.assertTrue(held["held_for_recovery"])

        recovered = subprocess.run([str(REPO / "bin" / "helm"), "recover", "recover-held",
                                    "--request-id", "captain-recover-1"],
                                   env={**os.environ}, text=True, capture_output=True)
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        leased = work.claim_next("recovery-worker")
        self.assertTrue((leased.get("lease") or {}).get("recovery_attempt"))
        new_token = leased["lease"]["claim_token"]
        self.assertNotEqual(new_token, old)
        with mock.patch("helm.work._execute", side_effect=HelmError("Herdr liveness is still unknown")):
            with self.assertRaises(HelmError):
                work.execute(leased)
        stored = work.load("recover-held")
        claim = json.loads((self.root / "scope-claims.json").read_text())["claims"][0]
        self.assertEqual(stored["status"], "paused")
        self.assertEqual(stored["phase"], "recovery-required")
        self.assertEqual(stored["recovery_claim_token"], new_token)
        self.assertEqual(claim["claim_token"], new_token)
        self.assertTrue(claim["held_for_recovery"])

    def test_old_runner_cannot_release_or_hold_a_newer_scope_claim(self):
        from helm import scope
        old = scope.claim("p", "same", ["src/**"], "old", os.getpid())
        self.assertFalse(scope.claim("p", "same", ["src/**"], "unauthorized", os.getpid()))
        self.assertTrue(scope.hold("same", old, "explicit recovery boundary"))
        newer = scope.claim("p", "same", ["src/**"], "new", os.getpid(), replace_token=old)
        self.assertTrue(old); self.assertTrue(newer); self.assertNotEqual(old, newer)
        self.assertFalse(scope.release("same", old))
        self.assertFalse(scope.hold("same", old, "stale runner"))
        claim = json.loads((self.root / "scope-claims.json").read_text())["claims"][0]
        self.assertEqual(claim["claim_token"], newer)
        self.assertEqual(claim["owner"], "new")
        self.assertTrue(scope.release("same", newer))

    def test_recovery_claim_exchange_stays_held_if_lease_transition_crashes(self):
        from helm import scope, work
        path = self.root / "work" / "exchange-crash" / "item.json"; path.parent.mkdir(parents=True)
        old = scope.claim("p", "exchange-crash", ["src/**"], "old", os.getpid())
        self.assertTrue(scope.hold("exchange-crash", old, "recover"))
        path.write_text(json.dumps({
            "id": "exchange-crash", "project": "p", "status": "queued", "phase": "queued",
            "revision": 0, "created": "2026-01-01T00:00:00Z", "updated": "2026-01-01T00:00:00Z",
            "scope": {"paths": ["src/**"], "claim": "paths"}, "recovery_claim_token": old,
            "controls": {"paused": False, "pending": [], "events": []}, "history": [], "attempts": 0,
        }))
        with mock.patch("helm.work.transition", side_effect=RuntimeError("crash before item lease CAS")):
            with self.assertRaises(RuntimeError):
                work.claim_next("recovery-worker")
        stored = work.load("exchange-crash")
        claim = json.loads((self.root / "scope-claims.json").read_text())["claims"][0]
        self.assertNotEqual(claim["claim_token"], old)
        self.assertEqual(stored["recovery_claim_token"], claim["claim_token"])
        self.assertEqual(stored["phase"], "recovery-required")
        self.assertTrue(claim["held_for_recovery"])

    def test_cancellation_recovers_a_crash_after_the_intact_quarantine_move(self):
        from helm import work, worktree
        from helm.util import write_json
        repo = self.root / "repo"; repo.mkdir()
        def g(*args):
            return subprocess.run(["git", "-C", str(repo), *args], check=True,
                                  capture_output=True, text=True).stdout.strip()
        g("init", "-q", "-b", "main"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
        (repo / "base.txt").write_text("base\n"); g("add", "-A"); g("commit", "-qm", "base")
        project = {"id": "p", "path": str(repo), "base": "main", "mode": "local-only",
                   "authority": 1, "test_cmd": "true", "protected_paths": [], "gate": "native"}
        write_json(self.root / "projects.json", {"projects": {"p": project}})
        write_json(self.root / "supervisor.json", {"version": 1, "observations": {},
                                                    "away": {"enabled": False}})
        write_json(self.root / "wakes.json", {"version": 1, "next_id": 1, "events": []})
        wt = worktree.create(project, "p-crash")
        (wt / "work.txt").write_text("preserve me\n")
        subprocess.run(["git", "-C", str(wt), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(wt), "commit", "-qm", "work"], check=True)
        directory = self.root / "work" / "p-crash"; directory.mkdir(parents=True)
        write_json(directory / "item.json", {
            "id": "p-crash", "project": "p", "status": "failed", "phase": "failed", "revision": 0,
            "created": "2026-01-01T00:00:00Z", "updated": "2026-01-01T00:00:00Z",
            "branch": "firstmate/p-crash", "worktree": str(wt), "history": [], "session": None,
            "agent_launches": [], "controls": {"pending": [], "events": []}, "dispatch": {"graph": "local-only"}
        })
        original = worktree.quarantine
        def moved_then_crashed(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("process died after git worktree move")
        with mock.patch("helm.worktree.quarantine", side_effect=moved_then_crashed):
            with self.assertRaises(RuntimeError):
                work.cancel("p-crash", discard=True)
        midway = work.load("p-crash")
        destination = Path(midway["cancellation"]["quarantine_path"])
        self.assertEqual(midway["cancellation"]["state"], "filesystem-requested")
        self.assertFalse(wt.exists()); self.assertTrue((destination / "work.txt").is_file())
        completed = work.cancel("p-crash", discard=True)
        self.assertEqual(completed["status"], "cancelled")
        self.assertTrue((destination / "work.txt").is_file())

    def test_model_and_thinking_metadata_fail_closed(self):
        from helm import dispatch, herdr
        # Model/thinking fields copied from launch arguments are not identity
        # evidence. Only Pi runtime attestation or its durable JSONL can make
        # this otherwise plausible payload acceptable.
        good = {"agent_session_id": "s1", "pane_id": "p1", "agent_status": "idle",
                "model": "openai-codex/gpt-5.6-sol", "thinking": "high"}
        for bad in (good, {**good, "thinking": "low"}, {k: v for k, v in good.items() if k != "model"}):
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

    def test_retry_signature_ignores_volatile_session_time_and_sha_evidence(self):
        from helm.work import _failure_signature
        one = {"failed_ids": ["review_correctness"],
               "error": "review failed at 2026-01-01T00:00:00Z review-x=1234 commit " + "a" * 40}
        two = {"failed_ids": ["review_correctness"],
               "error": "review failed at 2026-02-02T03:04:05Z review-y=9876 commit " + "b" * 40}
        self.assertEqual(_failure_signature(one, one["error"]), _failure_signature(two, two["error"]))

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
            {"verdict": "accept", "notes": "ok", "sha": "a" * 40, "base_sha": "b" * 40}))
        review = _headless_reviews(item, {"run_dir": str(run)}, "a" * 40, "b" * 40)[0]
        self.assertEqual(review["sha"], "a" * 40); self.assertEqual(review["base_sha"], "b" * 40)


if __name__ == "__main__":
    unittest.main()
