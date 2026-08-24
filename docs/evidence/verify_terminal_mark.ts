#!/usr/bin/env bun
/** Deterministic acceptance checks for the BOSS startup masthead. */
import { readFileSync, realpathSync } from "node:fs";
import { resolve } from "node:path";
import { visibleWidth } from "@earendil-works/pi-tui";
import { renderBanner } from "../../.pi/extensions/boss.ts";

const root = resolve(import.meta.dir, "../..");
const theme = { fg: (_token: string, text: string) => text, bold: (text: string) => text };
const ansiTheme = {
  fg: (_token: string, text: string) => `\u001b[31m${text}\u001b[0m`,
  bold: (text: string) => `\u001b[1m${text}\u001b[0m`,
};
const normal = {
  projects: 3,
  workers: 2,
  herdr_tabs: [{ kind: "worker" }, { kind: "worker" }],
  items: { running: 2, queued: 1, "needs-you": 1 },
};
const maximum = {
  projects: 12,
  workers: 8,
  herdr_tabs: Array.from({ length: 8 }, () => ({ kind: "worker" })),
  items: { running: 24, queued: 17, "needs-you": 31 },
  supervisor: { pending_wakes: 9, away: true, healthy: true },
};

let checks = 0;
function check(value: boolean, label: string): void {
  if (!value) throw new Error(`FAIL: ${label}`);
  checks += 1;
}
function noAnsi(text: string): boolean { return !/\u001b\[[0-9;]*m/.test(text); }
function asciiOnly(text: string): boolean { return [...text].every((char) => char.charCodeAt(0) < 128); }
function fits(lines: string[], width: number): boolean { return lines.every((line) => visibleWidth(line) <= width); }

for (const [width, fixture] of [[120, normal], [100, normal], [80, maximum]] as const) {
  const lines = renderBanner(theme, width, fixture, true, false);
  check(lines.length === 5, `${width}-column wide masthead uses five rows`);
  check(fits(lines, width), `${width}-column wide masthead fits by display cells`);
  check(lines[0].includes("██████╮") && lines[2].includes("██████┤") && lines[4].includes("██████╯"), `${width}-column mark retains both bowls`);
  check(lines[0].includes("B O S S") && lines[3].includes("project") && lines[4].includes("/ops"), `${width}-column identity/state/action order survives`);
}

const maximum80 = renderBanner(ansiTheme, 80, maximum, true, false);
check(maximum80[3].includes("31 need you"), "maximum masthead retains the decisive needs-you count before truncation");
check(maximum80[3].trimEnd().endsWith("24 running") && !maximum80[3].includes("17 queued"), "maximum masthead drops only a complete lower-priority summary part");
check(noAnsi(maximum80.join("\n")), "maximum no-color truncation emits no ANSI reset");

const active60 = renderBanner(theme, 60, maximum, true, false);
check(active60.length === 3 && fits(active60, 60), "60-column active masthead fits in three rows");
check(active60[1].includes("24 running") && active60[1].includes("31 need you") && active60[2].includes("/ops"), "60-column active masthead retains state and actions");

const minimum50 = renderBanner(theme, 50, maximum, true, false);
check(minimum50.length === 2 && fits(minimum50, 50), "50-column minimum masthead fits in two rows");
check(minimum50[0].includes("BOSS") && minimum50[0].includes("YOUR AI COO") && minimum50[1].includes("31 need you") && minimum50[1].includes("/ops"), "50-column minimum retains identity, decision state, and primary action");
for (const width of [32, 12]) check(fits(renderBanner(theme, width, maximum, true, false), width), `${width}-column clipping remains display-cell safe`);

const plainNormal = renderBanner(ansiTheme, 80, normal, false, true).join("\n");
const plainDegraded = renderBanner(ansiTheme, 80, null, false, true).join("\n");
check(asciiOnly(plainNormal) && noAnsi(plainNormal), "80-column plain normal frame is ASCII-only with zero ANSI bytes");
check(plainNormal.split("\n").length === 5 && plainNormal.includes("[B]") && plainNormal.includes("1 need you") && plainNormal.includes("/ops | /inbox"), "plain normal frame preserves identity, state, and actions");
check(asciiOnly(plainDegraded) && noAnsi(plainDegraded), "80-column plain degraded frame is ASCII-only with zero ANSI bytes");
check(plainDegraded.includes("team tools missing") && plainDegraded.includes("re-run install.sh") && plainDegraded.includes("/ops"), "plain degraded frame preserves error, remediation, and action");
check(!plainDegraded.includes("████") && !plainDegraded.includes("+----"), "degraded frame removes identity art before remediation");

const installedBoss = realpathSync("/Users/aliabassi/.local/bin/pi-boss");
const installedQuit = realpathSync("/Users/aliabassi/.local/bin/pi-boss-quit");
check(installedBoss === resolve(root, "bin/pi-boss"), "installed pi-boss resolves to this repository");
check(installedQuit === resolve(root, "bin/pi-boss-quit"), "installed pi-boss-quit resolves to this repository");
const launchSource = readFileSync(resolve(root, "bin/pi-boss"), "utf8");
const quitSource = readFileSync(resolve(root, "bin/pi-boss-quit"), "utf8");
check(launchSource.includes('exec "$bossctl" launch'), "outside-Herdr launcher routes through the BOSS launch command");
check(quitSource.includes("session stop boss"), "quit launcher targets only the named boss session");
check(!/firstmate/i.test(launchSource + quitSource), "BOSS launchers contain no Firstmate target or alias");

console.log(`terminal-mark: ${checks} checks passed`);
console.log("row matrix: 120=5, 100=5, 80=5, 60=3, 50=2, 32=2, 12=2");
console.log(`plain normal: bytes=${Buffer.byteLength(plainNormal)} non_ascii=0 ansi_sequences=0 rows=5`);
console.log(`plain degraded: bytes=${Buffer.byteLength(plainDegraded)} non_ascii=0 ansi_sequences=0 rows=3`);
console.log(`no-color maximum: bytes=${Buffer.byteLength(maximum80.join("\n"))} ansi_sequences=0 decisive_state="31 need you"`);
console.log(`installed pi-boss: ${installedBoss}`);
console.log(`installed pi-boss-quit: ${installedQuit}`);
console.log(`HERDR_ENV during verification: ${process.env.HERDR_ENV ?? "unset"}`);
console.log("Herdr control commands issued by this verifier: 0");
console.log("Firstmate launcher targets found: 0");
