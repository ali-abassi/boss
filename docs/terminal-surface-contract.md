# Terminal surface contract

Contract version: 1
Project: BOSS
Surface: Persistent Pi session masthead, footer status, working indicator, and slash-command receipts
Artifact type: persistent panel
Audience and usage moment: A technical owner opening one long-lived conversation to delegate and inspect work across repositories
Immediate user question: Am I in BOSS, what is operating now, and where do I inspect or act?
Primary action: Type `/ops` for the complete portfolio; `/inbox` for decisions only
Design thesis: For a technical owner entering active software operations, this surface helps them orient and decide by making identity, portfolio state, and the next inspection action immediately legible. Its visual language comes from one operating spine with two returning loops—the geometric structure of a `B`—rather than executive decoration. Identity, state, and commands never move or disappear. The signature move is the three-row B mark aligned to the operations hierarchy. It remains BOSS as `BOSS · YOUR AI COO`, one status line, and plain commands at narrow width, no color, no motion, or ASCII-only output. We reject nautical imagery, corporate cosplay, giant banners, and ambient spectacle because they compete with live work.
Approved references and qualities to borrow: `docs/brand-brief.md` for the calm/exact brand direction; terminal-experience design doctrine for stable reading order, semantic tokens, bounded art, and graceful degradation; the existing measured-width renderer and transcript-entry approach for scroll/copy continuity
Anti-references and failures to avoid: Firstmate compass/anchor/boat language; crowns, ties, briefcases, fake telemetry, blue-purple gradients, random boxes, emoji-width dependence, and any banner that hides an error or pushes current work away
Source paths or URLs: `/Users/aliabassi/boss/.pi/extensions/boss.ts`; `/Users/aliabassi/boss/bossctl/board.py`; `/Users/aliabassi/boss/bin/pi-boss`; `/Users/aliabassi/boss/docs/brand-brief.md`
Direction decision: use: geometric B operating spine, six-row maximum, semantic theme tokens, explicit status text, `/ops` as visible primary action | avoid: images in the terminal, ambient animation, color-only state, fixed byte-width alignment, nautical residue | prove: wide/narrow/plain/no-color frames fit, current state and action survive, history remains append-only, error receipts remain plain and dominant
Fixed constraints: Maximum six masthead rows at 72+ cells, three rows at 52–71, two rows below 52; all alignment uses display-cell width; `NO_COLOR` removes ANSI without removing meaning; `TERM=dumb` or `BOSS_PLAIN=1` uses ASCII/text; no image protocol; Pi owns input, focus, selection, scrollback, and transcript history
Non-goals: A full-screen dashboard, a custom Herdr sidebar, image-protocol rendering, new keyboard bindings, or changes to agent lifecycle/control-plane behavior

## Runtime envelope

Primary terminal: Ghostty through Herdr 0.8.0 and Pi; source rendering is also exercised non-interactively with Bun
Other required terminals or transports: Redirected/no-color/plain text for CI and logs; no SSH/tmux compatibility claim
Default viewport: 100 × 30 cells
Minimum viewport: 50 × 12 cells
Observed capabilities and evidence: `herdr 0.8.0`, protocol 19, compatible running server, and `config: ok` observed 2026-08-24; current automation is redirected with `TERM=dumb`, `NO_COLOR=1`; Pi extension provides measured-width truncation and transcript entry rendering
Unknown capabilities: Kitty graphics, Ghostty image passthrough, screen-reader behavior, SSH, nested tmux, and third-party Herdr lifecycle integrations; none are required or claimed
Plain-output requirement: Identity, state, and `/ops`/`/inbox`/`/wake` actions remain ordered, copyable ASCII without ANSI, cursor motion, images, or animation

## Information and behavior

Reading order: BOSS identity → “YOUR AI COO / OPERATIONS DESK” role → portfolio/team state → `/ops`, `/inbox`, `/wake` actions → transcript conversation
State vocabulary: team stopped, team ready, N active agents, N running, N queued, inbox clear, N need you, away, tools unavailable
Stable regions: Masthead transcript entry and prior `/ops`/`/inbox` entries never redraw; command order and status vocabulary remain stable
Live regions and update cadence: One footer status value refreshes at most every eight seconds; only durable supervisor events trigger a model turn; unchanged history is not repainted
Keyboard, focus, selection, and scrolling: Pi owns the input line, focus, selection, and scrollback; BOSS adds slash commands through Pi's command registry and transcript entries rather than an alternate screen; no focus is stolen
Normal state: Registered projects, team ready, inbox clear, commands visible
Empty state: Zero projects, team state, inbox clear, commands visible; `/ops` explains how to add a repository
Loading or active state: Running/queued counts in status; stable three-cell progress indicator plus plain working message during a model turn
Success state: Ready/PR-open item appears as `need you` and in `/inbox`; no celebration obscures the decision
Warning state: Durable stale/wedged/budget or pending-decision state appears in inbox/status text
Error or degraded state: Missing controller tools render `team tools missing · re-run install.sh`; command failures use a warning notification with the exact failed action; no art replaces the error
Maximum-content fixture: 12 open items with long project/request/question text, multiple running and actionable states, rendered through the board's width bounds and transcript wrapping

## Visual and rendering system

Semantic tokens and non-color cues: `warning` marks the B identity only; `accent` marks commands and working progress; `muted`/`dim` separate role and state; every status is also explicit text and ordered position
Spacing, borders, symbols, and width rules: Three-row `┏━━╮ / ┣━━┫ / ┗━━╯` B mark at wide width; one measured divider; no nested borders; `truncateToWidth` and `visibleWidth` gate every line; ASCII fallback uses `[B]`, `-`, and `>`
Art role, footprint, and opt-out: Identity/orientation only, maximum three mark rows inside a six-row header; `BOSS_PLAIN=1` or `TERM=dumb` selects the quiet ASCII equivalent; narrow mode removes the mark before content
Motion purpose and budget: Working progress exists only while a model turn is active, updates at 220 ms in a fixed-width three-cell lane, and never moves transcript content
Reduced-motion equivalent: `BOSS_REDUCED_MOTION=1` uses one static `[working]` frame and the same five-second textual status cadence
Color chain: truecolor -> 256-color semantic approximation -> 16-color semantic approximation -> no-color text; exact RGB is never required
Visual chain: no Kitty/image path -> measured Unicode B mark -> ASCII `[B]` heading -> plain labeled text
Glyph chain: unicode box drawing and `◆` -> conservative unicode -> ASCII `[B]`, `>`, `-`
Narrow-mode removal order: B mark detail → divider/padding → operations-desk subtitle → secondary running count; retain `BOSS`, “YOUR AI COO,” decisive status, and primary commands as width permits
Streaming and performance budget: Eight-second coalesced footer poll; five-second working-message change; fixed-width indicator; append-only transcript entries; no full-screen redraw or owned scroll region

## Locked checks

- `TX-C1` — Every masthead line is at most the requested display width and row limits are 6/3/2 at 120/60/50 cells | normal and maximum-content | Unicode/no-color | deterministic rendered frame
- `TX-C2` — Wide masthead visibly reads `B O S S`, `YOUR AI COO`, `OPERATIONS DESK`, current state, and `/ops` in that order | normal | 100×30/default theme | screenshot plus transcript
- `TX-C3` — Minimum masthead retains `BOSS`, `YOUR AI COO`, and decisive state without clipping | active and needs-you | 50×12/no-color | screenshot plus transcript
- `TX-C4` — `TERM=dumb`/`BOSS_PLAIN=1` emits ASCII-only identity, divider, state, and actions with no ANSI or ambiguous-width dependency | degraded/plain | 80×24 | byte/transcript inspection
- `TX-C5` — `NO_COLOR=1` removes ANSI while status meaning and command labels remain unchanged | normal, warning, error | 80×24 | transcript comparison
- `TX-C6` — Reduced-motion mode uses one static progress frame; normal progress frames have identical display width and exist only during an active turn | active | default and reduced motion | deterministic test plus source/runtime capture
- `TX-C7` — `/ops` and `/inbox` append stable transcript entries and do not consume a model turn or steal input focus | normal and maximum-content | actual Pi session | interaction transcript
- `TX-C8` — Missing tools and command errors remain explicit plain text/notifications and visually outrank identity art | degraded/error | minimum/no-color | screenshot plus transcript
- `TX-C9` — Footer polling changes only the footer; previous conversation and board entries remain selectable/copyable and do not jitter during updates | active/rapid update/scroll-away | actual Pi session | operated recording/transcript
- `TX-C10` — BOSS launches in its own Herdr session and never closes, focuses, reads, or mutates the Firstmate session | coexistence | actual Herdr runtime | topology/process receipt

## Evidence and results

Wide/default evidence: Actual 113-column Herdr/Pi pane plus deterministic 100-column frame and source-rendered 100×30 PNG in `docs/evidence/herdr-coexistence.md`, `docs/evidence/terminal-render.txt`, and `docs/evidence/screenshots/default-100x30.png`
Minimum-viewport evidence: Deterministic 50-, 32-, and 12-column display-cell checks plus the source-rendered 50×12 no-color PNG in `docs/evidence/terminal-render.txt` and `docs/evidence/screenshots/minimum-50x12-no-color.png`
No-color evidence: ANSI-absence check and no-color frames in `docs/evidence/terminal-render.txt`
ASCII/plain evidence: ASCII-only 80-column and degraded frames plus maximum-content width tests in `docs/evidence/terminal-render.txt`
Reduced-motion evidence: Static-frame selection and equal-width active-frame checks in `docs/evidence/terminal-render.txt`
Keyboard/focus evidence: Actual attached Pi /ops and /inbox keyboard path with idle/cost receipts in `docs/evidence/herdr-coexistence.md`
Streaming/resize evidence: Actual 54- and 113-column launches, transcript append, footer updates, scroll receipt, and deterministic width matrix in `docs/evidence/herdr-coexistence.md` and `docs/evidence/terminal-render.txt`
Error/degraded evidence: Tool-missing live discovery, corrected local-controller launch, stale-footer replacement in source, deterministic degraded frame, and source-rendered plain PNG in `docs/evidence/herdr-coexistence.md`, `docs/evidence/terminal-render.txt`, and `docs/evidence/screenshots/degraded-60x12-plain.png`

### Terminal quality floor

| Gate | Frozen criterion |
|---|---|
| `T1` | No accidental clipping, overlap, display-width drift, or escape corruption. |
| `T2` | Product identity, current state, and next inspection action are immediate. |
| `T3` | Input focus remains visible and footer/transcript updates do not steal it. |
| `T4` | The critical workflow has a keyboard path; transcript copy and scroll remain usable. |
| `T5` | Essential meaning survives no-color mode. |
| `T6` | Essential meaning survives image and rich-glyph loss. |
| `T7` | The minimum viewport preserves identity and decisive state without clipping. |
| `T8` | Plain stdout/stderr and `TERM=dumb` output remain useful, ASCII, and copyable. |
| `T9` | Streaming/footer updates do not jitter history or steal scroll/selection. |
| `T10` | Reduced-motion mode is static, complete, and explicitly cleared with the turn. |
| `T11` | Errors and blockers outrank identity art and retain remediation. |
| `T12` | Art has a stated role, bounded footprint, quiet equivalent, and explicit opt-out. |

- T1 — PASS — `docs/evidence/terminal-render.txt` `docs/evidence/herdr-coexistence.md`
- T2 — PASS — `docs/evidence/herdr-coexistence.md`
- T3 — PASS — `docs/evidence/herdr-coexistence.md`
- T4 — PASS — `docs/evidence/herdr-coexistence.md`
- T5 — PASS — `docs/evidence/terminal-render.txt`
- T6 — PASS — `docs/evidence/terminal-render.txt`
- T7 — PASS — `docs/evidence/terminal-render.txt`
- T8 — PASS — `docs/evidence/terminal-render.txt`
- T9 — PASS — `docs/evidence/herdr-coexistence.md`
- T10 — PASS — `docs/evidence/terminal-render.txt`
- T11 — PASS — `docs/evidence/terminal-render.txt` `docs/evidence/herdr-coexistence.md`
- T12 — PASS — `docs/evidence/terminal-render.txt`

### Locked-check results

TX-C1 — PASS — `docs/evidence/terminal-render.txt`
TX-C2 — PASS — `docs/evidence/herdr-coexistence.md` `docs/evidence/terminal-render.txt`
TX-C3 — PASS — `docs/evidence/terminal-render.txt`
TX-C4 — PASS — `docs/evidence/terminal-render.txt`
TX-C5 — PASS — `docs/evidence/terminal-render.txt`
TX-C6 — PASS — `docs/evidence/terminal-render.txt`
TX-C7 — PASS — `docs/evidence/herdr-coexistence.md`
TX-C8 — PASS — `docs/evidence/terminal-render.txt` `docs/evidence/herdr-coexistence.md`
TX-C9 — PASS — `docs/evidence/herdr-coexistence.md`
TX-C10 — PASS — `docs/evidence/herdr-coexistence.md`

Independent reviewer: fresh ephemeral tool-free Pi (`gpt-5.6-sol`, high reasoning), accepted 2026-08-24
Reviewer report: `docs/evidence/terminal-review.md`
Final verdict: Pass
