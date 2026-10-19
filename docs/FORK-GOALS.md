# Fork goals: autonomous game research

This fork (`sheppysheps/mcp-bizhawk`) extends [dmang-dev/mcp-bizhawk](https://github.com/dmang-dev/mcp-bizhawk). The goal is to let coding agents **research a game's internals on their own**. Agents run repeated controlled tests, manipulate savestates, set breakpoints and write bespoke Lua, and use the results to catalog how a game stores and changes objects and values in its ROM and RAM.

Other reverse-engineering tools are expected to run alongside it, such as Ghidra (through GhidraMCP) and REA, but they aren't required.

## Principles

- **Capabilities, not features.** This repo ships general capabilities. It does not ship anything game-specific or task-specific. An agent builds a pointer walker, an "ESP-style" overlay or a dialog dumper **when a task asks for one**, out of those capabilities. When a task can't be done, the fix is a missing capability, not a built-in feature.
- **Least privilege.** Anything that runs code or touches the system follows the rules in [lua/sandbox.lua](../lua/sandbox.lua) and [src/launcher.ts](../src/launcher.ts).
- **Big data stays inside BizHawk.** Searching 32 KB of RAM by sending it through an agent's context costs time and tokens. The bridge should keep snapshots and do comparisons in BizHawk, then return counts and short candidate lists.
- **Stock BizHawk only.** Everything must work on an unmodified BizHawk release. A fork would tie every user to one niche build.
- **Game research lives in its own project.** For example, Shantae analysis happens in the Shando project, not here. In this repo, games are only test targets for the tools.

## What upstream already gives us

Upstream's [SCOPE.md](SCOPE.md) is its original planning document (written before any code), and [RECIPES.md](RECIPES.md) shows its usage patterns. Everything upstream planned shipped, plus batched input playback:

| Area | Tools | Notes |
|---|---|---|
| Discovery | `get_info`, `list_memory_domains`, `ping` | Named domains per core (WRAM, VRAM, OAM, HRAM, CartRAM, ROM, System Bus …) |
| Memory | `read8/16/32`, `read_range` (≤ 4096 bytes), `write8/16/32`, `write_range` | Direct access; no mapper side effects |
| Search | `search_memory` | **Known-value** scan with iterative narrowing (8/16/32-bit, up to 16 MiB per scan) |
| Input | `press_buttons`, `press_buttons_multi`, `play_input_sequence` | Batched playback near 60 fps, with screenshots, memory observations and stop-on-change |
| Control | `pause`, `unpause`, `frame_advance`, `reset` | |
| State | `save_state`, `load_state`, `screenshot` | File-based savestates make reruns exact |
| Tooling | `scripts/replay-bk2.cjs` | Replays BizHawk `.bk2` movies through the bridge |

The fork has added so far:
- **Debugging:** `add/remove/list_breakpoint(s)`, `get_breakpoint_hits` and `get_registers`. These are logging breakpoints that capture registers, stack words and extra memory such as the current ROM bank.
- **Lifecycle:** `launch` and `close`.
- **Opt-in sandboxed `eval_lua`** behind a consent dialog and the `BIZHAWK_EVAL_LUA` config switch.
- **Tests** in CI.

Corrections to upstream's docs, learned the hard way:
- BizHawk 2.11's socket **only** exists when EmuHawk starts with `--socket_ip` and `--socket_port`. Neither the menus nor Lua can create it. SCOPE.md rated this risk "Low".
- RECIPES 1 and 2 diff RAM by pulling `read_range` dumps into the agent's context. That works for 2 KB of NES RAM but doesn't scale. Snapshot and compare ([RECIPES 10](RECIPES.md)) replaces it.

## The workflows, re-evaluated

| # | Workflow | Already possible | Missing capability |
|---|---|---|---|
| 1 | **RAM search with controls.** Do an action from a savestate, search for changed, unchanged, increased or decreased values, and rerun the same savestate *without* the action as a control. | Savestates plus `play_input_sequence` give exact, repeatable runs, which is the control half. `search_memory` handles values you already know. | ✅ Done: `snapshot_memory` and `compare_snapshots`. Multi-level pointer scans can come later. |
| 2 | **ROM structure mapping.** Banks, headers and file structures; marking data regions so tracing never treats them as code. | The ROM is a file the agent can read itself. Ghidra gives segments, strings and disassembly. `read_range` reads any domain, and breakpoints show what code touches. | **Execution coverage map:** record which ROM addresses (and banks) actually run as code during play, using `on_bus_exec_any`. That's the runtime fact static tools can't provide. Byte-pattern search ✅ done (`bizhawk_search_bytes`). |
| 3 | **Text extraction by speaker.** | Static table discovery can run on the ROM file. Read breakpoints with `capture` show the text routine, its pointer and the object that triggered it. VRAM and OAM reads and screenshots confirm what's on screen. | ✅ Relative search done (`bizhawk_search_bytes` mode `relative`). Sorting lines by speaker is agent work built from breakpoint captures. |
| 4 | **Instruction replication** to 95% confidence (Wilson lower bound; see [TESTING.md](TESTING.md)). | Breakpoints on a routine's entry with `capture` log real input states during play. | **Exit capture:** pair each entry with the matching return and the outputs, so normal gameplay produces (input, output) test cases. A **register write** tool (`emu.setregister`) for setting up tests. Driving a routine with made-up inputs mid-frame comes later and needs its own security review. |
| 5 | **Live helper scripts** (overlays, trackers, hitbox drawers), only when a human is truly needed. | `eval_lua` can draw with `gui.*`, but only for one frame. | **Persistent per-frame sandboxed scripts:** register, list and remove scripts that run every frame, behind the same consent gate, with a per-frame budget, and switched off automatically after errors. |

Some things stay out of the server on purpose:
- **General file export:** scripts and tools return data, and the agent writes JSON with its own file tools, so the sandbox has no file access. The one exception is `bizhawk_dump_memory`, which writes raw bytes only into the user's `BIZHAWK_DUMP_DIR` for external tools, so large dumps never pass through the agent's context.
- **Game-specific knowledge:** RAM maps, function names, text tables.
- **Static disassembly:** Ghidra does that.

## Roadmap

Each step is a branch (they all change the protocol), comes with tests at Layers 1 and 2, and gets a security review where marked.

| Step | Capability | Unblocks | Notes |
|---|---|---|---|
| 1 ✅ | **Memory snapshots and compare** (done; see RECIPES 10): `snapshot_memory` (named, per domain and range, stored in the bridge as compact strings) and `compare_snapshots` (changed, unchanged, increased, decreased, equal to, changed by N; 8/16/32-bit, signed or not; narrows existing candidates) | W1, and pointer hunting in general | Pure reads. Return counts plus a capped candidate list. Set a total memory cap for snapshots. |
| 2 ✅ | **Byte-pattern and relative search** over any domain (done: `bizhawk_search_bytes`, RECIPES 11) | W2, W3 | Pure reads. Wildcards (`??`). Relative mode for unknown text encodings. |
| 2½ ✅ | **Memory dumps to files** (done: `bizhawk_dump_memory`, fenced to `BIZHAWK_DUMP_DIR`) | External decoders and skills (e.g. tile and sprite extraction) | Writes files, so it's limited to a folder the user configures. |
| 3 | **Execution coverage map** over N frames, with bank capture | W2, W4 | `on_bus_exec_any` is very slow per instruction, so cap the frames, report the slowdown, and return merged address ranges. |
| 4 | **Breakpoint upgrades:** capture memory ranges, exit capture (return pairing), and a `set_register` tool | W4 | The register write is a normal write tool, like memory writes. |
| 5 | **Persistent per-frame sandboxed scripts** | W5 | Same consent gate and sandbox. Security review. |
| 6 | **Fixture ROM and Layer 3 integration tests** ([TESTING.md](TESTING.md)) | Proves steps 1–5 against known answers | RGBDS build, redistributable. |
| 7 | **Catalog conventions (a doc, not a tool):** a shared format for findings (address, domain, bank, width, name, evidence, confidence) that maps onto Ghidra labels and `function BB:AAAA name` annotation files | Every workflow | Lets agents in different sessions build on each other's results. |

Skills (core-agnostic workflows built from these capabilities) live in `skills/`. The first, [`tile-graphics`](../skills/tile-graphics/SKILL.md), covers tile/sprite/palette extraction and was the motivation for `bizhawk_dump_memory`.

Later or optional: multi-level pointer scans, read and write coverage, and driving a routine with made-up inputs mid-frame (W4 phase B).
