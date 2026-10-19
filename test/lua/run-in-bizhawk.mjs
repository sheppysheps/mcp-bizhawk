// Run the Lua checks inside real BizHawk (NLua, Lua 5.4), the runtime we ship for.
// Usage: npm run test:bizhawk   (needs BIZHAWK_EXE and TEST_ROM; any ROM works)
// Exits 0 when every check passes, 1 on failures, 2 when not configured.
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync, readFileSync, existsSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const exe = process.env.BIZHAWK_EXE, rom = process.env.TEST_ROM;
if (!exe || !rom) {
  console.log("skipped: set BIZHAWK_EXE (path to EmuHawk.exe) and TEST_ROM (any ROM file) to run these.");
  process.exit(2);
}

// A tiny launcher script passes paths in as globals, then runs the real runner.
const work = mkdtempSync(path.join(tmpdir(), "mcp-bizhawk-luatest-"));
const out = path.join(work, "results.txt");
const lua = (s) => JSON.stringify(s.replaceAll("\\", "/"));  // a quoted Lua string literal
const launcherScript = path.join(work, "launch.lua");
writeFileSync(launcherScript,
  `MCP_BIZHAWK_TEST_ROOT = ${lua(root)}\nMCP_BIZHAWK_TEST_OUT = ${lua(out)}\n` +
  `dofile(${lua(path.join(root, "test/lua/bizhawk-runner.lua"))})\n`);

// No socket flags: these checks don't use the bridge, and BizHawk exits by itself.
const child = spawn(exe, [`--lua=${launcherScript}`, rom], { cwd: path.dirname(exe), stdio: "ignore" });
const timer = setTimeout(() => { console.error("timed out after 120 s"); child.kill(); }, 120_000);
child.on("exit", () => {
  clearTimeout(timer);
  if (!existsSync(out)) { console.error("no results: the runner crashed (open BizHawk's Lua Console to see why)"); process.exit(1); }
  const lines = readFileSync(out, "utf8").split("\n");
  rmSync(work, { recursive: true, force: true });
  const failed = lines.filter((l) => l.startsWith("FAIL"));
  for (const l of lines) console.log(l);
  console.log(`\n${lines.filter((l) => /^(ok|FAIL)/.test(l)).length} checks, ${failed.length} failed`);
  process.exit(failed.length ? 1 : 0);
});
