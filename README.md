<div align="center">

<img src="assets/logo.svg" width="560" alt="BOSS — your AI COO">

<h1>Run your software operation from one thread.</h1>

<p><strong>BOSS is an AI COO for coding work. Set outcomes in one Pi conversation; it coordinates real persistent Pi agents in Herdr, enforces deterministic delivery gates, and brings every consequential decision back to you.</strong></p>

[![tests](https://github.com/ali-abassi/boss/actions/workflows/tests.yml/badge.svg)](https://github.com/ali-abassi/boss/actions/workflows/tests.yml)
[![license](https://img.shields.io/badge/license-MIT-0b0d10)](LICENSE)
[![platform](https://img.shields.io/badge/managed_workers-macOS-b7f34a)](#install)

[Install](#install) · [Operating model](#operating-model) · [Commands](#the-whole-daily-interface) · [Guarantees](#what-code-enforces) · [Firstmate coexistence](#boss-and-firstmate-can-coexist) · [Evidence](#evidence)

</div>

<img src="assets/hero.svg" width="100%" alt="Boss sets outcomes in one conversation. The BOSS COO routes isolated work to real Herdr Pi agents, passes delivery through deterministic tests and exact-SHA review, and returns blockers, decisions, and ready work while Boss keeps merge authority.">

Most multi-agent setups give you more agents—and more agents to manage. BOSS changes the job.
You keep one durable conversation and name the outcomes. Your COO handles intake, isolation,
assignment, live steering, retries, budgets, verification, and status across every registered
repository. You see the whole operation with `/ops`; you see only decisions with `/inbox`.

Nothing merges, gets discarded, or receives a destructive recovery action unless you approve
that exact item.

## Install

> [!IMPORTANT]
> Managed production workers currently require **macOS**,
> [`/usr/bin/sandbox-exec`](https://keith.github.io/xcode-man-pages/sandbox-exec.1.html),
> [Herdr](https://herdr.dev), [Pi](https://github.com/earendil-works/pi), Git, Python 3.10+,
> and a Codex subscription. The controller and deterministic test suite also run on Linux.
> GitHub PR delivery additionally requires `gh`.

```sh
curl -fsSL https://raw.githubusercontent.com/ali-abassi/boss/main/install.sh | bash
```

Open a new terminal, then:

```sh
pi-boss
```

That command opens—or returns to—the persistent `boss` Herdr session. BOSS starts its quiet
schedulers itself; real work items appear as watchable Herdr tabs only when there is real work.
There are no decorative empty worker tabs.

A typical exchange looks like this:

> **Boss:** Add `~/code/api` and `~/code/web`.<br>
> **COO:** Both are registered. I found their test commands; delivery stays local until you ask
> for a different mode.
>
> **Boss:** Fix the flaky login test in API and investigate why the web bundle is 4 MB.<br>
> **COO:** Two independent items are under way. The implementation is isolated from the bundle
> investigation. I’ll bring back failures, questions, and exact work ready for your decision.

The installer creates a private runtime, places `pi-boss`, `pi-boss-quit`, and `bossctl` on your
PATH, and installs the BOSS skill for Pi, Claude Code, and Codex. It can reuse an existing Pi
Codex login, but BOSS keeps its own config and runtime state. It does **not** install Herdr or the
external no-mistakes product, silently update an existing checkout, or claim an integration is
ready without proof.

## Operating model

1. **You set the outcome.** The COO registers projects, separates independent requests, and
   keeps one portfolio-level plan in this conversation.
2. **Every item gets durable custody.** One branch, worktree, scope claim, checkpoint, budget,
   and real Pi implementer identity stay attached through questions, steering, retries, and
   review feedback.
3. **Production agents run in Herdr.** Every active implementer, scout, and reviewer is a real
   Pi agent in a watchable Herdr tab. The tab is the live drill-down; `/ops` is the durable
   cross-project view.
4. **Code orders the work.** Pi Graph executes configured tests, protected-path checks, review
   gates, and delivery checks in an order the model cannot skip.
5. **The decision returns to you.** Ready branches, PRs, blockers, failures, and questions land
   in one inbox. Promotion still requires your item-specific approval and fresh evidence.

Independent, non-overlapping items can run concurrently up to configured worker capacity.
Overlapping, repository-global, protected, or unknown scopes serialize instead of racing.

### Delivery modes

| Mode | What runs | Where it stops |
|---|---|---|
| `local-only` | Isolated implementation plus the project’s configured verification | Ready local branch |
| `direct-pr` | Verification plus one fresh correctness review bound to the exact head SHA | Open PR awaiting your decision |
| `high-assurance` | Protected-path checks, full verification, and fresh correctness + adversarial reviews on the exact final SHA | Reviewed branch or PR awaiting your decision |
| `scout` | Read-only investigation by a persistent agent | Findings only; no delivery path |

The bundled runner is Pi Graph 0.3.0 with two pinned run-bundle compatibility fixes ported from
[Agent Workflows v0.2.0](https://github.com/ali-abassi/agent-workflows/tree/v0.2.0). Agent
Workflows is not downloaded or silently updated at runtime.

## The whole daily interface

| You want to… | Use |
|---|---|
| Open or return to your COO | `pi-boss` |
| Close BOSS and positively verify its Herdr session stopped | `pi-boss-quit` |
| See the complete portfolio inside Pi | `/ops` |
| See only questions, failures, and ready work | `/inbox` |
| Ask the COO to check back in this session | `/wake 20m` |
| Enter or leave gated unattended supervision | `/away on` · `/away off` · `/away status` |
| Read status without entering Pi | `pi-boss status` |
| Run a read-only health/recovery audit | `pi-boss doctor` |
| Stop schedulers but preserve the BOSS session | `pi-boss stop` |
| Use Claude Code or Codex as the COO liaison | `pi-boss claude` · `pi-boss codex` |

You do not need to type internal orchestration commands. Talk to the COO in plain language.
`bossctl` exists for diagnostics, automation, and people who want to inspect the machinery.

## What code enforces

| Boundary | Runtime guarantee |
|---|---|
| **Identity** | A live item keeps one real Pi session UUID, Herdr pane, process-birth fingerprint, and signed lifecycle ledger. A label, tab name, or reused PID is not identity. |
| **Exact delivery** | Integration records the base, checks scope and protected paths, runs verification, rejects test-created mutations, and binds every review to the final commit SHA. |
| **Budgets** | Item-wide and per-node token, cost, and elapsed-time usage are durable and cumulative. Missing attribution or an exhausted limit fails closed. Raising a paused limit is explicit. |
| **Retries** | The same branch, worktree, identity, and checkpoint survive a failure. An unchanged repeated failure pauses instead of spinning. |
| **Live control** | Steering, pause, resume, interrupt, and recovery are durable idempotent events reconciled against the exact Pi turn ledger. |
| **Supervision** | Healthy steady state consumes zero model turns. Deduplicated durable wakes surface `needs-you`, stale, wedged, dead, and unknown states without automatic killing or replacement. |
| **Recovery** | Unknown stays unknown. Doctor never silently kills, relaunches, prunes, discards, promotes, or merges. Its audit is read-only; every repair needs `--repair --confirm`. |
| **GitHub** | PR checks are bound to repository, PR, base ref/SHA, and exact reviewed head SHA. Pending, failed, moved, closed, outage, rate limit, and uncertainty never collapse into green. |
| **Memory** | Operational facts are explicit, narrow, keyed, and deduplicated. Project knowledge becomes a normal reviewed project change—not a hidden transcript dump. |
| **Away mode** | Entry requires healthy supervision and recovery gates. It may finish work, but cannot merge, discard, hide a decision, or answer an external approval gate. |

## BOSS and Firstmate can coexist

BOSS started from the same trustworthy control-plane lineage as
[Firstmate Graph](https://github.com/ali-abassi/firstmate-graph), then became a separate product.
It is not a skin sharing live state.

| Surface | BOSS | Firstmate Graph |
|---|---|---|
| Relationship | COO ↔ Boss | First Mate ↔ Captain |
| Main command | `pi-boss` | `pi-firstmate` |
| Close command | `pi-boss-quit` | `pi-firstmate-quit` |
| Portfolio command | `/ops` | `/fleet` |
| State | `~/.boss` | `~/.helm` |
| Herdr session | `boss` | `firstmate` |
| Work branches | `boss/*` | `helm/*` |

The namespace split is deliberate and covered by tests: BOSS does not focus, read, stop, or
mutate the Firstmate session or state tree.

This focused control plane also differs from
[the original Firstmate](https://github.com/kunchenguid/firstmate), a broader, more mature agent
distribution with multiple harnesses, terminal backends, remote secondmates, and more operating
integrations. Choose BOSS for the opinionated job **one owner thread → real Herdr Pi agents →
deterministic evidence → owner decision**. Choose the original when backend breadth matters more
than this narrow operating contract.

## Evidence

The default suite makes no model calls and exercises the complete one-thread story, process death
through every workflow phase, Herdr restart, racing controls, corrupted state, stale identities
and claims, dirty trees, scope escape, base movement, post-review mutation, reviewer loss,
exhausted budgets, forge outages/rate limits, and restart recovery.

```sh
python3 -m unittest discover -s tests -v
bun .pi/extensions/boss.ts
bun bossctl/pi_attest.ts
python3 -m compileall -q bossctl tests
git diff --check
```

GitHub Actions runs the full deterministic suite on Ubuntu and macOS with Python 3.10 and 3.13.
The badge at the top is the current default-branch receipt; this README intentionally avoids a
test-count claim that will go stale.

- [COO prompt contract and evaluation set](docs/boss-prompt-contract.md)
- [Fresh tool-free COO prompt receipt](docs/evidence/boss-prompt-eval.md)
- [Terminal surface contract](docs/terminal-surface-contract.md)
- [Independent terminal acceptance review](docs/evidence/terminal-review.md)
- [Live Herdr, maximum-content, scroll, and coexistence receipts](docs/evidence/herdr-coexistence.md)
- [Control-plane architecture](docs/control-plane.md)
- [Complete CLI reference](docs/cli.md)
- [Control-plane provenance receipts](docs/evidence/interactive-session.md)

These receipts prove only the paths they name. BOSS is early and opinionated, not broadly
field-tested. Its default posture is to stop with work preserved when identity, liveness,
review, budget, sandbox, or network evidence cannot be proven.

<details>
<summary><strong>Optional external no-mistakes boundary</strong></summary>

The native careful workflow is named `high-assurance`; legacy local `no-mistakes` records read
back compatibly under that name. It is not the separate no-mistakes product.

The optional adapter pins no-mistakes v1.57.1 at tag SHA
`a6f64fcdb4e82c0ddbbd9f01ed91e97dd233d42d`. BOSS never installs, initializes, updates, syncs,
resets, aborts, or repairs it. Availability and evidence fail closed unless the exact signed
macOS build and authoritative repository-bound receipt pass the complete attestation boundary.

</details>

## License

MIT. See [LICENSE](LICENSE).
