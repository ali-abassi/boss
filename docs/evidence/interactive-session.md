# Control-plane provenance — interactive session

This receipt was captured on 2026-08-21 in the Firstmate Graph lineage that BOSS forked at
`7eb63860f022e1eada632cf7ccb5c09bb35c8ea6`. It proves the inherited implementation, failure,
retry, verification, and explicit-promotion path. It does **not** claim that the renamed BOSS
launcher or COO prompt was used in this earlier session; BOSS-specific evidence is recorded
separately.

> Commands shown are the First Mate's internal tooling; the Captain only talked.

`pi-firstmate` ran in a tmux pane with background workers, a real model
(`openai-codex/gpt-5.5`), and a scratch repository registered as `fizz` in `local-only` mode
with authority 3.

| Captain said | First Mate did |
|---|---|
| *hi, who are you and what can you do for me here?* | Introduced itself as the liaison: queues work through `helm`, never edits registered projects, promotes only on the Captain's explicit word. |
| *in the fizz project, implement fizzbuzz(n) … queue it and tell me when it's done.* | Ran `helm task fizz "…"`, checked `helm inbox` / `helm show`, and reported **queued — “It hasn't run yet, so I'm not claiming completion.”** |
| *what happened with the fizz task? give it to me straight.* | Read the evidence and reported plainly: **“It failed.”** All three attempts failed at `verify` because the worktree lacked the repository's `.venv`. No promotion. |
| *(controller bug fixed; workers restarted)* *retry once more and report with evidence.* | Retried once, waited for the worker, then reported that implementation and verification passed, `1 passed`, commit `f8aa99b`, branch ready—**“It is ready but not promoted or merged.”** |
| *looks good. merge it.* | Ran `helm promote … --confirm`; `main` fast-forwarded to `f8aa99b`. |

Outside the session, `git log` showed the commit on `main`, the working tree was clean,
`pytest` passed, and the item state was `merged`.

## What the failure taught

A fresh Git worktree contains tracked files only. The repository's untracked `.venv` was absent,
so the auto-detected test command could not run. The controller now links known ignored dependency
trees (`node_modules`, `.venv`, `venv`, `vendor`, `.tox`, `target`) into each worktree and gives Git
an excludes file so `git add -A` cannot commit those links. The regression is covered by
`test_ignored_dependency_dirs_are_linked_into_worktree_not_committed`.

See also the [real-model workflow receipt](live-run.md).
