// mcp-bizhawk backend
// ───────────────────
// What the MCP tools talk to. Two implementations:
//
//   LocalBackend   owns BizHawk's TCP port and the launcher in THIS process.
//                  The shared hub (src/hub.ts) runs one; so does direct mode
//                  (BIZHAWK_HUB=0) and the tests.
//   RemoteBackend  (src/hub-client.ts) forwards everything to the shared hub,
//                  so several Claude sessions can use one BizHawk.

import { BizhawkServer } from "./bizhawk.js";
import { BizhawkLauncher } from "./launcher.js";

/** Snapshot of the backend's state, for bizhawk_hub_status. */
export interface BackendStatus {
  mode: "hub" | "direct";
  /** Owns BizHawk's port (false while another process holds it). */
  listening: boolean;
  /** A BizHawk socket is connected. */
  connected: boolean;
  /** bridge.lua is polling. */
  bridgeReady: boolean;
  bizhawkPort: number;
  /** PID of a BizHawk this backend launched and that is still running. */
  launchedPid?: number;
  /** Why the port isn't ours, when listening is false. */
  notListeningReason?: string;
  /** Hub only: process id, build, uptime and attached sessions. */
  hubPid?: number;
  hubBuild?: string;
  hubUptimeSeconds?: number;
  sessions?: number;
  /** Client only: the running hub is older than this session's build. */
  staleHub?: boolean;
  /** Client only: this session is older than the hub (rebuilt since it started). */
  outdatedSession?: boolean;
}

export interface Backend {
  /** Send one bridge command and wait for its result. */
  call<T = unknown>(method: string, params?: Record<string, unknown>): Promise<T>;
  /** Start BizHawk with the bridge and wait for it; returns the tool's text. */
  launch(rom: string, waitSeconds: number): Promise<string>;
  /** Close BizHawk; returns the tool's text. */
  close(): Promise<string>;
  /** Current state. */
  status(): Promise<BackendStatus>;
}

/**
 * Backend that owns BizHawk's port and launcher in this process.
 * Used by the hub, by direct mode, and by tests.
 */
export class LocalBackend implements Backend {
  constructor(
    readonly bh: BizhawkServer,
    readonly launcher: BizhawkLauncher,
    private readonly mode: "hub" | "direct" = "direct",
  ) {}

  /**
   * Forward one command to the bridge.
   * @returns the bridge's result; rejects as BizhawkServer.call does.
   * @requires A listening BizhawkServer with a connected bridge.
   */
  call<T = unknown>(method: string, params: Record<string, unknown> = {}): Promise<T> {
    return this.bh.call<T>(method, params);
  }

  /**
   * Start BizHawk and wait for the bridge to come up.
   * @returns Text with PID, ROM and connection status. Throws on validation or
   *          spawn failure, when another process owns the port, or if BizHawk
   *          is already connected.
   * @requires BizhawkLauncher (BIZHAWK_EXE configured); a listening BizhawkServer.
   */
  async launch(rom: string, waitSeconds: number): Promise<string> {
    if (!this.bh.isListening()) {
      // The port belongs to another process; a BizHawk started now would connect THERE.
      throw new Error(`Can't launch: ${this.bh.notListeningReason()}`);
    }
    if (this.bh.isConnected()) throw new Error("BizHawk is already connected. Use bizhawk_close first to restart it.");
    const r = await this.launcher.launch(rom, this.bh.target());
    const ready = await this.bh.waitForBridge(waitSeconds * 1000);
    return `Launched BizHawk (PID ${r.pid})\nROM: ${r.rom}\n` +
      (ready
        ? "Bridge connected — ready for tool calls."
        : `Bridge not connected after ${waitSeconds}s. BizHawk may still be starting; retry bizhawk_ping shortly.`);
  }

  /**
   * Close BizHawk: gracefully through the bridge, else by ending our own process.
   * @returns Text describing which path was taken. Throws if BizHawk is
   *          neither connected nor launched by this backend.
   * @requires The bridge (client.exit) or a process started by the launcher.
   */
  async close(): Promise<string> {
    const { bh, launcher } = this;
    if (bh.isBridgeReady()) {
      try {
        await bh.call("close");
        const gone = await bh.waitForDisconnect(10_000);
        await launcher.waitForExit(10_000);
        if (gone) return "BizHawk closed (client.exit).";
        if (!launcher.isRunning()) {
          return "Close sent, but BizHawk is still connected after 10s. It may be showing a dialog (e.g. save prompt).";
        }
      } catch (err) {
        if (!launcher.isRunning()) throw err;
      }
    }
    if (launcher.isRunning()) {
      const pid = launcher.pid();
      launcher.kill();
      const exited = await launcher.waitForExit(10_000);
      return exited
        ? `Bridge didn't respond; terminated the BizHawk process this server launched (PID ${pid}).`
        : `Sent terminate to PID ${pid}, but it hasn't exited after 10s.`;
    }
    throw new Error("BizHawk isn't connected and wasn't launched by this server, so there's nothing to close.");
  }

  /**
   * Describe the port, connection and launched process.
   * @returns BackendStatus (hub fields are added by the hub).
   * @requires Nothing.
   */
  async status(): Promise<BackendStatus> {
    const listening = this.bh.isListening();
    return {
      mode: this.mode,
      listening,
      connected: this.bh.isConnected(),
      bridgeReady: this.bh.isBridgeReady(),
      bizhawkPort: this.bh.target().port,
      launchedPid: this.launcher.pid(),
      notListeningReason: listening ? undefined : this.bh.notListeningReason(),
    };
  }
}
