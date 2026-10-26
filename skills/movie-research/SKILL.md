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
python movie_export.py writes  <out> --address 0xC0A0      # who wrote this byte, when, which value (--bits: which bits)
python movie_export.py flags   <out> --range 0xD000 0x40 --once   # bits set once and never cleared: pickups, flags
python movie_export.py flags   <out> --snapshots WRAM --once --from 1100   # the same from periodic snapshots (offsets)
python movie_export.py locate  <out> --frame 5000 --domain CartRAM --inside WRAM   # live copy of the save file
python movie_export.py timeline <out> --address 0xC9F9 --width 2 --unique   # one row per room/map seen
python movie_export.py events  <out> --source room 0xC9F9 2 --source door 0xC100 1 --fades 0.5   # every transition
python movie_export.py compare <outA> <outB>               # first divergence between two recordings
python movie_export.py execs   <out> --address 0x1234 --group HL,DE,stack0   # distinct register values when code ran
python movie_export.py shots   <out> --dark 0.5            # screenshots darker than half the median: fades
python movie_export.py video   <out> --out run.mp4 --audio dump.wav   # MP4 with frame counter, inputs and sound
```

Add `--json <file>` to keep a full result. Long lists are truncated on screen with a count of what was left out.

**`compare` is the sync oracle.** Record the same movie on two emulators (or two versions), then compare:

- *first input difference* means they aren't the same movie or frame offset; fix that first.
- *first lag difference* means the emulators' timing diverged there; inputs after that frame land on different game states.
- *first snapshot difference* per domain shows which memory changed first, with byte offsets.

### Recipes

- **Which room or map is on screen?** Ask the game, not a vision model: every game keeps a "current room" variable. Record with `snapshot_on_change = <address> <width> 30`, then `timeline --unique` lists each distinct value with its first frame, visit count and a snapshot frame taken just after entering. Label each *distinct* value once, from its screenshot, instead of recognizing every frame. The `pc` of the writes leads to the room loader, and from there to each room's data. Confirm the address first: the value must change exactly at room transitions and nowhere else.
  - *Finding the variable:* room loads usually show up as lag bursts of a typical length in `frames.tsv`. Rank bytes by how well their changes between snapshots match those bursts (precision: it changes only at bursts; recall: it changes at every burst), then confirm the best candidates with `watch` on a second pass. A block of many bytes changing together is a buffer the room loader fills, not the ID.
  - *Not every transition lags.* Some doors are only a palette fade with no load, so they leave no lag burst. Find them by brightness instead: record the window with `snapshot_range = <start> <end> 5` and `stop_after`, then `shots --dark 0.5` lists the screenshots darker than half the median, i.e. the fades and black transition screens, to within the step. Rank bytes against those frames.
  - *Expect more than one kind of transition.* Bursts the candidate doesn't explain are the next lead: a game may teleport inside one large map (a fade with no room change), or scroll into a neighboring map with no fade at all. Rank again against only the unexplained bursts, and check a few screenshots by eye, since the person watching the movie spots transitions that the numbers miss.

- **Where is the table a routine reads?** The registers of the code that copies a value point at its source. Two ways to log them:
  - `watch = <dest start> <length> registers stack 2` logs the registers at every write to the copied data (`write_hits.tsv`; read it with `execs --writes`). This works wherever write-watches work, and the first write of each copy shows the source pointer at its start.
  - `exec_watch = <pc> stack 2` logs them when the code itself runs (watch a few addresses before a writer PC, since the logged PC can be the next instruction). Not every core fires execute callbacks (BizHawk 1.11.7's Gambatte doesn't), so check `execs.tsv` early in the run.
  
  `execs [--writes] --group HL,DE,stack0` (register names depend on the CPU) lists each distinct source pointer, with the caller's return address and the frames it happened. The source pointer is the table entry; the caller usually chose it. Add `capture` for the game's "current bank" variable when the CPU maps code and data in banks.

- **Study a late part of a long movie quickly.** A movie is only inputs, so the game state at frame N exists only after playing frames 0 to N-1; cutting the input log doesn't skip anything. Use checkpoints instead: one pass with `save_states = <frames>` writes savestates, and later passes set `start_state` to one of them. In read-only playback BizHawk continues the movie from the loaded frame. Confirm a checkpoint once: record the same window from frame 0 and from the state, and `compare` must report them identical.
- **When does an event happen?** Snapshot every N frames, find the two snapshots it falls between (by screenshot or a known RAM value), then snapshot every frame in that gap.
- **Which byte is a flag or counter?** Diff snapshots from just before and just after the event, then `watch` the candidate addresses on a second pass: `writes` shows the exact frame, the old and new values, and the PC of the code that wrote it (a lead into the disassembly).
- **Which bit is an item, key or door flag?** Games pack these into single bits, so a byte going 0x04 → 0x0C is really "bit 3 set". Watch the candidate range for a whole run, then `flags --once` lists every bit that started clear, was set exactly once and never cleared, in frame order: the run's collected items, opened doors and story flags. `writes --bits` shows the flipped bits next to each change. Bits that toggle back and forth are states (a door currently open, an ability in use), not pickups. Match each first-set frame with its screenshot to name it.
- **Where are the item flags at all?** Start from the save file: everything a game must remember between sessions (items, keys, flags, counters) is in it, so its live copy in RAM is the progress block. Record a whole run with `domains = WRAM, CartRAM` (or the core's equivalents), `snapshot_every = 100` and `screenshots = true`. CartRAM fills at the first in-game save; `locate --frame <that frame> --domain CartRAM --inside WRAM` gives the offset of the live copy (trailing padding is ignored; a few bytes may differ: a checksum, or values changed since the save). Then `flags --snapshots WRAM --once --from <first gameplay frame> --range <offset> <length>` lists the block's pickups, each between two snapshot frames. Starting at the first gameplay frame skips RAM being initialised; across all of WRAM, buffers filled for the first time also look like flags, which is why narrowing to the block matters. Check counts against known totals (a 100% run's item counts) to tell flags from counters.
- **Which text does a run show, and where is it in ROM?** Find the text cursor in two steps. First, from a snapshot with a dialog box on screen, relative-search RAM for a word on screen: the hit is the box's tile map or its RAM shadow, and the offset says how the font maps to character codes. Then search the ROM for the full line and look in that snapshot for a RAM pointer (plus a bank byte, on banked systems) to just past the text typed so far: that variable is the cursor. Watch it (`watch = <cursor> <width>`, no registers needed) for a whole run; each jump that isn't a one- or two-byte step forward starts a new string, so the log gives every string shown with its frame, and the ROM bytes there show the terminator and control codes. Watching the box itself works less well: text scrolls between lines, and on systems with banked RAM the same address holds other data in other banks.
- **A video to scan by frame number.** Record a pass with `snapshot_every = 1`, `screenshots = true` and no `domains` line (screenshots only), then `video --out run.mp4`: the picture scaled with sharp pixels, the movie frame number (counted by ffmpeg, so it matches every other capture of the movie) and each frame's input with a LAG marker in a bar underneath. For sound, start the emulator's WAV dump before playback begins and stop it after the pass; `--audio` aligns it (a dump that ran past the movie's end is assumed to start at frame 0; set `--audio-offset` otherwise). Emulated sound is produced per frame, so a dump made in turbo plays back in sync at normal speed. Needs ffmpeg with the drawtext and ass filters.
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
