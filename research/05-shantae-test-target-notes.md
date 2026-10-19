# Shantae (USA, GBC): observations from tool testing

Shantae was the convenient test ROM for this repo's tools. These observations
came up while testing, not from a deliberate study of the game. They're
**leads for the game-specific projects** in the decompilation effort, where
game research belongs. Verify before relying on them.

ROM: `shantae.gbc`, 4 MiB, SHA-256
`1b92e22d5510c51bab97d23074e4aad7464d93eb15f7596ef7da0a5efa27a19d` (Ghidra's
hash and `bizhawk_dump_memory`'s ROM dump agree). BizHawk ROM hash (SHA-1)
`520E48C50F6E997FCD841CA368FC9ABC1DBDDEC1`.

## Code

- **Far call: `CALL $0540`**, followed inline by `dw address, db bank`; the
  current bank is kept in HRAM `$FF91`. 3,141 `CD 40 05` sites in the ROM, 37
  in bank 0. Example: `CD 40 05 39 41 01` = call `$4139` in bank 1.
- `$056B` = `farjump_reset_sp` (far jump after `LD SP,$CFFF`, scene change).
  I first misidentified it as the trampoline.
- Reference: vibecodekun/shantaerecomp's `shantae.annotations` names 16,569
  functions (hand-curated core + byte-scan hints). **PolyForm Noncommercial**:
  reference only, don't copy into MIT repos.

## Text

- **Dialogue is plain ASCII** (relative search for "SHANTAE" inferred `A = 0x41`).
  77 hits: header title at `0x0134`, one at `0x1002A`, dialogue from about
  `0xA05CA` (bank `0x28`). Lines look like `SHANTAE): [1So y...`; `[0`..`[4`
  are probably portrait/expression or speaker codes (unconfirmed).

## RAM

- Per-frame counters in WRAM at offsets `0x0377`, `0x037D`, `0x7709` (each +1
  per frame; found by snapshot narrowing).
- From the Shando session: health `$CA80` (half-hearts), map word `$C9F9`.

## Video

- Title screen (frame ~1500): LCDC reads `0xE5` at frame end (sprites off),
  but 34 sprites are visible; with LCDC `0xE7` the decoded screen matched
  BizHawk's screenshot 100%. The game likely disables OBJ at the start of
  vblank while copying sprite data (DMA register `$D8` points at `$D800`).
- 8x16 sprites; BG uses signed tile addressing; 17 BG and 7 OBJ colors
  visible on the title screen.
- VRAM bank 0 holds a **"GBA ENHANCED!"** badge (loaded, not on the map).
  Shantae unlocks extras on a Game Boy Advance, so this is a lead toward the
  GBA check (HRAM `$FFFE` = 0 DMG / 1 GBC / 3 GBA per shantaerecomp).
- Frame ~620 is a black fade on the WayForward splash ("© 1996-2002 Matt
  Bozon"); palettes there are all black.
- Pressing Start on the title screen changes 3,368 WRAM bytes versus an idle
  control run from the same savestate.
