"""Memory stays narrow, explicit, deduplicated, and authority-safe."""
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

from bossctl import dispatch, memory
from bossctl.util import BossError, write_json


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()); self.home = self.root / "home"
        os.environ["BOSS_HOME"] = str(self.home)
        os.environ["BOSS_PIW"] = str(Path(__file__).resolve().parent / "fake_piw.py")
        self.repo = self.root / "repo"; self.repo.mkdir()
        subprocess.run(["git", "-C", str(self.repo), "init", "-q", "-b", "main"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "t"], check=True)
        (self.repo / "README.md").write_text("x\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "init"], check=True)
        write_json(self.home / "projects.json", {"projects": {"p": {"id": "p", "path": str(self.repo),
            "mode": "local-only", "authority": 1, "base": "main", "test_cmd": "true",
            "protected_paths": [], "gate": "native"}}})

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        os.environ.pop("BOSS_PIW", None)

    def test_operational_upsert_is_keyed_and_identical_write_is_noop(self):
        first = memory.set_operational("worker.preference", "Keep retries bounded")
        path = self.home / "memory.json"; before = (path.read_bytes(), path.stat().st_mtime_ns)
        duplicate = memory.set_operational("worker.preference", "Keep retries bounded")
        after = (path.read_bytes(), path.stat().st_mtime_ns)
        updated = memory.set_operational("worker.preference", "Stop after repeated evidence")
        self.assertTrue(first["changed"])
        self.assertTrue(duplicate["deduplicated"])
        self.assertEqual(before, after)
        self.assertEqual(updated["revision"], 2)
        self.assertEqual(len(memory.list_operational()), 1)

    def test_secrets_transcript_keys_and_bulk_values_are_rejected(self):
        for key, value in (("transcript", "hello"), ("auth", "token=supersecretvalue"),
                           ("cloud", "AWS key AKIAIOSFODNN7EXAMPLE"),
                           ("chat", "User: do this\nAssistant: done"),
                           ("json", '{"messages":[{"role":"user","content":"dump"}]}'),
                           ("bulk", "x" * 2001)):
            with self.assertRaises(BossError): memory.set_operational(key, value)
        self.assertFalse((self.home / "memory.json").exists())

    def test_remove_requires_confirmation(self):
        memory.set_operational("note", "value")
        with self.assertRaises(BossError): memory.remove_operational("note", confirm=False)
        self.assertEqual(len(memory.list_operational()), 1)
        memory.remove_operational("note", confirm=True)
        self.assertEqual(memory.list_operational(), [])

    def test_entry_bound_fails_before_writing_an_unreadable_ledger(self):
        with mock.patch("bossctl.memory.MAX_ENTRIES", 2):
            memory.set_operational("one", "first")
            memory.set_operational("two", "second")
            path = self.home / "memory.json"; before = path.read_bytes()
            with self.assertRaises(BossError):
                memory.set_operational("three", "must not be written")
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual([entry["key"] for entry in memory.list_operational()], ["one", "two"])

    def test_project_memory_queues_high_assurance_review_without_editing_checkout(self):
        before = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True, capture_output=True).stdout
        result = memory.request_project("p", "set", "testing.rule", "Run the full suite before promotion")
        item = result["item"]
        self.assertEqual(item["status"], "queued")
        self.assertEqual(item["dispatch"]["graph"], "high-assurance")
        self.assertEqual(item["scope"]["paths"], ["AGENTS.md"])
        self.assertEqual(item["memory_request"]["key"], "testing.rule")
        self.assertFalse((self.repo / "AGENTS.md").exists())
        after = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True, capture_output=True).stdout
        self.assertEqual(before, after)
        duplicate = memory.request_project("p", "set", "testing.rule", "Run the full suite before promotion")
        self.assertTrue(duplicate["deduplicated"])
        with self.assertRaises(BossError):
            memory.request_project("p", "set", "testing.rule", "A conflicting new value")

    def test_project_remove_is_also_a_confirmed_reviewed_change(self):
        with self.assertRaises(BossError): memory.request_project("p", "remove", "old.rule", confirm=False)
        result = memory.request_project("p", "remove", "old.rule", confirm=True)
        self.assertEqual(result["item"]["dispatch"]["graph"], "high-assurance")
        self.assertEqual(result["request"]["operation"], "remove")

    def test_project_memory_cannot_be_downgraded_by_a_custom_dispatch_rule(self):
        config = json.loads(json.dumps(dispatch.DEFAULT))
        config["rules"].insert(0, {"name": "hostile-memory-downgrade", "kind": "ship",
                                    "labels": ["project-memory"], "graph": "local-only"})
        write_json(self.home / "dispatch.json", config)
        result = memory.request_project("p", "set", "security.rule", "Never trust missing evidence")
        self.assertEqual(result["item"]["dispatch"]["rule"], "hostile-memory-downgrade")
        self.assertEqual(result["item"]["dispatch"]["graph"], "high-assurance")


if __name__ == "__main__":
    unittest.main()
