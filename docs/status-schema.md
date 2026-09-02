# Status/inbox JSON contract

`bossctl status --json`, `bossctl inbox --json`, and `bossctl work --json`/`--summary --json`
are the three read shapes external consumers (the Pi extension, a dashboard, a script)
should pin to. This documents the CURRENT shapes exactly as implemented
(`bossctl/cli.py:cmd_status`, `cmd_inbox`, `cmd_work`) — it does not change any code.

## `bossctl status --json`

```jsonc
{
  "workers": 12345,               // FIRST live worker pid, or null if none is running
  "worker_count": 2,              // how many workers are positively live
  "herdr_tabs": [ /* raw tabs array from ~/.boss/herdr.json, [] if absent */ ],
  "projects": 3,                  // count of registered projects
  "projects_unavailable": ["web"],// registered ids whose path is no longer a Git checkout
  "items": {"queued": 2, "running": 1, "ready": 1},  // status -> count, only statuses present
  "supervisor": {
    "healthy": true,
    "last_scan": "2026-08-24T12:00:00Z",
    "classifications": {"unknown": 1},   // observation classification -> count
    "pending_wakes": 0,                  // unacknowledged wake events
    "away": false
  },
  "planning": { /* see `bossctl planning status`; advisory pulses, off by default */ }
}
```

`workers` is the first live pid rather than a count, so it stays a truthy
"schedulers are up" signal for existing consumers; use `worker_count` for the number.
Both are derived from live process identity, not from the presence of `daemon.pid`: a
stale ledger entry whose process is gone is not counted.

`projects_unavailable` lists registered projects whose recorded path no longer contains a
`.git` entry. The underlying per-project `available` flag is derived by
`registry.load(check_paths=True)`, which only the rendering/gating callers ask for
(`status`, `projects`, the board); the daemon poll and queue claiming load the registry
without stating any path, so a project on an unresponsive mount cannot block them. The registration is deliberately kept (restoring the path restores the
project), but `bossctl task` refuses new work for those ids, `bossctl projects` marks the
row, and the board prints a warning line. An empty list is the healthy case.

If the supervisor ledger itself is malformed, `supervisor` degrades to
`{"healthy": false, "error": "...", "pending_wakes": null, "away": null}` instead of raising —
callers must check `supervisor.healthy` before trusting `pending_wakes`/`away`.

Durable state that cannot be read at all is a different case from state that is merely
absent: an unreadable or malformed `projects.json`, `daemon.pid`, `herdr.json`, or
`work/*/item.json` raises a `BossError` naming the file rather than being reported as
"nothing registered", "no workers", or an item that quietly vanishes from the portfolio.

## `bossctl inbox --json`

A raw JSON **array** (not `{"items": [...]}`) of work items whose `status` is one of
`needs-you`, `failed`, `ready`, `pr-open` — i.e. the subset that needs a human. Each
element is a full (redacted) work-item record; see `bossctl show ID --json` for the full
item shape. `--hints` only affects the human-readable (non-JSON) output.

## `bossctl work --json` / `bossctl work --all --json`

A raw JSON **array** of full (redacted) work-item records. `--all` includes closed items
(`done`, `merged`, `cancelled`, `failed`); without it, only `work.OPEN` statuses are
returned: `queued`, `running`, `paused`, `needs-you`, `ready`, `pr-open`.

## `bossctl work --summary --json` / `bossctl work --all --summary --json`

A raw JSON array of COMPACT records — no `history`, `comments`, `failure_notes`, or run
logs. Only these keys: `id`, `project`, `status`, `kind`, `phase`, `attempts`,
`max_attempts`, `created`, `updated`. Use this for scripts that only need an overview;
a full item record can be hundreds of KB.

## Shared conventions

- Every list-of-items endpoint returns a **raw JSON array**, never `{"items": [...]}`.
  (`bin/pi-boss-quit`'s in-flight-work check had a bug assuming the envelope shape — fixed;
  see the mission progress notes. Any new consumer should match the array shape, not guess.)
- `control.redact()` is applied to every work-item record before it leaves `bossctl`
  (`bossctl/control.py:redact`). It recurses through the whole structure: dict values
  under a secret-named key become `"[REDACTED]"` wholesale, and every OTHER string value
  (including inside `history`/`comments`/`failure_notes` text) is scanned against known
  secret-value patterns (API keys, tokens, credential-bearing URLs) and redacted in place.
  Free-text fields are still user-authored and not otherwise sanitized — treat them as
  containing whatever the boss or an implementer typed.
- There is no version field on these shapes yet. A consumer that needs forward
  compatibility should treat unknown keys as ignorable and missing keys as absent (not an
  error), matching how `bossctl doctor`'s own `_claims_schema`/`_tabs_schema`/`_item_schema`
  validators are written.
