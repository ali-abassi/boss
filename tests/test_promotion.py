"""Promotion is serialized, exact-SHA-bound, crash-reconcilable, and preserves late work."""
try:
    import _gitenv  # noqa: F401
except ImportError:
    from tests import _gitenv  # noqa: F401

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bossctl import control, deliver, work, worktree
from bossctl.util import BossError, sh as real_sh, write_json


class PromotionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()); self.home = self.root / "home"
        os.environ["BOSS_HOME"] = str(self.home)
        self.repo = self.root / "repo"; self.repo.mkdir()
        self.git("init", "-q", "-b", "main"); self.git("config", "user.email", "t@t"); self.git("config", "user.name", "t")
        (self.repo / "base.txt").write_text("base\n"); self.git("add", "-A"); self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD")
        self.project = {"id": "p", "path": str(self.repo), "mode": "local-only", "authority": 3,
                        "base": "main", "test_cmd": "true", "protected_paths": [], "gate": "native"}
        write_json(self.home / "projects.json", {"projects": {"p": self.project}})
        write_json(self.home / "supervisor.json", {"version": 1, "observations": {}, "away": {"enabled": False}})
        write_json(self.home / "wakes.json", {"version": 1, "next_id": 1, "events": []})
        self.work_id = "p-item"
        self.wt = worktree.create(self.project, self.work_id)
        (self.wt / "change.txt").write_text("reviewed\n")
        self.wgit("add", "-A"); self.wgit("commit", "-qm", "reviewed")
        self.head = self.wgit("rev-parse", "HEAD")
        self.item = self.make_item()
        directory = self.home / "work" / self.work_id; directory.mkdir(parents=True, exist_ok=True)
        write_json(directory / "item.json", self.item)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              text=True, capture_output=True).stdout.strip()

    def wgit(self, *args):
        return subprocess.run(["git", "-C", str(self.wt), *args], check=True,
                              text=True, capture_output=True).stdout.strip()

    def make_item(self, graph="local-only", status="ready"):
        reviews = []
        if graph == "direct-pr":
            reviews = [{"role": "correctness", "verdict": "accept", "sha": self.head,
                        "base_sha": self.base, "fresh": True, "valid": True,
                        "reviewer": {"kind": "test", "identity": "independent"}}]
        return {"id": self.work_id, "project": "p", "status": status, "phase": "merge-ready",
                "created": "2026-01-01T00:00:00Z", "updated": "2026-01-01T00:00:00Z",
                "revision": 0, "branch": f"boss/{self.work_id}", "worktree": str(self.wt),
                "head_sha": self.head, "pr_url": None, "session": None, "agent_launches": [],
                "dispatch": {"graph": graph}, "reviews": reviews, "controls": {"events": [], "pending": []},
                "verification": [{"ok": True, "complete": True, "base_sha": self.base,
                                  "head_sha": self.head, "fingerprint": {"sha": self.head, "dirty": []}}],
                "history": [], "attempts": 1}

    def test_initial_promotion_refuses_a_missing_worktree(self):
        self.git("worktree", "remove", "--force", str(self.wt))
        with self.assertRaises(BossError):
            deliver.promote(work.load(self.work_id), self.project, True)
        self.assertNotIn("promotion", work.load(self.work_id))
        self.assertEqual(self.git("rev-parse", "main"), self.base)

    def test_local_merge_crash_reconciles_from_exact_base_head(self):
        with mock.patch("bossctl.deliver._mark_merged", side_effect=RuntimeError("crash after git merge")):
            with self.assertRaises(RuntimeError):
                deliver.promote(work.load(self.work_id), self.project, True)
        self.assertEqual(self.git("rev-parse", "main"), self.head)
        midway = work.load(self.work_id)
        self.assertEqual(midway["promotion"]["state"], "external-requested")
        result = deliver.promote(midway, self.project, True)
        self.assertEqual(result["state"], "merged")
        self.assertEqual(work.load(self.work_id)["promotion"]["state"], "complete")

    def test_cleanup_crash_after_clean_worktree_removal_replays_without_ref_loss(self):
        original = worktree.remove
        crashed = [False]
        def remove_then_die(*args, **kwargs):
            result = original(*args, **kwargs)
            if not crashed[0]:
                crashed[0] = True
                raise RuntimeError("controller died after clean worktree removal")
            return result
        with mock.patch("bossctl.worktree.remove", side_effect=remove_then_die):
            with self.assertRaises(RuntimeError):
                deliver.promote(work.load(self.work_id), self.project, True)
        midway = work.load(self.work_id)
        self.assertEqual(midway["status"], "merged")
        self.assertEqual(midway["promotion"]["cleanup_phase"], "worktree-removal-requested")
        self.assertFalse(self.wt.exists())
        result = deliver.promote(midway, self.project, True)
        self.assertEqual(result["state"], "merged")
        stored = work.load(self.work_id)
        self.assertEqual(stored["promotion"]["state"], "complete")
        self.assertTrue(stored["promotion"]["branch_retained"])
        self.assertEqual(self.git("rev-parse", f"boss/{self.work_id}"), self.head)

    def test_gate_change_after_arm_never_reaches_local_merge(self):
        original = deliver._mark_requested
        def switch_gate(item):
            requested = original(item)
            changed = {**self.project, "gate": "no-mistakes"}
            write_json(self.home / "projects.json", {"projects": {"p": changed}})
            return requested
        with mock.patch("bossctl.deliver._mark_requested", side_effect=switch_gate):
            with self.assertRaises(BossError):
                deliver.promote(work.load(self.work_id), self.project, True)
        self.assertEqual(self.git("rev-parse", "main"), self.base)
        self.assertEqual(work.load(self.work_id)["promotion"]["state"], "external-requested")

    def test_late_branch_mutation_is_never_deleted_by_cleanup(self):
        original = deliver._mark_requested
        late_sha = []
        def mutate_after_arm(item):
            armed = original(item)
            (self.wt / "late.txt").write_text("unreviewed but preserved\n")
            self.wgit("add", "-A"); self.wgit("commit", "-qm", "late unreviewed")
            late_sha.append(self.wgit("rev-parse", "HEAD"))
            return armed
        with mock.patch("bossctl.deliver._mark_requested", side_effect=mutate_after_arm):
            with self.assertRaises(BossError):
                deliver.promote(work.load(self.work_id), self.project, True)
        stored = work.load(self.work_id)
        self.assertEqual(stored["status"], "ready")
        self.assertEqual(self.git("rev-parse", "main"), self.base)
        self.assertTrue(self.wt.exists()); self.assertEqual(self.wgit("rev-parse", "HEAD"), late_sha[0])
        self.assertEqual(stored["promotion"]["state"], "external-requested")

    def test_successful_gh_command_without_merged_observation_stays_pr_open(self):
        item = self.make_item(graph="direct-pr", status="pr-open")
        item["pr_url"] = "https://github.com/acme/widget/pull/7"
        write_json(self.home / "work" / self.work_id / "item.json", item)
        green = {"classification": "checks-green", "evidence_complete": True,
                 "expected": {"head_sha": self.head, "base_sha": self.base}, "reason": "green",
                 "fingerprint": "green", "checks": {"merge_policy": {
                     "strict": True, "required_checks": ["build"], "base_ref": "main"}}}
        def command(args, **kwargs):
            if args[:3] == ["gh", "pr", "merge"]:
                return subprocess.CompletedProcess(args, 0, "queued", "")
            return real_sh(args, **kwargs)
        with mock.patch("bossctl.forge.require_green", return_value=green), \
             mock.patch("bossctl.forge.monitor_item", return_value=green), \
             mock.patch("bossctl.deliver.sh", side_effect=command):
            result = deliver.promote(work.load(self.work_id), self.project, True)
        stored = work.load(self.work_id)
        self.assertEqual(result["state"], "merge-requested")
        self.assertEqual(stored["status"], "pr-open")
        self.assertEqual(stored["promotion"]["state"], "external-requested")

    def test_away_and_armed_transaction_block_merge_and_steering(self):
        write_json(self.home / "supervisor.json", {"version": 1, "observations": {}, "away": {"enabled": True}})
        with self.assertRaises(BossError): deliver.promote(work.load(self.work_id), self.project, True)
        write_json(self.home / "supervisor.json", {"version": 1, "observations": {}, "away": {"enabled": False}})
        control.cas_update(self.work_id, lambda item: item.update(promotion={"state": "external-requested"}))
        with self.assertRaises(BossError): control.request(self.work_id, "steer", "change it")
        self.assertEqual(self.git("rev-parse", "main"), self.base)


if __name__ == "__main__":
    unittest.main()
