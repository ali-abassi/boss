"""Doctor is observational by default and quarantines rather than destroys."""
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
import time
import unittest
from pathlib import Path
from unittest import mock

from bossctl import doctor, supervisor, work, worktree
from bossctl.paths import supervisor_lock
from bossctl.util import BossError, locked, write_json

REPO = Path(__file__).resolve().parents[1]


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()); self.home = self.root / "home"; self.home.mkdir()
        os.environ["BOSS_HOME"] = str(self.home)
        os.environ["BOSS_AVAILABLE_MODELS"] = "openai-codex/gpt-5.6-sol,openai-codex/gpt-5.4-mini"
        self.repo = self.root / "repo"; self.repo.mkdir()
        subprocess.run(["git", "-C", str(self.repo), "init", "-q", "-b", "main"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "t"], check=True)
        (self.repo / "README.md").write_text("x\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "init"], check=True)
        write_json(self.home / "projects.json", {"projects": {"p": {
            "id": "p", "path": str(self.repo), "mode": "local-only", "gate": "native",
            "authority": 1, "base": "main", "test_cmd": "true", "protected_paths": []}}})
        write_json(self.home / "supervisor.json", {"version": 1, "observations": {}, "away": {"enabled": False}})
        write_json(self.home / "wakes.json", {"version": 1, "next_id": 1, "events": []})

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        os.environ.pop("BOSS_AVAILABLE_MODELS", None)

    def snapshot(self):
        return {str(p.relative_to(self.home)): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.home.rglob("*") if p.is_file()}

    def detached_item(self, work_id: str, *, session=None):
        project = json.loads((self.home / "projects.json").read_text())["projects"]["p"]
        wt = worktree.create(project, work_id)
        (wt / "README.md").write_text(f"modified by {work_id}\n")
        (wt / "untracked.txt").write_text("untracked payload\n")
        (wt / "staged-new.txt").write_text("staged payload\n")
        (wt / "payload-link").symlink_to("untracked.txt")
        subprocess.run(["git", "-C", str(wt), "add", "staged-new.txt"], check=True)
        item_dir = self.home / "work" / work_id; item_dir.mkdir(parents=True)
        item = {"id": work_id, "project": "p", "status": "paused", "phase": "recovery-required",
                "created": "2026-01-01T00:00:00Z", "updated": "2026-01-01T00:00:00Z",
                "revision": 0, "session": session, "history": [], "attempts": 1,
                "agent_launches": [], "worktree": str(wt),
                "controls": {"paused": True, "events": [], "pending": []}}
        write_json(item_dir / "item.json", item)
        gitfile = wt / ".git"
        admin = Path(gitfile.read_text().removeprefix("gitdir: ").strip())
        preserved_admin = self.root / f"{work_id}-pruned-admin"
        os.replace(admin, preserved_admin)
        return project, wt, item, preserved_admin

    def test_audit_is_byte_for_byte_read_only(self):
        before = self.snapshot()
        report = doctor.audit(network=False)
        after = self.snapshot()
        self.assertTrue(report["read_only"])
        self.assertEqual(after, before)
        self.assertFalse((self.home / "doctor-repair.lock").exists())
        freshness = next(c for c in report["checks"] if c["id"] == "project:p:freshness")
        self.assertIn(freshness["status"], ("warning", "unknown"))

    def test_state_home_permission_repair_is_read_only_until_confirmed(self):
        self.home.chmod(0o755)
        before = self.home.stat().st_mode & 0o777
        report = doctor.audit(network=False)
        check = next(value for value in report["checks"] if value["id"] == "state:home-permissions")
        self.assertEqual(check["status"], "error")
        self.assertEqual(self.home.stat().st_mode & 0o777, before)
        self.assertTrue(any(action["action"] == "tighten-state-home-permissions"
                            for action in report["repairs"]))
        result = doctor.repair(confirm=True, network=False)
        self.assertEqual(self.home.stat().st_mode & 0o777, 0o700)
        self.assertTrue(any(action["action"] == "tighten-state-home-permissions"
                            for action in result["applied"]))

    def test_state_record_permission_repair_is_inode_bound_and_confirmed(self):
        self.home.chmod(0o700)
        work_dir = self.home / "work" / "p-old"; work_dir.mkdir(parents=True)
        item_file = work_dir / "item.json"; item_file.write_text("{}\n")
        log_file = self.home / "bossctl.log"; log_file.write_text("old log\n")
        git_store = work_dir / "runs" / "old-run" / ".git"
        hook = git_store / "hooks" / "pre-commit.sample"
        hook.parent.mkdir(parents=True); hook.write_text("#!/bin/sh\nexit 0\n")
        git_store.chmod(0o755); hook.chmod(0o755)
        work_dir.chmod(0o755); item_file.chmod(0o644); log_file.chmod(0o644)
        report = doctor.audit(network=False)
        check = next(value for value in report["checks"] if value["id"] == "state:record-permissions")
        self.assertEqual(check["status"], "error")
        self.assertEqual(work_dir.stat().st_mode & 0o777, 0o755)
        self.assertEqual(item_file.stat().st_mode & 0o777, 0o644)
        self.assertFalse(any(".git" in Path(entry["target"]).parts
                             for entry in report["repairs"]
                             if entry["action"] == "tighten-state-record-permissions"))
        result = doctor.repair(confirm=True, network=False)
        applied = [entry for entry in result["applied"]
                   if entry["action"] == "tighten-state-record-permissions"]
        self.assertGreaterEqual(len(applied), 3)
        self.assertEqual(work_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual(item_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(log_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(git_store.stat().st_mode & 0o777, 0o755)
        self.assertEqual(hook.stat().st_mode & 0o777, 0o755)

    def test_model_inventory_runs_in_a_disposable_pi_home(self):
        os.environ.pop("BOSS_AVAILABLE_MODELS", None)
        source = self.home / "pi"; source.mkdir(); (source / "settings.json").write_text("{}\n")
        (source / "auth.json").write_text('{"openai-codex":{"type":"oauth","access":"fixture"}}\n')
        fake_bin = self.root / "bin"; fake_bin.mkdir(); fake_pi = fake_bin / "pi"
        fake_pi.write_text("#!/bin/sh\ntest -f \"$PI_CODING_AGENT_DIR/auth.json\" || exit 7\nprintf touched > \"$PI_CODING_AGENT_DIR/touched\"\nprintf 'openai-codex gpt-5.6-sol\\n'\n")
        fake_pi.chmod(0o755)
        before = self.snapshot(); old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{fake_bin}:{old_path}"
        try:
            available, error = doctor._offline_model_inventory()
        finally:
            os.environ["PATH"] = old_path
            os.environ["BOSS_AVAILABLE_MODELS"] = "openai-codex/gpt-5.6-sol,openai-codex/gpt-5.4-mini"
        self.assertIsNone(error)
        self.assertEqual(available, {"openai-codex/gpt-5.6-sol"})
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((source / "touched").exists())

    def test_unconfirmed_repair_changes_nothing(self):
        (self.home / "wakes.json").write_text("{broken")
        before = self.snapshot()
        with self.assertRaises(BossError): doctor.repair(confirm=False, network=False)
        self.assertEqual(self.snapshot(), before)

    def test_confirmed_corrupt_derived_state_is_backed_up_then_rebuilt(self):
        (self.home / "wakes.json").write_text("{broken")
        result = doctor.repair(confirm=True, network=False)
        repaired = json.loads((self.home / "wakes.json").read_text())
        backups = list(self.home.glob("wakes.json.quarantine-*"))
        self.assertEqual(repaired["version"], 1)
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "{broken")
        self.assertTrue(any(a["action"] == "quarantine-state" for a in result["applied"]))

    def test_parseable_but_wrong_supervisor_schema_is_not_reported_healthy(self):
        write_json(self.home / "supervisor.json", {"version": 999, "observations": []})
        report = doctor.audit(network=False)
        check = next(c for c in report["checks"] if c["id"] == "state:supervisor")
        self.assertEqual(check["status"], "error")
        self.assertTrue(any(r["action"] == "quarantine-state" and r["target"].endswith("supervisor.json")
                            for r in report["repairs"]))

    def test_nested_corrupt_item_and_memory_are_reported_without_rewriting(self):
        directory = self.home / "work" / "p-corrupt"; directory.mkdir(parents=True)
        item_path = directory / "item.json"
        write_json(item_path, {"id": "p-corrupt", "project": "p", "status": "running", "revision": 0,
                               "branch": "boss/p-corrupt",
                               "worktree": str(self.home / "worktrees" / "p" / "p-corrupt"),
                               "agent_launches": [None], "controls": {"events": [], "pending": []}})
        memory_path = self.home / "memory.json"
        write_json(memory_path, {"version": 1, "entries": [{"id": "wrong", "scope": "operational"}]})
        before = {item_path: item_path.read_bytes(), memory_path: memory_path.read_bytes()}
        report = doctor.audit(network=False)
        checks = {value["id"]: value for value in report["checks"]}
        self.assertEqual(checks["state:item:p-corrupt"]["status"], "error")
        self.assertEqual(checks["state:memory"]["status"], "error")
        self.assertEqual(before, {item_path: item_path.read_bytes(), memory_path: memory_path.read_bytes()})

    def test_corrupt_node_budget_state_is_reported_without_rewriting(self):
        directory = self.home / "work" / "p-node-corrupt"; directory.mkdir(parents=True)
        item_path = directory / "item.json"
        # Simulate externally corrupted/non-standard JSON. The controller's
        # durable writer now refuses to manufacture NaN itself.
        item_path.write_text(json.dumps({
            "id": "p-node-corrupt", "project": "p", "status": "paused", "revision": 0,
            "branch": "boss/p-node-corrupt",
            "worktree": str(self.home / "worktrees" / "p" / "p-node-corrupt"),
            "agent_launches": [], "controls": {"events": [], "pending": []},
            "node_budgets": {"implement": {"tokens": 10}},
            "node_usage": {"implement": {
                "tokens": 0, "cost": 0.0, "seconds": 0.0,
                "receipts": {"session": {"tokens": float("nan"),
                                            "tokens_available": True}}}}}))
        before = item_path.read_bytes()
        report = doctor.audit(network=False)
        check = next(value for value in report["checks"] if value["id"] == "state:item:p-node-corrupt")
        self.assertEqual(check["status"], "error")
        self.assertIn("receipt tokens is malformed", check["summary"])
        self.assertEqual(item_path.read_bytes(), before)

    def test_dead_execution_is_paused_and_claim_held_without_touching_work(self):
        work_id = "p-dead"
        item_dir = self.home / "work" / work_id; item_dir.mkdir(parents=True)
        wt = self.home / "worktrees" / "p" / work_id; wt.mkdir(parents=True)
        marker = wt / "unlanded.txt"; marker.write_text("preserve me")
        started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 2))
        item = {"id": work_id, "project": "p", "status": "running", "phase": "implementing",
                "created": "2026-01-01T00:00:00Z", "updated": "2026-01-01T00:00:00Z", "revision": 0,
                "lease": {"pid": 999_999_999, "owner": "dead", "started": started},
                "activity": {"state": "verifying", "node": "verify", "node_started_at": started},
                "node_budgets": {"verify": {"tokens": None, "cost": None, "seconds": 60}},
                "node_usage": {"verify": work._new_node_usage()},
                "budgets": {"tokens": None, "cost": None, "seconds": 120},
                "usage": {"tokens": 0, "cost": 0.0, "seconds": 0.0,
                          "tokens_evidence_complete": True, "cost_evidence_complete": True},
                "session": None,
                "worktree": str(wt), "controls": {"paused": False}, "history": [], "attempts": 0}
        write_json(item_dir / "item.json", item)
        write_json(self.home / "scope-claims.json", {"version": 1, "claims": [{
            "project": "p", "work_id": work_id, "paths": ["src/**"], "owner": "dead", "pid": 999_999_999}]})
        result = doctor.repair(confirm=True, network=False)
        repaired = json.loads((item_dir / "item.json").read_text())
        claim = json.loads((self.home / "scope-claims.json").read_text())["claims"][0]
        self.assertEqual(repaired["status"], "paused")
        self.assertNotIn("lease", repaired)
        self.assertGreaterEqual(repaired["usage"]["seconds"], 1)
        self.assertGreaterEqual(repaired["node_usage"]["verify"]["seconds"], 1)
        self.assertTrue(repaired["node_usage"]["verify"]["seconds_evidence_complete"])
        self.assertEqual(marker.read_text(), "preserve me")
        self.assertTrue(claim["held_for_recovery"])
        self.assertIsNone(claim["pid"])
        self.assertFalse(any(a["action"].startswith("remove") for a in result["applied"]))

    def test_pi_boss_doctor_routes_to_doctor_not_a_harness(self):
        result = subprocess.run([str(REPO / "bin" / "pi-boss"), "doctor", "--offline", "--json"],
                                env={**os.environ}, text=True, capture_output=True)
        self.assertTrue(result.stdout.lstrip().startswith("{"), result.stderr)
        self.assertTrue(json.loads(result.stdout)["read_only"])

    def test_legacy_or_reused_pid_never_authorizes_worker_health_or_signal(self):
        reused = {"version": 1, "kind": "boss-worker", "pid": os.getpid(),
                  "pgid": os.getpgid(os.getpid()), "owner": "stale",
                  "start_sha256": "0" * 64, "command_sha256": "0" * 64}
        write_json(self.home / "daemon.pid", [reused, os.getpid()])
        report = doctor.audit(network=False)
        workers = [check for check in report["checks"] if check["id"].startswith("worker:")]
        self.assertTrue(workers); self.assertTrue(all(check["status"] == "unknown" for check in workers))
        result = subprocess.run([str(REPO / "bin" / "bossctl"), "down", "--json"],
                                env={**os.environ}, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not prove", result.stderr)
        self.assertEqual(json.loads((self.home / "daemon.pid").read_text()), [reused, os.getpid()])

    def test_confirmed_repair_reconciles_only_a_positively_dead_reviewer_launch(self):
        work_id = "p-reviewer"
        directory = self.home / "work" / work_id; directory.mkdir(parents=True)
        item = {"id": work_id, "project": "p", "status": "failed", "phase": "review-correctness",
                "created": "2026-01-01T00:00:00Z", "updated": "2026-01-01T00:00:00Z",
                "revision": 0, "session": None, "history": [], "attempts": 1,
                "agent_launches": [{"launch_id": "review-dead", "agent_session_id": "review-dead",
                                    "role": "reviewer", "state": "tab-created",
                                    "agent_name": "review-x", "pane_id": "w1:p9"}]}
        write_json(directory / "item.json", item)
        with mock.patch("bossctl.doctor._herdr_agent", return_value={"state": "dead", "status": "dead"}):
            result = doctor.repair(confirm=True, network=False)
        repaired = json.loads((directory / "item.json").read_text())
        self.assertEqual(repaired["agent_launches"][0]["state"], "dead-confirmed")
        self.assertTrue(any(action["action"] == "close-dead-reviewer-launch" for action in result["applied"]))

    def test_reused_herdr_tab_id_is_never_treated_as_owned_or_closed(self):
        write_json(self.home / "herdr.json", {"tabs": [{"tab_id": "w1:t7", "label": "our old tab",
                                                          "workspace_id": "w1", "herdr_session": "boss"}]})
        live = {"w1:t7": {"tab_id": "w1:t7", "label": "someone else's tab",
                            "workspace_id": "w1", "herdr_session": "boss"}}
        with mock.patch("bossctl.doctor._live_tabs", return_value=(live, None)):
            report = doctor.audit(network=False)
            check = next(value for value in report["checks"] if value["id"] == "tab:w1:t7")
            self.assertEqual(check["status"], "error")
            result = doctor.repair(confirm=True, network=False)
        self.assertTrue(any(action["action"] == "forget-unowned-tab" for action in result["applied"]))
        self.assertEqual(json.loads((self.home / "herdr.json").read_text())["tabs"], [])

    def test_wake_receipt_audit_uses_real_ack_and_consumer_fields(self):
        supervisor.observe({"id": "p-ready", "project": "p", "status": "ready", "phase": "merge-ready",
                            "created": "2026-01-01T00:00:00Z", "updated": "2026-01-01T00:00:00Z",
                            "activity": {"last": "2026-01-01T00:00:00Z"}, "attempts": 1,
                            "session": None, "lease": None, "ask": None}, probe_agent=False)
        event = supervisor.claim("coo")[0]
        supervisor.mark_sending([event["id"]], "coo")
        supervisor.mark_sent([event["id"]], "coo")
        uncertain = doctor.audit(network=False)
        receipt = next(check for check in uncertain["checks"] if check["id"] == f"wake-receipt:{event['id']}")
        self.assertEqual(receipt["consumer"], "coo")
        supervisor.acknowledge([event["id"]], "coo")
        acknowledged = doctor.audit(network=False)
        self.assertFalse(any(check["id"] == f"wake-receipt:{event['id']}" for check in acknowledged["checks"]))

    def test_quarantine_revalidation_holds_the_component_writer_lock_through_rename(self):
        path = self.home / "wakes.json"; path.write_text("{broken")
        original = doctor._quarantine
        writer_acquired = threading.Event(); blocked_while_replacing = []
        thread_holder = []
        def wrapped(target, default):
            def writer():
                with locked(supervisor_lock()):
                    writer_acquired.set()
            thread = threading.Thread(target=writer, daemon=True); thread_holder.append(thread); thread.start()
            time.sleep(0.1)
            blocked_while_replacing.append(not writer_acquired.is_set())
            return original(target, default)
        with mock.patch("bossctl.doctor._quarantine", side_effect=wrapped):
            doctor.repair(confirm=True, network=False)
        for thread in thread_holder: thread.join(2)
        self.assertEqual(blocked_while_replacing, [True])
        self.assertTrue(writer_acquired.is_set())

    def test_explicit_confirmed_auth_model_test_and_fetch_reconciliation_is_nondestructive(self):
        remote = self.root / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "remote", "add", "origin", str(remote)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "push", "-q", "origin", "main"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "update-ref", "-d", "refs/remotes/origin/main"], check=True)
        auth_source = self.root / "source-auth.json"
        write_json(auth_source, {"openai-codex": {"type": "oauth", "access": "fixture"},
                                 "other": {"secret": "must-not-copy"}})
        before_head = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"],
                                     text=True, capture_output=True, check=True).stdout.strip()
        before_status = subprocess.run(["git", "-C", str(self.repo), "status", "--porcelain"],
                                       text=True, capture_output=True, check=True).stdout
        result = doctor.repair(
            confirm=True, network=False, auth_source=str(auth_source),
            model_specs=["implement=openai-codex/gpt-5.4-mini"],
            test_specs=["p=python3 -m unittest"], fetch_projects=["p"],
        )
        actions = {action["action"] for action in result["applied"]}
        self.assertTrue({"reconcile-authentication", "reconcile-model", "reconcile-test-command",
                         "reconcile-clone-freshness"} <= actions)
        auth = json.loads((self.home / "pi" / "auth.json").read_text())
        self.assertEqual(set(auth), {"openai-codex"})
        self.assertEqual((self.home / "pi" / "auth.json").stat().st_mode & 0o777, 0o600)
        dispatch_data = json.loads((self.home / "dispatch.json").read_text())
        self.assertEqual(dispatch_data["models"]["implement"], "openai-codex/gpt-5.4-mini")
        project = json.loads((self.home / "projects.json").read_text())["projects"]["p"]
        self.assertEqual(project["test_cmd"], "python3 -m unittest")
        remote_sha = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "refs/remotes/origin/main"],
                                    text=True, capture_output=True, check=True).stdout.strip()
        self.assertEqual(remote_sha, before_head)
        self.assertEqual(subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"],
                                        text=True, capture_output=True, check=True).stdout.strip(), before_head)
        self.assertEqual(subprocess.run(["git", "-C", str(self.repo), "status", "--porcelain"],
                                        text=True, capture_output=True, check=True).stdout, before_status)

    def test_confirmed_orphan_worktree_moves_intact_to_quarantine_and_keeps_branch(self):
        project = json.loads((self.home / "projects.json").read_text())["projects"]["p"]
        wt = worktree.create(project, "p-orphan")
        marker = wt / "uncommitted.txt"; marker.write_text("preserve this exact work\n")
        report = doctor.audit(network=False)
        self.assertTrue(any(action["action"] == "quarantine-orphan-worktree" for action in report["repairs"]))
        result = doctor.repair(confirm=True, network=False)
        action = next(action for action in result["applied"] if action["action"] == "quarantine-orphan-worktree")
        destination = Path(action["result"]["path"])
        self.assertFalse(wt.exists())
        self.assertEqual((destination / "uncommitted.txt").read_text(), "preserve this exact work\n")
        branch = subprocess.run(["git", "-C", str(self.repo), "show-ref", "--verify", "--quiet",
                                 "refs/heads/boss/p-orphan"])
        self.assertEqual(branch.returncode, 0)

    def test_confirmed_detached_worktree_rebuild_retains_exact_source_and_payload(self):
        project, wt, _item, _admin = self.detached_item("p-detached")
        expected_payload = worktree.payload_manifest(wt)
        before_files = {name: (wt / name).read_bytes()
                        for name in ("README.md", "untracked.txt", "staged-new.txt")}
        report = doctor.audit(network=False)
        action = next(value for value in report["repairs"]
                      if value["action"] == "rebuild-worktree-registration")
        self.assertFalse(worktree.recovery_journal_path("p", "p-detached").exists())
        self.assertEqual(worktree.payload_manifest(wt), expected_payload)

        result = doctor.repair(confirm=True, network=False)
        applied = next(value for value in result["applied"]
                       if value["action"] == "rebuild-worktree-registration")
        source = Path(applied["result"]["preserved_source"])
        self.assertEqual(applied["result"]["branch_sha"], action["evidence"]["branch_sha"])
        self.assertTrue(source.is_dir())
        self.assertTrue((source / ".git").is_file())
        self.assertEqual(worktree.entry_manifest(source), action["evidence"]["source_manifest"])
        self.assertEqual(worktree.payload_manifest(wt), expected_payload)
        self.assertEqual({name: (wt / name).read_bytes() for name in before_files}, before_files)
        self.assertEqual(os.readlink(wt / "payload-link"), "untracked.txt")
        self.assertEqual(subprocess.run(["git", "-C", str(wt), "branch", "--show-current"],
                                        text=True, capture_output=True, check=True).stdout.strip(),
                         "boss/p-detached")
        status = subprocess.run(["git", "-C", str(wt), "status", "--porcelain"],
                                text=True, capture_output=True, check=True).stdout
        self.assertIn(" M README.md", status)
        self.assertIn("?? staged-new.txt", status)
        self.assertIn("?? untracked.txt", status)
        self.assertEqual(worktree.registration_recovery_status("p", "p-detached")["state"], "complete")

    def test_detached_worktree_recovery_resumes_after_process_death_at_every_phase(self):
        phases = ("journaled", "source-preserved", "registered",
                  "payload-restored", "reattached", "complete")
        for phase in phases:
            with self.subTest(phase=phase):
                work_id = f"p-crash-{phase}"
                _project, wt, _item, _admin = self.detached_item(work_id)
                expected_payload = worktree.payload_manifest(wt)

                def crash_here(observed, _transaction):
                    if observed == phase:
                        raise RuntimeError(f"injected death after {phase}")

                with mock.patch("bossctl.worktree._recovery_checkpoint", side_effect=crash_here):
                    with self.assertRaisesRegex(RuntimeError, "injected death"):
                        doctor.repair(confirm=True, network=False)

                interrupted = worktree.registration_recovery_status("p", work_id)
                self.assertNotEqual(interrupted["state"], "corrupt")
                followup = doctor.audit(network=False)
                resumable = [value for value in followup["repairs"]
                             if value["action"] == "resume-worktree-registration"
                             and value["target"]["work_id"] == work_id]
                if phase == "complete":
                    self.assertEqual(interrupted["state"], "complete")
                    self.assertEqual(resumable, [])
                else:
                    self.assertEqual(len(resumable), 1)
                    doctor.repair(confirm=True, network=False)
                completed = worktree.registration_recovery_status("p", work_id)
                self.assertEqual(completed["state"], "complete")
                self.assertTrue(Path(completed["paths"]["source"]).is_dir())
                self.assertEqual(worktree.payload_manifest(wt), expected_payload)
                self.assertEqual(subprocess.run(
                    ["git", "-C", str(wt), "branch", "--show-current"],
                    text=True, capture_output=True, check=True).stdout.strip(),
                    f"boss/{work_id}")

    def test_detached_worktree_recovery_refuses_changed_branch_and_retains_source(self):
        project, wt, _item, _admin = self.detached_item("p-moved-branch")
        def crash_after_source(phase, _transaction):
            if phase == "source-preserved":
                raise RuntimeError("injected death")
        with mock.patch("bossctl.worktree._recovery_checkpoint", side_effect=crash_after_source):
            with self.assertRaises(RuntimeError):
                doctor.repair(confirm=True, network=False)
        interrupted = worktree.registration_recovery_status("p", "p-moved-branch")
        source = Path(interrupted["paths"]["source"])
        before = worktree.entry_manifest(source)
        old_sha = interrupted["transaction"]["branch_sha"]
        tree = subprocess.run(["git", "-C", str(self.repo), "rev-parse", f"{old_sha}^{{tree}}"],
                              text=True, capture_output=True, check=True).stdout.strip()
        moved_sha = subprocess.run(["git", "-C", str(self.repo), "commit-tree", tree, "-p", old_sha,
                                    "-m", "external branch movement"],
                                   text=True, capture_output=True, check=True).stdout.strip()
        subprocess.run(["git", "-C", str(self.repo), "update-ref",
                        "refs/heads/boss/p-moved-branch", moved_sha, old_sha], check=True)
        result = doctor.repair(confirm=True, network=False)
        skipped = [value for value in result["skipped"]
                   if value["action"] == "resume-worktree-registration"
                   and value["target"]["work_id"] == "p-moved-branch"]
        self.assertEqual(len(skipped), 1)
        self.assertIn("branch moved", skipped[0]["skip"])
        self.assertTrue(source.is_dir())
        self.assertFalse(wt.exists())
        self.assertEqual(worktree.entry_manifest(source), before)

    def test_detached_worktree_is_never_moved_while_agent_liveness_is_live_or_unknown(self):
        for state in ("live", "unknown"):
            with self.subTest(state=state):
                work_id = f"p-agent-{state}"
                _project, wt, _item, _admin = self.detached_item(
                    work_id, session={"agent_name": f"agent-{state}"})
                before = worktree.entry_manifest(wt)
                with mock.patch("bossctl.doctor._herdr_agent", return_value={"state": state}):
                    report = doctor.audit(network=False)
                self.assertFalse(any(value["action"] == "rebuild-worktree-registration"
                                     and value["target"]["work_id"] == work_id
                                     for value in report["repairs"]))
                self.assertTrue(wt.is_dir())
                self.assertEqual(worktree.entry_manifest(wt), before)
                self.assertFalse(worktree.recovery_journal_path("p", work_id).exists())

    def test_symlinked_worktree_root_is_never_traversed_or_repaired(self):
        work_id = "p-root-escape"
        outside = self.root / "outside-worktrees"
        canonical_outside = outside / "p" / work_id; canonical_outside.mkdir(parents=True)
        marker = canonical_outside / "private-marker"; marker.write_text("do not inspect or move\n")
        (self.home / "worktrees").symlink_to(outside, target_is_directory=True)
        item_dir = self.home / "work" / work_id; item_dir.mkdir(parents=True)
        lexical = self.home / "worktrees" / "p" / work_id
        write_json(item_dir / "item.json", {
            "id": work_id, "project": "p", "status": "paused", "phase": "recovery-required",
            "created": "2026-01-01T00:00:00Z", "updated": "2026-01-01T00:00:00Z",
            "revision": 0, "session": None, "history": [], "attempts": 1,
            "agent_launches": [], "worktree": str(lexical),
            "controls": {"paused": True, "events": [], "pending": []},
        })
        original_git = doctor._git
        def guarded(repo, *args, **kwargs):
            self.assertNotEqual(Path(repo), lexical, "doctor followed a symlinked worktree root")
            return original_git(repo, *args, **kwargs)
        before = (marker.read_bytes(), marker.stat().st_mtime_ns)
        with mock.patch("bossctl.doctor._git", side_effect=guarded):
            report = doctor.audit(network=False)
        self.assertEqual((marker.read_bytes(), marker.stat().st_mtime_ns), before)
        self.assertTrue(any(check["id"] == "worktree-root" and check["status"] == "error"
                            for check in report["checks"]))
        self.assertFalse(any(value["action"] in {"rebuild-worktree-registration",
                                                 "resume-worktree-registration",
                                                 "quarantine-orphan-worktree"}
                             for value in report["repairs"]))


if __name__ == "__main__":
    unittest.main()
