# Actual Pi TUI masthead receipts — 2026-08-24

These are sanitized text extractions from two real PTY launches of Pi 0.84.3 with the exact local
extension loaded explicitly. They are not Herdr or Ghostty screenshots. Both used disposable Pi
homes, `--no-session`, `--no-tools`, `--no-skills`, `--no-context-files`, `--offline`, and
`HERDR_ENV=unset`. No model turn or Herdr control command ran.

## Typical PTY — 80 × 24

Command shape:

```bash
PI_CODING_AGENT_DIR=<disposable> \
BOSSCTL_BIN=/Users/aliabassi/boss/bin/bossctl \
TERM=xterm-256color \
pi --no-session --no-tools --no-skills --no-context-files --no-extensions \
  --no-approve --offline --tui-mode regular \
  -e /Users/aliabassi/boss/.pi/extensions/boss.ts
```

Pi reported `[Extensions] boss.ts`, then rendered this exact startup entry at 80 columns:

```text
   ██████╮     B O S S
   █     │     Y O U R   A I   C O O
   ██████┤     O P E R A T I O N S   D E S K
   █     │     0 projects · team stopped · inbox clear
   ██████╯     /ops  ·  /inbox  ·  /wake 20m
```

The live footer then rendered:

```text
◆ BOSS · team idle · /ops
```

Pi also emitted its inverse-video input cursor after the masthead; the extension did not take or
hide focus.

## Minimum PTY — 50 × 12

The same command was preceded by `stty cols 50 rows 12`. Pi again reported
`[Extensions] boss.ts` and rendered:

```text
 BOSS · YOUR AI COO
 0 projects · inbox clear · /ops
```

The live footer again retained `◆ BOSS · team idle · /ops`. The expected disposable-home
warnings about project trust and unavailable models appeared below the masthead; they do not
replace or clip the BOSS identity, state, primary action, input line, or footer.

## Cleanup and scope

- Both Pi processes exited with code 0 through Ctrl-D.
- Both exact disposable Pi homes were moved to Trash after inspection.
- Neither launch created a Pi session record.
- Neither launch issued a Herdr command, opened or focused a Herdr session, or targeted Firstmate.
- Actual Herdr/Ghostty pixel capture remains unclaimed because this verifying process was not in a
  Herdr-managed pane and Herdr's installed safety contract forbids controlling the focused session
  from outside Herdr.
