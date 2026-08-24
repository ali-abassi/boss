# Control-plane provenance — real-model workflow

This receipt was captured on 2026-08-22 before the BOSS namespace and COO prompt split. It proves
the real Pi Graph/model path inherited by BOSS, not a fresh BOSS-branded launch. The distinction
is intentional: evidence is never relabeled after the fact.

`tests/test_live.py` ran against the real `piw` and model `openai-codex/gpt-5.4-mini`.

- Task: implement `fizzbuzz(n)` in `fizz.py` so `test_fizz.py` passes; return `str(n)` for
  non-multiples; do not modify `test_fizz.py`.
- Graph: `local-only`; dispatch rule: `cheap`.
- Wall time: 18 seconds; tokens: 11,320; reported cost: $0.005024.
- Result: verification green → `ready` → explicit `promote --confirm` fast-forwarded `main`.

Pi Graph ledger:

```text
- 03:09:48 ledger:
  implement          gpt-5.4-mini       16.4s    11320 tok  $0.0050
  protected          cmd                 0.0s        0 tok  $0.0000
  verify             cmd                 1.2s        0 tok  $0.0000
  TOTAL 18s compute · 11320 tok · $0.0050 · ledger.json written
```
