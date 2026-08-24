<div align="center">

# Run a coding crew from one conversation

**Firstmate Graph gives one persistent Pi agent a Herdr crew across your repositories. Every task follows an explicit workflow; high-assurance work cannot skip implement → test → review. Nothing merges until you say so.**

[![tests](https://github.com/ali-abassi/firstmate-graph/actions/workflows/tests.yml/badge.svg)](https://github.com/ali-abassi/firstmate-graph/actions/workflows/tests.yml)
[![license](https://img.shields.io/badge/license-MIT-11110f)](LICENSE)

[Quickstart](#quickstart) · [How it works](#how-it-works) · [Commands](#commands) · [Which Firstmate?](#which-firstmate-should-you-use) · [Evidence](#evidence) · [Control-plane docs](docs/control-plane.md)

<img src="assets/hero.svg" width="100%" alt="One captain conversation flows through Firstmate Graph into persistent Herdr workers. High-assurance work passes implement, test, and exact-SHA review gates before ready work or decisions return to the captain, who keeps merge authority.">

</div>

## Why this exists

One coding agent is easy. Several tasks turn you into the scheduler:

- one tab per agent, each with half the context;
- worktrees, retries, questions, and costs tracked in your head;
- “done” reports that may not name the tested commit;
- sessions that disappear while unfinished work is still on disk.

Firstmate Graph flips that arrangement. You keep one conversation. The control plane gives each
task its own persistent worker, worktree, workflow, budget, and evidence trail, then returns only
ready work, honest failures, or a decision that actually needs you.

## Quickstart

> [!IMPORTANT]
> Managed production workers currently require **macOS** with `/usr/bin/sandbox-exec`,
> [Herdr](https://herdr.dev), [Pi](https://github.com/earendil-works/pi), Git, Python 3.10+,
> and a Codex subscription. The CLI and deterministic suite also run on Linux. `gh` is needed
> only for pull requests.

```sh
curl -fsSL https://raw.githubusercontent.com/ali-abassi/firstmate-graph/main/install.sh | bash
```

Verified installer receipt on 2026-08-24, on a Mac where Pi and Codex were already connected:

```text
✓ bundled runner ready
✓ pi 0.84.2
✓ Codex login found in your Pi — the first mate will reuse it

next:  pi-firstmate
```

Then run `pi-firstmate` and talk normally:

> **You:** add `~/code/api` and `~/code/web`<br>
> **First mate:** Registered both. Local branches by default; nothing merges without your word.
>
> **You:** fix the flaky login test in api, and find out why the web bundle is 4 MB<br>
> **First mate:** Two items under way. I’ll report when they land or need a decision.

The installer builds a private runtime, puts `pi-firstmate` and `pi-firstmate-quit` on your PATH,
and reuses an existing Pi Codex login when available. It does not install Herdr or the external
no-mistakes product, and it never claims either is ready when it cannot prove it.

## How it works

1. **You describe the outcome.** The first mate registers projects and turns requests into
   explicit work items; you do not write orchestration commands.
2. **Each item gets durable custody.** It keeps one branch, worktree, scope claim, checkpoint,
   budget, and real Herdr implementer identity through questions, steering, and review feedback.
3. **Code orders the work.** Bundled [Pi Graph](https://github.com/ali-abassi/pi-graph) workflows
   run the configured gates in an order the model cannot skip. High-assurance delivery includes
   implementation, protected-path checks, tests, and two exact-commit reviews.
4. **A zero-model-turn supervisor watches.** Healthy steady state consumes no agent turns.
   Durable deduplicated wakes surface `needs-you`, stale, wedged, dead, and unknown states without
   automatically killing or replacing anything.
5. **Delivery stays a captain decision.** A branch or PR may become ready, but local merge and
   GitHub promotion revalidate the exact base, head SHA, reviews, and authority before acting.

The runner bundles Pi Graph 0.3.0 and ports the two pinned run-bundle compatibility fixes from
[Agent Workflows v0.2.0](https://github.com/ali-abassi/agent-workflows/tree/v0.2.0). The fuller
Pi Graph command surface remains available; Agent Workflows is not downloaded at runtime.

## Commands

The captain-facing surface stays deliberately small:

| You want to… | Use |
|---|---|
| Open or return to the persistent captain session | `pi-firstmate` |
| See the fleet without entering the session | `pi-firstmate status` |
| Run a read-only health and recovery audit | `pi-firstmate doctor` |
| Enter or leave gated unattended supervision | `pi-firstmate away on` / `off` / `status` |
| Stop background workers but keep the session | `pi-firstmate stop` |
| Stop the crew and close its Herdr session | `pi-firstmate-quit` |
| Launch the same first mate through another captain harness | `pi-firstmate claude` or `pi-firstmate codex` |

Inside Pi, `/fleet` shows the board, `/inbox` shows only actionable items, and `/wake 20m`
schedules a session-local check-in. Ordinary work is requested in plain language.

## What the control plane enforces

| Boundary | Runtime contract |
|---|---|
| **Identity** | A live item keeps one real Pi session UUID, Herdr pane, process-birth fingerprint, and signed lifecycle ledger. A label or reused PID is never accepted as identity. |
| **Workflow** | Every delivery graph integrates the recorded base, checks scope/protected paths, runs the configured test command, rejects test-created mutations, and binds reviews to the final SHA. |
| **Budgets** | Item-wide and per-node token, cost, and elapsed-time usage is durable and cumulative. Missing attribution or an exhausted limit fails closed; raising a paused limit is explicit. |
| **Retries** | The same branch, worktree, and checkpoint survive failure. An unchanged repeated failure pauses instead of spinning forever. |
| **Live control** | Steering, pause, resume, interrupt, and recovery are durable idempotent events reconciled against the exact Pi turn ledger. |
| **Recovery** | Unknown stays unknown. No supervisor or doctor automatically kills, relaunches, prunes, discards, promotes, or merges. Confirmed repair preserves branches, worktrees, session evidence, and recovery holds. |
| **GitHub** | PR state and checks are bound to repository, PR, base ref/SHA, and exact reviewed head SHA. Pending, failed, green, moved, closed, outage, and rate-limit evidence never collapse into “success.” |
| **Memory** | Operational memory is narrow, explicit, keyed, and deduplicated. Project knowledge becomes a reviewed `AGENTS.md` work item—not a transcript dump or direct hidden write. |
| **Away mode** | Entry requires a healthy supervisor/recovery gate and a live worker. Away mode can finish work but cannot merge, discard, hide decisions, or answer an external approval gate. |

Delivery is a branch by default. A project may instead use direct PR delivery or native
`high-assurance`, which adds protected-path enforcement, full verification, and two fresh
correctness/adversarial reviews bound to the exact final commit.

## Which Firstmate should you use?

This repository keeps the operating contract of
[the original Firstmate](https://github.com/kunchenguid/firstmate), but it is not a
feature-for-feature fork.

| Choose | When it wins | What you trade |
|---|---|---|
| **Firstmate Graph (this repo)** | You want **one Pi thread → deterministic workflow → persistent Herdr crew**, with hard budgets, exact-SHA evidence, conservative recovery, and no standing merge authority. | It is early, Herdr/Pi-specific, and managed production is currently macOS-only. |
| **[Original Firstmate](https://github.com/kunchenguid/firstmate)** | You need a broader agent distro: Claude/Grok/Pi/Codex/OpenCode/Cursor harnesses, tmux/Herdr/Zellij/Orca/cmux backends, local or remote secondmates, dispatch profiles, and Relay/X/Discord/voice operations. | A larger toolchain and operating surface; it is not the narrow Pi Graph control plane built here. |
| **Plain Pi/Codex sessions** | One small task, one repository, and you are happy to supervise it directly. | You remain the scheduler, retry loop, evidence binder, and recovery system. |

The original is broader, more mature, and more deeply exercised across platforms. This repo is
more opinionated. For the specific job “one thread, agent orchestrates,” that focus is the point;
it is not a claim that this project is better at every kind of fleet operation.

## Compatibility and boundaries

| Surface | Verified scope |
|---|---|
| Captain UX | Persistent Pi session by default; Claude Code and Codex launch routes are available. |
| Production workers | Real persistent Pi agents in Herdr tabs only; no headless production fallback or fixture identity. |
| Platforms | CLI and deterministic tests: macOS + Linux. Managed sandboxed workers: macOS only. |
| Forge | GitHub-first PR lifecycle and promotion. No GitLab adapter without a real registered GitLab project. |
| External no-mistakes | Optional, pinned, signed-macOS adapter boundary; never installed, initialized, updated, or represented as verified by this repo. |
| Deliberate non-goals | Extra terminal backends, remote secondmates, Relay/X/Discord/voice, silent self-update, destructive automatic repair, and standing auto-merge. |

If Herdr, the sandbox boundary, an exact identity, a model, a budget receipt, a reviewer, or
network evidence cannot be proven, production work stops without converting uncertainty into
success.

## Evidence

The deterministic suite covers the end-to-end one-thread story plus process death at workflow
phases, Herdr restart, duplicate/racing controls, corrupted state, stale identities and claims,
dirty trees, base movement, post-review mutation, reviewer loss, exhausted budgets, forge
outages/rate limits, and restart recovery.

```sh
python3 -m unittest discover -s tests -v
```

Local verification on 2026-08-24:

```text
Ran 225 tests in 196.909s

OK (skipped=1)
```

The one skip is the explicitly opt-in real-model test. The default suite makes no model calls.
CI runs it on Ubuntu and macOS with Python 3.10 and 3.13.

- [`tests/test_one_thread.py`](tests/test_one_thread.py) runs six tasks across three repos with
  two workers, a real decision pause, one inbox, and no merge before captain approval.
- [`docs/evidence/interactive-session.md`](docs/evidence/interactive-session.md) records a real
  interactive failure, honest report, retry, exact ready commit, and captain-approved merge.
- [`docs/evidence/live-run.md`](docs/evidence/live-run.md) records the opt-in real Pi Graph/model
  path: 18 seconds, 11,320 tokens, $0.005024 on the dated model shown in the receipt.
- A clean `python:3.13-slim-bookworm` container with no host mounts ran the published installer
  and the installed `pi-firstmate --help` successfully; it correctly surfaced the deliberately
  absent Pi CLI as the next prerequisite.
- [GitHub Actions](https://github.com/ali-abassi/firstmate-graph/actions/workflows/tests.yml) is the
  current cross-platform receipt; the badge at the top reflects its latest default-branch run.

These receipts prove the paths they name. They do not make this broadly field-tested software.

## Trust, recovery, and external gates

Managed implementers run behind two pinned macOS sandbox profiles. The outer profile confines
Pi writes to its worktree and launch state; model-invoked shell commands and final verification
run inside a second no-network, bounded profile that blocks unrelated home/control-plane reads,
process signalling, and Git-metadata writes. Each launch must prove a forbidden write is denied
before its identity is accepted. This is **not** a separate OS account.

`pi-firstmate doctor` is byte-for-byte read-only. A repair plan changes nothing unless both
`--repair` and `--confirm` are present; it rechecks evidence under lock, quarantines corrupt
derived state, and retains project work. It never logs in, installs tools, guesses a choice,
kills an unknown PID, runs broad `git worktree prune`, or deletes unlanded work.

<details>
<summary><strong>External no-mistakes boundary</strong></summary>

The native careful workflow is named `high-assurance`; legacy local `no-mistakes` records read
back compatibly under that name. It is not the separate no-mistakes product.

The optional adapter pins no-mistakes v1.57.1 at tag SHA
`a6f64fcdb4e82c0ddbbd9f01ed91e97dd233d42d`. It accepts only the matching signed macOS release
after architecture, SHA-256, embedded build, Developer ID, hardened-runtime, timestamp, and
repository/database binding checks. Firstmate Graph never installs, initializes, updates, syncs,
resets, aborts, or repairs it.

An external delivery succeeds only from one authoritative SQLite receipt matching the exact
repository, branch, base, head, build, review approval, push target, PR, and persisted CI state.
An ambiguous run, changed head, moved base, closed PR, missing database evidence, or outage is not
success. Configured Firstmate Graph token/cost/time budgets reject external execution because the
pinned external product cannot prove those limits to this controller. Deterministic adapter tests
use an explicit fake seam; real local integration remains unproven unless the complete attestation
passes on that machine.

</details>

## Project status

Early and opinionated. Used daily by one person; not yet battle-tested like the original
Firstmate. The safety posture prefers an explicit stop and preserved work over a clever recovery
that cannot prove what it owns.

Read next: [CLI reference](docs/cli.md) · [control-plane architecture](docs/control-plane.md) ·
[terminal banner contract](docs/terminal-banner-contract.md) ·
[interactive evidence](docs/evidence/interactive-session.md)

MIT licensed.
