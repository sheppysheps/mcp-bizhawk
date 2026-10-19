# mcp-bizhawk

[![npm version](https://img.shields.io/npm/v/mcp-bizhawk.svg)](https://www.npmjs.com/package/mcp-bizhawk)
[![npm downloads](https://img.shields.io/npm/dm/mcp-bizhawk.svg)](https://www.npmjs.com/package/mcp-bizhawk)
[![CI](https://github.com/dmang-dev/mcp-bizhawk/actions/workflows/ci.yml/badge.svg)](https://github.com/dmang-dev/mcp-bizhawk/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/npm/l/mcp-bizhawk.svg)](LICENSE)
[![Snyk](https://snyk.io/test/npm/mcp-bizhawk/badge.svg)](https://snyk.io/test/npm/mcp-bizhawk)
[![Socket](https://img.shields.io/badge/Socket-security-2F7BFF?logo=socket)](https://socket.dev/npm/package/mcp-bizhawk)
[![Bundlephobia](https://img.shields.io/badge/bundlephobia-size-FF6B81)](https://bundlephobia.com/package/mcp-bizhawk)
[![npmgraph](https://img.shields.io/badge/npmgraph-dependencies-2496ED)](https://npmgraph.js.org/?q=mcp-bizhawk)

An [MCP](https://modelcontextprotocol.io) server that exposes [BizHawk](https://github.com/TASEmulators/BizHawk) — the multi-system emulator the TAS community lives in — to any MCP-compatible client (Claude Desktop, Claude Code, etc.).

One bridge, **many systems**: NES, SNES, Game Boy / GBC / GBA, Sega Master System / Genesis / 32X / Saturn, N64, PlayStation 1, Atari 2600/5200/7800, Lynx, ColecoVision, Intellivision, and more — all through the same MCP tools, with per-system memory domains exposed cleanly.

<p align="center">
  <img src="docs/sm-claude-play.gif" alt="Claude driving Super Metroid through mcp-bizhawk">
</p>

<p align="center"><em>Claude (the agent) drives Samus through the opening of Ceres Station — all input batched through <code>bizhawk_play_input_sequence</code>, all motion verified by reading WRAM addresses found via live RAM-hunt. Recording is 2× speed; the actual playback runs at native 60fps emulation. See <a href="docs/RECIPES.md"><code>docs/RECIPES.md</code></a> and <a href="docs/SUPER-METROID-ADDRESSES.md"><code>docs/SUPER-METROID-ADDRESSES.md</code></a> for the workflow.</em></p>

## About this fork

This fork adds what coding agents need to research game internals autonomously: launch and close, logging breakpoints, and opt-in sandboxed Lua. See [docs/FORK-GOALS.md](docs/FORK-GOALS.md) for the goals and the "capabilities, not features" principle, and [docs/TESTING.md](docs/TESTING.md) for the testing strategy.

## How it works

```
+------------------+    stdio     +------------------+   TCP :8766   +------------------+
|   MCP client     |   JSON-RPC   |    mcp-bizhawk   |  newline JSON |     BizHawk      |
| (Claude / etc.)  | ===========> |     (Node.js)    | <============ |    bridge.lua    |
+------------------+              +------------------+               +------------------+
```

The transport is **inverted** compared to most other emulator-MCP bridges: BizHawk's Lua doesn't have native server sockets, only an outbound `comm.socketServer*` client. So `mcp-bizhawk` runs the TCP **listener**, and BizHawk's Lua bridge dials in once per frame to ferry commands and replies.

Two pieces:
- **`lua/bridge.lua`** — runs *inside* BizHawk's Lua Console, polls our TCP server once per frame
- **`dist/index.js`** — the Node.js MCP server, listens on `127.0.0.1:8766` by default, exposes tools over stdio

Trade-off: this design adds **~one frame of latency** per call (≈16ms at 60Hz). Fine for interactive memory hunting, save-state experimentation, and frame-by-frame inspection. Less ideal for high-rate-of-fire scripting.

## Requirements

- [BizHawk](https://github.com/TASEmulators/BizHawk/releases) **2.6.2 or newer** (earlier builds use an older socket-server wire format)
- **Node.js 22+** (for the MCP server)

Tested on BizHawk 2.11.1 across SNES (Super Metroid). Should work on any system BizHawk supports.

## Install

### Option A — install from npm (recommended)

```bash
npm install -g mcp-bizhawk
```

### Option B — `npx` (no install)

```bash
npx -y mcp-bizhawk
```

### Option C — clone and develop

```bash
git clone https://github.com/dmang-dev/mcp-bizhawk
cd mcp-bizhawk
npm install        # also runs the build via the `prepare` hook
```

## Set up the BizHawk bridge

There are two pieces to configure: telling BizHawk where to connect, and loading the bridge script.

### 1. Point BizHawk at the MCP server

BizHawk 2.11 only creates its socket when EmuHawk is **started with the socket flags**. Opening it normally (double-clicking `EmuHawk.exe` or a ROM) gives it no socket, and neither Lua nor any menu we found can add one later. If you load the bridge anyway, the Lua Console prints `STOPPED: BizHawk's socket isn't set up` along with these fixes.

Pick one:

- **Let the AI start it:** set `BIZHAWK_EXE` (see [Configuration](#configuration)), then the agent calls `bizhawk_launch`. The flags and the bridge script are handled for you.
- **A Windows shortcut you double-click:** right-click `EmuHawk.exe` → **Create shortcut**, open the shortcut's **Properties**, and set **Target** to:
  ```
  "C:\path\to\EmuHawk.exe" --socket_ip=127.0.0.1 --socket_port=8766 --lua="C:\path\to\mcp-bizhawk\lua\bridge.lua"
  ```
  Opening BizHawk through this shortcut also loads the bridge, so you can skip step 2. Add a ROM path at the end to boot straight into a game.
- **Command line:**
  ```bash
  EmuHawk.exe --socket_ip=127.0.0.1 --socket_port=8766 --lua=path/to/lua/bridge.lua <path/to/rom>
  ```

Use the port your server listens on (`BIZHAWK_PORT`, default `8766`).

### 2. Load the bridge script

Skip this if you used `--lua=` above. Otherwise, in BizHawk: **Tools → Lua Console → Open Script** → select `lua/bridge.lua` from this repo. Load only `bridge.lua`: it loads `json.lua` and `sandbox.lua` itself.

You should see in the Lua Console:
```
[mcp-bizhawk] bridge starting
[mcp-bizhawk] socket server target: 127.0.0.1:8766
[mcp-bizhawk] socket receive timeout set to 50ms
[mcp-bizhawk] frame loop active — bridge is polling once per frame
```

And in the `mcp-bizhawk` process's stderr:
```
[mcp-bizhawk] BizHawk client connected (waiting for bridge.lua to start polling)
[mcp-bizhawk] bridge.lua is polling — bridge ready
```

## Register with your MCP client

### Claude Code (CLI)

```bash
claude mcp add bizhawk --scope user mcp-bizhawk
```

Verify:
```bash
claude mcp list
# bizhawk: mcp-bizhawk - ✓ Connected
```

### Claude Desktop

Edit `claude_desktop_config.json`:

| Platform | Path |
|---|---|
| macOS    | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows  | `%APPDATA%\Claude\claude_desktop_config.json` |
| Linux    | `~/.config/Claude/claude_desktop_config.json` |

```json
{
  "mcpServers": {
    "bizhawk": {
      "command": "mcp-bizhawk"
    }
  }
}
```

Restart Claude Desktop after editing.

### Other MCP clients

The server speaks standard MCP over stdio. Run `mcp-bizhawk` and connect any MCP client to its stdio.

## Configuration

| Env var        | Default       | Purpose                              |
|----------------|---------------|--------------------------------------|
| `BIZHAWK_HOST` | `127.0.0.1`   | TCP host to listen on for BizHawk    |
| `BIZHAWK_PORT` | `8766`        | TCP port to listen on for BizHawk    |
| `BIZHAWK_EXE`  | *(unset)*     | Absolute path to `EmuHawk.exe` (or `EmuHawkMono.sh`). Only needed for `bizhawk_launch`; tool callers can't change it |
| `BIZHAWK_EVAL_LUA` | *(unset)* | Set to `1` to offer `bizhawk_eval_lua`. Unset, agents don't see the tool and the confirmation dialog can never appear |

## Tools

| Tool | Description |
|------|-------------|
| `bizhawk_launch` | Start BizHawk (from `BIZHAWK_EXE`) with a ROM and `lua/bridge.lua` preloaded; waits for the bridge |
| `bizhawk_close` | Close BizHawk via `client.exit()`; falls back to ending the process this server launched |
| `bizhawk_ping` | Verify bridge connectivity (returns `pong`) |
| `bizhawk_get_info` | ROM name, ROM hash, framecount, memory domains, capabilities |
| `bizhawk_list_memory_domains` | List available memory domains for the loaded core |
| `bizhawk_read8` / `bizhawk_read16` / `bizhawk_read32` | Read u8 / u16-LE / u32-LE from memory |
| `bizhawk_write8` / `bizhawk_write16` / `bizhawk_write32` | Write to memory |
| `bizhawk_read_range` | Read up to 4096 bytes as a byte array |
| `bizhawk_write_range` | Write up to 4096 bytes from a byte array |
| `bizhawk_press_buttons` | Set joypad state for one player; keys are button names, values booleans |
| `bizhawk_frame_advance` | Step the emulator by N frames |
| `bizhawk_pause` / `bizhawk_unpause` | Pause / resume emulation |
| `bizhawk_reset` | Reset the loaded core |
| `bizhawk_screenshot` | Save a PNG of the current display to a path |
| `bizhawk_save_state` / `bizhawk_load_state` | Save / load emulator state to a file path |
| `bizhawk_add_breakpoint` | Log every read, write or execution at an address (or a range up to 256): registers, stack words, extra memory reads; optionally pause |
| `bizhawk_list_breakpoints` / `bizhawk_remove_breakpoint` | List breakpoints with hit counts / remove one or all |
| `bizhawk_get_breakpoint_hits` | Fetch a breakpoint's logged hits (optionally clearing the buffer) |
| `bizhawk_get_registers` | Read the CPU registers (names are core-specific) |
| `bizhawk_eval_lua` | **Off by default; only listed when `BIZHAWK_EVAL_LUA=1`.** Run a short Lua script in a read-only sandbox (pointer walks, struct decoding, `gui.*` drawing). The first call opens a confirmation dialog in BizHawk; only the person at the computer can enable it (tick the box, then click Accept). See [lua/sandbox.lua](lua/sandbox.lua) for exactly what scripts can reach |

Breakpoints are *logging* breakpoints: BizHawk's Lua can't halt the CPU mid-instruction, so each hit is recorded (and `pause_on_hit` pauses at the end of that frame). Breakpoint addresses are on the CPU's system bus by default (pass `scope` for another), unlike the memory tools' per-domain offsets. Whether read and exec hooks fire, and whether the written value is reported, depends on the core: on Gambatte (GB/GBC) all three kinds fire, the value of a write is always reported as 0, and the registers include the mapped `ROMX BANK`/`WRAM BANK`.

All memory r/w tools take an optional `domain` parameter — if omitted, the active "current" memory domain is used. Use `bizhawk_list_memory_domains` to discover the names available on the loaded core.

See [`docs/RECIPES.md`](docs/RECIPES.md) for end-to-end examples (RAM hunting on SNES/NES/N64, frame-precise input, snapshot-experiment-restore, cross-system regression testing) and [`CHANGELOG.md`](CHANGELOG.md) for release history.

### Memory domains by system (cheat sheet)

Names come straight from BizHawk's core implementation. Use `bizhawk_list_memory_domains` to see the exact set for the loaded ROM.

| System  | Main RAM domain | Other common domains              |
|---------|-----------------|-----------------------------------|
| NES     | `RAM`           | `PPU`, `OAM`, `PRG ROM`, `CHR`    |
| SNES    | `WRAM`          | `VRAM`, `CARTROM`, `CARTRAM`      |
| GB/GBC  | `WRAM`          | `VRAM`, `HRAM`, `OAM`, `ROM`      |
| GBA     | `EWRAM`, `IWRAM`| `VRAM`, `PALRAM`, `OAM`, `ROM`    |
| Genesis | `68K RAM`       | `VRAM`, `Z80 RAM`, `CARTRAM`      |
| N64     | `RDRAM`         | `SP DMEM`, `SP IMEM`, `PI Reg`    |
| PSX     | `MainRAM`       | `VRAM`, `Scratchpad`, `BIOS`      |

### Buttons by system

BizHawk's `joypad.set` takes a `{ButtonName=true, ...}` table where button names depend on the core. Common ones:

| System  | Names                                                              |
|---------|---------------------------------------------------------------------|
| NES     | `A`, `B`, `Up`, `Down`, `Left`, `Right`, `Start`, `Select`         |
| SNES    | `A`, `B`, `X`, `Y`, `L`, `R`, `Up`, `Down`, `Left`, `Right`, `Start`, `Select` |
| GB/GBC  | `A`, `B`, `Up`, `Down`, `Left`, `Right`, `Start`, `Select`         |
| GBA     | `A`, `B`, `L`, `R`, `Up`, `Down`, `Left`, `Right`, `Start`, `Select` |
| N64     | `A`, `B`, `Z`, `L`, `R`, `Start`, `Up`, `Down`, `Left`, `Right`, `C-Up`, `C-Down`, `C-Left`, `C-Right` |
| Genesis | `A`, `B`, `C`, `X`, `Y`, `Z`, `Up`, `Down`, `Left`, `Right`, `Start`, `Mode` |

If you're unsure, run a probe: `bizhawk_press_buttons {"A": true}` and watch the active core's input display in BizHawk.

## Troubleshooting

| Symptom | Cause / Fix |
|---|---|
| MCP tool calls hang for 10 seconds, then time out with "is the bridge.lua script still polling?" | bridge.lua isn't loaded. In BizHawk: **Tools → Lua Console → Open Script → bridge.lua**. Check the console for `frame loop active`. |
| BizHawk connects to the server but tool calls still time out | You're on BizHawk older than 2.6.2 — the socket wire format changed then. Upgrade BizHawk. |
| `[mcp-bizhawk] FATAL: comm.socketServer* not available` in the Lua Console | BizHawk wasn't launched with `--socket_ip` / `--socket_port` flags, and no socket server is configured in **Settings → Customize → External Tools**. |
| Tools missing in Claude after install | Restart your MCP client; Claude only enumerates servers on startup. |
| Memory reads return zeros for the first few seconds after boot | The emulator hasn't initialized RAM yet. Either advance some frames (`bizhawk_frame_advance`) or check `bizhawk_get_info` to confirm framecount > 0 before relying on game state. |
| `unknown memory domain: <name>` | The domain name didn't match anything for the loaded core. Call `bizhawk_list_memory_domains` to see the actual list — names are case-sensitive. |
| `client.screenshot not available` or `savestate.* not available` | Some BizHawk cores expose a slightly different surface. Check `bizhawk_get_info` — the `capabilities` map shows which optional functions are present on your current build/core combo. |

## Development

```bash
npm install
npm run dev      # tsc --watch — autobuilds on src/ changes
```

Tests (see [docs/TESTING.md](docs/TESTING.md) for the full strategy):

```bash
npm test               # build, then Node unit tests (launcher, tools, TCP protocol); no BizHawk needed
npm run test:lua       # sandbox, consent gate and json.lua checks on stock Lua 5.4 (needs lua5.4)
npm run test:bizhawk   # the same Lua checks inside real BizHawk (needs BIZHAWK_EXE and TEST_ROM)
```

CI runs the first two on every push. Set `MCP_BIZHAWK_DEBUG=1` to log every line sent to and from the bridge.

## Debugging with the MCP Inspector

Browse and call this server's tools interactively with the [MCP Inspector](https://github.com/modelcontextprotocol/inspector):

```bash
npm run inspector
```

Build first if you've edited `src/` since your last `npm install` (`npm run build`, or keep `npm run dev` running). Override the listener with `BIZHAWK_HOST` / `BIZHAWK_PORT` (default `127.0.0.1:8766`). `tools/list` works even without BizHawk connected; *calling* a tool needs EmuHawk running with the socket flags and `lua/bridge.lua` loaded.

## License

[MIT](LICENSE)
