#!/usr/bin/env node
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { BizhawkServer } from "./bizhawk.js";
import { registerTools } from "./tools.js";
import { BizhawkLauncher } from "./launcher.js";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

// Report the real package version to MCP clients (dist/index.js → ../package.json).
const PACKAGE_VERSION: string = JSON.parse(
  readFileSync(fileURLToPath(new URL("../package.json", import.meta.url)), "utf8"),
).version;

const HOST = process.env.BIZHAWK_HOST ?? "127.0.0.1";
const PORT = parseInt(process.env.BIZHAWK_PORT ?? "8766", 10);

async function main() {
  const bh = new BizhawkServer({ host: HOST, port: PORT });

  // Start the TCP listener immediately. If another session's mcp-bizhawk holds
  // the port, stay up anyway: tools explain the situation and the listener
  // retries in the background, taking over when the port frees up.
  try {
    if (await bh.startOrWait() === "listening") {
      process.stderr.write(
        `[mcp-bizhawk] ${bh.describeTarget()}; waiting for BizHawk to connect.\n` +
        `             Start it with the bizhawk_launch tool, or run EmuHawk with\n` +
        `             --socket_ip=${HOST} --socket_port=${PORT} --lua=<this repo>/lua/bridge.lua\n`,
      );
    }
  } catch (err) {
    process.stderr.write(`[mcp-bizhawk] FATAL: could not bind ${HOST}:${PORT}: ${err}\n`);
    process.exit(1);
  }

  const server = new Server(
    { name: "mcp-bizhawk", version: PACKAGE_VERSION },
    { capabilities: { tools: {} } },
  );

  // bizhawk_eval_lua is opt-in at the config level too: off unless the user sets BIZHAWK_EVAL_LUA=1.
  registerTools(server, bh, new BizhawkLauncher(), { evalLua: process.env.BIZHAWK_EVAL_LUA === "1" });

  const transport = new StdioServerTransport();
  await server.connect(transport);
  process.stderr.write("[mcp-bizhawk] MCP server ready (stdio)\n");

  // Clean shutdown when the MCP client (Claude Code, Claude Desktop, etc.)
  // disconnects. Without this, the TCP listener for bridge.lua keeps the
  // event loop alive after stdin closes — leaving an orphan process bound
  // to the port. Every subsequent MCP-client restart would then fail with
  // EADDRINUSE until the orphan is manually killed.
  const shutdown = (reason: string) => {
    process.stderr.write(`[mcp-bizhawk] shutting down (${reason})\n`);
    bh.stop();
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
