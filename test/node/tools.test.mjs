// Layer 1: tool definitions, dispatch and formatting (src/tools.ts). Uses a fake
// MCP server and a fake bridge, so no network and no BizHawk.
import { test } from "node:test";
import assert from "node:assert/strict";
import { CallToolRequestSchema, ListToolsRequestSchema } from "@modelcontextprotocol/sdk/types.js";
import { TOOLS, registerTools, describeEval } from "../../dist/tools.js";

/**
 * Register the tools against stand-ins and return the two request handlers.
 * @returns `{ list(), call(name, args), calls }`; `calls` records every bridge method sent.
 * @requires dist/tools.js (run `npm run build` first).
 */
function harness(opts = { evalLua: false }) {
  const handlers = new Map();
  const server = { setRequestHandler: (schema, fn) => handlers.set(schema, fn) };
  const calls = [];
  const bh = {
    call: async (method, params) => { calls.push({ method, params }); return {}; },
    isConnected: () => false,
    isListening: () => true,
    notListeningReason: () => "busy",
    isBridgeReady: () => true,
    waitForBridge: async () => true,
    waitForDisconnect: async () => true,
    target: () => ({ host: "127.0.0.1", port: 1 }),
  };
  const launcher = {
    launch: async () => ({ pid: 1, exe: "x", rom: "y" }),
    isRunning: () => false, pid: () => undefined, kill: () => false, waitForExit: async () => true,
  };
  registerTools(server, bh, launcher, opts);
  return {
    list: async () => (await handlers.get(ListToolsRequestSchema)({})).tools,
    call: (name, args = {}) => handlers.get(CallToolRequestSchema)({ params: { name, arguments: args } }),
    calls,
  };
}

test("tool names are unique and prefixed", () => {
  const names = TOOLS.map((t) => t.name);
  assert.equal(new Set(names).size, names.length);
  for (const n of names) assert.match(n, /^bizhawk_[a-z0-9_]+$/);
});

test("every description has PURPOSE / USAGE / BEHAVIOR / RETURNS", () => {
  for (const t of TOOLS) {
    for (const part of ["PURPOSE:", "USAGE:", "BEHAVIOR:", "RETURNS:"]) {
      assert.ok(t.description.includes(part), `${t.name} is missing ${part}`);
    }
  }
});

/**
 * Build the smallest arguments that satisfy a JSON schema (required fields only).
 * @returns a value valid for `schema`, so tests reach the dispatcher past validation.
 * @requires Nothing.
 */
function minimalValid(schema) {
  if (schema.enum) return schema.enum[0];
  switch (schema.type) {
    case "integer": case "number": return schema.minimum ?? 0;
    case "string": return "x".repeat(Math.max(1, schema.minLength ?? 1));
    case "boolean": return false;
    case "array": return Array.from({ length: schema.minItems ?? 0 }, () => minimalValid(schema.items ?? {}));
    default: {
      const out = {};
      for (const k of schema.required ?? []) out[k] = minimalValid(schema.properties[k]);
      return out;
    }
  }
}

test("every listed tool has a dispatch case (none hit 'Unknown tool')", async () => {
  const h = harness({ evalLua: true });
  for (const t of TOOLS) {
    const args = minimalValid(t.inputSchema);
    try {
      await h.call(t.name, args);
    } catch (err) {
      assert.doesNotMatch(String(err), /Unknown tool/, `${t.name} has no dispatch case`);
      assert.doesNotMatch(String(err), /Invalid arguments/, `${t.name}: generated args ${JSON.stringify(args)} failed validation`);
    }
  }
});

test("an unknown tool name is rejected", async () => {
  await assert.rejects(harness().call("bizhawk_nope"), /Unknown tool/);
});

test("bizhawk_eval_lua is hidden and refused unless evalLua is on", async () => {
  const off = harness({ evalLua: false });
  assert.ok(!(await off.list()).some((t) => t.name === "bizhawk_eval_lua"));
  await assert.rejects(off.call("bizhawk_eval_lua", { code: "return 1" }), /turned off in this server's config/);
  assert.equal(off.calls.length, 0, "nothing may reach the bridge while eval is off");

  const on = harness({ evalLua: true });
  assert.ok((await on.list()).some((t) => t.name === "bizhawk_eval_lua"));
  await on.call("bizhawk_eval_lua", { code: "return 1" });
  assert.deepEqual(on.calls.map((c) => c.method), ["eval_lua"]);
});

test("the off switch hides only eval: every other tool stays listed", async () => {
  const names = (await harness({ evalLua: false }).list()).map((t) => t.name);
  assert.equal(names.length, TOOLS.length - 1);
});

test("describeEval formats arrays, nil holes and empty results", () => {
  assert.match(describeEval({ values: [2], count: 1, output: "", instructions: 0 }), /return\[1\]: 2/);
  const holes = describeEval({ values: { "2": "b" }, count: 2, output: "hi", instructions: 1000 });
  assert.match(holes, /return\[1\]: null/);
  assert.match(holes, /return\[2\]: "b"/);
  assert.match(holes, /--- output ---\nhi/);
  assert.match(describeEval({ values: [], count: 0, output: "", instructions: 0 }), /\(no return values\)/);
});

test("describeCompare lists matches and the saved set", async () => {
  const { describeCompare } = await import("../../dist/tools.js");
  const out = describeCompare({ before: "a", after: "b", op: "decreased", width: "u8" },
    { count: 3, checked: 100, results: [{ address: 0xC123, before: 5, after: 4 }], truncated: true, domain: "WRAM", saved_as: "hp", saved_count: 3 });
  assert.match(out, /^3 of 100 address\(es\) in WRAM decreased \(u8\), 'a' → 'b'\. Saved 3 as candidate set 'hp'\. Listing the first 1\./);
  assert.match(out, /0xC123: 5 → 4/);
});

test("describeCompare and describeSnapshotList accept json.lua's {} for empty lists", async () => {
  const { describeCompare, describeSnapshotList } = await import("../../dist/tools.js");
  assert.match(describeCompare({ before: "a", after: "b", op: "changed" }, { count: 0, checked: 8, results: {}, truncated: false }), /^0 of 8/);
  const list = describeSnapshotList({ snapshots: {}, candidates: {}, used_bytes: 0, max_bytes: 67108864 });
  assert.match(list, /No snapshots\./);
  assert.match(list, /No candidate sets\./);
  assert.match(list, /0 KiB of 64\.0 MiB/);
});

test("malformed arguments are rejected before reaching the bridge", async () => {
  const h = harness();
  await assert.rejects(h.call("bizhawk_play_input_sequence", { frames: [{ Start: true }] }),
    /Invalid arguments for bizhawk_play_input_sequence: .*must NOT have additional properties/);
  await assert.rejects(h.call("bizhawk_read8", {}), /Invalid arguments for bizhawk_read8: .*must have required property 'address'/);
  await assert.rejects(h.call("bizhawk_compare_snapshots", { before: "a", after: "b", op: "bigger" }), /must be equal to one of the allowed values/);
  await assert.rejects(h.call("bizhawk_read8", { address: -1 }), /must be >= 0/);
  assert.equal(h.calls.length, 0, "nothing may reach the bridge");
  await h.call("bizhawk_play_input_sequence", { frames: [{ buttons: { Start: true } }] });
  assert.deepEqual(h.calls.map((c) => c.method), ["play_input_sequence"]);
});

test("every tool's inputSchema compiles", async () => {
  const { compileArgValidators } = await import("../../dist/tools.js");
  assert.equal(compileArgValidators(TOOLS).size, TOOLS.length);
});

test("launch refuses while another session owns the port", async () => {
  const handlers = new Map();
  const server = { setRequestHandler: (schema, fn) => handlers.set(schema, fn) };
  const bh = { isListening: () => false, notListeningReason: () => "Port 8766 is already in use", isConnected: () => false };
  let launched = false;
  registerTools(server, bh, { launch: async () => { launched = true; } }, { evalLua: false });
  await assert.rejects(handlers.get(CallToolRequestSchema)({ params: { name: "bizhawk_launch", arguments: { rom: "x.gbc" } } }),
    /Can't launch: Port 8766 is already in use/);
  assert.equal(launched, false);
});
