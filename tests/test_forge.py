"""Exact-SHA GitHub lifecycle evidence and race rejection."""
try:
    import _gitenv  # noqa: F401
except ImportError:
    from tests import _gitenv  # noqa: F401
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helm import control, forge
from helm.util import HelmError, write_json


class ForgeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()); self.home = self.root / "home"
        os.environ["HELM_HOME"] = str(self.home)
        self.repo = self.root / "repo"; self.repo.mkdir()
        subprocess.run(["git", "-C", str(self.repo), "init", "-q", "-b", "main"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "remote", "add", "origin", "https://github.com/acme/widget.git"], check=True)
        self.head, self.base = "a" * 40, "b" * 40
        self.project = {"id": "p", "path": str(self.repo), "base": "main"}
        self.item = {"id": "p-item", "project": "p", "status": "pr-open", "phase": "merge-ready",
                     "pr_url": "https://github.com/acme/widget/pull/7", "head_sha": self.head,
                     "verification": [{"base_sha": self.base}], "revision": 0,
                     "created": "2026-01-01T00:00:00Z", "updated": "2026-01-01T00:00:00Z",
                     "activity": {"last": "2026-01-01T00:00:00Z"}, "attempts": 1,
                     "session": None, "lease": None, "ask": None}

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def pr(self, **changes):
        value = {"state": "open", "merged": False, "draft": False,
                 "head": {"sha": self.head, "ref": "firstmate/p-item", "repo": {"full_name": "acme/widget"}},
                 "base": {"sha": self.base, "ref": "main", "repo": {"full_name": "acme/widget"}},
                 "updated_at": "2026-01-01T00:00:00Z"}
        value.update(changes); return value

    def inspect_with(self, pr=None, runs=None, statuses=None, failure=None, protection=None):
        pr = self.pr() if pr is None else pr
        runs = [{"name": "test", "status": "completed", "conclusion": "success", "head_sha": self.head}] if runs is None else runs
        statuses = [] if statuses is None else statuses
        protection = ({"strict": True, "contexts": ["test"], "checks": []}
                      if protection is None else protection)
        def api(_path, endpoint):
            if failure: return None, failure
            if "/pulls/" in endpoint: return pr, None
            if "check-runs" in endpoint: return {"total_count": len(runs), "check_runs": runs}, None
            if "required_status_checks" in endpoint: return protection, None
            combined = "failure" if any(value.get("state") in ("failure", "error") for value in statuses) \
                else "pending" if any(value.get("state") == "pending" for value in statuses) else "success"
            return {"sha": self.head, "state": combined, "total_count": len(statuses), "statuses": statuses}, None
        with mock.patch("helm.forge._api", side_effect=api):
            return forge.inspect(self.item, self.project)

    def test_check_states_are_bound_to_exact_sha(self):
        self.assertEqual(self.inspect_with()["classification"], "checks-green")
        pending = [{"name": "test", "status": "in_progress", "conclusion": None, "head_sha": self.head}]
        self.assertEqual(self.inspect_with(runs=pending)["classification"], "checks-pending")
        failed = [{"name": "test", "status": "completed", "conclusion": "failure", "head_sha": self.head}]
        self.assertEqual(self.inspect_with(runs=failed)["classification"], "checks-failed")
        wrong = [{"name": "test", "status": "completed", "conclusion": "success", "head_sha": "c" * 40}]
        observation = self.inspect_with(runs=wrong)
        self.assertEqual(observation["classification"], "unknown")
        self.assertFalse(observation["evidence_complete"])

    def test_missing_sha_and_incomplete_pages_fail_closed(self):
        missing = [{"name": "test", "status": "completed", "conclusion": "success"}]
        self.assertEqual(self.inspect_with(runs=missing)["classification"], "unknown")
        status_missing = [{"context": "legacy", "state": "success"}]
        self.assertEqual(self.inspect_with(statuses=status_missing)["classification"], "unknown")
        def api(_path, endpoint):
            if "/pulls/" in endpoint: return self.pr(), None
            if "check-runs" in endpoint: return {"total_count": 2, "check_runs": [
                {"name": "one", "status": "completed", "conclusion": "success", "head_sha": self.head}]}, None
            if "required_status_checks" in endpoint: return {"strict": True, "contexts": ["one"], "checks": []}, None
            return {"sha": self.head, "state": "success", "total_count": 0, "statuses": []}, None
        with mock.patch("helm.forge._api", side_effect=api):
            self.assertEqual(forge.inspect(self.item, self.project)["classification"], "unknown")

    def test_malformed_or_contradictory_check_schema_never_becomes_green(self):
        malformed = [
            {"name": "test", "status": "MALFORMED", "conclusion": "success", "head_sha": self.head},
            {"name": "test", "status": "completed", "conclusion": None, "head_sha": self.head},
            {"name": "test", "status": "in_progress", "conclusion": "success", "head_sha": self.head},
            {"name": "", "status": "completed", "conclusion": "success", "head_sha": self.head},
            {"name": "test", "status": "completed", "conclusion": "success", "head_sha": self.head,
             "app": {"id": "not-an-integer"}},
        ]
        for run in malformed:
            with self.subTest(run=run):
                observation = self.inspect_with(runs=[run])
                self.assertEqual(observation["classification"], "unknown")
                self.assertFalse(observation["evidence_complete"])
        for state in ("MALFORMED", "", None, 7):
            with self.subTest(status_state=state):
                observation = self.inspect_with(
                    runs=[], statuses=[{"context": "test", "state": state, "sha": self.head}],
                )
                self.assertEqual(observation["classification"], "unknown")

    def test_absent_checks_outage_and_rate_limit_never_become_green(self):
        self.assertEqual(self.inspect_with(runs=[], statuses=[])["classification"], "checks-pending")
        outage = self.inspect_with(failure={"kind": "outage-or-auth", "reason": "network down"})
        rate = self.inspect_with(failure={"kind": "rate-limited", "reason": "API rate limit exceeded"})
        self.assertEqual(outage["classification"], "unknown")
        self.assertEqual(rate["classification"], "unknown")

    def test_missing_required_check_and_non_strict_base_never_become_green(self):
        only_fast = [{"name": "fast", "status": "completed", "conclusion": "success", "head_sha": self.head}]
        pending = self.inspect_with(
            runs=only_fast,
            protection={"strict": True, "contexts": ["fast", "full-suite"], "checks": []},
        )
        self.assertEqual(pending["classification"], "checks-pending")
        self.assertIn("full-suite", pending["reason"])
        unsafe = self.inspect_with(protection={"strict": False, "contexts": ["test"], "checks": []})
        self.assertEqual(unsafe["classification"], "unknown")
        wrong_app = [{"name": "test", "status": "completed", "conclusion": "success",
                      "head_sha": self.head, "app": {"id": 8}}]
        app_bound = self.inspect_with(runs=wrong_app,
                                      protection={"strict": True, "contexts": [],
                                                  "checks": [{"context": "test", "app_id": 7}]})
        self.assertEqual(app_bound["classification"], "checks-pending")
        self.assertIn("app:7", app_bound["reason"])

    def test_lifecycle_and_binding_changes_have_distinct_states(self):
        changed = self.pr(); changed["head"] = {**changed["head"], "sha": "c" * 40}
        self.assertEqual(self.inspect_with(pr=changed)["classification"], "changed-head")
        moved = self.pr(); moved["base"] = {**moved["base"], "sha": "d" * 40}
        self.assertEqual(self.inspect_with(pr=moved)["classification"], "moved-base")
        self.assertEqual(self.inspect_with(pr=self.pr(merged=True, state="closed"))["classification"], "merged")
        self.assertEqual(self.inspect_with(pr=self.pr(state="closed"))["classification"], "closed")

    def test_repository_url_mismatch_fails_before_network(self):
        self.item["pr_url"] = "https://github.com/other/widget/pull/7"
        with mock.patch("helm.forge._api") as api:
            observation = forge.inspect(self.item, self.project)
        self.assertEqual(observation["classification"], "unknown")
        api.assert_not_called()

    def persist(self):
        write_json(self.home / "projects.json", {"projects": {"p": {**self.project, "mode": "direct-pr", "authority": 3,
                                                                       "test_cmd": "true", "protected_paths": [], "gate": "native"}}})
        directory = self.home / "work" / self.item["id"]; directory.mkdir(parents=True)
        write_json(directory / "item.json", self.item)
        write_json(self.home / "supervisor.json", {"version": 1, "observations": {}, "away": {"enabled": False}})
        write_json(self.home / "wakes.json", {"version": 1, "next_id": 1, "events": []})

    def test_observation_history_and_wake_are_deduplicated(self):
        self.persist()
        observation = {"provider": "github", "classification": "checks-green", "reason": "green",
                       "evidence_complete": True, "expected": {"head_sha": self.head}, "observed_at": "2026-01-01T00:00:00Z"}
        with mock.patch("helm.forge.inspect", return_value=observation):
            forge.monitor_item(self.item["id"]); forge.monitor_item(self.item["id"])
        stored = json.loads((self.home / "work" / self.item["id"] / "item.json").read_text())
        wakes = json.loads((self.home / "wakes.json").read_text())["events"]
        self.assertEqual(len(stored["forge"]["history"]), 1)
        self.assertEqual(len(wakes), 1)

    def test_post_observation_item_mutation_causes_cas_rejection(self):
        self.persist()
        observation = {"provider": "github", "classification": "checks-green", "reason": "green",
                       "evidence_complete": True, "expected": {"head_sha": self.head}, "observed_at": "2026-01-01T00:00:00Z"}
        def mutate_during_network(_item, _project):
            control.cas_update(self.item["id"], lambda current: current.update(head_sha="c" * 40))
            return observation
        with mock.patch("helm.forge.inspect", side_effect=mutate_during_network):
            with self.assertRaises(HelmError): forge.monitor_item(self.item["id"])
        stored = json.loads((self.home / "work" / self.item["id"] / "item.json").read_text())
        self.assertNotIn("forge", stored)


if __name__ == "__main__":
    unittest.main()
