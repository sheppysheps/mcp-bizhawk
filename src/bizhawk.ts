// mcp-bizhawk Node.js side
// ────────────────────────
// Hosts a TCP server that BizHawk's Lua bridge connects out to. Each frame
// the Lua script does ONE round-trip:
//
//   Lua → us:  "READY\n"             (no pending result)
//          OR  "RESULT <json>\n"      (carrying back the previous frame's result)
//   us → Lua:  "NONE\n"               (nothing pending)
//          OR  "<json command>\n"     (next queued command)
//
// MCP tool calls land via call(method, params) → we enqueue the command and
// resolve the returned promise once the matching RESULT comes back. Each
// command therefore takes at minimum ~one frame (~16ms at 60fps) of latency.

import net from "node:net";

interface PendingCmd {
  id:      number;
  method:  string;
  params:  Record<string, unknown>;
  resolve: (result: unknown) => void;
  reject:  (err: Error) => void;
}

export interface BizhawkOptions {
  /** TCP host to listen on. Default 127.0.0.1. */
  host?: string;
  /** TCP port the BizHawk Lua bridge will dial. Default 8766. */
  port?: number;
  /** Per-call timeout (ms). Default 10000. */
  timeoutMs?: number;
  /** How long a call waits for BizHawk to connect before failing fast (ms). Default 2000. */
  connectGraceMs?: number;
}

export class BizhawkServer {
  private server: net.Server | null = null;
  private client: net.Socket | null = null;
  private buf = "";
  private queue: PendingCmd[] = [];
  /** Commands sent to BizHawk awaiting a RESULT reply. Keyed by command id. */
  private inflight = new Map<number, PendingCmd>();
  /** True between sending a command and receiving its reply. Lua only handles
   *  one command per round-trip, so we wait for a RESULT before sending the
   *  next command. */
  private awaitingResult = false;
  private nextId = 1;
  private readonly host: string;
  private readonly port: number;
  private readonly timeoutMs: number;
  private readonly connectGraceMs: number;

  constructor(opts: BizhawkOptions = {}) {
    this.host      = opts.host      ?? "127.0.0.1";
    this.port      = opts.port      ?? 8766;
    this.timeoutMs = opts.timeoutMs ?? 10000;
    this.connectGraceMs = opts.connectGraceMs ?? 2000;
  }

  describeTarget(): string {
    return `tcp listening on ${this.host}:${this.port}`;
  }

  isConnected(): boolean {
    return this.client !== null && !this.client.destroyed;
  }

  /**
   * True once we've received at least one message (READY or RESULT) from the
   * bridge. A bare TCP connection isn't enough — BizHawk's CLI flag opens the
   * socket at process startup, but `bridge.lua` only starts polling after the
   * user loads it via Tools > Lua Console. Tools should wait for this before
   * issuing commands.
   */
  isBridgeReady(): boolean {
    return this.bridgeReady;
  }
  private bridgeReady = false;

  /**
   * Wait until bridge.lua is polling (see isBridgeReady()).
   * @returns Promise resolving true once the bridge is ready, or false if
   *          `timeoutMs` passes first. Never rejects.
   * @requires Nothing beyond this instance; polls every 250ms.
   */
  async waitForBridge(timeoutMs: number): Promise<boolean> {
    return this.waitFor(() => this.bridgeReady, timeoutMs);
  }

  /**
   * Wait until the BizHawk socket has closed (e.g. after bizhawk_close).
   * @returns Promise resolving true once disconnected, or false on timeout.
   *          Never rejects.
   * @requires Nothing beyond this instance; polls every 250ms.
   */
  async waitForDisconnect(timeoutMs: number): Promise<boolean> {
    return this.waitFor(() => !this.isConnected(), timeoutMs);
  }

  /**
   * Poll `check` every 250ms until it's true or `timeoutMs` elapses.
   * @returns Promise resolving to the final value of `check()`.
   * @requires Nothing.
   */
  private async waitFor(check: () => boolean, timeoutMs: number): Promise<boolean> {
    const deadline = Date.now() + timeoutMs;
    while (!check()) {
      if (Date.now() >= deadline) return false;
      await new Promise((r) => setTimeout(r, 250));
    }
    return true;
  }

  /**
   * Host and port BizHawk should dial, for building launch arguments.
   * @returns `{ host, port }` as configured at construction.
   * @requires Nothing.
   */
  target(): { host: string; port: number } {
    return { host: this.host, port: this.port };
  }

  /** Start listening. Resolves once the listen socket is bound. */
  async start(): Promise<void> {
    if (this.server) return;
    return new Promise<void>((resolve, reject) => {
      const srv = net.createServer((sock) => this.attachClient(sock));
      srv.once("error", (err) => reject(err));
      srv.listen(this.port, this.host, () => {
        this.server = srv;
        resolve();
      });
    });
  }

  /** Why the listener isn't bound (e.g. the port is taken), or null. */
  private busyReason: string | null = null;
  private retryTimer: NodeJS.Timeout | null = null;

  /**
   * Start listening, but treat "port already in use" as a waiting state
   * instead of a fatal error: keep retrying every `retryMs` and make tool calls
   * fail fast with an explanation until the port frees up. This happens when
   * another Claude session's mcp-bizhawk already holds the port.
   * @returns "listening" if bound now, or "busy" if the port is taken and
   *          retries were scheduled. Rejects for any other listen error.
   * @requires node:net.
   */
  async startOrWait(retryMs = 5000): Promise<"listening" | "busy"> {
    try {
      await this.start();
      return "listening";
    } catch (err) {
      if ((err as NodeJS.ErrnoException).code !== "EADDRINUSE") throw err;
      this.busyReason =
        `Port ${this.port} is already in use, most likely by another Claude session's mcp-bizhawk ` +
        `(only one can own BizHawk's connection at a time). Close the bizhawk server in that session ` +
        `(or the session), and this one takes over within ${Math.round(retryMs / 1000)} s; BizHawk then ` +
        `needs restarting (bizhawk_launch) so it connects here. Or give this session its own BizHawk ` +
        `with a different BIZHAWK_PORT.`;
      process.stderr.write(`[mcp-bizhawk] ${this.busyReason}\n`);
      this.retryTimer = setInterval(() => {
        this.start().then(() => {
          if (this.retryTimer) clearInterval(this.retryTimer);
          this.retryTimer = null;
          this.busyReason = null;
          process.stderr.write(`[mcp-bizhawk] port ${this.port} is free now; ${this.describeTarget()}\n`);
        }, () => { /* still busy; try again next tick */ });
      }, retryMs);
      this.retryTimer.unref();  // never keep the process alive just to retry
      return "busy";
    }
  }

  /**
   * Whether this server owns the BizHawk port right now.
   * @returns true once bound; false while the port is busy or before start.
   * @requires Nothing.
   */
  isListening(): boolean {
    return this.server !== null;
  }

  /**
   * Explain why this server can't talk to BizHawk because it doesn't own the port.
   * @returns The busy-port explanation, or a generic note if never started.
   * @requires Nothing.
   */
  notListeningReason(): string {
    return this.busyReason ?? `mcp-bizhawk isn't listening on ${this.host}:${this.port}.`;
  }

  stop(): void {
    if (this.retryTimer) clearInterval(this.retryTimer);
    this.retryTimer = null;
    this.client?.destroy();
    this.client = null;
    this.server?.close();
    this.server = null;
  }

  private attachClient(sock: net.Socket): void {
    if (this.client && !this.client.destroyed) {
      // Already have a client. Refuse the new one — only one BizHawk at a time.
      sock.write("ERROR another BizHawk client already connected\n");
      sock.destroy();
      return;
    }
    process.stderr.write("[mcp-bizhawk] BizHawk client connected (waiting for bridge.lua to start polling)\n");
    this.client = sock;
    this.buf = "";
    this.awaitingResult = false;
    this.bridgeReady = false;

    // CRITICAL: disable Nagle. Bridge's socketServerResponse has a 5ms timeout;
    // our 5-byte NONE replies get coalesced by Nagle for up to 200ms otherwise,
    // missing every receive window. With Nagle off, writes flush immediately.
    sock.setNoDelay(true);

    sock.setEncoding("utf8");
    sock.on("data", (chunk: string) => this.onData(chunk));
    sock.on("close", () => {
      process.stderr.write("[mcp-bizhawk] BizHawk client disconnected\n");
      this.client = null;
      this.bridgeReady = false;
      // Leave inflight commands hanging — they'll time out cleanly.
    });
    sock.on("error", (err) => {
      process.stderr.write(`[mcp-bizhawk] socket error: ${err.message}\n`);
    });
  }

  private onData(chunk: string): void {
    if (process.env.MCP_BIZHAWK_DEBUG) {
      process.stderr.write(`[trace] RX raw (${chunk.length}B): ${JSON.stringify(chunk)}\n`);
    }
    this.buf += chunk;
    while (true) {
      const nl = this.buf.indexOf("\n");
      if (nl === -1) break;
      let line = this.buf.slice(0, nl).trim();
      this.buf = this.buf.slice(nl + 1);
      if (line.length === 0) continue;

      // BizHawk's SocketServer.SendString prepends every outgoing message
      // with "<byte-count> " as a framing header. Strip it.
      const m = line.match(/^(\d+) (.+)$/);
      if (m) line = m[2];

      if (process.env.MCP_BIZHAWK_DEBUG) {
        process.stderr.write(`[trace] RX line: ${JSON.stringify(line)}\n`);
      }
      this.handleMessage(line);
    }
  }

  private handleMessage(line: string): void {
    if (!this.bridgeReady) {
      this.bridgeReady = true;
      process.stderr.write("[mcp-bizhawk] bridge.lua is polling — bridge ready\n");
    }
    if (line === "READY") {
      // Lua is asking for work; no result to deliver this round.
      this.maybeDispatchNext();
      return;
    }
    if (line.startsWith("RESULT ")) {
      const json = line.slice(7);
      let parsed: { id?: number; result?: unknown; error?: { code: number; message: string } };
      try {
        parsed = JSON.parse(json);
      } catch (err) {
        process.stderr.write(`[mcp-bizhawk] bad RESULT json: ${(err as Error).message}\n`);
        this.awaitingResult = false;
        this.maybeDispatchNext();
        return;
      }
      const id = parsed.id ?? -1;
      const pending = this.inflight.get(id);
      if (pending) {
        this.inflight.delete(id);
        if (parsed.error) {
          pending.reject(new Error(`BizHawk RPC error [${parsed.error.code}]: ${parsed.error.message}`));
        } else {
          pending.resolve(parsed.result);
        }
      }
      this.awaitingResult = false;
      this.maybeDispatchNext();
      return;
    }
    process.stderr.write(`[mcp-bizhawk] unrecognised line from BizHawk: ${line}\n`);
    this.maybeDispatchNext();
  }

  /**
   * Write a single message to the bridge. BizHawk's socket server, since
   * 2.6.2, requires INCOMING messages to be length-prefixed the same way it
   * frames its OUTGOING ones: `"{length:D} {message}"`. Without the prefix,
   * BizHawk's parser silently discards the line and `socketServerResponse()`
   * returns empty — which is exactly the failure mode we hit before finding
   * this in `Lua/_docs_luacats/comm.d.lua`.
   */
  private sendFramed(payload: string): void {
    if (!this.client || this.client.destroyed) return;
    // length = byte count of the payload itself (not including the framing
    // prefix or the trailing newline that delimits the line on the wire).
    const byteLen = Buffer.byteLength(payload, "utf8");
    const wire = `${byteLen} ${payload}\n`;
    if (process.env.MCP_BIZHAWK_DEBUG) {
      process.stderr.write(`[trace] TX: ${JSON.stringify(wire)}\n`);
    }
    this.client.write(wire);
  }

  /**
   * Called when Lua sends READY. We MUST reply to every send because
   * BizHawk's Lua bridge polls `socketServerResponse()` once per frame and
   * expects something there — and we want to keep the round-trip turning
   * even when there's no work. Reply with the next queued command if
   * available, otherwise NONE.
   */
  private maybeDispatchNext(): void {
    if (!this.client || this.client.destroyed) return;
    if (this.awaitingResult) {
      // a command is in flight; tell bridge to keep waiting
      this.sendFramed("NONE");
      return;
    }
    const next = this.queue.shift();
    if (!next) {
      this.sendFramed("NONE");
      return;
    }
    this.inflight.set(next.id, next);
    this.awaitingResult = true;
    const msg = JSON.stringify({ id: next.id, method: next.method, params: next.params });
    this.sendFramed(msg);
  }

  /**
   * Enqueue a command and wait for BizHawk's reply.
   * @returns Promise of the bridge's result. Rejects at once (instead of after
   *          the full timeout) when this server doesn't own the port, or when
   *          no BizHawk has connected within `connectGraceMs`; rejects on an
   *          RPC error or timeout otherwise.
   * @requires A listening server and a connected bridge.lua.
   */
  async call<T = unknown>(method: string, params: Record<string, unknown> = {}): Promise<T> {
    if (!this.server) throw new Error(this.notListeningReason());
    if (!this.isConnected() && !(await this.waitFor(() => this.isConnected(), this.connectGraceMs))) {
      throw new Error(
        `BizHawk isn't connected to ${this.host}:${this.port}. Start it with bizhawk_launch, or open EmuHawk ` +
        `with --socket_ip=${this.host} --socket_port=${this.port} and lua/bridge.lua loaded.`);
    }
    return new Promise<T>((resolve, reject) => {
      const id = this.nextId++;
      const pending: PendingCmd = {
        id,
        method,
        params,
        resolve: (r) => resolve(r as T),
        reject,
      };

      const timer = setTimeout(() => {
        // Drop from queue if still waiting; from inflight if already sent.
        this.queue   = this.queue.filter((p) => p.id !== id);
        this.inflight.delete(id);
        if (this.inflight.size === 0) this.awaitingResult = false;
        reject(new Error(
          `BizHawk call "${method}" timed out (${this.timeoutMs}ms) — ` +
          `is the bridge.lua script still polling?`,
        ));
      }, this.timeoutMs);

      // Wrap so the timer always clears
      const origResolve = pending.resolve, origReject = pending.reject;
      pending.resolve = (r) => { clearTimeout(timer); origResolve(r); };
      pending.reject  = (e) => { clearTimeout(timer); origReject(e); };

      this.queue.push(pending);
    });
  }
}
