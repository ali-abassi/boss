# Under the hood: `bossctl`

The BOSS drives a small CLI, `bossctl`, so the boss never has to. Everything below
is what the agent (or a curious developer) uses; `pi-boss` is the only user command.

```
bossctl setup [--import-login]        own Pi home + Codex login (pi-boss does this on first run)
bossctl add PATH [--id ID] [--mode local-only|direct-pr|high-assurance] [--gate native|no-mistakes] [--authority N] [--test CMD] [--protected GLOBS] [--base BRANCH]
bossctl set ID [--mode M] [--gate G] [--authority N] [--test CMD]
bossctl projects
bossctl task PROJECT "request" [--kind ship|scout] [--scope GLOBS] [--model PROVIDER/MODEL] [--thinking high] [--max-tokens N] [--max-cost N] [--max-seconds N]
          [--node-max-tokens NODE=N] [--node-max-cost NODE=N] [--node-max-seconds NODE=N]
bossctl work [--all] [--summary] · bossctl show ID · bossctl inspect ID · bossctl inbox [--hints]
bossctl steer ID "guidance" · bossctl pause ID · bossctl resume ID · bossctl interrupt ID · bossctl recover ID [--request-id KEY]
bossctl budget ID [--tokens N] [--cost N] [--seconds N]
          [--node-max-tokens NODE=N] [--node-max-cost NODE=N] [--node-max-seconds NODE=N]
bossctl scope ID "src/api/**,tests/api/**" · bossctl wait ID 20m "reason"
bossctl respond ID "boss's answer" · bossctl retry ID · bossctl cancel ID [--discard]
bossctl comment ID "note" · bossctl diff ID · bossctl logs ID [--lines N] | bossctl logs --worker [--lines N]
bossctl promote ID --confirm
bossctl up [--workers N] · bossctl down · bossctl status · bossctl watch [--once] · bossctl tail ID
bossctl daemon · bossctl run-once
bossctl supervise [--no-herdr] · bossctl wakes [--claim --consumer ID | --ack IDS | --release IDS]
bossctl forge ID | bossctl forge --all
bossctl gate-status ID
bossctl gate-reconcile ID
bossctl gate-respond ID --action approve|fix|skip [--findings IDS --instructions TEXT]
bossctl memory operational list|get KEY|set KEY VALUE|remove KEY --confirm
bossctl memory project list PROJECT|set PROJECT KEY VALUE|remove PROJECT KEY --confirm
bossctl away-mode on|off|status
bossctl dispatch
bossctl doctor [--offline] [--probe]
bossctl doctor --repair --confirm [--offline] [--auth-source PATH --auth-provider PROVIDER]
            [--model PHASE=PROVIDER/MODEL] [--test PROJECT=COMMAND] [--fetch PROJECT]
bossctl boss [pi|claude|codex]
```

Legacy project records may use the on-disk alias `no-mistakes`; `bossctl/modes.py` defines `LEGACY_HIGH_ASSURANCE = "no-mistakes"`, and `normalize()` maps it to `high-assurance`. `bossctl projects --json` may show either string for old records. Both strings mean the same thing: two independent reviews plus the no-mistakes gate.

`bossctl comment ID "…"` appends a durable note to a work item's history WITHOUT a state
transition or requeue — the in-band channel for a question or context that should reach
the next implementer/reviewer without living only in chat and without forcing `respond`
(which requeues). `bossctl diff ID` prints the item's worktree diff against its project's
base branch. `bossctl logs ID` lists an item's run directories and shows the latest
transcript (the live Herdr session log when present, else the newest run artifact);
`bossctl logs --worker` tails the daemon's own log instead. `bossctl work --summary --json`
returns compact records (id/project/status/kind/phase/attempts/timestamps only) — no
history, comments, or failure notes — for scripts that only need a cheap overview.

## Model dispatch

`bossctl dispatch` prints the resolved `dispatch.json` (defaults + rules); `bossctl
dispatch --set PHASE=provider/model` edits one phase's default model in place. Every
resolved model is validated against the live `pi --list-models` inventory before use
(`bossctl/dispatch.py:assert_available`) — an unavailable model fails closed rather than
silently substituting. The default dispatch is intentionally opinionated: every phase
(`plan`, `implement`, `review_correctness`, `review_adversarial`, `scout`) runs on
`openai-codex/gpt-5.6-sol` so review always lands on an independent model turn.

To route a phase to a different provider (for example DeepSeek via a `baseten`
provider already registered in `~/.pi/agent/models.json`), add a rule to
`~/.boss/dispatch.json`:

```json
{
  "rules": [
    {"name": "scout-deepseek", "kind": "scout",
     "models": {"scout": "baseten/deepseek-ai/DeepSeek-V4-Pro-0813"},
     "thinking": {"scout": "high"}},
    {"name": "default-ship", "kind": "ship"}
  ]
}
```

Rules are matched top to bottom on `kind` (`ship`|`scout`), an optional `project` id
regex, and required `labels`; the first match wins. A rule only needs to override the
phases it changes — `resolve()` merges it over the file's `models`/`thinking` defaults,
which merge over `DEFAULT` in `dispatch.py`. Any phase left without a resolved model or
an invalid `thinking` level raises `BossError` instead of silently falling back.

### Codex subscription tier — a documented assumption, not a detected one

The default dispatch pins every phase to `openai-codex/gpt-5.6-sol`, meaning every task,
review, and scout consumes Codex CLI capacity under whatever ChatGPT/Codex subscription
`bossctl setup --import-login` picked up. **BOSS does not detect or check your Codex plan
tier** (Free/Plus/Pro/Team/Enterprise) — there is no public API for that, so this is not
a deferred feature, it is a known, permanent limitation. A `high-assurance` or
`direct-pr` item that fans out to two review phases plus implementation can burn through
a lower tier's rate limits faster than a `local-only` item would. If tasks start failing
with Codex rate-limit errors, either route some phases to a non-Codex provider (see the
cross-provider example above) or reduce concurrency (`bossctl up --workers N`). There is
no automatic warning or refusal for this today — `bossctl doctor`'s `binary:pi` /
`runner:pi-graph` checks confirm the CLI itself works, not that your plan has headroom.

State lives in `$BOSS_HOME` (default `~/.boss`): `projects.json`, `dispatch.json`, atomic
versioned `work/<id>/item.json` records, durable `scope-claims.json`, `supervisor.json`,
`wakes.json`, optional `memory.json`, persistent `worktrees/`, durable `recovery/worktrees/`
journals, evidence bundles, and the
isolated `pi/` login. Fresh directories are created `0700` and state/lock/log files `0600`;
existing permissive paths are reported for explicit confirmed repair rather than silently
chmodded. Unknown, overlapping glob, global, and sensitive claims serialize;
mechanically disjoint path prefixes may run concurrently. Scope escape pauses the item for
approval. A stale claim remains blocking until confirmed reconciliation turns it into a
recovery hold; PID death is never permission to forget unlanded work.

The production path runs implementation in the item's reconnectable Herdr Pi agent and uses
fresh Herdr agents for exact-SHA reviews. Checkpoints retain branch SHA and compact state.
Positive death requires confirmed quarantine and an item-specific `recover`; its one-use
authorization is bound to the old durable session or unresolved implementer launch ID and
reserved before a replacement can start. Unknown liveness refuses replacement. Launch phases
are journaled so a crash between tab creation and final session persistence stays visible.
Likewise, an uncertain prompt acknowledgement pauses as recovery-required and retains the
exact agent tab and collision claim; it is never closed or retried automatically.
The configured base SHA is captured, integrated, and rechecked around final verification and
reviews; any later mutation invalidates approval. Review input over 200,000 bytes fails rather
than being truncated. On macOS, every managed Herdr Pi is started through `libexec/pi` and
`/usr/bin/sandbox-exec`. Its outer profile gives Pi provider access but grants writes only to the
owned implementer worktree (reviewers get no repository write), exact launch paths, one exact
reviewer verdict when applicable, and a private copy of the minimum Pi configuration. Every
model-invoked shell and final verification command is rewritten through a controller-owned,
hash-pinned runner whose nested profile denies network, unrelated private reads, process
signalling, and Git-metadata writes, scrubs the environment, and bounds runtime. A unique
inherited sandbox fingerprint lets settlement stop and reap every matching descendant—even one
that double-forked or detached its session—and the runner fails closed unless absence is proven.
Built-in file tools are scoped to the worktree. The real Pi process must
prove a forbidden write failed before the launch is accepted. A per-launch Ed25519 key signs the
hash-chained, prompt-free input/start/settled ledger. This is not user-account isolation.
Production launch fails closed when Herdr or the boundaries are absent; the checked-in inert
runner is only a deterministic test fixture and is never represented as a production worker.

Item budgets are cumulative across the persistent implementer and all fresh reviewers. Optional
per-node limits independently cover `implement`, `scout`, `review_correctness`,
`review_adversarial`, and the non-model `verify` node (`verify` accepts seconds only). Each model
node keeps cumulative per-session receipts, and every node keeps elapsed-time evidence. Limits
are enforced before, during, and after the node; an already-running provider turn may cross a
threshold before usage is returned. Pause, interrupt, timeout, and budget settlement spend no
extra checkpoint model turn—the preserved worktree is the checkpoint. Missing or historically
unattributable node evidence fails closed. The inert deterministic graph seam refuses per-node
budgets rather than fabricating attribution. `bossctl budget` changes a limit only on a paused,
needs-you, or failed item; a new node limit cannot be applied retroactively after unattributed
history, and `resume` still refuses an exhausted limit.

The `--gate no-mistakes` provider is a separate, optional external transaction—not the native
`high-assurance` graph. It accepts only the pinned v1.57.1 signed macOS release and an already
initialized exact repository binding. BOSS does not install or repair it. The item must be
a ship item in non-local mode with authority at least 2, a clean exact-SHA BOSS verification,
and no configured BOSS item-wide or per-node token/cost/time limit. The request is journaled before one
`axi run`; uncertain requests are never repeated. `gate-status` reads the exact SQLite receipt,
`gate-reconcile` durably records current external evidence without issuing another command, and
`gate-respond` sends one boss decision for the exact observed step. `fix` requires a subset of
the displayed finding IDs; `--instructions` is valid only with `fix`. No adapter command uses
`--yes`. Until a unique exact review/push/open-PR/CI receipt exists, delivery and promotion remain
blocked. Changed external heads require fresh custody and review rather than being trusted.

`bossctl doctor` itself is byte-for-byte read-only: it does not initialize Pi, clean PID files,
fetch, prune, or normalize state. Default network probes compare the configured base to the
exact remote head and check GitHub authentication; `--offline` skips those. `--probe` is the
only token-spending model probe. `--repair` still performs no mutation without `--confirm`,
and every confirmed action revalidates its target under lock. Worker shutdown and repair require
the recorded process start/command identity; bare, legacy, or reused PIDs are not signaled.
Repairs quarantine or reconcile metadata rather than deleting work or closing a reused tab.
Mutation-capable choices must be named on the confirmed command: `--auth-source` copies only the
selected provider record into BOSS's private config; `--model` accepts only an exact model
from Pi's offline inventory; `--test` checks shell syntax and records the command without running
it; and `--fetch` updates only the configured `refs/remotes/origin/<base>` after proving checkout
HEAD and status stayed unchanged. An orphan worktree is moved intact to controller quarantine
and its branch is retained. Doctor never logs in, installs, invents a replacement, merges,
updates a checkout, or treats network uncertainty as repaired.

Doctor never invokes broad `git worktree prune`. For a non-running item with no lease and
positively absent/dead Herdr identities, it can distinguish a canonical directory whose exact
Git admin entry vanished from an arbitrary broken checkout. Confirmed repair journals the
detached directory's owner/inode, payload manifest, missing admin path, branch ref, and exact SHA;
moves that complete directory into retained private custody; creates a separate no-checkout
registration; rebuilds only its index at the unchanged SHA; restores and verifies the payload;
then atomically moves the registration to the canonical path. The journal resumes after process
death at every phase. Changed branch, files, ownership, paths, or liveness stop recovery while all
copies remain preserved. The retained source is never automatically deleted. A pruned Git index
cannot prove its old staged-versus-unstaged distinctions, so that metadata is reported as
unrecoverable rather than fabricated.

The GitHub observer reads REST evidence for the registered origin and canonical PR URL, then
checks the PR head, head repository, base ref, reviewed base SHA, and check/status records for
the exact reviewed head SHA. Incomplete pagination, missing SHA fields, outages, and rate limits
remain unknown. Promotion is a restart-recoverable serialized journal: it re-observes green
evidence, uses GitHub's expected-head merge guard, and treats command success as only a request
until GitHub reports that exact SHA merged. Local promotion fast-forwards the immutable SHA.
Only `promote ID --confirm` has merge authority; promotion is refused while away mode or an
unresolved control is active, and post-arm worktree changes are preserved instead of cleaned.
After exact merge proof, cleanup quiesces the exact implementer and removes only the unchanged
clean worktree without force; it retains the merged branch ref as recovery evidence. `cancel
--discard` is likewise an authorization boundary, not data destruction: unlanded worktrees move
intact to controller quarantine and their branch refs remain.
