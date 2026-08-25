"""Behavioral contract for the deterministic planning pulse backend."""
try:
    import _gitenv  # noqa: F401
except ImportError:
    from tests import _gitenv  # noqa: F401

import copy
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from bossctl import doctor, planning
from bossctl.paths import planning_file
from bossctl.util import BossError, write_json

REPO = Path(__file__).resolve().parents[1]
BOSS = str(REPO / "bin" / "bossctl")
USAGE = {"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 0,
         "cache_write_tokens": 0, "total_tokens": 15}


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.home = self.root / "home"; self.home.mkdir()
        os.environ["BOSS_HOME"] = str(self.home)
        write_json(self.home / "supervisor.json",
                   {"version": 1, "observations": {}, "away": {"enabled": False}})
        write_json(self.home / "wakes.json", {"version": 1, "next_id": 1, "events": []})

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def item(self, item_id="p-task", **changes):
        value = {
            "id": item_id, "project": "p", "status": "queued", "kind": "ship",
            "phase": "queued", "text": "ship the release", "labels": ["release"],
            "attempts": 0, "max_attempts": 3, "branch": f"boss/{item_id}",
            "head_sha": "1" * 40, "pr_url": None, "scope": {"paths": ["src/"]},
            "ask": None, "failure_notes": [], "runs": [],
            "created": "2026-01-01T00:00:00Z", "updated": "2026-01-01T00:00:00Z",
            "revision": 1, "lease": None, "session": None, "agent_launches": [],
            "history": [], "controls": {"events": [], "pending": []},
        }
        value.update(changes)
        return value

    def put_item(self, value):
        directory = self.home / "work" / value["id"]; directory.mkdir(parents=True, exist_ok=True)
        write_json(directory / "item.json", value)
        return directory / "item.json"

    def put_wake(self, **changes):
        event = {"id": "1", "key": "wake-key", "item_id": "p-task",
                 "classification": "needs-you", "reason": "approval needed",
                 "acknowledged": False}
        event.update(changes)
        write_json(self.home / "wakes.json", {"version": 1, "next_id": 2, "events": [event]})
        return event

    def create_event(self, *, epoch=100.0):
        result = planning.tick(force=True, at=epoch)
        self.assertTrue(result["created"], result)
        return result["event"]

    def generating_event(self, *, epoch=100.0, consumer="pi"):
        event = self.create_event(epoch=epoch)
        planning.claim(consumer, event_id=event["id"], at=epoch)
        return planning.begin(event["id"], consumer, at=epoch)

    def complete(self, event_id, consumer="pi"):
        return planning.complete(event_id, consumer, provider="openai-codex", model="gpt-5.6-sol",
                                 response_sha256="a" * 64, usage=dict(USAGE), at=200)

    def test_absent_status_off_and_doctor_are_read_only_and_do_not_create_state(self):
        self.assertFalse(planning_file().exists())
        self.assertEqual(planning.status()["enabled"], False)
        self.assertFalse(planning_file().exists())
        self.assertEqual(planning.disable()["enabled"], False)
        self.assertFalse(planning_file().exists())
        doctor.audit(network=False)
        self.assertFalse(planning_file().exists())

    def test_on_off_and_one_shot_does_not_enable_future_pulses(self):
        enabled = planning.enable(at=10)
        self.assertTrue(enabled["enabled"]); self.assertTrue(planning_file().exists())
        disabled = planning.disable(at=11)
        self.assertFalse(disabled["enabled"])
        one_shot = planning.tick(force=True, at=12)
        self.assertTrue(one_shot["created"])
        self.assertFalse(planning.status()["enabled"])
        self.assertEqual(planning.tick(at=13)["reason"], "disabled")

    def test_cli_status_on_off_and_now(self):
        env = {**os.environ, "BOSS_HOME": str(self.home)}
        status = subprocess.run([BOSS, "planning", "status", "--json"], env=env,
                                text=True, capture_output=True, check=True)
        self.assertFalse(json.loads(status.stdout)["initialized"])
        self.assertFalse(planning_file().exists())
        subprocess.run([BOSS, "planning", "on", "--json"], env=env, check=True,
                       text=True, capture_output=True)
        subprocess.run([BOSS, "planning", "off", "--json"], env=env, check=True,
                       text=True, capture_output=True)
        result = subprocess.run([BOSS, "planning", "now", "--json"], env=env, check=True,
                                text=True, capture_output=True)
        self.assertTrue(json.loads(result.stdout)["created"])
        self.assertFalse(json.loads(planning_file().read_text())["enabled"])

    def test_fingerprint_ignores_capture_time_revision_updated_activity_lease_and_session(self):
        first = self.item(revision=1, updated="2026-01-01T00:00:00Z",
                          activity={"last": "old"}, lease={"pid": 1}, session={"pane_id": "a"})
        second = copy.deepcopy(first)
        second.update(revision=999, updated="2030-01-01T00:00:00Z",
                      activity={"last": "new"}, lease={"pid": 999}, session={"pane_id": "b"})
        with mock.patch("bossctl.planning.work.all_items", return_value=[first]), \
             mock.patch("bossctl.planning.supervisor.pending", return_value=[]):
            a = planning.capture_snapshot(captured_at="2026-01-01T00:00:00Z")
        with mock.patch("bossctl.planning.work.all_items", return_value=[second]), \
             mock.patch("bossctl.planning.supervisor.pending", return_value=[]):
            b = planning.capture_snapshot(captured_at="2030-01-01T00:00:00Z")
        self.assertEqual(planning.semantic_fingerprint(a), planning.semantic_fingerprint(b))

    def test_fingerprint_ignores_run_usage_cost_time_and_other_bookkeeping(self):
        first = self.item(runs=[{"attempt": 1, "ok": True, "failed_ids": [], "sha": "2" * 40,
                                 "tokens": 10, "cost": 0.1, "attempt_tokens": 10,
                                 "attempt_cost": 0.1, "attempt_seconds": 5, "at": "old",
                                 "usage_total": {"tokens": 10}, "node_usage_total": {"verify": 2}}])
        second = copy.deepcopy(first)
        second["runs"][0].update(tokens=999999, cost=500, attempt_tokens=777,
                                  attempt_cost=99, attempt_seconds=999, at="new",
                                  usage_total={"tokens": 999999}, node_usage_total={"verify": 999})
        def capture(value):
            with mock.patch("bossctl.planning.work.all_items", return_value=[value]), \
                 mock.patch("bossctl.planning.supervisor.pending", return_value=[]):
                return planning.capture_snapshot(captured_at="2026-01-01T00:00:00Z")
        a, b = capture(first), capture(second)
        self.assertEqual(planning.semantic_fingerprint(a), planning.semantic_fingerprint(b))
        meaningful = copy.deepcopy(second); meaningful["runs"][0]["sha"] = "3" * 40
        self.assertNotEqual(planning.semantic_fingerprint(a), planning.semantic_fingerprint(capture(meaningful)))
        self.assertNotIn("tokens", json.dumps(a["items"][0]["latest_run"]))

    def test_fingerprint_changes_for_meaningful_item_run_and_wake_evidence(self):
        base = self.item()
        changed = copy.deepcopy(base); changed["status"] = "failed"
        run = copy.deepcopy(base); run["runs"] = [{"attempt": 1, "ok": False, "failed_ids": ["test"]}]
        wake = {"id": "7", "item_id": "p-task", "classification": "needs-you", "reason": "choose"}
        def capture(item, wakes=()):
            with mock.patch("bossctl.planning.work.all_items", return_value=[item]), \
                 mock.patch("bossctl.planning.supervisor.pending", return_value=list(wakes)):
                return planning.capture_snapshot(captured_at="2026-01-01T00:00:00Z")
        fingerprints = {planning.semantic_fingerprint(capture(base)),
                        planning.semantic_fingerprint(capture(changed)),
                        planning.semantic_fingerprint(capture(run)),
                        planning.semantic_fingerprint(capture(base, [wake]))}
        self.assertEqual(len(fingerprints), 4)

    def test_removed_item_is_counted_retained_and_rendered_as_a_citable_change(self):
        item = self.item()
        with mock.patch("bossctl.planning.work.all_items", return_value=[item]), \
             mock.patch("bossctl.planning.supervisor.pending", return_value=[]):
            first = planning.capture_snapshot(captured_at="2026-01-01T00:00:00Z")
        with mock.patch("bossctl.planning.work.all_items", return_value=[]), \
             mock.patch("bossctl.planning.supervisor.pending", return_value=[]):
            second = planning.capture_snapshot(planning.item_fingerprints(first), first["items"],
                                               captured_at="2026-01-02T00:00:00Z")
        self.assertEqual(second["changed_item_ids"], ["p-task"])
        self.assertEqual(second["removed_items"][0]["source_id"], "item:p-task")
        recap = planning.build_recap(second)
        self.assertIn("changed=1", recap); self.assertIn("item:p-task", recap)
        self.assertIn("removed · changed", recap)

    def test_recap_is_byte_deterministic_tiered_and_lists_only_frozen_source_ids(self):
        item = self.item(runs=[{"attempt": 1, "ok": True}], status="ready")
        wake = {"id": "7", "item_id": "p-task", "classification": "needs-you", "reason": "choose"}
        with mock.patch("bossctl.planning.work.all_items", return_value=[item]), \
             mock.patch("bossctl.planning.supervisor.pending", return_value=[wake]):
            snapshot = planning.capture_snapshot(captured_at="2026-01-01T00:00:00Z")
        first = planning.build_recap(snapshot); second = planning.build_recap(copy.deepcopy(snapshot))
        self.assertEqual(first.encode(), second.encode())
        for marker in ("ADVISORY — NO ACTION TAKEN", "Tier 0", "Tier 1", "Tier 2",
                       "item:p-task", "run:p-task:1", "event:7"):
            self.assertIn(marker, first)
        self.assertEqual(planning.source_ids(snapshot), ["event:7", "item:p-task", "run:p-task:1"])

    def test_capture_never_mutates_objects_or_canonical_item_and_wake_files(self):
        item_path = self.put_item(self.item())
        wake = self.put_wake()
        before_objects = copy.deepcopy((self.item(), wake))
        item_bytes = item_path.read_bytes(); wake_bytes = (self.home / "wakes.json").read_bytes()
        # A pending wake normally blocks tick, but raw snapshot capture must still be observational.
        planning.capture_snapshot(captured_at="2026-01-01T00:00:00Z")
        self.assertEqual(item_path.read_bytes(), item_bytes)
        self.assertEqual((self.home / "wakes.json").read_bytes(), wake_bytes)
        objects = [self.item()]
        with mock.patch("bossctl.planning.work.all_items", return_value=objects), \
             mock.patch("bossctl.planning.supervisor.pending", return_value=[wake]):
            planning.capture_snapshot(captured_at="2026-01-01T00:00:00Z")
        self.assertEqual((objects[0], wake), before_objects)

    def test_unstable_sources_fail_closed_after_bounded_attempts(self):
        versions = [self.item(status="queued"), self.item(status="running")] * planning.CAPTURE_ATTEMPTS
        with mock.patch("bossctl.planning.work.all_items", side_effect=[[value] for value in versions]), \
             mock.patch("bossctl.planning.supervisor.pending", return_value=[]):
            with self.assertRaises(BossError) as raised:
                planning.capture_snapshot(captured_at="2026-01-01T00:00:00Z")
        self.assertIn("changed during", raised.exception.msg)

    def test_concurrent_ticks_deduplicate_one_fingerprint(self):
        self.put_item(self.item()); planning.enable(at=100)
        results, errors = [], []
        barrier = threading.Barrier(12)
        def run():
            try:
                barrier.wait(); results.append(planning.tick(at=100))
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)
        threads = [threading.Thread(target=run) for _ in range(12)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(5)
        self.assertFalse(errors)
        self.assertEqual(sum(bool(value["created"]) for value in results), 1)
        state = json.loads(planning_file().read_text())
        self.assertEqual(len(state["events"]), 1)

    def test_unchanged_cadence_doubles_but_never_exceeds_six_hours(self):
        self.put_item(self.item()); planning.enable(at=0)
        self.assertTrue(planning.tick(force=True, at=0)["created"])
        for index in range(12):
            result = planning.tick(force=True, at=1 + index)
            self.assertFalse(result["created"]); self.assertEqual(result["reason"], "unchanged")
        state = json.loads(planning_file().read_text())
        self.assertEqual(state["interval_seconds"], planning.MAX_CADENCE_SECONDS)
        self.assertLessEqual(state["interval_seconds"], 6 * 60 * 60)

    def test_away_and_normal_wake_have_priority_without_creating_planning_state(self):
        write_json(self.home / "supervisor.json",
                   {"version": 1, "observations": {}, "away": {"enabled": True}})
        self.assertEqual(planning.tick(force=True, at=1)["reason"], "away")
        self.assertFalse(planning_file().exists())
        write_json(self.home / "supervisor.json",
                   {"version": 1, "observations": {}, "away": {"enabled": False}})
        self.put_wake()
        self.assertEqual(planning.tick(force=True, at=2)["reason"], "normal-wake-priority")
        self.assertFalse(planning_file().exists())

    def test_malformed_and_oversized_state_fail_closed_without_rewrite(self):
        planning_file().write_text('{"version": 999}')
        before = planning_file().read_bytes()
        self.assertFalse(planning.summary()["healthy"])
        with self.assertRaises(BossError): planning.status()
        self.assertEqual(planning_file().read_bytes(), before)
        planning_file().write_bytes(b"x" * (planning.MAX_STATE_BYTES + 1))
        before = planning_file().read_bytes()
        self.assertFalse(planning.summary()["healthy"])
        self.assertEqual(planning_file().read_bytes(), before)

    def test_oversized_source_field_fails_visibly_instead_of_truncating(self):
        huge = self.item(text="x" * (planning.MAX_SOURCE_STRING_BYTES + 1))
        with mock.patch("bossctl.planning.work.all_items", return_value=[huge]), \
             mock.patch("bossctl.planning.supervisor.pending", return_value=[]):
            with self.assertRaises(BossError) as raised:
                planning.capture_snapshot(captured_at="2026-01-01T00:00:00Z")
        self.assertIn("larger", raised.exception.msg)

    def test_claim_expiry_reassigns_but_generating_receipt_is_never_replayed(self):
        event = self.create_event(epoch=100)
        self.assertEqual(planning.claim("first", event_id=event["id"], at=100)["claimed_by"], "first")
        self.assertIsNone(planning.claim("second", event_id=event["id"], at=150))
        reassigned = planning.claim("second", event_id=event["id"],
                                    at=100 + planning.CLAIM_SECONDS)
        self.assertEqual(reassigned["claimed_by"], "second")
        with self.assertRaises(BossError): planning.begin(event["id"], "first", at=221)
        begun = planning.begin(event["id"], "second", at=221)
        self.assertEqual(begun["state"], "generating")
        self.assertIsNone(planning.claim("third", at=1000))
        with self.assertRaises(BossError): planning.begin(event["id"], "second", at=222)

    def test_begin_atomically_refuses_wake_or_away_inserted_after_claim(self):
        event = self.create_event(epoch=100)
        planning.claim("pi", event_id=event["id"], at=100)
        self.put_wake()
        with self.assertRaises(BossError) as wake_blocked:
            planning.begin(event["id"], "pi", at=101)
        self.assertIn("wake has priority", wake_blocked.exception.msg)
        self.assertEqual(planning.show(event["id"])["state"], "claimed")
        write_json(self.home / "wakes.json", {"version": 1, "next_id": 2, "events": []})
        write_json(self.home / "supervisor.json",
                   {"version": 1, "observations": {}, "away": {"enabled": True}})
        with self.assertRaises(BossError) as away_blocked:
            planning.begin(event["id"], "pi", at=102)
        self.assertIn("away mode", away_blocked.exception.msg)
        self.assertEqual(planning.show(event["id"])["state"], "claimed")
        write_json(self.home / "supervisor.json",
                   {"version": 1, "observations": {}, "away": {"enabled": False}})
        self.assertEqual(planning.begin(event["id"], "pi", at=103)["state"], "generating")

    def test_disable_cancels_safe_receipts_but_preserves_uncertain_generating_receipt(self):
        planning.enable(at=0)
        event = planning.tick(force=True, at=1)["event"]
        planning.claim("pi", event_id=event["id"], at=1)
        disabled = planning.disable(at=2)
        self.assertIn(event["id"], disabled["cancelled"])
        self.assertEqual(planning.show(event["id"])["state"], "cancelled")
        with self.assertRaises(BossError): planning.begin(event["id"], "pi", at=2)
        retried = planning.tick(force=True, at=3)["event"]
        planning.claim("pi", event_id=retried["id"], at=3)
        planning.begin(retried["id"], "pi", at=3)
        disabled = planning.disable(at=4)
        self.assertIn(retried["id"], disabled["generating"])
        self.assertEqual(planning.show(retried["id"])["state"], "generating")

    def test_nonterminal_full_ledger_refuses_to_drop_or_append_receipts(self):
        item = self.item(); path = self.put_item(item)
        for index in range(planning.MAX_EVENTS):
            item["text"] = f"meaningful version {index}"; write_json(path, item)
            self.assertTrue(planning.tick(force=True, at=index)["created"])
        before = planning_file().read_bytes()
        item["text"] = "one too many"; write_json(path, item)
        with self.assertRaises(BossError) as raised:
            planning.tick(force=True, at=99)
        self.assertIn("nonterminal receipts", raised.exception.msg)
        self.assertEqual(planning_file().read_bytes(), before)

    def test_completion_requires_strict_provider_model_digest_and_usage_receipt(self):
        event = self.generating_event()
        bad_values = [
            {"provider": "", "model": "gpt", "response_sha256": "a" * 64, "usage": USAGE},
            {"provider": "openai", "model": "gpt", "response_sha256": "bad", "usage": USAGE},
            {"provider": "openai", "model": "gpt", "response_sha256": "a" * 64,
             "usage": {**USAGE, "input_tokens": -1}},
            {"provider": "openai", "model": "gpt", "response_sha256": "a" * 64,
             "usage": {**USAGE, "surprise": 1}},
        ]
        for value in bad_values:
            with self.subTest(value=value), self.assertRaises(BossError):
                planning.complete(event["id"], "pi", **value, at=200)
            self.assertEqual(planning.show(event["id"])["state"], "generating")
        delivered = self.complete(event["id"])
        self.assertEqual(delivered["state"], "delivered")
        self.assertEqual(delivered["delivery_receipt"]["provider"], "openai-codex")
        self.assertEqual(delivered["delivery_receipt"]["response_sha256"], "a" * 64)
        self.assertEqual(delivered["delivery_receipt"]["usage"], USAGE)
        self.assertEqual(delivered["claimed_by"], "pi")  # retained audit attribution is intentional
        self.assertNotIn("claim_until", delivered)

    def test_explicit_delivered_reconciliation_has_distinct_strict_operator_receipt(self):
        event = self.generating_event()
        reconciled = planning.reconcile(event["id"], "delivered", confirm=True,
                                        reason="operator confirmed transcript entry", at=250)
        self.assertEqual(reconciled["state"], "delivered")
        self.assertNotIn("delivery_receipt", reconciled)
        self.assertEqual(reconciled["reconciliation_receipt"], {
            "confirmed_outcome": "delivered", "reason": "operator confirmed transcript entry",
            "confirmed_at": "1970-01-01T00:04:10Z"})
        self.assertIsNone(planning.validate_state(json.loads(planning_file().read_text())))
        report = doctor.audit(network=False)
        check = next(value for value in report["checks"]
                     if value["id"] == f"planning-reconciliation:{event['id']}")
        self.assertEqual(check["status"], "ok"); self.assertEqual(check["outcome"], "delivered")

    def test_rejection_and_defer_retry_same_fingerprint_after_bounded_due_time(self):
        planning.enable(at=100)
        event = planning.tick(force=True, at=100)["event"]
        planning.claim("pi", event_id=event["id"], at=100); planning.begin(event["id"], "pi", at=100)
        planning.reject(event["id"], "pi", "model unavailable", at=101)
        self.assertEqual(planning.tick(at=400)["reason"], "not-due")
        retry = planning.tick(at=401)
        self.assertTrue(retry["created"]); self.assertEqual(retry["event"]["fingerprint"], event["fingerprint"])
        planning.defer(retry["event"]["id"], None, "wait", at=402)
        state = json.loads(planning_file().read_text()); due = planning._epoch(state["next_due"])
        self.assertEqual(planning.tick(at=due - 1)["reason"], "not-due")
        self.assertTrue(planning.tick(at=due)["created"])

    def test_doctor_reports_malformed_state_repairs_only_when_confirmed(self):
        planning_file().write_text("{broken")
        report = doctor.audit(network=False)
        check = next(value for value in report["checks"] if value["id"] == "state:planning")
        self.assertEqual(check["status"], "error")
        self.assertTrue(any(value["action"] == "quarantine-state" and
                            value["target"].endswith("planning.json") for value in report["repairs"]))
        before = planning_file().read_bytes()
        with self.assertRaises(BossError): doctor.repair(confirm=False, network=False)
        self.assertEqual(planning_file().read_bytes(), before)
        doctor.repair(confirm=True, network=False)
        self.assertIsNone(planning.validate_state(json.loads(planning_file().read_text())))
        backups = list(self.home.glob("planning.json.quarantine-*"))
        self.assertEqual(len(backups), 1); self.assertEqual(backups[0].read_bytes(), before)

    def test_doctor_exposes_uncertain_generating_receipt_without_mutating_it(self):
        event = self.generating_event()
        before = planning_file().read_bytes()
        report = doctor.audit(network=False)
        check = next(value for value in report["checks"]
                     if value["id"] == f"planning-receipt:{event['id']}")
        self.assertEqual(check["status"], "unknown")
        self.assertEqual(check["consumer"], "pi")
        self.assertEqual(planning_file().read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
