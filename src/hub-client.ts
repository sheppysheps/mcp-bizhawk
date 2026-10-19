// mcp-bizhawk hub client
// ──────────────────────
// The Backend each Claude session uses by default: it forwards every tool call
// to the shared hub (src/hub.ts), starting the hub on first use.
//
// WHY THIS FILE STARTS A PROCESS
// When no hub is running, ensureHub() spawns one. That is a process launch, so
// it is kept narrow: the executable is the Node binary already running this
// server (process.execPath), the script is this package's own dist/index.js,
// and the only argument is the fixed flag "--hub". No input from tool calls
// reaches the command line. The hub is detached so it outlives this session.

import net from "node:net";
import { spawn } from "node:child_process";
import { openSync, closeSync } from "node:fs";
import { fileURLToPath } from "node:url";
import type { Backend, BackendStatus } from "./backend.js";
import { buildId } from "./version.js";

export interface RemoteOptions {
  host: string;
  controlPort: number;
  /** Start a hub process. Defaults to spawning this package's `dist/index.js --hub`. */
  spawnHub?: () => void;
  /** How long to wait for a freshly spawned hub to accept connections (ms). */
  startTimeoutMs?: number;
  /** This session's build (defaults to buildId()). */
  build?: string;
}

interface Pending { resolve: (v: unknown) => void; reject: (e: Error) => void; timer: NodeJS.Timeout }

/** Per-request time limits (ms). Launch waits for BizHawk to boot. */
const TIMEOUT = { call: 120_000, launch: 300_000, close: 60_000, status: 10_000, hello: 10_000, shutdown_if_idle: 10_000 };

/**
 * Spawn `node <this package>/dist/index.js --hub`, detached and windowless.
 * @returns nothing; the hub reports readiness by accepting connections.
 * @requires node:child_process. See the header for why this is allowed.
 */
export function spawnHubProcess(): void {
  const entry = fileURLToPath(new URL("./index.js", import.meta.url));
  // The detached hub has no console. If the user sets MCP_BIZHAWK_HUB_LOG to a
  // file path, its stdout/stderr are appended there for troubleshooting; that
  // user-chosen file is the only thing written, and nothing is written by default.
  const logPath = process.env.MCP_BIZHAWK_HUB_LOG;
  let logFd: number | undefined;
  if (logPath) { try { logFd = openSync(logPath, "a"); } catch { logFd = undefined; } }
  const out = logFd ?? "ignore";
  const child = spawn(process.execPath, [entry, "--hub"], {
    detached: true,
    stdio: ["ignore", out, out],
    windowsHide: true,
    env: process.env,
  });
  child.unref();
  if (logFd !== undefined) closeSync(logFd);  // the child keeps its own copy
}

export class RemoteBackend implements Backend {
  private sock: net.Socket | null = null;
  private buf = "";
  private nextId = 1;
  private pending = new Map<number, Pending>();
  private connecting: Promise<void> | null = null;
  private stale = false;
  private readonly build: string;
  private readonly spawnHub: () => void;
  private readonly startTimeoutMs: number;

  constructor(private readonly opts: RemoteOptions) {
    this.build = opts.build ?? buildId();
    this.spawnHub = opts.spawnHub ?? spawnHubProcess;
    this.startTimeoutMs = opts.startTimeoutMs ?? 15_000;
  }

  call<T = unknown>(method: string, params: Record<string, unknown> = {}): Promise<T> {
    return this.request("call", { method, params }) as Promise<T>;
  }
  async launch(rom: string, waitSeconds: number): Promise<string> {
    return String(await this.request("launch", { rom, waitSeconds }));
  }
  async close(): Promise<string> {
    return String(await this.request("close", {}));
  }
  async status(): Promise<BackendStatus> {
    return { ...(await this.request("status", {}) as BackendStatus), staleHub: this.stale };
  }

  /**
   * One-line warning when the hub runs an older build than this session.
   * @returns the warning, or undefined when builds match.
   * @requires Nothing.
   */
  notice(): string | undefined {
    return this.stale
      ? "Note: the shared hub is running an older mcp-bizhawk build. It restarts with this build " +
        "automatically once BizHawk is closed (bizhawk_close)."
      : undefined;
  }

  /** Drop the connection (session shutting down). The hub keeps running. */
  disconnect(): void {
    this.sock?.destroy();
    this.sock = null;
  }

  /**
   * Send one control request, connecting (and starting a hub) first if needed.
   * @returns the hub's result; rejects with the hub's error message, on
   *          timeout, or if the connection drops mid-request.
   * @requires A reachable or startable hub.
   */
  private async request(op: keyof typeof TIMEOUT, payload: Record<string, unknown>): Promise<unknown> {
    await this.ensureHub();
    return this.send(op, payload);
  }

  /** Write a request on the current socket and wait for its reply. */
  private send(op: keyof typeof TIMEOUT, payload: Record<string, unknown>): Promise<unknown> {
    const sock = this.sock;
    if (!sock) return Promise.reject(new Error("not connected to the mcp-bizhawk hub"));
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`mcp-bizhawk hub didn't answer "${op}" within ${TIMEOUT[op] / 1000}s`));
      }, TIMEOUT[op]);
      this.pending.set(id, { resolve, reject, timer });
      sock.write(JSON.stringify({ id, op, ...payload }) + "\n");
    });
  }

  /**
   * Make sure we're connected to a hub running this build when possible.
   * Starts a hub if none answers. If the hub is older and BizHawk isn't
   * connected, asks it to exit and starts a fresh one; otherwise keeps using
   * it and flags `stale` (see notice()).
   * @returns once connected and greeted. Rejects if no hub could be reached.
   * @requires node:net; spawnHub.
   */
  private async ensureHub(): Promise<void> {
    if (this.sock && !this.stale) return;
    if (this.connecting) return this.connecting;
    this.connecting = (async () => {
      try {
        if (!this.sock) await this.connectOrStart();
        if (this.stale) {
          // Retry the upgrade on every call until BizHawk is closed.
          const r = await this.send("shutdown_if_idle", {}) as { shutdown: boolean };
          if (r.shutdown) {
            await this.waitClosed();
            this.stale = false;
            await this.connectOrStart();
          }
        }
      } finally {
        this.connecting = null;
      }
    })();
    return this.connecting;
  }

  /** Connect to the hub, spawning it when nothing is listening; then say hello. */
  private async connectOrStart(): Promise<void> {
    let sock = await this.tryConnect();
    if (!sock) {
      this.spawnHub();
      const deadline = Date.now() + this.startTimeoutMs;
      while (!sock && Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, 150));
        sock = await this.tryConnect();
      }
      if (!sock) {
        throw new Error(`Couldn't start the mcp-bizhawk hub on ${this.opts.host}:${this.opts.controlPort} ` +
          `within ${this.startTimeoutMs / 1000}s. Set MCP_BIZHAWK_HUB_LOG to a file path to see why, ` +
          `or run without the hub (BIZHAWK_HUB=0).`);
      }
    }
    this.attach(sock);
    const hello = await this.send("hello", { build: this.build }) as { build: string };
    this.stale = hello.build !== this.build;
  }

  /** Try one TCP connection to the control port. Resolves null when refused. */
  private tryConnect(): Promise<net.Socket | null> {
    return new Promise((resolve) => {
      const s = net.connect(this.opts.controlPort, this.opts.host);
      s.once("connect", () => { s.removeAllListeners("error"); resolve(s); });
      s.once("error", () => { s.destroy(); resolve(null); });
    });
  }

  /** Wire up a connected socket: parse replies, fail pending requests on close. */
  private attach(s: net.Socket): void {
    this.sock = s;
    this.buf = "";
    s.setEncoding("utf8");
    s.setNoDelay(true);
    s.on("data", (chunk: string) => {
      this.buf += chunk;
      let nl: number;
      while ((nl = this.buf.indexOf("\n")) >= 0) {
        const line = this.buf.slice(0, nl);
        this.buf = this.buf.slice(nl + 1);
        let msg: { id: number; ok: boolean; result?: unknown; error?: string };
        try { msg = JSON.parse(line); } catch { continue; }
        const p = this.pending.get(msg.id);
        if (!p) continue;
        this.pending.delete(msg.id);
        clearTimeout(p.timer);
        if (msg.ok) p.resolve(msg.result); else p.reject(new Error(msg.error ?? "hub error"));
      }
    });
    s.on("close", () => {
      if (this.sock === s) this.sock = null;
      for (const [id, p] of this.pending) {
        clearTimeout(p.timer);
        p.reject(new Error("lost the connection to the mcp-bizhawk hub; retry the call"));
        this.pending.delete(id);
      }
    });
    s.on("error", () => { /* handled by close */ });
  }

  /** Wait for the current socket to close (after shutdown_if_idle). */
  private waitClosed(): Promise<void> {
    const s = this.sock;
    if (!s) return Promise.resolve();
    return new Promise((r) => { s.once("close", () => r()); setTimeout(r, 3000); });
  }
}
