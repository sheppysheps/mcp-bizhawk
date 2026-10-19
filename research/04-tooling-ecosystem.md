# Tooling ecosystem

What ran alongside mcp-bizhawk on the development machine (Windows 11), and
what each can and can't do for Game Boy research. All verified 2026-10-18/19.

## Ghidra 12.1.4 + GhidraBoy

- Ghidra 12.1.4 with a portable Temurin JDK 21 (Ghidra requires a 64-bit JDK 21+).
- **GhidraBoy** (Gekkio, archived upstream at Ghidra 11.4.2) was ported to
  Ghidra 12's loader API: `loadProgram` / `loadProgramInto` /
  `getDefaultOptions` now take an `ImporterSettings` object (or an extra
  `mirrorFsLayout` flag) instead of separate provider/options/log/monitor
  arguments, and the test task needs
  `-Djdk.serialFilterFactory=ghidra.framework.remote.GhidraSerialFilterFactory`.
  The port is a local patch (332/335 GhidraBoy tests pass; the 3 failures are
  textual decompiler differences with the same results). It installs like any
  extension (File → Install Extensions).
- Headless check: `analyzeHeadless.bat <proj> <name> -import shantae.gbc -noanalysis -deleteProject`
  → "Game Boy" loader, `SM83:LE:16:default`, ~18 s, 271 memory blocks (256 ROM banks + hardware).
- Gotcha: `!` and brackets in ROM filenames break `analyzeHeadless.bat`.
- Auto-analysis of Shantae found **only 4 functions**: calls go through a
  far-call trampoline with inline arguments, which Ghidra can't follow alone
  (see 05).

## GhidraMCP

- Python bridge via `uvx --from git+https://github.com/LaurieWired/GhidraMCP@1e980252… ghidra-mcp --ghidra-server http://127.0.0.1:8080/`,
  user-level MCP server `ghidra` (pinned commit; re-audit before bumping).
- Plugin 2.0 installed in Ghidra; needs CodeBrowser open with a program.
- Works: program info, segments (271), functions, decompile, xrefs.
- Missing: no "create function at address" tool, so resolving far-call
  targets needs a Ghidra script (Script Manager or headless).

## REA (rea-agents 6.7.0)

- User-level MCP server `rea`, env `GHIDRA_INSTALL_DIR` + `JAVA_HOME`.
  `rea doctor`: Ghidra provider fully healthy.
- **Can't open Game Boy ROMs**: `open_binary` rejects `.gbc` as
  "unsupported binary format" before Ghidra sees it; raw-binary mode allows
  only 6502/x86/x86_64/arm/arm64/m68k (no SM83); `REA_GHIDRA_LANGUAGE_ID`
  can't override the format gate.
- Useful for PE/ELF/Mach-O/JS targets (e.g. BizHawk's own binaries).

## Other

- **uv 0.13.1** (`~/.local/bin`), needed for GhidraMCP.
- **gh 2.102.0** (via Scoop; winget's MSI failed with exit 110 needing
  elevation). Logged in as sheppysheps (keyring, HTTPS).
- With only the VS Code extension installed, the `claude` CLI isn't on PATH;
  a copy ships inside the extension under `resources/native-binary/`.
- PowerShell **script files were blocked by execution policy** on the
  development machine (inline commands work; it wasn't bypassed).
- Python 3.13 is installed; Pillow is available but the tile-graphics skill
  doesn't need it.

## Division of labor (Game Boy)

| Need | Tool |
|---|---|
| Runtime state, RAM search, breakpoints, input, dumps | mcp-bizhawk |
| Static disassembly / decompilation, naming | Ghidra + GhidraBoy (GhidraMCP) |
| Tiles, maps, sprites, palettes | `skills/tile-graphics` |
| Non-GB binaries | REA |
