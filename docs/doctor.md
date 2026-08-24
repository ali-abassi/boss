# `bossctl doctor`

`bossctl doctor` runs a read-only audit and reports checks for BOSS's files, workers,
worktrees, projects, and integrations. `bossctl doctor --repair --confirm` can fix a
subset of findings; it does not turn the audit into unrestricted mutation.

## Check IDs

The report uses these check-ID families:

- `binary:{name}` — whether a required external binary is on `PATH`; missing binaries are errors, except missing optional `gh`, which is a warning.
- `boundary:macos-sandbox` — whether the macOS sandbox-exec boundary is ready.
- `boundary:signed-ledger` — whether the signed event ledger is ready.
- `runner:pi-graph` — whether the pi-graph workflow runner responds.
- `gate:no-mistakes` — whether the external no-mistakes gate integration is ready.
- `state:home-permissions` — whether `~/.boss` (or `$BOSS_HOME`) is owner-only.
- `state:record-permissions` — whether individual state records are owner-only.
- `state:projects` — whether `projects.json` parses and how many projects are registered.
- `state:dispatch` — whether `dispatch.json` parses.
- `state:item:{id}` — validity of one work-item state record.
- `state:items` — rollup for work-item records; errors if any item record is broken.
- `state:{name}` — validity of a durable state file, such as supervisor or wakes state.
- `state:memory` — whether operational `memory.json` parses.
- `wake-receipt:{id}` — validity of one wake-delivery receipt; severity is `unknown` when liveness cannot be proven.
- `workers:pid-file` — whether `daemon.pid` is readable and how many worker PIDs are registered.
- `worker:{pid}` — liveness of one worker process: live is `ok`, dead is `error`, and unprovable is `unknown`.
- `tab:{tab_id}` — liveness of one Herdr tab: exactly matched live is `ok`, positively absent is `error`, and unprovable is `unknown`.
- `claim:{work_id}` — liveness of one scope claim: live/held is `ok`, dead is `error`, and unprovable is `unknown`; unsafe identities are errors without inspection.
- `claim-collision:{a}:{b}` — whether two scope claims collide.
- `lease:{work_id}` / `session:{work_id}` — running-item lease and Herdr-session liveness cross-check.
- `launch:{work_id}:{launch_id}` — status of one agent-launch attestation.
- `worktree:{work_id}` — worktree path safety, branch attachment, dirtiness, and presence.
- `worktree-recovery:{work_id}` / `worktree-recovery-candidate:{work_id}` — crash-recovery state.
- `worktree-root` — safety of the shared worktree root directory.
- `worktree-project:{project}` / `worktree-orphan:{project}:{name}` — orphaned worktrees.
- `project:{id}` — project registry entry safety and existence.
- `project:{id}:base` — whether the declared base branch ref exists.
- `project:{id}:checkout` — whether the project's working checkout is dirty.
- `project:{id}:test` — whether the declared test command has valid shell syntax.
- `project:{id}:gate` — whether the project's configured external gate is ready.
- `project:{id}:freshness` — whether the checkout can be compared with origin.

## Severity

- `ok` — verified good.
- `warning` — non-blocking, but worth human attention (for example, an optional binary
  is missing or there is no origin remote).
- `error` — blocking or actively broken; some errors are repairable.
- `unknown` — liveness or state could not be positively proven either way. This is a
  deliberate fail-open-on-inspection state, not a bug or a guess about health.

## Repair

`--repair` without `--confirm` is a dry run. `--repair --confirm` applies available
fixes, optionally with `--offline`, `--auth-source`, `--auth-provider`, `--model`, and
`--test` choices. Only some `error` findings are auto-fixable; many require human
judgment, such as a genuinely dirty worktree.

## Reading `bossctl doctor --json`

The JSON report contains checks with `id`, `severity`, and a message. Use
`bossctl/doctor.py:audit()` as the source of truth for the exact fields.
