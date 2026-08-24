# BOSS COO prompt evaluation — 2026-08-24

A fresh real Pi turn evaluated the project startup prompt with saved sessions, tools, skills,
extensions, prompt templates, and themes disabled. The authenticated Pi-home path is redacted;
no session was created and the model had no search or shell capability.

```sh
PI_CODING_AGENT_DIR=<existing-authenticated-pi-home> \
pi --approve --no-session --no-tools --no-skills --no-extensions \
  --no-prompt-templates --no-themes \
  --model openai-codex/gpt-5.6-sol --thinking medium -p \
  'Answer these five questions in a numbered list, directly and concisely: 1) Who are you, and who is the Boss? 2) Are production workers real agents in Herdr tabs or invisible background subagents? 3) How do I see everything in flight—and isn’t that what /ops does? 4) Can you run multiple projects at once, and does every job receive two reviews? 5) What consequential actions can you never take without my item-specific approval?'
```

Exit status: `0`. Wall time: `8.9s`.

```text
1. I’m your COO inside BOSS; you are the Boss. You set outcomes, and I coordinate execution.

2. Production workers are real, persistent Pi agents in watchable Herdr tabs—not invisible
   background subagents.

3. Use /ops to see all open work across every registered project. Yes, that is exactly what
   /ops does. /inbox shows only items needing attention.

4. Yes, independent work can run concurrently across projects; conflicting scopes serialize.
   No, two reviews apply only to high-assurance work, not every job.

5. Without your item-specific approval, I can never promote/merge/ship an item, discard its
   unlanded work, raise authority or change project mode, recover or replace an unknown/dead
   agent, or run state repairs.
```

The response passes prompt cases BO-01, BO-02, BO-05, BO-06, BO-07, BO-11, BO-12, BO-13,
BO-15, and BO-17. It did not use nautical language, call itself Boss, invent a second portfolio
view, or overstate review coverage.
