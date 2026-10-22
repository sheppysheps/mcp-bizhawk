# Plan: research tool upgrades (2026-10-21)

Status: **in progress** (reviewed 2026-10-22: start with group A, one item at a time, the items that help the game repos most first). A1 is built on `feat/movie-tools-2` and waits for its first real pass.

The first movie captures (a 284,015-frame Game Boy Color TAS recorded four times on BizHawk 1.11.7) showed what game research needs next: room and door pointers are found by ranking bytes against lag bursts, a "teleport" was found as a block copy, and the writer PCs of watched bytes pointed straight at the routines involved. This plan turns those one-off analysis scripts into tools, and adds the live-emulator capabilities a randomizer project needs. Everything stays game-agnostic: addresses, domains and thresholds come from the user, never from the code.

It also takes in three open dev-master handoffs: **breakpoint upgrades**, the **coverage map**, and **catalog conventions**.

## What exists today

- **Movie recorder** (`lua/legacy/movie_recorder.lua`, BizHawk 1.x, no socket API): per-frame lag and input, memory snapshots (periodic, listed, or after a watched change), screenshots named by reason, write-watches with the writer PC.
- **`movie_export.py`:** `summary`, `frames`, `writes`, `timeline` and `compare`.
- **Live bridge** (BizHawk 2.x): `bizhawk_add_breakpoint` with kinds `read`, `write` and `exec`, short ranges, registers, stack words, extra memory `capture` (e.g. a bank byte) and `max_hits`; snapshots, compare, search and dump.

## Work items

### A. Movie recorder and `movie_export` (offline, any movie)

| # | Item | What it does | Why |
|---|---|---|---|
| A1 | **`exec_watch`** (built) | Config `exec_watch = <address> [stack <words>] [capture <start> <len>]... [max <hits>]`: on every execution of the address, log frame, registers, stack words and captured bytes to `execs.tsv`; hit counts in `run.tsv`. `movie_export execs` lists hits or groups them by fields (registers, pairs like `HL`, `stackN`, `capN`). | The routine that copies a table holds the table's address in a register. One line in a config gives a data table's ROM location. |
| A1b | **Write registers** (built) | `watch = <start> <len> registers [stack N] [capture ...] [max N]`: log registers at every write to the range (`write_hits.tsv`, `execs --writes`). Added when exec callbacks proved dead on BizHawk 1.11.7's Gambatte. | The registers of the code writing copied data show where it comes from. |
| A1c | **Windows and fades** (built) | `snapshot_range`, `stop_after`, `movie_export shots --dark`. | Fast transitions leave no lag burst; screenshot brightness finds them. |
| A1d | **Checkpoints** (built) | `save_states = <frames>` writes savestates during a pass; `start_state = <file>` loads one at startup so recording starts there. | Late parts of a long movie in seconds instead of replaying it; the starting point for interactive disassembly in the game repos. |
| A2 | **Writer summary** | `movie_export writers`: per watched address, each distinct writer PC once, with first/last frame and count. | The per-writer deduplication from Shando's tracer, done offline from `writes.tsv` (no recorder change). |
| A3 | **Bit-level diffs** (built) | `movie_export writes --bits`: for each change, which bits flipped (`+3 -0`). `movie_export flags [--once]`: per-bit history; `--once` lists bits set once and never cleared (pickups, opened doors, story flags) in frame order. | Item, key and door flags are single bits; bytes hide them. |
| A4 | **Annotations** | A `labels.tsv` next to a capture (`start`, `end`, `label`, `kind` such as `glitch`, `cutscene`, `menu`); `movie_export` shows labels in timelines and can exclude labeled ranges from analysis. | Glitch windows must not be cataloged as normal states, and logic presets need a glitch catalog. |
| A5 | **`rank`** | Product version of the ad-hoc ranking scripts: score every byte's changes between snapshots against an event list (lag bursts of a chosen length, a watched address's change frames, or labels), with precision, recall and distinct-value counts; restrict to regions; exclude labeled ranges. | Found the room pointer and the teleport record in minutes. Worth a tested command. |
| A6 | **`events`** | A transition table from a capture: for a watched pointer or block, each change with frame, old and new value, writer PC, the nearest lag burst and the screenshot. | The door-table starting point a randomizer needs. |
| A7 | **I/O register logging** | `watch_io = <start> <length> [all]`: log every write (not only changes) to I/O registers, summarized per frame (count, last value), with a cap. | Sound drivers rewrite the same registers constantly; change-only logging misses them. For the music/sound research. |

Recorder cost to keep in check: every watched byte is a callback. A1 and A7 add callbacks only at the configured addresses; A2-A6 are offline.

### A9. Labeling station (A4 labels + A6 events, merged)

Requested for the door and item randomizer MVP. A person names what the numbers can't; the tools supply what a person can't. Labeling happens **offline from captures**, not with prompts inside the emulator: no stopping a turbo pass at every door, no BizHawk 1.x `forms` dialogs (modal dialogs have blocked the emulator before), and labels can be corrected later.

1. **`movie_export events`** finds every transition in a capture: room-pointer changes, building-style record copies, fast fades found by brightness (`shots --dark`), and edge transitions once their variable is known. Each event gets frames, old and new pointers, writer PC, before and after screenshots, and a **guessed method** where the data tells (pointer change with a long load, record copy, fade without load, no fade).
2. **A local labeling page** (a static HTML file written into the export folder, never published, since the screenshots are game-derived) shows each event's before and after screenshots and asks:
   - *Doors:* where is the player now; where were they before; the **exit method** at the origin (walk off an edge, press up at a door, climb down or up a ladder, crawl, warp, cutscene); the **entry state** at the destination (standing, climbing, crawling, facing, falling); what is required to use it (none, an ability or form, an item, a key).
   - *Items:* what was picked up; whether it's major progression, a dungeon key, or minor.
   Each distinct place (pointer value) is named once; later visits inherit the name.
3. **Output:** `rooms.json`, `doors.json`, `items.json` in the shared findings format (C), each entry with its evidence (capture, frames, pointers, writer PCs).
4. **Filled in automatically per room:** a tileset fingerprint (hash of the VRAM tile data), the palette (recovered from the screenshot with the tile-graphics skill, marked with the core that rendered it), and the player's pose (sprite tiles from the OAM snapshot).

**Why entry state matters:** a randomizer that connects an exit to an entry of a different kind (an edge scroll into a ladder entry) can strand the player or confuse them. Recording both ends lets a randomizer offer pairing rules: same type only, any type, or one-way (decoupled) doors. Whether "any type" is safe depends on one research question per game: is the arrival state set by the destination, or carried over from the exit? Compare the player-state bytes on arrival at one destination reached by different exits.

### B. Live bridge (BizHawk 2.x)

| # | Item | What it does | Why |
|---|---|---|---|
| B1 | **Unique-writer mode** | Breakpoint option `unique: "pc"`: keep only the first hit per distinct PC (plus a count). | Shando's tracer keeps each writer once; buffers otherwise fill with the same loop. |
| B2 | **Reset on load** | Breakpoint option to clear hits when a savestate loads (or a hit is marked "after load"). | Stale hits recorded before a scene load mislead. |
| B3 | **Wide range capture** | Allow larger watch ranges (for `read` in particular), bounded by a byte cap. | "Who reads this table?" finds the code behind enemy stats, damage and door data. |
| B4 | **Exit capture** | For an `exec` breakpoint, also capture registers and memory when the routine returns (via its return address on the stack). | A routine's results (the computed value, the next state) are visible only at exit. |
| B5 | **`bizhawk_set_register`** | Set one CPU register (core-specific names, checked against the core's list). | Testing patches and forcing paths without editing ROM. |
| B6 | **Triggered writes** | Breakpoint option: on hit, write given bytes to memory (data only, never code execution; capped; off unless the call asks for it). | Spawn, day/night and start-location tests need a write at a precise moment. |
| B7 | **Coverage map** | Record which addresses execute (and, where the core allows, which are read as data) over a window, as a per-bank bitmap; export as ranges. | Separates code from data in a bank, isolates code that runs only during cutscenes or boss fights. Needs a check of BizHawk's execute-any callback cost first; may be limited to short windows. |
| B8 | **Patched-ROM test harness** | Scenario files: a savestate, an input sequence, and assertions on RAM ("room pointer becomes X within N frames"). Runs on 2.x, reports pass/fail. | Patched ROMs desync movies, so randomizer features must be checked by scenarios. |

Security: B5 and B6 only set registers and data bytes through the existing bridge; they don't run supplied code, so the arbitrary-Lua rules aren't touched. B6 stays off by default per call and is capped.

### C. Catalog conventions (shared with the game repos)

- One findings format, written as JSON lines: `address`, `domain`, `bank`, `width`, `name`, `evidence` (capture, frames, writer PCs, method), `confidence`, `labels`.
- Exporters to Ghidra label scripts and `BB:AAAA name` symbol files.
- Agreed with dev-master and the game repos before building; `movie_export rank`/`events` and breakpoint results then emit it directly.

## Feature to tool mapping

Planned randomizer features from the game side, and which items serve them:

| Feature | Needs | Items |
|---|---|---|
| Difficulty presets (enemy HP and damage) | The code that reads stat tables | B3, B1, A1 |
| Skip cutscenes, dialog and fanfares | Code that runs only in those scenes, their flags | B7, A4, A3 |
| Keysanity | Key counters and door/unlock flag bits | A3, A5 |
| Logic presets (glitch levels) | A glitch catalog with frame ranges | A4, C |
| Music and sound registers (research only) | The music-ID variable, sound-register writes | A7, A5 |
| Door randomization | Door tables (one per transition type), exit methods, entry states | A1, A9, B4 |
| Item randomization | Item locations (room + pickup), item identity and class | A9, A3 |
| Testing any patch | Scenario checks on 2.x | B8 |

## Suggested order

1. **A1 `exec_watch`**, then a recorder pass on a known copy routine (fast, high value).
2. **A2-A5** (offline `movie_export` commands; pure Python with unit tests).
3. **C** (agree the format; small).
4. **B1, B2, B5** (small bridge changes), then **B3, B4, B6**.
5. **B8** test harness.
6. **B7** coverage map (research the callback cost first).
7. **A6, A7** alongside the research that needs them.

Branches: A items on `feat/movie-tools-2`; B items on `feat/breakpoints` (touches the Node↔Lua protocol, so both sides land together); C as `docs/` first.

## Porting to modern BizHawk

The recorder targets BizHawk 1.x because the movies studied so far only sync there. Modern BizHawk (2.x, Lua 5.4, current cores) is the long-term home, so the recorder is written to port cheaply:

- Everything that doesn't call BizHawk is a pure helper (config parsing, formatting, snapshot reasons, register text) tested on both Lua 5.1 and 5.4, and all analysis lives in `movie_export.py`, which only reads files. A port replaces the thin BizHawk layer (`setup`, `step`, `finish`, the callbacks), not the logic.
- The file formats (`frames.tsv`, `snapshots.tsv`, `writes.tsv`, `execs.tsv`, `run.tsv`) stay the same on both, so captures from either version compare with `movie_export compare`.
- Known 1.x workarounds to drop or keep behind a check in a port: API functions exposed as userdata, `readbyterange` refusing ranges that reach a domain's end, the speed cap below 6400%. 2.x adds `comm` sockets, so a 2.x recorder can also stream to the live bridge instead of only writing files.
- The live bridge's breakpoints (group B) already cover exec hits with registers on 2.x; A1 and B4 should share one hit format.

## Open questions for review

- Is the order right, or should B8 (test harness) come earlier?
- Should A4 labels live in the capture folder (local, game-specific) or in the shared findings format?
- How long may a coverage-map window be before it's too slow to be useful?
