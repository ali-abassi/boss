"""Unit tests for the decisions bossctl makes without a model: dispatch, rendering, authority."""
try:
    import _gitenv  # noqa: F401  (git hygiene for temp repos)
except ImportError:
    from tests import _gitenv  # noqa: F401
import contextlib, io, json, os, re, shutil, subprocess, sys, tempfile, unittest
from unittest import mock
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


class Isolated(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        os.environ["BOSS_HOME"] = str(self.home)

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)


class DispatchTests(Isolated):
    def test_first_matching_rule_wins_and_merges_models(self):
        from bossctl import dispatch
        proj = {"id": "api", "mode": "no-mistakes"}
        d = dispatch.resolve({"kind": "ship", "labels": ["cheap"]}, proj)
        self.assertEqual(d["rule"], "cheap")
        self.assertEqual(d["graph"], "high-assurance")                    # legacy state reads canonically
        self.assertEqual(d["models"]["implement"], "openai-codex/gpt-5.4-mini")
        self.assertEqual(d["models"]["review_correctness"], "openai-codex/gpt-5.6-sol")  # default kept

    def test_scout_ignores_mode(self):
        from bossctl import dispatch
        d = dispatch.resolve({"kind": "scout", "labels": []}, {"id": "x", "mode": "direct-pr"})
        self.assertEqual(d["graph"], "scout")

    def test_project_regex_and_missing_model_fail_closed(self):
        from bossctl import dispatch
        from bossctl.util import write_json
        from bossctl.paths import dispatch_file
        write_json(dispatch_file(), {"models": {"implement": "a/b"}, "thinking": {},
                                     "rules": [{"name": "only-web", "project": "web-.*"}]})
        with self.assertRaises(SystemExit):                                # no rule for api
            dispatch.resolve({"kind": "ship", "labels": []}, {"id": "api", "mode": "local-only"})
        d = dispatch.resolve({"kind": "ship", "labels": []}, {"id": "web-1", "mode": "local-only"})
        self.assertTrue(all(d["models"].values()), "missing phases inherit shipped defaults")

    def test_load_never_leaks_a_mutable_reference_to_the_module_default(self):
        # Regression: `dispatch.load()` used to return the literal module-level DEFAULT
        # object when dispatch.json didn't exist yet. `cmd_dispatch --set` then mutated
        # it in place (`cfg["models"][phase] = model`), corrupting DEFAULT for the rest
        # of the process — invisible per one-shot CLI call, but real pollution across
        # test cases/BOSS_HOMEs sharing one long-running process (this discovered it via
        # `python -m unittest discover`, where it broke an unrelated, later-running test).
        from bossctl import dispatch
        original_implement = dispatch.DEFAULT["models"]["implement"]
        cfg = dispatch.load()  # dispatch.json does not exist yet in this Isolated fixture
        cfg["models"]["implement"] = "some-other-provider/mutated-model"
        self.assertEqual(dispatch.DEFAULT["models"]["implement"], original_implement,
                         "load() leaked a mutable reference to the shared DEFAULT dict")
        # A second, independent load() call must be unaffected by the first caller's mutation.
        fresh = dispatch.load()
        self.assertEqual(fresh["models"]["implement"], original_implement)

    def test_cross_provider_scout_rule_from_docs_example_resolves(self):
        # This mirrors the exact example in docs/cli.md's "Model dispatch" section:
        # routing `scout` to a non-Codex provider via a small dispatch.json rule.
        from bossctl import dispatch
        from bossctl.util import write_json
        from bossctl.paths import dispatch_file
        write_json(dispatch_file(), {
            "rules": [
                {"name": "scout-deepseek", "kind": "scout",
                 "models": {"scout": "baseten/deepseek-ai/DeepSeek-V4-Pro-0813"},
                 "thinking": {"scout": "high"}},
                {"name": "default-ship", "kind": "ship"},
            ]
        })
        d = dispatch.resolve({"kind": "scout", "labels": []}, {"id": "api", "mode": "local-only"})
        self.assertEqual(d["rule"], "scout-deepseek")
        self.assertEqual(d["models"]["scout"], "baseten/deepseek-ai/DeepSeek-V4-Pro-0813")
        # Other phases still inherit the shipped Codex default — only `scout` was overridden.
        self.assertEqual(d["models"]["implement"], "openai-codex/gpt-5.6-sol")
        ship = dispatch.resolve({"kind": "ship", "labels": []}, {"id": "api", "mode": "local-only"})
        self.assertEqual(ship["rule"], "default-ship")


class RenderTests(Isolated):
    def test_every_graph_renders_with_no_placeholders_and_shell_intact(self):
        from bossctl import graphs, dispatch
        cfg = dispatch.load()
        proj = {"id": "p", "path": "/tmp/p", "base": "main", "test_cmd": "npm test", "protected_paths": [".github/*", "a b.txt"]}
        for g in ("local-only", "direct-pr", "high-assurance", "scout"):
            steps = graphs.render(g, self.home / g, cwd=Path("/tmp/wt"), branch="bossctl/x", project=proj,
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
        from bossctl import graphs, dispatch
        os.environ.pop("BOSS_PIW", None)
        cfg = dispatch.load()
        repo = self.home / "repo"; repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        proj = {"id": "p", "path": str(repo), "base": "main", "test_cmd": "true", "protected_paths": []}
        for g in ("local-only", "direct-pr", "high-assurance", "scout"):
            steps = graphs.render(g, self.home / g, cwd=repo, branch="bossctl/x", project=proj,
                                  models=cfg["models"], thinking=cfg["thinking"], timeout=42)
            r = subprocess.run([graphs.piw_bin(), "validate", str(steps)], text=True, capture_output=True)
            self.assertEqual(r.returncode, 0, f"{g}: {r.stdout}{r.stderr}")

    def test_legacy_graph_name_renders_the_canonical_template(self):
        from bossctl import graphs, dispatch
        cfg = dispatch.load()
        proj = {"id": "p", "path": "/tmp/p", "base": "main", "test_cmd": "true", "protected_paths": []}
        steps = graphs.render("no-mistakes", self.home / "legacy", cwd=Path("/tmp/wt"), branch="bossctl/x",
                              project=proj, models=cfg["models"], thinking=cfg["thinking"], timeout=42)
        self.assertIn("workflow: bossctl-high-assurance", steps.read_text())


class GateBoundaryTests(Isolated):
    def test_absent_external_gate_fails_closed_without_installing_or_faking_evidence(self):
        from unittest import mock
        from bossctl import gates
        with mock.patch("bossctl.no_mistakes.shutil.which", return_value=None):
            evidence = gates.no_mistakes_status(self.home)
        self.assertFalse(evidence["ready"])
        self.assertFalse(evidence["installed"])
        self.assertFalse(evidence["adapter_verified"])
        self.assertEqual(evidence["required_tag_sha"], gates.NO_MISTAKES_TAG_SHA)

    def test_unattested_executable_is_not_mistaken_for_the_pinned_product(self):
        from unittest import mock
        from bossctl import gates
        binary = self.home / "no-mistakes"; binary.write_text("not the release\n"); binary.chmod(0o755)
        with mock.patch("bossctl.no_mistakes.shutil.which", return_value=str(binary)):
            evidence = gates.no_mistakes_status(self.home)
        self.assertTrue(evidence["installed"])
        self.assertFalse(evidence["binary_provenance_verified"])
        self.assertFalse(evidence["adapter_verified"])
        self.assertFalse(evidence["ready"])


class ProtectedPathTests(unittest.TestCase):
    def check(self, files, globs):
        return subprocess.run([sys.executable, str(REPO / "bossctl" / "check_protected.py"), *globs],
                              input="\n".join(files), text=True, capture_output=True).returncode

    def test_blocks_protected_and_allows_others(self):
        self.assertEqual(self.check(["src/a.py"], [".github/workflows/*"]), 0)
        self.assertEqual(self.check(["src/a.py", ".github/workflows/ci.yml"], [".github/workflows/*"]), 1)
        self.assertEqual(self.check([], [".github/*"]), 0)


class WorktreeStatusTests(Isolated):
    def test_unstaged_first_porcelain_record_keeps_its_complete_path(self):
        from bossctl import worktree
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
        from bossctl import scope
        literal = r"src\literal.py"
        self.assertEqual(scope.normalize([literal]), [literal])
        self.assertEqual(scope.escaped([literal], [literal]), [])
        self.assertEqual(scope.escaped([literal], ["src/literal.py"]), ["src/literal.py"])

    def test_checkpoint_excludes_managed_dependency_links_without_ambient_git_config(self):
        from bossctl import worktree
        from bossctl.util import sh
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
        (repo / ".boss-ask.json").write_text('{"question":"keep me unstaged"}\n')
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
        self.assertIn(".boss-ask.json", raw_status)

    def test_creating_a_sibling_never_prunes_a_retained_item_worktree(self):
        from bossctl import worktree
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
        self.assertEqual(worktree.branch_worktrees(project, "boss/p-first"), [first.resolve()])
        self.assertEqual(worktree.branch_worktrees(project, "boss/p-second"), [second.resolve()])


class DetectTests(unittest.TestCase):
    def repo(self, files):
        d = Path(tempfile.mkdtemp())
        for name, body in files.items():
            (d / name).parent.mkdir(parents=True, exist_ok=True); (d / name).write_text(body)
        return d

    def test_detects_common_stacks(self):
        from bossctl import detect
        cases = [
            ({"package.json": '{"scripts": {"test": "vitest"}}'}, "npm test"),
            ({"package.json": '{"scripts": {"test": "vitest"}}', "pnpm-lock.yaml": ""}, "pnpm test"),
            ({"package.json": '{"scripts": {"test": "echo \\"Error: no test specified\\""}}'}, None),
            ({"Cargo.toml": ""}, "cargo test"),
            ({"go.mod": ""}, "go test ./..."),
            ({"pyproject.toml": ""}, "python3 -m pytest -q"),
            ({"pyproject.toml": "", "uv.lock": ""}, "uv run pytest -q"),
            ({"pyproject.toml": "", "poetry.lock": ""}, "poetry run pytest -q"),
            ({"pyproject.toml": "", "mise.toml": "[env]\n_python = \"3.13\"\n[tasks]\npytest = \"pytest -q\"\n"}, "mise run pytest"),
            ({"Makefile": "build:\n\techo\ntest:\n\tpytest\n"}, "make test"),
            ({"justfile": "test:\n\tpytest -q\n"}, "just test"),
            ({"MODULE.bazel": ""}, "bazel test //..."),
            ({"package.json": '{"scripts": {"test": "vitest"}}',
              "pnpm-lock.yaml": "", "pnpm-workspace.yaml": ""}, "pnpm test"),
            ({"package.json": '{"scripts": {"test": "vitest"}}',
              "bun.lock": ""}, "bun test"),
            ({"README.md": ""}, None),
            ({"package.json": '{"scripts": {"test": "echo \\"Error: no test specified\\""}}'}, None),
        ]
        for files, want in cases:
            self.assertEqual(detect.test_command(self.repo(files)), want, files)


class PiExtensionTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("bun"), "bun not installed")
    def test_extension_self_test_passes(self):
        r = subprocess.run(["bun", str(REPO / ".pi" / "extensions" / "boss.ts")], text=True, capture_output=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("checks passed", r.stdout)

    def test_voice_contract_is_in_agents_md_too(self):
        agents = (REPO / "AGENTS.md").read_text()
        self.assertIn("chief operating officer (COO)", agents)
        self.assertIn("The user is **Boss**", agents)
        self.assertIn("Never call yourself Boss", agents)
        self.assertIn("promote", agents)

    def test_boss_prompt_knows_the_actual_control_plane(self):
        agents = (REPO / "AGENTS.md").read_text()
        for truth in (
            "boss-coo/v1",
            "real persistent Pi agent",
            "own Herdr tab",
            "`/ops` is the canonical portfolio view",
            "Active tabs are watchable",
            "Independent items can run concurrently",
            "`direct-pr` adds one correctness review",
            "`high-assurance` adds",
            "fresh correctness and adversarial reviews",
            "zero model turns in healthy steady state",
            "Isn't that what `/ops` does?",
            "Yes, exactly",
        ):
            self.assertIn(truth, agents)
        self.assertNotIn("I run the team in the background", agents)

    def test_list_models_cache_avoids_repeated_subprocess(self):
        from bossctl import dispatch
        # If pi is missing, the cache stores None and we don't have to shell out again.
        with mock.patch("bossctl.dispatch.shutil.which", return_value=None):
            dispatch._invalidate_list_models_cache()
            self.assertIsNone(dispatch._list_models())
            with mock.patch("bossctl.dispatch.subprocess") as sp:
                self.assertIsNone(dispatch._list_models())
                sp.run.assert_not_called()
        # When pi is present we still cache the parse.
        dispatch._invalidate_list_models_cache()
        fake = mock.MagicMock()
        fake.returncode = 0
        fake.stdout = "openai-codex    gpt-5.6-sol    1.0M    32.8K    no    yes\n"
        with mock.patch("bossctl.dispatch.shutil.which", return_value="/usr/bin/pi"), \
             mock.patch("bossctl.dispatch.subprocess.run", return_value=fake) as run:
            self.assertIn("openai-codex/gpt-5.6-sol", dispatch._list_models() or set())
            self.assertIn("openai-codex/gpt-5.6-sol", dispatch._list_models() or set())
            self.assertEqual(run.call_count, 1)

        skill = (REPO / "SKILL.md").read_text()
        for trigger in ("what BOSS or the COO can do", "whether workers use Herdr", "/ops", "/inbox"):
            self.assertIn(trigger, skill)
        self.assertIn("Do not search unrelated", skill)

    def test_prompt_eval_set_covers_twenty_representative_conversations(self):
        contract = (REPO / "docs" / "boss-prompt-contract.md").read_text()
        cases = re.findall(r"^\| BO-\d{2} ", contract, re.MULTILINE)
        self.assertEqual(len(cases), 20)

    def test_readme_clarifies_bossctl_is_for_diagnostics_only(self):
        readme = (REPO / "README.md").read_text()
        # The contract is: the COO inside pi-boss will not use the `bossctl` verb unless
        # asked, and the README must spell that out so new users do not see two vocabularies.
        self.assertIn("will not use that verb unless you ask", readme)
        self.assertIn("bossctl", readme)
        self.assertTrue(re.search(r"## What code enforces", readme))

    def test_extensions_doc_covers_every_slash_command(self):
        doc = (REPO / "docs" / "extensions.md").read_text()
        for slash in ("/ops", "/inbox", "/away", "/wake"):
            self.assertIn(slash, doc)
        self.assertIn("bossctl", doc)

    def test_cli_doc_explains_legacy_mode_alias(self):
        doc = (REPO / "docs" / "cli.md").read_text()
        self.assertIn("no-mistakes", doc)
        self.assertIn("LEGACY_HIGH_ASSURANCE", doc)
        self.assertIn("high-assurance", doc)

    def test_status_schema_doc_matches_the_real_work_summary_keys(self):
        # Locks docs/status-schema.md's claimed --summary key list to the actual
        # _WORK_SUMMARY_KEYS constant in cli.py, so the doc can't silently drift.
        from bossctl.cli import _WORK_SUMMARY_KEYS
        doc = (REPO / "docs" / "status-schema.md").read_text()
        for key in _WORK_SUMMARY_KEYS:
            self.assertIn(f"`{key}`", doc, f"docs/status-schema.md is missing summary key {key!r}")

    def test_status_schema_doc_matches_the_real_open_statuses(self):
        from bossctl.work import OPEN
        doc = (REPO / "docs" / "status-schema.md").read_text()
        for status in OPEN:
            self.assertIn(f"`{status}`", doc, f"docs/status-schema.md is missing OPEN status {status!r}")

    def test_doctor_doc_covers_the_severity_vocabulary(self):
        doc = (REPO / "docs" / "doctor.md").read_text()
        for severity in ("ok", "warning", "error", "unknown"):
            self.assertIn(severity, doc)
        self.assertIn("--repair", doc)
        self.assertIn("--confirm", doc)

    def test_capabilities_json_is_valid_and_covers_skill_md_commands(self):
        cap = json.loads((REPO / "capabilities.json").read_text())
        for key in ("intents", "commands", "prerequisites", "failure_modes"):
            self.assertIn(key, cap)
            self.assertTrue(cap[key], f"capabilities.json[{key!r}] must not be empty")
        # Every top-level bossctl verb mentioned in SKILL.md's command block must have a
        # matching entry in capabilities.json["commands"] so the two never silently diverge.
        skill = (REPO / "SKILL.md").read_text()
        skill_verbs = set(re.findall(r"^bossctl (\S+)", skill, re.MULTILINE))
        cap_verbs = {c["command"].split()[1] for c in cap["commands"]}
        missing = skill_verbs - cap_verbs
        self.assertFalse(missing, f"capabilities.json is missing SKILL.md verbs: {missing}")

    def test_capabilities_json_commands_include_the_new_mission_additions(self):
        cap = json.loads((REPO / "capabilities.json").read_text())
        verbs = {c["command"].split()[1] for c in cap["commands"]}
        for verb in ("comment", "diff", "logs"):
            self.assertIn(verb, verbs)

    def test_cli_doc_is_honest_about_codex_plan_tier_detection(self):
        # #18 from the review cannot be implemented (no public API for Codex plan tier);
        # this locks in that the doc says so explicitly instead of silently dropping it.
        doc = (REPO / "docs" / "cli.md").read_text()
        self.assertIn("does not detect or check your Codex plan", doc)
        self.assertIn("known, permanent limitation", doc)


class BoardRenderTests(Isolated):
    def test_cell_width_and_truncation_handle_wide_and_combining_text(self):
        from bossctl import board
        self.assertEqual(board.cell_width("BOSS"), 4)
        self.assertEqual(board.cell_width("界e\u0301"), 3)
        self.assertLessEqual(board.cell_width(board.fit_width("界" * 20, 11)), 11)
        self.assertEqual(board.cell_width(board.pad_width("界e\u0301", 8)), 8)
        self.assertEqual(board.pad_width("界" * 20, 7), "界界界 ")
        escaped = board.ascii_text("界 e\u0301 🚀 — done")
        self.assertEqual(escaped, r"\u754c e \U0001f680 - done")
        self.assertTrue(all(ord(char) < 128 for char in escaped))

    def test_maximum_content_and_plain_header_fit_the_requested_width(self):
        from unittest import mock
        from bossctl import board
        items = [{"status": "running" if n % 2 else "needs-you", "project": "超長-project-name",
                  "text": "🚀 investigate a deliberately long request " * 4,
                  "ask": {"question": "decision with wide glyphs 界界"}}
                 for n in range(12)]
        projects = {f"p{n}": {"id": f"project-{n}-界", "mode": "high-assurance", "authority": 3}
                    for n in range(8)}
        with mock.patch("bossctl.board.registry.load", return_value={"projects": projects}), \
             mock.patch("bossctl.board.work.all_items", return_value=items), \
             mock.patch("bossctl.supervisor.summary", return_value={"away": False, "pending_wakes": 0}), \
             mock.patch.dict(os.environ, {"TERM": "dumb", "BOSS_PLAIN": "1"}):
            for width in (24, 52, 80):
                rendered = board.header(width) + board.render([123], width)
                self.assertTrue(all(board.cell_width(line) <= width - 1 for line in rendered.splitlines()), width)
                self.assertTrue(all(ord(char) < 128 for char in board.header(width)), width)
            maximum = board.render([123], 100)
            self.assertEqual(sum("investigate" in line for line in maximum.splitlines()), 12)
            self.assertTrue(all(board.cell_width(line) <= 99 for line in maximum.splitlines()))
            self.assertNotIn("超", maximum)
            self.assertNotIn("🚀", maximum)
            self.assertTrue(all(ord(char) < 128 for char in maximum))

    def test_continuous_watch_has_no_cursor_controls_in_plain_or_no_color_modes(self):
        from unittest import mock
        from bossctl import board
        # BOSS_HOME must survive `clear=True`, and the patched name must be the one
        # board.watch actually calls: otherwise this test reads (and locks) the
        # developer's real ~/.boss and probes their live worker pids.
        for env in ({"TERM": "dumb", "BOSS_PLAIN": "1", "BOSS_HOME": str(self.home)},
                    {"TERM": "xterm-256color", "NO_COLOR": "1", "BOSS_HOME": str(self.home)}):
            stream = io.StringIO()
            with self.subTest(env=env), mock.patch.dict(os.environ, env, clear=True), \
                 mock.patch("bossctl.cli.daemon_pids", return_value=[]), \
                 mock.patch("bossctl.board.time.sleep", side_effect=KeyboardInterrupt), \
                 contextlib.redirect_stdout(stream):
                with self.assertRaises(KeyboardInterrupt):
                    board.watch(1, once=False)
            self.assertNotIn("\x1b", stream.getvalue())

    def test_plain_inbox_escapes_repository_text_to_ascii(self):
        from unittest import mock
        from bossctl import cli
        item = {"id": "demo-20260824-120000-abcd", "status": "needs-you", "project": "超長",
                "kind": "scout", "attempts": 0, "max_attempts": 3,
                "text": "🚀 résumé — release", "ask": {"question": "approve 界?"}}
        stream = io.StringIO()
        args = type("Args", (), {"json": False, "hints": True})()
        with mock.patch.dict(os.environ, {"TERM": "dumb", "BOSS_PLAIN": "1"}, clear=True), \
             mock.patch("bossctl.work.all_items", return_value=[item]), contextlib.redirect_stdout(stream):
            cli.cmd_inbox(args)
        rendered = stream.getvalue()
        self.assertTrue(all(ord(char) < 128 for char in rendered))
        self.assertIn(r"\u754c", rendered)
        self.assertIn(r"\U0001f680", rendered)


class NodeBudgetTests(Isolated):
    def test_node_budget_schema_and_non_model_metrics_fail_closed(self):
        from bossctl import work
        self.assertEqual(work.validate_node_budgets({"implement": {"tokens": 100, "seconds": 30}}),
                         {"implement": {"tokens": 100, "cost": None, "seconds": 30}})
        with self.assertRaises(SystemExit):
            work.validate_node_budgets({"verify": {"tokens": 1}})
        with self.assertRaises(SystemExit):
            work.validate_node_budgets({"mystery": {"seconds": 1}})
        with self.assertRaises(SystemExit):
            work.validate_node_budgets({"implement": {"cost": float("nan")}})

    def test_session_receipts_are_cumulative_per_node_and_never_double_counted(self):
        from bossctl import work
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
        from bossctl import work
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
        from bossctl import work
        from bossctl.util import write_json
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
        with mock.patch("bossctl.work._execute") as execute:
            with self.assertRaises(SystemExit):
                work.execute(persisted)
        execute.assert_not_called()

    def test_active_first_turn_waits_for_provider_receipt_but_settled_turn_fails_closed(self):
        from bossctl import work
        budgets = {"tokens": 100, "cost": 1.0}
        self.assertEqual(work._missing_settled_usage(
            budgets, {}, 0, "became unavailable"), [])
        self.assertEqual(work._missing_settled_usage(
            budgets, {}, 1, "became unavailable"),
            ["tokens usage evidence became unavailable", "cost usage evidence became unavailable"])

    def test_cli_node_budget_parser_rejects_duplicates_and_normalizes(self):
        from bossctl import cli
        self.assertEqual(cli._node_budget_args(["implement=100"], ["review_correctness=0.5"], ["verify=20"]),
                         {"implement": {"tokens": 100, "cost": None, "seconds": None},
                          "review_correctness": {"tokens": None, "cost": 0.5, "seconds": None},
                          "verify": {"tokens": None, "cost": None, "seconds": 20}})
        with self.assertRaises(SystemExit):
            cli._node_budget_args(["implement=10", "implement=20"], [], [])


class OwnPiHomeTests(Isolated):
    def test_coo_has_its_own_pi_home_and_inherits_nothing(self):
        src = self.home / "boss-pi"; src.mkdir()
        (src / "auth.json").write_text(json.dumps({"openai-codex": {"access": "tok"}, "anthropic": {"x": 1}}))
        (src / "AGENTS.md").write_text("# personal agent"); (src / "extensions").mkdir()
        os.environ["PI_CODING_AGENT_DIR"] = str(src); os.environ["BOSS_IMPORT_PI_DIR"] = str(src)
        from bossctl.cli import _isolated_pi_home
        dst = _isolated_pi_home()
        self.assertFalse((dst / "AGENTS.md").exists()); self.assertFalse((dst / "extensions").exists())
        self.assertFalse((dst / "auth.json").exists(), "no login is inherited silently")
        settings = json.loads((dst / "settings.json").read_text())
        self.assertEqual((settings["defaultProvider"], settings["defaultModel"]), ("openai-codex", "gpt-5.6-sol"))
        self.assertNotIn("enabledModels", settings, "no model cage: the boss decides")
        (dst / "settings.json").write_text(json.dumps({"defaultModel": "mine", "defaultProvider": "x"}))
        _isolated_pi_home()                                   # second run keeps the boss's choice
        self.assertEqual(json.loads((dst / "settings.json").read_text())["defaultModel"], "mine")
        # --import-login copies only the Codex credential
        r = subprocess.run([str(REPO / "bin" / "bossctl"), "setup", "--import-login", "--json"],
                           env={**os.environ}, text=True, capture_output=True)
        auth = json.loads((dst / "auth.json").read_text())
        self.assertEqual(list(auth), ["openai-codex"])
        del os.environ["PI_CODING_AGENT_DIR"]; del os.environ["BOSS_IMPORT_PI_DIR"]

    def test_every_bossctl_command_runs_in_the_coos_pi_home(self):
        r = subprocess.run([sys.executable, "-c", "import os,sys; sys.argv=['bossctl','status','--json']; sys.path.insert(0, sys.argv[0]); "
                            "from bossctl import cli; cli.main(['status','--json']); print('DIR='+os.environ['PI_CODING_AGENT_DIR'])"],
                           cwd=str(REPO), env={**os.environ, "BOSS_HOME": str(self.home)}, text=True, capture_output=True)
        self.assertIn(f"DIR={self.home.resolve()}/pi", r.stdout)


class DispatchSetTests(Isolated):
    def test_boss_can_change_a_steps_model(self):
        r = subprocess.run([str(REPO / "bin" / "bossctl"), "dispatch", "--set", "implement=openai-codex/gpt-5.6-luna"],
                           env={**os.environ, "BOSS_HOME": str(self.home)}, text=True, capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr); self.assertIn("implement=openai-codex/gpt-5.6-luna", r.stdout)
        from bossctl import dispatch
        self.assertEqual(dispatch.load()["models"]["implement"], "openai-codex/gpt-5.6-luna")
        r = subprocess.run([str(REPO / "bin" / "bossctl"), "dispatch", "--set", "bogus=x"],
                           env={**os.environ, "BOSS_HOME": str(self.home)}, text=True, capture_output=True)
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
        direct = subprocess.run([str(REPO / "bin" / "bossctl"), "--version"],
                                text=True, capture_output=True)
        self.assertEqual(direct.returncode, 0, direct.stderr)
        with tempfile.TemporaryDirectory() as raw:
            link = Path(raw) / "bossctl"; link.symlink_to(REPO / "bin" / "bossctl")
            linked = subprocess.run([str(link), "--version"], text=True, capture_output=True)
            self.assertEqual(linked.returncode, 0, linked.stderr)
            self.assertEqual(linked.stdout, direct.stdout)

    def test_launcher_prefers_the_installer_private_runtime(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); (root / "bin").mkdir(); (root / ".venv" / "bin").mkdir(parents=True)
            shutil.copy2(REPO / "bin" / "bossctl", root / "bin" / "bossctl")
            fake = root / ".venv" / "bin" / "python"
            fake.write_text("#!/bin/sh\n[ \"$1\" = -c ] && exit 0\nprintf 'private-runtime\\n'\n")
            fake.chmod(0o755)
            result = subprocess.run([str(root / "bin" / "bossctl"), "--version"],
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "private-runtime\n")

    def test_cryptographic_runtime_dependency_is_installed_in_ci_and_locally(self):
        requirements = (REPO / "vendor" / "pi-graph" / "requirements.txt").read_text()
        self.assertIn("cryptography>=42,<51", requirements)
        self.assertIn("vendor/pi-graph/requirements.txt", (REPO / "install.sh").read_text())
        self.assertIn("vendor/pi-graph/requirements.txt",
                      (REPO / ".github" / "workflows" / "tests.yml").read_text())
        syntax = subprocess.run(["sh", "-n", str(REPO / "bin" / "bossctl")],
                                text=True, capture_output=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)


class FailClosedStateTests(Isolated):
    """Unreadable durable state is an error to report, never a default that reads as success."""

    def test_corrupt_item_never_disappears_from_the_portfolio(self):
        from bossctl import work
        from bossctl.util import BossError
        good = self.home / "work" / "p-20260101-000000-aaaa"
        good.mkdir(parents=True)
        (good / "item.json").write_text(json.dumps(
            {"id": "p-20260101-000000-aaaa", "project": "p", "status": "queued", "text": "fine",
             "created": "2026-01-01T00:00:00Z", "updated": "2026-01-01T00:00:00Z"}))
        self.assertEqual([i["id"] for i in work.all_items()], ["p-20260101-000000-aaaa"])
        broken = self.home / "work" / "p-20260101-000001-bbbb"
        broken.mkdir(parents=True)
        (broken / "item.json").write_text("{not json")
        with self.assertRaises(BossError) as ctx:
            work.all_items()
        self.assertIn("p-20260101-000001-bbbb", ctx.exception.msg)
        self.assertIn("doctor", ctx.exception.msg)
        with self.assertRaises(BossError):
            work.load("p-20260101-000001-bbbb")

    def test_corrupt_project_registry_is_never_read_as_no_projects(self):
        from bossctl import registry
        from bossctl.util import BossError
        from bossctl.paths import projects_file
        projects_file().parent.mkdir(parents=True, exist_ok=True)
        projects_file().write_text("{\"projects\": [1, 2]}")
        with self.assertRaises(BossError) as ctx:
            registry.load()
        self.assertIn("malformed", ctx.exception.msg)
        projects_file().write_text("{\"projects\": {\"p\": 1}}")
        with self.assertRaises(BossError) as ctx:
            registry.load()
        self.assertIn("malformed entry", ctx.exception.msg)
        projects_file().write_text("{oops")
        with self.assertRaises(BossError):
            registry.load()

    def test_corrupt_worker_ledger_is_never_read_as_no_workers(self):
        # Reading a damaged daemon.pid as "nothing running" would let `up` start a
        # second team against one queue and let `down` claim an unsignalled stop.
        from bossctl import cli
        from bossctl.util import BossError
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "daemon.pid").write_text("[{tru")
        with self.assertRaises(BossError) as ctx:
            cli.daemon_pids()
        self.assertIn("worker ledger", ctx.exception.msg)
        (self.home / "daemon.pid").write_text("")
        self.assertEqual(cli.daemon_pids(), [], "an empty ledger really does mean no workers")

    def test_malformed_package_json_refuses_instead_of_reporting_no_tests(self):
        from bossctl import detect
        from bossctl.util import BossError
        repo = self.home / "broken"; repo.mkdir(parents=True)
        (repo / "package.json").write_text('{"scripts": {"test": ')
        with self.assertRaises(BossError) as ctx:
            detect.test_command(repo)
        self.assertIn("not valid JSON", ctx.exception.msg)
        # A manifest with no test script is still an honest "nothing detected".
        (repo / "package.json").write_text('{"name": "x"}')
        self.assertIsNone(detect.test_command(repo))


class ScopeEscapeTests(unittest.TestCase):
    def test_declaring_a_sensitive_path_does_not_disable_the_escape_check(self):
        # Regression: SENSITIVE membership made is_global() true, and escaped()
        # short-circuited on is_global(), so declaring a lockfile returned "nothing
        # escaped" for every other path the agent touched.
        from bossctl import scope
        declared = ["package-lock.json"]
        self.assertTrue(scope.is_global(declared), "still serializes against everything")
        self.assertEqual(scope.escaped(declared, ["package-lock.json"]), [])
        self.assertEqual(scope.escaped(declared, ["src/secret.py", "package-lock.json"]),
                         ["src/secret.py"])
        # A genuinely global declaration still has nothing to escape from.
        for glob in ("*", "**", "unknown"):
            self.assertEqual(scope.escaped([glob], ["anything/at/all.py"]), [])


class LogRotationTests(Isolated):
    def test_log_rotates_once_and_reports_an_unwritable_trail(self):
        from bossctl import util
        from bossctl.paths import log_file
        util.log("first line", console=False)
        log_file().write_bytes(b"x" * (util.MAX_LOG_BYTES + 1))
        util.log("after rotation", console=False)
        rotated = log_file().with_name(log_file().name + ".1")
        self.assertTrue(rotated.exists(), "the oversized log is preserved as one generation")
        self.assertIn("after rotation", log_file().read_text())
        self.assertLess(log_file().stat().st_size, util.MAX_LOG_BYTES)


class TabRegistryTests(Isolated):
    def test_remembered_tabs_are_bounded_and_malformed_state_fails_closed(self):
        from bossctl import herdr
        from bossctl.util import BossError
        from bossctl.util import write_json
        write_json(self.home / "herdr.json", {"tabs": [
            {"kind": "worker", "tab_id": f"t{n}"} for n in range(herdr.MAX_REMEMBERED_TABS)]})
        for n in range(herdr.MAX_REMEMBERED_TABS, herdr.MAX_REMEMBERED_TABS + 25):
            herdr.remember("worker", {"tab_id": f"t{n}"})
        tabs = herdr.remembered_tabs()
        self.assertEqual(len(tabs), herdr.MAX_REMEMBERED_TABS)
        self.assertEqual(tabs[-1]["tab_id"], f"t{herdr.MAX_REMEMBERED_TABS + 24}", "newest kept")
        (self.home / "herdr.json").write_text('["not", "a", "registry"]')
        with self.assertRaises(BossError):
            herdr.remembered_tabs()


class DeadProjectPathTests(Isolated):
    def test_a_registered_path_that_is_no_longer_a_checkout_is_visible_and_refuses_work(self):
        from bossctl import registry, work
        from bossctl.util import BossError, write_json
        from bossctl.paths import projects_file
        gone = self.home / "was-a-repo"
        write_json(projects_file(), {"projects": {"p": {
            "id": "p", "path": str(gone), "mode": "local-only", "authority": 1,
            "base": "main", "test_cmd": "true", "protected_paths": [], "gate": "native"}}})
        loaded = registry.load(check_paths=True)["projects"]["p"]
        self.assertFalse(loaded["available"])
        # The stat is opt-in: the daemon-poll path must not pay for it.
        self.assertNotIn("available", registry.load()["projects"]["p"])
        with self.assertRaises(BossError) as ctx:
            work.create("p", "do something", "ship", [], 3, None, None, None, None, None, None, None)
        self.assertIn("no longer a Git checkout", ctx.exception.msg)
        # The registration survives: restoring the checkout restores the project.
        gone.mkdir(parents=True); (gone / ".git").mkdir()
        self.assertTrue(registry.load(check_paths=True)["projects"]["p"]["available"])
        # The derived flag is never written back into the durable registry.
        registry.set_fields("p", authority=2)
        self.assertNotIn("available", json.loads(projects_file().read_text())["projects"]["p"])


class BoardMetaTests(Isolated):
    def test_rows_carry_id_age_and_attempts_and_say_what_was_not_shown(self):
        from bossctl import board
        items = [{"id": f"p-2026-{n:04d}", "status": "running", "project": "p",
                  "text": "make the thing work", "attempts": 1, "max_attempts": 3,
                  "updated": "2026-01-01T00:00:00Z", "created": "2026-01-01T00:00:00Z"}
                 for n in range(board.MAX_BOARD_ROWS + 3)]
        with mock.patch("bossctl.board.registry.load", return_value={"projects": {}}), \
             mock.patch("bossctl.board.work.all_items", return_value=items), \
             mock.patch("bossctl.supervisor.summary", return_value={"away": False, "pending_wakes": 0}):
            rendered = board.render([4242], 200)
        self.assertIn("1 worker", rendered)
        self.assertIn(f"p-2026-{board.MAX_BOARD_ROWS + 2:04d}", rendered, "newest row is addressable by id")
        self.assertIn("a1/3", rendered, "attempt count is visible")
        self.assertIn("3 older open items not shown", rendered)

    def test_age_is_compact_and_unparseable_stamps_stay_unknown(self):
        import datetime as dt
        from bossctl import board
        now = dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc)
        self.assertEqual(board.age("2026-01-02T00:00:00Z", now=now), "0s")
        self.assertEqual(board.age("2026-01-01T23:58:00Z", now=now), "2m")
        self.assertEqual(board.age("2026-01-01T21:00:00Z", now=now), "3h")
        self.assertEqual(board.age("2025-12-30T00:00:00Z", now=now), "3d")
        self.assertEqual(board.age(None), "?")
        self.assertEqual(board.age("not a timestamp"), "?")

    def test_whitespace_only_item_text_never_raises_on_the_board(self):
        from bossctl import board
        self.assertEqual(board.first_line("   \n\n"), "(untitled)")
        self.assertEqual(board.first_line("  real title \nsecond"), "real title")


class CheckScriptTests(unittest.TestCase):
    """./check.sh must stay honest: never green on a skip, never touch live Herdr."""

    def test_help_is_syntactically_valid_and_documents_the_flags(self):
        script = REPO / "check.sh"
        self.assertTrue(os.access(script, os.X_OK), "check.sh must be executable")
        syntax = subprocess.run(["bash", "-n", str(script)], text=True, capture_output=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        helped = subprocess.run([str(script), "--help"], text=True, capture_output=True)
        self.assertEqual(helped.returncode, 0, helped.stderr)
        self.assertIn("--fast", helped.stdout)
        rejected = subprocess.run([str(script), "--nope"], text=True, capture_output=True)
        self.assertEqual(rejected.returncode, 2)

    def test_a_gate_never_sees_the_callers_herdr_session(self):
        # The suite starts real Herdr agents when it can see a live session, and once
        # did exactly that against a working session. Prove the stripping behaviourally:
        # a gate that prints its own environment must not receive the poisoned values.
        script = REPO / "check.sh"
        body = script.read_text()
        self.assertIn("env -u HERDR_ENV", body)
        harness = Path(tempfile.mkdtemp()) / "probe.sh"
        # Reuse check.sh's own gate() definition rather than a copy of it.
        gate_src = body[body.index("gate() {"):body.index("# ------------------------------------------------------------------ runtime --")]
        harness.write_text(
            "set -u\n"
            'LOG_DIR="$1"\n'
            "RESULTS=()\n"
            "OVERALL_PASS=true\n"
            "SKIPPED=0\n"
            "say() { :; }\n"
            "record() { :; }\n"
            + gate_src +
            '\ngate "PROBE" probe sh -c \'env | grep -c HERDR_ || true\'\n'
        )
        log_dir = harness.parent
        result = subprocess.run(["bash", str(harness), str(log_dir)], text=True, capture_output=True,
                                env={**os.environ, "HERDR_ENV": "1", "HERDR_SESSION": "poison",
                                     "HERDR_WORKSPACE_ID": "poison", "PATH": os.environ.get("PATH", "")})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((log_dir / "probe.log").read_text().strip(), "0",
                         "a gate saw HERDR_* coordinates from the caller")

    def test_a_skipped_gate_is_never_reported_as_a_pass(self):
        # With no bun on PATH the bun gates must print SKIPPED, and the final line
        # must not claim an unqualified pass.
        empty_bin = Path(tempfile.mkdtemp())
        for tool in ("bash", "git", "python3", "sh", "env", "grep", "sed", "tail", "awk", "mktemp", "printf"):
            source = shutil.which(tool)
            if source:
                (empty_bin / tool).symlink_to(source)
        result = subprocess.run([str(REPO / "check.sh"), "--fast"], text=True, capture_output=True,
                                env={**os.environ, "PATH": str(empty_bin)})
        self.assertIn("SKIPPED", result.stdout)
        self.assertNotIn("ALL GATES PASSED.", result.stdout)
        if "ALL RUNNABLE GATES PASSED" in result.stdout:
            self.assertIn("not proven, just not run", result.stdout)


if __name__ == "__main__":
    unittest.main()
