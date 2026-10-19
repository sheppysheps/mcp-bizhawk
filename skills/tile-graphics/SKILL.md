---
name: tile-graphics
description: Extract tile graphics, tile maps and sprites from a running game as PNG + JSON, using mcp-bizhawk memory dumps. Raw tile sheets for any tile-based system (Game Boy, NES, SNES, GBA, Genesis layouts), optionally colorized; full Game Boy / Game Boy Color decoding (tile banks colored by use, tile maps with CGB attributes, OAM sprites, palettes recovered from a screenshot) with a pixel-exact self-check and negative control against BizHawk's screenshot. Use when asked to rip, view, catalog or verify a game's tiles, sprites, backgrounds or palettes, or to render a binary dump as tiles.
---

# tile-graphics

Turn emulator memory into images a person or an agent can inspect: tile sheets, whole tile maps, sprite sheets, the composed screen, and palettes, each with compact JSON describing what was decoded. Everything runs on **Python 3.9+ standard library only** (no pip installs), so every line is auditable.

The skill only reads dump files. It never talks to the emulator: you capture state with mcp-bizhawk tools, then run `scripts/tilegfx.py` on the files.

**Priorities, in order:**

1. **Complete, correct data.** Extract everything that was asked for, verify it against the screenshot, and keep the full results: the tools write every map cell, all 40 sprites and every recovered color to files. Never trim data to save space; keep it in files instead.
2. **Efficiency.** Use the bundled commands instead of writing decoders from scratch. Read the one-line summaries the tools print, not the JSON files, unless you need a specific value.
3. **A report someone can check** (section 4), written last. It costs little and comes after the data is right.

## Prerequisites

- mcp-bizhawk with **`BIZHAWK_DUMP_DIR`** set in its MCP config (where `bizhawk_dump_memory` writes).
- A game running in BizHawk (`bizhawk_launch`).

## 1. Capture one moment (mcp-bizhawk tools)

Pause first, so every dump and the screenshot describe the **same frame**:

```
bizhawk_pause()
bizhawk_dump_memory(file="<game>/<tag>_vram", domain="VRAM")
bizhawk_dump_memory(file="<game>/<tag>_oam",  domain="OAM")
bizhawk_dump_memory(file="<game>/<tag>_io",   domain="System Bus", start=0xFF40, length=12)   # Game Boy registers
bizhawk_screenshot(path="<BIZHAWK_DUMP_DIR>/<game>/<tag>_shot.png")
```

Use `bizhawk_get_info` / `bizhawk_list_memory_domains` first on other systems: domain names differ (e.g. `VRAM`, `PPU Bus`, `CHR`, `OAM`).

Check the screenshot before decoding: a solid black or white frame (a fade) can't verify anything. Advance a few frames and capture again.

## 2. Decode (scripts/tilegfx.py)

Run from `skills/tile-graphics/scripts/` (or give its full path). `G=<BIZHAWK_DUMP_DIR>/<game>`. Each command prints a short summary and the files it wrote.

**Any system: raw tile sheet**

```
python tilegfx.py tiles $G/<tag>_vram.bin --format gb2bpp --count 384 --out $G/tiles.png
python tilegfx.py tiles $G/rom.bin --format nes2bpp --offset 0x8010 --count 512 --out $G/chr.png
python tilegfx.py tiles $G/chr.bin --format nes2bpp --colors "#000000,#B53120,#EA9E22,#FFFFFF" --out $G/chr_color.png
```

| `--format` | Layout | Systems |
|---|---|---|
| `gb2bpp` | 2bpp, planes interleaved per row | Game Boy, GBC, SNES 2bpp |
| `nes2bpp` | 2bpp, plane 0 rows then plane 1 rows | NES / Famicom |
| `snes4bpp` | 4bpp, planes 0+1 then 2+3 | SNES 4bpp, PC Engine BG |
| `gba4bpp` / `gba8bpp` | linear, low nibble = left pixel | Game Boy Advance, DS |
| `genesis4bpp` | packed, high nibble = left pixel | Genesis / Mega Drive, Master System |

Raw sheets are grayscale by color index (0 = white) unless you pass `--colors` (one `#RRGGBB` per index) or `--palettes <json> --palette bg:N`. ROM graphics may be compressed: if a sheet looks like noise, dump the decompressed copy from video memory instead. Before calling data "not graphics", show the evidence: blank-tile count, byte entropy, repeating patterns, and the zoomed sheet.

**Game Boy / Game Boy Color: full decoding**

Common inputs: `IN="--vram $G/<tag>_vram.bin --oam $G/<tag>_oam.bin --io $G/<tag>_io.bin"`. A 16 KiB VRAM dump means CGB mode (override with `--dmg`/`--cgb`).

```
# CGB only: recover palettes from the screenshot (DMG palettes come from BGP/OBP0/OBP1)
python tilegfx.py gb-infer-palettes $IN --screenshot $G/<tag>_shot.png --out $G/data/pal.json

# Self-check: rebuild the screen, compare with the screenshot, run a negative control
python tilegfx.py gb-verify $IN --screenshot $G/<tag>_shot.png --palettes $G/data/pal.json \
    --diff $G/diff.png --report $G/data/verify.json

# Outputs
python tilegfx.py gb-tiles   $IN --palettes $G/data/pal.json --out $G/vram --json $G/data/vram.json
python tilegfx.py gb-map     $IN --palettes $G/data/pal.json --out $G/map.png --json $G/data/map.json
python tilegfx.py gb-sprites $IN --palettes $G/data/pal.json --out $G/sprites.png --json $G/data/sprites.json
python tilegfx.py gb-screen  $IN --palettes $G/data/pal.json --out $G/screen.png --scale 1
python tilegfx.py montage $G/<tag>_shot.png $G/screen.png $G/diff.png --out $G/compare.png
```

- `gb-tiles`: VRAM bank 0 (and bank 1 on CGB), 16 tiles wide; each tile drawn in the palette a tile map or sprite uses it with, unused tiles gray.
- `gb-map --map bg|win|9800|9c00`: the whole 256x256 map (more than the visible 160x144), plus JSON grids: `tile_numbers[row][col]` and, on CGB, `cgb_attributes` (palette, bank, flips, priority bits explained in the file).
- `gb-sprites`: all 40 OAM objects in a 10-column sheet, plus JSON for the on-screen ones (position, tile, palette, bank, flips, priority). Grouping objects into a character ("metasprite") is game-specific: use the positions.

## 3. Read the results

- **`gb-verify` verdict** (also its exit code): `match` (0), `mismatch` (1), or `inconclusive` (2) when the screenshot is too plain or the **negative control** (the same render with every tile blanked) matches too. A match only counts when the control is far lower; on the Shantae title screen it was 100% vs 19%.
- **Colors from the screenshot check structure, not color.** When palettes were inferred from the same screenshot, 100% proves the tiles, maps, sprites and priorities are right; the colors match by construction. Say so in the report.
- **`gb-infer-palettes` conflicting pixels**: screenshot pixels that disagreed with the majority color for their (palette, color index). 0 means the decode is consistent with the picture. Colors are recovered only for palette entries visible on screen; capture other scenes to fill more.
- **Below 100%? Suspect registers that changed mid-frame.** The IO dump is read at the end of the frame, but games change registers while the screen draws (status bars, split scrolling) or during vblank. Look at where the magenta is, then override with `--lcdc`, `--scx`, `--scy`, `--wx`, `--wy`, and **use the same overrides for every command, including `gb-infer-palettes`**. Example, Shantae's title screen: LCDC read `0xE5` (sprites off), probably because the game turns sprites off at the start of vblank while it updates them; the "Press Start" plate is sprites, and `--lcdc 0xE7` gave a 100% match. The tools warn when the palettes were inferred with a different LCDC.
- Recovered colors are in **the screenshot's color space** (BizHawk's GBC color correction, if enabled), which is what the game looks like, not raw RGB555 hardware values.

### Getting real CGB palettes

BizHawk's Lua can't read CGB palette memory: the palette data ports `$FF69`/`$FF6B` can't be driven from Lua (writes to I/O through the System Bus are ignored), so don't advise reading palettes through `$FF68`/`$FF69`. Routes that work:

1. **Infer from a screenshot** (`gb-infer-palettes`): fast, covers what's on screen.
2. **Catch the game writing them** (not yet verified): a write breakpoint on `$FF69` (BG) or `$FF6B` (OBJ) with `bizhawk_add_breakpoint`, then read register A at each hit; on the Gambatte core write callbacks report the written value as 0. The address the game copies from would lead to the palette table in ROM/RAM. If you try this, report whether it worked.
3. **By eye**: BizHawk's GB GPU Viewer window shows the palettes; useful for spot checks only.

## 4. Report

**Where files go:** images (1x for machine checks, plus the `_4x` copies every image command writes) at the top of the output folder, full JSON under `data/`, the report as `00-report.md` so it lists first. Make side-by-side images for every comparison (screenshot | rebuilt | diff, before | after) with `tilegfx.py montage`. Never paste whole JSON (maps, sprite lists, palettes) into the report or the conversation: it buries the answer and fills the context. Quote the few values that matter.

**Read text inside graphics** (titles, logos, badges) from the zoomed copy, not the 1x image, and say when a letter is uncertain. At 1x, "GBA" and "CGB" look alike.

**Brief report (the default, and always when you're one agent in a pipeline):**

1. **Answer** in the first sentence: the dimensions, the match percentage, what the files are, or that the input can't support an answer.
2. **Files:** each file, what it shows, and whether it's **real colors** or **grayscale by color index**.
3. **Checks:** the verification numbers (match %, negative control, conflicting pixels) and every `WARNING` the tools printed.
4. **Commands:** the exact `tilegfx.py` commands, so the work can be rerun on another frame.

**Full write-up (when a person asks how or why, or the result is surprising):** add a numbered **method**, each step with its reason ("Recovered palettes from the screenshot, because BizHawk's Lua can't read CGB palette memory"), and save any extra code you wrote next to the outputs.

**If something looks wrong,** report what you suspect, the evidence (numbers, byte patterns, an image showing it) and the command that shows it, then the next steps to try. Don't end with a question: in a pipeline nobody is there to answer it.

## Extending to another system

Tile decoders live in `scripts/formats.py` (add a function and a `FORMATS` entry). System rules live in their own module, like `scripts/gameboy.py`: tile addressing, map layout, sprite table, palettes, priority. Keep each system's rules in one module and cite the documentation you followed. Add tests in `tests/test_tilegfx.py` with known-answer data (hardware docs usually include worked examples).

## Tests

```
python -m unittest discover -s skills/tile-graphics/tests -v
```

## Credits

- Game Boy hardware rules follow **Pan Docs** (<https://gbdev.io/pandocs/>, CC0), including its worked tile example used in the tests.
- BizHawk's MIT-licensed **GB GPU Viewer** (`src/BizHawk.Client.EmuHawk/tools/GB/GBGPUView.cs`, © BizHawk team) was used to cross-check the decoding. No code was copied.
