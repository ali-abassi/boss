# firstmate graph

[![tests](https://github.com/ali-abassi/firstmate-graph/actions/workflows/tests.yml/badge.svg)](https://github.com/ali-abassi/firstmate-graph/actions/workflows/tests.yml)
[![license](https://img.shields.io/badge/license-MIT-11110f)](LICENSE)

**You talk to one agent. It runs a crew across your repos and comes back only when it needs you.**

```
you ──► first mate ──► crew: one agent per task, each in its own copy of the repo
                          implement → test → review, steps it cannot skip
you ◄── first mate ◄── questions · finished branches · failures
```

You describe the outcome. The first mate hands it to a worker, watches the crew in the
background, and reports when something is ready or needs a decision. Nothing merges until
you say so. Runs on your Codex subscription.

## On deck

```
         N
      W  ✦  E       ⚓  F I R S T   M A T E
         S          C A P T A I N ' S   C O N T R O L   D E C K
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  ◇  2 projects · workers in background · 1 running · 1 need you
     /fleet  ·  /inbox  ·  /wake 20m

 you ›  fix the flaky login test in api, and find out why the web bundle is 4 MB
        ~~⛵~~~  hailing the crew

 Two items under way, captain. I'll report when they land or need a decision.
 ──────────────────────────────────────────────────── ⚓ 2 under way ─
```

That's the interface: a persistent Pi session with a compass masthead, a boat while it
thinks, and a footer that tells you how many hands are busy. A zero-token supervisor writes
durable, deduplicated wakes; only actionable evidence can wake the first mate.

## Why

One coding agent is easy. Five of them across five repos means five terminals, five
half-remembered contexts, and you as the scheduler. firstmate graph gives you one
conversation instead: the first mate keeps the thread, code decides what runs and in what
order, and every run leaves evidence on disk.

It keeps the operating contract of [firstmate](https://github.com/kunchenguid/firstmate)
([fork](https://github.com/ali-abassi/firstmate)). Production work runs only in persistent
Herdr Pi agents. The bundled [pi-graph](https://github.com/ali-abassi/pi-graph) runner orders
the workflow; a checked-in inert runner is the deterministic test seam, never a production
agent or substitute identity.

## Proof

**[`tests/test_one_thread.py`](tests/test_one_thread.py)** runs the whole idea on every push:
six tasks, three repos, two workers in parallel, one worker that stops to ask a question,
one inbox with everything in it, no repo touched until the captain says merge.

**[`docs/evidence/interactive-session.md`](docs/evidence/interactive-session.md)** is a
transcript of `pi-firstmate` used for real: delegate, get told the truth when it failed,
retry, merge on the captain's word. **[`live-run.md`](docs/evidence/live-run.md)** is the
same path as an opt-in test (`HELM_LIVE=1 python3 -m unittest tests.test_live`).

```sh
python3 -m unittest discover -s tests -v     # deterministic suite; no model calls
```

## Install

```sh
curl -fsSL https://raw.githubusercontent.com/ali-abassi/firstmate-graph/main/install.sh | bash
pi-firstmate
```

That's it. The installer checks what you have, builds a private runtime, puts
`pi-firstmate` on your PATH, and tells you the one next step. The first run connects to
your Codex subscription (it reuses the login from your Pi if there is one).

You need [Pi](https://github.com/earendil-works/pi) (`npm i -g @earendil-works/pi-coding-agent`),
git, Python 3.10+, and a Codex subscription. `gh` is needed only for pull requests. The CLI and
deterministic tests run on macOS and Linux; managed production agents currently require macOS
`sandbox-exec`. Prefer a checkout? `git clone … ~/firstmate-graph && ~/firstmate-graph/install.sh`.

## Use

There is one command, and then you talk.

```sh
pi-firstmate
```

> **you:** add ~/code/api and ~/code/web
> **first mate:** Registered both, captain — `api` runs `npm test`, `web` runs `pnpm test`. Local-only for now; say the word for PRs.
>
> **you:** fix the flaky login test in api, and find out why the web bundle is 4 MB
> **first mate:** Two items under way. I'll report when they land or need a decision.
>
> *(later)* **first mate:** Captain — the login fix is ready on a branch, tests green. The bundle scout wants to know: is the analytics SDK required in production?
> **you:** no, drop it. merge the login fix.
> **first mate:** Merged `api` to main. Guidance passed to the scout; it's back under way.

The first mate wakes itself from durable supervisor evidence when the crew has news, and
`/wake 20m` schedules a session-local check-in. Inside Pi: `/fleet` shows the board, `/inbox` what needs
you, and `/away on` enters unattended mode only after supervision, recovery, worker, and
decision-queue gates pass. Away mode can finish work but never merges or discards anything;
new decisions remain visibly queued until `/away off`. `pi-firstmate` is the sole
launcher: outside [Herdr](https://herdr.dev) it creates or attaches the named persistent
`firstmate` session; inside Herdr it runs directly, without recursion. Each live item keeps one
real implementer identity, tab, and worktree through questions, steering, and review feedback;
positive death still needs an item-specific recovery authorization before replacement. Reviews
use fresh identities.

Managed Herdr Pi launches fail closed unless macOS `/usr/bin/sandbox-exec` is available. The
canonical Herdr `pi` command is bound inside its pane to a repository-owned wrapper. Its outer
profile gives Pi provider access but confines writes to the owned worktree and exact launch
state. Every model-invoked shell command and final test command then passes through a
controller-owned runner with a second pinned profile: no network, unrelated user-home/control-plane
reads, process signalling, or Git-metadata writes; the environment is scrubbed and execution is
bounded. Each invocation receives a unique inherited sandbox fingerprint; settlement stops and
reaps every process that still carries it, including double-forked/session-detached descendants,
and fails closed unless the host can prove none remain. Built-in file tools are scoped to the worktree. Pi must
prove a forbidden write was blocked before its identity is accepted, and its prompt-free
lifecycle ledger is hash-chained and signed with a per-launch Ed25519 key. This is not a separate
OS account. If Herdr or either boundary is unavailable, production work fails without changing
project work; there is no headless production fallback.

`pi-firstmate doctor` is a read-only audit. New controller directories are `0700`; state,
lock, evidence, and log files are created `0600`, and JSON replacement is crash-durable.
Existing permissive paths are reported without being silently chmodded. Its optional repair plan changes nothing unless
you run `pi-firstmate doctor --repair --confirm`; confirmed repair rechecks its evidence,
quarantines corrupt derived state or positively dead execution, and preserves worktrees,
branches, session history, and recovery holds. It never treats a bare or reused PID as an
owned worker and never kills, relaunches, merges, or deletes project work. Explicit confirmed
options can copy one named provider record from `--auth-source`, select a `--model
PHASE=provider/model` already present in the offline inventory, set a shell-valid `--test
PROJECT=COMMAND` without running it, or `--fetch PROJECT` for the exact configured origin/base
while preserving checkout HEAD and status. Orphaned worktrees are moved intact to quarantine.
First Mate never runs broad `git worktree prune`. If Git metadata for a quiescent item's
canonical worktree is positively absent, confirmed repair first moves the complete detached
directory into private retained custody, journals every phase, reconstructs a separate worktree
at the unchanged branch SHA, verifies the exact payload, and only then atomically reattaches it.
The retained source is not deleted. Recovery resumes after a crash but stops if the branch,
payload, path ownership, or agent liveness changes. Because the pruned Git index no longer
exists, its staged-versus-unstaged metadata is explicitly unrecoverable; file bytes, modes,
links, xattrs, deletions, and untracked payload remain preserved in the retained source.
Doctor never logs in, installs, guesses a choice, merges, or updates a checkout.
`pi-firstmate stop` stops the crew. `pi-firstmate claude` opens the same first mate in
Claude Code. `pi-firstmate-quit` stops the crew and closes its Herdr session. The machinery
underneath is in
[`docs/cli.md`](docs/cli.md) for the curious.

## Rules

| | |
|---|---|
| **Delivery** per repo | a branch for you to merge (default) · a pull request · or `high-assurance`: protected-path gate, full verification, and two fresh exact-SHA reviews. You pick by saying so ("open PRs for api") |
| **Authority** per repo | investigate only · build · open PRs · merge on your word. Starts at build; raised only when you ask |
| **Models** | GPT-5.6 Sol with high thinking by default. Resolution, overrides, and rationale are pinned before execution; unavailable or drifted models fail instead of silently swapping |
| **Budgets** | token, cost, and elapsed-time usage is durable and cumulative across the persistent implementer plus fresh reviewers. A reached limit pauses before the next observable turn; an already-running provider turn can finish before its usage is known. Raising a paused limit is an explicit captain action |
| **Retries** | failed, paused, interrupted, and blocked work keeps the same branch/worktree/checkpoint; unchanged repeated failures pause instead of retrying blindly |
| **Questions** | a worker that needs a decision stops and asks; it does not guess |
| **Recovery** | dead/unknown workers, sessions, leases, claims, tabs, and broken worktree registrations are surfaced, never killed, pruned, or relaunched automatically. Confirmed doctor reconciliation retains the original worktree; item-specific `recover` is still required for a replacement agent |
| **GitHub** | open PRs are monitored against the registered repository, PR number, base ref/SHA, and exact reviewed head SHA. Check/status evidence must be complete and SHA-bound. Pending, failed, green, merged, closed, moved, outage, and rate-limit evidence are distinct; uncertainty is never green |
| **Memory** | operational facts are explicit keyed records; project knowledge becomes a scoped high-assurance `AGENTS.md` work item. Secrets and transcript dumps are rejected |
| **Evidence** | every task keeps its brief, exact graph, checkpoint, real session identity, required exact-SHA reviews, tests, output, tokens and cost; inspection recursively redacts secrets. See [the control-plane architecture](docs/control-plane.md) |

`high-assurance` is First Mate’s native workflow; legacy local `no-mistakes` mode records read
back as that canonical name. It is not the separate no-mistakes product.

The optional external adapter pins no-mistakes v1.57.1/tag
`a6f64fcdb4e82c0ddbbd9f01ed91e97dd233d42d`. It currently accepts only the matching signed
macOS release executable after checking its architecture-specific SHA-256, embedded build,
Developer ID team/identifier, hardened runtime, and timestamp. It also requires an already
initialized, owner-controlled no-mistakes home whose read-only SQLite schema and repository,
origin, gate remote, default branch, and push target bind to the registered First Mate project.
First Mate never installs, initializes, updates, syncs, resets, aborts, or repairs that product.

For an eligible ship item, First Mate journals the clean reviewed head, base, worktree
fingerprint, intent, binary proof, repository binding, and all prior matching run IDs before one
`axi run` request. It never passes `--yes` and never repeats an uncertain run or decision request.
Captain decisions are bound to the exact observed gate and use `gate-status`, `gate-reconcile`,
and `gate-respond`; conflicting controls, cancellation, and away mode remain blocked until the
transaction is authoritatively reconciled. Delivery succeeds only from a unique SQLite receipt
matching the exact repo/branch/base/head/version/build, exact review approval, inactive exact
push target/ref, open canonical PR, and persisted CI readiness. A changed head, moved base,
closed PR, ambiguous run, missing database evidence, or external outage is never success.
Configured First Mate token/cost/time budgets reject external execution because v1.57.1 cannot
prove those limits to this controller. The installer never installs no-mistakes or claims a real
run was verified; deterministic adapter tests use an explicit fake seam.

Promotion is a serialized, restart-recoverable exact-SHA transaction. It revalidates the
registered project, base, branch, clean fingerprint, reviews, gate, controls, and away state
before arming. A successful merge command is only a request until Git or GitHub independently
shows that exact commit merged; late worktree changes are preserved and block cleanup. Proven
cleanup removes only the exact clean worktree and deliberately retains the already-merged branch
ref as recovery evidence.

## Status

Early and opinionated, used daily by one person. Issues and PRs welcome.

MIT.
