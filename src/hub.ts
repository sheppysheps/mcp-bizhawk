// mcp-bizhawk shared hub
// ──────────────────────
// One background process that owns BizHawk's TCP port, so several Claude
// sessions (one per repo) can drive the SAME BizHawk:
//
//   session A (MCP stdio) ──┐
//                           ├──► hub ──► port 8766 ◄── BizHawk + bridge.lua
//   session B (MCP stdio) ──┘   (control port 8765)
//
// Each session's mcp-bizhawk is a thin client (src/hub-client.ts) that starts
// the hub on first use. The hub outlives sessions, so switching repos or
// reloading VS Code no longer cuts BizHawk off. It exits on its own once no
// session is attached and BizHawk is gone for `idleMs`.
//
// Control protocol: newline-delimited JSON over 127.0.0.1 only.
//   client → hub: {"id":1,"op":"hello"|"call"|"launch"|"close"|"status"|"shutdown_if_idle", ...}
//   hub → client: {"id":1,"ok":true,"result":...} | {"id":1,"ok":false,"error":"..."}
// The first line on a connection must be a valid "hello"; anything else
// (e.g. an HTTP request a web page aimed at this port) closes the connection
// before any command runs. There's no token: the maintainer chose local-only
// access as sufficient for this single-user development setup.

import net from "node:net";
import { BizhawkServer } from "./bizhawk.js";
import { BizhawkLauncher } from "./launcher.js";
import { LocalBackend, type BackendStatus } from "./backend.js";
import { buildId } from "./version.js";

export interface HubOptions {
  host: string;
  /** Port BizHawk dials (BIZHAWK_PORT). */
  bizhawkPort: number;
  /** Port sessions connect to (BIZHAWK_HUB_PORT). */
  controlPort: number;
  /** Exit after this long with no sessions and no BizHawk (ms). */
  idleMs: number;
  /** Called once when the hub stops (idle, shutdown_if_idle, or stop()). */
  onStop?: () => void;
  /** Build identity reported to clients (defaults to this build). */
  build?: string;
}

export interface HubHandle {
  backend: LocalBackend;
  sessions(): number;
  stop(): void;
}

const MAX_LINE = 4 * 1024 * 1024;  // a single control message (e.g. eval code) is far smaller

/**
 * Start the hub: bind the control port first (so a second hub fails fast),
 * then BizHawk's port (waiting if another process holds it).
 * @returns HubHandle once both are set up. Rejects with EADDRINUSE when
 *          another hub already owns the control port.
 * @requires node:net; BIZHAWK_EXE for launches (see src/launcher.ts).
 */
export async function startHub(opts: HubOptions): Promise<HubHandle> {
  const build = opts.build ?? buildId();
  const started = Date.now();
  const sockets = new Set<net.Socket>();
  let lastActivity = Date.now();
  let stopped = false;

  const control = net.createServer();
  await new Promise<void>((resolve, reject) => {
    control.once("error", reject);
    control.listen(opts.controlPort, opts.host, () => { control.off("error", reject); resolve(); });
  });

  const bh = new BizhawkServer({ host: opts.host, port: opts.bizhawkPort });
  const backend = new LocalBackend(bh, new BizhawkLauncher(), "hub");
  await bh.startOrWait();

  /** Stop everything once. */
  const stop = () => {
    if (stopped) return;
    stopped = true;
    clearInterval(idleTimer);
    for (const s of sockets) s.destroy();
    control.close();
    bh.stop();
    opts.onStop?.();
  };

  /** Hub-level status on top of the backend's. */
  const status = async (): Promise<BackendStatus> => ({
    ...(await backend.status()),
    hubPid: process.pid,
    hubBuild: build,
    hubUptimeSeconds: Math.round((Date.now() - started) / 1000),
    sessions: sockets.size,
  });

  /**
   * Run one control request.
   * @returns the result to send back; throws to send an error.
   * @requires The backend.
   */
  const handle = async (msg: Record<string, unknown>): Promise<unknown> => {
    switch (msg.op) {
      case "call":   return backend.call(String(msg.method), (msg.params ?? {}) as Record<string, unknown>);
      case "launch": return backend.launch(String(msg.rom), Number(msg.waitSeconds ?? 30));
      case "close":  return backend.close();
      case "status": return status();
      case "shutdown_if_idle": {
        // Only when BizHawk isn't connected: restarting the hub would cut it off.
        if (bh.isConnected()) return { shutdown: false };
        setImmediate(stop);
        return { shutdown: true };
      }
      default: throw new Error(`unknown hub op: ${String(msg.op)}`);
    }
  };

  control.on("connection", (sock) => {
    let buf = "";
    let greeted = false;
    sock.setEncoding("utf8");
    sock.setNoDelay(true);
    const reply = (obj: unknown) => { if (!sock.destroyed) sock.write(JSON.stringify(obj) + "\n"); };

    sock.on("data", (chunk: string) => {
      buf += chunk;
      if (buf.length > MAX_LINE) { sock.destroy(); return; }
      let nl: number;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (!line) continue;
        let msg: Record<string, unknown>;
        try { msg = JSON.parse(line); } catch { sock.destroy(); return; }
        if (!greeted) {
          // Gate: the first message must be a proper hello, or we hang up.
          if (msg.op !== "hello" || typeof msg.id !== "number") { sock.destroy(); return; }
          greeted = true;
          sockets.add(sock);
          lastActivity = Date.now();
          reply({ id: msg.id, ok: true, result: { build, pid: process.pid } });
          continue;
        }
        handle(msg).then(
          (result) => reply({ id: msg.id, ok: true, result }),
          (err: Error) => reply({ id: msg.id, ok: false, error: err.message }),
        );
      }
    });
    sock.on("close", () => { if (sockets.delete(sock)) lastActivity = Date.now(); });
    sock.on("error", () => { /* handled by close */ });
  });

  // Idle shutdown: no sessions and no BizHawk for idleMs.
  const idleTimer = setInterval(() => {
    if (sockets.size > 0 || bh.isConnected()) { lastActivity = Date.now(); return; }
    if (Date.now() - lastActivity >= opts.idleMs) stop();
  }, Math.max(50, Math.min(30_000, Math.floor(opts.idleMs / 4))));

  return { backend, sessions: () => sockets.size, stop };
}

/**
 * Entry point for `node dist/index.js --hub` (started detached by a session).
 * @returns nothing; exits the process when the hub stops, or immediately (0)
 *          if another hub already owns the control port.
 * @requires BIZHAWK_HOST / BIZHAWK_PORT / BIZHAWK_HUB_PORT / MCP_BIZHAWK_HUB_IDLE_MS env (all optional).
 */
export async function runHubProcess(): Promise<void> {
  const host = process.env.BIZHAWK_HOST ?? "127.0.0.1";
  const bizhawkPort = parseInt(process.env.BIZHAWK_PORT ?? "8766", 10);
  const controlPort = parseInt(process.env.BIZHAWK_HUB_PORT ?? String(bizhawkPort - 1), 10);
  const idleMs = parseInt(process.env.MCP_BIZHAWK_HUB_IDLE_MS ?? "300000", 10);
  try {
    await startHub({ host, bizhawkPort, controlPort, idleMs, onStop: () => process.exit(0) });
    process.stderr.write(`[mcp-bizhawk hub] pid ${process.pid}: control ${host}:${controlPort}, BizHawk ${host}:${bizhawkPort}\n`);
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code === "EADDRINUSE") process.exit(0);  // another hub won the race
    process.stderr.write(`[mcp-bizhawk hub] fatal: ${err}\n`);
    process.exit(1);
  }
}
