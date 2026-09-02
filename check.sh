#!/usr/bin/env bash
# check.sh — one command that says whether BOSS is known-good right now.
#
#   ./check.sh              every deterministic gate, then a read-only live report
#   ./check.sh --fast       skip the full Python suite (the slow one, ~10 min)
#
# Part 1 runs exactly what CI runs, so a green scorecard here means a green
# badge there. Part 2 reports the live control plane READ-ONLY: it never starts,
# stops, repairs, or writes anything under ~/.boss, and never spends a model
# token. The exit code is about the tree, not the environment: live findings
# print as ACTION NEEDED and are counted in the last line, but only unreadable
# control state (a corrupt ~/.boss, not merely an empty one) fails the run.
#
# Exit code: 0 only when every runnable gate passed. A gate that cannot run on
# this machine (no bun, no Python 3.10+) is reported as SKIPPED, never as green.
#
# Safety: every gate runs with HERDR_* stripped from the environment. The test
# suite starts real Herdr agents when it can see a live session, and once did
# exactly that against Ali's working session. Do not remove that guard.

set -u

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

FAST=0
for arg in "$@"; do
  case "$arg" in
    --fast) FAST=1 ;;
    --help|-h) awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"; exit 0 ;;
    *) printf 'check.sh: unknown argument %s\n' "$arg" >&2; exit 2 ;;
  esac
done

LOG_DIR="$(mktemp -d "${TMPDIR:-/tmp}/boss-check.XXXXXX")"
OVERALL_PASS=true
SKIPPED=0
LIVE_ACTIONS=0
RESULTS=()

say() { printf '%s\n' "$*"; }

record() {   # record LABEL STATUS  — STATUS is one of: ok, fail, skip
  case "$2" in
    fail) OVERALL_PASS=false ;;
    skip) SKIPPED=$((SKIPPED + 1)) ;;
  esac
  RESULTS+=("$1")
}

gate() {     # gate LABEL LOGNAME COMMAND...
  local label=$1 logname=$2; shift 2
  say "  ... $label"
  if env -u HERDR_ENV -u HERDR_SESSION -u HERDR_WORKSPACE_ID -u HERDR_BIN \
      "$@" >"$LOG_DIR/$logname.log" 2>&1; then
    record "$label PASS" ok
  else
    record "$label FAIL (see $LOG_DIR/$logname.log)" fail
    say "      last lines:"
    tail -12 "$LOG_DIR/$logname.log" | sed 's/^/      | /'
  fi
}

# ------------------------------------------------------------------ runtime --
# bin/bossctl picks the same interpreter; ask it rather than guessing twice.
PYTHON=""
for candidate in "$REPO_DIR/.venv/bin/python" "${BOSS_PYTHON:-}" python3; do
  [ -n "$candidate" ] || continue
  command -v "$candidate" >/dev/null 2>&1 || continue
  if "$candidate" -c 'import sys; from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
    PYTHON=$candidate
    break
  fi
done

say "BOSS check — deterministic gates first, live report second."
say ""
say "[1/2] Deterministic gates (what CI runs)"

if [ -z "$PYTHON" ]; then
  record "PYTHON GATES SKIPPED (no Python 3.10+ with cryptography; run install.sh)" skip
else
  gate "SHELL SYNTAX" shellcheck bash -n install.sh bin/pi-boss bin/pi-boss-quit check.sh
  gate "COMPILEALL" compileall "$PYTHON" -m compileall -q bossctl tests
  if [ "$FAST" -eq 1 ]; then
    record "PYTHON SUITE SKIPPED (--fast)" skip
  else
    say "      (the suite is the slow gate — several minutes)"
    gate "PYTHON SUITE" unittest "$PYTHON" -m unittest discover -s tests
    # unittest prints its count to stderr, which the gate captured.
    COUNT="$(grep -Eo '^Ran [0-9]+ tests' "$LOG_DIR/unittest.log" | grep -Eo '[0-9]+' | tail -1)"
    [ -n "${COUNT:-}" ] && RESULTS[${#RESULTS[@]} - 1]="${RESULTS[${#RESULTS[@]} - 1]} (${COUNT} tests)"
  fi
fi

if command -v bun >/dev/null 2>&1 && [ -d node_modules ]; then
  gate "PI EXTENSION" extension bun .pi/extensions/boss.ts
  gate "ATTESTATION SCOPE" attest bun bossctl/pi_attest.ts
elif command -v bun >/dev/null 2>&1; then
  record "BUN GATES SKIPPED (run: bun add -d @earendil-works/pi-coding-agent @earendil-works/pi-tui)" skip
else
  record "BUN GATES SKIPPED (bun is not installed)" skip
fi

gate "WHITESPACE" whitespace git diff --check

# --------------------------------------------------------------------- live --
say ""
say "[2/2] Live control plane (read-only)"
LIVE_LINES=()
if [ -z "$PYTHON" ]; then
  LIVE_LINES+=("LIVE STATE unavailable (no usable Python runtime)")
else
  BOSS_STATUS="$("$REPO_DIR/bin/bossctl" status --json 2>"$LOG_DIR/status.err")" || BOSS_STATUS=""
  if [ -z "$BOSS_STATUS" ]; then
    LIVE_LINES+=("LIVE STATE unreadable (see $LOG_DIR/status.err)")
    OVERALL_PASS=false
  else
    # bash 3.2 (the macOS system shell) has no `mapfile`.
    while IFS= read -r summary_line; do
      LIVE_LINES+=("$summary_line")
    done < <(printf '%s' "$BOSS_STATUS" | "$PYTHON" -c '
import json, sys

status = json.load(sys.stdin)
items = status.get("items") or {}
open_states = ("queued", "running", "paused", "needs-you", "ready", "pr-open")
decision_states = ("needs-you", "failed", "ready", "pr-open")
open_count = sum(items.get(state, 0) for state in open_states)
needs = sum(items.get(state, 0) for state in decision_states)
print("TEAM {} live worker(s); {} open item(s); {} awaiting the boss".format(
    status.get("worker_count", 0), open_count, needs))
print("PROJECTS {} registered".format(status.get("projects", 0)))
dead = status.get("projects_unavailable") or []
if dead:
    print("DEAD PATHS {} - registered but no longer a Git checkout".format(", ".join(dead)))
supervisor = status.get("supervisor") or {}
if supervisor.get("healthy") is False:
    print("SUPERVISOR unhealthy: {}".format(supervisor.get("error")))
else:
    print("SUPERVISOR healthy; {} pending wake(s); away={}".format(
        supervisor.get("pending_wakes", 0), bool(supervisor.get("away"))))
planning = status.get("planning") or {}
if planning.get("enabled"):
    print("PLANNING pulses enabled (advisory only)")
' 2>"$LOG_DIR/live.err")
    if [ "${#LIVE_LINES[@]}" -eq 0 ]; then
      LIVE_LINES+=("LIVE STATE could not be summarized (see $LOG_DIR/live.err)")
    fi
    case "${LIVE_LINES[*]}" in
      *"DEAD PATHS"*|*"SUPERVISOR unhealthy"*) LIVE_ACTIONS=$((LIVE_ACTIONS + 1)) ;;
    esac
  fi
fi

for line in "${LIVE_LINES[@]}"; do say "  ... $line"; done

# ---------------------------------------------------------------- scorecard --
say ""
say "===================== BOSS SCORECARD ====================="
for line in "${RESULTS[@]}"; do say "  $line"; done
say "  ---"
for line in "${LIVE_LINES[@]}"; do say "  $line"; done
say "=========================================================="
if [ "$OVERALL_PASS" = true ]; then
  if [ "$SKIPPED" -gt 0 ]; then
    say "  ALL RUNNABLE GATES PASSED ($SKIPPED skipped — not proven, just not run)."
  else
    say "  ALL GATES PASSED. This tree matches what CI will say."
  fi
  if [ "$LIVE_ACTIONS" -gt 0 ]; then
    say "  ACTION NEEDED: $LIVE_ACTIONS live finding(s) above — your setup, not this tree."
  fi
  exit 0
fi
say "  SOMETHING FAILED — this tree is NOT known-good."
say "  Logs: $LOG_DIR"
exit 1
