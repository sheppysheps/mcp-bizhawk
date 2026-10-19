# tile-graphics skill: evaluation, iteration 1

Run with Claude Code's skill-creator pipeline on 2026-10-19. Workspace (local,
gitignored): `skills/tile-graphics-workspace/` (inputs, `grade.py`,
`iteration-1/` with every run's outputs, grading, `benchmark.json`,
`review.html`). Test prompts: `skills/tile-graphics/evals/evals.json`.

## Method

- **4 test prompts**, each run by a fresh agent **with** the skill and one
  **without** (baseline), 8 runs in parallel, from pre-captured dumps only (no
  live emulator, so no shared-hub interference).
  1. `rip-title-graphics`: rip sprites + both VRAM banks in real colors.
  2. `verify-title-decode`: does the decode reproduce the screenshot? (answer: 94.56% → 100% with LCDC 0xE7)
  3. `black-frame-trap`: palettes + verify on a solid-black fade frame (answer: a 100% match proves nothing).
  4. `nes-chr-sheet`: render synthetic NES CHR (core-agnostic check).
- **Grading** (`grade.py`) is independent of the skill: its own PNG reader and
  GB/NES decoders written from the docs, BizHawk screenshots as ground truth,
  tile matching tolerant of layout/scale/palette. Validated first: a correct
  sheet scored 100%, wrong-format decodes 13.8% and 0%.
- Judgment checks were graded by reading each answer, with line evidence.

## Results

| | With skill | Without skill |
|---|---|---|
| Checks passed | 93% | 88% |
| Mean time | 77 s | 93 s |
| Mean tokens | 59k | 57k |

One run per configuration: indicative, not statistically solid.

**No false positives in the tool results.** The baseline agent wrote its own
decoder from scratch and independently got 94.557% → 100% with the same 1,254
mismatched pixels and the same LCDC cause. Every automatic check passed in
both configurations, which also means those checks don't discriminate: a
capable agent can write a GB decoder; the skill mostly saves time and
standardizes outputs.

## Failures found

1. **Misreading text in images (with skill, 2x).** "CGB ENHANCED!" for the
   actual **"GBA ENHANCED!"** (the maintainer confirmed), and "© 1994" for
   1996. The skill writes 1x images; the baseline agents made 2x/4x copies
   before describing them and read both correctly.
2. **Infeasible advice (without skill, 3x).** Told the user to read CGB
   palette RAM by writing `$FF68` and reading `$FF69`, which BizHawk ignores (see 01).
3. **Silent false confidence.** `gb-verify` prints "100% match" on a
   single-color frame. Both black-frame agents caught it on their own (one ran
   a wrong-palette negative control: 10.08%), but the tool doesn't warn.
4. **Repeated helper scripts.** Agents in both configurations wrote their own
   code to color tile-bank sheets with the palettes the map uses.

## Planned iteration 2

- `--scale N` on image outputs, and SKILL.md guidance: enlarge and zoom before
  naming any text or object; quote text only when it's legible.
- State in SKILL.md that CGB palette RAM isn't readable through BizHawk Lua,
  and list the routes that work (screenshot inference; palette-write
  breakpoints using register A).
- `gb-verify` / `gb-infer-palettes`: warn when the screenshot has very few
  distinct colors or few palette entries were recovered; run a negative
  control automatically.
- `tiles --colorize` using the map's palette per tile (or a chosen palette).
- Reword the LCDC case: likely OBJ disabled at vblank start, not "mid-frame".
- Rerun all 4 tests as iteration 2 against iteration 1; then description
  optimization, user-level install (junction to the repo), and notes to
  Shando/shantex.
