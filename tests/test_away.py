"""Away mode is gated, durable, and cannot hide or consume decisions."""
try:
    import _gitenv  # noqa: F401
except ImportError:
    from tests import _gitenv  # noqa: F401
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bossctl import supervisor
from bossctl.util import BossError, write_json


class AwayTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp()); os.environ["BOSS_HOME"] = str(self.home)
        write_json(self.home / "supervisor.json", {"version": 1, "observations": {}, "away": {"enabled": False}})
        write_json(self.home / "wakes.json", {"version": 1, "next_id": 1, "events": []})
        self.good_report = {"healthy": True, "summary": {"ok": 10, "errors": 0, "warnings_or_unknown": 1},
                            "checks": [{"id": "worker:123", "status": "ok", "summary": "live"}]}

    def tearDown(self): shutil.rmtree(self.home, ignore_errors=True)

    def enable(self):
        with mock.patch("bossctl.supervisor.scan", return_value=[]), mock.patch("bossctl.doctor.audit", return_value=self.good_report):
            return supervisor.set_away(True)

    def test_enable_requires_clean_supervision_doctor_and_live_worker(self):
        with mock.patch("bossctl.supervisor.scan", return_value=[{"item_id": "x", "classification": "unknown"}]), \
             mock.patch("bossctl.doctor.audit", return_value=self.good_report):
            with self.assertRaises(BossError): supervisor.set_away(True)
        bad = {**self.good_report, "healthy": False, "checks": [{"id": "state:wakes", "status": "error"}]}
        with mock.patch("bossctl.supervisor.scan", return_value=[]), mock.patch("bossctl.doctor.audit", return_value=bad):
            with self.assertRaises(BossError): supervisor.set_away(True)
        no_worker = {**self.good_report, "checks": []}
        with mock.patch("bossctl.supervisor.scan", return_value=[]), mock.patch("bossctl.doctor.audit", return_value=no_worker):
            with self.assertRaises(BossError): supervisor.set_away(True)
        self.assertFalse(supervisor.away_status()["enabled"])

    def test_decisions_arriving_away_remain_visible_unclaimed_and_flush_on_return(self):
        self.assertTrue(self.enable()["enabled"])
        ready = {"id": "p-ready", "project": "p", "status": "ready", "phase": "merge-ready",
                 "created": "2026-01-01T00:00:00Z", "updated": "2026-01-01T00:00:00Z",
                 "activity": {"last": "2026-01-01T00:00:00Z"}, "session": None, "attempts": 1,
                 "head_sha": "a" * 40, "ask": None, "pr_url": None}
        supervisor.observe(ready, probe_agent=False)
        self.assertEqual(len(supervisor.pending()), 1)
        self.assertEqual(supervisor.claim("pi"), [])
        self.assertEqual(supervisor.away_status()["pending_wakes"], 1)
        self.assertFalse(supervisor.set_away(False)["enabled"])
        self.assertEqual(len(supervisor.claim("pi")), 1)

    def test_pending_decision_blocks_initial_entry(self):
        ready = {"id": "p-ready", "project": "p", "status": "ready", "phase": "merge-ready",
                 "created": "2026-01-01T00:00:00Z", "updated": "2026-01-01T00:00:00Z",
                 "activity": {"last": "2026-01-01T00:00:00Z"}, "session": None, "attempts": 1,
                 "head_sha": "a" * 40, "ask": None, "pr_url": None}
        supervisor.observe(ready, probe_agent=False)
        with mock.patch("bossctl.supervisor.scan", return_value=[]), mock.patch("bossctl.doctor.audit", return_value=self.good_report):
            with self.assertRaises(BossError): supervisor.set_away(True)

    def test_open_external_transaction_blocks_away_even_for_terminal_item(self):
        directory = self.home / "work" / "p-merged"; directory.mkdir(parents=True)
        write_json(directory / "item.json", {
            "id": "p-merged", "project": "p", "status": "merged", "phase": "merged", "revision": 0,
            "created": "2026-01-01T00:00:00Z", "updated": "2026-01-01T00:00:00Z",
            "branch": "boss/p-merged", "worktree": str(self.home / "worktrees" / "p" / "p-merged"),
            "external_gate": {"provider": "no-mistakes", "id": "tx-1", "state": "unknown"},
            "controls": {"events": [], "pending": []},
            "agent_launches": [], "history": [], "runs": [], "reviews": [], "verification": []
        })
        with mock.patch("bossctl.supervisor.scan", return_value=[]), mock.patch("bossctl.doctor.audit", return_value=self.good_report):
            with self.assertRaises(BossError): supervisor.set_away(True)


if __name__ == "__main__": unittest.main()
