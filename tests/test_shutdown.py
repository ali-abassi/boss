"""The canonical quit command reports success only after exact shutdown proof."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


class ShutdownTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()); self.home = self.root / "boss-home"; self.user_home = self.root / "user-home"; self.bin = self.root / "bin"
        self.home.mkdir(); self.user_home.mkdir(); self.bin.mkdir()
        self.firstmate = self.user_home / ".helm"; self.firstmate.mkdir()
        (self.firstmate / "sentinel").write_text("firstmate must remain untouched\n")
        self.herdr_log = self.root / "herdr.log"
        fake = self.bin / "herdr"
        fake.write_text("""#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_HERDR_LOG"
if [ \"${FAKE_STOP_FAIL:-0}\" = 1 ] && [ \"$1 $2\" = \"session stop\" ]; then exit 3; fi
if [ \"$1 $2\" = \"session list\" ]; then
  if [ \"${FAKE_STILL_RUNNING:-0}\" = 1 ]; then running=true; else running=false; fi
  printf '{\"sessions\":[{\"name\":\"boss\",\"running\":%s},{\"name\":\"firstmate\",\"running\":true}]}\\n' \"$running\"
  exit 0
fi
printf '{}\\n'
""")
        fake.chmod(0o755)
        self.script = Path(__file__).resolve().parents[1] / "bin" / "pi-boss-quit"
        self.env = {**os.environ, "HOME": str(self.user_home), "BOSS_HOME": str(self.home),
                    "FAKE_HERDR_LOG": str(self.herdr_log),
                    "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", "")}

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def invoke(self, **env):
        return subprocess.run([str(self.script)], env={**self.env, **env}, text=True, capture_output=True)

    def test_success_is_printed_only_after_stopped_session_observation(self):
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "BOSS closed")
        calls = self.herdr_log.read_text().splitlines()
        self.assertIn("session stop boss --json", calls)
        self.assertFalse(any("session stop firstmate" in call for call in calls))
        self.assertEqual((self.firstmate / "sentinel").read_text(), "firstmate must remain untouched\n")

    def test_stop_failure_and_false_stopped_observation_propagate(self):
        failed = self.invoke(FAKE_STOP_FAIL="1")
        self.assertNotEqual(failed.returncode, 0); self.assertNotIn("BOSS closed", failed.stdout)
        unproven = self.invoke(FAKE_STILL_RUNNING="1")
        self.assertNotEqual(unproven.returncode, 0); self.assertNotIn("BOSS closed", unproven.stdout)

    @unittest.skipIf(os.name == "nt", "POSIX process-state receipt")
    def test_exact_zombie_is_dead_without_trusting_a_foreign_pid(self):
        from bossctl import processes
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.2)"])
        try:
            record = processes.capture(child.pid, "zombie-test")
            self.assertIsNotNone(record)
            time.sleep(0.5)  # do not poll/wait yet: preserve the exited child as a zombie
            self.assertEqual(processes.probe(record)["state"], "dead")
            foreign = {**record, "start_sha256": "0" * 64}
            self.assertEqual(processes.probe(foreign)["state"], "reused")
        finally:
            child.wait(timeout=5)

    def test_shutdown_accepts_pid_reuse_only_after_signalling_exact_live_identity(self):
        """A recycled PID proves our worker exited but never authorizes a second signal."""
        from bossctl import cli
        record = {"version": 1, "kind": "boss-worker", "pid": 43210, "pgid": 43210,
                  "owner": "worker-1", "start_sha256": "a" * 64,
                  "command_sha256": "b" * 64}
        (self.home / "daemon.pid").write_text(json.dumps([record]) + "\n")
        observations = iter([
            {"state": "live", "pid": 43210, "pgid": 43210},
            {"state": "reused", "pid": 43210,
             "reason": "PID now belongs to a different process identity (birth receipt changed)"},
        ])
        stream = io.StringIO()
        with mock.patch.dict(os.environ, {"BOSS_HOME": str(self.home)}), \
             mock.patch.object(cli.processes, "probe", side_effect=observations), \
             mock.patch.object(cli.os, "killpg") as killpg, \
             contextlib.redirect_stdout(stream):
            cli.cmd_down(SimpleNamespace(json=True))
        killpg.assert_called_once_with(43210, cli.signal.SIGTERM)
        self.assertFalse((self.home / "daemon.pid").exists())
        self.assertEqual(json.loads(stream.getvalue())["pids"], [43210])

    def test_command_drift_is_not_positive_pid_reuse_evidence(self):
        from bossctl import processes
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
        try:
            record = processes.capture(child.pid, "drift-test")
            self.assertIsNotNone(record)
            altered = {**record, "command_sha256": "0" * 64}
            finding = processes.probe(altered)
            self.assertEqual(finding["state"], "untrusted")
            self.assertIn("command or group", finding["reason"])
        finally:
            child.terminate(); child.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
