// Layer 1: the shared hub (src/hub.ts) and its client (src/hub-client.ts).
// Runs a hub in-process on random local ports with a fake bridge, so no
// BizHawk and no background processes are involved.
import { test } from "node:test";
import assert from "node:assert/strict";
import net from "node:net";
import { startHub } from "../../dist/hub.js";
import { RemoteBackend } from "../../dist/hub-client.js";

/**
 * Find a free local TCP port.
 * @returns Promise of a port that was free a moment ago.
 * @requires node:net.
 */
function freePort() {
  return new Promise((resolve) => {
    const s = net.createServer().listen(0, "127.0.0.1", () => { const { port } = s.address(); s.close(() => resolve(port)); });
  });
}

/**
 * Connect a fake bridge.lua to BizHawk's port: answers every command with
 * `answer(cmd)` and keeps polling with READY.
 * @returns the socket (destroy it to "close BizHawk").
 * @requires A listening hub.
 */
async function fakeBizhawk(port, answer = (cmd) => (cmd.method === "ping" ? "pong" : { echo: cmd.method })) {
  const sock = net.connect(port, "127.0.0.1");
  await new Promise((r) => sock.once("connect", r));
  sock.setEncoding("utf8");
  let buf = "";
  sock.on("data", (d) => {
    buf += d; let i;
    while ((i = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, i).replace(/^\d+ /, ""); buf = buf.slice(i + 1);
      if (line === "NONE") setTimeout(() => !sock.destroyed && sock.write("READY\n"), 5);
      else {
        const cmd = JSON.parse(line);
        const out = answer(cmd);
        const reply = out && out.__error ? { id: cmd.id, error: out.__error } : { id: cmd.id, result: out };
        sock.write("RESULT " + JSON.stringify(reply) + "\n");
      }
    }
  });
  sock.write("READY\n");
  return sock;
}

/**
 * Start an in-process hub on fresh ports, run `fn`, always stop it.
 * @returns whatever `fn` returns.
 * @requires dist/hub.js.
 */
async function withHub(opts, fn) {
  const bizhawkPort = await freePort(), controlPort = await freePort();
  let stopped = false;
  const hub = await startHub({ host: "127.0.0.1", bizhawkPort, controlPort, idleMs: 60_000, build: "B1",
                               onStop: () => { stopped = true; }, ...opts });
  try { return await fn({ hub, bizhawkPort, controlPort, isStopped: () => stopped }); } finally { hub.stop(); }
}

const client = (controlPort, extra = {}) =>
  new RemoteBackend({ host: "127.0.0.1", controlPort, build: "B1", startTimeoutMs: 2000,
                      spawnHub: () => { throw new Error("test should not spawn a hub"); }, ...extra });

test("two sessions drive the same BizHawk through one hub", () => withHub({}, async ({ hub, bizhawkPort, controlPort }) => {
  const bridge = await fakeBizhawk(bizhawkPort);
  const a = client(controlPort), b = client(controlPort);
  assert.equal(await a.call("ping"), "pong");
  assert.deepEqual(await b.call("read8", { address: 1 }), { echo: "read8" });
  assert.equal(hub.sessions(), 2);
  a.disconnect();
  assert.equal(await b.call("ping"), "pong", "the other session keeps working when one leaves");
  const st = await b.status();
  assert.equal(st.mode, "hub");
  assert.equal(st.connected, true);
  assert.equal(st.hubBuild, "B1");
  b.disconnect(); bridge.destroy();
}));

test("bridge errors come back as errors for the calling session", () => withHub({}, async ({ bizhawkPort, controlPort }) => {
  const bridge = await fakeBizhawk(bizhawkPort, () => ({ __error: { code: -1, message: "boom" } }));
  const a = client(controlPort);
  await assert.rejects(a.call("read8", { address: 0 }), /BizHawk RPC error \[-1\]: boom/);
  a.disconnect(); bridge.destroy();
}));

test("a connection whose first line isn't a hello is dropped (e.g. an HTTP request)", () => withHub({}, async ({ controlPort }) => {
  const s = net.connect(controlPort, "127.0.0.1");
  await new Promise((r) => s.once("connect", r));
  const closed = new Promise((r) => s.once("close", r));
  let got = "";
  s.on("data", (d) => (got += d));
  s.write('POST / HTTP/1.1\r\nHost: evil.example\r\n\r\n{"id":1,"op":"call","method":"write8","params":{"address":0,"value":1}}\n');
  await closed;
  assert.equal(got, "", "nothing may be answered or executed");
}));

// Build ids look like buildId(): "<version>+<mtime ms>".
const OLD = "0.2.0+1000", NEW = "0.2.0+2000";

test("a session with a newer build replaces an idle hub, but not one with BizHawk attached", async () => {
  // Idle hub (no BizHawk): the newer session asks it to exit, then starts a fresh one.
  await withHub({ build: OLD }, async ({ controlPort, isStopped }) => {
    let spawned = 0;
    const fresh = [];
    const c = client(controlPort, { build: NEW, spawnHub: () => {
      spawned++;
      startHub({ host: "127.0.0.1", bizhawkPort: 0, controlPort, idleMs: 60_000, build: NEW }).then((h) => fresh.push(h));
    } });
    await c.status();  // first contact: OLD hub, stale → shutdown_if_idle on the next request
    const st = await c.status();
    assert.equal(isStopped(), true, "the old idle hub should have stopped");
    assert.equal(spawned, 1);
    assert.equal(st.hubBuild, NEW);
    assert.equal(st.staleHub, false);
    c.disconnect(); fresh.forEach((h) => h.stop());
  });
  // Hub with BizHawk attached: keep it, and flag the session as stale.
  await withHub({ build: OLD }, async ({ bizhawkPort, controlPort, isStopped }) => {
    const bridge = await fakeBizhawk(bizhawkPort);
    const c = client(controlPort, { build: NEW });
    assert.equal(await c.call("ping"), "pong");
    assert.equal(await c.call("ping"), "pong");
    assert.equal(isStopped(), false, "must not cut BizHawk off");
    assert.match(c.notice(), /older mcp-bizhawk build/);
    c.disconnect(); bridge.destroy();
  });
});

test("a session OLDER than the hub never replaces it (no restart loop)", () =>
  // Regression: a session started before `npm run build` saw the newer hub as
  // "different", shut it down, respawned the same newer build, and repeated
  // that on every call, cutting BizHawk off while it was connecting.
  withHub({ build: NEW }, async ({ controlPort, isStopped }) => {
    let spawned = 0;
    const c = client(controlPort, { build: OLD, spawnHub: () => { spawned++; } });
    const st = await c.status();
    await c.status();
    await c.status();
    assert.equal(isStopped(), false, "the newer hub must keep running");
    assert.equal(spawned, 0, "no hub may be spawned");
    assert.equal(st.staleHub, false);
    assert.equal(st.outdatedSession, true);
    assert.match(c.notice(), /restart the session/);
    c.disconnect();
  }));

test("compareBuilds orders by build stamp and refuses to guess", async () => {
  const { compareBuilds } = await import("../../dist/version.js");
  assert.ok(compareBuilds(OLD, NEW) < 0);
  assert.ok(compareBuilds(NEW, OLD) > 0);
  assert.equal(compareBuilds(NEW, NEW), 0);
  assert.equal(compareBuilds("0.2.0+unknown", NEW), undefined, "unknown order: never replace");
});

test("the hub stops itself after idleMs with no sessions and no BizHawk", () =>
  withHub({ idleMs: 200 }, async ({ controlPort, isStopped }) => {
    const c = client(controlPort);
    await c.status();
    c.disconnect();
    const deadline = Date.now() + 3000;
    while (!isStopped() && Date.now() < deadline) await new Promise((r) => setTimeout(r, 50));
    assert.equal(isStopped(), true);
  }));

test("the hub stays up while BizHawk is connected, even with no sessions", () =>
  withHub({ idleMs: 150 }, async ({ bizhawkPort, isStopped }) => {
    const bridge = await fakeBizhawk(bizhawkPort);
    await new Promise((r) => setTimeout(r, 600));
    assert.equal(isStopped(), false);
    bridge.destroy();
  }));

test("a second hub on the same control port fails with EADDRINUSE", () => withHub({}, async ({ controlPort }) => {
  await assert.rejects(startHub({ host: "127.0.0.1", bizhawkPort: 0, controlPort, idleMs: 1000 }), { code: "EADDRINUSE" });
}));

test("the client starts a hub when none is running", async () => {
  const controlPort = await freePort();
  let hub;
  const c = new RemoteBackend({ host: "127.0.0.1", controlPort, build: "B1", startTimeoutMs: 3000,
    spawnHub: () => { startHub({ host: "127.0.0.1", bizhawkPort: 0, controlPort, idleMs: 60_000, build: "B1" }).then((h) => { hub = h; }); } });
  try {
    const st = await c.status();
    assert.equal(st.mode, "hub");
    assert.equal(st.sessions, 1);
  } finally { c.disconnect(); hub?.stop(); }
});

test("hub-side launch refuses when BizHawk isn't configured, without starting anything", () =>
  withHub({}, async ({ controlPort }) => {
    const saved = process.env.BIZHAWK_EXE;
    delete process.env.BIZHAWK_EXE;
    try {
      await assert.rejects(client(controlPort).launch("x.gbc", 1), /BIZHAWK_EXE is not set/);
    } finally { if (saved !== undefined) process.env.BIZHAWK_EXE = saved; }
  }));
