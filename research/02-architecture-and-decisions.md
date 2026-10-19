# Architecture and decisions

Decisions made with the maintainer, with the reasoning, so they aren't
relitigated later. Dates are 2026-10-18 / 19.

## Principles

- **Capabilities, not features.** The server ships general tools (memory,
  search, snapshots, breakpoints, input, dumps, sandboxed Lua). Game- or
  system-specific features (an ESP overlay, a GPU viewer) are built per task
  out of those tools, or packaged as **skills** in `skills/`. Skills should be
  core-agnostic where possible.
- **Big data stays inside BizHawk.** Snapshots, comparisons and searches run in
  the bridge; only counts and short lists cross into the agent's context.
  Large binary data goes to files (`bizhawk_dump_memory`), not through chat.
- **Stock BizHawk only.** No BizHawk fork: users would need a niche build.
  BizHawk's C# frontend is MIT and can be *referenced* (credited), but GB cores
  like Gambatte are GPL and the repo as a whole is self-described as a
  licensing "minefield".
- **Game research lives in its own project** (Shando for Shantae). This repo
  uses games only as test targets.
- **Least privilege** for anything that runs code or touches the system, with
  a comment justifying the access.

## Shared hub (feat/shared-hub, bd8bfc0)

BizHawk connects to exactly one port, and the maintainer keeps one Claude
session per repo. So:

```
session A (MCP stdio) ──┐
                        ├──► hub (127.0.0.1:8765) ──► port 8766 ◄── BizHawk + bridge.lua
session B (MCP stdio) ──┘
```

- Sessions are thin relays (`RemoteBackend`); the hub (`dist/index.js --hub`)
  owns BizHawk's port and the launcher (`LocalBackend`).
- First tool call starts the hub detached; it exits after 5 min with no
  sessions and no BizHawk. Switching repos or reloading VS Code keeps BizHawk.
- Build mismatch (after `npm run build`): replaced only when BizHawk isn't
  connected; otherwise ping/get_info add a note.
- **No auth token**, by the maintainer's choice: single user, local only,
  frequently audited. Mitigation: a connection must open with a JSON `hello`,
  so an HTTP request aimed at the port is dropped before anything runs.
- `BIZHAWK_HUB=0` = old direct mode.

## Arbitrary Lua (bizhawk_eval_lua)

Three switches, all off by default:
1. `BIZHAWK_EVAL_LUA=1` in the MCP config, or the tool isn't listed and calls are refused.
2. `lua/sandbox.lua` is loaded only on first use.
3. A BizHawk dialog with the maintainer's exact warning text; only **tick + Accept** is consent.
   Cancel / X / Disable withdraw it, every close starts a **2-minute reprompt
   cooldown** (anti-nagging), and the Asterisk sound plays on open.

Sandbox: text-only `load`, whitelist environment (memory reads, emu info,
`gui` drawing, screen sizes, gameinfo, bit, math/string/table/utf8), every
BizHawk function wrapped (no userdata reaches scripts), instruction budget,
memory ceiling, pcall can't swallow aborts, `string.rep` capped (also via
`("x"):rep`), no `__gc`, result formatting never runs script metamethods, bulk
reads capped at 64 KiB. Writes are deliberately absent.

## Files the server writes

- `bizhawk_screenshot` / `save_state`: caller-chosen paths (upstream behavior).
- `bizhawk_dump_memory`: **only inside `BIZHAWK_DUMP_DIR`** (user config),
  relative names, `.bin/.raw/.dat`, no escapes/absolute/UNC, no overwrite by
  default; JSON sidecar with SHA-256.
- `bizhawk_launch`: runs only `BIZHAWK_EXE` (must be EmuHawk), always with
  this package's bridge, ROM must be an existing file with a ROM extension.

## Git workflow (maintainer decision)

- No pull requests (two maintainers). `git pull` first; the daily security
  workflow pushes lockfile refreshes to `main`.
- Rule: `main` always builds and works. Commit straight to `main` when done
  in one sitting; branch when work spans sessions, is experimental, changes
  the Node↔Lua protocol, or touches security. Merge with `--no-ff`, run CI on
  the branch first (`gh workflow run ci.yml --ref <branch>`), delete after.
- Don't push `v*` tags: they publish to npm, and the name belongs to upstream.
- Git identity for this repo: sheppysheps + GitHub noreply email. GitHub via
  `gh` (keyring auth); the GitHub MCP plugin reads `GITHUB_PERSONAL_ACCESS_TOKEN`.

## Configuration used during development

User-level MCP servers only (no `.mcp.json`): `bizhawk` (this fork's
`dist/index.js`, `BIZHAWK_PORT=8766`, `BIZHAWK_EXE` pointing at a BizHawk copy
used only for testing, `BIZHAWK_DUMP_DIR` set to a dedicated dumps folder),
`rea` (6.7.0, with GHIDRA_INSTALL_DIR/JAVA_HOME), `ghidra` (GhidraMCP pinned
to commit 1e98025), GitHub via plugin.
