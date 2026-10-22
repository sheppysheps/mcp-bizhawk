# Open questions, roadmap and tech debt

## Roadmap status

The roadmap lives in [docs/FORK-GOALS.md](../docs/FORK-GOALS.md); steps 3-7 and the movie tools are planned in detail in [docs/plans/2026-10-21-research-tool-upgrades.md](../docs/plans/2026-10-21-research-tool-upgrades.md), whose status line is the current one. This table is a summary.

| Step | Status |
|---|---|
| 1. Snapshots + compare (unknown-value RAM search) | ✅ merged 7ddb820 |
| 2. Byte-pattern / text / relative search | ✅ merged a62a00a |
| 2½. Memory dumps to files | ✅ merged 7362af4 |
| Skill: tile-graphics | ✅ merged e848a73; iteration 2 pending (see 06) |
| Movie research (BizHawk 1.x recorder, `movie_export`, skill `movie-research`) | ✅ merged 0ce8429 and 8dbc21b (plan group A); next: labeling station (plan A9) |
| 3. Execution coverage map | planned (plan B7) |
| 4. Breakpoint upgrades | planned (plan B1-B6); also enables palette-write logging |
| 5. Persistent per-frame sandboxed scripts | planned; needs security review; not in the plan yet |
| 6. RGBDS fixture ROM + Layer 3 integration tests; patched-ROM scenario harness | planned (harness: plan B8) |
| 7. Catalog conventions (shared findings format) | planned (plan section C, to be agreed with the game repos) |

## Open questions

- **Exec-any cost**: how slow is `event.on_bus_exec_any` on Gambatte under BizHawk 2.x, and how many frames can a coverage run afford? Measure before building step 3. Answered for 1.11.7: execute callbacks register without error and never fire there, so the movie recorder logs registers on watched writes instead.
- **Bank attribution**: the coverage map must tag ROMX addresses with the bank; the callback's registers include `ROMX BANK`, which needs checking per hit.
- **Palette-write logging**: confirm register A holds the written value at the `ldh [$69],a` hit, and that reading `$FF68` via System Bus gives the index.

Game-specific questions (a test game's LCDC timing, dialogue control codes, hardware checks) belong to the game projects, not here; findings from test runs go to them as handoffs through the dev-master dashboard.

## Tech debt

- `frame_advance` > ~600 frames exceeds the 10 s call timeout; long advances need chunking or a longer per-call timeout.
- Only one game (Game Boy Color) has been used for live tests and movie captures; DMG and other systems are covered by unit tests only. Proving the tools on other consoles and games comes before contacting upstream.
- `package.json` still points `repository`/`bugs`/`homepage` at upstream; the npm name is upstream's (don't tag releases yet).
- BizHawk `config.ini` keeps stale recent-file paths from the old folder name (harmless).
- The hub has no auth token (deliberate, see 02).
- The movie recorder's output files can stay locked by BizHawk 1.11.7 after a pass ends with `stop_after` (seen once, cause unconfirmed; closing BizHawk releases them).
