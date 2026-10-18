// mcp-bizhawk launcher
// ────────────────────
// Starts and (as a fallback) stops the BizHawk emulator process.
//
// WHY THIS FILE NEEDS SYSTEM ACCESS
// This is the only module in the server that starts an OS process
// (node:child_process). bizhawk_launch exists so an agent can bring the
// emulator up without a human double-clicking EmuHawk.exe. Starting a process
// is high-level system access, so this file keeps it as narrow as possible:
//
//   • The executable comes ONLY from the BIZHAWK_EXE environment variable,
//     which the human sets in their MCP config. Tool callers can't choose it,
//     and its filename must be EmuHawk.exe / EmuHawkMono.sh, so a misconfigured
//     path can't turn this into "run any program".
//   • The Lua script passed with --lua is ALWAYS this package's
//     lua/bridge.lua. Tool callers can't load their own scripts this way.
//   • The only caller-supplied input is a ROM path. It must be an existing
//     regular file with a known ROM extension, which also keeps BizHawk from
//     being handed a .lua script, movie, or savestate as its "ROM".
//   • spawn() runs without a shell and with an argument array, so nothing in
//     the ROM path can be interpreted as a command or extra flag.
//   • kill() only ever targets the process this server launched itself.

import { spawn, type ChildProcess } from "node:child_process";
import { statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

/** Executable names accepted for BIZHAWK_EXE (Windows build / Linux Mono script). */
const EXE_NAME = /^EmuHawk(\.exe|Mono\.sh)$/i;

/** ROM extensions BizHawk can boot. Archives are allowed; BizHawk picks the ROM inside. */
const ROM_EXTS = new Set([
  ".nes", ".fds", ".unf", ".sfc", ".smc", ".gb", ".gbc", ".sgb", ".gba",
  ".nds", ".n64", ".z64", ".v64", ".gen", ".md", ".smd", ".32x", ".sms",
  ".gg", ".sg", ".pce", ".sgx", ".a26", ".a78", ".lnx", ".ws", ".wsc",
  ".vb", ".ngp", ".ngc", ".col", ".int", ".cue", ".ccd", ".iso", ".chd",
  ".m3u", ".bin", ".zip", ".7z",
]);

/** Absolute path to the bridge script shipped with this package (dist/../lua). */
const BRIDGE_LUA = fileURLToPath(new URL("../lua/bridge.lua", import.meta.url));

export interface LaunchTarget {
  host: string;
  port: number;
}

export class BizhawkLauncher {
  private child: ChildProcess | null = null;
  private exited: Promise<number | null> | null = null;

  /**
   * Whether a BizHawk process launched by this server is still running.
   * @returns true between a successful launch() and that process exiting.
   * @requires Nothing.
   */
  isRunning(): boolean {
    return this.child !== null && this.child.exitCode === null && this.child.signalCode === null;
  }

  /**
   * PID of the BizHawk process this server launched.
   * @returns The PID, or undefined if nothing is running.
   * @requires Nothing.
   */
  pid(): number | undefined {
    return this.isRunning() ? this.child?.pid : undefined;
  }

  /**
   * Start EmuHawk with the bridge script and the given ROM.
   * @returns `{ pid, exe, rom }` once the process has spawned. Throws if
   *          BIZHAWK_EXE is unset or invalid, the ROM fails validation, a
   *          launched BizHawk is already running, or the OS can't start it.
   * @requires BIZHAWK_EXE env var (absolute path to EmuHawk); node:child_process.
   */
  async launch(romPath: string, target: LaunchTarget): Promise<{ pid: number; exe: string; rom: string }> {
    if (this.isRunning()) {
      throw new Error(`BizHawk is already running (PID ${this.child!.pid}). Use bizhawk_close first.`);
    }
    const exe = resolveExe();
    const rom = validateRom(romPath);

    const args = [
      `--socket_ip=${target.host}`,
      `--socket_port=${target.port}`,
      `--lua=${BRIDGE_LUA}`,
      rom,
    ];

    // shell: false (the default) — args go straight to the OS, never via cmd/sh.
    // cwd is BizHawk's own folder so it finds its config.ini, dll/ and firmware.
    const child = spawn(exe, args, {
      cwd:         path.dirname(exe),
      stdio:       "ignore",
      detached:    true,
      windowsHide: false,
    });

    const pid = await new Promise<number>((resolve, reject) => {
      child.once("spawn", () => resolve(child.pid!));
      child.once("error", (err) => reject(new Error(`could not start BizHawk: ${err.message}`)));
    });

    // Let the MCP server exit without taking BizHawk down with it.
    child.unref();
    this.child  = child;
    this.exited = new Promise((resolve) => child.once("exit", (code) => resolve(code)));
    return { pid, exe, rom };
  }

  /**
   * Wait for the launched process to exit.
   * @returns true if it exited (or none was running) within `timeoutMs`, else false.
   * @requires Nothing.
   */
  async waitForExit(timeoutMs: number): Promise<boolean> {
    if (!this.isRunning() || !this.exited) return true;
    const timeout = new Promise<false>((r) => setTimeout(() => r(false), timeoutMs));
    return Promise.race([this.exited.then(() => true), timeout]);
  }

  /**
   * Forcefully end the BizHawk process this server launched. A fallback for
   * when the bridge isn't answering, so a graceful client.exit() can't be sent.
   * @returns true if a process was signalled, false if none was running.
   * @requires The process must have been started by launch(); never touches others.
   */
  kill(): boolean {
    if (!this.isRunning()) return false;
    return this.child!.kill();
  }
}

/**
 * Read and validate BIZHAWK_EXE.
 * @returns The absolute path to EmuHawk. Throws a setup hint if it's unset,
 *          relative, missing, or not named EmuHawk.exe / EmuHawkMono.sh.
 * @requires process.env.BIZHAWK_EXE; node:fs.
 */
function resolveExe(): string {
  const exe = process.env.BIZHAWK_EXE;
  if (!exe) {
    throw new Error(
      "BIZHAWK_EXE is not set. Add it to the bizhawk server's \"env\" in your MCP config, " +
      "e.g. \"BIZHAWK_EXE\": \"C:/BizHawk/EmuHawk.exe\", then reload the MCP server.",
    );
  }
  if (!path.isAbsolute(exe)) throw new Error(`BIZHAWK_EXE must be an absolute path (got ${exe})`);
  if (!EXE_NAME.test(path.basename(exe))) {
    throw new Error(`BIZHAWK_EXE must point at EmuHawk.exe or EmuHawkMono.sh (got ${path.basename(exe)})`);
  }
  if (!isFile(exe)) throw new Error(`BIZHAWK_EXE does not exist or is not a file: ${exe}`);
  return exe;
}

/**
 * Validate a caller-supplied ROM path.
 * @returns The absolute ROM path. Throws if it's empty, missing, not a regular
 *          file, or doesn't have a known ROM extension.
 * @requires node:fs. Relative paths resolve against the server's working directory.
 */
function validateRom(romPath: string): string {
  if (typeof romPath !== "string" || romPath.trim() === "") throw new Error("rom path required");
  const rom = path.resolve(romPath);
  const ext = path.extname(rom).toLowerCase();
  if (!ROM_EXTS.has(ext)) {
    throw new Error(`not a recognised ROM extension: "${ext || "(none)"}" — expected one of ${[...ROM_EXTS].join(" ")}`);
  }
  if (!isFile(rom)) throw new Error(`ROM not found or not a file: ${rom}`);
  return rom;
}

/**
 * Check that a path is an existing regular file.
 * @returns true for a regular file; false if missing, a directory, or unreadable.
 * @requires node:fs.
 */
function isFile(p: string): boolean {
  try { return statSync(p).isFile(); } catch { return false; }
}
