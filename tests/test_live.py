"""Live end-to-end test against the real piw and a real model. Opt in: BOSS_LIVE=1.

Spends a few cents on the cheapest configured model. It prints the receipt; a caller may opt
into a specific evidence path with BOSS_LIVE_EVIDENCE. The test never silently rewrites docs.
"""
try:
    import _gitenv  # noqa: F401  (git hygiene for temp repos)
except ImportError:
    from tests import _gitenv  # noqa: F401
import json, os, shutil, subprocess, tempfile, time, unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BOSS = [str(REPO / "bin" / "bossctl")]


@unittest.skipUnless(os.environ.get("BOSS_LIVE") == "1" and shutil.which("pi"),
                     "set BOSS_LIVE=1 with pi installed to run the live test")
class LiveTest(unittest.TestCase):
    def test_real_piw_real_model_local_only(self):
        tmp = Path(tempfile.mkdtemp()); home = tmp / "home"; repo = tmp / "repo"; repo.mkdir()
        env = {**os.environ, "BOSS_HOME": str(home)}
        env.pop("BOSS_PIW", None)
        g = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True).stdout.strip()
        g("init", "-q", "-b", "main"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
        (repo / "fizz.py").write_text("def fizzbuzz(n):\n    raise NotImplementedError\n")
        (repo / "test_fizz.py").write_text('from fizz import fizzbuzz\ndef test_basic():\n'
                                           '    assert fizzbuzz(3) == "Fizz" and fizzbuzz(5) == "Buzz" and fizzbuzz(15) == "FizzBuzz" and fizzbuzz(7) == "7"\n')
        g("add", "-A"); g("commit", "-qm", "init"); before = g("rev-parse", "HEAD")
        test_cmd = "python3 -m venv .venv >/dev/null && .venv/bin/pip install -q pytest && .venv/bin/python -m pytest -q"
        def bossctl(*a):
            r = subprocess.run(BOSS + list(a), env=env, text=True, capture_output=True)
            self.assertEqual(r.returncode, 0, f"bossctl {' '.join(a)}\n{r.stdout}\n{r.stderr}"); return r.stdout
        bossctl("setup", "--json")                     # own Pi home; reuses the Codex login from your Pi
        bossctl("add", str(repo), "--id", "live", "--test", test_cmd, "--mode", "local-only", "--authority", "3", "--protected", "test_fizz.py")
        wid = json.loads(bossctl("task", "live", "Implement fizzbuzz(n) in fizz.py so test_fizz.py passes; return str(n) for non-multiples. Do not modify test_fizz.py.", "--labels", "cheap", "--json"))["id"]
        t0 = time.time(); bossctl("run-once", "--timeout", "600"); secs = time.time() - t0
        it = json.loads(bossctl("show", wid, "--json"))
        self.assertEqual(it["status"], "ready", it)
        run = it["runs"][-1]
        self.assertTrue(Path(run["run_dir"], "ledger.json").is_file(), "pi-graph evidence bundle missing")
        self.assertEqual(g("rev-parse", "HEAD"), before, "main moved before promotion")
        bossctl("promote", wid, "--confirm")
        self.assertNotEqual(g("rev-parse", "HEAD"), before)
        self.assertIn("FizzBuzz", (repo / "fizz.py").read_text())
        ledger = Path(run["run_dir"], "log.md").read_text().strip().splitlines()[-5:]
        receipt = (
            f"# Live run — {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}\n\n"
            f"`tests/test_live.py` against the real `piw` and model `{it['dispatch']['models']['implement']}`.\n\n"
            f"- task: {it['text']}\n- graph: `{it['dispatch']['graph']}` · rule `{it['dispatch']['rule']}`\n"
            f"- wall time: {secs:.0f}s · tokens: {run['tokens']} · cost: ${run['cost']}\n"
            f"- result: verify gate green → `ready` → `promote --confirm` fast-forwarded `main`\n\n"
            f"pi-graph ledger:\n\n```\n" + "\n".join(ledger) + "\n```\n")
        print(receipt)
        if evidence_path := os.environ.get("BOSS_LIVE_EVIDENCE"):
            Path(evidence_path).write_text(receipt)


if __name__ == "__main__":
    unittest.main()
