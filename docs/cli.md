# Under the hood: `helm`

The first mate drives a small CLI, `helm`, so the captain never has to. Everything below
is what the agent (or a curious developer) uses; `pi-firstmate` is the only user command.

```
helm setup [--import-login]        own Pi home + Codex login (pi-firstmate does this on first run)
helm add PATH [--id ID] [--mode M] [--authority N] [--test CMD] [--protected GLOBS] [--base BRANCH]
helm set ID [--mode M] [--authority N] [--test CMD]
helm projects
helm task PROJECT "request" [--kind ship|scout] [--scope GLOBS] [--model PROVIDER/MODEL] [--thinking high] [--max-tokens N] [--max-cost N] [--max-seconds N]
helm work [--all] · helm show ID · helm inspect ID · helm inbox [--hints]
helm steer ID "guidance" · helm pause ID · helm resume ID · helm interrupt ID · helm recover ID
helm away ID on|off · helm scope ID "src/api/**,tests/api/**"
helm respond ID "captain's answer" · helm retry ID · helm cancel ID [--discard]
helm promote ID --confirm
helm up [--workers N] · helm down · helm status · helm watch [--once] · helm tail ID
helm daemon · helm run-once
helm dispatch · helm doctor [--probe] · helm captain [pi|claude|codex]
```

State lives in `$HELM_HOME` (default `~/.helm`): `projects.json`, `dispatch.json`, atomic
versioned `work/<id>/item.json` records, crash-recoverable `scope-claims.json`, persistent
`worktrees/`, evidence bundles, and the isolated `pi/` login. Unknown, overlapping glob,
global, and sensitive claims serialize; mechanically disjoint path prefixes may run
concurrently. Scope escape pauses the item for approval.

The normal path runs implementation in the item's reconnectable Herdr Pi agent and uses
fresh Herdr agents for exact-SHA reviews. Checkpoints retain branch SHA and compact state;
if the live agent is gone, recovery starts a real replacement and supplies the checkpoint
rather than inventing an identity or verdict. The configured base is integrated before the
last verification/reviews, and any later mutation invalidates approval. Graph templates and
the bundled deterministic runner remain the explicit headless/test fallback.
