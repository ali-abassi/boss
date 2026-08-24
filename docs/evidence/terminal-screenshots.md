# Deterministic terminal screenshots — 2026-08-24

These PNGs rasterize the exact source-rendered frames in `terminal-render.txt`; the caption in
each image says `not a live capture`. They provide pixel-level default, minimum, and degraded
inspection without pretending an automated macOS screen capture occurred. The independent live
Herdr transcript, input/focus receipt, and scroll offsets remain in `herdr-coexistence.md`.

- `screenshots/default-100x30.png` — 100×30 grid, BOSS dark palette
- `screenshots/minimum-50x12-no-color.png` — 50×12 grid, no color
- `screenshots/degraded-60x12-plain.png` — 60×12 grid, plain degraded state and remediation
- `screenshots/terminal-mark-before-after.png` — rejected outlined box mark beside the final
  solid-spine B lockup

Rebuild on macOS with Pillow already available:

```bash
python3 docs/evidence/render_terminal_screenshots.py
```

The renderer uses the local SF Mono terminal font and does not alter or capture another app.
