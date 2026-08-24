// BOSS — Pi extension.
// A compact status line, a live operations strip in the footer, a working state,
// /ops and /inbox that never spend a model turn, and a wake: the COO turns by itself
// when the team has news or when the Boss schedules a check-in (/wake 20m). The COO's
// voice and rules are instructions in AGENTS.md; nothing here fabricates them.
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { truncateToWidth, visibleWidth } from "@earendil-works/pi-tui";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

type Theme = { fg(token: string, text: string): string; bold?(text: string): string };
type Wake = { id: string; item_id: string; project?: string; classification: string; reason: string };
type Status = {
  projects: number; workers: number | null; herdr_tabs?: { kind: string }[]; items: Record<string, number>;
  supervisor?: { pending_wakes: number | null; away: boolean | null; healthy: boolean };
};

// ------------------------------------------------------------------ rendering

const MARK = "◆";
const CONTROLLER = process.env.BOSSCTL_BIN || resolve(dirname(fileURLToPath(import.meta.url)), "../../bin/bossctl");

function gutter(width: number): number { return width >= 70 ? 2 : width >= 32 ? 1 : 0; }
function inner(width: number): number { return Math.max(1, width - gutter(width) * 2); }
function fit(lines: string[], width: number): string[] {
  const pad = " ".repeat(gutter(width));
  return lines.map((l) => pad + truncateToWidth(l, inner(width), ""));
}
function plainStatus(text: string): string { return text.replaceAll(" · ", " | "); }
export function terminalText(text: string, ascii = false): string {
  if (!ascii) return text;
  const punctuation: Record<string, string> = { "—": "-", "–": "-", "…": "...", "‘": "'", "’": "'", "“": '"', "”": '"', "⏰": ">", [MARK]: "[B]" };
  let out = "";
  for (const char of plainStatus(text).normalize("NFKD")) {
    const point = char.codePointAt(0)!;
    if (punctuation[char] !== undefined) out += punctuation[char];
    else if (point < 128) out += char;
    else if (/^[\p{Mark}\p{Cf}]$/u.test(char)) continue;
    else if (point <= 0xffff) out += `\\u${point.toString(16).padStart(4, "0")}`;
    else out += `\\U${point.toString(16).padStart(8, "0")}`;
  }
  return out;
}
export function statusLine(s: Status | null): string {
  if (!s) return "team tools missing · re-run install.sh";
  const workers = s.herdr_tabs?.some((t) => t.kind === "worker")
    ? `${s.herdr_tabs!.filter((t) => t.kind === "worker").length} workers`
    : s.workers ? "team ready" : "team stopped";
  const needs = (s.items["needs-you"] || 0) + (s.items["failed"] || 0) + (s.items["ready"] || 0) + (s.items["pr-open"] || 0);
  const running = s.items["running"] || 0, queued = s.items["queued"] || 0;
  const parts = [`${s.projects} project${s.projects === 1 ? "" : "s"}`, workers];
  if (s.supervisor?.away) parts.push("away");
  if (running) parts.push(`${running} running`);
  if (queued) parts.push(`${queued} queued`);
  parts.push(needs ? `${needs} need you` : "inbox clear");
  return parts.join(" · ");
}

function compactStatus(s: Status | null, narrow = false): string {
  if (!s) return "team tools missing · re-run install.sh";
  const needs = (s.items["needs-you"] || 0) + (s.items["failed"] || 0) + (s.items["ready"] || 0) + (s.items["pr-open"] || 0);
  const running = s.items["running"] || 0;
  const parts = [`${s.projects} project${s.projects === 1 ? "" : "s"}`];
  if (!narrow && running) parts.push(`${running} running`);
  if (needs) parts.push(`${needs} need you`);
  else if (!running) parts.push("inbox clear");
  else if (narrow) parts.push(`${running} running`);
  return parts.join(" · ");
}

export function persistentStatus(away: string, needs: number, queuedDecisions: number, ascii = false): string {
  const parts = ["BOSS", away];
  if (needs) parts.push(`${needs} need you`);
  if (queuedDecisions) parts.push(`${queuedDecisions} wake${queuedDecisions === 1 ? "" : "s"}`);
  parts.push("/ops");
  const separator = ascii ? " | " : " · ";
  return `${ascii ? "[B]" : MARK} ${parts.join(separator)}`;
}

export function degradedPersistentStatus(ascii = false): string {
  return ascii
    ? "[B] BOSS | team tools missing | re-run install.sh | /ops"
    : `${MARK} BOSS · team tools missing · re-run install.sh · /ops`;
}

export function renderBanner(theme: Theme, width: number, status: Status | null, noColor = false, plain = false): string[] {
  const titlePlain = "B O S S";
  const title = noColor ? titlePlain : (theme.bold?.(titlePlain) ?? titlePlain);
  const statePlain = statusLine(status);
  const state = noColor ? statePlain : theme.fg("muted", statePlain);
  const hintPlain = "/ops  ·  /inbox  ·  /wake 20m";
  const hint = noColor ? hintPlain
    : `${theme.fg("accent", "/ops")}${theme.fg("dim", "  ·  ")}${theme.fg("accent", "/inbox")}${theme.fg("dim", "  ·  ")}${theme.fg("accent", "/wake 20m")}`;
  if (!status) {
    const role = plain ? "[B] BOSS | YOUR AI COO" : noColor ? "BOSS · YOUR AI COO"
      : `${theme.bold?.("BOSS") ?? "BOSS"}${theme.fg("dim", " · YOUR AI COO")}`;
    const errorPlain = plain ? "! team tools missing | re-run install.sh" : "! team tools missing · re-run install.sh";
    const error = noColor || plain ? errorPlain : theme.fg("warning", errorPlain);
    const action = plain ? "/ops | /inbox | /wake 20m" : hint;
    return fit(width < 52 ? [role, error] : [role, error, action], width);
  }
  if (plain) {
    const plainState = plainStatus(statePlain);
    const plainHint = "/ops | /inbox | /wake 20m";
    if (width < 52) return fit(["BOSS | YOUR AI COO", plainStatus(compactStatus(status, true))], width);
    if (width < 72) return fit(["[B] BOSS | YOUR AI COO", plainStatus(compactStatus(status)), plainHint], width);
    const divider = "-".repeat(Math.max(12, Math.min(92, inner(width))));
    return fit([
      `[B]      ${titlePlain}`,
      " |       YOUR AI COO",
      " +----   OPERATIONS DESK",
      divider,
      `> ${plainState}`,
      `  ${plainHint}`,
    ], width);
  }
  if (width < 52) {
    const compactTitle = theme.bold?.("BOSS") ?? "BOSS";
    const role = noColor ? "BOSS · YOUR AI COO" : `${compactTitle}${theme.fg("dim", " · YOUR AI COO")}`;
    return fit([role, noColor ? compactStatus(status, true) : theme.fg("muted", compactStatus(status, true))], width);
  }
  if (width < 72) {
    const role = noColor ? `${MARK} ${titlePlain} · YOUR AI COO`
      : `${theme.fg("warning", MARK)} ${title}${theme.fg("dim", " · YOUR AI COO")}`;
    return fit([role, noColor ? compactStatus(status) : theme.fg("muted", compactStatus(status)), hint], width);
  }

  const mark = (text: string) => noColor ? text : theme.fg("warning", text);
  const dividerWidth = Math.max(12, Math.min(92, inner(width)));
  const divider = noColor ? "━".repeat(dividerWidth) : theme.fg("borderMuted", "━".repeat(dividerWidth));
  const role = noColor ? "Y O U R   A I   C O O" : theme.fg("dim", "Y O U R   A I   C O O");
  const desk = noColor ? "O P E R A T I O N S   D E S K" : theme.fg("dim", "O P E R A T I O N S   D E S K");
  return fit([
    `    ${mark("┏━━╮")}     ${title}`,
    `    ${mark("┣━━┫")}     ${role}`,
    `    ${mark("┗━━╯")}     ${desk}`,
    divider,
    `${mark("◆")}  ${state}`,
    `   ${hint}`,
  ], width);
}

// ------------------------------------------------------------------ wake

/** "20m", "1h", "1h30m", "45" (minutes) → ms; null when unparseable. */
export function parseWake(text: string): number | null {
  const m = text.trim().match(/^(?:(\d+)h)?(?:(\d+)m?)?$/i);
  if (!m || (!m[1] && !m[2])) return null;
  const ms = (Number(m[1] || 0) * 60 + Number(m[2] || 0)) * 60_000;
  return ms > 0 ? ms : null;
}

export function wakeText(kind: "inbox" | "timer", detail: string): string {
  return kind === "inbox"
    ? `BOSS EVENT — the team has news:\n${detail}\n\nReport this to the Boss in plain language (never mention bossctl): what landed, what failed, what needs a decision. Relay any question verbatim and wait for their answer.`
    : `BOSS CHECK-IN — scheduled review${detail ? ` ("${detail}")` : ""}. Look at operations (inbox and running work) and give the Boss a short status. If nothing moved, say so in one line.`;
}

// ------------------------------------------------------------------ working state

const PROGRESS_FRAMES = ["[=  ]", "[== ]", "[===]", "[ ==]", "[  =]", "[ ==]"];
const WORKING = ["coordinating the team", "checking the decision queue", "verifying evidence", "reviewing open work",
  "keeping one thread", "running the gates", "updating the plan", "waiting on the team"];

// ------------------------------------------------------------------ extension

export default function boss(pi: ExtensionAPI) {
  const bossctl = (...args: string[]) => pi.exec(CONTROLLER, args, { timeout: 15_000 });
  const noColor = () => process.env.NO_COLOR !== undefined || process.env.TERM === "dumb" || process.env.BOSS_PLAIN === "1";
  const plain = () => process.env.TERM === "dumb" || process.env.BOSS_PLAIN === "1";
  const reducedMotion = () => process.env.BOSS_REDUCED_MOTION === "1";
  let ui: any;
  let poll: ReturnType<typeof setInterval> | undefined;
  let ticker: ReturnType<typeof setInterval> | undefined;
  let deliveringWake = false;
  let pendingWakeIds: string[] = [];
  const wakeConsumer = `pi:${process.env.HERDR_SESSION || "local"}:${process.env.HERDR_PANE_ID || process.pid}`;
  const wakeTimers = new Map<number, { at: number; note: string; t: ReturnType<typeof setTimeout> }>();
  let wakeSeq = 0;

  async function status(): Promise<Status | null> {
    try {
      const r = await bossctl("status", "--json");
      return r.code === 0 ? (JSON.parse(r.stdout) as Status) : null;
    } catch { return null; }
  }

  async function refreshStrip(notifyNew = false) {
    if (!ui) return;
    const s = await status();
    if (!s) {
      // Never leave a stale healthy footer behind when the controller disappears.
      // The degraded state is actionable, text-only, and cannot trigger a model turn.
      try { ui.setStatus("boss", degradedPersistentStatus(plain())); } catch {}
      return;
    }
    const needs = (s.items["needs-you"] || 0) + (s.items["failed"] || 0) + (s.items["ready"] || 0) + (s.items["pr-open"] || 0);
    const running = s.items["running"] || 0;
    try {
      const away = s.supervisor?.away ? "away" : running ? `${running} in progress` : "team idle";
      const queuedDecisions = s.supervisor?.pending_wakes || 0;
      ui.setStatus("boss", persistentStatus(away, needs, queuedDecisions, plain()));
      // Only durable supervisor events may trigger a model turn. Count changes
      // update this strip but are never treated as evidence by themselves.
      if (notifyNew) await deliverWakes();
    } catch {}
  }

  async function deliverWakes() {
    if (deliveringWake) return;
    if (pendingWakeIds.length) {
      // A follow-up already exists in Pi. Renew its durable receipt instead of
      // claiming/sending it again while an earlier model turn is long-running.
      try { await bossctl("wakes", "--consumer", wakeConsumer, "--renew", pendingWakeIds.join(","), "--json"); } catch {}
      return;
    }
    deliveringWake = true;
    let events: Wake[] = [];
    let sendingRecorded = false;
    try {
      const claimed = await bossctl("wakes", "--claim", "--consumer", wakeConsumer, "--limit", "20", "--json");
      if (claimed.code !== 0) return;
      events = JSON.parse(claimed.stdout) as Wake[];
      if (!events.length) return;
      const inbox = await bossctl("inbox", "--hints");
      if (inbox.code !== 0) throw new Error(inbox.stderr || "inbox unavailable");
      const evidence = events.map((e) => `[${e.classification}] ${e.item_id}: ${e.reason}`).join("\n");
      pendingWakeIds = events.map((e) => e.id);
      const sending = await bossctl("wakes", "--consumer", wakeConsumer, "--sending", pendingWakeIds.join(","), "--json");
      if (sending.code !== 0 || Number(JSON.parse(sending.stdout)?.sending) !== pendingWakeIds.length) {
        throw new Error("durable wake sending receipt was not acquired");
      }
      sendingRecorded = true;
      pi.sendMessage({ customType: "boss-wake", content: wakeText("inbox", `${evidence}\n\n${inbox.stdout.trim()}`), display: false },
                     { deliverAs: "followUp", triggerTurn: true });
      // If this acknowledgement is lost, the durable `sending` state remains
      // uncertain and is never automatically replayed as a duplicate turn.
      await bossctl("wakes", "--consumer", wakeConsumer, "--sent", pendingWakeIds.join(","), "--json");
    } catch {
      if (events.length && !sendingRecorded) {
        try { await bossctl("wakes", "--consumer", wakeConsumer, "--release", events.map((e) => e.id).join(","), "--json"); } catch {}
        pendingWakeIds = [];
      }
    } finally {
      deliveringWake = false;
    }
  }

  // Put a fresh banner at the bottom of the transcript on every launch. Historical
  // entries still render correctly, while restored sessions never open on a buried header.
  pi.registerEntryRenderer("-boss-hello", (entry: any, _opts: unknown, theme: Theme) => ({
    render: (width: number) => renderBanner(theme, width, entry?.data?.status ?? null, noColor(), plain()),
    invalidate() {},
  }));
  // Board and inbox as transcript entries (plain text, themed dim), not toasts.
  pi.registerEntryRenderer("-boss-board", (entry: any, _opts: unknown, theme: Theme) => ({
    render: (width: number) => fit(String(entry?.data?.text ?? "").split("\n").map((l: string) => noColor() ? l : theme.fg("muted", l)), width),
    invalidate() {},
  }));

  pi.on("session_start", async (_event: unknown, ctx: any) => {
    if (!ctx.hasUI) return;
    ui = ctx.ui;
    try {
      pi.appendEntry("-boss-hello", { status: await status() });
      ctx.ui.setTitle?.("BOSS · your COO");
    } catch {}
    await refreshStrip();
    poll = setInterval(() => { refreshStrip(true).catch(() => {}); }, 8000);
    poll.unref?.();
  });

  pi.on("turn_start", async (_e: unknown, ctx: any) => {
    // A queued wake is acknowledged only once Pi proves that its model turn
    // began. If sending may have started but this hook never runs, the durable
    // uncertain receipt stays visible for explicit reconciliation and is not
    // replayed into a duplicate model turn.
    if (pendingWakeIds.length) {
      const ids = pendingWakeIds;
      try {
        const result = await bossctl("wakes", "--consumer", wakeConsumer, "--ack", ids.join(","), "--json");
        const receipt = result.code === 0 ? JSON.parse(result.stdout) : null;
        if (Number(receipt?.acknowledged) === ids.length) pendingWakeIds = [];
      } catch {}
    }
    if (!ctx.hasUI) return;
    try {
      const nc = noColor();
      const frames = reducedMotion() ? ["[working]"] : PROGRESS_FRAMES;
      ctx.ui.setWorkingIndicator({ frames: frames.map((f) => nc ? f : ctx.ui.theme.fg("accent", f)), intervalMs: 220 });
      const paint = () => { const m = WORKING[Math.floor(Date.now() / 5000) % WORKING.length]; ctx.ui.setWorkingMessage(nc ? m : ctx.ui.theme.fg("muted", m)); };
      paint();
      if (ticker) clearInterval(ticker);
      ticker = setInterval(paint, 5000); ticker.unref?.();
    } catch {}
  });
  function clearWorking() {
    if (ticker) clearInterval(ticker);
    ticker = undefined;
    try { ui?.setWorkingMessage?.(); ui?.setWorkingIndicator?.(); } catch {}
  }
  pi.on("turn_end", async () => { clearWorking(); });
  pi.on("agent_end", async () => { clearWorking(); await refreshStrip(true); });

  pi.registerCommand("wake", {
    description: "Schedule your COO to check in: /wake 20m [note] | /wake = list | /wake clear",
    handler: async (args: string, ctx: any) => {
      const text = args.trim();
      if (!text) {
        const rows = [...wakeTimers.values()].map((w) => terminalText(`⏰ in ${Math.max(1, Math.round((w.at - Date.now()) / 60000))}m${w.note ? ` — ${w.note}` : ""}`, plain()));
        ctx.ui.notify(terminalText(rows.length ? rows.join("\n") : "No check-ins scheduled. /wake 20m see if the login fix landed", plain()), "info");
        return;
      }
      if (text === "clear") { for (const w of wakeTimers.values()) clearTimeout(w.t); wakeTimers.clear(); ctx.ui.notify(terminalText("Check-ins cleared.", plain()), "info"); return; }
      const [when, ...rest] = text.split(/\s+/);
      const ms = parseWake(when);
      if (!ms) { ctx.ui.notify(terminalText("Usage: /wake <20m|1h|1h30m> [what to check]", plain()), "warning"); return; }
      const id = ++wakeSeq, note = rest.join(" ");
      const t = setTimeout(() => {
        wakeTimers.delete(id);
        try { pi.sendMessage({ customType: "boss-wake", content: wakeText("timer", note), display: false }, { deliverAs: "followUp", triggerTurn: true }); } catch {}
      }, ms);
      t.unref?.();
      wakeTimers.set(id, { at: Date.now() + ms, note, t });
      ctx.ui.notify(terminalText(`Check-in scheduled for ${Math.round(ms / 60000)}m${note ? ` on "${note}"` : ""}.`, plain()), "info");
    },
  });

  pi.registerCommand("ops", {
    description: "Operations board: workers, projects, queue",
    handler: async (_args: string, ctx: any) => {
      const r = await bossctl("watch", "--once");
      if (r.code === 0) pi.appendEntry("-boss-board", { text: r.stdout.trim() });
      else ctx.ui.notify(terminalText(`Operations board unavailable: ${r.stderr.trim()}`, plain()), "warning");
    },
  });
  pi.registerCommand("inbox", {
    description: "What needs the boss: questions, failures, ready branches, open PRs",
    handler: async (_args: string, ctx: any) => {
      const r = await bossctl("inbox");
      if (r.code === 0) pi.appendEntry("-boss-board", { text: `inbox\n${r.stdout.trim()}` });
      else ctx.ui.notify(terminalText(`Inbox unavailable: ${r.stderr.trim()}`, plain()), "warning");
    },
  });
  pi.registerCommand("away", {
    description: "Gated unattended mode: /away on | /away off | /away status",
    handler: async (args: string, ctx: any) => {
      const state = args.trim() || "status";
      if (!["on", "off", "status"].includes(state)) { ctx.ui.notify(terminalText("Usage: /away on|off|status", plain()), "warning"); return; }
      const r = await bossctl("away-mode", state, "--json");
      if (r.code !== 0) { ctx.ui.notify(terminalText(r.stderr.trim() || "Away preflight failed.", plain()), "warning"); return; }
      const result = JSON.parse(r.stdout);
      ctx.ui.notify(terminalText(result.enabled ? "BOSS · Away mode is on. Decisions stay queued; merges still need you." :
                    `BOSS · Away mode is off. ${result.pending_wakes || 0} preserved wake(s) ready.`, plain()), "info");
      await refreshStrip(true);
    },
  });

  pi.on("session_shutdown", async () => {
    if (poll) clearInterval(poll); clearWorking();
    for (const w of wakeTimers.values()) clearTimeout(w.t); wakeTimers.clear();
    if (pendingWakeIds.length) {
      try { await bossctl("wakes", "--consumer", wakeConsumer, "--release", pendingWakeIds.join(","), "--json"); } catch {}
      pendingWakeIds = [];
    }
    try { ui?.setStatus?.("boss", undefined); } catch {}
    ui = undefined;
  });
}

// self-test: `bun .pi/extensions/boss.ts`
if (process.argv[1]?.endsWith("boss.ts")) {
  let n = 0;
  const ok = (v: boolean, label: string) => { if (!v) throw new Error(`FAIL: ${label}`); n++; };
  const theme: Theme = { fg: (_t, s) => s, bold: (s) => s };
  const st: Status = { projects: 2, workers: 123, items: { running: 1, "needs-you": 1 } };
  for (const width of [120, 80, 72]) {
    const lines = renderBanner(theme, width, st, false);
    ok(lines.length === 6 && lines.every((l) => visibleWidth(l) <= width), `banner fits ${width}`);
    ok(lines[0].includes("B O S S") && lines[1].includes("A I   C O O") && lines[4].includes("2 projects"), `identity + status at ${width}`);
  }
  for (const width of [71, 60, 52]) {
    const lines = renderBanner(theme, width, st, false);
    ok(lines.length === 3 && lines.every((l) => visibleWidth(l) <= width) && lines[2].includes("/ops"), `medium banner fits ${width}`);
  }
  for (const width of [51, 50, 32, 12]) {
    const lines = renderBanner(theme, width, st, false);
    ok(lines.length === 2 && lines.every((l) => visibleWidth(l) <= width), `narrow banner fits ${width}`);
    if (width >= 17) ok(lines[0].includes("BOSS"), `narrow identity survives ${width}`);
  }
  ok(renderBanner(theme, 80, st, false).join("|") === renderBanner(theme, 80, st, false).join("|"), "render is stable");
  const ascii = renderBanner(theme, 80, st, true, true).join("\n");
  ok([...ascii].every((c) => c.charCodeAt(0) < 128) && ascii.includes("[B]") && ascii.includes("/ops | /inbox"), "plain fallback is ASCII and actionable");
  const ansiTheme: Theme = { fg: (_t, s) => `\u001b[31m${s}\u001b[0m`, bold: (s) => `\u001b[1m${s}\u001b[0m` };
  ok(!renderBanner(ansiTheme, 80, st, true, false).join("\n").includes("\u001b["), "no-color output contains no ANSI");
  const degraded = renderBanner(theme, 80, null, true, true).join("\n");
  ok(degraded.includes("re-run install.sh") && !degraded.includes("+----"), "degraded error outranks identity art and keeps remediation");
  ok(PROGRESS_FRAMES.every((f) => visibleWidth(f) === visibleWidth(PROGRESS_FRAMES[0])), "progress frames have stable width");
  ok(statusLine({ projects: 1, workers: null, items: {} }) === "1 project · team stopped · inbox clear", "status line when idle");
  ok(statusLine({ projects: 1, workers: 123, items: {} }) === "1 project · team ready · inbox clear", "ready schedulers are not mislabeled as hidden agents");
  ok(statusLine(st).includes("1 need you") && statusLine(st).includes("1 running"), "status line counts");
  ok(compactStatus({ projects: 1, workers: 1, items: { running: 1 } }) === "1 project · 1 running", "medium status never duplicates running count");
  ok(persistentStatus("team idle", 0, 0, true) === "[B] BOSS | team idle | /ops", "plain persistent footer keeps identity, state, and action");
  ok(degradedPersistentStatus(true) === "[B] BOSS | team tools missing | re-run install.sh | /ops", "plain degraded footer replaces stale health and keeps remediation");
  ok(!renderBanner(ansiTheme, 80, null, false, true).join("\n").includes("\u001b["), "plain degraded banner contains no ANSI even without NO_COLOR");
  const escapedReceipt = terminalText("⏰ now — résumé 界 🚀 · next", true);
  ok([...escapedReceipt].every((c) => c.charCodeAt(0) < 128) && escapedReceipt.includes("\\u754c") && escapedReceipt.includes("\\U0001f680"), "plain receipts and arbitrary user text contain only ASCII");
  ok(parseWake("20m") === 1_200_000 && parseWake("1h") === 3_600_000 && parseWake("1h30m") === 5_400_000 && parseWake("45") === 2_700_000, "wake durations parse");
  ok(parseWake("soon") === null && parseWake("0m") === null, "bad wake durations rejected");
  ok(wakeText("inbox", "x").includes("never mention bossctl") && /check-in/i.test(wakeText("timer", "")), "wake prompts carry the rules");
  ok(CONTROLLER.endsWith("/bin/bossctl"), "controller resolves beside the extension instead of depending on PATH");
  console.log(`boss.ts: ${n} checks passed`);
}
