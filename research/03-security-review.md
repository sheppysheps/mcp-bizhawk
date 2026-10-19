# Security review notes

## Threat model

The agent driving the tools may be **prompt-injected** (by game text, web
pages, files it reads). So anything an agent can trigger must not be able to
run arbitrary code on the PC, read/write arbitrary files, or grant itself
consent. A session with shell access could edit files directly; the
consent dialog defends against tricked tool calls, not against that. Keep
`bizhawk_eval_lua` on Claude Code's ask-every-time permission list as a
second layer.

## Sandbox escape suite (test/lua/sandbox_spec.lua)

72 checks on stock Lua 5.4 in CI, plus 4 NLua-only checks inside BizHawk
(76). Covers: absence of `os io luanet require load loadfile dofile debug
getmetatable coroutine forms comm package _G collectgarbage`, memory writes,
`gui.drawImage`, `emu.frameadvance`, `client.exit`, `string.dump`; infinite
loops; `pcall`/`xpcall` swallowing aborts; `string.rep` and method-call rep;
`__gc`; memory ceiling; 64 KiB bulk-read cap; planted `__tostring` /
`__pairs` / `__name` traps; control-byte escaping; host isolation (globals,
`math`, `string.rep` restored). Plus 23 consent-gate state-machine checks with
stand-in `forms` (tick ≠ consent, X ≠ consent, cooldown, etc.).

## /security-review of feat/lua-eval

No high/medium vulnerabilities. Checked:
- Every whitelisted BizHawk function returns plain values or tables per the
  2.11 type docs; the ones returning .NET objects (`client.getconfig`,
  `createinstance`, `gettool`, `gui.createcanvas`) are not whitelisted.
- Shared string metatable reaches the host `string` library; only
  `string.dump` matters and nothing can load bytecode.
- Gate can't be bypassed from the sandbox (no `forms`) or from Node.
Hardened anyway (DoS-class): host-side formatting ran script metamethods
outside the budget; bulk reads could allocate past the ceiling (`6f6fb13`).

## Other boundaries

- **Launcher** (`src/launcher.ts`): only `BIZHAWK_EXE` (must be named
  EmuHawk.exe/EmuHawkMono.sh), always our `bridge.lua`, ROM extension
  whitelist (blocks passing a `.lua` as the "ROM"), no shell, kill only its own child.
- **Dumps** (`src/dumps.ts`): fenced to `BIZHAWK_DUMP_DIR`; tested against
  `..`, absolute, drive-relative (`C:x`), UNC, odd extensions, overwrite.
- **Argument validation**: every tool's inputSchema enforced with the SDK's
  Ajv validator (`48b39e8`); malformed input used to reach the bridge silently.
- **Ports**: bridge 8766 and hub 8765 listen on 127.0.0.1 only, no token.
  Any local process could connect, as with BizHawk's own socket.

## Third-party audits

| Tool | Verdict |
|---|---|
| **GhidraMCP** (LaurieWired, Apache-2.0), audited at `1e98025` | Plugin HTTP server binds loopback only, rejects `Origin` and foreign `Host` (DNS-rebinding/CSRF safe). No script execution, process spawning, file writes or outbound network in plugin or bridge. 29 endpoints: read + annotate. No auth: any local process can use it while Ghidra runs. Pinned in the MCP config. |
| **rea-agents 6.7.0** (morluto, MIT) | `npm pack` inspected: no install scripts. Setup edits `~/.claude.json` and `~/.claude/skills` with backups. The old 6.5.0 entry failed due to a broken npx cache. |
| **uv 0.13.1** installer | cargo-dist script; downloads from releases.astral.sh / GitHub, installs to `~/.local/bin`, adds to user PATH. Binary Authenticode-signed by "OpenAI OpCo, LLC" (valid). |
| **markdown-pdf** (AGPL-3.0) | Pulls `plantuml` + `requests` (only contact a server if a document has PlantUML diagrams). |

## npm audit (fix(deps) 430168a)

Advisories in `@modelcontextprotocol/sdk` (OAuth client), `hono`,
`ip-address`, `qs` were unreachable over stdio (HTTP transports never
loaded); bumped anyway. Minor bumps need human review; the CI lockfile gate
only auto-commits patch moves.

## Reference material policy

TCRF content is strictly local reference (gitignored, never redistributed).
Pan Docs (CC0) is credited in the README; its local copy is gitignored.
