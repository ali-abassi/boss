# Pi extension: `boss.ts`

The `.pi/extensions/boss.ts` extension adds a persistent footer status line and three operational slash commands (`/ops`, `/inbox`, and `/away`) inside a Pi conversation, plus `/wake` for scheduled check-ins. Unlike the CLI, `/ops` and `/inbox` are direct reads with zero model turns.

| Command | Description | Registration |
| --- | --- | --- |
| `/ops` | Operations board: workers, projects, queue | `boss.ts:338-345` |
| `/inbox` | What needs the boss: questions, failures, ready branches, open PRs | `boss.ts:346-353` |
| `/away on\|off\|status` | Gated unattended mode | `boss.ts:354+` |
| `/wake 20m [note]` | Schedule your COO to check in | `boss.ts:314-337` |

## How it talks to `bossctl`

The extension shells out to the `bossctl` binary. `BOSSCTL_BIN` selects the binary when set; otherwise it resolves relative to the extension file through the `CONTROLLER` constant near the top of `boss.ts`: `process.env.BOSSCTL_BIN || resolve(dirname(fileURLToPath(import.meta.url)), "../../bin/bossctl")`. It parses JSON output. It never asks a model anything for `/ops` or `/inbox`.

## The persistent status line

The footer format is `◆ BOSS · <away-state> · <N need you> · <N wake(s)> · /ops`. When `plain()` mode is on, it degrades to plain ASCII: `[B] BOSS | ...`. If `bossctl` or Herdr cannot be reached, it shows a `team tools missing` message with `re-run install.sh`.

## Testing

`tests/test_units.py::PiExtensionTests` runs `bun .pi/extensions/boss.ts` as a self-test. `bun <path>` runs the file's own assertions, exits 0, and prints `checks passed`. This is the extension's test suite; run `python3 -m unittest tests.test_units.PiExtensionTests.test_extension_self_test_passes`.
