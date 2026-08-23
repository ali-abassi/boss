"""Adversarial tests for the zero-token supervisor and durable wake queue."""
try:
    import _gitenv  # noqa: F401
except ImportError:
    from tests import _gitenv  # noqa: F401
import datetime as dt
import json
import multiprocessing
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helm import supervisor, processes
from helm.util import HelmError


def iso(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def observe_in_child(home: str, item: dict, epoch: float) -> None:
    os.environ["HELM_HOME"] = home
    supervisor.observe(item, at=epoch, probe_agent=False)


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        os.environ["HELM_HOME"] = str(self.home)
        self.epoch = 2_000_000_000.0
        for name in ("HELM_SUPERVISOR_STALE_SECONDS", "HELM_SUPERVISOR_QUEUE_STALE_SECONDS",
                     "HELM_SUPERVISOR_WEDGE_OBSERVATIONS", "HELM_SUPERVISOR_WEDGE_RESURFACE_SECONDS"):
            os.environ.pop(name, None)

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def item(self, status="running", age=1, **extra):
        item = {
            "id": "p-item", "project": "p", "status": status, "phase": status,
            "created": iso(self.epoch - age), "updated": iso(self.epoch - age),
            "activity": {"last": iso(self.epoch - age), "state": status},
            "lease": {"pid": os.getpid(), "owner": "test", "started": iso(self.epoch - age),
                      "process_identity": processes.capture(os.getpid(), "test")},
            "session": None, "attempts": 0, "head_sha": None, "ask": None, "pr_url": None,
        }
        item.update(extra)
        return item

    def test_healthy_steady_state_emits_no_wake(self):
        item = self.item()
        first = supervisor.observe(item, at=self.epoch, probe_agent=False)
        second = supervisor.observe(item, at=self.epoch + 1, probe_agent=False)
        self.assertEqual(first["classification"], "healthy")
        self.assertEqual(second["classification"], "healthy")
        self.assertEqual(supervisor.pending(), [])

    def test_wakes_deduplicate_across_restart_claim_release_and_ack(self):
        item = self.item(status="ready")
        supervisor.observe(item, at=self.epoch, probe_agent=False)
        supervisor.observe(item, at=self.epoch + 1, probe_agent=False)
        self.assertEqual(len(supervisor.pending()), 1)
        claimed = supervisor.claim("mate-a", at=self.epoch + 2)
        self.assertEqual(len(claimed), 1)
        self.assertEqual(supervisor.claim("mate-b", at=self.epoch + 3), [])
        self.assertEqual(supervisor.release([claimed[0]["id"]], "mate-a"), 1)
        claimed = supervisor.claim("mate-b", at=self.epoch + 4)
        self.assertEqual(supervisor.acknowledge([claimed[0]["id"]], "mate-b"), 1)
        self.assertEqual(supervisor.pending(), [])
        # An acknowledged key is still evidence: restart/re-observe must not replay it.
        supervisor.observe(item, at=self.epoch + 5, probe_agent=False)
        self.assertEqual(supervisor.pending(), [])

    def test_active_wake_lease_is_not_redelivered_to_same_consumer(self):
        item = self.item(status="ready")
        supervisor.observe(item, at=self.epoch, probe_agent=False)
        first = supervisor.claim("mate-a", at=self.epoch + 1)
        self.assertEqual(len(first), 1)
        self.assertEqual(supervisor.claim("mate-a", at=self.epoch + 2), [])
        self.assertEqual(supervisor.claim("mate-b", at=self.epoch + 2), [])
        expired = supervisor.claim("mate-a", at=self.epoch + supervisor.CLAIM_SECONDS + 2)
        self.assertEqual([event["id"] for event in expired], [first[0]["id"]])

    def test_wake_send_receipt_is_never_automatically_replayed_and_can_be_renewed(self):
        supervisor.observe(self.item(status="ready"), at=self.epoch, probe_agent=False)
        claimed = supervisor.claim("pi", at=self.epoch + 1)
        ids = [claimed[0]["id"]]
        self.assertEqual(supervisor.mark_sending(ids, "pi"), 1)
        self.assertEqual(supervisor.mark_sent(ids, "pi"), 1)
        self.assertEqual(supervisor.renew(ids, "pi", at=self.epoch + 50), 1)
        self.assertEqual(supervisor.claim("pi", at=self.epoch + 500), [],
                         "a send that may have reached Pi is never replayed automatically")
        self.assertEqual(supervisor.acknowledge(ids, "pi"), 1)
        self.assertEqual(supervisor.pending(), [])

    def test_concurrent_observers_create_exactly_one_wake(self):
        item = self.item(status="failed")
        processes = [multiprocessing.Process(target=observe_in_child,
                                             args=(str(self.home), item, self.epoch)) for _ in range(8)]
        for process in processes: process.start()
        for process in processes:
            process.join(10)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(len(supervisor.pending()), 1)

    def test_declared_wait_resurfaces_without_changing_item_state(self):
        item = self.item(status="queued", declared_wait={"until": iso(self.epoch + 10), "reason": "CI"})
        before = supervisor.observe(item, at=self.epoch, probe_agent=False)
        after = supervisor.observe(item, at=self.epoch + 11, probe_agent=False)
        self.assertEqual(before["classification"], "waiting")
        self.assertEqual(after["classification"], "needs-you")
        self.assertTrue(after["declared_wait_elapsed"])
        self.assertEqual(item["status"], "queued")

    def test_unchanged_stale_evidence_escalates_and_resurfaces_as_wedged(self):
        os.environ.update(HELM_SUPERVISOR_STALE_SECONDS="5", HELM_SUPERVISOR_WEDGE_OBSERVATIONS="3",
                          HELM_SUPERVISOR_WEDGE_RESURFACE_SECONDS="5")
        item = self.item(age=20)
        states = [supervisor.observe(item, at=self.epoch + n, probe_agent=False)["classification"] for n in range(3)]
        self.assertEqual(states, ["stale", "stale", "wedged"])
        self.assertEqual(len(supervisor.pending()), 2, "one stale wake and one wedge escalation")
        later = supervisor.observe(item, at=self.epoch + 8, probe_agent=False)
        self.assertEqual(later["classification"], "wedged")
        self.assertEqual(len(supervisor.pending()), 3, "unchanged wedge resurfaces in a bounded new bucket")

    def test_unknown_herdr_liveness_is_never_relabelled_dead(self):
        item = self.item(session={"agent_name": "impl-p", "agent_session_id": "real"})
        unknown = supervisor.classify(item, at=self.epoch, agent={"state": "unknown", "reason": "outage"})
        dead = supervisor.classify(item, at=self.epoch, agent={"state": "dead"})
        self.assertEqual(unknown["classification"], "unknown")
        self.assertEqual(dead["classification"], "dead")
        item["lease"]["pid"] = 999_999_999
        item["lease"]["process_identity"] = None
        conflicting = supervisor.classify(item, at=self.epoch, agent={"state": "live"})
        self.assertEqual(conflicting["classification"], "unknown")

    def test_positive_process_death_is_preserved_at_every_execution_phase(self):
        phases = ("integrating", "implementing", "verifying", "review-correctness",
                  "review-adversarial", "final-verification", "delivering")
        for index, phase in enumerate(phases):
            item = self.item(phase=phase, id=f"p-{index}")
            item["lease"]["pid"] = 999_999_999
            item["lease"]["process_identity"] = None
            before = json.loads(json.dumps(item))
            observation = supervisor.observe(item, at=self.epoch, probe_agent=False)
            self.assertEqual(observation["classification"], "dead", phase)
            self.assertEqual(item, before, "supervision must not mutate/recover the item")
        self.assertEqual(len(supervisor.pending()), len(phases))

    def test_herdr_restart_uncertainty_surfaces_unknown_then_recovers_without_identity_rewrite(self):
        from unittest import mock
        session = {"agent_name": "impl-p", "agent_session_id": "durable-real-id", "pane_id": "w1:p1"}
        item = self.item(session=session)
        live = {"state": "live", "agent": {"pane_id": "w1:p1"}}
        with mock.patch("helm.herdr.agent_liveness", return_value=live), \
             mock.patch("helm.herdr.exact_agent_liveness", side_effect=lambda _identity, evidence: evidence):
            self.assertEqual(supervisor.observe(item, at=self.epoch)["classification"], "healthy")
        with mock.patch("helm.herdr.agent_liveness", return_value={"state": "unknown", "reason": "Herdr restarting"}):
            self.assertEqual(supervisor.observe(item, at=self.epoch + 1)["classification"], "unknown")
        self.assertEqual(supervisor.pending()[0]["classification"], "unknown")
        with mock.patch("helm.herdr.agent_liveness", return_value=live), \
             mock.patch("helm.herdr.exact_agent_liveness", side_effect=lambda _identity, evidence: evidence):
            self.assertEqual(supervisor.observe(item, at=self.epoch + 2)["classification"], "healthy")
        self.assertEqual(item["session"]["agent_session_id"], "durable-real-id")

    def test_legacy_or_reused_live_pid_is_unknown_never_healthy(self):
        legacy = self.item()
        legacy["lease"].pop("process_identity", None)
        self.assertEqual(supervisor.classify(legacy, at=self.epoch)["classification"], "unknown")
        reused = self.item()
        reused["lease"]["process_identity"] = {
            "version": 1, "kind": "firstmate-worker", "pid": os.getpid(),
            "pgid": os.getpgid(os.getpid()), "owner": "old",
            "start_sha256": "0" * 64, "command_sha256": "0" * 64,
        }
        self.assertEqual(supervisor.classify(reused, at=self.epoch)["classification"], "unknown")

    def test_unfinalized_launch_is_supervised_and_never_mistaken_for_no_agent(self):
        from unittest import mock
        launch = {"launch_id": "launch-1", "agent_session_id": "launch-1", "role": "implementer",
                  "state": "tab-created", "agent_name": "impl-p", "pane_id": "w1:p7", "workspace_id": "w1"}
        item = self.item(agent_launches=[launch])
        with mock.patch("helm.herdr.agent_liveness", return_value={"state": "unknown", "reason": "Herdr restarting"}):
            self.assertEqual(supervisor.observe(item, at=self.epoch)["classification"], "unknown")
        with mock.patch("helm.herdr.agent_liveness", return_value={
                "state": "live", "agent": {"pane_id": "w1:other", "workspace_id": "w1"}}):
            self.assertEqual(supervisor.observe(item, at=self.epoch + 1)["classification"], "unknown")
        with mock.patch("helm.herdr.agent_liveness", return_value={
                "state": "live", "agent": {"pane_id": "w1:p7", "workspace_id": "w1"}}):
            observation = supervisor.observe(item, at=self.epoch + 2)
            self.assertEqual(observation["classification"], "unknown")
            self.assertIn("model/thinking evidence is missing", observation["reason"])

    def test_unknown_heartbeats_do_not_create_duplicate_wakes(self):
        from unittest import mock
        item = self.item(session={"agent_name": "impl-p", "agent_session_id": "real", "pane_id": "w1:p1"})
        with mock.patch("helm.herdr.agent_liveness", return_value={"state": "unknown", "reason": "transport"}):
            supervisor.observe(item, at=self.epoch)
            item["activity"]["last"] = iso(self.epoch + 1)
            supervisor.observe(item, at=self.epoch + 1)
        self.assertEqual(len(supervisor.pending()), 1)

    def test_corrupted_queue_fails_closed_and_is_not_overwritten(self):
        path = self.home / "wakes.json"
        path.write_text("{broken")
        with self.assertRaises(HelmError):
            supervisor.observe(self.item(status="ready"), at=self.epoch, probe_agent=False)
        self.assertEqual(path.read_text(), "{broken")

    def test_nested_corrupt_queue_fails_closed_instead_of_crashing_or_rewriting(self):
        path = self.home / "wakes.json"
        path.write_text('{"version":1,"next_id":2,"events":[null]}\n')
        with self.assertRaises(HelmError):
            supervisor.pending()
        self.assertEqual(path.read_text(), '{"version":1,"next_id":2,"events":[null]}\n')

    def test_colliding_next_id_is_rejected_before_an_enqueue_can_wedge_the_queue(self):
        path = self.home / "wakes.json"
        valid_event = {
            "id": "7", "key": "p-item:needs-you:x", "item_id": "p-item",
            "classification": "needs-you", "reason": "decision", "created": iso(self.epoch),
            "acknowledged": False,
        }
        path.write_text(json.dumps({"version": 1, "next_id": 7, "events": [valid_event]}) + "\n")
        before = path.read_bytes()
        with self.assertRaises(HelmError) as caught:
            supervisor.pending()
        self.assertIn("next_id", caught.exception.msg)
        self.assertEqual(path.read_bytes(), before)

    def test_terminal_observations_do_not_exhaust_long_running_state(self):
        with mock.patch("helm.supervisor.MAX_RECORDS", 2):
            for index in range(5):
                supervisor.observe(self.item(status="done", id=f"p-done-{index}"),
                                   at=self.epoch + index, probe_agent=False)
        state = json.loads((self.home / "supervisor.json").read_text())
        self.assertEqual(state["observations"], {})
        self.assertEqual(supervisor.pending(), [])

    def test_capacity_failure_preserves_the_last_valid_ledgers(self):
        with mock.patch("helm.supervisor.MAX_RECORDS", 1):
            supervisor.observe(self.item(status="ready", id="p-first"), at=self.epoch, probe_agent=False)
            state_path, queue_path = self.home / "supervisor.json", self.home / "wakes.json"
            before = (state_path.read_bytes(), queue_path.read_bytes())
            with self.assertRaises(HelmError):
                supervisor.observe(self.item(status="failed", id="p-second"),
                                   at=self.epoch + 1, probe_agent=False)
            self.assertEqual((state_path.read_bytes(), queue_path.read_bytes()), before)


if __name__ == "__main__":
    unittest.main()
