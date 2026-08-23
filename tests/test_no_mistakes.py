"""The optional real no-mistakes adapter is reachable, exact, and non-replaying."""
try:
    import _gitenv  # noqa: F401
except ImportError:
    from tests import _gitenv  # noqa: F401

import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from helm import control, gates, no_mistakes, work, worktree
from helm.util import HelmError
from helm.util import write_json


RUN_COLUMNS = {
    "id": "TEXT PRIMARY KEY", "repo_id": "TEXT", "branch": "TEXT",
    "head_sha": "TEXT", "base_sha": "TEXT", "submitted_head_sha": "TEXT",
    "no_mistakes_version": "TEXT", "no_mistakes_build_sha": "TEXT",
    "review_approved_head_sha": "TEXT", "status": "TEXT", "pr_url": "TEXT",
    "pr_state": "TEXT", "ci_ready_at": "INTEGER", "ci_ready_no_ci": "INTEGER",
    "last_pushed_sha": "TEXT", "push_target_kind": "TEXT",
    "push_target_fingerprint": "TEXT", "push_ref": "TEXT", "push_active": "INTEGER",
    "terminal_head_verified_at": "INTEGER", "error": "TEXT",
    "awaiting_agent_since": "INTEGER", "created_at": "INTEGER", "updated_at": "INTEGER",
}


class NoMistakesAdapterTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="firstmate-no-mistakes-test-"))
        self.helm_home = self.root / "helm"; self.nm_home = self.root / "no-mistakes"
        self.repo = self.root / "repo"; self.repo.mkdir(); self.nm_home.mkdir(mode=0o700)
        self.old_env = {key: os.environ.get(key) for key in ("HELM_HOME", "NM_HOME", "PATH")}
        os.environ["HELM_HOME"] = str(self.helm_home); os.environ["NM_HOME"] = str(self.nm_home)
        self.bin_dir = self.root / "bin"; self.bin_dir.mkdir()
        self.binary = self.bin_dir / "no-mistakes"
        self.binary.write_text("#!/bin/sh\nexit 99\n"); self.binary.chmod(0o755)
        os.environ["PATH"] = str(self.bin_dir) + os.pathsep + (self.old_env["PATH"] or "")

        self.git("init", "-q", "-b", "main"); self.git("config", "user.email", "t@t")
        self.git("config", "user.name", "t")
        (self.repo / "base.txt").write_text("base\n")
        self.git("add", "base.txt"); self.git("commit", "-qm", "base")
        self.git("remote", "add", "origin", "git@github.com:acme/widget.git")
        self.base = self.git("rev-parse", "HEAD")

        self.repo_id = "nm-repo"
        self.gate_repo = self.nm_home / "repos" / f"{self.repo_id}.git"
        self.gate_repo.mkdir(parents=True)
        self.git("remote", "add", "no-mistakes", str(self.gate_repo))
        self.db_path = self.nm_home / "state.sqlite"
        self.create_db()

        self.project = {
            "id": "p", "path": str(self.repo), "mode": "direct-pr", "authority": 2,
            "base": "main", "test_cmd": "true", "protected_paths": [], "gate": "no-mistakes",
        }
        write_json(self.helm_home / "projects.json", {"projects": {"p": self.project}})
        write_json(self.helm_home / "supervisor.json", {"version": 1, "observations": {},
                                                         "away": {"enabled": False}})
        write_json(self.helm_home / "wakes.json", {"version": 1, "next_id": 1, "events": []})
        self.work_id = "p-external-gate"
        self.wt = worktree.create(self.project, self.work_id)
        (self.wt / "change.txt").write_text("reviewed\n")
        self.wgit("add", "change.txt"); self.wgit("commit", "-qm", "reviewed")
        self.head = self.wgit("rev-parse", "HEAD")
        directory = self.helm_home / "work" / self.work_id; directory.mkdir(parents=True)
        write_json(directory / "item.json", self.item())
        self.attestation = {
            "verified": True, "binary": str(self.binary),
            "sha256": no_mistakes.MACOS_RELEASES["arm64"]["binary_sha256"],
            "version": no_mistakes.VERSION, "build_sha": no_mistakes.BUILD_SHA,
            "tag_sha": no_mistakes.TAG_SHA, "team_id": no_mistakes.MACOS_TEAM_ID,
            "identifier": no_mistakes.MACOS_IDENTIFIER, "architecture": "arm64",
        }
        self.attest = mock.patch("helm.no_mistakes._binary_attestation",
                                 return_value=self.attestation)
        self.attest.start()

    def tearDown(self):
        self.attest.stop()
        for key, value in self.old_env.items():
            if value is None: os.environ.pop(key, None)
            else: os.environ[key] = value
        shutil.rmtree(self.root, ignore_errors=True)

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              text=True, capture_output=True).stdout.strip()

    def wgit(self, *args):
        return subprocess.run(["git", "-C", str(self.wt), *args], check=True,
                              text=True, capture_output=True).stdout.strip()

    def create_db(self):
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("CREATE TABLE repos (id TEXT PRIMARY KEY, working_path TEXT UNIQUE, "
                       "upstream_url TEXT, fork_url TEXT, default_branch TEXT)")
            db.execute("INSERT INTO repos VALUES (?, ?, ?, ?, ?)",
                       (self.repo_id, str(self.repo.resolve()), "git@github.com:acme/widget.git", "", "main"))
            definitions = ", ".join(f"{name} {kind}" for name, kind in RUN_COLUMNS.items())
            db.execute(f"CREATE TABLE runs ({definitions})")
            db.execute("CREATE TABLE step_results (id TEXT PRIMARY KEY, run_id TEXT, step_name TEXT, "
                       "step_order INTEGER, status TEXT, findings_json TEXT)")

    def item(self):
        review = {"role": "correctness", "verdict": "accept", "sha": self.head,
                  "base_sha": self.base, "fresh": True, "valid": True,
                  "reviewer": {"kind": "test", "identity": "independent"}}
        return {
            "id": self.work_id, "project": "p", "kind": "ship", "status": "running",
            "phase": "delivering", "created": "2026-01-01T00:00:00Z",
            "updated": "2026-01-01T00:00:00Z", "revision": 0,
            "branch": f"firstmate/{self.work_id}", "worktree": str(self.wt),
            "head_sha": self.head, "pr_url": None, "session": None, "agent_launches": [],
            "text": "Ship the exact reviewed change without bypassing human decisions.",
            "dispatch": {"graph": "direct-pr", "rule": "test"}, "reviews": [review],
            "controls": {"paused": False, "events": [], "pending": []}, "history": [],
            "verification": [{"ok": True, "complete": True, "base_sha": self.base,
                              "head_sha": self.head,
                              "fingerprint": {"sha": self.head, "dirty": []}}],
            "attempts": 1, "budgets": {"tokens": None, "cost": None, "seconds": None},
            "usage": {"seconds": 0.0},
        }

    def insert_run(self, *, run_id="run-1", head=None, base=None, build=None,
                   reviewed=True, ready=True, status="running"):
        head = head or self.head; base = base or self.base
        values = {name: None for name in RUN_COLUMNS}
        values.update({
            "id": run_id, "repo_id": self.repo_id, "branch": f"firstmate/{self.work_id}",
            "head_sha": head, "base_sha": base, "submitted_head_sha": self.head,
            "no_mistakes_version": no_mistakes.VERSION,
            "no_mistakes_build_sha": build or no_mistakes.BUILD_SHA,
            "review_approved_head_sha": head if reviewed else None, "status": status,
            "pr_url": "https://github.com/acme/widget/pull/7" if ready else None,
            "pr_state": "open" if ready else "none", "ci_ready_at": 10 if ready else None,
            "ci_ready_no_ci": 0, "last_pushed_sha": head if ready else None,
            "push_target_kind": "upstream",
            "push_target_fingerprint": no_mistakes._target_fingerprint("git@github.com:acme/widget.git"),
            "push_ref": f"refs/heads/firstmate/{self.work_id}", "push_active": 0,
            "created_at": 1, "updated_at": 2,
        })
        with closing(sqlite3.connect(self.db_path)) as db, db:
            names = list(values)
            db.execute(f"INSERT INTO runs ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})",
                       [values[name] for name in names])

    def insert_gate(self, run_id="run-1"):
        findings = {"summary": "one decision", "items": [{
            "id": "r1", "severity": "error", "file": "src/app.py", "line": 3,
            "action": "ask-user", "description": "This changes product behavior; choose explicitly.",
        }]}
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("INSERT INTO step_results VALUES (?, ?, ?, ?, ?, ?)",
                       ("step-review", run_id, "review", 3, "awaiting_approval", json.dumps(findings)))

    @staticmethod
    def write_command_evidence(kwargs, stdout="ok\n", stderr=""):
        kwargs["stdout_path"].parent.mkdir(parents=True, exist_ok=True)
        kwargs["stdout_path"].write_text(stdout); kwargs["stderr_path"].write_text(stderr)

    def test_status_proves_pinned_binary_repository_and_schema_without_writing_database(self):
        before = (self.db_path.read_bytes(), self.db_path.stat().st_mtime_ns)
        result = no_mistakes.status(self.repo)
        after = (self.db_path.read_bytes(), self.db_path.stat().st_mtime_ns)
        self.assertTrue(result["ready"], result)
        self.assertTrue(result["binary_provenance_verified"])
        self.assertTrue(result["repository_binding_verified"])
        self.assertEqual(result["binding"]["repo_id"], self.repo_id)
        self.assertEqual(before, after, "status must keep the external database byte-for-byte read-only")

    def test_exact_success_receipt_completes_once_and_is_required_for_delivery(self):
        calls = []
        def driver(_binary, args, **kwargs):
            calls.append(args)
            self.assertEqual(work.load(self.work_id)["external_gate"]["state"], "request-started")
            self.write_command_evidence(kwargs)
            self.insert_run()
            return subprocess.CompletedProcess(args, 0)
        with mock.patch("helm.no_mistakes.invoke", side_effect=driver):
            completed = gates.start_or_reconcile(work.load(self.work_id), self.project, self.wt)
            again = gates.start_or_reconcile(completed, self.project, self.wt)
        self.assertEqual(len(calls), 1, "a complete transaction must never be submitted twice")
        self.assertEqual(again["status"], "pr-open")
        self.assertEqual(again["external_gate"]["state"], "complete")
        self.assertEqual(again["pr_delivery"]["provider"], "no-mistakes")
        self.assertEqual(gates.require_receipt("no-mistakes", again, self.project)["run_id"], "run-1")

    def test_request_without_receipt_becomes_unknown_and_is_never_replayed(self):
        calls = []
        def driver(_binary, args, **kwargs):
            calls.append(args); self.write_command_evidence(kwargs, stderr="transport ended\n")
            return subprocess.CompletedProcess(args, 1)
        with mock.patch("helm.no_mistakes.invoke", side_effect=driver):
            first = gates.start_or_reconcile(work.load(self.work_id), self.project, self.wt)
            second = gates.start_or_reconcile(first, self.project, self.wt)
        self.assertEqual(len(calls), 1)
        self.assertEqual(second["status"], "needs-you")
        self.assertEqual(second["external_gate"]["state"], "unknown")
        self.assertIn("will not be replayed", second["ask"]["context"])

    def test_run_side_effect_then_caller_crash_reconciles_without_replay(self):
        calls = []
        def driver(_binary, args, **kwargs):
            calls.append(args); self.write_command_evidence(kwargs)
            self.insert_run()
            raise RuntimeError("simulated caller crash after external commit")
        with mock.patch("helm.no_mistakes.invoke", side_effect=driver):
            with self.assertRaisesRegex(RuntimeError, "simulated caller crash"):
                gates.start_or_reconcile(work.load(self.work_id), self.project, self.wt)
            journal = work.load(self.work_id)
            self.assertEqual(journal["external_gate"]["state"], "request-started")
            recovered = gates.start_or_reconcile(journal, self.project, self.wt)
        self.assertEqual(len(calls), 1, "recovery must observe the receipt, not replay the run")
        self.assertEqual(recovered["external_gate"]["state"], "complete")
        self.assertEqual(recovered["external_gate"]["run_id"], "run-1")

    def test_exact_gate_is_relayed_and_explicit_response_advances_same_run(self):
        calls = []
        def driver(_binary, args, **kwargs):
            calls.append(args); self.write_command_evidence(kwargs)
            if args[0] == "run":
                self.insert_run(reviewed=False, ready=False); self.insert_gate()
            else:
                self.assertEqual(work.load(self.work_id)["external_gate"]["state"], "response-requested")
                with closing(sqlite3.connect(self.db_path)) as db, db:
                    db.execute("UPDATE step_results SET status = 'completed' WHERE id = 'step-review'")
                    db.execute("UPDATE runs SET review_approved_head_sha = ?, pr_url = ?, pr_state = 'open', "
                               "ci_ready_at = 10, last_pushed_sha = ? WHERE id = 'run-1'",
                               (self.head, "https://github.com/acme/widget/pull/7", self.head))
            return subprocess.CompletedProcess(args, 0)
        with mock.patch("helm.no_mistakes.invoke", side_effect=driver):
            waiting = gates.start_or_reconcile(work.load(self.work_id), self.project, self.wt)
            self.assertEqual(waiting["status"], "needs-you")
            finding = waiting["ask"]["gate"]["findings"]["items"][0]
            self.assertEqual(finding["description"], "This changes product behavior; choose explicitly.")
            completed = gates.respond(self.work_id, "fix", ["r1"], "Keep the public behavior explicit")
        self.assertEqual([call[0] for call in calls], ["run", "respond"])
        self.assertNotIn("--yes", calls[0] + calls[1])
        self.assertEqual(calls[1][calls[1].index("--step") + 1], "review")
        self.assertEqual(completed["status"], "pr-open")
        self.assertEqual(completed["external_gate"]["responses"][-1]["state"], "observed")

    def test_unchanged_gate_after_response_is_unknown_and_cannot_be_answered_again(self):
        def driver(_binary, args, **kwargs):
            self.write_command_evidence(kwargs)
            if args[0] == "run":
                self.insert_run(reviewed=False, ready=False); self.insert_gate()
            return subprocess.CompletedProcess(args, 0)
        with mock.patch("helm.no_mistakes.invoke", side_effect=driver):
            gates.start_or_reconcile(work.load(self.work_id), self.project, self.wt)
            uncertain = gates.respond(self.work_id, "approve")
            with self.assertRaises(SystemExit):
                gates.respond(self.work_id, "approve")
        self.assertEqual(uncertain["external_gate"]["state"], "unknown")
        self.assertIn("will not be replayed", uncertain["ask"]["context"])

    def test_response_side_effect_then_caller_crash_reconciles_without_replay(self):
        calls = []
        def driver(_binary, args, **kwargs):
            calls.append(args); self.write_command_evidence(kwargs)
            if args[0] == "run":
                self.insert_run(reviewed=False, ready=False); self.insert_gate()
                return subprocess.CompletedProcess(args, 0)
            with closing(sqlite3.connect(self.db_path)) as db, db:
                db.execute("UPDATE step_results SET status = 'completed' WHERE id = 'step-review'")
                db.execute("UPDATE runs SET review_approved_head_sha = ?, pr_url = ?, pr_state = 'open', "
                           "ci_ready_at = 10, last_pushed_sha = ? WHERE id = 'run-1'",
                           (self.head, "https://github.com/acme/widget/pull/7", self.head))
            raise RuntimeError("simulated caller crash after external response")
        with mock.patch("helm.no_mistakes.invoke", side_effect=driver):
            gates.start_or_reconcile(work.load(self.work_id), self.project, self.wt)
            with self.assertRaisesRegex(RuntimeError, "simulated caller crash"):
                gates.respond(self.work_id, "approve")
            journal = work.load(self.work_id)
            self.assertEqual(journal["external_gate"]["state"], "response-requested")
            self.assertEqual(journal["external_gate"]["responses"][-1]["state"], "requested")
            recovered = gates.start_or_reconcile(journal, self.project, self.wt)
        self.assertEqual([call[0] for call in calls], ["run", "respond"])
        self.assertEqual(recovered["external_gate"]["state"], "complete")
        self.assertEqual(recovered["external_gate"]["responses"][-1]["state"], "observed")

    def test_uncertain_response_cannot_be_reissued_after_caller_crash(self):
        calls = []
        def driver(_binary, args, **kwargs):
            calls.append(args); self.write_command_evidence(kwargs)
            if args[0] == "run":
                self.insert_run(reviewed=False, ready=False); self.insert_gate()
                return subprocess.CompletedProcess(args, 0)
            raise RuntimeError("simulated caller crash before a changed receipt")
        with mock.patch("helm.no_mistakes.invoke", side_effect=driver):
            gates.start_or_reconcile(work.load(self.work_id), self.project, self.wt)
            with self.assertRaises(RuntimeError):
                gates.respond(self.work_id, "approve")
            uncertain = gates.start_or_reconcile(work.load(self.work_id), self.project, self.wt)
            with self.assertRaises(SystemExit):
                gates.respond(self.work_id, "approve")
        self.assertEqual([call[0] for call in calls], ["run", "respond"])
        self.assertEqual(uncertain["external_gate"]["state"], "unknown")
        self.assertIn("will not be replayed", uncertain["ask"]["context"])

    def test_open_external_transaction_blocks_controls_and_cancellation(self):
        def driver(_binary, args, **kwargs):
            self.write_command_evidence(kwargs, stderr="outcome unavailable\n")
            return subprocess.CompletedProcess(args, 1)
        with mock.patch("helm.no_mistakes.invoke", side_effect=driver):
            uncertain = gates.start_or_reconcile(work.load(self.work_id), self.project, self.wt)
        self.assertEqual(uncertain["external_gate"]["state"], "unknown")
        for action, value in (("pause", None), ("steer", "change direction"), ("recover", None)):
            with self.subTest(action=action), self.assertRaises(HelmError):
                control.request(self.work_id, action, value)
        with self.assertRaises(HelmError):
            work.cancel(self.work_id, discard=True)

    def test_new_exact_revision_archives_completed_transaction_and_submits_once(self):
        calls = []
        def first_driver(_binary, args, **kwargs):
            calls.append((self.head, args)); self.write_command_evidence(kwargs); self.insert_run()
            return subprocess.CompletedProcess(args, 0)
        with mock.patch("helm.no_mistakes.invoke", side_effect=first_driver):
            first = gates.start_or_reconcile(work.load(self.work_id), self.project, self.wt)
        old_transaction = first["external_gate"]["id"]

        (self.wt / "second.txt").write_text("new reviewed revision\n")
        self.wgit("add", "second.txt"); self.wgit("commit", "-qm", "second reviewed revision")
        self.head = self.wgit("rev-parse", "HEAD")
        revised = work.load(self.work_id)
        revised.update(status="running", phase="delivering", head_sha=self.head,
                       pr_url=None, ask=None)
        revised["controls"]["paused"] = False
        revised["reviews"] = [{"role": "correctness", "verdict": "accept", "sha": self.head,
                               "base_sha": self.base, "fresh": True, "valid": True,
                               "reviewer": {"kind": "test", "identity": "independent-2"}}]
        revised["verification"] = [{"ok": True, "complete": True, "base_sha": self.base,
                                    "head_sha": self.head,
                                    "fingerprint": {"sha": self.head, "dirty": []}}]
        work.save(revised)

        def second_driver(_binary, args, **kwargs):
            calls.append((self.head, args)); self.write_command_evidence(kwargs)
            self.insert_run(run_id="run-2")
            return subprocess.CompletedProcess(args, 0)
        with mock.patch("helm.no_mistakes.invoke", side_effect=second_driver):
            second = gates.start_or_reconcile(work.load(self.work_id), self.project, self.wt)
            repeated = gates.start_or_reconcile(second, self.project, self.wt)
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(second["external_gate"]["id"], old_transaction)
        self.assertEqual(second["external_gate_history"][-1]["id"], old_transaction)
        self.assertEqual(repeated["external_gate"]["run_id"], "run-2")
        self.assertEqual(repeated["pr_delivery"]["head_sha"], self.head)

    def test_post_review_head_mutation_never_becomes_success(self):
        changed = "f" * 40
        def driver(_binary, args, **kwargs):
            self.write_command_evidence(kwargs); self.insert_run(head=changed)
            return subprocess.CompletedProcess(args, 0)
        with mock.patch("helm.no_mistakes.invoke", side_effect=driver):
            result = gates.start_or_reconcile(work.load(self.work_id), self.project, self.wt)
        self.assertEqual(result["status"], "needs-you")
        self.assertEqual(result["external_gate"]["last_observation"]["classification"], "changed-head")
        self.assertIn("fresh First Mate verification", result["ask"]["context"])

    def test_wrong_push_target_or_closed_pr_never_becomes_success(self):
        def driver(_binary, args, **kwargs):
            self.write_command_evidence(kwargs); self.insert_run()
            with closing(sqlite3.connect(self.db_path)) as db, db:
                db.execute("UPDATE runs SET push_target_fingerprint = ?, pr_state = 'closed' WHERE id = 'run-1'",
                           ("0" * 64,))
            return subprocess.CompletedProcess(args, 0)
        with mock.patch("helm.no_mistakes.invoke", side_effect=driver):
            result = gates.start_or_reconcile(work.load(self.work_id), self.project, self.wt)
        observation = result["external_gate"]["last_observation"]
        self.assertNotEqual(observation["classification"], "checks-passed")
        self.assertFalse(observation["checks"]["push_target_fingerprint"])
        self.assertFalse(observation["checks"]["pr_open"])
        with self.assertRaises(HelmError):
            gates.require_receipt("no-mistakes", result, self.project)

    def test_configured_first_mate_budget_fails_before_external_invocation(self):
        item = work.load(self.work_id); item["budgets"]["tokens"] = 100; work.save(item)
        with mock.patch("helm.no_mistakes.invoke") as driver, self.assertRaises(SystemExit):
            gates.start_or_reconcile(work.load(self.work_id), self.project, self.wt)
        driver.assert_not_called()

    def test_configured_node_budget_fails_before_external_invocation(self):
        item = work.load(self.work_id)
        item["node_budgets"] = {"implement": {"tokens": 100, "cost": None, "seconds": None}}
        item["node_usage"] = {"implement": work._new_node_usage()}
        work.save(item)
        with mock.patch("helm.no_mistakes.invoke") as driver, self.assertRaises(SystemExit):
            gates.start_or_reconcile(work.load(self.work_id), self.project, self.wt)
        driver.assert_not_called()


class BinaryAttestationTests(unittest.TestCase):
    def test_release_hash_build_signature_requirement_and_architecture_are_all_required(self):
        root = Path(tempfile.mkdtemp(prefix="firstmate-nm-binary-test-"))
        try:
            binary = root / "no-mistakes"; binary.write_bytes(b"fixture"); binary.chmod(0o755)
            signature = ("Identifier=com.kunchenguid.no-mistakes\n"
                         "Authority=Developer ID Application: Kun Chen (9T2J7MNUP9)\n"
                         "TeamIdentifier=9T2J7MNUP9\nTimestamp=Aug 22, 2026\n"
                         "CodeDirectory v=20500 flags=0x10000(runtime)\n")
            designated = ('designated => identifier "com.kunchenguid.no-mistakes" and anchor apple generic '
                          'and certificate leaf[subject.OU] = "9T2J7MNUP9"\n')
            def command(args, **_kwargs):
                if args[-1] == "--version":
                    return subprocess.CompletedProcess(args, 0,
                        "no-mistakes version v1.57.1 (a6f64fc) 2026-08-22T13:12:55Z\n", "")
                if args[0] == "/usr/bin/lipo":
                    return subprocess.CompletedProcess(args, 0, "arm64\n", "")
                if "-r-" in args:
                    return subprocess.CompletedProcess(args, 0, "", designated)
                if "-dvvv" in args:
                    return subprocess.CompletedProcess(args, 0, "", signature)
                return subprocess.CompletedProcess(args, 0, "", "valid on disk")
            with mock.patch("helm.no_mistakes.platform.system", return_value="Darwin"), \
                 mock.patch("helm.no_mistakes.platform.machine", return_value="arm64"), \
                 mock.patch("helm.no_mistakes._sha256",
                            return_value=no_mistakes.MACOS_RELEASES["arm64"]["binary_sha256"]), \
                 mock.patch("helm.no_mistakes._command", side_effect=command):
                result = no_mistakes._binary_attestation(binary)
            self.assertTrue(result["verified"], result)
            self.assertEqual(result["build_sha"], no_mistakes.TAG_SHA[:7])
            self.assertEqual(result["team_id"], "9T2J7MNUP9")
        finally:
            shutil.rmtree(root, ignore_errors=True)
