#!/usr/bin/env python3
"""Stand-in for `piw` so the pipeline is testable without spending tokens.

Behaviour is driven by FAKE_PIW_MODE: ok | ask | fail | scout.
Reads cwd/BASE/BRANCH out of the rendered steps.yaml, like the real runner would.
"""
import json, os, re, subprocess, sys, time
from pathlib import Path

argv = sys.argv[1:]
if argv[:1] == ["validate"]:
    text = Path(argv[1]).read_text()
    if "@{" in text:
        print("unrendered placeholder"); sys.exit(1)
    sys.exit(0)
assert argv[:1] == ["run"], argv
steps = Path(argv[1])
text = steps.read_text()
cwd = re.search(r"^cwd: (.+)$", text, re.M).group(1).strip()
run_dir = steps.parent / "runs" / f"fake-{int(time.time()*1000)}"
run_dir.mkdir(parents=True)
brief = Path(argv[argv.index("--input-file") + 1]).read_text()
(run_dir / "input.txt").write_text(brief)
mode = os.environ.get("FAKE_PIW_MODE", "ok")
# Per-item behaviour can be requested from inside the brief, so one daemon can serve a mixed queue.
for marker in ("ask", "fail", "dirty", "sensitive", "scout-write", "ok"):
    if f"[fake:{marker}]" in brief:
        mode = marker
if mode == "ask" and "Boss guidance" in brief:
    mode = "ok"          # the question was answered; a real worker would proceed too
if "workflow: bossctl-scout" in text and mode != "scout-write":
    mode = "scout"
(run_dir / "mode.txt").write_text(mode)
# Evidence for concurrency assertions: when this "worker" started and finished, and where.
started = time.time()
work_seconds = float(os.environ.get("FAKE_PIW_SECONDS", "0"))
time.sleep(work_seconds)
(run_dir / "worker.json").write_text(json.dumps({"cwd": cwd, "started": started, "finished": time.time(), "pid": os.getpid()}))

def done(ok, failed):
    print(json.dumps({"ok": ok, "passed": 1, "failed": len(failed), "cached": 0, "skipped": 0,
                      "cost": 0.01, "tokens": 123, "run_dir": str(run_dir), "failed_ids": failed}))
    sys.exit(0 if ok else 1)

if mode == "ask":
    Path(cwd, ".boss-ask.json").write_text(json.dumps({"question": "Which auth provider?", "context": "two exist"}))
    (run_dir / "protected.stderr").write_text("ASKED")
    done(False, ["protected"])
if mode == "fail":
    (run_dir / "verify.stderr").write_text("FAIL test_thing: expected 2 got 3")
    done(False, ["verify"])
if mode in ("scout", "scout-write"):
    (run_dir / "report.md").write_text("# Report\n\nfindings…\n")
    if mode == "scout-write": Path(cwd, "scout-wrote.txt").write_text("forbidden\n")
    done(True, [])
# ok: make a commit in the worktree, honouring guidance if present
Path(cwd, "bossctl-change.txt").write_text("changed\n" + ("guided\n" if "Boss guidance" in brief else ""))
if mode == "sensitive": Path(cwd, "package-lock.json").write_text(json.dumps({"generated": time.time()}))
subprocess.run(["git", "-C", cwd, "add", "-A"], check=True)
subprocess.run(["git", "-C", cwd, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "bossctl: fake change"], check=True)
sha = subprocess.run(["git", "-C", cwd, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
base_ref = re.search(r"\(base: ([^)]+)\)", text).group(1)
base_sha = subprocess.run(["git", "-C", cwd, "rev-parse", base_ref], check=True,
                          capture_output=True, text=True).stdout.strip()
review_sha = {"omit": None, "wrong": "0" * 40}.get(os.environ.get("FAKE_PIW_REVIEW_SHA"), sha)
# The stand-in emits actual exact-SHA reviewer-node evidence; the control plane never invents verdicts.
if "review_correctness" in text:
    evidence = {"verdict": "accept", "notes": "fake reviewer evidence", "base_sha": base_sha}
    if review_sha is not None: evidence["sha"] = review_sha
    (run_dir / "review_correctness.json").write_text(json.dumps(evidence))
if "review_adversarial" in text:
    evidence = {"verdict": "accept", "notes": "fake reviewer evidence", "base_sha": base_sha}
    if review_sha is not None: evidence["sha"] = review_sha
    (run_dir / "review_adversarial.json").write_text(json.dumps(evidence))
if mode == "dirty": Path(cwd, "unreviewed.txt").write_text("must not deliver\n")
done(True, [])
