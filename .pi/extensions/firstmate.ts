// firstmate graph — Pi extension.
// A compact status line, a live fleet strip in the footer, a working state,
// /fleet and /inbox that never spend a model turn, and a wake: the mate turns by itself
// when the crew has news or when the captain schedules a check-in (/wake 20m). The first mate's voice and
// rules are instructions in AGENTS.md, as in firstmate — nothing here forces them.
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { truncateToWidth, visibleWidth } from "@earendil-works/pi-tui";

type Theme = { fg(token: string, text: string): string; bold?(text: string): string };
type Status = { projects: number; workers: number | null; herdr_tabs?: { kind: string }[]; items: Record<string, number> };

// ------------------------------------------------------------------ rendering

const ANCHOR = "⚓";

function gutter(width: number): number { return width >= 70 ? 2 : width >= 32 ? 1 : 0; }
function inner(width: number): number { return Math.max(1, width - gutter(width) * 2); }
function fit(lines: string[], width: number): string[] {
  const pad = " ".repeat(gutter(width));
  return lines.map((l) => pad + truncateToWidth(l, inner(width), ""));
}
export function statusLine(s: Status | null): string {
  if (!s) return "crew tools missing · re-run install.sh";
  const workers = s.herdr_tabs?.some((t) => t.kind === "worker")
    ? `${s.herdr_tabs!.filter((t) => t.kind === "worker").length} workers`
    : s.workers ? "workers in background" : "workers stopped";
  const needs = (s.items["needs-you"] || 0) + (s.items["failed"] || 0) + (s.items["ready"] || 0) + (s.items["pr-open"] || 0);
  const running = s.items["running"] || 0, queued = s.items["queued"] || 0;
  const parts = [`${s.projects} project${s.projects === 1 ? "" : "s"}`, workers];
  if (running) parts.push(`${running} running`);
  if (queued) parts.push(`${queued} queued`);
  parts.push(needs ? `${needs} need you` : "inbox clear");
  return parts.join(" · ");
}

function compactStatus(s: Status | null, narrow = false): string {
  if (!s) return "crew tools unavailable";
  const needs = (s.items["needs-you"] || 0) + (s.items["failed"] || 0) + (s.items["ready"] || 0) + (s.items["pr-open"] || 0);
  const running = s.items["running"] || 0;
  const parts = [`${s.projects} project${s.projects === 1 ? "" : "s"}`];
  if (!narrow && running) parts.push(`${running} running`);
  parts.push(needs ? `${needs} need you` : running ? `${running} running` : "inbox clear");
  return parts.join(" · ");
}

export function renderBanner(theme: Theme, width: number, status: Status | null, noColor = false): string[] {
  const titlePlain = `${ANCHOR}  F I R S T   M A T E`;
  const title = noColor ? titlePlain
    : `${theme.fg("warning", ANCHOR)}  ${theme.bold?.("F I R S T   M A T E") ?? "F I R S T   M A T E"}`;
  const statePlain = statusLine(status);
  const state = noColor ? statePlain : theme.fg("muted", statePlain);
  const hintPlain = "/fleet  ·  /inbox  ·  /wake 20m";
  const hint = noColor ? hintPlain
    : `${theme.fg("accent", "/fleet")}${theme.fg("dim", "  ·  ")}${theme.fg("accent", "/inbox")}${theme.fg("dim", "  ·  ")}${theme.fg("accent", "/wake 20m")}`;
  if (width < 52) return fit([title, noColor ? compactStatus(status, true) : theme.fg("muted", compactStatus(status, true))], width);
  if (width < 72) return fit([title, noColor ? compactStatus(status) : theme.fg("muted", compactStatus(status)), hint], width);

  const compass = (text: string) => noColor ? text : theme.fg("accent", text);
  const horizonWidth = Math.max(12, Math.min(88, inner(width) - 2));
  const horizon = noColor ? "━".repeat(horizonWidth) : theme.fg("borderMuted", "━".repeat(horizonWidth));
  const deck = noColor ? "C A P T A I N ' S   C O N T R O L   D E C K"
    : theme.fg("dim", "C A P T A I N ' S   C O N T R O L   D E C K");
  return fit([
    `       ${compass("N")}`,
    `    ${compass("W  ✦  E")}       ${title}`,
    `       ${compass("S")}          ${deck}`,
    horizon,
    `${compass("◇")}  ${state}`,
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
    ? `⚓ WAKE — the crew has news:\n${detail}\n\nReport this to the captain in plain language (never mention helm): what landed, what failed, what needs a decision. Relay any question verbatim and wait for their answer.`
    : `⚓ WAKE — check-in you scheduled${detail ? ` ("${detail}")` : ""}. Look at the fleet (inbox, running work) and give the captain a short status. If nothing moved, say so in one line.`;
}

// ------------------------------------------------------------------ working state

const BOAT_FRAMES = ["⛵~~~~~", "~⛵~~~~", "~~⛵~~~", "~~~⛵~~", "~~~~⛵~", "~~~⛵~~", "~~⛵~~~", "~⛵~~~~"];
const WORKING = ["hailing the crew", "checking the ledger", "trimming the sails", "reading the evidence",
  "keeping one thread", "gates before glory", "plotting the course", "all hands, one voice"];

// ------------------------------------------------------------------ extension

export default function firstmate(pi: ExtensionAPI) {
  const helm = (...args: string[]) => pi.exec("helm", args, { timeout: 15_000 });
  const noColor = () => process.env.NO_COLOR !== undefined || process.env.TERM === "dumb";
  let ui: any;
  let poll: ReturnType<typeof setInterval> | undefined;
  let ticker: ReturnType<typeof setInterval> | undefined;
  let lastNeeds = -1;
  let lastSignature = "";
  const wakeTimers = new Map<number, { at: number; note: string; t: ReturnType<typeof setTimeout> }>();
  let wakeSeq = 0;

  async function status(): Promise<Status | null> {
    try {
      const r = await helm("status", "--json");
      return r.code === 0 ? (JSON.parse(r.stdout) as Status) : null;
    } catch { return null; }
  }

  async function refreshStrip(notifyNew = false) {
    if (!ui) return;
    const s = await status();
    if (!s) return;
    const needs = (s.items["needs-you"] || 0) + (s.items["failed"] || 0) + (s.items["ready"] || 0) + (s.items["pr-open"] || 0);
    const running = s.items["running"] || 0;
    try {
      ui.setStatus("firstmate", `${ANCHOR} ${running ? `${running} under way` : "crew idle"}${needs ? ` · ${needs} need you` : ""}`);
      // Wake the first mate when the inbox changes shape: it reports, the captain never polls.
      const signature = ["needs-you", "failed", "ready", "pr-open", "done", "merged"].map((k) => `${k}:${s.items[k] || 0}`).join(",");
      if (notifyNew && lastSignature && signature !== lastSignature && needs > lastNeeds) {
        const r = await helm("inbox", "--hints");
        if (r.code === 0) pi.sendMessage({ customType: "firstmate-wake", content: wakeText("inbox", r.stdout.trim()), display: false },
                                         { deliverAs: "followUp", triggerTurn: true });
      }
      lastSignature = signature;
    } catch {}
    lastNeeds = needs;
  }

  // Put a fresh banner at the bottom of the transcript on every launch. Historical
  // entries still render correctly, while restored sessions never open on a buried header.
  pi.registerEntryRenderer("-firstmate-hello", (entry: any, _opts: unknown, theme: Theme) => ({
    render: (width: number) => renderBanner(theme, width, entry?.data?.status ?? null, noColor()),
    invalidate() {},
  }));
  // Board and inbox as transcript entries (plain text, themed dim), not toasts.
  pi.registerEntryRenderer("-firstmate-board", (entry: any, _opts: unknown, theme: Theme) => ({
    render: (width: number) => fit(String(entry?.data?.text ?? "").split("\n").map((l: string) => noColor() ? l : theme.fg("muted", l)), width),
    invalidate() {},
  }));

  pi.on("session_start", async (_event: unknown, ctx: any) => {
    if (!ctx.hasUI) return;
    ui = ctx.ui;
    try {
      pi.appendEntry("-firstmate-hello", { status: await status() });
      ctx.ui.setTitle?.("⚓ first mate");
    } catch {}
    await refreshStrip();
    poll = setInterval(() => { refreshStrip(true).catch(() => {}); }, 8000);
    poll.unref?.();
  });

  pi.on("turn_start", async (_e: unknown, ctx: any) => {
    if (!ctx.hasUI) return;
    try {
      const nc = noColor();
      ctx.ui.setWorkingIndicator({ frames: BOAT_FRAMES.map((f) => nc ? f : ctx.ui.theme.fg("accent", f)), intervalMs: 220 });
      const paint = () => { const m = WORKING[Math.floor(Date.now() / 5000) % WORKING.length]; ctx.ui.setWorkingMessage(nc ? m : ctx.ui.theme.fg("muted", m)); };
      paint();
      if (ticker) clearInterval(ticker);
      ticker = setInterval(paint, 5000); ticker.unref?.();
    } catch {}
  });
  pi.on("turn_end", async () => { if (ticker) clearInterval(ticker); ticker = undefined; });
  pi.on("agent_end", async () => { if (ticker) clearInterval(ticker); ticker = undefined; await refreshStrip(true); });

  pi.registerCommand("wake", {
    description: "Wake the first mate later to check in: /wake 20m [note] · /wake = list · /wake clear",
    handler: async (args: string, ctx: any) => {
      const text = args.trim();
      if (!text) {
        const rows = [...wakeTimers.values()].map((w) => `⏰ in ${Math.max(1, Math.round((w.at - Date.now()) / 60000))}m${w.note ? ` — ${w.note}` : ""}`);
        ctx.ui.notify(rows.length ? rows.join("\n") : "No check-ins scheduled. /wake 20m see if the login fix landed", "info");
        return;
      }
      if (text === "clear") { for (const w of wakeTimers.values()) clearTimeout(w.t); wakeTimers.clear(); ctx.ui.notify("Check-ins cleared.", "info"); return; }
      const [when, ...rest] = text.split(/\s+/);
      const ms = parseWake(when);
      if (!ms) { ctx.ui.notify("Usage: /wake <20m|1h|1h30m> [what to check]", "warning"); return; }
      const id = ++wakeSeq, note = rest.join(" ");
      const t = setTimeout(() => {
        wakeTimers.delete(id);
        try { pi.sendMessage({ customType: "firstmate-wake", content: wakeText("timer", note), display: false }, { deliverAs: "followUp", triggerTurn: true }); } catch {}
      }, ms);
      t.unref?.();
      wakeTimers.set(id, { at: Date.now() + ms, note, t });
      ctx.ui.notify(`⏰ Aye — I'll check in in ${Math.round(ms / 60000)}m${note ? ` on "${note}"` : ""}.`, "info");
    },
  });

  pi.registerCommand("fleet", {
    description: "Fleet board: workers, projects, queue",
    handler: async (_args: string, ctx: any) => {
      const r = await helm("watch", "--once");
      if (r.code === 0) pi.appendEntry("-firstmate-board", { text: r.stdout.trim() });
      else ctx.ui.notify(`helm watch failed: ${r.stderr}`, "warning");
    },
  });
  pi.registerCommand("inbox", {
    description: "What needs the captain: questions, failures, ready branches, open PRs",
    handler: async (_args: string, ctx: any) => {
      const r = await helm("inbox");
      if (r.code === 0) pi.appendEntry("-firstmate-board", { text: `inbox\n${r.stdout.trim()}` });
      else ctx.ui.notify(`helm inbox failed: ${r.stderr}`, "warning");
    },
  });

  pi.on("session_shutdown", async () => {
    if (poll) clearInterval(poll); if (ticker) clearInterval(ticker);
    for (const w of wakeTimers.values()) clearTimeout(w.t); wakeTimers.clear();
    try { ui?.setStatus?.("firstmate", undefined); ui?.setWorkingMessage?.(); ui?.setWorkingIndicator?.(); } catch {}
    ui = undefined;
  });
}

// self-test: `bun .pi/extensions/firstmate.ts`
if (process.argv[1]?.endsWith("firstmate.ts")) {
  let n = 0;
  const ok = (v: boolean, label: string) => { if (!v) throw new Error(`FAIL: ${label}`); n++; };
  const theme: Theme = { fg: (_t, s) => s, bold: (s) => s };
  const st: Status = { projects: 2, workers: 123, items: { running: 1, "needs-you": 1 } };
  for (const width of [120, 80]) {
    const lines = renderBanner(theme, width, st, false);
    ok(lines.length === 6 && lines.every((l) => visibleWidth(l) <= width), `banner fits ${width}`);
    ok(lines[1].includes("F I R S T") && lines[4].includes("2 projects"), `identity + status at ${width}`);
  }
  ok(renderBanner(theme, 60, st, false).length === 3 && renderBanner(theme, 60, st, false)[2].includes("/fleet"), "medium banner keeps actions");
  ok(renderBanner(theme, 50, st, false)[0].includes("F I R S T") && renderBanner(theme, 50, st, false).length === 2, "narrow banner keeps identity");
  ok(renderBanner(theme, 80, st, false).join("|") === renderBanner(theme, 80, st, false).join("|"), "render is stable");
  ok(statusLine({ projects: 1, workers: null, items: {} }) === "1 project · workers stopped · inbox clear", "status line when idle");
  ok(statusLine(st).includes("1 need you") && statusLine(st).includes("1 running"), "status line counts");
  ok(parseWake("20m") === 1_200_000 && parseWake("1h") === 3_600_000 && parseWake("1h30m") === 5_400_000 && parseWake("45") === 2_700_000, "wake durations parse");
  ok(parseWake("soon") === null && parseWake("0m") === null, "bad wake durations rejected");
  ok(wakeText("inbox", "x").includes("never mention helm") && wakeText("timer", "").includes("check-in"), "wake prompts carry the rules");
  console.log(`firstmate.ts: ${n} checks passed`);
}
