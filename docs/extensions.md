# Pi extension: `boss.ts`

The `.pi/extensions/boss.ts` extension adds a persistent footer status line, operational reads,
controlled wake delivery, scheduled check-ins, away mode, and an optional advisory planning
pulse inside a Pi conversation. `/ops`, `/inbox`, status polling, and disabled planning use zero
model turns.

| Command | Description |
| --- | --- |
| `/ops` | Operations board: workers, projects, queue |
| `/inbox` | What needs the boss: questions, failures, ready branches, open PRs |
| `/away on\|off\|status` | Gated unattended mode |
| `/wake 20m [note]` | Schedule your COO to check in |
| `/pulse on\|off\|status\|now` | Control or request a read-only advisory portfolio pulse |

## How it talks to `bossctl`

The extension shells out to the `bossctl` binary. `BOSSCTL_BIN` selects the binary when set; otherwise it resolves relative to the extension file through the `CONTROLLER` constant near the top of `boss.ts`: `process.env.BOSSCTL_BIN || resolve(dirname(fileURLToPath(import.meta.url)), "../../bin/bossctl")`. It parses JSON output. It never asks a model anything for `/ops` or `/inbox`.

## Planning pulse

Planning is off by default. `/pulse now` performs a one-shot without enabling future pulses;
`/pulse on` enables bounded adaptive checks. Normal supervisor wakes and away mode always take
priority. An unchanged semantic snapshot consumes no model call and backs off up to six hours.

For changed evidence, the extension claims a dedicated planning receipt and marks generation
started before calling the current Pi model directly. The request contains one untrusted frozen
snapshot plus its deterministic recap—no conversation history and no tools—with a 2,048-token,
90-second bound and no retained cache. Output must match the strict advisory JSON contract and
cite only source IDs in the snapshot. The extension appends a custom non-context transcript
entry labeled `ADVISORY — NO ACTION TAKEN`, then records provider, model, response digest, and
usage. A crash or uncertain append after generation begins is never replayed automatically.

## The persistent status line

The footer format is `◆ BOSS · <away-state> · <N need you> · <N wake(s)> · /ops`. When `plain()` mode is on, it degrades to plain ASCII: `[B] BOSS | ...`. If `bossctl` or Herdr cannot be reached, it shows a `team tools missing` message with `re-run install.sh`.

## Testing

`tests/test_units.py::PiExtensionTests` runs `bun .pi/extensions/boss.ts` as a self-test. `bun <path>` runs the file's own assertions, exits 0, and prints `checks passed`. This is the extension's test suite; run `python3 -m unittest tests.test_units.PiExtensionTests.test_extension_self_test_passes`.
