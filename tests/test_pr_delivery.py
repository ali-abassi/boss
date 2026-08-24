"""PR delivery is exact-bound, journaled before side effects, and restart-safe."""
try:
    import _gitenv  # noqa: F401
except ImportError:
    from tests import _gitenv  # noqa: F401

import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from bossctl import control, deliver, registry, work, worktree
from bossctl.util import write_json


class PRDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()); self.home = self.root / "home"
        os.environ["BOSS_HOME"] = str(self.home)
        self.repo = self.root / "repo"; self.repo.mkdir()
        self.git("init", "-q", "-b", "main"); self.git("config", "user.email", "t@t")
        self.git("config", "user.name", "t")
        (self.repo / "base.txt").write_text("base\n"); self.git("add", "-A"); self.git("commit", "-qm", "base")
        self.git("remote", "add", "origin", "git@github.com:acme/widget.git")
        self.base = self.git("rev-parse", "HEAD")
        self.project = {"id": "p", "path": str(self.repo), "mode": "direct-pr", "authority": 2,
                        "base": "main", "test_cmd": "true", "protected_paths": [], "gate": "native"}
        write_json(self.home / "projects.json", {"projects": {"p": self.project}})
        write_json(self.home / "supervisor.json", {"version": 1, "observations": {},
                                                    "away": {"enabled": False}})
        write_json(self.home / "wakes.json", {"version": 1, "next_id": 1, "events": []})
        self.work_id = "p-delivery"
        self.wt = worktree.create(self.project, self.work_id)
        (self.wt / "change.txt").write_text("reviewed\n")
        self.wgit("add", "-A"); self.wgit("commit", "-qm", "reviewed")
        self.head = self.wgit("rev-parse", "HEAD")
        directory = self.home / "work" / self.work_id; directory.mkdir(parents=True)
        write_json(directory / "item.json", self.item())

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              text=True, capture_output=True).stdout.strip()

    def wgit(self, *args):
        return subprocess.run(["git", "-C", str(self.wt), *args], check=True,
                              text=True, capture_output=True).stdout.strip()

    def item(self):
        review = {"role": "correctness", "verdict": "accept", "sha": self.head,
                  "base_sha": self.base, "fresh": True, "valid": True,
                  "reviewer": {"kind": "test", "identity": "independent"}}
        return {"id": self.work_id, "project": "p", "status": "running", "phase": "delivering",
                "created": "2026-01-01T00:00:00Z", "updated": "2026-01-01T00:00:00Z",
                "revision": 0, "branch": f"boss/{self.work_id}", "worktree": str(self.wt),
                "head_sha": self.head, "pr_url": None, "session": None, "agent_launches": [],
                "text": "Ship the reviewed change", "dispatch": {"graph": "direct-pr", "rule": "test"},
                "reviews": [review], "controls": {"events": [], "pending": []}, "history": [],
                "verification": [{"ok": True, "complete": True, "base_sha": self.base,
                                  "head_sha": self.head, "fingerprint": {"sha": self.head, "dirty": []}}],
                "attempts": 1, "budgets": {}, "usage": {"seconds": 0.0}}

    def fake_external(self, *, crash_after_push=False, crash_after_create=False):
        state = {"remote": None, "prs": [], "pushes": 0, "creates": 0,
                 "crash_after_push": crash_after_push, "crash_after_create": crash_after_create}

        def command(args, cwd=None, check=True, **_kwargs):
            if args[:4] == ["git", "-C", str(self.repo), "ls-remote"]:
                stdout = (f"{state['remote']}\trefs/heads/boss/{self.work_id}\n"
                          if state["remote"] else "")
                return subprocess.CompletedProcess(args, 0, stdout, "")
            if len(args) > 4 and args[:3] == ["git", "-C", str(self.wt)] and "push" in args:
                state["pushes"] += 1; state["remote"] = self.head
                if state["crash_after_push"]:
                    state["crash_after_push"] = False
                    raise RuntimeError("controller died after remote accepted push")
                return subprocess.CompletedProcess(args, 0, "ok", "")
            if args[:3] == ["gh", "pr", "list"]:
                return subprocess.CompletedProcess(args, 0, json.dumps(state["prs"]), "")
            if args[:3] == ["gh", "pr", "create"]:
                state["creates"] += 1
                state["prs"] = [{"url": "https://github.com/acme/widget/pull/7", "number": 7,
                                  "state": "OPEN", "headRefName": f"boss/{self.work_id}",
                                  "headRefOid": self.head, "baseRefName": "main"}]
                if state["crash_after_create"]:
                    state["crash_after_create"] = False
                    raise RuntimeError("controller died after GitHub accepted PR")
                return subprocess.CompletedProcess(args, 0, "untrusted stdout", "")
            raise AssertionError(f"unexpected external command: {args}")
        return state, command

    def test_exact_delivery_uses_non_force_push_and_authoritative_pr_receipt(self):
        state, command = self.fake_external()
        with mock.patch("bossctl.deliver.sh", side_effect=command):
            completed = deliver.resume_pr_delivery(work.load(self.work_id), self.project, self.wt)
        self.assertEqual(completed["status"], "pr-open")
        self.assertEqual(completed["pr_url"], "https://github.com/acme/widget/pull/7")
        self.assertEqual(completed["pr_delivery"]["state"], "complete")
        self.assertEqual((state["pushes"], state["creates"]), (1, 1))

    def test_crash_after_push_replays_without_a_second_push(self):
        state, command = self.fake_external(crash_after_push=True)
        with mock.patch("bossctl.deliver.sh", side_effect=command):
            with self.assertRaises(RuntimeError):
                deliver.open_pr(work.load(self.work_id), self.project, self.wt)
            self.assertEqual(work.load(self.work_id)["pr_delivery"]["state"], "push-requested")
            completed = deliver.resume_pr_delivery(work.load(self.work_id), self.project, self.wt)
        self.assertEqual(completed["status"], "pr-open")
        self.assertEqual((state["pushes"], state["creates"]), (1, 1))

    def test_crash_after_pr_create_reconciles_without_a_second_create_or_model_turn(self):
        state, command = self.fake_external(crash_after_create=True)
        with mock.patch("bossctl.deliver.sh", side_effect=command):
            with self.assertRaises(RuntimeError):
                deliver.open_pr(work.load(self.work_id), self.project, self.wt)
        midway = work.load(self.work_id)
        self.assertEqual(midway["pr_delivery"]["state"], "pr-create-requested")
        control.cas_update(self.work_id, lambda item: item.update(status="failed", phase="failed"))
        work.retry(self.work_id)
        leased = work.claim_next("recovery-worker")
        with mock.patch("bossctl.deliver.sh", side_effect=command), \
             mock.patch("bossctl.work._execute", side_effect=AssertionError("must not start a model turn")):
            completed = work.execute(leased)
        self.assertEqual(completed["status"], "pr-open")
        self.assertEqual((state["pushes"], state["creates"]), (1, 1))

    def test_ambiguous_pr_create_is_never_reissued_and_later_observation_reconciles(self):
        state, command = self.fake_external()
        original = command
        def delayed(args, **kwargs):
            if args[:3] == ["gh", "pr", "create"]:
                state["creates"] += 1
                return subprocess.CompletedProcess(args, 0, "request accepted but not visible", "")
            return original(args, **kwargs)
        with mock.patch("bossctl.deliver.sh", side_effect=delayed):
            with self.assertRaises(SystemExit):
                deliver.open_pr(work.load(self.work_id), self.project, self.wt)
            self.assertEqual(work.load(self.work_id)["pr_delivery"]["state"], "pr-create-requested")
            with self.assertRaises(SystemExit):
                deliver.open_pr(work.load(self.work_id), self.project, self.wt)
        self.assertEqual(state["creates"], 1)
        state["prs"] = [{"url": "https://github.com/acme/widget/pull/7", "number": 7,
                          "state": "OPEN", "headRefName": f"boss/{self.work_id}",
                          "headRefOid": self.head, "baseRefName": "main"}]
        with mock.patch("bossctl.deliver.sh", side_effect=delayed):
            completed = deliver.resume_pr_delivery(work.load(self.work_id), self.project, self.wt)
        self.assertEqual(completed["status"], "pr-open")
        self.assertEqual(state["creates"], 1)

    def test_controls_and_cancellation_cannot_race_an_armed_delivery(self):
        armed = deliver._arm_pr_delivery(work.load(self.work_id), self.project, self.wt)
        self.assertEqual(armed["pr_delivery"]["state"], "armed")
        with self.assertRaises(SystemExit):
            control.request(self.work_id, "steer", "change the implementation")
        with self.assertRaises(SystemExit):
            work.cancel(self.work_id)
        stored = work.load(self.work_id)
        self.assertEqual(stored["reviews"][0]["verdict"], "accept")
        self.assertEqual(stored["pr_delivery"]["state"], "armed")

    def test_review_mutation_after_arm_blocks_push_and_create(self):
        deliver._arm_pr_delivery(work.load(self.work_id), self.project, self.wt)
        control.cas_update(self.work_id, lambda item: item.update(reviews=[]))
        state, command = self.fake_external()
        with mock.patch("bossctl.deliver.sh", side_effect=command):
            with self.assertRaises(SystemExit):
                deliver.open_pr(work.load(self.work_id), self.project, self.wt)
        self.assertEqual((state["pushes"], state["creates"]), (0, 0))
        self.assertEqual(work.load(self.work_id)["pr_delivery"]["state"], "armed")

    def test_current_registry_authority_is_reread_before_arming(self):
        registry.set_fields("p", authority=1)
        state, command = self.fake_external()
        with mock.patch("bossctl.deliver.sh", side_effect=command):
            with self.assertRaises(SystemExit):
                deliver.open_pr(work.load(self.work_id), self.project, self.wt)
        self.assertEqual((state["pushes"], state["creates"]), (0, 0))
        self.assertFalse((work.load(self.work_id).get("pr_delivery") or {}).get("state"))

    def test_registry_policy_change_cannot_interleave_with_external_delivery(self):
        state, command = self.fake_external()
        entered = threading.Event(); release = threading.Event(); changed = threading.Event()
        errors = []
        blocked_once = False

        def blocking_command(args, **kwargs):
            nonlocal blocked_once
            if args[:3] == ["gh", "pr", "list"] and not blocked_once:
                blocked_once = True; entered.set()
                if not release.wait(5):
                    raise AssertionError("test did not release delivery")
            return command(args, **kwargs)

        def delivery():
            try: deliver.open_pr(work.load(self.work_id), self.project, self.wt)
            except BaseException as exc: errors.append(exc)

        def change_policy():
            try: registry.set_fields("p", authority=1)
            except BaseException as exc: errors.append(exc)
            finally: changed.set()

        with mock.patch("bossctl.deliver.sh", side_effect=blocking_command):
            delivery_thread = threading.Thread(target=delivery)
            delivery_thread.start(); self.assertTrue(entered.wait(2))
            policy_thread = threading.Thread(target=change_policy)
            policy_thread.start()
            self.assertFalse(changed.wait(0.15), "authority changed while PR delivery held its transaction lock")
            self.assertEqual(registry.get("p")["authority"], 2)
            release.set(); delivery_thread.join(5); policy_thread.join(5)
        self.assertFalse(delivery_thread.is_alive() or policy_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual((state["pushes"], state["creates"]), (1, 1))
        self.assertEqual(registry.get("p")["authority"], 1)


if __name__ == "__main__":
    unittest.main()
