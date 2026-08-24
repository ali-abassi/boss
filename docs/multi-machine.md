# Multi-machine operation

BOSS runs on one machine: `pi-boss` starts the persistent local Herdr session named
`boss`, `$BOSS_HOME` (default `~/.boss`) holds local state, and every registered
project must be a local git checkout on that same machine. BOSS has no cross-machine
dispatch today. This is a documented design gap, not a shipped feature.

## Not yet implemented

The following are proposals, not available commands or supported behavior.

### Option A: remote project registration

A future registration could look like `bossctl remote add NAME ssh://host`, with
`bossctl task` dispatching work to a project on another machine over SSH. The project,
worktree, and BOSS state would remain on the remote host rather than being treated as
local paths. Open questions include authority and authorization across a machine
boundary, and worktree/git safety when the checkout and its git remote live elsewhere.

### Option B: independent BOSS instances

Run a separate, independent `pi-boss` instance on each machine. A thin bridge could let
one COO request the other's `bossctl work --summary --json` output for status. This would
provide read-only cross-machine visibility; it would not permit remote mutation. Open
questions include identity, authorization, stale or conflicting status, and how to keep
each instance's authority and state isolated.

The `peer-agent` transport used elsewhere in this environment (SSH over Tailscale,
ngrok fallback) is the proven pattern to reuse if this gets built.
