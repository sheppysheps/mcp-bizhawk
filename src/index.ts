#!/usr/bin/env node
// mcp-bizhawk entry point. Three ways to run:
//
//   node dist/index.js           MCP server over stdio (one per Claude session).
//                                By default it relays to the shared hub, starting
//                                it if needed, so sessions share one BizHawk.
//   BIZHAWK_HUB=0 node ...       Direct mode: this process owns BizHawk's port
//                                (the pre-hub behavior; one session at a time).
//   node dist/index.js --hub     The shared hub itself (started by sessions).
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { BizhawkServer } from "./bizhawk.js";
import { registerTools } from "./tools.js";
import { BizhawkLauncher } from "./launcher.js";
import { LocalBackend, type Backend } from "./backend.js";
import { RemoteBackend } from "./hub-client.js";
import { runHubProcess } from "./hub.js";
import { PACKAGE_VERSION } from "./version.js";

const HOST = process.env.BIZHAWK_HOST ?? "127.0.0.1";
const PORT = parseInt(process.env.BIZHAWK_PORT ?? "8766", 10);
const HUB_PORT = parseInt(process.env.BIZHAWK_HUB_PORT ?? String(PORT - 1), 10);
const DIRECT = process.env.BIZHAWK_HUB === "0";

/**
 * Direct mode: own BizHawk's port in this process.
 * @returns the backend and a stop function. Exits the process on a fatal bind error.
 * @requires node:net (via BizhawkServer).
 */
async function startDirect(): Promise<{ backend: Backend; stop: () => void }> {
  const bh = new BizhawkServer({ host: HOST, port: PORT });
  // If another process holds the port, stay up: tools explain the situation
  // and the listener retries in the background.
  try {
    if (await bh.startOrWait() === "listening") {
      process.stderr.write(
        `[mcp-bizhawk] direct mode: ${bh.describeTarget()}; waiting for BizHawk to connect.\n` +
        `             Start it with the bizhawk_launch tool, or run EmuHawk with\n` +
        `             --socket_ip=${HOST} --socket_port=${PORT} --lua=<this repo>/lua/bridge.lua\n`,
      );
    }
  } catch (err) {
    process.stderr.write(`[mcp-bizhawk] FATAL: could not bind ${HOST}:${PORT}: ${err}\n`);
    process.exit(1);
  }
  return { backend: new LocalBackend(bh, new BizhawkLauncher(), "direct"), stop: () => bh.stop() };
}

/**
 * Run the MCP server over stdio with the hub or direct backend.
 * @returns once connected; the process exits when the client disconnects.
 * @requires The MCP SDK; the hub (or BIZHAWK_HUB=0).
 */
async function main() {
  if (process.argv.includes("--hub")) return runHubProcess();

  let backend: Backend;
  let stop: () => void;
  if (DIRECT) {
    ({ backend, stop } = await startDirect());
  } else {
    // The hub starts lazily on the first tool call, so an idle session costs nothing.
    const remote = new RemoteBackend({ host: HOST, controlPort: HUB_PORT });
    backend = remote;
    stop = () => remote.disconnect();
    process.stderr.write(`[mcp-bizhawk] relaying to the shared hub on ${HOST}:${HUB_PORT} (BizHawk port ${PORT})\n`);
  }

  const server = new Server(
    { name: "mcp-bizhawk", version: PACKAGE_VERSION },
    { capabilities: { tools: {} } },
  );

  // bizhawk_eval_lua is opt-in at the config level too: off unless the user sets BIZHAWK_EVAL_LUA=1.
  registerTools(server, backend, { evalLua: process.env.BIZHAWK_EVAL_LUA === "1" });

  const transport = new StdioServerTransport();
  await server.connect(transport);
  process.stderr.write("[mcp-bizhawk] MCP server ready (stdio)\n");

  // Clean shutdown when the MCP client disconnects. In direct mode this frees
  // BizHawk's port (an orphan holding it would block the next session); in hub
  // mode it only drops this session's connection and the hub keeps running.
  const shutdown = (reason: string) => {
    process.stderr.write(`[mcp-bizhawk] shutting down (${reason})\n`);
    stop();
    process.exit(0);
  };
  process.stdin.on("end",   () => shutdown("stdin closed"));
  process.stdin.on("close", () => shutdown("stdin closed"));
  process.on("SIGINT",  () => shutdown("SIGINT"));
  process.on("SIGTERM", () => shutdown("SIGTERM"));
}

main().catch((err) => {
  process.stderr.write(`[mcp-bizhawk] fatal: ${err}\n`);
  process.exit(1);
});
