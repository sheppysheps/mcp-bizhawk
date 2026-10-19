# BizHawk 2.11.1 runtime facts

Verified on BizHawk 2.11.1 (`2.11.1+bdddf4a5`), Windows, Gambatte core, with
throwaway probe scripts run in separate EmuHawk instances. "How verified" says
what was run.

## Lua runtime

| Fact | How verified |
|---|---|
| Lua version is **5.4** (NLua). | `_VERSION` in a probe |
| `load(code, name, "t", env)` works and **rejects bytecode** ("attempt to load a binary chunk (mode is 't')"). | probe loading `string.dump` output |
| `debug.sethook` on a coroutine works, and an error raised in the hook aborts the coroutine. | probe stopping `while true do end` |
| **`luanet` is a global table** (NLua's .NET bridge). `import` and `CLRPackage` are nil. | probe `type(luanet)` |
| BizHawk API functions are **`luaNet_function` userdata** with a metatable; indexing them (`memory.read_u8.GetType`) errors. | probe |
| `luanet.import_type("System.Media.SystemSounds")` returns **nil until `luanet.load_assembly("System")`**; after that `SystemSounds.Asterisk:Play()` works. | probe |
| Lua scripts loaded with `--lua=` run with `io` and `os` available on the host side. | probes wrote result files with `io.open` |
| BizHawk ships LuaCATS API docs in `Lua/_docs_luacats/*.d.lua`, the most reliable reference for 2.11 signatures. | read directly |

## Sockets and startup

| Fact | How verified |
|---|---|
| The `comm` socket client **only exists when EmuHawk starts with `--socket_ip` and `--socket_port`**. Opened from the GUI, `socketServerGetIp()` returns nil and `socketServerIsConnected`, `SetIp` and `SetPort` throw NullReferenceException. No menu setting creates it. | probe without flags |
| `comm.httpPost` without `--url_post` returns nil in ~1 ms and sends nothing. | probe with a local HTTP listener |
| Incoming socket messages must be length-prefixed: `"<len> <msg>"`. | upstream code + docs |
| `client.exit()` closes EmuHawk cleanly. Called after the bridge sends its reply, the caller gets the reply first. | `bizhawk_close` end-to-end tests |
| A first `CloseMainWindow` while paused can stop the Lua script instead of closing; a second close exits. | observed in the first smoke test |

## Memory domains (Gambatte, GBC)

| Fact | How verified |
|---|---|
| Domains: `WRAM`, `ROM`, `VRAM`, `OAM`, `HRAM`, `System Bus`, `CartRAM`, `CPU registers`. | `bizhawk_get_info` |
| `VRAM` is **16384 bytes: both GBC banks** (bank 0 then bank 1). `OAM` is 160 bytes. | probe |
| `memory.read_bytes_as_binary_string(addr, len, domain)` returns a compact Lua string (1 byte per byte); 4 MiB ROM in ~90 ms. | dump tool timing |
| **I/O registers can be READ through `System Bus`** ($FF40-$FF4B gave real LCDC/SCY/SCX/LY/BGP/WY/WX values). | dump + decoding matched the screenshot |
| **I/O WRITES through `System Bus` are ignored**: writing $FF68 (BCPS) left it at `C0`; $FF69 reads returned `FF` x64; OBJ palette reads `00` x64. So **CGB palette RAM can't be read from Lua**. | probe on a colorful title screen |
| There's no GB-specific Lua library (unlike `nes`, `snes`, `genesis`, `nds`). The GPU Viewer's palettes come from the core's C# `LockGPU()` API, which Lua can't reach. | docs listing; GBGPUView.cs |
| `client.openramsearch`, `openhexeditor` etc. only OPEN the GUI tools; RAM Search, Hex Editor and the Code/Data Logger can't be driven from Lua. | docs |
| `event.on_bus_exec_any` exists (fires on every executed instruction); there's no read/write "any" variant. | docs |
| `emu.setregister` exists. | docs |

## Input

| Fact | How verified |
|---|---|
| Gambatte's buttons are named **without a player prefix**: `A B Down Left Power Right Select Start Up`. | `joypad.get()` keys |
| **`joypad.set(buttons, 1)` silently drops all input** on that core (it looks for `P1 Start`). `joypad.set(buttons)` works. Upstream's press/play tools never pressed anything on GB/GBC. Fixed in `80c25a1`. | probe + controlled test |
| Savestates make runs deterministic: two idle runs from one state gave byte-identical WRAM. | snapshot compare, 0 differences |

## Timing and behavior

- One bridge command per frame (~16 ms at 60 fps). `frame_advance` over ~600 frames exceeds the 10 s call timeout.
- `emu.frameadvance()` blocks while paused; the bridge uses `emu.yield()` then (fixed upstream-of-fork in `fix/pause`).
- Registers dumped at the end of a frame (LY = 144, vblank) may not be what was used while the screen was drawn: Shantae's title screen had LCDC = `0xE5` (sprites off) at frame end with 34 sprites visible.
- Gambatte memory callbacks: read/write/exec all fire; **the written value is reported as 0** (take the value from register A). Registers include `ROMX BANK`, `WRAM BANK`, `SRAM BANK`, `VRAM BANK`. (From the Shando session's notes.)
- `client.screenshot()` of a GBC game is exactly **160x144**, RGB, in BizHawk's displayed colors (after any GBC color correction).
