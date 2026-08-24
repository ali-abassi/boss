# Live Herdr terminal and coexistence receipt — 2026-08-24

Runtime: Herdr 0.8.0, Pi 0.84.2, macOS, terminal title `π - boss`. The live capture used a
140×40 attached PTY, leaving 113 content columns after the Herdr sidebar. Paths below are shown
as observed; no Firstmate command or control was sent.

## Actual BOSS pane

```text
      ┏━━╮     B O S S
      ┣━━┫     Y O U R   A I   C O O
      ┗━━╯     O P E R A T I O N S   D E S K
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  ◆  0 projects · team ready · inbox clear
     /ops  ·  /inbox  ·  /wake 20m

  ◆  B O S S   ·   O P E R A T I O N S
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    team      ready
    projects  0   none yet - say: "add ~/code/my-repo"
    inbox     clear

  inbox
  nothing needs you

~/boss (main)
$0.000 (sub) 0.0%/272k (auto)                         gpt-5.6-sol • high
◆ team idle
```

`/ops` and `/inbox` were typed through the attached Pi UI. Both appended transcript entries;
the usage meter remained `$0.000` and the Pi agent stayed `idle`, so neither command consumed a
model turn. Previous entries remained readable after both footer and transcript updates.

## Scroll-away preservation

For the final live run, eight `/ops` entries created 34 rows of scrollback in a 29-row viewport.
The pane was moved away from the bottom with Page Up, then left in place across a complete
eight-second footer polling interval. Herdr reported the same scroll position afterward:

```text
before Page Up  max_offset_from_bottom=34  offset_from_bottom=0
after Page Up   max_offset_from_bottom=34  offset_from_bottom=29
after refresh   max_offset_from_bottom=34  offset_from_bottom=29
```

The agent remained `idle`, the input stayed focused, prior `/ops` entries remained in the pane,
and the persistent footer still read `◆ BOSS · team idle · /ops`. This directly exercises the
scroll-away case in `TX-C9`: footer polling updated its owned region without snapping the user
back to the bottom or repainting transcript history.

## Operated maximum-content commands

A separate, isolated `boss-evidence` Herdr session used a temporary `BOSS_HOME` containing one
local-only project and 12 `needs-you` items. No worker daemon was started, the supervisor wake
queue was empty, and the real project extension/controller handled both commands. `/ops` appended
all 12 item rows and `/inbox` appended all 12 questions, including the final records:

```text
/ops
  team      stopped
  projects  1   demo [local-only/a1]
  inbox     12 questions
  ... 12 visible needs-you rows ...
  ? needs-you demo         Review release decision 11: validate a deliberately detailed maximum-con

/inbox
  ... 12 visible question blocks ...
  [question]  demo: Review release decision 11: validate a deliberately detailed
              Choose safe release option 11; evidence includes wide text 界 and emoji U0001f680
```

Immediately after both keyboard commands, and again after another complete footer polling
interval, the runtime receipt remained:

```text
session boss-evidence · workspace BOSS max-content proof · tab ◆ BOSS proof · pane w1:p1
agent pi · status idle · focused true · cwd /Users/aliabassi/boss · title π - boss
scroll max_offset_from_bottom=121 · offset_from_bottom=0 · viewport_rows=23
$0.000 (sub) 0.0%/272k (auto)
◆ BOSS · team idle · 12 need you · /ops
```

This is the operated maximum-content half of `TX-C7`: both registered slash commands appended
large transcript entries, retained input focus, and consumed no model turn. The temporary Herdr
session and fixture state were stopped and removed after capture.

The pane receipt was:

```text
session boss · workspace BOSS · tab ◆ BOSS · pane w1:p2
agent pi · status idle · cwd /Users/aliabassi/boss · terminal title π - boss
viewport rows 39 · scroll offset 0
```

## Separate live Firstmate session

During the BOSS run, Firstmate remained:

```text
session firstmate · workspace First Mate · tab ⚓ First Mate · pane w1:p2
agent pi · status idle · cwd /Users/aliabassi/firstmate-graph
terminal title π - firstmate-graph
```

The repeated pane identifier is scoped to its named session; BOSS never treated it as global
identity. After `pi-boss-quit`:

```text
boss       running=false
firstmate  running=true
BOSS closed
```

The Firstmate workspace/tab/pane/cwd/title receipt was unchanged, and no `bossctl daemon`
process remained. A deterministic shutdown test separately keeps a sentinel under `.helm`,
lists Firstmate as live, and proves `pi-boss-quit` sends only `session stop boss`.

## Failures found and fixed during the live proof

1. The first direct-checkout launch showed `team tools unavailable` because the extension
   depended on `bossctl` being on PATH. It now resolves the controller beside its own extension;
   the second and third launches reported real state.
2. The first shutdown stopped both schedulers, but their brief zombie state prevented positive
   proof. Exact captured zombies now classify as dead only after their process-start receipt
   matches; a foreign zombie remains `reused`. The final two shutdowns returned `BOSS closed`
   immediately.

An early PTY input burst was received as `/oops`, legitimately spent one model turn, and was
not used as slash-command evidence. The clean wide run entered each key separately and retained
the `$0.000` receipt above.

## Explicitly unproven

Pixel-level capture of the actual Ghostty window was unavailable because the privacy-bounded
Mac-control tools were not exposed in this session; raw screen capture was intentionally not
used. Clearly labeled source-rendered PNGs provide separate visual inspection and never claim to
be live captures. SSH, nested tmux, screen readers, Kitty graphics, and third-party Herdr
lifecycle integrations remain unclaimed and are not required by BOSS.
