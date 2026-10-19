// Layer 1: bizhawk_dump_memory's file fencing (src/dumps.ts). No BizHawk needed.
import { test, before, after } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync, readFileSync, rmSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { dumpDir, resolveDumpPath, ensureDumpParent, finishDump } from "../../dist/dumps.js";

let dir;
before(() => { dir = mkdtempSync(path.join(tmpdir(), "mcp-bizhawk-dumps-")); });
after(() => rmSync(dir, { recursive: true, force: true }));

test("dumpDir requires an absolute BIZHAWK_DUMP_DIR", () => {
  assert.throws(() => dumpDir({}), /BIZHAWK_DUMP_DIR is not set/);
  assert.throws(() => dumpDir({ BIZHAWK_DUMP_DIR: "dumps" }), /must be an absolute path/);
  assert.equal(dumpDir({ BIZHAWK_DUMP_DIR: dir }), path.resolve(dir));
});

test("names resolve inside the folder, with .bin added when there's no extension", () => {
  assert.equal(resolveDumpPath(dir, "vram"), path.join(dir, "vram.bin"));
  assert.equal(resolveDumpPath(dir, "shantae/f1500_oam.raw"), path.join(dir, "shantae", "f1500_oam.raw"));
  assert.equal(resolveDumpPath(dir, "a/../b.dat"), path.join(dir, "b.dat"), "harmless .. that stays inside is fine");
});

test("escapes out of the folder are refused", () => {
  assert.throws(() => resolveDumpPath(dir, "../outside.bin"), /must stay inside the dump folder/);
  assert.throws(() => resolveDumpPath(dir, "a/../../outside.bin"), /must stay inside the dump folder/);
  // Refused on every platform: on Windows it escapes; on Linux/macOS ("\" isn't a
  // separator there) it's one file whose name starts with "..", refused anyway.
  assert.throws(() => resolveDumpPath(dir, "..\\..\\Windows\\x.bin"), /must stay inside the dump folder/);
  assert.throws(() => resolveDumpPath(dir, path.join(dir, "abs.bin")), /not an absolute path/);
  assert.throws(() => resolveDumpPath(dir, "C:x.bin"), /not an absolute path/);
  assert.throws(() => resolveDumpPath(dir, "\\\\server\\share\\x.bin"), /not an absolute path/);
  assert.throws(() => resolveDumpPath(dir, "."), /must stay inside the dump folder/);
});

test("only data extensions are allowed", () => {
  for (const bad of ["x.exe", "x.lua", "x.bat", "x.ps1", "x.json", "x.BIN.exe"]) {
    assert.throws(() => resolveDumpPath(dir, bad), /must end in \.bin, \.raw or \.dat/, bad);
  }
  assert.equal(resolveDumpPath(dir, "X.BIN"), path.join(dir, "X.BIN"), "extension check ignores case");
});

test("existing files are kept unless overwrite is set", () => {
  writeFileSync(path.join(dir, "exists.bin"), "x");
  assert.throws(() => resolveDumpPath(dir, "exists.bin"), /already exists; pass overwrite=true/);
  assert.equal(resolveDumpPath(dir, "exists.bin", true), path.join(dir, "exists.bin"));
});

test("ensureDumpParent creates subfolders; finishDump hashes and writes the sidecar", () => {
  const target = resolveDumpPath(dir, "game/sub/vram.bin");
  ensureDumpParent(target);
  writeFileSync(target, Buffer.from([1, 2, 3, 4]));
  const r = finishDump(target, { bytes: 4, domain: "VRAM", start: 0x100, frame: 42 }, { note: "title screen" });
  assert.equal(r.sha256, "9f64a747e1b97f131fabb6b447296c9b6f0201e79fb3c5356e6c77e89b6a806a");
  const meta = JSON.parse(readFileSync(r.sidecar, "utf8"));
  assert.deepEqual([meta.file, meta.bytes, meta.domain, meta.start, meta.frame, meta.note], ["vram.bin", 4, "VRAM", 256, 42, "title screen"]);
  assert.ok(existsSync(target + ".json"));
});

test("finishDump refuses a size mismatch", () => {
  const target = resolveDumpPath(dir, "short.bin");
  writeFileSync(target, Buffer.from([1, 2]));
  assert.throws(() => finishDump(target, { bytes: 4, start: 0 }), /dump size mismatch/);
});
