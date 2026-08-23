# Persistent control plane

Each work item has one durable JSON record, branch, worktree, scope claim, compact checkpoint,
and Herdr implementer identity. Records are atomically replaced under per-item locks. An alive
Herdr agent is reattached only after lifecycle, session, model, and thinking metadata validate;
a dead agent starts a replacement from the saved checkpoint. Correctness and adversarial
reviewers always use fresh identities.

## Safety pipeline

Normal Herdr execution and the explicit headless/test fallback share the same final pipeline:

1. integrate the latest configured base without overwriting a dirty checkpoint;
2. require a clean tree, declared-scope compliance, and protected-path compliance;
3. run the complete configured test command and reject test-created mutations;
4. record fresh review evidence against the exact final commit SHA; and
5. persist the clean worktree fingerprint and base SHA used for delivery.

A changed commit, uncommitted file, untracked file, or moved base invalidates approval. Scouts
are read-only; any mutation fails while preserving the worktree for inspection. Failed,
interrupted, paused, and blocked items retain all unlanded work. Cleanup that can discard work
requires item-specific captain authorization.

## Scheduling and controls

Declared path globs are claimed atomically. Mechanically disjoint prefixes may run together;
unknown, overlapping, protected, lockfile, workflow, and repository-global scopes serialize.
Dead-process claims are reclaimed. Scope escape interrupts execution and asks before expansion.
Rigor starts as scout, quick, standard, or high-risk with a recorded rationale and escalates on
sensitive paths, expanded scope, or failed verification.

`steer`, `pause`, `resume`, `away`, `interrupt`, and `recover` are durable events. Pending events
are consumed exactly once at an agent boundary. Inspection exposes phase/state, activity,
branch/SHA, scopes, tests, reviews, blockers, frozen model/thinking rationale, usage evidence,
controls, rigor, and recent output through recursive secret redaction. Away mode can reach
merge-ready but cannot promote; promotion always requires explicit confirmation for that item.

## Herdr launcher

`pi-firstmate` is canonical. Outside Herdr it creates or attaches the named `firstmate` session
and launches itself there. Inside Herdr it runs directly, preventing recursion. If Herdr is
unavailable it exits without touching work; `PI_FIRSTMATE_HEADLESS=1` explicitly selects the
non-persistent fallback, which still uses the same safety pipeline.
