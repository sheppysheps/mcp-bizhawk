# Fork goals: autonomous game research

This fork (`sheppysheps/mcp-bizhawk`) extends [dmang-dev/mcp-bizhawk](https://github.com/dmang-dev/mcp-bizhawk). The goal is to let coding agents **research a game's internals on their own**. Agents run repeated controlled tests, manipulate savestates, set breakpoints and write bespoke Lua, and use the results to catalog how a game stores and changes objects and values in its ROM and RAM.

Other reverse-engineering tools can run alongside this server, such as Ghidra or REA MCP servers. They help, but they aren't required.

## Principle: capabilities, not features

This repo ships **general capabilities**: reading and searching memory, savestates, breakpoints, input, screenshots and sandboxed Lua. It does **not** ship game-specific or task-specific features.

An agent builds a pointer walker, an "ESP-style" overlay or a dialog dumper **when a task asks for one**, out of those capabilities. Nothing like that comes pre-packaged.

So when a task can't be done, the fix is a missing *capability*, not a new built-in feature. Capabilities that run code or touch the system follow the least-privilege rules in [lua/sandbox.lua](../lua/sandbox.lua) and [src/launcher.ts](../src/launcher.ts).

## Target workflows

A user prompt plus a savestate should be enough to run any of these:

| # | Workflow | What the agent does |
|---|---|---|
| 1 | **RAM search with controls** | Does the requested in-game action from a savestate, searches for changed, unchanged, increased or decreased values the way BizHawk's RAM Search does, and reruns from the same savestate *without* the action as a control, to weed out false positives. Finds values, pointers and offsets. |
| 2 | **ROM structure mapping** | Works like BizHawk's Hex Editor: identifies headers, banks and file structures, and marks data regions as non-code so CPU tracing never treats them as instructions. This supports exporters for sound and graphics, patches and quick ROM hacks. |
| 3 | **Text extraction** | Finds valid blocks of ASCII, Unicode or game-specific text encodings, dumps the full script and dialog, and sorts it by speaking NPC into separate JSON objects. |
| 4 | **Instruction replication** | Reimplements an identified routine in a test program and checks it against the emulator across many inputs, to statistical confidence (target: 95%). |
| 5 | **Live helper scripts** | Writes Lua that shows information in real time, such as pointer walkers, graphs, overlays, minimaps, enemy health trackers and hitbox drawers. These are only for when the agent is certain a task needs a human in the loop. |

## Known capability gaps

These are what the workflows above need and the server can't do yet:

- **Unknown-value search** (changed, unchanged, increased, decreased): `bizhawk_search_memory` only matches a known value. BizHawk's RAM Search and Hex Editor are GUI tools that Lua can't drive, so their logic has to be rebuilt as capabilities, for example memory snapshot and compare.
- **Persistent per-frame scripts:** `bizhawk_eval_lua` runs once, within a single frame, and `gui.*` drawings disappear on the next frame. Workflow 5 needs a sandboxed callback that runs every frame, behind the same consent gate.
- **Large reads:** sandbox bulk reads are capped at 64 KB per call. Agents read big regions in chunks.
- **Exporting files:** the sandbox has no file access. That's by design: scripts return data, and the agent writes any JSON or dumps with its own tools.
