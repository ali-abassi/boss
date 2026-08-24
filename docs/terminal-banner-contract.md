# BOSS masthead — terminal identity contract

- Role: establish BOSS, identify the agent as the user's COO, show portfolio state, and expose
  the next inspection actions at Pi startup.
- Thesis: a geometric `B` built from one operating spine and two returning loops generates the
  visual language; identity and state remain plain, ordered, and copyable.
- Signature move: the three-row `┏━━╮ / ┣━━┫ / ┗━━╯` mark aligned to `B O S S`, `YOUR AI COO`,
  and `OPERATIONS DESK`.
- Maximum footprint: 6 rows at 72+ cells; 3 rows at 52–71; 2 rows below 52.
- Target widths: 120, 100, 80, 60, 50, and 32 cells.
- Opt-out: `NO_COLOR` removes styling; `BOSS_PLAIN=1` or `TERM=dumb` selects ASCII; narrow
  widths remove art before identity or state.
- Quiet fallback: `[B] BOSS | YOUR AI COO`, textual state, and `/ops | /inbox | /wake 20m`.
- Motion: only a fixed-width progress lane during an active model turn;
  `BOSS_REDUCED_MOTION=1` makes it static.
- Use: semantic Pi theme tokens, measured cell clipping, stable reading order, operations-spine
  geometry, and explicit state words.
- Avoid: nautical/corporate-costume imagery, giant logos, nested boxes, gradients, fake
  telemetry, ambiguous-width dependencies, and ambient animation.
- Prove: identity and state at every width; actions at 52+; no line exceeds the viewport;
  no-color and ASCII output retain meaning; errors and decisions outrank the masthead.

The full surface, runtime matrix, frozen checks, and evidence gates live in
[`terminal-surface-contract.md`](terminal-surface-contract.md).
