"""Unit tests for the decisions helm makes without a model: dispatch, rendering, authority."""
try:
    import _gitenv  # noqa: F401  (git hygiene for temp repos)
except ImportError:
    from tests import _gitenv  # noqa: F401
import json, os, re, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


class Isolated(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        os.environ["HELM_HOME"] = str(self.home)

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)


class DispatchTests(Isolated):
    def test_first_matching_rule_wins_and_merges_models(self):
        from helm import dispatch
        proj = {"id": "api", "mode": "no-mistakes"}
        d = dispatch.resolve({"kind": "ship", "labels": ["cheap"]}, proj)
        self.assertEqual(d["rule"], "cheap")
        self.assertEqual(d["graph"], "high-assurance")                    # legacy state reads canonically
        self.assertEqual(d["models"]["implement"], "openai-codex/gpt-5.4-mini")
        self.assertEqual(d["models"]["review_correctness"], "openai-codex/gpt-5.6-sol")  # default kept

    def test_scout_ignores_mode(self):
        from helm import dispatch
        d = dispatch.resolve({"kind": "scout", "labels": []}, {"id": "x", "mode": "direct-pr"})
        self.assertEqual(d["graph"], "scout")

    def test_project_regex_and_missing_model_fail_closed(self):
        from helm import dispatch
        from helm.util import write_json
        from helm.paths import dispatch_file
        write_json(dispatch_file(), {"models": {"implement": "a/b"}, "thinking": {},
                                     "rules": [{"name": "only-web", "project": "web-.*"}]})
        with self.assertRaises(SystemExit):                                # no rule for api
            dispatch.resolve({"kind": "ship", "labels": []}, {"id": "api", "mode": "local-only"})
        d = dispatch.resolve({"kind": "ship", "labels": []}, {"id": "web-1", "mode": "local-only"})
        self.assertTrue(all(d["models"].values()), "missing phases inherit shipped defaults")


class RenderTests(Isolated):
    def test_every_graph_renders_with_no_placeholders_and_shell_intact(self):
        from helm import graphs, dispatch
        cfg = dispatch.load()
        proj = {"id": "p", "path": "/tmp/p", "base": "main", "test_cmd": "npm test", "protected_paths": [".github/*", "a b.txt"]}
        for g in ("local-only", "direct-pr", "high-assurance", "scout"):
            steps = graphs.render(g, self.home / g, cwd=Path("/tmp/wt"), branch="helm/x", project=proj,
                                  models=cfg["models"], thinking=cfg["thinking"], timeout=42)
            text = steps.read_text()
            self.assertNotIn("@{", text, g)
            self.assertIn("cwd: /tmp/wt", text)
            if g != "scout":
                self.assertIn("$(git", text)                                     # shell survives rendering
            if g in ("direct-pr", "high-assurance"):
                self.assertIn("$OUT", text)
                self.assertIn("'a b.txt'", text)                                 # protected globs are shell-quoted
                self.assertIn("npm test", text)

    def test_rendered_graphs_pass_the_bundled_runner_validate(self):
        from helm import graphs, dispatch
        os.environ.pop("HELM_PIW", None)
        cfg = dispatch.load()
        repo = self.home / "repo"; repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        proj = {"id": "p", "path": str(repo), "base": "main", "test_cmd": "true", "protected_paths": []}
        for g in ("local-only", "direct-pr", "high-assurance", "scout"):
            steps = graphs.render(g, self.home / g, cwd=repo, branch="helm/x", project=proj,
                                  models=cfg["models"], thinking=cfg["thinking"], timeout=42)
            r = subprocess.run([graphs.piw_bin(), "validate", str(steps)], text=True, capture_output=True)
            self.assertEqual(r.returncode, 0, f"{g}: {r.stdout}{r.stderr}")

    def test_legacy_graph_name_renders_the_canonical_template(self):
        from helm import graphs, dispatch
        cfg = dispatch.load()
        proj = {"id": "p", "path": "/tmp/p", "base": "main", "test_cmd": "true", "protected_paths": []}
        steps = graphs.render("no-mistakes", self.home / "legacy", cwd=Path("/tmp/wt"), branch="helm/x",
                              project=proj, models=cfg["models"], thinking=cfg["thinking"], timeout=42)
        self.assertIn("workflow: helm-high-assurance", steps.read_text())


class GateBoundaryTests(Isolated):
    def test_absent_external_gate_fails_closed_without_installing_or_faking_evidence(self):
        from unittest import mock
        from helm import gates
        with mock.patch("helm.no_mistakes.shutil.which", return_value=None):
            evidence = gates.no_mistakes_status(self.home)
        self.assertFalse(evidence["ready"])
        self.assertFalse(evidence["installed"])
        self.assertFalse(evidence["adapter_verified"])
        self.assertEqual(evidence["required_tag_sha"], gates.NO_MISTAKES_TAG_SHA)

    def test_unattested_executable_is_not_mistaken_for_the_pinned_product(self):
        from unittest import mock
        from helm import gates
        binary = self.home / "no-mistakes"; binary.write_text("not the release\n"); binary.chmod(0o755)
        with mock.patch("helm.no_mistakes.shutil.which", return_value=str(binary)):
            evidence = gates.no_mistakes_status(self.home)
        self.assertTrue(evidence["installed"])
        self.assertFalse(evidence["binary_provenance_verified"])
        self.assertFalse(evidence["adapter_verified"])
        self.assertFalse(evidence["ready"])


class ProtectedPathTests(unittest.TestCase):
    def check(self, files, globs):
        return subprocess.run([sys.executable, str(REPO / "helm" / "check_protected.py"), *globs],
                              input="\n".join(files), text=True, capture_output=True).returncode

    def test_blocks_protected_and_allows_others(self):
        self.assertEqual(self.check(["src/a.py"], [".github/workflows/*"]), 0)
        self.assertEqual(self.check(["src/a.py", ".github/workflows/ci.yml"], [".github/workflows/*"]), 1)
        self.assertEqual(self.check([], [".github/*"]), 0)


class WorktreeStatusTests(Isolated):
    def test_unstaged_first_porcelain_record_keeps_its_complete_path(self):
        from helm import worktree
        repo = self.home / "repo"; repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
        (repo / "task.py").write_text("before\n")
        subprocess.run(["git", "-C", str(repo), "add", "task.py"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True)
        (repo / "task.py").write_text("after\n")
        self.assertEqual(worktree.status_paths(repo), ["task.py"])

    @unittest.skipIf(os.name == "nt", "POSIX Git path identity test")
    def test_scope_preserves_a_literal_backslash_filename(self):
        from helm import scope
        literal = r"src\literal.py"
        self.assertEqual(scope.normalize([literal]), [literal])
        self.assertEqual(scope.escaped([literal], [literal]), [])
        self.assertEqual(scope.escaped([literal], ["src/literal.py"]), ["src/literal.py"])

    def test_checkpoint_excludes_managed_dependency_links_without_ambient_git_config(self):
        from helm import worktree
        from helm.util import sh
        repo = self.home / "checkpoint"; repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
        (repo / ".gitignore").write_text("node_modules/\n")
        (repo / "task.py").write_text("before\n")
        subprocess.run(["git", "-C", str(repo), "add", ".gitignore", "task.py"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
        dependency = self.home / "shared-node-modules"; dependency.mkdir()
        (dependency / "dep.js").write_text("module.exports = 1\n")
        (repo / "node_modules").symlink_to(dependency, target_is_directory=True)
        (repo / ".helm-ask.json").write_text('{"question":"keep me unstaged"}\n')
        (repo / "task.py").write_text("after\n")
        result = sh(["git", "-C", str(repo), "add", "-A", "--",
                     *worktree.checkpoint_pathspecs(repo)], check=False,
                    env={**os.environ, "GIT_CONFIG_COUNT": "0"})
        self.assertEqual(result.returncode, 0, result.stderr)
        staged = subprocess.run(["git", "-C", str(repo), "diff", "--cached", "--name-only"],
                                text=True, capture_output=True, check=True).stdout.splitlines()
        self.assertEqual(staged, ["task.py"])
        raw_status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain=v1"],
                                    text=True, capture_output=True, check=True).stdout
        self.assertIn("node_modules", raw_status)
        self.assertIn(".helm-ask.json", raw_status)

    def test_creating_a_sibling_never_prunes_a_retained_item_worktree(self):
        from helm import worktree
        repo = self.home / "multi"; repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
        (repo / "README.md").write_text("base\n")
        subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
        project = {"id": "p", "path": str(repo), "base": "main"}
        first = worktree.create(project, "p-first")
        second = worktree.create(project, "p-second")
        self.assertTrue(first.is_dir() and second.is_dir())
        self.assertEqual(worktree.branch_worktrees(project, "firstmate/p-first"), [first.resolve()])
        self.assertEqual(worktree.branch_worktrees(project, "firstmate/p-second"), [second.resolve()])


class DetectTests(unittest.TestCase):
    def repo(self, files):
        d = Path(tempfile.mkdtemp())
        for name, body in files.items():
            (d / name).parent.mkdir(parents=True, exist_ok=True); (d / name).write_text(body)
        return d

    def test_detects_common_stacks(self):
        from helm import detect
        cases = [
            ({"package.json": '{"scripts": {"test": "vitest"}}'}, "npm test"),
            ({"package.json": '{"scripts": {"test": "vitest"}}', "pnpm-lock.yaml": ""}, "pnpm test"),
            ({"package.json": '{"scripts": {"test": "echo \\"Error: no test specified\\""}}'}, None),
            ({"Cargo.toml": ""}, "cargo test"),
            ({"go.mod": ""}, "go test ./..."),
            ({"pyproject.toml": ""}, "python3 -m pytest -q"),
            ({"pyproject.toml": "", "uv.lock": ""}, "uv run pytest -q"),
            ({"Makefile": "build:\n\techo\ntest:\n\tpytest\n"}, "make test"),
            ({"README.md": ""}, None),
        ]
        for files, want in cases:
            self.assertEqual(detect.test_command(self.repo(files)), want, files)


class PiExtensionTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("bun"), "bun not installed")
    def test_extension_self_test_passes(self):
        r = subprocess.run(["bun", str(REPO / ".pi" / "extensions" / "firstmate.ts")], text=True, capture_output=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("checks passed", r.stdout)

    def test_voice_contract_is_in_agents_md_too(self):
        agents = (REPO / "AGENTS.md").read_text()
        self.assertIn('"captain"', agents)
        self.assertIn("promote", agents)

    def test_captain_prompt_knows_the_actual_control_plane(self):
        agents = (REPO / "AGENTS.md").read_text()
        for truth in (
            "firstmate-captain/v2",
            "real persistent Pi agent",
            "own Herdr tab",
            "`/fleet` is the canonical portfolio view",
            "Active Herdr tabs are the drill-down view",
            "Independent items can run concurrently",
            "`direct-pr` adds one correctness review",
            "`high-assurance` adds fresh correctness and adversarial reviews",
            "zero model turns in healthy steady state",
            "Isn't that what `/fleet` does?",
            "Yes, exactly",
        ):
            self.assertIn(truth, agents)
        self.assertNotIn("I run the crew in the background", agents)

    def test_capability_questions_trigger_the_firstmate_skill(self):
        skill = (REPO / "SKILL.md").read_text()
        for trigger in ("what First Mate is or can do", "whether workers use Herdr", "/fleet", "/inbox"):
            self.assertIn(trigger, skill)
        self.assertIn("Do not search unrelated", skill)

    def test_prompt_eval_set_covers_twenty_representative_conversations(self):
        contract = (REPO / "docs" / "captain-prompt-contract.md").read_text()
        cases = re.findall(r"^\| FM-\d{2} ", contract, re.MULTILINE)
        self.assertEqual(len(cases), 20)


class NodeBudgetTests(Isolated):
    def test_node_budget_schema_and_non_model_metrics_fail_closed(self):
        from helm import work
        self.assertEqual(work.validate_node_budgets({"implement": {"tokens": 100, "seconds": 30}}),
                         {"implement": {"tokens": 100, "cost": None, "seconds": 30}})
        with self.assertRaises(SystemExit):
            work.validate_node_budgets({"verify": {"tokens": 1}})
        with self.assertRaises(SystemExit):
            work.validate_node_budgets({"mystery": {"seconds": 1}})
        with self.assertRaises(SystemExit):
            work.validate_node_budgets({"implement": {"cost": float("nan")}})

    def test_session_receipts_are_cumulative_per_node_and_never_double_counted(self):
        from helm import work
        usage = {}
        first = {"agent_session_id": "first"}
        work._record_node_session(usage, "implement", first, {}, started=False)
        self.assertEqual(usage["implement"]["receipts"], {})
        self.assertTrue(usage["implement"]["tokens_evidence_complete"])
        work._record_node_session(usage, "implement", first, {"tokens": 10, "cost": 0.1}, started=True)
        work._record_node_session(usage, "implement", first, {"tokens": 15, "cost": 0.2}, started=True)
        work._record_node_session(usage, "implement", {"agent_session_id": "second"},
                                  {"tokens": 5, "cost": 0.05}, started=True)
        self.assertEqual(usage["implement"]["tokens"], 20)
        self.assertAlmostEqual(usage["implement"]["cost"], 0.25)
        item = {"node_budgets": {"implement": {"tokens": 18, "cost": None, "seconds": None}},
                "node_usage": usage}
        self.assertIn("implement tokens budget exhausted", "; ".join(work.node_budget_blockers(item)))

    def test_invalid_node_usage_measurements_fail_closed(self):
        from helm import work
        usage = {"implement": work._new_node_usage()}
        identity = {"agent_session_id": "session-invalid"}
        work._record_node_session(usage, "implement", identity,
                                  {"tokens": float("nan"), "cost": -1}, started=True)
        state = usage["implement"]
        self.assertEqual(state["tokens"], 0)
        self.assertEqual(state["cost"], 0.0)
        self.assertFalse(state["tokens_evidence_complete"])
        self.assertFalse(state["cost_evidence_complete"])

    def test_malformed_persisted_budget_state_blocks_before_runtime_and_cannot_be_written(self):
        from unittest import mock
        from helm import work
        from helm.util import write_json
        base = {"attempts": 1, "runs": [{"attempt": 1}], "session": None, "agent_launches": [],
                "budgets": {"tokens": 100, "cost": None, "seconds": None},
                "usage": {"tokens": 0, "cost": 0.0, "seconds": 0.0,
                          "tokens_evidence_complete": True, "cost_evidence_complete": True},
                "node_budgets": {}, "node_usage": {}}
        cases = []
        nan_limit = json.loads(json.dumps(base)); nan_limit["budgets"]["tokens"] = float("nan")
        cases.append(nan_limit)
        null_evidence = json.loads(json.dumps(base)); null_evidence["usage"]["tokens_evidence_complete"] = None
        cases.append(null_evidence)
        nan_node = json.loads(json.dumps(base)); nan_node["budgets"]["tokens"] = None
        nan_node["node_budgets"] = {"implement": {"tokens": 100}}
        nan_node["node_usage"] = {"implement": {**work._new_node_usage(), "tokens": float("nan")}}
        cases.append(nan_node)
        seconds_mismatch = json.loads(json.dumps(base))
        seconds_mismatch["budgets"] = {"tokens": None, "cost": None, "seconds": 100}
        seconds_mismatch["usage"].update(seconds=0.0, seconds_receipts={"captured-execution": 1000.0})
        cases.append(seconds_mismatch)
        seconds_overflow = json.loads(json.dumps(base))
        seconds_overflow["budgets"] = {"tokens": None, "cost": None, "seconds": 100}
        seconds_overflow["usage"].update(seconds=0.0, seconds_receipts={"a": 1e308, "b": 1e308})
        cases.append(seconds_overflow)
        for corrupt in cases:
            self.assertIsNotNone(work.budget_state_error(corrupt), corrupt)
            self.assertTrue(work.budget_blockers(corrupt), corrupt)
            with self.assertRaises(SystemExit):
                work.validate_budget_state(corrupt)

        rejected = self.home / "nan.json"
        with self.assertRaises(ValueError):
            write_json(rejected, {"limit": float("nan")})
        self.assertFalse(rejected.exists())

        work_id = "malformed-budget"
        item_path = self.home / "work" / work_id / "item.json"; item_path.parent.mkdir(parents=True)
        persisted = {**seconds_overflow, "id": work_id, "project": "p", "status": "queued",
                     "phase": "queued", "revision": 0, "created": "2026-01-01T00:00:00Z",
                     "updated": "2026-01-01T00:00:00Z", "controls": {"paused": False}}
        item_path.write_text(json.dumps(persisted))
        before = item_path.read_bytes()
        with self.assertRaises(SystemExit):
            work.control.cas_update(work_id, lambda item: item["controls"].update(paused=True))
        self.assertEqual(item_path.read_bytes(), before)
        with mock.patch("helm.work._execute") as execute:
            with self.assertRaises(SystemExit):
                work.execute(persisted)
        execute.assert_not_called()

    def test_active_first_turn_waits_for_provider_receipt_but_settled_turn_fails_closed(self):
        from helm import work
        budgets = {"tokens": 100, "cost": 1.0}
        self.assertEqual(work._missing_settled_usage(
            budgets, {}, 0, "became unavailable"), [])
        self.assertEqual(work._missing_settled_usage(
            budgets, {}, 1, "became unavailable"),
            ["tokens usage evidence became unavailable", "cost usage evidence became unavailable"])

    def test_cli_node_budget_parser_rejects_duplicates_and_normalizes(self):
        from helm import cli
        self.assertEqual(cli._node_budget_args(["implement=100"], ["review_correctness=0.5"], ["verify=20"]),
                         {"implement": {"tokens": 100, "cost": None, "seconds": None},
                          "review_correctness": {"tokens": None, "cost": 0.5, "seconds": None},
                          "verify": {"tokens": None, "cost": None, "seconds": 20}})
        with self.assertRaises(SystemExit):
            cli._node_budget_args(["implement=10", "implement=20"], [], [])


class OwnPiHomeTests(Isolated):
    def test_first_mate_has_its_own_pi_home_and_inherits_nothing(self):
        src = self.home / "captain-pi"; src.mkdir()
        (src / "auth.json").write_text(json.dumps({"openai-codex": {"access": "tok"}, "anthropic": {"x": 1}}))
        (src / "AGENTS.md").write_text("# personal agent"); (src / "extensions").mkdir()
        os.environ["PI_CODING_AGENT_DIR"] = str(src); os.environ["HELM_IMPORT_PI_DIR"] = str(src)
        from helm.cli import _isolated_pi_home
        dst = _isolated_pi_home()
        self.assertFalse((dst / "AGENTS.md").exists()); self.assertFalse((dst / "extensions").exists())
        self.assertFalse((dst / "auth.json").exists(), "no login is inherited silently")
        settings = json.loads((dst / "settings.json").read_text())
        self.assertEqual((settings["defaultProvider"], settings["defaultModel"]), ("openai-codex", "gpt-5.6-sol"))
        self.assertNotIn("enabledModels", settings, "no model cage: the captain decides")
        (dst / "settings.json").write_text(json.dumps({"defaultModel": "mine", "defaultProvider": "x"}))
        _isolated_pi_home()                                   # second run keeps the captain's choice
        self.assertEqual(json.loads((dst / "settings.json").read_text())["defaultModel"], "mine")
        # --import-login copies only the Codex credential
        r = subprocess.run([str(REPO / "bin" / "helm"), "setup", "--import-login", "--json"],
                           env={**os.environ}, text=True, capture_output=True)
        auth = json.loads((dst / "auth.json").read_text())
        self.assertEqual(list(auth), ["openai-codex"])
        del os.environ["PI_CODING_AGENT_DIR"]; del os.environ["HELM_IMPORT_PI_DIR"]

    def test_every_helm_command_runs_in_the_first_mates_pi_home(self):
        r = subprocess.run([sys.executable, "-c", "import os,sys; sys.argv=['helm','status','--json']; sys.path.insert(0, sys.argv[0]); "
                            "from helm import cli; cli.main(['status','--json']); print('DIR='+os.environ['PI_CODING_AGENT_DIR'])"],
                           cwd=str(REPO), env={**os.environ, "HELM_HOME": str(self.home)}, text=True, capture_output=True)
        self.assertIn(f"DIR={self.home.resolve()}/pi", r.stdout)


class DispatchSetTests(Isolated):
    def test_captain_can_change_a_steps_model(self):
        r = subprocess.run([str(REPO / "bin" / "helm"), "dispatch", "--set", "implement=openai-codex/gpt-5.6-luna"],
                           env={**os.environ, "HELM_HOME": str(self.home)}, text=True, capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr); self.assertIn("implement=openai-codex/gpt-5.6-luna", r.stdout)
        from helm import dispatch
        self.assertEqual(dispatch.load()["models"]["implement"], "openai-codex/gpt-5.6-luna")
        r = subprocess.run([str(REPO / "bin" / "helm"), "dispatch", "--set", "bogus=x"],
                           env={**os.environ, "HELM_HOME": str(self.home)}, text=True, capture_output=True)
        self.assertEqual(r.returncode, 1)


class RuntimePackagingTests(unittest.TestCase):
    def test_full_runner_keeps_agent_workflows_input_and_public_resume_fixes(self):
        runner = REPO / "vendor" / "pi-graph" / "scripts" / "run_steps.py"
        runtime = REPO / ".venv" / "bin" / "python"
        python = str(runtime if runtime.is_file() else Path(sys.executable))
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); steps = root / "steps.yaml"
            steps.write_text("""version: 1
workflow: agent-workflows-compat
input:
  required: true
  description: immutable test input
steps:
  - id: fail
    cmd: printf 'candidate\\n'
    gate: 'false'
""")
            missing = subprocess.run([python, str(runner), str(steps)],
                                     cwd=root, text=True, capture_output=True)
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn("requires --input", missing.stderr)
            self.assertFalse((root / "runs").exists(), "missing input must not create a run")
            failed = subprocess.run([python, str(runner), str(steps), "--input", "x"],
                                    cwd=root, text=True, capture_output=True)
            self.assertEqual(failed.returncode, 1, failed.stdout + failed.stderr)
            self.assertIn("piw resume", failed.stderr)
            self.assertNotIn("python3 " + str(runner), failed.stderr)
        provenance = (REPO / "vendor" / "pi-graph" / "VENDOR.md").read_text()
        self.assertIn("Agent Workflows v0.2.0", provenance)
        self.assertIn("d2f84bb740d8e336a198145022a367acdf18824f", provenance)

    def test_direct_and_symlinked_launcher_use_a_receipt_capable_runtime(self):
        direct = subprocess.run([str(REPO / "bin" / "helm"), "--version"],
                                text=True, capture_output=True)
        self.assertEqual(direct.returncode, 0, direct.stderr)
        with tempfile.TemporaryDirectory() as raw:
            link = Path(raw) / "helm"; link.symlink_to(REPO / "bin" / "helm")
            linked = subprocess.run([str(link), "--version"], text=True, capture_output=True)
            self.assertEqual(linked.returncode, 0, linked.stderr)
            self.assertEqual(linked.stdout, direct.stdout)

    def test_launcher_prefers_the_installer_private_runtime(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); (root / "bin").mkdir(); (root / ".venv" / "bin").mkdir(parents=True)
            shutil.copy2(REPO / "bin" / "helm", root / "bin" / "helm")
            fake = root / ".venv" / "bin" / "python"
            fake.write_text("#!/bin/sh\n[ \"$1\" = -c ] && exit 0\nprintf 'private-runtime\\n'\n")
            fake.chmod(0o755)
            result = subprocess.run([str(root / "bin" / "helm"), "--version"],
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "private-runtime\n")

    def test_cryptographic_runtime_dependency_is_installed_in_ci_and_locally(self):
        requirements = (REPO / "vendor" / "pi-graph" / "requirements.txt").read_text()
        self.assertIn("cryptography>=42,<51", requirements)
        self.assertIn("vendor/pi-graph/requirements.txt", (REPO / "install.sh").read_text())
        self.assertIn("vendor/pi-graph/requirements.txt",
                      (REPO / ".github" / "workflows" / "tests.yml").read_text())
        syntax = subprocess.run(["sh", "-n", str(REPO / "bin" / "helm")],
                                text=True, capture_output=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)


if __name__ == "__main__":
    unittest.main()
