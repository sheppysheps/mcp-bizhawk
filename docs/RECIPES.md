# mcp-bizhawk recipes

Practical examples of driving BizHawk through Claude (or any MCP client). Each recipe is self-contained — copy-paste the prompt at the top of a Claude conversation with `mcp-bizhawk` registered, and Claude will work through the tool calls.

> Prerequisites: BizHawk running with `--socket_ip=127.0.0.1 --socket_port=8766`, ROM loaded, `lua/bridge.lua` open in **Tools → Lua Console**, or let the agent start everything with `bizhawk_launch`. Sanity-check with `bizhawk_ping` first.
>
> Recipes 1 and 2 diff RAM by pulling `read_range` dumps into the conversation. That's fine for small RAMs like the NES's 2 KB. For anything bigger, use recipe 10: the snapshot tools do the diff inside BizHawk.

The big BizHawk superpower: **the same tools work across every system its cores cover**. The only thing that changes per platform is the memory-domain name (`WRAM` for SNES vs `RAM` for NES vs `RDRAM` for N64) and the button names. Use `bizhawk_get_info` and `bizhawk_list_memory_domains` to discover both for any loaded ROM.

---

## 1. Find an SNES address from a value you can see on screen

> "I'm playing Super Metroid. Samus has 99 missiles. I want to find which WRAM address holds the missile counter. Fire one missile so it drops to 98, then find the address."

The classic two-snapshot diff. SNES WRAM is 128 KiB so it fits in 32 `read_range` calls of 4096 bytes each — but realistically the missile counter is in the first 8 KiB of game-state RAM, so start there.

```
1. bizhawk_read_range(address=0x0000, length=4096, domain="WRAM")    # snapshot A
2. <user fires a missile>
3. bizhawk_read_range(address=0x0000, length=4096, domain="WRAM")    # snapshot B
4. <Claude diffs A vs B for u8/u16/u32 that went 99 → 98>
```

For Super Metroid specifically, `0x09C6` (WRAM) is the missile counter. The diff approach finds it in seconds with no manual reverse engineering.

---

## 2. Same recipe, different system: NES

> "Mario has 5 lives in SMB1. Lose one, then find the lives counter."

```
1. bizhawk_read_range(address=0x0000, length=2048, domain="RAM")     # NES has 2 KiB RAM
2. <user dies>
3. bizhawk_read_range(address=0x0000, length=2048, domain="RAM")
4. <Claude diffs for byte that went 5 → 4>
```

Same tool calls, different domain name (`RAM` instead of `WRAM`), tiny address space (NES has only 2 KiB system RAM). On Genesis it would be `68K RAM` and 64 KiB, on N64 `RDRAM` and 4–8 MiB. The pattern transfers verbatim.

---

## 3. Inject a value into a known address

> "Give Samus 999 missiles. The address is WRAM 0x09C6 (missile count) and 0x09C8 (missile capacity), both u16-LE."

```
bizhawk_write16(address=0x09C6, value=999, domain="WRAM")
bizhawk_write16(address=0x09C8, value=999, domain="WRAM")
```

Memory writes go straight through BizHawk's memory accessor — there's no MBC / mapper / DMA mediation, so they're appropriate for cheats, debug pokes, and savestate-style seeding, but **not** for emulating cartridge behaviour. Want to seed cart save RAM with proper semantics? Use `bizhawk_load_state` with a pre-prepared state instead.

---

## 4. Verify a write landed

> "I just wrote 0xDEADBEEF to N64 RDRAM address 0x80100000. Confirm."

```
bizhawk_write32(address=0x80100000, value=3735928559, domain="RDRAM")
bizhawk_read32(address=0x80100000, domain="RDRAM")
```

Or use `bizhawk_read_range` for raw bytes and decode endianness manually — useful when you're debugging BizHawk's per-domain endianness conventions (most are little-endian; N64 / older PowerPC stuff can surprise you).

---

## 5. Drive a character with frame-precise input

> "On NES Super Mario Bros: hold Right for 60 frames, then jump (A) for 8 frames, while still holding Right."

BizHawk's `joypad.set` sets state for **one frame** — so to hold a button across N frames you call `bizhawk_press_buttons` and then `bizhawk_frame_advance(count=N)` while it's held, repeating each frame the press needs to remain. The cleanest pattern is to interleave:

```
for _ in 1..60:
    bizhawk_press_buttons(buttons={"Right": true})
    bizhawk_frame_advance(count=1)

for _ in 1..8:
    bizhawk_press_buttons(buttons={"Right": true, "A": true})
    bizhawk_frame_advance(count=1)
```

Per-call latency is ~one frame each (it's how the bridge works), so this is real-time-ish but not faster-than-realtime. For TAS-style frame-perfect input over thousands of frames you'd want BizHawk's native TAStudio instead — this MCP surface is for interactive exploration, not movie creation.

---

## 6. Snapshot → experiment → restore

> "Save state, try this risky write, screenshot the result. If it crashed the game, reload."

```
bizhawk_save_state(path="C:/temp/before.State")
bizhawk_write32(address=0x...., value=0xDEADBEEF, domain="WRAM")
bizhawk_frame_advance(count=10)
bizhawk_screenshot(path="C:/temp/after.png")
# if Claude inspects the screenshot and decides things look bad:
bizhawk_load_state(path="C:/temp/before.State")
```

BizHawk's savestates are path-based (not slot-based like mGBA's). The state file captures the entire emulator state — RAM, registers, mapper state, sound chip state — so reloading is a true rewind.

---

## 7. Walk every memory domain to discover what's interesting

> "What memory domains are available, and how big is each?"

```
1. bizhawk_get_info()                   # overview + framecount + capabilities
2. bizhawk_list_memory_domains()        # array of domain names
3. for each domain:
       bizhawk_read_range(address=0, length=16, domain=name)
                                        # quick sample to see if it has data
```

Useful when you've loaded an unfamiliar ROM and want to understand the memory map before hunting values. Names vary by core: SNES has `WRAM`/`VRAM`/`CARTROM`/`CARTRAM`/`Waterbox PageData`, N64 has `RDRAM`/`SP DMEM`/`SP IMEM`/`PI Reg`/etc., Genesis has `68K RAM`/`Z80 RAM`/`VRAM`/`CARTRAM`.

---

## 8. Cross-system regression test of a homebrew

> "I'm porting my game to NES, Genesis, and GBA. The score variable is at the same logical offset on all three. Verify by writing 12345 and reading back on each."

The Glama-pitch use case: same recipe, swap ROM + domain, no other changes.

```
# NES build loaded:
bizhawk_write16(address=0x0080, value=12345, domain="RAM")
bizhawk_read16 (address=0x0080,              domain="RAM")  # → 12345

# Genesis build loaded:
bizhawk_write16(address=0xFF0080, value=12345, domain="68K RAM")
bizhawk_read16 (address=0xFF0080,              domain="68K RAM")

# GBA build loaded:
bizhawk_write16(address=0x03000080, value=12345, domain="IWRAM")
bizhawk_read16 (address=0x03000080,              domain="IWRAM")
```

Each port runs in its own BizHawk session pointed at this MCP server — restart BizHawk between systems, the bridge picks back up automatically.

---

## 9. Replay a BizHawk `.bk2` TAS movie through mcp

> "I have a short BizHawk .bk2 movie. Can I replay it through the mcp bridge to see Samus do the same thing my recording captured?"

Yes — there's a reference tool in `scripts/replay-bk2.cjs` that does exactly this. It parses the .bk2, optionally restores the embedded starting savestate, and then plays back the input log via the `bizhawk_play_input_sequence` batched RPC.

Since 0.1.3 the tool uses **batched mode**: it ships 200 frames of input per single bridge round-trip, and `bridge.lua` runs the `joypad.set + emu.frameadvance` loop entirely server-side. Empirically this plays back at **~59 fps wall-clock** — essentially native 60-fps emulation speed. A 1242-frame movie replays in ~21 seconds; the same movie in the old per-frame mode (`press_buttons + frame_advance(1)` looping from the client) took 103 seconds at 12 fps.

This is still not the right tool for authoring or polishing a real TAS — BizHawk's built-in TAStudio is faster (no bridge at all) and has a frame-perfect editor. It IS a clean way to programmatically replay any captured input sequence through the bridge for inspection, instrumentation, or to drive deterministic re-runs from a Claude session.

### Setup

The replay tool needs to BE the bridge during playback (so it can hammer tool calls directly without going through MCP stdio + Claude). That means it competes with `mcp-bizhawk` for port 8766.

```bash
# 1. Find and kill the current mcp-bizhawk process so the port frees up.
#    (claude mcp list shows it; netstat -ano | grep :8766 finds the PID.)
taskkill /F /PID <mcp-bizhawk-pid>

# 2. Start the replay (it'll bind 8766 and wait for bridge.lua to connect).
node scripts/replay-bk2.cjs C:/path/to/your.bk2

# 3. Relaunch BizHawk with the standard flags so bridge.lua reconnects
#    to our script. (Stopping and restarting bridge.lua alone doesn't
#    recover the broken socket — needs a full BizHawk restart.)
EmuHawk.exe --socket_ip=127.0.0.1 --socket_port=8766 --lua=lua/bridge.lua your-rom.smc
```

The script then loads any embedded starting state from the .bk2, plays back every frame's input, and exits. After it exits, your next Claude Code session will respawn `mcp-bizhawk` and tools come back.

### Critical caveat: core mismatch silently breaks state restore

The .bk2 header records the exact BizHawk core that authored the movie (e.g. `Core BSNESv115+`). If your live BizHawk session is running a different core (e.g. `BSNES` not `BSNESv115+`, or `Snes9x`), `savestate.load` will silently reject the embedded state. The replay still runs but every input lands on whatever state BizHawk happens to be in — usually the title screen — producing no visible motion.

The replay tool now verifies the post-load framecount and ROM name to surface the mismatch loudly, but you should also pre-check by reading the .bk2 header (unzip + `Header.txt`) and matching BizHawk's **Config → Cores → SNES** to it before recording or replaying.

### Override the starting state

If the embedded state won't load (core mismatch, version drift, corruption), use `--state PATH` to supply a known-good savestate:

```bash
# Use a BizHawk QuickSave you made under the current core.
# QuickSaves live in <BizHawk>/SNES/State/<ROM>.<CORE>.QuickSave<N>.State
node scripts/replay-bk2.cjs movie.bk2 \
  --state "I:/BizHawk-2.11.1-win-x64/SNES/State/Super Metroid (Japan, USA) (En,Ja).BSNESv115+.QuickSave1.State"
```

Or `--no-state` to skip loading entirely and replay against whatever state BizHawk is currently in (useful if you've already positioned things by hand):

```bash
node scripts/replay-bk2.cjs movie.bk2 --no-state
```

### What a successful run looks like

The script prints per-60-frame progress with the input being held at that checkpoint:

```
Bridge ready. Pinging...
  pong
Loading starting state from ...QuickSave1.State...
  load_state RPC returned without error.
  post-load framecount: 19847
  post-load ROM: Super Metroid (Japan, USA) (En,Ja)

=== starting playback ===
  frame 60/1242  @12.1fps wall-clock  elapsed=5.0s  last=Right
  frame 120/1242  @12.0fps wall-clock  elapsed=10.0s  last=Down+Left
  ...
  frame 1242/1242  @12.0fps wall-clock  elapsed=103.5s  last=(none)

=== done. 1242 frames in 103.5s (avg 12.0 fps wall-clock) ===
```

If you see `post-load framecount: 0` or some value far from where you'd expect, the savestate didn't apply — fix the core mismatch and rerun.

---

## 10. Find an unknown value with snapshots and a controlled test

> "From this savestate, find the RAM address that changes when I press Start on the title screen. Use a control run so timers don't show up."

You don't need to know the value ahead of time. The bridge keeps snapshots and compares them inside BizHawk, and only counts and short address lists come back. Savestates make the runs repeatable: the same state plus the same input gives byte-for-byte the same RAM.

**A controlled test** (the action vs. no action, compared at the same frame):

```
1. bizhawk_save_state(path="C:/temp/base.State")
2. bizhawk_play_input_sequence(frames=[{}, {}, ... 60 frames])                      # control: no input
3. bizhawk_snapshot_memory(name="control", domain="WRAM")
4. bizhawk_load_state(path="C:/temp/base.State")
5. bizhawk_play_input_sequence(frames=[{"buttons":{"Start":true}} ×5, {} ×55])      # action
6. bizhawk_snapshot_memory(name="action", domain="WRAM")
7. bizhawk_compare_snapshots(before="control", after="action", op="changed", save_as="start_effects")
```

Anything that changes on its own, like timers and animation counters, ends up the same in both runs and drops out. Only what the action affected remains. Running the control twice and comparing those snapshots should give 0 matches, which is a quick way to confirm the game is deterministic from that savestate.

**Narrowing, like BizHawk's RAM Search:**

```
1. bizhawk_snapshot_memory(name="a", domain="WRAM")
2. <take damage>
3. bizhawk_snapshot_memory(name="b", domain="WRAM")
4. bizhawk_compare_snapshots(before="a", after="b", op="decreased", save_as="hp")
5. <heal>
6. bizhawk_snapshot_memory(name="c", domain="WRAM")
7. bizhawk_compare_snapshots(before="b", after="c", op="increased", candidates="hp", save_as="hp")
8. repeat until a few candidates remain, then confirm with bizhawk_write8 and a screenshot
```

On Shantae (GBC), snapshotting WRAM, advancing 1 frame and keeping what `increased`, then advancing 10 frames and keeping what `changed_by` 10, narrowed 32,768 addresses to 3 frame counters in two comparisons.

Use `width` (`u16`, `s16`, …) for values wider than a byte, `big_endian` on big-endian systems, and `bizhawk_delete_snapshots` when the 64 MiB store fills up.

---

## 11. Find byte patterns and text in an unknown encoding

> "Find where the game stores its dialogue, and work out how the text is encoded."

`bizhawk_search_bytes` searches inside BizHawk and returns only the matches.

**Relative search** is the standard ROM-hacking trick for text in an encoding you don't know yet. Pick a word you've seen on screen, ideally 5 letters or more, written in one case:

```
bizhawk_search_bytes(mode="relative", text="SHANTAE", domain="ROM")
→ 0xA05CA: 53 48 41 4E 54 41 45 29 3A 20 5B 31 …  (S = 0x53, A = 0x41)
```

The search ignores what value each letter is stored as. It matches byte runs whose *differences* equal the letters' differences (S→H is −11, H→A is −7, and so on), then reports the inferred mapping. `A = 0x41` means plain ASCII; something like `A = 0x80` means a custom table, so read the text with `0x80 = A, 0x81 = B, …` and fill in punctuation from what's around it. Spaces and punctuation in your word are wildcards, because games often encode them separately.

**Hex patterns** find code idioms and data headers. `??` matches any byte:

```
bizhawk_search_bytes(mode="hex", pattern="CD 40 05", domain="ROM")   # every SM83 "call $0540"
bizhawk_search_bytes(mode="hex", pattern="3E ?? E0 FF", domain="ROM") # ld a,?? / ldh ($FF),a
```

**Text mode** finds exact ASCII or UTF-8, such as a cartridge header title. To search what was on screen at a particular moment, snapshot VRAM or a tile map with `bizhawk_snapshot_memory`, then pass `snapshot="name"` to search that copy.

On Shantae (GBC), relative search over the whole 4 MiB ROM takes about 0.7 s and shows its dialogue is plain ASCII with bracketed control codes. Hex and text searches take about 0.1 s.

---

## Tips for Claude prompts

- **Always start with `bizhawk_get_info`** so Claude knows which system and which memory domains are live before reasoning about addresses.
- **Pass the `domain` parameter explicitly** when you know it — defaults to whichever domain BizHawk currently has selected, which can drift after savestate loads.
- **Snapshot framecount around mutations** so Claude can correlate before/after — `bizhawk_frame_advance(count=N)` returns the new framecount.
- **Memory reads in the first ~30 frames** after a hard reset return mostly zeros — the system hasn't initialized RAM yet. Advance frames or wait for the title screen.
