// Package version and build identity, shared by the hub and its clients.

import { readFileSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";

/** Version from package.json (dist/version.js → ../package.json). */
export const PACKAGE_VERSION: string = JSON.parse(
  readFileSync(fileURLToPath(new URL("../package.json", import.meta.url)), "utf8"),
).version;

/**
 * Identify this exact build, so a session can tell when the running hub was
 * started from an older build (e.g. after `npm run build`).
 * @returns "<version>+<mtime of dist/index.js in ms>".
 * @requires node:fs; dist/index.js next to this file.
 */
export function buildId(): string {
  const entry = fileURLToPath(new URL("./index.js", import.meta.url));
  let stamp = "unknown";
  try { stamp = String(Math.round(statSync(entry).mtimeMs)); } catch { /* keep "unknown" */ }
  return `${PACKAGE_VERSION}+${stamp}`;
}
