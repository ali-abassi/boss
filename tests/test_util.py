"""Controller state replacement is private, concurrent-safe, and parseable."""
import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from helm.paths import home
from helm.util import locked, log, write_json


class DurableJSONTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_new_state_directory_and_record_are_private(self):
        path = self.root / "state" / "record.json"
        write_json(path, {"value": 1})
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(path.read_text()), {"value": 1})
        self.assertEqual(list(path.parent.glob(".record.json.*.tmp")), [])

    def test_fresh_nested_controller_state_is_private_under_permissive_umask(self):
        state_home = self.root / "fresh-home"
        previous_home = os.environ.get("HELM_HOME")
        previous_umask = os.umask(0)
        os.environ["HELM_HOME"] = str(state_home)
        try:
            record = home() / "work" / "p-item" / "item.json"
            write_json(record, {"id": "p-item"})
            with locked(home() / "locks" / "controller.lock"):
                pass
            log("private-log-test", console=False)
        finally:
            os.umask(previous_umask)
            if previous_home is None:
                os.environ.pop("HELM_HOME", None)
            else:
                os.environ["HELM_HOME"] = previous_home
        for directory in (state_home, state_home / "work", state_home / "work" / "p-item",
                          state_home / "locks"):
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700, directory)
        for path in (record, state_home / "locks" / "controller.lock", state_home / "helm.log"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600, path)

    def test_concurrent_replacements_never_share_a_temp_file(self):
        path = self.root / "state" / "record.json"
        errors = []
        def writer(index):
            try:
                for generation in range(20):
                    write_json(path, {"writer": index, "generation": generation})
            except BaseException as exc:
                errors.append(exc)
        threads = [threading.Thread(target=writer, args=(index,)) for index in range(4)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        value = json.loads(path.read_text())
        self.assertIn(value["writer"], range(4)); self.assertIn(value["generation"], range(20))
        self.assertEqual(list(path.parent.glob(".record.json.*.tmp")), [])

    def test_state_creation_refuses_a_symlinked_missing_parent(self):
        outside = self.root / "outside"; outside.mkdir()
        linked = self.root / "linked"; linked.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(NotADirectoryError):
            write_json(linked / "nested" / "record.json", {"must": "not escape"})
        self.assertFalse((outside / "nested").exists())


if __name__ == "__main__":
    unittest.main()
