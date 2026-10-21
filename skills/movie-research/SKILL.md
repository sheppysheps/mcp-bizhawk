---
name: movie-research
description: Research a game by recording what a BizHawk movie (a .bk2 TAS or input recording) does frame by frame - lag frames, inputs, memory snapshots, screenshots and watched memory writes with the code address that wrote them - then summarizing and comparing recordings. Works for any game and core, including old movies that only sync on BizHawk 1.x (no sockets there, so it records to files). Use when asked to use a movie or TAS to find RAM addresses, flags, item or event frames, to check whether a movie syncs on another emulator version, or to find where two runs diverge.
---

# movie-research

A movie replays the same inputs on the same emulator, so the game runs **identically every time**. That makes a movie a perfect, repeatable research subject: a 100% TAS visits nearly everything a game can do, in a fixed order, and any frame can be revisited exactly.

This skill has two parts:

- `lua/legacy/movie_recorder.lua` (in the mcp-bizhawk repo) runs **inside BizHawk** and records the movie to files.
- `scripts/movie_export.py` (Python 3.9+, standard library only) reads those files and prints short summaries and comparisons.

## Priorities

1. **Correct, complete data.** A recording is only valid if the movie **syncs** on that emulator (see step 0). Keep every recorded file; never trim data to save space.
2. **Efficiency.** Record once at full speed, then query the files as often as needed. Read the tools' summaries, not the raw files.
3. **A report someone can check**, written last (see "Report").

## 0. Pick the emulator the movie syncs on

Read the movie's `Header.txt` (a `.bk2` is a zip file): `emuVersion`, `Core`, and `SyncSettings.json`.

- A movie only reproduces the game on an emulator whose **core behaves cycle-for-cycle like the one that recorded it**. Newer cores fix accuracy bugs, and that alone can shift lag frames and break a run (especially luck-manipulated or lag-tuned TASes).
- **Check sync before trusting anything**: play the movie and confirm it reaches its ending (or the frame you need). If it desyncs, the first sign is usually early: a menu or dialogue that a TAS would skip instantly lingers, or the title screen never advances.
- The safest choice is **the BizHawk version named in the header**, stock. Old versions (1.x) have no socket API, so the live mcp-bizhawk bridge can't connect; record with `movie_recorder.lua` instead.
- Sync settings get renamed between versions without migration (e.g. Gambatte's `GBACGB` became `ConsoleMode`). A setting the new version doesn't understand silently falls back to its default.
- BizHawk 2.x forces the official boot ROM when playing Game Boy movies; movies recorded without one won't sync there.

## 1. Record

On the BizHawk the movie syncs on:

1. Copy `lua/legacy/movie_recorder.example.cfg` to `movie_recorder.cfg` next to the script, and set `out` (an existing folder, **outside any repo**), the `domains` to dump, `snapshot_every` and/or `snapshot_frames`, and any `watch` ranges.
2. **Emulation > Pause**, then **File > Movie > Play Movie** with **Read-only** ticked. Read-write mode would let inputs overwrite the movie, so the recorder refuses to run in it.
3. **Tools > Lua Console > Script > Open Script** > `movie_recorder.lua`, then unpause.

**Speed is free; frame skip isn't.** Running unthrottled never changes what a frame computes, so record as fast as the PC allows (untick Config > Speed/Skip > Clock Throttle if the recorder warns it couldn't). But frame skip skips *drawing*: memory, lag and inputs stay exact, while screenshots show the last *drawn* frame and can be a few frames stale. Set **Frameskip to 0** whenever screenshots must match their frame (e.g. for `tile-graphics`).

Start with a cheap first pass: no `watch` lines, `snapshot_every = 1000`. It records every frame's lag flag and input, plus periodic snapshots. Then do targeted passes: `snapshot_frames` around an event, and `watch` on a narrow address range (every watched byte costs a callback, so the total is capped by `max_watch_bytes`).

Files written to `out`: `frames.tsv`, `snapshots.tsv` plus `snap_f<frame>_<domain>.bin`, `shot_f<frame>_<reason>.png` (reason `every`, `listed` or `change-0x<address>`, so a folder listing shows which shots a watched change triggered), `writes.tsv`, `run.tsv`. Frame numbers are BizHawk's on-screen frame counter after the frame ran.

## 2. Read the results

Run from `skills/movie-research/scripts/`:

```
python movie_export.py summary <out>                       # totals, lag runs, warnings
python movie_export.py frames  <out> --from 140 --to 160   # lag flags and inputs per frame
python movie_export.py writes  <out> --address 0xC0A0      # who wrote this byte, when, which value
python movie_export.py timeline <out> --address 0xC9F9 --width 2 --unique   # one row per room/map seen
python movie_export.py compare <outA> <outB>               # first divergence between two recordings
python movie_export.py execs   <out> --address 0x1234 --group HL,DE,stack0   # distinct register values when code ran
```

Add `--json <file>` to keep a full result. Long lists are truncated on screen with a count of what was left out.

**`compare` is the sync oracle.** Record the same movie on two emulators (or two versions), then compare:

- *first input difference* means they aren't the same movie or frame offset; fix that first.
- *first lag difference* means the emulators' timing diverged there; inputs after that frame land on different game states.
- *first snapshot difference* per domain shows which memory changed first, with byte offsets.

### Recipes

- **Which room or map is on screen?** Ask the game, not a vision model: every game keeps a "current room" variable. Record with `snapshot_on_change = <address> <width> 30`, then `timeline --unique` lists each distinct value with its first frame, visit count and a snapshot frame taken just after entering. Label each *distinct* value once, from its screenshot, instead of recognizing every frame. The `pc` of the writes leads to the room loader, and from there to each room's data. Confirm the address first: the value must change exactly at room transitions and nowhere else.
  - *Finding the variable:* room loads usually show up as lag bursts of a typical length in `frames.tsv`. Rank bytes by how well their changes between snapshots match those bursts (precision: it changes only at bursts; recall: it changes at every burst), then confirm the best candidates with `watch` on a second pass. A block of many bytes changing together is a buffer the room loader fills, not the ID.
  - *Expect more than one kind of transition.* Bursts the candidate doesn't explain are the next lead: a game may teleport inside one large map (a fade with no room change), or scroll into a neighboring map with no fade at all. Rank again against only the unexplained bursts, and check a few screenshots by eye, since the person watching the movie spots transitions that the numbers miss.

- **Where is the table a routine reads?** The registers of the code that copies a value point at its source. Two ways to log them:
  - `watch = <dest start> <length> registers stack 2` logs the registers at every write to the copied data (`write_hits.tsv`; read it with `execs --writes`). This works wherever write-watches work, and the first write of each copy shows the source pointer at its start.
  - `exec_watch = <pc> stack 2` logs them when the code itself runs (watch a few addresses before a writer PC, since the logged PC can be the next instruction). Not every core fires execute callbacks (BizHawk 1.11.7's Gambatte doesn't), so check `execs.tsv` early in the run.
  
  `execs [--writes] --group HL,DE,stack0` (register names depend on the CPU) lists each distinct source pointer, with the caller's return address and the frames it happened. The source pointer is the table entry; the caller usually chose it. Add `capture` for the game's "current bank" variable when the CPU maps code and data in banks.

- **When does an event happen?** Snapshot every N frames, find the two snapshots it falls between (by screenshot or a known RAM value), then snapshot every frame in that gap.
- **Which byte is a flag or counter?** Diff snapshots from just before and just after the event, then `watch` the candidate addresses on a second pass: `writes` shows the exact frame, the old and new values, and the PC of the code that wrote it (a lead into the disassembly).
- **Known values are anchors.** Glitches leave predictable, abnormal values (a counter underflowing to 255, money going up when buying). Read the run's submission notes first: they say which glitches happen where, and those frames should be flagged, not treated as normal game behavior.

## Caveats

- **Colors from old cores are not reference colors.** Record screenshots, tile coordinates and sprite sheets for layout, and re-extract colors later on an accurate core (see the `tile-graphics` skill).
- **BizHawk 1.x Lua is not sandboxed** (`io`, `os` and `luanet` are open, and Lua 5.1 can't refuse bytecode). Only run scripts you have reviewed. The recorder writes only into `out`, never runs its config and never uses the network.
- **Movies, savestates, ROMs and firmware are never committed.** Keep them and all recordings outside the repo.

## Report

Same format as `tile-graphics`. Every report must stand alone:

1. **Answer first:** the frame, the address, the verdict ("syncs to the end" / "diverges at frame N").
2. **What was recorded:** emulator version and core, the movie (author and source, e.g. its TASVideos submission), the config used.
3. **Checks:** sync evidence, lag counts, warnings from the tools.
4. **Commands and code:** the exact commands, the recorder version (`run.tsv`), and any helper scripts you wrote, saved next to the report.

Quote a few rows or a short JSON excerpt when they show the key evidence; never paste whole files.

## Tests

```
python -m unittest discover -s skills/movie-research/tests -v
lua5.1 test/lua/run-legacy.lua
```
