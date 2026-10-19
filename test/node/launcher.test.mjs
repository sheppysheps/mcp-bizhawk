// Layer 1: the launcher's input checks (src/launcher.ts). No BizHawk needed.
import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync, mkdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { resolveExe, validateRom, BizhawkLauncher } from "../../dist/launcher.js";

let dir;
const savedExe = process.env.BIZHAWK_EXE;

/**
 * Make a scratch folder with stand-in files for the checks below.
 * @returns nothing; sets `dir`.
 * @requires node:fs, node:os.
 */
before(() => {
  dir = mkdtempSync(path.join(tmpdir(), "mcp-bizhawk-test-"));
  for (const f of ["game.gbc", "game.GBA", "bridge.lua", "save.State", "noext", "EmuHawk.exe", "cmd.exe"]) {
    writeFileSync(path.join(dir, f), "");
  }
  mkdirSync(path.join(dir, "folder.gbc"));
});

after(() => {
  rmSync(dir, { recursive: true, force: true });
  if (savedExe === undefined) delete process.env.BIZHAWK_EXE; else process.env.BIZHAWK_EXE = savedExe;
});

test("validateRom accepts known ROM extensions, case-insensitively", () => {
  assert.equal(validateRom(path.join(dir, "game.gbc")), path.join(dir, "game.gbc"));
  assert.equal(validateRom(path.join(dir, "game.GBA")), path.join(dir, "game.GBA"));
});

test("validateRom rejects scripts, savestates and extensionless files", () => {
  assert.throws(() => validateRom(path.join(dir, "bridge.lua")), /not a recognised ROM extension/);
  assert.throws(() => validateRom(path.join(dir, "save.State")), /not a recognised ROM extension/);
  assert.throws(() => validateRom(path.join(dir, "noext")), /not a recognised ROM extension/);
});

test("validateRom rejects missing files, directories and empty input", () => {
  assert.throws(() => validateRom(path.join(dir, "missing.gbc")), /ROM not found/);
  assert.throws(() => validateRom(path.join(dir, "folder.gbc")), /ROM not found or not a file/);
  assert.throws(() => validateRom(""), /rom path required/);
  assert.throws(() => validateRom("   "), /rom path required/);
});

test("resolveExe requires BIZHAWK_EXE to be set", () => {
  delete process.env.BIZHAWK_EXE;
  assert.throws(() => resolveExe(), /BIZHAWK_EXE is not set/);
});

test("resolveExe rejects relative paths and non-EmuHawk executables", () => {
  process.env.BIZHAWK_EXE = "EmuHawk.exe";
  assert.throws(() => resolveExe(), /must be an absolute path/);
  process.env.BIZHAWK_EXE = path.join(dir, "cmd.exe");
  assert.throws(() => resolveExe(), /must point at EmuHawk\.exe/);
  process.env.BIZHAWK_EXE = path.join(dir, "Missing", "EmuHawk.exe");
  assert.throws(() => resolveExe(), /does not exist/);
});

test("resolveExe accepts an existing EmuHawk.exe", () => {
  process.env.BIZHAWK_EXE = path.join(dir, "EmuHawk.exe");
  assert.equal(resolveExe(), path.join(dir, "EmuHawk.exe"));
});

test("kill() and waitForExit() are no-ops when nothing was launched", async () => {
  const l = new BizhawkLauncher();
  assert.equal(l.isRunning(), false);
  assert.equal(l.pid(), undefined);
  assert.equal(l.kill(), false);
  assert.equal(await l.waitForExit(10), true);
});
