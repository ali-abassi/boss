// Narrow runtime attestation for Herdr-started Pi agents.
// This does not create or imitate Pi's session JSONL. Pi writes that durable
// record after its first assistant event; First Mate validates it separately.
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { execFileSync } from "node:child_process";
import { createHash, generateKeyPairSync, sign as cryptoSign, type KeyObject } from "node:crypto";
import { appendFileSync, existsSync, linkSync, mkdirSync, realpathSync, unlinkSync, writeFileSync } from "node:fs";
import { dirname, isAbsolute, relative, resolve } from "node:path";

function inside(root: string, candidate: string): boolean {
  const rel = relative(root, candidate);
  return rel === "" || (!rel.startsWith("..") && !isAbsolute(rel));
}

function canonical(path: string): string {
  const absolute = resolve(path);
  if (existsSync(absolute)) return realpathSync(absolute);
  const parent = dirname(absolute);
  return resolve(existsSync(parent) ? realpathSync(parent) : parent, absolute.slice(parent.length + 1));
}

function shellQuote(value: string): string {
  return `'${value.replace(/'/g, `'"'"'`)}'`;
}

function processField(name: "lstart" | "command" | "pgid"): string {
  let lastError: unknown;
  for (const binary of ["/bin/ps", "/usr/bin/ps"]) {
    try {
      const value = execFileSync(binary, ["-p", String(process.pid), "-o", `${name}=`], {
        encoding: "utf8",
        timeout: 3000,
        stdio: ["ignore", "pipe", "ignore"],
      }).trim();
      if (value) return value;
    } catch (error) {
      lastError = error;
    }
  }
  throw new Error(`cannot attest Pi process ${name}: ${String(lastError ?? "empty ps result")}`);
}

function processBirthIdentity(sessionId: string) {
  const started = processField("lstart");
  const command = processField("command");
  const pgid = Number(processField("pgid"));
  if (!Number.isSafeInteger(pgid) || pgid <= 0) throw new Error("cannot attest Pi process group");
  return {
    version: 1,
    kind: "firstmate-pi-agent",
    pid: process.pid,
    pgid,
    owner: sessionId,
    start_sha256: createHash("sha256").update(started, "utf8").digest("hex"),
    command_sha256: createHash("sha256").update(command, "utf8").digest("hex"),
    registered_at: new Date().toISOString(),
  };
}

function mentionedAbsolutePaths(command: string): string[] {
  const withoutUrls = command.replace(/\b[a-z][a-z0-9+.-]*:\/\/[^\s'"`]+/gi, "");
  return withoutUrls.match(/\/(?:[^\s'"`;|&<>()[\]{}\\]|\\.)+/g) ?? [];
}

export function scopeViolation(toolName: string, input: any, rootValue?: string,
                               allowedValues: string[] = []): string | undefined {
  if (!["read", "write", "edit", "bash"].includes(toolName)) {
    return `tool ${toolName || "<missing>"} is outside the managed read/write/edit/bash capability set`;
  }
  if (!rootValue) return "First Mate worktree boundary is missing";
  const root = canonical(rootValue);
  const allowed = new Set(allowedValues.filter(Boolean).map(canonical));
  const pathAllowed = (raw: unknown) => {
    if (typeof raw !== "string" || !raw.trim()) return false;
    const target = canonical(isAbsolute(raw) ? raw : resolve(root, raw));
    return inside(root, target) || allowed.has(target);
  };
  if (toolName !== "bash") {
    const raw = input?.path ?? input?.file_path ?? input?.filePath;
    if ((toolName === "write" || toolName === "edit") && typeof raw === "string") {
      const target = canonical(isAbsolute(raw) ? raw : resolve(root, raw));
      const rel = relative(root, target);
      if (rel === ".git" || rel.startsWith(`.git/`)) return "Git metadata is controller-only";
    }
    return pathAllowed(raw) ? undefined : `${toolName} path escapes the owned worktree`;
  }
  const command = String(input?.command ?? "");
  if (!command.trim()) return;
  if (/(?:^|[;&|()\n]\s*|\b(?:sudo|command|exec|xargs)\s+)(?:git\s+(?:push|fetch|pull|clone|remote)|gh\b|curl\b|wget\b|ssh\b|scp\b|sftp\b|rsync\b|nc\b|ncat\b|security\b)/i.test(command)) {
    return "network, remote Git/forge mutation, and host credential commands are controller-only";
  }
  if (/(^|[\s'"`/])\.\.($|[\s'"`/])/.test(command)) return "parent traversal is outside the owned worktree";
  if (/(?:^|[^\\])~(?:\/|\b)|\$(?:\{)?(?:HOME|HELM_HOME|PI_CODING_AGENT_DIR|CODEX_HOME|HELM_AGENT_)/.test(command)) {
    return "home, control-plane, or attestation environment access is forbidden";
  }
  const bareSet = /(?:^|[;\n&|]\s*)set(?:\s*(?:$|[;\n&|]))/.test(command);
  if (bareSet || /(?:^|[;&|\n]\s*|\bsudo\s+)env(?:\s|$)|\bprintenv\b|\bexport(?:\s|$)|\bdeclare\s+-x\b|\btypeset\s+-x\b|\bos\.environ\b|\bprocess\.env\b|\bgetenv\s*\(|\bENVIRON\b|\blaunchctl\s+getenv\b|\/proc\/(?:self|[0-9]+)\/environ/.test(command)) {
    return "process environment inspection is forbidden in a managed agent";
  }
  if (/(?:^|[;&|\n]\s*)ps\s+(?:-[A-Za-z]*[eE][A-Za-z]*|[^;&|\n]*\b(?:eww|e|E)\b)/.test(command)
      || /\bsysctl\s+[^;&|\n]*kern\.procargs/.test(command)) {
    return "process argument/environment inspection is forbidden in a managed agent";
  }
  for (const raw of mentionedAbsolutePaths(command)) {
    if (["/dev/null", "/dev/stdin", "/dev/stdout", "/dev/stderr"].includes(raw)) continue;
    if (!pathAllowed(raw.replace(/\\(.)/g, "$1"))) return "shell command names a path outside the owned worktree";
  }
  return;
}

export default function attest(pi: ExtensionAPI) {
  const destination = process.env.HELM_AGENT_ATTESTATION;
  const eventsPath = process.env.HELM_AGENT_EVENTS;
  const nonce = process.env.HELM_AGENT_NONCE;
  const agentRoot = process.env.HELM_AGENT_ROOT;
  const allowedWrites = (process.env.HELM_AGENT_ALLOWED_WRITES ?? "").split("\n").filter(Boolean);
  const sandboxRequired = process.env.HELM_SANDBOX_REQUIRED === "1";
  const sandboxProfileSha256 = process.env.HELM_SANDBOX_PROFILE_SHA256;
  const toolSandboxProfile = process.env.HELM_TOOL_SANDBOX_PROFILE;
  const toolSandboxProfileSha256 = process.env.HELM_TOOL_SANDBOX_PROFILE_SHA256;
  const toolRunner = process.env.HELM_TOOL_RUNNER;
  const toolSandboxTmp = process.env.TMPDIR;
  const sandboxProbe = process.env.HELM_SANDBOX_PROBE;
  let inputSequence = 0;
  let activeSequence = 0;
  const pendingSequences: number[] = [];
  let eventSequence = 0;
  let previousEventSha256 = "0".repeat(64);
  let eventPrivateKey: KeyObject | undefined;
  let eventPublicKey = "";

  function appendEvent(type: string, data: Record<string, unknown> = {}, sequence = inputSequence) {
    if (!eventsPath || !nonce || !eventPrivateKey) return;
    eventSequence += 1;
    const unsigned = {
      schema: 1,
      nonce,
      event_sequence: eventSequence,
      previous_sha256: previousEventSha256,
      type,
      input_sequence: sequence,
      at: new Date().toISOString(),
      ...data,
    };
    const eventSha256 = createHash("sha256").update(JSON.stringify(unsigned), "utf8").digest("hex");
    const signature = cryptoSign(null, Buffer.from(eventSha256, "hex"), eventPrivateKey).toString("base64");
    appendFileSync(eventsPath, JSON.stringify({ ...unsigned, event_sha256: eventSha256, signature }) + "\n",
                   { encoding: "utf8", mode: 0o600 });
    previousEventSha256 = eventSha256;
  }

  pi.on("session_start", async (event: any, ctx: any) => {
    if (!destination && !eventsPath && !nonce) return;
    if (!destination || !eventsPath || !nonce || !ctx.model) {
      throw new Error("incomplete First Mate runtime-attestation request");
    }
    if (!sandboxRequired || !sandboxProfileSha256 || !toolSandboxProfile || !toolRunner
        || !toolSandboxProfileSha256 || !sandboxProbe) {
      throw new Error("managed Pi session has no pinned OS sandbox request");
    }
    let sandboxProbeBlocked = false;
    try {
      writeFileSync(sandboxProbe, "sandbox must block this\n", { flag: "wx", mode: 0o600 });
      try { unlinkSync(sandboxProbe); } catch {}
      throw new Error("managed Pi session escaped its OS write sandbox");
    } catch (error: any) {
      if (error?.code === "EPERM" || error?.code === "EACCES") sandboxProbeBlocked = true;
      else throw error;
    }
    mkdirSync(dirname(eventsPath), { recursive: true, mode: 0o700 });
    writeFileSync(eventsPath, "", { flag: "wx", mode: 0o600 });
    const keyPair = generateKeyPairSync("ed25519");
    eventPrivateKey = keyPair.privateKey;
    eventPublicKey = keyPair.publicKey.export({ type: "spki", format: "der" }).toString("base64");
    const sessionId = ctx.sessionManager.getSessionId();
    const payload = {
      schema: 1,
      nonce,
      pid: process.pid,
      process_identity: processBirthIdentity(sessionId),
      cwd: ctx.sessionManager.getCwd(),
      session_id: sessionId,
      session_dir: ctx.sessionManager.getSessionDir(),
      session_file: ctx.sessionManager.getSessionFile(),
      events_file: eventsPath,
      event_public_key: eventPublicKey,
      model: `${ctx.model.provider}/${ctx.model.id}`,
      thinking: pi.getThinkingLevel(),
      herdr: {
        session: process.env.HERDR_SESSION ?? null,
        workspace_id: process.env.HERDR_WORKSPACE_ID ?? null,
        tab_id: process.env.HERDR_TAB_ID ?? null,
        pane_id: process.env.HERDR_PANE_ID ?? null,
      },
      sandbox: { required: true, profile_sha256: sandboxProfileSha256,
                 tool_profile_sha256: toolSandboxProfileSha256, probe_blocked: sandboxProbeBlocked },
      reason: event?.reason ?? "unknown",
      attested_at: new Date().toISOString(),
    };
    mkdirSync(dirname(destination), { recursive: true, mode: 0o700 });
    const temporary = `${destination}.tmp.${process.pid}`;
    writeFileSync(temporary, JSON.stringify(payload) + "\n", { flag: "wx", mode: 0o600 });
    try {
      // link is an atomic no-overwrite publish on the same filesystem.
      linkSync(temporary, destination);
    } finally {
      unlinkSync(temporary);
    }
    // Keep capabilities in closure memory; do not hand controller paths/nonces
    // to later bash children through their inherited environment.
    for (const key of ["HELM_AGENT_ATTESTATION", "HELM_AGENT_EVENTS", "HELM_AGENT_NONCE",
                       "HELM_AGENT_ROOT", "HELM_AGENT_ALLOWED_WRITES", "HELM_SANDBOX_REQUIRED",
                       "HELM_SANDBOX_PROFILE_SHA256", "HELM_TOOL_SANDBOX_PROFILE",
                       "HELM_TOOL_SANDBOX_PROFILE_SHA256", "HELM_TOOL_RUNNER",
                       "HELM_SANDBOX_PROBE", "HELM_HOME"]) {
      delete process.env[key];
    }
  });

  pi.on("tool_call", async (event: any) => {
    const reason = scopeViolation(String(event.toolName ?? ""), event.input, agentRoot, allowedWrites);
    if (reason) return { block: true, reason: `First Mate blocked tool call: ${reason}` };
    if (event.toolName === "bash") {
      const original = String(event.input?.command ?? "");
      const safePath = process.env.PATH ?? "/usr/bin:/bin";
      const toolHome = toolSandboxTmp ?? dirname(toolSandboxProfile!);
      const requested = Number(event.input?.timeout ?? 3600);
      const boundedTimeout = Number.isFinite(requested) ? Math.max(1, Math.min(3600, Math.floor(requested))) : 3600;
      event.input.command = [
        "/usr/bin/env", "-i", shellQuote(`PATH=${safePath}`), shellQuote(`HOME=${toolHome}`),
        shellQuote(`TMPDIR=${toolHome}`), shellQuote("LANG=C.UTF-8"), shellQuote("GIT_OPTIONAL_LOCKS=0"),
        shellQuote(toolRunner!), shellQuote(toolSandboxProfile!), shellQuote(toolSandboxProfileSha256!),
        shellQuote(toolHome), shellQuote(String(boundedTimeout)), shellQuote(original),
      ].join(" ");
    }
  });

  pi.on("input", async (event: any, ctx: any) => {
    if (!eventsPath) return { action: "continue" as const };
    inputSequence += 1;
    pendingSequences.push(inputSequence);
    appendEvent("input", {
      session_id: ctx.sessionManager.getSessionId(),
      prompt_sha256: createHash("sha256").update(String(event.text), "utf8").digest("hex"),
      prompt_bytes: Buffer.byteLength(String(event.text), "utf8"),
      source: event.source,
      streaming_behavior: event.streamingBehavior ?? null,
      model: ctx.model ? `${ctx.model.provider}/${ctx.model.id}` : null,
      thinking: pi.getThinkingLevel(),
    });
    return { action: "continue" as const };
  });

  pi.on("before_agent_start", async (_event: any, ctx: any) => {
    // Inputs can be queued while a turn is active. Bind lifecycle events to
    // the sequence that actually starts instead of the most recently typed
    // input, otherwise an earlier settle could falsely acknowledge steering.
    activeSequence = pendingSequences.shift() ?? inputSequence;
    appendEvent("agent_start", { session_id: ctx.sessionManager.getSessionId() }, activeSequence);
  });

  pi.on("agent_settled", async (_event: any, ctx: any) => {
    appendEvent("agent_settled", { session_id: ctx.sessionManager.getSessionId() }, activeSequence);
    activeSequence = 0;
  });
}

// Deterministic guard checks without starting Pi. `bun helm/pi_attest.ts`
if (process.argv[1]?.endsWith("pi_attest.ts")) {
  const root = "/tmp/firstmate-owned";
  const allowed = ["/tmp/firstmate-state/verdict.json"];
  const ok = (condition: boolean, message: string) => { if (!condition) throw new Error(`FAIL: ${message}`); };
  ok(!scopeViolation("write", { path: "src/a.ts" }, root, allowed), "relative owned write");
  ok(Boolean(scopeViolation("write", { path: "../other/a.ts" }, root, allowed)), "write traversal blocked");
  ok(Boolean(scopeViolation("write", { path: ".git/config" }, root, allowed)), "Git metadata write blocked");
  ok(Boolean(scopeViolation("read", { path: "/etc/passwd" }, root, allowed)), "outside read blocked");
  ok(!scopeViolation("write", { path: allowed[0] }, root, allowed), "single controller verdict allowed");
  ok(!scopeViolation("bash", { command: "git status && python3 -m unittest" }, root, allowed), "relative shell allowed");
  ok(!scopeViolation("bash", { command: "set -e\npython3 -m unittest" }, root, allowed), "shell safety flags allowed");
  ok(Boolean(scopeViolation("bash", { command: "set" }, root, allowed)), "bare environment dump blocked");
  ok(!scopeViolation("bash", { command: `git -C ${root} status 2>/dev/null` }, root, allowed), "owned absolute path allowed");
  ok(Boolean(scopeViolation("bash", { command: "git -C /tmp/other status" }, root, allowed)), "other worktree blocked");
  ok(Boolean(scopeViolation("bash", { command: "python3 -c 'import os; print(os.environ)'" }, root, allowed)), "environment blocked");
  ok(Boolean(scopeViolation("bash", { command: "env" }, root, allowed)), "env command blocked");
  ok(Boolean(scopeViolation("bash", { command: "export" }, root, allowed)), "export dump blocked");
  ok(Boolean(scopeViolation("bash", { command: "ps eww" }, root, allowed)), "process environment blocked");
  ok(Boolean(scopeViolation("bash", { command: "echo x > $HELM_AGENT_EVENTS" }, root, allowed)), "ledger variable blocked");
  ok(Boolean(scopeViolation("bash", { command: "git push origin HEAD:main" }, root, allowed)), "git push blocked");
  ok(Boolean(scopeViolation("bash", { command: "gh pr merge 7 --merge" }, root, allowed)), "forge mutation blocked");
  ok(Boolean(scopeViolation("bash", { command: "curl -X POST https://example.invalid" }, root, allowed)), "network client blocked");
  ok(Boolean(scopeViolation("custom", {}, root, allowed)), "unknown tools fail closed");
  console.log("pi-attest: signed runtime attestation + 19 scope checks passed");
}
