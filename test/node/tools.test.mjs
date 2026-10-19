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

test("every listed tool has a dispatch case (none hit 'Unknown tool')", async () => {
  const h = harness({ evalLua: true });
  for (const t of TOOLS) {
    try {
      await h.call(t.name, { address: 0, value: 0, values: [0], path: "x", rom: "x.gbc", code: "return 1",
                             buttons: {}, frames: [], kind: "write", id: 1, count: 1 });
    } catch (err) {
      assert.doesNotMatch(String(err), /Unknown tool/, `${t.name} has no dispatch case`);
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
