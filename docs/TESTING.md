# Testing strategy

The current state: CI type-checks and builds on 3 operating systems × 3 Node versions, but **there are no automated tests**. Everything so far was verified by hand or by one-off scripts. This plan adds tests in layers, from fast and cheap to slow and realistic, and says what each layer is for.

```
Layer 4  Capability evals    agent + savestate + ground truth      before releases
Layer 3  BizHawk integration real EmuHawk over MCP stdio            local (Windows)
Layer 2  Lua unit            sandbox/json/handlers on stock Lua 5.4 CI (Ubuntu)
Layer 1  Node unit           launcher, protocol, tools, formatting  CI (all 9)
Layer 0  Static              tsc build (exists)                     CI (all 9)
```

## Layer 1: Node unit tests (CI, every push)

Use Node's built-in `node:test` runner. It needs no new dependencies, which keeps the supply chain small.

| Area | Example cases |
|---|---|
| `src/launcher.ts` | Rejects a `.lua` or `.state` file passed as the ROM, a missing file, a directory, or a ROM with no extension. Rejects `BIZHAWK_EXE` when it's unset, relative, or not named `EmuHawk.exe` (e.g. `cmd.exe`). `kill()` refuses when this server launched nothing. |
| `src/bizhawk.ts` protocol | Uses a fake bridge on a random local port: READY gets NONE back, a queued command is sent length-prefixed, RESULT resolves the right call, an error RESULT rejects it, timeouts reject cleanly, a disconnect resets `isBridgeReady()`, and a second client is refused. |
| `src/tools.ts` | Every tool in `TOOLS` has a `switch` case, and no case lacks a tool. Every description has PURPOSE / USAGE / BEHAVIOR / RETURNS. `describeEval` handles empty, array and nil-hole results. |

## Layer 2: Lua unit tests (CI, Ubuntu, `apt-get install lua5.4`)

`lua/sandbox.lua` and `lua/json.lua` only touch BizHawk through tables passed in to them, so they run on stock Lua 5.4 with stand-in host libraries.

- **Sandbox escape suite:** every blocked global (`os`, `io`, `luanet`, `load`, `require`, `debug`, `getmetatable`, `_G`, `forms`, memory writes, `gui.drawImage`) must be absent.
- **Limits:** infinite loops, a `pcall` that swallows the abort, `string.rep` called as a function and as a method, `__gc` finalizers, the memory ceiling, the 64 KB bulk-read cap, and planted `__tostring` / `__pairs` / `__name` traps.
- **Consent state machine:** stand-in `forms` lets the test click Accept, Cancel and the X. Accept starts grayed out, ticking the box alone isn't consent, unticking grays Accept again, tick plus Accept is consent, Disable withdraws it, the X is never consent, and every close starts the 2-minute cooldown.
- **json.lua:** a round-trip for nil holes, nested tables, control bytes and non-finite numbers.

All of this exists today as a 44-check script, verified passing inside BizHawk 2.11.1. Porting it is the first job.

**Caveat:** stock Lua isn't NLua. Anything specific to BizHawk's runtime belongs in Layer 3: userdata functions, `luanet` being present, and real `forms` behavior.

## Layer 3: BizHawk integration (local; skipped unless `BIZHAWK_EXE` and `TEST_ROM` are set)

An MCP stdio client launches real EmuHawk on a spare port and runs every tool round-trip:
- the launch, pause, frame-advance and close lifecycle (including closing while paused);
- a write breakpoint firing on a known write;
- savestate save and load giving identical RAM;
- the gate refusing a call and opening the dialog;
- sandbox checks that only hold on NLua.

**Fixture ROM.** Commercial ROMs can't go in the repo, and their RAM layouts aren't known ahead of time. So the plan is to build a tiny Game Boy test ROM with [RGBDS](https://rgbds.gbdev.io/) (an MIT-licensed assembler), with **known answers built in**:
- a frame counter at a fixed WRAM address, plus an HP value that only goes down when A is pressed (for workflow 1 and its controls);
- a two-level pointer chain ending in a known struct (pointer walking);
- a header, two ROM banks and a known data blob between code regions (workflow 2);
- dialog strings in a custom table encoding, each tagged with a speaker ID (workflow 3);
- a small pure routine, such as checksum or division, for differential testing (workflow 4).

The fixture ROM can be redistributed, so Layer 3 could later run in CI on Linux (BizHawk via Mono under `xvfb`). That's optional, and only worth it if it proves stable.

Shantae and other commercial ROMs stay manual, real-world spot checks.

## Layer 4: Capability evals (agent-level, before releases)

These test whether **an agent can actually do the target workflows** in [FORK-GOALS.md](FORK-GOALS.md). They're scored tasks with ground truth, not pass/fail unit tests.

| Workflow | Task | Scored on |
|---|---|---|
| 1. RAM search with controls | "Find the HP address" from a savestate | Correct address found; false positives removed by the no-action control; tool calls used |
| 2. ROM structure | "Map the banks and data regions" | Boundary accuracy against the fixture ROM's linker map |
| 3. Text extraction | "Dump all dialog by speaker" | Recall and precision of strings; speaker assignment accuracy |
| 4. Instruction replication | "Reimplement routine X" | Agreement over N random inputs (below) |
| 5. Live helpers | "Show a live HP tracker" | Runs without budget errors; screenshot shows the value; human sign-off |

**What "95% confidence" means for workflow 4.** Reaching it requires more than 95 matches out of 100. Give the emulator and the reimplementation the same N random input states (RAM writes followed by an exec breakpoint at the routine's entry and exit) and count matches. Then report the **Wilson score lower bound** of the agreement rate. "Replicated" means that lower bound is ≥ 0.95. At z = 1.96, that takes 73 out of 73, 109 out of 110, 140 out of 142, or 170 out of 173. 95 out of 100 only reaches 0.89.

## Rules

- **`main` must always build and work.** Run Layers 0 to 2 before every push once they exist.
- **Any change to `lua/sandbox.lua`, the gate, or `src/launcher.ts`** must add or update a Layer 2 case, and the consent flow gets a manual human check before merging.
- **Coverage targets:** every tool dispatched (100%), every whitelisted and every blocked sandbox name tested (100%), and every validation branch in the launcher. Line-coverage percentage isn't a goal in itself.

## Plan

1. `feat/tests`: set up the `node:test` runner, port the sandbox and gate script to Layer 2, and add a Lua job to CI.
2. `feat/fixture-rom`: build the RGBDS fixture ROM, and turn the scratchpad smoke scripts into the Layer 3 runner.
3. Add the capability gaps from FORK-GOALS.md (unknown-value search, per-frame scripts), each with tests.
4. Write the Layer 4 eval tasks against the fixture ROM, then spot-check them on real games.
