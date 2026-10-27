# Research notes

## Context

This repo is one prong of a **divide-and-conquer workflow for decompiling
Shantae (Game Boy Color)**. The work is split across separate projects so
each stays focused:

- **this repo (mcp-bizhawk fork):** general, core-agnostic emulator
  capabilities for coding agents: memory search, snapshots, breakpoints,
  dumps, sandboxed Lua, and reusable skills;
- **sibling projects** (Shando, shantex): the game-specific research, i.e.
  RAM and ROM catalogs, text and graphics extraction, and decompilation.

Shantae is simply the ROM chosen for testing these tools, out of convenience.
Nothing here is Shantae-specific: what testing found about the game went to
the game projects, and
[05-test-target-lessons.md](05-test-target-lessons.md) keeps only the
lessons that apply to any game.

These notes record what was learned while building the fork, for humans and
coding agents who want to pull or fork this work and build their own tools.
Every claim was **verified**, and each file says how. Where something is a
guess, it says so.

Collected 2026-10-18 to 2026-10-19 on BizHawk 2.11.1 (Windows, Gambatte core
for GB/GBC).

| File | What's in it |
|---|---|
| [01-bizhawk-runtime-facts.md](01-bizhawk-runtime-facts.md) | How BizHawk 2.11.1's Lua, NLua, sockets, memory domains, input and I/O actually behave, including the surprises |
| [02-architecture-and-decisions.md](02-architecture-and-decisions.md) | Why the fork is built the way it is: the hub, the sandbox, consent, file fencing, workflow rules |
| [03-security-review.md](03-security-review.md) | Threat model, the sandbox escape suite, security review findings, third-party tool audits |
| [04-tooling-ecosystem.md](04-tooling-ecosystem.md) | REA, Ghidra + GhidraBoy, GhidraMCP, uv, gh: what works, what doesn't, how they're configured |
| [05-test-target-lessons.md](05-test-target-lessons.md) | Game-agnostic lessons from testing the tools on a real game: in-frame register values, control runs, counter narrowing, far-call trampolines |
| [06-tile-graphics-skill-eval.md](06-tile-graphics-skill-eval.md) | skill-creator evaluation of the tile-graphics skill: method, results, failures, planned fixes |
| [07-open-questions-and-roadmap.md](07-open-questions-and-roadmap.md) | What's next, what's unresolved, and known tech debt |

Related living documents: [docs/FORK-GOALS.md](../docs/FORK-GOALS.md) (goals and
roadmap), [docs/TESTING.md](../docs/TESTING.md) (testing strategy),
[docs/RECIPES.md](../docs/RECIPES.md) (how to use the tools), [CHANGELOG.md](../CHANGELOG.md).
