// mcp-bizhawk memory dumps
// ────────────────────────
// bizhawk_dump_memory writes raw memory to a file so external tools (tile
// decoders, disassemblers, diff tools) can read it without the bytes passing
// through the agent's context.
//
// WHY THIS FILE TOUCHES THE FILESYSTEM
// Writing files is system access, so it is fenced in:
//   • Files may only go inside BIZHAWK_DUMP_DIR, a directory the USER sets in
//     the MCP config. Tool callers choose a relative name inside it, never a
//     directory: absolute paths, drive letters and ".." escapes are refused.
//   • Only data extensions (.bin .raw .dat) are allowed, so a dump can't be
//     named like a script or program.
//   • Existing files are kept unless the caller passes overwrite=true.
// The bridge writes the bytes to the path validated here; this module also
// hashes the result and writes a small JSON sidecar describing it.

import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";

/** Extensions a dump may have. */
const DUMP_EXTS = new Set([".bin", ".raw", ".dat"]);

/** What the bridge reports about a dump it wrote. */
export interface DumpInfo {
  bytes: number;
  domain?: string;
  start: number;
  frame?: number;
  snapshot?: string;
}

/**
 * Read and check BIZHAWK_DUMP_DIR.
 * @returns The absolute dump directory. Throws a setup hint when it's unset or relative.
 * @requires process.env.BIZHAWK_DUMP_DIR.
 */
export function dumpDir(env: NodeJS.ProcessEnv = process.env): string {
  const dir = env.BIZHAWK_DUMP_DIR;
  if (!dir) {
    throw new Error("BIZHAWK_DUMP_DIR is not set. Add it to the bizhawk server's \"env\" in your MCP config, " +
      "e.g. \"BIZHAWK_DUMP_DIR\": \"C:/path/to/dumps\", then reload the MCP server.");
  }
  if (!path.isAbsolute(dir)) throw new Error(`BIZHAWK_DUMP_DIR must be an absolute path (got ${dir})`);
  return path.resolve(dir);
}

/**
 * Turn a caller-supplied relative name into a safe absolute path inside `dir`.
 * @returns The absolute file path (".bin" is added when there is no extension).
 *          Throws for absolute paths, drive letters, escapes out of `dir`,
 *          disallowed extensions, or an existing file without `overwrite`.
 * @requires node:path, node:fs (existence check only).
 */
export function resolveDumpPath(dir: string, file: string, overwrite = false): string {
  if (typeof file !== "string" || file.trim() === "") throw new Error("file required (a name like \"shantae/vram.bin\")");
  if (path.isAbsolute(file) || /^[a-zA-Z]:/.test(file) || file.startsWith("\\\\")) {
    throw new Error(`file must be a name relative to the dump folder, not an absolute path (got ${file})`);
  }
  let target = path.resolve(dir, file);
  const rel = path.relative(dir, target);
  if (rel === "" || rel.startsWith("..") || path.isAbsolute(rel)) {
    throw new Error(`file must stay inside the dump folder (${dir}); got ${file}`);
  }
  let ext = path.extname(target).toLowerCase();
  if (ext === "") { target += ".bin"; ext = ".bin"; }
  if (!DUMP_EXTS.has(ext)) throw new Error(`dump files must end in .bin, .raw or .dat (got ${ext})`);
  if (!overwrite && existsSync(target)) throw new Error(`${target} already exists; pass overwrite=true to replace it`);
  return target;
}

/**
 * Create the directories a dump path needs (inside the dump folder only).
 * @returns nothing.
 * @requires node:fs. `target` must come from resolveDumpPath.
 */
export function ensureDumpParent(target: string): void {
  mkdirSync(path.dirname(target), { recursive: true });
}

/**
 * Hash a written dump and write its JSON sidecar (`<file>.json`).
 * @returns `{ sha256, sidecar }`. Throws if the file is missing or its size
 *          doesn't match what the bridge reported.
 * @requires node:fs, node:crypto.
 */
export function finishDump(target: string, info: DumpInfo, extra: Record<string, unknown> = {}): { sha256: string; sidecar: string } {
  const data = readFileSync(target);
  if (data.length !== info.bytes) {
    throw new Error(`dump size mismatch: bridge wrote ${info.bytes} bytes but ${target} has ${data.length}`);
  }
  const sha256 = createHash("sha256").update(data).digest("hex");
  const sidecar = `${target}.json`;
  writeFileSync(sidecar, JSON.stringify({
    file: path.basename(target), bytes: info.bytes, sha256,
    domain: info.domain ?? null, start: info.start, frame: info.frame ?? null,
    snapshot: info.snapshot ?? null, ...extra, written_at: new Date().toISOString(),
  }, null, 2) + "\n");
  return { sha256, sidecar };
}
