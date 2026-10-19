---
name: tile-graphics
description: Extract tile graphics, tile maps and sprites from a running game as PNG + JSON, using mcp-bizhawk memory dumps. Raw tile sheets for any tile-based system (Game Boy, NES, SNES, GBA, Genesis layouts); full Game Boy / Game Boy Color decoding (tile maps with CGB attributes, OAM sprites, palettes recovered from a screenshot) with a pixel-exact self-check against BizHawk's screenshot. Use when asked to rip, view, catalog or verify a game's tiles, sprites, backgrounds or palettes.
---

# tile-graphics

Turn emulator memory into images an agent or a human can inspect: tile sheets, whole tile maps, sprite sheets, the composed screen, and palettes, each with JSON describing exactly what was decoded. Everything runs on **Python 3.9+ standard library only** (no pip installs), so every line is auditable.

The skill only reads dump files. It never talks to the emulator itself: you capture state with mcp-bizhawk tools, then run `scripts/tilegfx.py` on the files.

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

## 2. Decode (scripts/tilegfx.py)

Run from `skills/tile-graphics/scripts/` (or give its full path). `G=<BIZHAWK_DUMP_DIR>/<game>`.

**Any system: raw tile sheet**

```
python tilegfx.py tiles $G/<tag>_vram.bin --format gb2bpp --count 384 --out $G/tiles.png
python tilegfx.py tiles $G/rom.bin --format nes2bpp --offset 0x8010 --count 512 --out $G/chr.png
```

| `--format` | Layout | Systems |
|---|---|---|
| `gb2bpp` | 2bpp, planes interleaved per row | Game Boy, GBC, SNES 2bpp |
| `nes2bpp` | 2bpp, plane 0 rows then plane 1 rows | NES / Famicom |
| `snes4bpp` | 4bpp, planes 0+1 then 2+3 | SNES 4bpp, PC Engine BG |
| `gba4bpp` / `gba8bpp` | linear, low nibble = left pixel | Game Boy Advance, DS |
| `genesis4bpp` | packed, high nibble = left pixel | Genesis / Mega Drive, Master System |

Raw sheets are grayscale by color index; ROM graphics may be compressed, in which case dump the decompressed copy from VRAM instead.

**Game Boy / Game Boy Color: full decoding**

Common inputs: `IN="--vram $G/<tag>_vram.bin --oam $G/<tag>_oam.bin --io $G/<tag>_io.bin"`. A 16 KiB VRAM dump means CGB mode (override with `--dmg`/`--cgb`).

```
# CGB only: recover palettes from the screenshot (DMG palettes come from BGP/OBP0/OBP1)
python tilegfx.py gb-infer-palettes $IN --screenshot $G/<tag>_shot.png --out $G/<tag>_pal.json

# Self-check: rebuild the screen and compare with the screenshot pixel by pixel
python tilegfx.py gb-verify $IN --screenshot $G/<tag>_shot.png --palettes $G/<tag>_pal.json --diff $G/<tag>_diff.png

# Outputs
python tilegfx.py gb-map     $IN --palettes $G/<tag>_pal.json --out $G/<tag>_map.png --json $G/<tag>_map.json
python tilegfx.py gb-sprites $IN --palettes $G/<tag>_pal.json --out $G/<tag>_sprites.png --json $G/<tag>_sprites.json
python tilegfx.py gb-screen  $IN --palettes $G/<tag>_pal.json --out $G/<tag>_screen.png
```

- `gb-map --map bg|win|9800|9c00`: the whole 256x256 map (more than the visible 160x144), plus per-cell JSON: tile index, CGB attribute byte, palette, BG-over-OBJ priority.
- `gb-sprites`: all 40 OAM objects in a 10-column sheet (8x8 or 8x16 per LCDC), plus JSON with screen position, tile, palette, bank, flips, priority and visibility. Grouping objects into a character ("metasprite") is game-specific: use the positions in the JSON.

## 3. Read the results

- **`gb-infer-palettes` conflicts** count screenshot pixels that disagreed with the majority color for their (palette, color index). **0 conflicts** means the decoding is consistent with the picture. Colors are recovered only for palette entries actually visible on screen; capture several scenes to fill more.
- **`gb-verify` match %**: 100% means the rebuilt screen is pixel-identical to BizHawk's. The `--diff` image shows mismatches in magenta.
- **Below 100%? Suspect mid-frame changes first.** Registers are dumped at frame end, but games change them while the screen draws (status bars, split scrolling, sprites switched on for part of the frame). Look at where the magenta is, then override with `--lcdc`, `--scx`, `--scy`, `--wx`, `--wy`. Example from Shantae's title screen: LCDC read `0xE5` (sprites off) at frame end, but the "Press Start" plate is made of sprites; `--lcdc 0xE7` gave a 100% match.
- Recovered colors are in **the screenshot's color space** (BizHawk's GBC color correction, if enabled), which is what the game looks like, not raw RGB555 hardware values.

## Extending to another system

Tile decoders live in `scripts/formats.py` (add a function and a `FORMATS` entry). System rules live in their own module, like `scripts/gameboy.py`: tile addressing, map layout, sprite table, palettes, priority. Keep each system's rules in one module and cite the documentation you followed. Add tests in `tests/test_tilegfx.py` with known-answer data (hardware docs usually include worked examples).

## Tests

```
python -m unittest discover -s skills/tile-graphics/tests -v
```

## Credits

- Game Boy hardware rules follow **Pan Docs** (<https://gbdev.io/pandocs/>, CC0), including its worked tile example used in the tests.
- BizHawk's MIT-licensed **GB GPU Viewer** (`src/BizHawk.Client.EmuHawk/tools/GB/GBGPUView.cs`, © BizHawk team) was used to cross-check the decoding. No code was copied.
