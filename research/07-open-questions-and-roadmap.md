# Open questions, roadmap and tech debt

## Roadmap status (docs/FORK-GOALS.md)

| Step | Status |
|---|---|
| 1. Snapshots + compare (unknown-value RAM search) | ✅ merged 7ddb820 |
| 2. Byte-pattern / text / relative search | ✅ merged a62a00a |
| 2½. Memory dumps to files | ✅ merged 7362af4 |
| Skill: tile-graphics | ✅ merged e848a73; iteration 2 pending (see 06) |
| 3. Execution coverage map (`on_bus_exec_any`, by bank) | next |
| 4. Breakpoint upgrades: range capture, exit capture, `set_register` | planned; also enables palette-write logging |
| 5. Persistent per-frame sandboxed scripts | planned; needs security review |
| 6. RGBDS fixture ROM + Layer 3 integration tests | planned |
| 7. Catalog conventions (shared findings format) | planned (doc) |

## Open questions

- **Exec-any cost**: how slow is `event.on_bus_exec_any` on Gambatte, and how
  many frames can a coverage run afford? Measure before designing step 3.
- **Bank attribution**: the coverage map must tag ROMX addresses with the
  bank; the callback's registers include `ROMX BANK`, which needs checking per hit.
- **Palette-write logging**: confirm register A holds the written value at the
  `ldh [$69],a` hit, and that reading `$FF68` via System Bus gives the index.
- **Shantae LCDC at vblank**: is OBJ really toggled at vblank start? Verify
  with a write breakpoint on `$FF40`.
- **`[n` dialogue codes**: portrait vs speaker vs expression (Shando).
- **GBA mode**: where is the GBA check, and what does "GBA ENHANCED!" unlock (Shando).

## Tech debt

- `frame_advance` > ~600 frames exceeds the 10 s call timeout; long advances
  need chunking or a longer per-call timeout.
- Only one game (Shantae, GBC) has been used for live tests; DMG and other
  systems are covered by unit tests only.
- `package.json` still points `repository`/`bugs`/`homepage` at upstream;
  the npm name is upstream's (don't tag releases yet).
- No `upstream` git remote set up yet for pulling dmang-dev changes.
- BizHawk `config.ini` keeps stale recent-file paths from the old folder name
  (harmless).
- The hub has no auth token (deliberate, see 02).
