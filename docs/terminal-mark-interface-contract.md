# BOSS terminal mark — interface design contract

Project: BOSS
Artifact/register: product UI
Audience and usage context: A technical owner opening one persistent Pi conversation to run software work across repositories; the masthead must identify the operating context in seconds and then get out of the way.
Design argument / generative thesis / taste read: BOSS should feel like a calm operating instrument, not a novelty shell theme. A compact solid-spine `B` expresses one accountable command path feeding two execution loops. The mark, identity, live state, and next actions form one five-row lockup so the visual signature earns its space.
Approved references + qualities to borrow: `/Users/aliabassi/boss/assets/logo.svg` for the obsidian, warm-white, signal-green brand and forward operating motion; `/Users/aliabassi/boss/docs/brand-brief.md` for exact, restrained COO language; `/Users/aliabassi/boss/docs/evidence/screenshots/default-100x30.png` only as a baseline for footprint and hierarchy, not mark geometry.
Anti-references + failures to avoid: The current tiny outlined `┏━━╮ / ┣━━┫ / ┗━━╯` mark, which reads as a box or `8`; oversized ANSI logos; detached ornamental dividers; nautical or executive-costume imagery; rainbow color; ambiguous emoji width; art that survives while state or remediation is clipped.
Source list / artifact manifest: `/Users/aliabassi/boss/.pi/extensions/boss.ts`; `/Users/aliabassi/boss/docs/terminal-surface-contract.md`; `/Users/aliabassi/boss/docs/terminal-banner-contract.md`; `/Users/aliabassi/boss/docs/evidence/terminal-render.txt`; `/Users/aliabassi/boss/docs/evidence/render_terminal_screenshots.py`; `/Users/aliabassi/boss/docs/evidence/screenshots/default-100x30.png`; `/Users/aliabassi/boss/docs/evidence/terminal-mark-review.md`
Direction decision (use / avoid / prove): use: a five-row solid-spine B, one consistent text column, semantic warning/accent/muted theme tokens, compact state and action rows, measured display-cell clipping | avoid: the old three-row box mark, a separate full-width divider, color-only meaning, animation, image protocols, and width-dependent content reorder | prove: compare the old baseline with the final 100×30 render, exercise 120/100/80/60/50/32/12-cell frames, inspect ASCII and no-color output, verify the installed launcher resolves to this exact extension without controlling Herdr from an unmanaged pane, and obtain a fresh independent visual review
Fixed constraints: The wide masthead is at most five rows at 72+ cells, medium is three rows at 52–71, and narrow is two rows below 52; every line fits by display-cell width; BOSS identity, decisive state, and `/ops` remain ahead of decorative detail; `TERM=dumb` or `BOSS_PLAIN=1` is ASCII-only; no changes to lifecycle, authority, budgets, Firstmate, or GitHub identity assets.
Non-goals: Redesigning the operations board, changing the footer protocol, adding a full-screen TUI, introducing terminal graphics, changing the GitHub logo/hero, touching the live Firstmate session, or claiming a Herdr/Ghostty pixel capture from a verifier that is not inside Herdr.
Shared type / spacing / color / shape / imagery / motion rules: Uppercase spaced wordmark only at wide width; one five-cell gutter between mark and copy; signal green is reserved for the B and command accents; role and state use muted tokens; the mark uses one-cell Unicode block/box glyphs with a visible spine and two bowls; no imagery or ambient motion; active-turn motion remains the existing fixed-width progress lane.
Shared interaction and feedback rules: The masthead is an append-only Pi transcript entry, never takes focus, never intercepts input, and never replaces an error; commands retain their current handlers and receipts; degraded state moves error and remediation to the first two lines and drops art.
Default viewport: 100 × 30 cells
Minimum viewport: 50 × 12 cells, with deterministic clipping checks down to 12 columns
Handoff path: `/Users/aliabassi/boss/docs/terminal-mark-interface-contract.md`
Evidence directory: `/Users/aliabassi/boss/docs/evidence`
Locked checks version / date: TM-1 / 2026-08-24
Required reviewer assignments: Fresh ephemeral tool-free Pi visual reviewer, separate from the implementing session, reviews only the locked contract and generated evidence.

---

## Surface: Pi startup masthead

- **Register and usage moment:** Persistent terminal product UI shown once when `pi-boss` opens or resumes the BOSS conversation.
- **Primary user job:** Confirm they are in BOSS, understand current portfolio/team state, and know where to inspect or act.
- **Observable successful outcome:** Within the five-row wide lockup, the user can identify BOSS, its COO role, the current operational state, and `/ops`; the B reads as a deliberate letterform rather than a box or `8`.
- **Entry / exit:** Entered by launching `pi-boss`; exits naturally into the Pi transcript and input line without a separate dismissal.
- **Critical information, ordered:** BOSS identity; `YOUR AI COO`; `OPERATIONS DESK`; project/team/inbox state; `/ops`, `/inbox`, `/wake 20m`.
- **Primary actions:** Type `/ops` or continue with a natural-language instruction.
- **Secondary actions:** Type `/inbox` for decisions or `/wake 20m` for a check-in.
- **Composition and hierarchy:** Five aligned rows at wide width: the signal-green B occupies the left column while title, role, desk, state, and actions occupy the right column; medium and narrow modes remove mark detail before content.
- **Interaction and feedback rules:** No interaction inside the masthead; Pi retains input, focus, selection, and scrolling; footer polling does not repaint the masthead; explicit degraded text outranks the mark.
- **Normal state:** Three registered projects, team ready, inbox clear; reached with a valid local controller status response.
- **Empty state:** Zero projects, team stopped, inbox clear; identity and inspection actions remain visible.
- **Long / maximum-content state:** Long project counts plus active, queued, away, and needs-you labels; every row truncates by display cells without wrapping or escape leakage.
- **Loading state:** Existing fixed-width working indicator appears outside the masthead only during a model turn; the startup lockup does not animate.
- **Error / degraded / disabled state:** Missing controller tools show `team tools missing` and `re-run install.sh` before actions; wide art is removed so remediation dominates.
- **Default viewport:** 100 × 30 cells
- **Minimum viewport:** 50 × 12 cells; 32- and 12-cell clipping behavior is also deterministic.
- **Representative content:** `3 projects · team ready · 1 running · 2 queued · 4 need you`, plus `/ops · /inbox · /wake 20m`; degraded fixture names the missing tools and reinstall action.
- **Surface-specific anti-slop risks:** A generic boxed `B`; a logo taller than the useful information; fake executive-dashboard chrome; misaligned Unicode caused by byte counting; low-contrast state; a decorative divider consuming a row; plain mode that leaks ANSI or non-ASCII glyphs.
- **Floor definitions used for review:** F1 no accidental clipping, overlap, or unreachable content; F2 primary job and action visible on first paint; F3 readable text at required viewports; F4 visible focus for interactive elements; F5 complete keyboard path; F6 reduced-motion behavior; F7 coherent normal, maximum, empty, and loading states; F8 distinguishable actionable degraded states; F9 real content remains inspectable; F10 state and health agree with underlying state; F11 primary actions survive normal and adverse states; F12 actual Pi TUI survives typical and minimum terminal sizes.
- **Acceptance checks:**
  - `TM-C1` — Wide mode is exactly five rows, every row fits the requested display width, and the B has a solid spine plus visibly distinct upper and lower bowls | normal and maximum-content | 120/100/80 cells | deterministic transcript and source-rendered image
  - `TM-C2` — Wide reading order is `B O S S` → `YOUR AI COO` → `OPERATIONS DESK` → live state → actions, with no detached divider row | normal | 100×30/default theme | screenshot and transcript
  - `TM-C3` — The new mark is materially more recognizable and ownable than the old box-like baseline without enlarging the total masthead footprint | normal | 100×30/default theme | side-by-side evidence and independent review
  - `TM-C4` — Medium and minimum modes retain BOSS, COO role, decisive state, and `/ops` without clipping; art is removed before information | normal, active, and needs-you | 60×12 and 50×12/no-color | screenshot and deterministic transcript
  - `TM-C5` — `TERM=dumb`/`BOSS_PLAIN=1` is ASCII-only and no-color output contains no ANSI while preserving identity, state, remediation, and actions | normal and degraded | 80×24 | byte and transcript inspection
  - `TM-C6` — Installed `pi-boss` and `pi-boss-quit` resolve to this repository, the launcher targets only the named BOSS session, and verification performs no focus/read/close/mutation against Firstmate from the current unmanaged pane | integration | installed shell path plus source inspection | command and topology receipt
- **Explicit failure conditions:**
  1. The mark still reads primarily as `8`, a crate, or an arbitrary box at the default viewport.
  2. The redesign exceeds five wide rows, introduces a decorative separator row, or pushes state/actions below the visible startup area.
  3. Any tested line exceeds its display width, plain output contains non-ASCII/ANSI, or no-color removes operational meaning.
  4. A missing-tool error is visually subordinate to the brand mark or lacks the reinstall action.
  5. Validation touches or disrupts the live Firstmate Herdr session.
- **Evidence:**
  - normal @ default: `docs/evidence/screenshots/default-100x30.png` and `docs/evidence/pi-tui-terminal-mark.md`
  - long/maximum @ default: `docs/evidence/terminal-render.txt` and `docs/evidence/terminal-mark-verification.md`
  - empty/degraded @ default: `docs/evidence/screenshots/degraded-60x12-plain.png` and `docs/evidence/pi-tui-terminal-mark.md`
  - normal @ minimum: `docs/evidence/screenshots/minimum-50x12-no-color.png` and `docs/evidence/pi-tui-terminal-mark.md`
  - interaction before/after or recording: `docs/evidence/pi-tui-terminal-mark.md` and `docs/evidence/herdr-coexistence.md`
- **Floor F1–F12:**
  - F1 — PASS — `docs/evidence/terminal-mark-verification.md` and `docs/evidence/terminal-render.txt`
  - F2 — PASS — `docs/evidence/screenshots/default-100x30.png` and `docs/evidence/screenshots/minimum-50x12-no-color.png`
  - F3 — PASS — `docs/evidence/terminal-render.txt` and `docs/evidence/pi-tui-terminal-mark.md`
  - F4 — PASS — `docs/evidence/pi-tui-terminal-mark.md`
  - F5 — PASS — `docs/evidence/herdr-coexistence.md`
  - F6 — PASS — `docs/evidence/terminal-render.txt`
  - F7 — PASS — `docs/evidence/terminal-render.txt` and `docs/evidence/pi-tui-terminal-mark.md`
  - F8 — PASS — `docs/evidence/screenshots/degraded-60x12-plain.png` and `docs/evidence/terminal-mark-verification.md`
  - F9 — PASS — `docs/evidence/terminal-render.txt` and `docs/evidence/herdr-coexistence.md`
  - F10 — PASS — `docs/evidence/pi-tui-terminal-mark.md` and `docs/evidence/herdr-coexistence.md`
  - F11 — PASS — `docs/evidence/terminal-render.txt` and `docs/evidence/screenshots/degraded-60x12-plain.png`
  - F12 — PASS — `docs/evidence/pi-tui-terminal-mark.md`
- **Locked-check results:**
  - TM-C1 — PASS — `docs/evidence/terminal-render.txt` and `docs/evidence/terminal-mark-verification.md`
  - TM-C2 — PASS — `docs/evidence/screenshots/default-100x30.png` and `docs/evidence/pi-tui-terminal-mark.md`
  - TM-C3 — PASS — `docs/evidence/screenshots/terminal-mark-before-after.png` and `docs/evidence/terminal-mark-review.md`
  - TM-C4 — PASS — `docs/evidence/terminal-render.txt` and `docs/evidence/screenshots/minimum-50x12-no-color.png`
  - TM-C5 — PASS — `docs/evidence/terminal-mark-verification.md` and `docs/evidence/screenshots/degraded-60x12-plain.png`
  - TM-C6 — PASS — `docs/evidence/terminal-mark-verification.md` and `docs/evidence/pi-tui-terminal-mark.md`
- **Independent reviewer:** Fresh ephemeral tool-free Pi (`gpt-5.6-sol`, high reasoning), accepted 2026-08-24 in `docs/evidence/terminal-mark-review.md`.
- **Verdict:** Pass

---

## Completion packet

- Final surface inventory: One Pi startup masthead with five-row wide, three-row medium, two-row narrow, no-color, ASCII/plain, empty, maximum-content, and degraded modes.
- Reviewer verdicts: Independent acceptance and review history are recorded in `docs/evidence/terminal-mark-review.md`.
- Unresolved unknowns / risks: none
- Check-change log: Existing wide row limit 6 → 5 and old three-row outlined mark → five-row solid-spine mark → reduce wasted space and correct the user's explicit recognition/taste rejection → authorized by the Boss on 2026-08-24 → `docs/evidence/screenshots/terminal-mark-before-after.png` and `docs/evidence/terminal-mark-review.md`; TM-C6 live Herdr capture → installed launcher/source verification plus real isolated Pi PTY verification with no Herdr control → the current session reports `HERDR_ENV=unset` and the installed Herdr safety contract forbids inspecting or controlling a focused session from outside Herdr → authorized by the governing runtime safety contract on 2026-08-24 → `docs/evidence/terminal-mark-verification.md` and `docs/evidence/pi-tui-terminal-mark.md`.
- Final decision: Pass
