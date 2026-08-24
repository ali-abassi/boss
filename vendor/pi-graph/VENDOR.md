# Bundled: pi-graph 0.3.0

The workflow runner used for every task. Copied from
https://github.com/ali-abassi/pi-graph at commit `3830313` (MIT, see LICENSE here).
Only the runtime is bundled: `scripts/`, `schemas/`, `actions/`. Studio, examples, and
the Pi extension live upstream.

The portable `steps.yaml` and durable run-bundle kernel is compatible with
[Agent Workflows v0.2.0](https://github.com/ali-abassi/agent-workflows/tree/v0.2.0),
commit `d2f84bb740d8e336a198145022a367acdf18824f`. BOSS keeps the fuller Pi Graph
runner because it is a strict functional superset. It ports the v0.2.0 fixes that reject
missing required input before creating a run directory and print the public `piw resume`
recovery command. Agent Workflows is not downloaded or silently installed at runtime.

To update: copy those three directories plus VERSION, LICENSE and requirements.txt from
a pi-graph checkout, run the test suite, and note the new commit here.

`piw version` reports "version unavailable" in the bundle because the upstream identity check
expects files we do not ship (extension, docs); every runtime command works.
