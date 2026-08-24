"""Controller Git never executes project/ambient helpers and preserves exact paths."""
from __future__ import annotations

import ast
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from bossctl import scope, work, worktree
from bossctl.git_boundary import local_config_value
from bossctl.util import BossError, sh

REPO = Path(__file__).resolve().parents[1]


class GitBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="boss-git-boundary-test-"))
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.raw("init", "-q", "-b", "main")
        self.raw("config", "user.name", "test")
        self.raw("config", "user.email", "test@example.invalid")
        (self.repo / "filtered.txt").write_text("base\n")
        (self.repo / "data.bin").write_bytes(b"base\n")
        (self.repo / "merge.txt").write_text("base\n")
        (self.repo / ".gitattributes").write_text(
            "filtered.txt filter=sentinel\n"
            "*.bin diff=sentinel\n"
            "merge.txt merge=sentinel\n"
        )
        self.raw("add", "-A")
        self.raw("commit", "-qm", "base")
        self.sentinel = self.root / "controller-helper-ran"
        self.helper = self.root / "sentinel-helper"
        self.helper.write_text(
            "#!/bin/sh\n"
            f"/usr/bin/touch {shlex.quote(str(self.sentinel))}\n"
            "/bin/cat\n"
        )
        self.helper.chmod(0o700)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def raw(self, *args: str, check: bool = True) -> str:
        result = subprocess.run(["git", "-C", str(self.repo), *args], check=check,
                                text=True, capture_output=True)
        return result.stdout.strip()

    def configure_executable_project_values(self) -> None:
        hooks = self.root / "hooks"
        hooks.mkdir()
        hook = hooks / "post-commit"
        shutil.copyfile(self.helper, hook)
        hook.chmod(0o700)
        for key, value in (
            ("core.hooksPath", str(hooks)),
            ("core.fsmonitor", str(self.helper)),
            ("filter.sentinel.clean", str(self.helper)),
            ("filter.sentinel.smudge", str(self.helper)),
            ("filter.sentinel.required", "true"),
            ("diff.sentinel.command", str(self.helper)),
            ("diff.external", str(self.helper)),
            ("merge.sentinel.driver", f"{self.helper} %O %A %B %L %P"),
            ("core.sshCommand", str(self.helper)),
            ("credential.helper", f"!{self.helper}"),
        ):
            self.raw("config", key, value)

    def assert_helper_absent(self) -> None:
        self.assertFalse(self.sentinel.exists(), "repository-controlled helper executed")

    def test_checkpoint_status_diff_and_hook_ignore_all_executable_config(self):
        (self.repo / "filtered.txt").write_text("changed\n")
        ambient = {
            **os.environ,
            "GIT_EXTERNAL_DIFF": str(self.helper),
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": str(self.root / "hooks"),
            "GIT_CONFIG_KEY_1": "diff.external",
            "GIT_CONFIG_VALUE_1": str(self.helper),
        }
        status = sh(["git", "-C", str(self.repo), "status", "--porcelain=v1"],
                    check=False, env=ambient)
        self.assertEqual(status.returncode, 0, status.stderr)
        diff = sh(["git", "-C", str(self.repo), "diff"], check=False, env=ambient)
        self.assertEqual(diff.returncode, 0, diff.stderr)
        added = sh(["git", "-C", str(self.repo), "add", "filtered.txt"],
                   check=False, env=ambient)
        self.assertEqual(added.returncode, 0, added.stderr)
        committed = sh([
            "git", "-C", str(self.repo), "-c", "user.name=BOSS Checkpoint",
            "-c", "user.email=boss@local.invalid", "commit", "-m", "checkpoint",
        ], check=False, env=ambient)
        self.assertEqual(committed.returncode, 0, committed.stderr)
        self.assert_helper_absent()

        self.configure_executable_project_values()
        (self.repo / "filtered.txt").write_text("second change\n")
        for args in (
            ["git", "-C", str(self.repo), "status", "--porcelain=v1"],
            ["git", "-C", str(self.repo), "diff"],
            ["git", "-C", str(self.repo), "add", "filtered.txt"],
        ):
            with self.assertRaises(BossError):
                sh(args, check=False)
        self.assert_helper_absent()

    def test_merge_driver_and_transport_helper_are_never_executed(self):
        self.raw("checkout", "-qb", "other")
        (self.repo / "merge.txt").write_text("other\n")
        self.raw("add", "merge.txt")
        self.raw("commit", "-qm", "other")
        self.raw("checkout", "-q", "main")
        (self.repo / "merge.txt").write_text("main\n")
        self.raw("add", "merge.txt")
        self.raw("commit", "-qm", "main")

        remote = sh(["git", "-C", str(self.repo), "ls-remote",
                     f"ext::{self.helper}"], check=False, network=True)
        self.assertNotEqual(remote.returncode, 0)
        self.assert_helper_absent()

        self.configure_executable_project_values()
        with self.assertRaises(BossError):
            sh(["git", "-C", str(self.repo), "merge", "other"], check=False)
        self.assert_helper_absent()

    def test_safe_config_reader_ignores_includes_and_rejects_ambiguity(self):
        included = self.root / "included.config"
        included.write_text("[remote \"origin\"]\n\turl = ext::should-not-load\n")
        self.raw("config", "include.path", str(included))
        self.raw("config", "remote.origin.url", "https://github.com/acme/widget.git")
        self.assertEqual(local_config_value(self.repo, "remote.origin.url"),
                         "https://github.com/acme/widget.git")
        self.raw("config", "--add", "remote.origin.url", "https://github.com/acme/other.git")
        with self.assertRaises(BossError):
            local_config_value(self.repo, "remote.origin.url")

    def test_local_remote_executable_config_is_rejected_before_fetch(self):
        remote = self.root / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "push", "-q", str(remote), "main"],
                       check=True)
        subprocess.run([
            "git", "--git-dir", str(remote), "config", "uploadpack.packObjectsHook",
            str(self.helper),
        ], check=True)
        with self.assertRaises(BossError):
            sh(["git", "-C", str(self.repo), "fetch", "--no-tags",
                "--no-write-fetch-head", str(remote),
                "refs/heads/main:refs/remotes/test/main"],
               check=False, network=True, write_paths=(self.repo,), read_paths=(remote,))
        self.assert_helper_absent()

    def test_every_production_literal_git_or_gh_subprocess_routes_through_boundary(self):
        offenders = []
        for path in sorted((REPO / "bossctl").glob("*.py")):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr not in {"run", "Popen", "check_output"} or not node.args:
                    continue
                command = node.args[0]
                if not isinstance(command, (ast.List, ast.Tuple)) or not command.elts:
                    continue
                first = command.elts[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    if Path(first.value).name in {"git", "gh"}:
                        offenders.append(f"{path.name}:{node.lineno}")
        self.assertEqual(offenders, [])


class ExactPathTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="boss-exact-path-test-"))
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "test")
        self.git("config", "user.email", "test@example.invalid")
        (self.repo / "base.txt").write_text("base\n")
        self.git("add", "base.txt")
        self.git("commit", "-qm", "base")
        self.git("checkout", "-qb", "work")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              text=True, capture_output=True).stdout.strip()

    def test_changed_status_and_protected_gate_preserve_hostile_path_bytes(self):
        names = [
            ".github/workflows/line\nbreak.yml",
            ".github/workflows/tab\tname.yml",
            '.github/workflows/quote"name.yml',
            ".github/workflows/back\\slash.yml",
            ".github/workflows/日本.yml",
            "src/line\nbreak.py",
        ]
        for name in names:
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(name)
        self.git("add", "-A")
        self.git("commit", "-qm", "unusual names")
        project = {"base": "main"}
        self.assertEqual(worktree.changed_files(project, self.repo), sorted(names))
        safety = work._apply_safety_pipeline(
            {"id": "p-exact", "kind": "ship", "dispatch": {"graph": "local-only"},
             "scope": {"paths": ["src/**"]}},
            {"base": "main", "protected_paths": [".github/workflows/*"],
             "test_cmd": "true"},
            self.repo, {"ok": True, "run_dir": str(self.root)}, {}, 30,
            restricted=False,
        )
        self.assertFalse(safety["ok"])
        self.assertEqual(safety["failed_ids"], ["protected"])
        self.assertEqual(scope.escaped(["src/**"], names), sorted(names[:-1]))

        dirty = ".github/workflows/dirty\nname.yml"
        (self.repo / dirty).write_text("dirty\n")
        self.assertIn(dirty, worktree.status_paths(self.repo))

        raw = b"\0".join(os.fsencode(value) for value in names) + b"\0"
        result = subprocess.run([
            sys.executable, str(REPO / "bossctl" / "check_protected.py"), "--nul",
            ".github/workflows/*",
        ], input=raw, capture_output=True)
        self.assertEqual(result.returncode, 1)
        output = os.fsdecode(result.stdout)
        for value in names[:-1]:
            self.assertIn(repr(value), output)

    def test_worktree_registry_preserves_newline_in_worktree_path(self):
        destination = self.root / "linked\nworktree"
        self.git("branch", "boss/item")
        self.git("worktree", "add", "-q", str(destination), "boss/item")
        paths = worktree.branch_worktrees({"path": str(self.repo)}, "boss/item")
        self.assertEqual(paths, [destination.resolve()])


if __name__ == "__main__":
    unittest.main()
