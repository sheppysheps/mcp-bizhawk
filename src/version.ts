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

/**
 * Order two build ids from buildId() by their build time stamp.
 * Used so a session only replaces a hub that is older than itself: a session
 * started before the last `npm run build` must leave the newer hub alone.
 * @returns negative if `a` is older than `b`, positive if newer, 0 if equal;
 *          undefined when either stamp isn't a number (e.g. "unknown"), so
 *          callers can treat an unknown order as "don't replace".
 * @requires Nothing.
 */
export function compareBuilds(a: string, b: string): number | undefined {
  if (a === b) return 0;
  const stamp = (id: string) => Number(id.slice(id.lastIndexOf("+") + 1));
  const sa = stamp(a), sb = stamp(b);
  if (!Number.isFinite(sa) || !Number.isFinite(sb) || !a.includes("+") || !b.includes("+")) return undefined;
  return sa - sb;
}
