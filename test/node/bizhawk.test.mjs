// Layer 1: the TCP protocol in src/bizhawk.ts, against a fake bridge on a random
// local port. No BizHawk needed.
import { test } from "node:test";
import assert from "node:assert/strict";
import net from "node:net";
import { BizhawkServer } from "../../dist/bizhawk.js";

/**
 * Find a free local TCP port.
 * @returns Promise of a port number that was free a moment ago.
 * @requires node:net.
 */
function freePort() {
  return new Promise((resolve) => {
    const s = net.createServer().listen(0, "127.0.0.1", () => { const { port } = s.address(); s.close(() => resolve(port)); });
  });
}

/**
 * Connect a fake bridge: collects framed lines the server sends.
 * @returns `{ sock, next(), send(line) }` where next() resolves with the next payload (prefix stripped).
 * @requires A BizhawkServer listening on `port`.
 */
async function fakeBridge(port) {
  const sock = net.connect(port, "127.0.0.1");
  await new Promise((r) => sock.once("connect", r));
  sock.setEncoding("utf8");
  const lines = []; const waiters = []; let buf = "";
  sock.on("data", (d) => {
    buf += d; let i;
    while ((i = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, i); buf = buf.slice(i + 1);
      const m = line.match(/^(\d+) (.*)$/);
      assert.ok(m, `server line must be length-prefixed: ${line}`);
      assert.equal(Buffer.byteLength(m[2]), Number(m[1]), "length prefix must equal payload bytes");
      (waiters.shift() ?? ((l) => lines.push(l)))(m[2]);
    }
  });
  return {
    sock,
    next: () => lines.length ? Promise.resolve(lines.shift()) : new Promise((r) => waiters.push(r)),
    send: (line) => sock.write(line + "\n"),
  };
}

/**
 * Start a server on a free port, run `fn`, always stop it.
 * @returns whatever `fn` returns.
 * @requires dist/bizhawk.js.
 */
async function withServer(opts, fn) {
  const port = await freePort();
  const srv = new BizhawkServer({ port, ...opts });
  await srv.start();
  try { return await fn(srv, port); } finally { srv.stop(); }
}

test("READY with nothing queued gets NONE", () => withServer({}, async (srv, port) => {
  const b = await fakeBridge(port);
  b.send("READY");
  assert.equal(await b.next(), "NONE");
  assert.equal(srv.isBridgeReady(), true);
  b.sock.destroy();
}));

test("a queued call is sent on READY and resolved by its RESULT", () => withServer({}, async (srv, port) => {
  const b = await fakeBridge(port);
  const pending = srv.call("ping", { x: 1 });
  b.send("READY");
  const cmd = JSON.parse(await b.next());
  assert.equal(cmd.method, "ping");
  assert.deepEqual(cmd.params, { x: 1 });
  b.send("RESULT " + JSON.stringify({ id: cmd.id, result: "pong" }));
  assert.equal(await pending, "pong");
  b.sock.destroy();
}));

test("BizHawk's own length prefix on incoming lines is accepted", () => withServer({}, async (srv, port) => {
  const b = await fakeBridge(port);
  b.send("5 READY");
  assert.equal(await b.next(), "NONE");
  b.sock.destroy();
}));

test("an error RESULT rejects the call with the bridge's message", () => withServer({}, async (srv, port) => {
  const b = await fakeBridge(port);
  const pending = srv.call("read8", { address: 0 });
  b.send("READY");
  const cmd = JSON.parse(await b.next());
  b.send("RESULT " + JSON.stringify({ id: cmd.id, error: { code: -32603, message: "boom" } }));
  await assert.rejects(pending, /BizHawk RPC error \[-32603\]: boom/);
  b.sock.destroy();
}));

test("a call nobody answers times out", () => withServer({ timeoutMs: 150 }, async (srv) => {
  await assert.rejects(srv.call("ping"), /timed out \(150ms\)/);
}));

test("disconnecting resets bridge readiness", () => withServer({}, async (srv, port) => {
  const b = await fakeBridge(port);
  b.send("READY");
  await b.next();
  assert.equal(srv.isBridgeReady(), true);
  b.sock.destroy();
  assert.equal(await srv.waitForDisconnect(2000), true);
  assert.equal(srv.isBridgeReady(), false);
}));

test("a second BizHawk client is refused", () => withServer({}, async (srv, port) => {
  const first = await fakeBridge(port);
  first.send("READY");
  await first.next();
  const second = net.connect(port, "127.0.0.1");
  const reply = await new Promise((r) => { let s = ""; second.setEncoding("utf8"); second.on("data", (d) => (s += d)); second.on("close", () => r(s)); });
  assert.match(reply, /another BizHawk client already connected/);
  first.sock.destroy();
}));
