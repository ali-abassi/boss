# BOSS terminal-mark verification — 2026-08-24

Scope: the startup masthead, its plain/no-color/degraded fallbacks, installed command resolution,
and unchanged Pi integration. The final source-rendered images explicitly identify themselves as
source renders; actual Pi PTY evidence is separately recorded in `pi-tui-terminal-mark.md`.

## Deterministic renderer and installation checks

```bash
bun .pi/extensions/boss.ts
```

```text
boss.ts: 39 checks passed
```

```bash
bun docs/evidence/verify_terminal_mark.ts
```

```text
terminal-mark: 31 checks passed
row matrix: 120=5, 100=5, 80=5, 60=3, 50=2, 32=2, 12=2
plain normal: bytes=177 non_ascii=0 ansi_sequences=0 rows=5
plain degraded: bytes=95 non_ascii=0 ansi_sequences=0 rows=3
no-color maximum: bytes=278 ansi_sequences=0 decisive_state="31 need you"
installed pi-boss: /Users/aliabassi/boss/bin/pi-boss
installed pi-boss-quit: /Users/aliabassi/boss/bin/pi-boss-quit
HERDR_ENV during verification: unset
Herdr control commands issued by this verifier: 0
Firstmate launcher targets found: 0
```

The 80-column maximum fixture retains `31 need you`, then drops the complete lower-priority
`17 queued` summary part rather than clipping a label. `/ops` remains on the next row for complete
inspection.

## Runtime and full-suite receipts

```bash
python3 -m unittest discover -s tests -v
```

```text
Ran 238 tests in 187.983s
OK (skipped=1)
```

The only skip is the intentionally opt-in `BOSS_LIVE=1` real-model test.

After the final non-behavioral cleanup, the focused extension integration suite was rerun:

```bash
python3 -m unittest tests.test_units.PiExtensionTests -v
```

```text
Ran 5 tests in 0.038s
OK
```

```bash
bun bossctl/pi_attest.ts
```

```text
pi-attest: signed runtime attestation + 19 scope checks passed
```

```bash
python3 docs/evidence/render_terminal_screenshots.py
```

Result: regenerated the default, minimum, degraded, and before/after PNGs from the exact frozen
frames in `terminal-render.txt` using local SF Mono. Each source-rendered image says it is not a
live capture.

## Evidence map

- Default visual and exact five-row lockup: `screenshots/default-100x30.png`
- Rejected old mark beside final mark: `screenshots/terminal-mark-before-after.png`
- Minimum and primary action: `screenshots/minimum-50x12-no-color.png`
- Degraded error/remediation: `screenshots/degraded-60x12-plain.png`
- 120/100/80/60/50/32/12 frames, maximum masthead, and byte receipts: `terminal-render.txt`
- Actual Pi startup at 80×24 and 50×12: `pi-tui-terminal-mark.md`
- Reproducible focused verifier: `verify_terminal_mark.ts`

No Herdr session was inspected or controlled during this verification. Installed command targets
were proven by resolving the two symlinks and reading only `bin/pi-boss` and `bin/pi-boss-quit`;
the full suite separately exercises the named-session launcher, restart, and shutdown paths.
