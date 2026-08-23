# First Mate masthead — micro-art contract

- Role: establish First Mate identity and orient the captain at Pi startup.
- Thesis: a compass rose leads into the captain's control-deck wordmark; navigation is the visual grammar, while live fleet state remains a plain, copyable line.
- Signature move: the four-point compass aligned with the wordmark and control-deck label.
- Maximum footprint: 6 rows at 72+ cells; 3 rows at 52–71; 2 rows below 52.
- Target widths: 120, 80, 60, 50, and 32 cells.
- Opt-out: `NO_COLOR` removes styling; narrow widths automatically remove art before information.
- Quiet fallback: `⚓ F I R S T M A T E` followed by the textual fleet state.
- Motion: none.
- Use: semantic theme tokens, measured cell clipping, stable reading order, nautical navigation geometry.
- Avoid: random waves, giant logos, nested boxes, gradients, fake telemetry, persistent animation.
- Prove: identity and state at every width; actions at 52+; no line exceeds viewport; no-color output retains meaning.
