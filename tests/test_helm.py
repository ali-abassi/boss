try:
    import _gitenv  # noqa: F401  (git hygiene for temp repos)
except ImportError:
    from tests import _gitenv  # noqa: F401
import json, os, shlex, subprocess, tempfile, time, unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HELM = [str(REPO / "bin" / "helm")]


class HelmTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        self.proj = self.tmp / "proj"
        self.proj.mkdir()
        g = lambda *a: subprocess.run(["git", "-C", str(self.proj), *a], check=True, capture_output=True, text=True)
        g("init", "-q", "-b", "main"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
        (self.proj / "README.md").write_text("hi\n"); g("add", "-A"); g("commit", "-qm", "init")
        self.env = {**os.environ, "HELM_HOME": str(self.home), "HELM_PIW": str(REPO / "tests" / "fake_piw.py")}

    def helm(self, *args, mode="ok", check=True):
        r = subprocess.run(HELM + list(args), env={**self.env, "FAKE_PIW_MODE": mode}, text=True, capture_output=True)
        if check and r.returncode != 0:
            self.fail(f"helm {' '.join(args)} failed ({r.returncode}):\n{r.stdout}\n{r.stderr}")
        return r

    def add(self, **kw):
        args = ["add", str(self.proj), "--id", "p", "--test", "true"]
        for k, v in kw.items():
            args += [f"--{k}", str(v)]
        self.helm(*args)

    def task(self, text="add a file", **kw):
        args = ["task", "p", text, "--json"]
        for k, v in kw.items():
            args += [f"--{k}", str(v)]
        return json.loads(self.helm(*args).stdout)

    def show(self, wid):
        return json.loads(self.helm("show", wid, "--json").stdout)

    def test_local_only_ready_then_promote_ff(self):
        self.add(mode="local-only", authority=3)
        it = self.task()
        self.assertEqual(it["dispatch"]["graph"], "local-only")
        self.helm("run-once")
        it = self.show(it["id"])
        self.assertEqual(it["status"], "ready", it)
        self.assertTrue((self.home / "work" / it["id"] / "steps.yaml").exists())
        # promotion requires --confirm
        r = self.helm("promote", it["id"], check=False)
        self.assertEqual(r.returncode, 1)
        self.helm("promote", it["id"], "--confirm")
        log = subprocess.run(["git", "-C", str(self.proj), "log", "--oneline"], capture_output=True, text=True).stdout
        self.assertIn("helm: fake change", log)
        self.assertEqual(self.show(it["id"])["status"], "merged")
        self.assertFalse((self.home / "worktrees" / "p" / it["id"]).exists())

    def test_authority_gates_promotion(self):
        self.add(mode="local-only", authority=1)
        it = self.task(); self.helm("run-once")
        r = self.helm("promote", it["id"], "--confirm", check=False)
        self.assertEqual(r.returncode, 1); self.assertIn("authority 1 < 3", r.stderr)
        r = self.helm("task", "p", "x", "--kind", "ship", check=False)  # fine at authority 1
        self.assertEqual(r.returncode, 0)
        self.helm("set", "p", "--authority", "0")
        r = self.helm("task", "p", "x", check=False)
        self.assertEqual(r.returncode, 1); self.assertIn("authority 0", r.stderr)

    def test_disjoint_explicit_scope_is_not_misclassified_by_protected_patterns(self):
        self.add(mode="local-only", authority=1)
        ordinary = self.task("change one implementation file", scope="task.py")
        self.assertEqual(ordinary["scope"], {"paths": ["task.py"], "claim": "paths"})
        self.assertEqual(ordinary["rigor"]["level"], "standard")
        self.assertEqual(ordinary["dispatch"]["graph"], "local-only")
        protected = self.task("change workflow", scope=".github/workflows/release.yml")
        self.assertEqual(protected["scope"]["claim"], "global")
        self.assertEqual(protected["rigor"]["level"], "high-risk")
        self.assertEqual(protected["dispatch"]["graph"], "high-assurance")

    def test_ask_goes_to_inbox_and_respond_requeues_with_guidance(self):
        self.add(mode="local-only")
        it = self.task()
        self.helm("run-once", mode="ask")
        it = self.show(it["id"])
        self.assertEqual(it["status"], "needs-you"); self.assertEqual(it["attempts"], 0)
        self.assertEqual(it["ask"]["question"], "Which auth provider?")
        self.assertIn("[question]", self.helm("inbox").stdout)
        self.helm("respond", it["id"], "use oauth")
        self.assertEqual(self.show(it["id"])["status"], "queued")
        self.helm("run-once", mode="ok")
        it = self.show(it["id"])
        self.assertEqual(it["status"], "ready")
        brief = (self.home / "work" / it["id"] / "brief.md").read_text()
        self.assertIn("use oauth", brief)
        wt = self.home / "worktrees" / "p" / it["id"]
        self.assertIn("guided", (wt / "helm-change.txt").read_text())

    def test_failures_requeue_with_notes_then_exhaust(self):
        self.add(mode="local-only")
        it = self.task(**{"max-attempts": 2})
        self.helm("run-once", mode="fail", check=False)
        it = self.show(it["id"])
        self.assertEqual(it["status"], "queued"); self.assertEqual(it["attempts"], 1)
        self.assertIn("expected 2 got 3", it["failure_notes"][0]["notes"])
        self.helm("run-once", mode="fail", check=False)
        it = self.show(it["id"])
        self.assertEqual(it["status"], "failed")
        brief = (self.home / "work" / it["id"] / "brief.md").read_text()
        self.assertIn("Earlier attempts failed", brief)
        self.helm("retry", it["id"]); self.helm("run-once", mode="ok")
        self.assertEqual(self.show(it["id"])["status"], "ready")

    def test_dirty_ship_and_mutating_scout_fail_without_losing_work(self):
        self.add(mode="local-only")
        dirty = self.task("leave an unreviewed file [fake:dirty]", **{"max-attempts": 1})
        self.helm("run-once", check=False)
        item = self.show(dirty["id"]); self.assertEqual(item["status"], "failed")
        wt = self.home / "worktrees" / "p" / dirty["id"]
        self.assertTrue((wt / "unreviewed.txt").exists(), "failed work must be preserved")
        scout = self.task("inspect only [fake:scout-write]", kind="scout")
        self.helm("run-once", check=False)
        item = self.show(scout["id"]); self.assertNotEqual(item["status"], "done")
        self.assertTrue((self.home / "worktrees" / "p" / scout["id"] / "scout-wrote.txt").exists())

    def test_cancel_discard_is_authorized_only_after_legality_and_quarantines_work(self):
        self.add(mode="local-only")
        item = self.task("leave an unreviewed file [fake:dirty]", **{"max-attempts": 1})
        self.helm("run-once", check=False)
        wt = self.home / "worktrees" / "p" / item["id"]
        branch = f"firstmate/{item['id']}"
        branch_before = subprocess.run(["git", "-C", str(self.proj), "rev-parse", branch],
                                       check=True, text=True, capture_output=True).stdout.strip()
        refused = self.helm("cancel", item["id"], check=False)
        self.assertNotEqual(refused.returncode, 0)
        self.assertTrue((wt / "unreviewed.txt").is_file())
        self.helm("cancel", item["id"], "--discard")
        stored = self.show(item["id"])
        receipt = stored["cancellation"]["quarantine_result"]
        quarantined = Path(receipt["path"])
        self.assertEqual(stored["status"], "cancelled")
        self.assertFalse(wt.exists()); self.assertTrue((quarantined / "unreviewed.txt").is_file())
        branch_after = subprocess.run(["git", "-C", str(self.proj), "rev-parse", branch],
                                      check=True, text=True, capture_output=True).stdout.strip()
        self.assertEqual(branch_after, branch_before, "non-destructive cancellation must retain the branch ref")

    def test_cancel_discard_never_touches_a_running_item(self):
        self.add(mode="local-only")
        item = self.task("leave an unreviewed file [fake:dirty]", **{"max-attempts": 1})
        self.helm("run-once", check=False)
        wt = self.home / "worktrees" / "p" / item["id"]
        state_path = self.home / "work" / item["id"] / "item.json"
        state = json.loads(state_path.read_text()); state["status"] = "running"; state["phase"] = "implementing"
        state_path.write_text(json.dumps(state))
        refused = self.helm("cancel", item["id"], "--discard", check=False)
        self.assertNotEqual(refused.returncode, 0)
        self.assertTrue((wt / "unreviewed.txt").is_file())
        self.assertFalse((self.home / "quarantine" / "worktrees" / "p").exists())

    def test_budget_threshold_pauses_and_preserves_checkpoint(self):
        self.add(mode="local-only")
        item = self.task("bounded work", **{"max-cost": 0.001})
        self.helm("run-once")
        item = self.show(item["id"])
        self.assertEqual(item["status"], "paused"); self.assertIn("budget", item["ask"]["question"].lower())
        self.assertTrue((self.home / "worktrees" / "p" / item["id"]).exists())

    def test_token_budget_exhaustion_pauses_without_delivery(self):
        self.add(mode="local-only")
        item = self.task("token bounded work", **{"max-tokens": 100})
        self.helm("run-once")
        item = self.show(item["id"])
        self.assertEqual(item["status"], "paused")
        self.assertEqual(item["checkpoint"]["usage"]["tokens"], 123)
        self.assertIn("123>100", item["ask"]["context"])
        refused = self.helm("resume", item["id"], check=False)
        self.assertNotEqual(refused.returncode, 0); self.assertIn("exhausted budget", refused.stderr)
        self.helm("budget", item["id"], "--tokens", "300")
        self.helm("resume", item["id"])
        self.assertEqual(self.show(item["id"])["status"], "queued")

    def test_active_headless_pause_stops_runner_and_preserves_recoverable_worktree(self):
        self.add(mode="local-only")
        item = self.task("slow bounded work")
        env = {**self.env, "FAKE_PIW_MODE": "ok", "FAKE_PIW_SECONDS": "5"}
        proc = subprocess.Popen(HELM + ["run-once"], env=env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.time() + 10
        while time.time() < deadline and self.show(item["id"])["status"] != "running":
            time.sleep(0.05)
        self.helm("pause", item["id"])
        stdout, stderr = proc.communicate(timeout=15)
        self.assertEqual(proc.returncode, 0, stdout + stderr)
        paused = self.show(item["id"])
        self.assertEqual(paused["status"], "paused")
        self.assertIsNotNone(paused["checkpoint"])
        self.assertTrue((self.home / "worktrees" / "p" / item["id"]).exists())
        self.helm("resume", item["id"]); self.helm("run-once")
        self.assertEqual(self.show(item["id"])["status"], "ready")

    def test_headless_review_evidence_must_name_the_exact_sha(self):
        self.add(mode="direct-pr", authority=1)
        for value in ("omit", "wrong"):
            item = self.task(f"review evidence {value}", **{"max-attempts": 1})
            r = subprocess.run(HELM + ["run-once"], env={**self.env, "FAKE_PIW_REVIEW_SHA": value},
                               text=True, capture_output=True)
            self.assertNotEqual(self.show(item["id"])["status"], "ready", r.stdout + r.stderr)
            self.assertIn("exact-sha-review", self.show(item["id"])["failure_notes"][-1]["notes"])

    def test_uncommitted_mutation_invalidates_ready_promotion(self):
        self.add(mode="local-only", authority=3)
        item = self.task(); self.helm("run-once")
        wt = self.home / "worktrees" / "p" / item["id"]
        (wt / "after-review.txt").write_text("not reviewed")
        r = self.helm("promote", item["id"], "--confirm", check=False)
        self.assertNotEqual(r.returncode, 0); self.assertIn("mutated after verification", r.stderr)
        self.helm("inspect", item["id"])
        inspected = self.show(item["id"])
        self.assertEqual(inspected["status"], "paused")

    def test_later_commit_invalidates_exact_sha_reviews(self):
        self.add(mode="local-only", authority=3)
        item = self.task("change auth handling")
        self.helm("run-once"); item = self.show(item["id"])
        self.assertEqual(len(item["reviews"]), 2)
        wt = self.home / "worktrees" / "p" / item["id"]
        (wt / "later.txt").write_text("later")
        subprocess.run(["git", "-C", str(wt), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(wt), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "later"], check=True)
        r = self.helm("promote", item["id"], "--confirm", check=False)
        self.assertNotEqual(r.returncode, 0); self.assertIn("mutated after verification", r.stderr)

    def test_moved_base_after_review_refuses_promotion(self):
        self.add(mode="local-only", authority=3)
        item = self.task("review against current base"); self.helm("run-once")
        (self.proj / "base-moved.txt").write_text("new base\n")
        subprocess.run(["git", "-C", str(self.proj), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.proj), "commit", "-qm", "move base"], check=True)
        result = self.helm("promote", item["id"], "--confirm", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("base moved after verification", result.stderr)

    def test_base_move_during_final_test_invalidates_the_entire_pipeline(self):
        self.add(mode="local-only")
        test_cmd = (f"git -C {shlex.quote(str(self.proj))} commit --allow-empty -m base-moved-during-test >/dev/null")
        self.helm("set", "p", "--test", test_cmd)
        item = self.task("bind verification to one base")
        self.helm("run-once", check=False)
        shown = self.show(item["id"])
        self.assertNotIn(shown["status"], ("ready", "pr-open", "merged"))
        self.assertIn("base-moved", shown["checkpoint"]["failed_ids"])

    def test_confirmed_doctor_quarantine_then_recover_resumes_preserved_item(self):
        self.add(mode="local-only")
        item = self.task("survive daemon restart")
        path = self.home / "work" / item["id"] / "item.json"
        data = json.loads(path.read_text()); data["status"] = "running"; data["phase"] = "implementing"
        data["lease"] = {"owner": "dead-daemon", "pid": 999_999_999}; path.write_text(json.dumps(data))
        (self.home / "scope-claims.json").write_text(json.dumps({"version": 1, "claims": [{
            "project": "p", "work_id": item["id"], "paths": ["unknown"], "owner": "dead-daemon", "pid": 999_999_999}]}))
        self.helm("doctor", "--repair", "--confirm", "--offline", check=False)
        quarantined = self.show(item["id"]); self.assertEqual(quarantined["status"], "paused")
        self.helm("recover", item["id"], "--request-id", "restart-recovery")
        self.helm("run-once")
        self.assertEqual(self.show(item["id"])["status"], "ready")

    def test_scout_writes_report_and_cleans_up(self):
        self.add(mode="high-assurance", authority=0)
        it = self.task("why is login flaky?", kind="scout")
        self.assertEqual(it["dispatch"]["graph"], "scout")
        self.helm("run-once", mode="scout")
        it = self.show(it["id"])
        self.assertEqual(it["status"], "done")
        self.assertTrue((self.home / "work" / it["id"] / "report.md").exists())
        self.assertFalse((self.home / "worktrees" / "p" / it["id"]).exists())

    def test_observed_sensitive_scope_escalates_and_reruns_stronger_graph(self):
        self.add(mode="local-only")
        item = self.task("update generated metadata [fake:sensitive]")
        self.helm("run-once")
        midway = self.show(item["id"])
        self.assertEqual(midway["status"], "queued"); self.assertEqual(midway["rigor"]["level"], "high-risk")
        self.helm("run-once")
        item = self.show(item["id"])
        self.assertEqual(item["status"], "ready"); self.assertEqual(item["dispatch"]["graph"], "high-assurance")
        self.assertEqual(len(item["reviews"]), 2)

    def test_dispatch_labels_pick_models_and_templates_render(self):
        self.add(mode="high-assurance", authority=1)
        it = self.task("big refactor", labels="hard")
        self.assertEqual(it["dispatch"]["rule"], "hard")
        self.assertEqual(it["dispatch"]["thinking"]["implement"], "high")
        self.helm("run-once")
        steps = (self.home / "work" / it["id"] / "steps.yaml").read_text()
        self.assertNotIn("@{", steps)
        self.assertIn("thinking: high", steps)
        self.assertIn("review_adversarial", steps)
        # high-assurance at authority 1 → ready, not PR
        self.assertEqual(self.show(it["id"])["status"], "ready")

    def test_per_project_concurrency_and_daemon_drain(self):
        self.add(mode="local-only")
        a = self.task("one"); b = self.task("two")
        self.helm("daemon", "--interval", "0", "--once-idle", "1")
        self.assertEqual({self.show(a["id"])["status"], self.show(b["id"])["status"]}, {"ready"})
        hist = self.show(b["id"])["history"]
        self.assertEqual(hist[0]["to"], "running")

    def test_stale_lease_is_preserved_and_surfaced_without_automatic_relaunch(self):
        self.add(mode="local-only")
        it = self.task()
        p = self.home / "work" / it["id"] / "item.json"
        d = json.loads(p.read_text()); d["status"] = "running"; d["lease"] = {"owner": "ghost", "pid": 999999}
        p.write_text(json.dumps(d))
        self.helm("run-once")                      # observes only; does not reclaim or relaunch
        it = self.show(it["id"])
        self.assertEqual(it["status"], "running")
        wakes = json.loads(self.helm("wakes", "--json").stdout)
        self.assertEqual(wakes[-1]["classification"], "dead")

    def test_add_autodetects_test_command(self):
        (self.proj / "package.json").write_text('{"scripts": {"test": "vitest"}}')
        r = self.helm("add", str(self.proj), "--id", "p", "--json")
        self.assertEqual(json.loads(r.stdout)["test_cmd"], "npm test")
        self.assertIn("detected", r.stderr)

    def test_explicit_project_id_cannot_escape_state_or_worktree_roots(self):
        result = self.helm("add", str(self.proj), "--id", "../../outside", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("project id must be", result.stderr)
        self.assertFalse((self.tmp / "outside").exists())

    def test_busy_worker_is_still_supervised_by_zero_token_watcher(self):
        self.add(mode="local-only")
        item = self.task("slow enough to wedge")
        env = {**self.env, "FAKE_PIW_SECONDS": "4", "HELM_SUPERVISOR_STALE_SECONDS": "1",
               "HELM_SUPERVISOR_WEDGE_OBSERVATIONS": "2"}
        run = subprocess.run(HELM + ["daemon", "--interval", "1", "--once-idle", "1"],
                             env=env, text=True, capture_output=True, timeout=20)
        self.assertEqual(run.returncode, 0, run.stderr)
        wakes = json.loads((self.home / "wakes.json").read_text())["events"]
        self.assertTrue(any(event["item_id"] == item["id"] and event["classification"] in ("stale", "wedged")
                            for event in wakes), wakes)

    def test_legacy_mode_writes_canonical_state_and_unavailable_external_gate_blocks_creation(self):
        self.add(mode="no-mistakes")
        project = json.loads(self.helm("projects", "--json").stdout)["p"]
        self.assertEqual(project["mode"], "high-assurance")
        self.helm("set", "p", "--gate", "no-mistakes")
        result = self.helm("task", "p", "must not start", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("gate provider no-mistakes is unavailable", result.stderr)
        self.assertFalse((self.home / "work").exists())

    def test_queued_native_item_rechecks_external_gate_before_any_execution_effect(self):
        self.add(mode="local-only")
        item = self.task("must remain queued work")
        self.helm("set", "p", "--gate", "no-mistakes")
        result = self.helm("run-once", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("gate provider no-mistakes is unavailable", result.stderr)
        self.assertFalse((self.home / "worktrees" / "p" / item["id"]).exists())
        self.assertEqual(self.show(item["id"])["status"], "failed")

    def test_up_status_down(self):
        self.add(mode="local-only")
        it = self.task()
        self.helm("up", "--interval", "1")
        self.assertIn("crew      ready", self.helm("status").stdout)
        deadline = time.time() + 30
        while time.time() < deadline and self.show(it["id"])["status"] != "ready":
            time.sleep(0.5)
        self.assertEqual(self.show(it["id"])["status"], "ready")
        self.assertIn("stopped", self.helm("down").stdout)
        self.assertIn("stopped", self.helm("status").stdout)

    def test_ignored_dependency_dirs_are_linked_into_worktree_not_committed(self):
        (self.proj / ".gitignore").write_text("node_modules/\n")                       # root ignores node_modules only
        (self.proj / "node_modules").mkdir(); (self.proj / "node_modules" / "dep.js").write_text("x")
        (self.proj / ".venv").mkdir(); (self.proj / ".venv" / "marker").write_text("x")
        (self.proj / ".venv" / ".gitignore").write_text("*\n")                           # like `python -m venv`
        (self.proj / "vendor").mkdir(); (self.proj / "vendor" / "tracked.txt").write_text("x")   # NOT ignored
        subprocess.run(["git", "-C", str(self.proj), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.proj), "commit", "-qm", "deps"], check=True)
        self.add(mode="local-only")
        it = self.task(); self.helm("run-once")
        wt = self.home / "worktrees" / "p" / it["id"]
        self.assertTrue((wt / "node_modules").is_symlink() and (wt / "node_modules" / "dep.js").exists())
        self.assertTrue((wt / ".venv").is_symlink())
        self.assertFalse((wt / "vendor").is_symlink(), "tracked dirs come from git, not links")
        files = subprocess.run(["git", "-C", str(wt), "diff", "--name-only", "main...HEAD"], capture_output=True, text=True).stdout
        self.assertNotIn("node_modules", files); self.assertNotIn(".venv", files)

    def test_pr_mode_without_origin_leaves_a_branch_instead_of_crashing(self):
        self.add(mode="high-assurance", authority=3)
        it = self.task(); self.helm("run-once")
        it = self.show(it["id"])
        self.assertEqual(it["status"], "ready")
        self.assertIn("no origin remote", it["history"][-1]["note"])


if __name__ == "__main__":
    unittest.main()
