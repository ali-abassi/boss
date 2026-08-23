"""The canonical quit command reports success only after exact shutdown proof."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class ShutdownTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()); self.home = self.root / "home"; self.bin = self.root / "bin"
        self.home.mkdir(); self.bin.mkdir()
        fake = self.bin / "herdr"
        fake.write_text("""#!/bin/sh
if [ \"${FAKE_STOP_FAIL:-0}\" = 1 ] && [ \"$1 $2\" = \"session stop\" ]; then exit 3; fi
if [ \"$1 $2\" = \"session list\" ]; then
  if [ \"${FAKE_STILL_RUNNING:-0}\" = 1 ]; then running=true; else running=false; fi
  printf '{\"sessions\":[{\"name\":\"firstmate\",\"running\":%s}]}\\n' \"$running\"
  exit 0
fi
printf '{}\\n'
""")
        fake.chmod(0o755)
        self.script = Path(__file__).resolve().parents[1] / "bin" / "pi-firstmate-quit"
        self.env = {**os.environ, "HELM_HOME": str(self.home),
                    "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", "")}

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def invoke(self, **env):
        return subprocess.run([str(self.script)], env={**self.env, **env}, text=True, capture_output=True)

    def test_success_is_printed_only_after_stopped_session_observation(self):
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "first mate closed")

    def test_stop_failure_and_false_stopped_observation_propagate(self):
        failed = self.invoke(FAKE_STOP_FAIL="1")
        self.assertNotEqual(failed.returncode, 0); self.assertNotIn("first mate closed", failed.stdout)
        unproven = self.invoke(FAKE_STILL_RUNNING="1")
        self.assertNotEqual(unproven.returncode, 0); self.assertNotIn("first mate closed", unproven.stdout)


if __name__ == "__main__":
    unittest.main()
