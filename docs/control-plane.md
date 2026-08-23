# Persistent control plane

Each work item has one durable JSON record, branch, worktree, scope claim, compact checkpoint,
and Herdr implementer identity. Records are atomically replaced under per-item locks. An alive
Herdr agent is reattached only after lifecycle and durable session identity validate. Model,
thinking, token, and cost evidence is attested from the Pi session JSONL when Herdr's generic
agent record does not carry it; missing or conflicting evidence fails closed. Unknown or dead
identity never triggers automatic replacement. A one-use captain recovery authorization is
bound to the old session or unresolved implementer launch ID, reserved before launch, and
consumed when the real replacement identity is persisted. A durable launch journal separates
reservation, tab creation, attestation, and finalization, so a controller crash cannot turn an
unfinished launch into permission to create a duplicate. Correctness and adversarial reviewers
always use fresh identities.

Herdr's canonical `pi` kind is bound in the new pane to `libexec/pi`; relying on inherited
`PATH` is explicitly insufficient because interactive shell startup may replace it. The wrapper
executes the real pinned path through macOS `sandbox-exec`. An implementer outer profile permits
writes only to its owned worktree plus exact per-launch session, attestation, temporary, and
private Pi-configuration directories. A reviewer cannot write the repository and receives at
most one exact verdict-file capability. Pi retains provider network access, but every
model-invoked shell and final verification command is rewritten through `libexec/firstmate-tool`
with a second hash-pinned profile. That nested profile denies network, unrelated home/controller
reads, process signalling, and Git-metadata writes; its environment is scrubbed and its runtime is
bounded. A unique inherited sandbox fingerprint identifies the complete descendant tree even
after double-fork/session detachment; settlement stops and reaps every matching process to a
fixed point and fails closed when that absence cannot be proven. Built-in file tools are
scoped to the worktree by the controller-owned extension. Pi's startup hook must demonstrate
that an out-of-capability write receives `EPERM`/`EACCES` before the controller accepts the
session. Each launch creates an in-memory Ed25519 key and signs a monotonic hash chain of
input/start/settled metadata; prompt text is never stored in that ledger. Controller validation
pins the public key, file ownership, inode, link count, pane PID plus process-start/command/group
fingerprints, model, thinking level, session UUID, worktree, and both profile digests. This is not a separate user account, and production
Herdr work fails closed off macOS or without these prerequisites.

## Safety pipeline

Production Herdr execution and the checked-in deterministic fixture exercise the same structural
final pipeline. The fixture is test-only and never supplies a production session identity:

1. capture the configured base SHA and integrate exactly that base without overwriting a dirty checkpoint;
2. require a clean tree, declared-scope compliance, and protected-path compliance;
3. run the complete configured test command and reject test-created mutations;
4. recheck that the configured base did not move, then record fresh review evidence against
   the exact base and final commit SHA; and
5. persist the clean worktree fingerprint and base SHA used for delivery.

A review input larger than 200,000 bytes fails closed; it is never silently truncated. A
changed commit, uncommitted file, untracked file, or moved base invalidates approval. Scouts
are read-only; any mutation fails while preserving the worktree for inspection. Failed,
interrupted, paused, and blocked items retain all unlanded work. Cleanup that can discard work
requires item-specific captain authorization.

## Scheduling and controls

Declared path globs are claimed atomically. Mechanically disjoint prefixes may run together;
unknown, overlapping, protected, lockfile, workflow, and repository-global scopes serialize.
Dead-process claims stay blocking. Confirmed doctor reconciliation can turn one into a recovery
hold, preserving collision safety until the same item explicitly recovers. Scope escape
interrupts execution and asks before expansion.
Rigor starts as scout, quick, standard, or high-risk with a recorded rationale and escalates on
sensitive paths, expanded scope, or failed verification.

`steer`, `pause`, `resume`, `interrupt`, and `recover` are durable per-item events. Away mode is
one separately gated global supervisor state. Legacy `controls.away` data remains read-compatible
but creates no command, event, or authority and is never consulted by the runtime. An
optional request ID makes retried/racing delivery idempotent. Live steering
is submitted without waiting on an unrelated active turn. Pause and interrupt stop the active
turn, wait for settlement, and then persist the paused state without starting another model turn;
the preserved worktree is the checkpoint. Pending events are
claimed and reconciled against the exact Pi turn ledger so a crash after accepted input does
not send the same steering twice. Inspection exposes phase/state, activity,
branch/SHA, scopes, tests, reviews, blockers, frozen model/thinking rationale, usage evidence,
controls, rigor, and recent output through recursive secret redaction.

Pi input is correlated using the same outer-whitespace canonicalization that Herdr submits.
Once prompt submission begins, missing or malformed acknowledgement is `recovery-required`,
not an ordinary retryable failure: the scope claim and exact implementer/reviewer tab remain
held until explicit reconciliation proves what stopped.

Token, cost, and elapsed-time usage is durable and cumulative across the persistent
implementer and every fresh reviewer. The item-wide envelope is supplemented by explicit
per-node envelopes for implementation/scouting, each independent reviewer, and verification.
Model nodes use cumulative Pi-session receipts; verification accepts only elapsed-time limits.
Limits are checked before, during, and after each node. Because providers report usage after a
turn, one already-running turn can cross a threshold. The controller then settles without
spending another checkpoint turn and refuses another turn until the captain explicitly raises
the paused item's budget and resumes it. Missing or legacy-unattributable node evidence fails
closed instead of being recorded as zero. Completed node receipts are durably checkpointed as
the pipeline advances. Every load and mutation validates finite limits, non-negative usage,
boolean evidence flags, and receipt totals; non-standard NaN/Infinity JSON cannot be written.
Confirmed dead-process reconciliation harvests the open lease exactly once
through a receipt key and accounts its active node before recovery can create another session.

## Supervisor, recovery, and away mode

Item transitions feed private, fsynced atomic supervisor records and a durable wake queue. A watchdog uses only files,
PIDs, clocks, Git/Herdr metadata, and network APIs—never a model—to classify each item as
`healthy`, `waiting`, `needs-you`, `stale`, `wedged`, `dead`, or `unknown`. Unknown evidence is
preserved as unknown. A live Herdr name/pane is healthy only when the durable Pi session UUID's
signed runtime attestation names the exact process birth currently running in that pane; a reused
numeric PID or display coordinate remains unknown. Durable wake keys deduplicate concurrent observers and survive daemon,
Herdr, and Pi restarts; claims have leases so a UI crash cannot lose a wake. Declared waits
resurface after their deadline. Repeated unchanged stale evidence escalates to a bounded wedge
reminder. The supervisor cannot kill, launch, release, discard, promote, or merge.

The same exact identity check gates prompt submission, steering, cooperative interrupt, and tab
closure. If the name, pane, UUID, process-birth fingerprint, or durable session evidence no longer agrees, the
operation fails as unknown and retains recovery custody rather than acting on a replacement tab.

Doctor audits tools, worker PIDs, tabs, leases, claims, session identities, JSON state,
worktrees, auth, models, test commands, clone freshness, graphs, and the optional gate boundary.
It is read-only unless both `--repair` and `--confirm` are present. Repair backs up corrupt
derived state, pauses positively dead execution, archives identity evidence, and retains
worktrees/branches/recovery holds. Worker ownership requires a recorded PID, process-group,
start fingerprint, and command fingerprint; legacy, unknown, or reused PIDs are never signaled.
Every repair rechecks its evidence under the relevant lock. It never deletes project work,
closes a reused live tab, or starts a process. Choices with mutation authority must be named on
the confirmed command. `--auth-source` copies one selected provider record into First Mate's
private config without logging in; `--model` accepts only an exact entry in Pi's offline
inventory; `--test` validates shell syntax and records the command without executing it; and
`--fetch` fetches only the exact configured origin/base remote ref after proving checkout HEAD
and status were preserved. Orphan worktrees move intact to controller quarantine with branches
retained. Repair never installs dependencies, invents a choice, merges, updates a checkout, or
treats uncertain evidence as success.

There is no automatic `git worktree prune`: an inaccessible sibling can look stale to Git even
while it contains live unlanded work. If one exact quiescent item's canonical directory remains
but its project-local admin entry is positively absent, doctor can offer a confirmed
reconstruction. It hashes the owner-controlled directory and payload, pins the unchanged branch
ref/SHA and missing admin target, and durably journals before moving anything. The complete
detached source moves atomically into retained private custody; a distinct no-checkout worktree
is registered, its index is populated from the pinned commit without moving the branch, and the
source payload is copied and verified before Git atomically reattaches it at the canonical path.
The source remains retained. Each phase is restart-resumable; live/unknown agents, changed branch
or payload, symlink/ownership escape, unexpected staging content, or ambiguous evidence stops
without deleting any copy. The missing admin directory also contained the old index, so its
staged-versus-unstaged distinctions are declared unrecoverable rather than invented.

Global away mode is enabled only after a fresh supervisor scan, a healthy offline doctor gate,
at least one positively live worker, an empty decision queue, and no open promotion, PR-delivery,
cancellation, or external no-mistakes transaction. It suppresses wake delivery,
not evidence: new decisions remain visible and durable, then flush when away mode is disabled.
Work may reach merge-ready while away, but promotion always requires item-specific captain
confirmation and is refused until away mode is off. Away enablement and promotion share the
same authority lock, so neither can race past the other's gate.

## GitHub lifecycle and memory

Open GitHub PRs are observed against the registered origin repository, canonical PR number,
base ref, reviewed base SHA, and exact reviewed head SHA. Check runs and commit statuses must
name that SHA, and paginated totals must prove that the complete set was observed. Protected-base
promotion additionally requires reachable strict branch-protection evidence, a nonempty required
check set, and any required GitHub App binding; missing/wrong-app checks stay pending. Pending,
failed, green, merged, closed, changed-head, moved-base, rate-limit,
outage, and malformed evidence remain distinct. Missing or uncertain network evidence is never
green. The observer only records evidence and wakes the captain; it has no merge authority.

Promotion is a globally serialized, durable transaction journal. Before it arms, it rechecks
authority, project binding, gate availability, controls, away state, exact branch/base/head,
clean fingerprint, and exact-base/exact-head reviews. Local delivery merges the immutable SHA,
not a branch name. A zero exit from GitHub is only a merge request: success is recorded only
after a fresh exact-SHA observation. Restart reconciliation uses current Git or GitHub evidence,
and cleanup refuses to remove a worktree if it changed after arming. After exact merge proof it
quiesces the exact implementer, removes only the unchanged clean worktree without force, and
retains the already-merged branch ref as recovery evidence. Direct-PR delivery has its own
repository/base/head/fingerprint transaction: non-force push, authoritative remote-SHA
observation, and authoritative PR lookup are journaled so a crash cannot blindly push or create
the PR twice; command stdout is never a delivery receipt.

Operational memory is an explicit, keyed, bounded local ledger with inspect/upsert/deduplicate
semantics. Secret-looking values, bulk values, and transcript/session dump keys are rejected.
Project memory never writes a project directly: it queues a scoped, high-assurance `AGENTS.md`
item and goes through the normal tests, exact-SHA reviews, and item-specific promotion path.

## Native high assurance and the external no-mistakes boundary

The native careful workflow is named `high-assurance`. Legacy local `no-mistakes` mode and graph
records read compatibly as that canonical name. The separate no-mistakes product is never used
as an alias or represented as installed evidence.

The optional adapter pins v1.57.1 at tag SHA
`a6f64fcdb4e82c0ddbbd9f01ed91e97dd233d42d`. On macOS it verifies the architecture-specific
official executable hash, exact embedded seven-character build, Developer ID team and bundle
identifier, hardened runtime, signing timestamp, designated requirement, and architecture.
Linux and unrecognized builds fail closed. Readiness additionally requires an externally
initialized, owner-controlled `NM_HOME`, a compatible SQLite schema opened with `mode=ro`,
`query_only`, and `trusted_schema=OFF`, one exact repository registration, matching origin/default
branch, and the exact local no-mistakes gate remote. First Mate does not install, initialize,
update, abort, reset, sync, or repair the external product.

The adapter is an explicit external transaction. Before invoking `no-mistakes axi run --intent`,
it durably records the exact First Mate project policy, clean worktree fingerprint, reviewed head,
base, intent hash, binary attestation, database/repository binding, push-target fingerprint, and
the complete prior matching run-ID set. `request-started` is durable before process creation.
Every later state is observation-only: a missing or ambiguous receipt is never replayed. Command
stdout/stderr are private diagnostic evidence, not success receipts. The adapter reads the
authoritative run and gate rows from SQLite and accepts delivery only when repo, branch,
submitted/base/current head, version/build, exact review approval, inactive push binding, target
kind/fingerprint/ref, open canonical same-repository PR, and persisted CI readiness all agree.
Base movement and any head mutation remain non-success and require explicit custody handling plus
fresh First Mate verification.

Approval gates are surfaced to the captain. `gate-respond` journals one decision, pins the exact
step, validates selected finding IDs, and invokes `axi respond` without `--yes`; a crash or
unchanged gate becomes unknown and cannot be answered automatically again. Open transactions
block ordinary steering, pause/recovery controls, cancellation, and away mode. Real no-mistakes
execution is limited to ship items with authority at least 2 and non-local delivery. Any configured
First Mate token, cost, or time budget rejects the invocation because the pinned external product
cannot provide controller-verifiable enforcement. Tests exercise the transaction with a declared
fake binary/SQLite seam; absence of a locally attested product remains explicitly unproven.

## Herdr launcher

`pi-firstmate` is canonical. Outside Herdr it creates or attaches the named `firstmate` session
and launches itself there. Inside Herdr it runs directly, preventing recursion. If Herdr is
unavailable it exits without touching work; there is no non-persistent production fallback.
Managed Pi sessions install the nested capability boundaries and signed lifecycle ledger
described above. The pane-local binding is verified by the runtime forbidden-write proof;
neither a command name nor a Herdr tab label is accepted as identity evidence. The only
non-Herdr execution seam recognizes the repository's exact checked-in inert fixture and exists
solely for deterministic tests.
