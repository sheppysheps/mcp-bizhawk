# Lessons from testing the tools on a real game

The tools were tested against one commercial Game Boy Color game. What was
learned about that game went to the game projects; this file keeps only what
applies to researching any game with these tools. Collected 2026-10-18 to
2026-10-21 on BizHawk 2.11.1 (Gambatte core).

| Lesson | How it showed up |
|---|---|
| **A register read at frame end can differ from its value during rendering.** Decode the screen with the in-frame value, and check the decode against BizHawk's screenshot. | The LCD control register read "sprites off" at frame end while 34 sprites were on screen: the game turns sprites off at the start of vblank while it copies sprite data. Decoding with sprites on matched the screenshot pixel for pixel. |
| **Find per-frame counters by snapshot narrowing.** Take several snapshots a known number of frames apart and keep the addresses that changed by exactly that many. | Three WRAM counters (+1 per frame) dropped out after a few rounds. They are noise to filter out of later diffs, not game state. |
| **Compare against a control run, not just "before".** Load the same savestate twice, do the action in one run and nothing in the other, and diff the two results. | Pressing Start on a title screen changed thousands of WRAM bytes; most of them change on their own each frame, which only the control run reveals. |
| **Try plain ASCII before building a text table.** A relative search (letter differences, not values) finds the encoding without guessing a table. | Dialogue turned out to be ASCII; a bracket plus a digit marked control codes inside the strings. |
| **Inline-argument far calls hide targets from a disassembler.** A call to a trampoline followed by `dw address, db bank` looks like a call to one function. Count the byte pattern's sites and resolve the inline arguments before trusting a call graph. | Thousands of sites used one trampoline. A second routine nearby (a far jump that also resets the stack pointer) was first mistaken for the trampoline; check what each does to SP. |
| **VRAM holds more than the screen shows.** Dump VRAM and decode every tile bank, not only the tiles the map uses. | A loaded but unused badge graphic in VRAM pointed to a hardware check the game makes. |
| **Fades zero the palettes.** Don't sample palettes or colorize tiles on a fade frame; pick a frame where the palette RAM is not all black. | A splash screen's fade frame had every palette black. |
| **Hash the ROM the same way in every tool.** Note which hash each tool reports (BizHawk shows SHA-1, Ghidra SHA-256) and confirm a dump against both before comparing addresses across tools. | Ghidra's hash and the ROM dumped through `bizhawk_dump_memory` agreed, so addresses could be shared between them. |
