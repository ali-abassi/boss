"""The managed Pi boundary is an OS capability, not a command-text heuristic."""
import os
import json
import socket
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from helm import sandbox


@unittest.skipUnless(os.uname().sysname == "Darwin" and Path("/usr/bin/sandbox-exec").is_file(),
                     "macOS sandbox-exec capability test")
class SandboxTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()); self.home = self.root / "home"
        os.environ["HELM_HOME"] = str(self.home)
        self.item = self.home / "work" / "p-sandbox"; self.item.mkdir(parents=True)
        self.wt = self.home / "worktrees" / "p" / "p-sandbox"; self.wt.mkdir(parents=True)
        self.pi_home = self.home / "pi"; self.pi_home.mkdir()
        (self.pi_home / "settings.json").write_text("{}\n")
        self.wrapper = Path(__file__).resolve().parents[1] / "libexec" / "pi"

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def run_profile(self, record, script, *args):
        env = {**os.environ, "HELM_SANDBOX_REQUIRED": "1",
               "HELM_SANDBOX_PROFILE": record["profile"], "HELM_REAL_PI": "/bin/sh"}
        return subprocess.run([str(self.wrapper), "-c", script, "sh", *map(str, args)],
                              env=env, text=True, capture_output=True)

    def test_indirect_shell_and_symlink_writes_cannot_escape_owned_worktree(self):
        record = sandbox.prepare(work_id="p-sandbox", launch_id="implementer", cwd=self.wt,
                                 session_dir=self.item / "sessions" / "one",
                                 attestation_dir=self.item / "attest" / "one",
                                 writable_worktree=True, pi_source_dir=self.pi_home)
        outside = self.root / "outside"; outside.mkdir()
        (self.wt / "linked").symlink_to(outside, target_is_directory=True)
        owned = self.wt / "owned.txt"; escaped = outside / "escaped.txt"
        result = self.run_profile(record,
                                  'printf owned > "$1"; printf escaped > "$2" 2>/dev/null || true',
                                  owned, self.wt / "linked" / "escaped.txt")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(owned.read_text(), "owned")
        self.assertFalse(escaped.exists(), "canonical symlink target must remain outside capability")

    def test_reviewer_can_write_only_exact_verdict_not_repository(self):
        verdict = self.item / "runs" / "review.pending.json"; verdict.parent.mkdir(parents=True)
        record = sandbox.prepare(work_id="p-sandbox", launch_id="reviewer", cwd=self.wt,
                                 session_dir=self.item / "sessions" / "review",
                                 attestation_dir=self.item / "attest" / "review",
                                 writable_worktree=False, exact_writes=[str(verdict)],
                                 pi_source_dir=self.pi_home)
        mutation = self.wt / "mutation.txt"
        result = self.run_profile(record,
                                  'printf verdict > "$1"; printf mutation > "$2" 2>/dev/null || true',
                                  verdict, mutation)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(verdict.read_text(), "verdict")
        self.assertFalse(mutation.exists())

    def test_private_pi_config_is_copied_and_shared_home_stays_outside_capability(self):
        (self.pi_home / "auth.json").write_text('{"token":"private"}\n')
        record = sandbox.prepare(work_id="p-sandbox", launch_id="private-pi", cwd=self.wt,
                                 session_dir=self.item / "sessions" / "private",
                                 attestation_dir=self.item / "attest" / "private",
                                 writable_worktree=True, pi_source_dir=self.pi_home)
        private_settings = Path(record["pi_dir"]) / "settings.json"
        result = self.run_profile(record,
                                  'printf private > "$1"; printf shared > "$2" 2>/dev/null || true',
                                  private_settings, self.pi_home / "settings.json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(private_settings.read_text(), "private")
        self.assertEqual((self.pi_home / "settings.json").read_text(), "{}\n")
        self.assertEqual((Path(record["pi_dir"]) / "auth.json").stat().st_mode & 0o777, 0o600)

    def _hostile_payload(self, secret: Path, outside: Path, late: Path, port: int, victim: int) -> Path:
        payload = self.wt / "payload.py"
        payload.write_text(
            "import json, os, signal, socket, subprocess, sys\n"
            "secret, outside, late, port, victim = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5])\n"
            "result = {'env_secret': os.environ.get('FIRSTMATE_TEST_SECRET')}\n"
            "try:\n open(outside, 'w').write('escaped')\n result['outside'] = 'wrote'\n"
            "except Exception as e:\n result['outside'] = type(e).__name__\n"
            "try:\n result['secret'] = open(secret).read()\n"
            "except Exception as e:\n result['secret'] = type(e).__name__\n"
            "try:\n socket.create_connection(('127.0.0.1', port), 0.5).close(); result['network'] = 'connected'\n"
            "except Exception as e:\n result['network'] = type(e).__name__\n"
            "try:\n os.kill(victim, signal.SIGTERM); result['signal'] = 'sent'\n"
            "except Exception as e:\n result['signal'] = type(e).__name__\n"
            "try:\n open('.git', 'w').write('rewritten'); result['git'] = 'wrote'\n"
            "except Exception as e:\n result['git'] = type(e).__name__\n"
            "child = subprocess.Popen(['/bin/sh', '-c', 'sleep 1; printf late > \"$1\"', 'sh', late])\n"
            "result['child_pid'] = child.pid\n"
            "open('result.json', 'w').write(json.dumps(result))\n"
        )
        return payload

    def test_nested_tool_boundary_denies_indirect_host_authority_and_reaps_children(self):
        record = sandbox.prepare(work_id="p-sandbox", launch_id="hostile-tool", cwd=self.wt,
                                 session_dir=self.item / "sessions" / "hostile",
                                 attestation_dir=self.item / "attest" / "hostile",
                                 writable_worktree=True, pi_source_dir=self.pi_home)
        secret = self.root / "host-secret"; secret.write_text("credential-material")
        outside = self.root / "outside-write"; late = self.root / "late-write"
        (self.wt / ".git").write_text("controller metadata")
        listener = socket.socket(); listener.bind(("127.0.0.1", 0)); listener.listen(1)
        listener.settimeout(0.2)
        victim = subprocess.Popen(["/bin/sleep", "30"])
        payload = self._hostile_payload(secret, outside, late, listener.getsockname()[1], victim.pid)
        command = " ".join(["/usr/bin/python3", str(payload), str(secret), str(outside), str(late),
                            str(listener.getsockname()[1]), str(victim.pid)])
        env = {**os.environ, "FIRSTMATE_TEST_SECRET": "must-not-cross"}
        try:
            result = subprocess.run([
                str(Path(record["tool_runner"])), record["tool_profile"], record["tool_profile_sha256"],
                record["tmpdir"], "10", command,
            ], cwd=self.wt, env=env, text=True, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            evidence = json.loads((self.wt / "result.json").read_text())
            self.assertIsNone(evidence["env_secret"])
            self.assertNotEqual(evidence["outside"], "wrote")
            self.assertNotEqual(evidence["secret"], "credential-material")
            self.assertNotEqual(evidence["network"], "connected")
            self.assertNotEqual(evidence["signal"], "sent")
            self.assertNotEqual(evidence["git"], "wrote")
            self.assertEqual((self.wt / ".git").read_text(), "controller metadata")
            self.assertFalse(outside.exists())
            with self.assertRaises(socket.timeout): listener.accept()
            self.assertIsNone(victim.poll(), "sandboxed payload must not signal an unrelated process")
            time.sleep(1.2)
            self.assertFalse(late.exists(), "background descendants must be reaped before they can mutate later")
        finally:
            listener.close()
            victim.terminate()
            try: victim.wait(timeout=2)
            except subprocess.TimeoutExpired: victim.kill(); victim.wait()

    def test_candidate_verification_uses_the_same_network_and_external_write_boundary(self):
        secret = self.root / "verify-secret"; secret.write_text("private")
        outside = self.root / "verify-outside"
        payload = self.wt / "verify_payload.py"
        payload.write_text(
            "import os, socket, sys\n"
            "try: open(sys.argv[1], 'w').write('escaped')\n"
            "except Exception: pass\n"
            "try: socket.create_connection(('127.0.0.1', int(sys.argv[2])), .2)\n"
            "except Exception: pass\n"
        )
        listener = socket.socket(); listener.bind(("127.0.0.1", 0)); listener.listen(1); listener.settimeout(0.2)
        try:
            result = sandbox.run_verification(
                work_id="p-sandbox", phase="hostile", cwd=self.wt,
                command=f"/usr/bin/python3 {payload} {outside} {listener.getsockname()[1]}",
                timeout=10, env={**os.environ, "FIRSTMATE_TEST_SECRET": "must-not-cross"},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(outside.exists())
            with self.assertRaises(socket.timeout): listener.accept()
        finally:
            listener.close()

    def test_double_forked_session_descendant_is_reaped_before_late_mutation(self):
        late = self.wt / "detached-late.txt"
        payload = self.wt / "detach_payload.py"
        payload.write_text(
            "import os, pathlib, sys, time\n"
            "for marker in pathlib.Path(os.environ['HOME']).glob('.firstmate-reaper-*'):\n"
            " try: marker.unlink()\n"
            " except OSError: pass\n"
            "if os.fork(): os._exit(0)\n"
            "os.setsid()\n"
            "if os.fork(): os._exit(0)\n"
            "time.sleep(1)\n"
            "open(sys.argv[1], 'w').write('escaped')\n"
        )
        result = sandbox.run_verification(
            work_id="p-sandbox", phase="detached", cwd=self.wt,
            command=f"/usr/bin/python3 {payload} {late}", timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        time.sleep(1.2)
        self.assertFalse(late.exists(), "a setsid/double-fork descendant escaped controller reaping")

    def test_timeout_reaps_the_gated_process_tree_and_preserves_timeout_status(self):
        late = self.wt / "timeout-late.txt"
        command = f"(/bin/sleep 2; /usr/bin/printf escaped > {late}) & /bin/sleep 30"
        result = sandbox.run_verification(
            work_id="p-sandbox", phase="timeout", cwd=self.wt,
            command=command, timeout=1,
        )
        self.assertEqual(result.returncode, 124, result.stderr)
        time.sleep(2.2)
        self.assertFalse(late.exists(), "a timed-out descendant survived its controller")


if __name__ == "__main__":
    unittest.main()
