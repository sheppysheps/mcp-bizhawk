import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
  Tool,
} from "@modelcontextprotocol/sdk/types.js";
import type { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { BizhawkServer } from "./bizhawk.js";
import { BizhawkLauncher } from "./launcher.js";

const VALID_BUTTONS = [
  "A", "B", "X", "Y",                 // Face buttons
  "Up", "Down", "Left", "Right",      // D-pad
  "Start", "Select",                  // System
  "L", "R", "L1", "R1", "L2", "R2", "L3", "R3", // Shoulders/triggers/sticks
  "C", "Z", "C-Up", "C-Down", "C-Left", "C-Right",  // N64
  "Mode",                             // Genesis 6-button
];

// ──────────────────────────────────────────────────────────────────────────────
// Tool descriptions are written to the TDQS rubric (Glama's Tool Definition
// Quality Score). Each description covers, in order:
//
//   • PURPOSE — one clear action sentence.
//   • USAGE — when to use this vs sibling tools (read8 vs read16 vs read_range,
//     write* vs load_state, frame_advance vs unpause, etc.).
//   • BEHAVIOR — side effects, error conditions, destructive notes. Reads say
//     "no side effects — pure read." Writes say "DESTRUCTIVE: overwrites".
//     Every tool documents the failure mode it can return (unknown domain,
//     out-of-range address, oversize range, missing capability, etc.).
//   • RETURNS — exact shape of the success output.
//
// Each parameter has a `description` that adds context the schema can't
// (interactions, examples, units, alignment requirements).
// ──────────────────────────────────────────────────────────────────────────────

const DOMAIN_PARAM_DESC =
  "Optional case-sensitive memory domain name. Omit to use BizHawk's currently selected domain " +
  "(see bizhawk_get_info → current_memory_domain). Discover available names with " +
  "bizhawk_list_memory_domains; they vary per system (WRAM on SNES, RAM on NES, RDRAM on N64, " +
  "68K RAM on Genesis, MainRAM on PSX, EWRAM/IWRAM on GBA). Returns an error if the name doesn't " +
  "match any domain on the loaded core.";

const ADDRESS_PARAM_DESC = (widthBytes: number) =>
  `Byte offset within the chosen memory domain. Per-domain offsets are 0-based and INDEPENDENT ` +
  `of system bus addresses (e.g. SNES WRAM uses 0x09C6, NOT 0x7E09C6). Reads ${widthBytes} ` +
  `consecutive byte${widthBytes === 1 ? "" : "s"} starting here. Returns an error if address < 0 ` +
  `or address + ${widthBytes} exceeds the domain's size.`;

export const TOOLS: Tool[] = [
  // ── Connectivity & introspection ────────────────────────────────────────

  {
    name: "bizhawk_ping",
    description:
      "PURPOSE: Verify that the BizHawk Lua bridge is connected and responding to RPC over the TCP socket. " +
      "USAGE: Call this once at start-of-session before issuing other tool calls; if it succeeds, every other tool will work. " +
      "BEHAVIOR: No side effects — pure liveness probe. Times out after ~10 seconds with a clear error if BizHawk isn't running, isn't pointed at the right host:port, or hasn't loaded lua/bridge.lua via Tools → Lua Console. " +
      "RETURNS: The literal string 'pong' on success.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "bizhawk_get_info",
    description:
      "PURPOSE: Get the loaded ROM's name and hash, current frame count, the list of available memory domains, the active default domain (the one used when 'domain' is omitted on read/write tool calls), and the bridge's capability map (which optional emu/client/savestate/joypad/memory methods this BizHawk build exposes). " +
      "USAGE: Call after bizhawk_ping to learn what system is loaded and which optional features are available; before any memory tool call to confirm the active domain and avoid silent reads from the wrong address space; before pause / unpause / reset / screenshot / save_state to check the corresponding `capabilities.*` flag. " +
      "BEHAVIOR: No side effects — pure read of emulator metadata. Returns 'unavailable' for fields the loaded core doesn't expose (rom_name when no ROM is loaded, framecount on cores without emu.framecount, etc.). " +
      "RETURNS: Multi-line text with ROM, ROM hash, framecount, memory_domains list, active domain, and a list of any missing capabilities for this build.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "bizhawk_list_memory_domains",
    description:
      "PURPOSE: List the memory domains available on the loaded core (e.g. 'WRAM', 'CARTRAM', 'VRAM', 'System Bus' on SNES; 'RAM', 'PPU', 'OAM' on NES). " +
      "USAGE: Call before any memory r/w tool when you don't know the domain layout for the loaded system. The returned names are exactly what to pass as the `domain` parameter on bizhawk_read*/write* tools (case-sensitive). " +
      "BEHAVIOR: No side effects — pure read. Returns an error if the loaded BizHawk core doesn't implement memory.getmemorydomainlist (extremely rare). " +
      "RETURNS: Newline-formatted list of domain names, one per line.",
    inputSchema: { type: "object", properties: {} },
  },

  // ── Memory reads ────────────────────────────────────────────────────────

  {
    name: "bizhawk_read8",
    description:
      "PURPOSE: Read an unsigned 8-bit byte from emulator memory at the given address. " +
      "USAGE: Use for single-byte status flags, counters, and 8-bit fields. For 16- or 32-bit values use bizhawk_read16/read32 (one call instead of multi-byte assembly); for spans of more than ~4 bytes use bizhawk_read_range (one round-trip instead of N frame-latency hops). " +
      "BEHAVIOR: No side effects — pure read. Reads work the same way whether emulation is paused or running. Returns an error if the named domain doesn't exist, the address is out of range for the domain, or the loaded core doesn't expose memory.read_u8. " +
      "RETURNS: Single line 'ADDR_HEX: VAL_DEC (0xVAL_HEX)', e.g. '0x09C6: 99 (0x63)'.",
    inputSchema: {
      type: "object",
      required: ["address"],
      properties: {
        address: { type: "integer", minimum: 0, description: ADDRESS_PARAM_DESC(1) },
        domain:  { type: "string", description: DOMAIN_PARAM_DESC },
      },
      additionalProperties: false,
    },
  },
  {
    name: "bizhawk_read16",
    description:
      "PURPOSE: Read an unsigned 16-bit little-endian value from emulator memory at the given address. " +
      "USAGE: Use for 16-bit fields (most game-state values: HP, score, coordinates). For single bytes use bizhawk_read8; for 32-bit values use bizhawk_read32; for non-aligned spans or big-endian fields use bizhawk_read_range and decode the bytes yourself (this tool always interprets bytes as little-endian regardless of the target system's native endianness). " +
      "BEHAVIOR: No side effects — pure read. Reads two consecutive bytes (low byte at `address`, high byte at `address+1`) and combines them as little-endian. Returns an error if the named domain doesn't exist, address+2 exceeds domain size, or the core doesn't expose memory.read_u16_le. " +
      "RETURNS: Single line 'ADDR_HEX: VAL_DEC (0xVAL_HEX)'.",
    inputSchema: {
      type: "object",
      required: ["address"],
      properties: {
        address: { type: "integer", minimum: 0, description: ADDRESS_PARAM_DESC(2) },
        domain:  { type: "string", description: DOMAIN_PARAM_DESC },
      },
      additionalProperties: false,
    },
  },
  {
    name: "bizhawk_read32",
    description:
      "PURPOSE: Read an unsigned 32-bit little-endian value from emulator memory at the given address. " +
      "USAGE: Use for 32-bit fields (timestamps, large counters, pointers on 32-bit systems, RGBA colors). For 8/16-bit reads use bizhawk_read8/read16; for big-endian or unaligned multi-word reads use bizhawk_read_range and decode yourself. " +
      "BEHAVIOR: No side effects — pure read. Reads four consecutive bytes starting at `address` and combines them as little-endian (LSB at `address`, MSB at `address+3`). Returns an error if the domain doesn't exist, address+4 exceeds the domain, or the core lacks memory.read_u32_le. " +
      "RETURNS: Single line 'ADDR_HEX: VAL_DEC (0xVAL_HEX)'.",
    inputSchema: {
      type: "object",
      required: ["address"],
      properties: {
        address: { type: "integer", minimum: 0, description: ADDRESS_PARAM_DESC(4) },
        domain:  { type: "string", description: DOMAIN_PARAM_DESC },
      },
      additionalProperties: false,
    },
  },
  {
    name: "bizhawk_read_range",
    description:
      "PURPOSE: Read a contiguous range of bytes from emulator memory as a hex dump. " +
      "USAGE: Use for >4 bytes (one round-trip vs N frame-latency hops). Max 4096 bytes/call (BizHawk serialization limit); chunk larger reads in 4 KiB. Powers the two-snapshot RAM-hunt workflow (snapshot before/after a known change, diff for matching deltas). " +
      "BEHAVIOR: No side effects — pure read. Returns an error if domain is unknown, length is out of 1-4096, or address+length exceeds the domain. " +
      "RETURNS: 'ADDR_HEX [N bytes, DOMAIN]:' header + space-separated 2-digit uppercase hex bytes.",
    inputSchema: {
      type: "object",
      required: ["address", "length"],
      properties: {
        address: { type: "integer", minimum: 0, description: "Starting byte offset within the chosen memory domain (0-based per-domain, NOT a system-bus address). Reads [address, address+length)." },
        length:  { type: "integer", minimum: 1, maximum: 4096, description: "Number of bytes to read (1-4096; hard cap is BizHawk's per-call serialization limit)." },
        domain:  { type: "string", description: DOMAIN_PARAM_DESC },
      },
      additionalProperties: false,
    },
  },

  // ── Memory writes ───────────────────────────────────────────────────────

  {
    name: "bizhawk_write8",
    description:
      "PURPOSE: Write a single unsigned byte (0-255) to emulator memory at the given address. " +
      "USAGE: Use for single-byte cheats, debug pokes, and game-state mutations (give a player N lives, unlock a flag, set a counter). For 16/32-bit values prefer bizhawk_write16/write32 (single call instead of byte-at-a-time); for spans use bizhawk_write_range. To seed cart save RAM realistically (with proper MBC behavior), prefer bizhawk_load_state with a pre-prepared .State file rather than poking SRAM bytes here. " +
      "BEHAVIOR: DESTRUCTIVE: overwrites whatever was at `address` with no undo (snapshot via bizhawk_save_state first if you need rollback). The write is direct memory access — bypasses MBC bank switches, cartridge mapper side-effects, and DMA semantics — so it cannot be used to emulate cartridge hardware. Returns an error if the domain is unknown, address is out of range, value < 0 or > 255, or the core lacks memory.write_u8. Works whether emulation is paused or running. " +
      "RETURNS: Single line 'Wrote VAL_DEC (0xVAL_HEX) → ADDR_HEX (DOMAIN)'.",
    inputSchema: {
      type: "object",
      required: ["address", "value"],
      properties: {
        address: { type: "integer", minimum: 0, description: ADDRESS_PARAM_DESC(1) },
        value:   { type: "integer", minimum: 0, maximum: 255, description: "Byte value to write. Must be 0-255 (0x00-0xFF). Values outside this range return an error." },
        domain:  { type: "string", description: DOMAIN_PARAM_DESC },
      },
      additionalProperties: false,
    },
  },
  {
    name: "bizhawk_write16",
    description:
      "PURPOSE: Write an unsigned 16-bit little-endian value to emulator memory at the given address. " +
      "USAGE: Use for 16-bit cheats and pokes (HP, score, coordinates). For single bytes use bizhawk_write8; for 32-bit use bizhawk_write32; for big-endian fields, byteswap and use bizhawk_write_range; for cart save RAM seeding, use bizhawk_load_state. " +
      "BEHAVIOR: DESTRUCTIVE: overwrites two bytes (low byte at `address`, high byte at `address+1`) with no undo. Direct memory write — no MBC/mapper/DMA mediation, see bizhawk_write8 notes. Returns an error if the domain is unknown, address+2 exceeds the domain, value < 0 or > 65535, or the core lacks memory.write_u16_le. " +
      "RETURNS: Single line 'Wrote VAL_DEC (0xVAL_HEX) → ADDR_HEX (DOMAIN)'.",
    inputSchema: {
      type: "object",
      required: ["address", "value"],
      properties: {
        address: { type: "integer", minimum: 0, description: ADDRESS_PARAM_DESC(2) },
        value:   { type: "integer", minimum: 0, maximum: 65535, description: "16-bit value to write. Must be 0-65535 (0x0000-0xFFFF). LSB is written to `address`, MSB to `address+1`. Values outside this range return an error." },
        domain:  { type: "string", description: DOMAIN_PARAM_DESC },
      },
      additionalProperties: false,
    },
  },
  {
    name: "bizhawk_write32",
    description:
      "PURPOSE: Write an unsigned 32-bit little-endian value to emulator memory at the given address. " +
      "USAGE: Use for 32-bit cheats and pokes (timestamps, large counters, pointers on 32-bit systems). For 8/16-bit values use bizhawk_write8/write16; for big-endian layouts byteswap and use bizhawk_write_range. " +
      "BEHAVIOR: DESTRUCTIVE: overwrites four bytes starting at `address` with no undo (snapshot via bizhawk_save_state first if you need rollback). Direct memory write — bypasses MBC/mapper/DMA, see bizhawk_write8 notes. Returns an error if the domain is unknown, address+4 exceeds the domain, value < 0 or > 4294967295, or the core lacks memory.write_u32_le. " +
      "RETURNS: Single line 'Wrote VAL_DEC (0xVAL_HEX) → ADDR_HEX (DOMAIN)'.",
    inputSchema: {
      type: "object",
      required: ["address", "value"],
      properties: {
        address: { type: "integer", minimum: 0, description: ADDRESS_PARAM_DESC(4) },
        value:   { type: "integer", minimum: 0, maximum: 4294967295, description: "32-bit value to write. Must be 0-4294967295 (0x00000000-0xFFFFFFFF). LSB lands at `address`, MSB at `address+3`. Values outside this range return an error." },
        domain:  { type: "string", description: DOMAIN_PARAM_DESC },
      },
      additionalProperties: false,
    },
  },
  {
    name: "bizhawk_write_range",
    description:
      "PURPOSE: Write a contiguous byte sequence to emulator memory starting at the given address. " +
      "USAGE: Use whenever you're seeding more than ~4 bytes — one round-trip vs N frame-latency hops compared to looping bizhawk_write8. Maximum 4096 bytes per call (BizHawk serialization limit); for larger writes, batch in 4 KiB chunks. Useful for installing cheat tables, patching code blocks, restoring a captured byte window after experiments, and writing big-endian multi-byte values (byteswap them yourself first). For cart save RAM seeding with proper MBC semantics, use bizhawk_load_state instead. " +
      "BEHAVIOR: DESTRUCTIVE: overwrites N bytes starting at `address` with no undo. Direct memory write — bypasses MBC/mapper/DMA, see bizhawk_write8 notes. Bytes are written sequentially address, address+1, ..., address+N-1. Returns an error if the domain is unknown, address+N exceeds the domain, the array contains a value outside 0-255, or the array length is < 1 or > 4096. " +
      "RETURNS: Single line 'Wrote N bytes → ADDR_HEX (DOMAIN)'.",
    inputSchema: {
      type: "object",
      required: ["address", "bytes"],
      properties: {
        address: { type: "integer", minimum: 0, description: "Starting byte offset within the chosen memory domain. The N bytes [address, address+len) are written." },
        bytes: {
          type: "array",
          items: { type: "integer", minimum: 0, maximum: 255 },
          minItems: 1,
          maxItems: 4096,
          description: "Byte values to write, one per element (each 0-255). Length 1-4096 (hard caps from BizHawk's serialization limit). Written sequentially from `address`.",
        },
        domain:  { type: "string", description: DOMAIN_PARAM_DESC },
      },
      additionalProperties: false,
    },
  },

  // ── Memory search ─────────────────────────────────────────────────────────

  {
    name: "bizhawk_search_memory",
    description:
      "PURPOSE: Find every address in a memory domain whose value equals a target — a Cheat-Engine-style value scan with iterative narrowing. " +
      "USAGE: Two modes. FIRST scan — omit `addresses`: sweeps the whole domain (or the [start, start+length) window you give) and returns matching offsets. NEXT scan — pass `addresses` (the offsets a prior scan returned): keeps only those that STILL equal `value`, the classic 'the value changed to X — which of my candidates match now?' narrowing. Typical RAM hunt: search a known value (lives=3) → play until it changes → search the new value passing the prior addresses → repeat until a handful remain, then poke them with bizhawk_write*. Pick `width` to match the field size; leave `aligned` true (steps by the width — fast and standard) unless you suspect an unaligned value. " +
      "BEHAVIOR: No side effects — pure read. A FIRST scan runs synchronously inside one bridge tick and briefly stalls the emulator: it bulk-reads the window in one engine call where the build supports it (cap 16 MiB per scan) and falls back to a byte-by-byte read otherwise (cap 256 KiB per scan — chunk larger windows with start+length). Results are capped at `max_results` (default 200); when more matched, `count` reports the true total and the result is flagged truncated — narrow with a more specific value or range. A NEXT scan is exact over whatever addresses you pass. Returns an error on unknown domain, a value out of range for the width, or a scan window over the cap. " +
      "RETURNS: A summary line (match count, value, width, bytes scanned) followed by the matching offsets as per-domain hex (0-based — the same offsets the read/write tools take).",
    inputSchema: {
      type: "object",
      required: ["value"],
      properties: {
        value: { type: "integer", minimum: 0, maximum: 4294967295, description: "Target value to find. Must fit the chosen width (0-255 for u8, 0-65535 for u16, 0-4294967295 for u32)." },
        width: { type: "string", enum: ["u8", "u16", "u32"], default: "u16", description: "Value width. u16/u32 are little-endian (BizHawk default). Match the field's real size — a u8 search won't find a 16-bit counter." },
        addresses: {
          type: "array",
          items: { type: "integer", minimum: 0 },
          description: "NEXT-scan candidate offsets (from a prior scan's output). When present, only these are re-read and filtered; `start`/`length`/`aligned` are ignored.",
        },
        start: { type: "integer", minimum: 0, default: 0, description: "FIRST-scan window start (0-based domain offset). Default 0." },
        length: { type: "integer", minimum: 1, description: "FIRST-scan window length in bytes. Omit to scan to the end of the domain (needs memory.getmemorydomainsize on the build)." },
        aligned: { type: "boolean", default: true, description: "FIRST-scan stride: true steps by the width (aligned — fast, standard for game values); false steps by 1 byte (catches unaligned values, slower)." },
        domain: { type: "string", description: DOMAIN_PARAM_DESC },
        max_results: { type: "integer", minimum: 1, maximum: 5000, default: 200, description: "Max offsets to return (1-5000, default 200). `count` always reports the true match total even when the returned list is truncated." },
      },
      additionalProperties: false,
    },
  },

  // ── Memory snapshots (unknown-value search) ───────────────────────────

  {
    name: "bizhawk_snapshot_memory",
    description:
      "PURPOSE: Save a named copy of a memory region inside BizHawk, for finding values you don't know yet (\"which byte is HP?\") with bizhawk_compare_snapshots. " +
      "USAGE: Snapshot, make something happen (frame_advance, play_input_sequence, a hit), snapshot again under another name, then compare. For a CONTROLLED test, load the same savestate twice and replay the same input with and without the action, snapshotting both at the same frame: comparing those two snapshots with op 'changed' leaves only addresses the action affected, ruling out timers and counters that change anyway. Use bizhawk_search_memory instead when you already know the value. " +
      "BEHAVIOR: No side effects on the game — pure read. Runs inside one bridge tick, so large regions briefly stall the emulator. The snapshot stays in the bridge (it survives load_state) until deleted or the bridge restarts; only a summary comes back. Reusing a name replaces that snapshot. Limits: 16 MiB per snapshot (256 KiB on builds without a bulk reader), 64 MiB for all snapshots together. Returns an error for a range past the end of the domain, an unknown domain, or a full store (delete old snapshots with bizhawk_delete_snapshots). " +
      "RETURNS: One line with the name, domain, range, frame number and store usage.",
    inputSchema: {
      type: "object",
      required: ["name"],
      properties: {
        name:   { type: "string", minLength: 1, description: "Label for this snapshot, e.g. 'hp_before' or 'control_f120'. Reusing a name replaces it." },
        domain: { type: "string", description: DOMAIN_PARAM_DESC },
        start:  { type: "integer", minimum: 0, default: 0, description: "First byte offset within the domain (default 0)." },
        length: { type: "integer", minimum: 1, description: "Bytes to copy. Omit to copy from `start` to the end of the domain. Both snapshots in a comparison must use the same domain, start and length." },
      },
      additionalProperties: false,
    },
  },
  {
    name: "bizhawk_compare_snapshots",
    description:
      "PURPOSE: Find addresses whose value changed (or didn't) between two snapshots, the core of an unknown-value RAM search. " +
      "USAGE: Compare 'before' and 'after' with an op: changed, unchanged, increased, decreased, changed_by (after − before == value), or equal_to (after == value). Pass save_as to keep the matching addresses as a candidate set, then narrow it on later comparisons with candidates=<that set>, exactly like repeated scans in BizHawk's RAM Search. Typical loop: take damage → compare 'decreased' save_as=hp; heal → compare with candidates=hp 'increased' save_as=hp; repeat until a few remain, then confirm with bizhawk_read8/write8. " +
      "BEHAVIOR: No side effects — pure comparison inside the bridge; memory isn't re-read. Both snapshots must cover the same domain, start and length. Values are decoded at `width` (u8/u16/u32 or signed s8/s16/s32), little-endian unless big_endian is true; with aligned (default true) multi-byte values are checked only at multiples of their width from the snapshot start. Lists at most max_results matches (with before/after values); `count` is always the true total. A candidate set holds up to 1,000,000 addresses. Returns an error for unknown names, mismatched snapshots, a candidate set from another domain, or a missing `value` for changed_by/equal_to. " +
      "RETURNS: A summary line (matches, addresses checked, saved set), then one line per listed match: address, before → after.",
    inputSchema: {
      type: "object",
      required: ["before", "after", "op"],
      properties: {
        before:      { type: "string", description: "Name of the earlier snapshot." },
        after:       { type: "string", description: "Name of the later snapshot (or, for a controlled test, the run with the action)." },
        op:          { type: "string", enum: ["changed", "unchanged", "increased", "decreased", "changed_by", "equal_to"], description: "Which addresses to keep." },
        value:       { type: "integer", description: "For changed_by: the exact difference (after − before), e.g. -1 for 'went down by one'. For equal_to: the value 'after' must hold." },
        width:       { type: "string", enum: ["u8", "u16", "u32", "s8", "s16", "s32"], default: "u8", description: "How many bytes make one value and whether it's signed (signed matters for increased/decreased across 0)." },
        big_endian:  { type: "boolean", default: false, description: "Decode multi-byte values big-endian (e.g. Genesis 68K RAM, N64)." },
        aligned:     { type: "boolean", default: true, description: "Check multi-byte values only at multiples of their width. Set false to check every byte offset." },
        candidates:  { type: "string", description: "Only check the addresses in this saved candidate set (from an earlier save_as)." },
        save_as:     { type: "string", description: "Save the matching addresses as a candidate set with this name (replaces any set with the same name)." },
        max_results: { type: "integer", minimum: 1, maximum: 5000, default: 200, description: "How many matches to list (count is always the full total)." },
      },
      additionalProperties: false,
    },
  },
  {
    name: "bizhawk_list_snapshots",
    description:
      "PURPOSE: List the snapshots and candidate sets currently stored in the bridge. " +
      "USAGE: Check what's available before comparing, or see how full the 64 MiB store is. " +
      "BEHAVIOR: No side effects — pure read of the bridge's own state. Everything listed disappears when the bridge script restarts (e.g. BizHawk closes). " +
      "RETURNS: One line per snapshot (name, domain, range, frame) and per candidate set (name, domain, width, address count), plus store usage.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "bizhawk_delete_snapshots",
    description:
      "PURPOSE: Free memory by deleting a snapshot or candidate set, or everything. " +
      "USAGE: Delete snapshots you no longer need when the store fills up, or clear everything between investigations. " +
      "BEHAVIOR: DESTRUCTIVE to the bridge's stored snapshots only (never touches game memory). With `name`, deletes the snapshot and/or candidate set with that name; without it, deletes everything. Deleting an unknown name is not an error (it reports 0). " +
      "RETURNS: Single line with how many entries were deleted.",
    inputSchema: {
      type: "object",
      properties: {
        name: { type: "string", description: "Snapshot or candidate set to delete. Omit to delete everything." },
      },
      additionalProperties: false,
    },
  },

  // ── Input ───────────────────────────────────────────────────────────────

  {
    name: "bizhawk_press_buttons",
    description:
      "PURPOSE: Set the joypad button state for one player for EXACTLY the next emulated frame. " +
      "USAGE: Drive games with input. Each call sets joypad state for ONE frame only — the very next frame BizHawk processes. After that frame, BizHawk's input goes back to whatever the human user is holding (typically nothing). " +
      "To HOLD a button across N consecutive frames, INTERLEAVE: call bizhawk_press_buttons + bizhawk_frame_advance(count=1) N times in a loop. " +
      "DO NOT call bizhawk_press_buttons once and then bizhawk_frame_advance(count=N) — only the first of those N frames sees the button; the rest are no-input. Verified empirically against SNES Super Metroid in May 2026: a 60-frame advance after a single press_buttons(Right) moved Samus the same +1 pixel as a 10-frame advance, because frames 2-60 had no input. " +
      "To release a button mid-hold, just stop calling press_buttons for it; the next frame_advance will see it released. " +
      "BEHAVIOR: Modifies emulator input state for the next frame poll only — no other side effects. Returns an error if the loaded core doesn't expose joypad.set. Button names that aren't valid for the active core are silently ignored by BizHawk (no error). " +
      "RETURNS: Single line 'Set joypad N: BUTTON+BUTTON+...' or '... (all released)' if nothing was pressed. " +
      `\n\nButton names vary per system. Common names across cores: ${VALID_BUTTONS.join(", ")}. Use whatever names the active core understands — if unsure, try a name and check BizHawk's input display, or use bizhawk_get_info to confirm joypad_set is available.`,
    inputSchema: {
      type: "object",
      required: ["buttons"],
      properties: {
        buttons: {
          type: "object",
          description: "Map of button name (string, case-sensitive per the active core) → pressed (boolean: true=pressed, false=released). Example: {\"A\": true, \"Up\": true} presses A and Up while leaving everything else released. Names not recognized by the active core are silently ignored.",
          additionalProperties: { type: "boolean" },
        },
        player: {
          type: "integer",
          minimum: 1,
          default: 1,
          description: "Player number (1-based). Default 1. For multi-controller cores (e.g. N64 with 4 controllers) pass 2/3/4 to address other players.",
        },
      },
      additionalProperties: false,
    },
  },
  {
    name: "bizhawk_press_buttons_multi",
    description:
      "PURPOSE: Set joypad input for MULTIPLE controllers at once, all applied to the SAME next emulated frame. " +
      "USAGE: Use for 2+ player games where controllers must act on the same frame — fighting, racing, co-op (e.g. P1 presses A while P2 presses Right on the very same frame). This is the ONLY way to get simultaneous multi-controller input: calling bizhawk_press_buttons twice in a row lands the two presses on DIFFERENT frames, because each bridge command is followed by exactly one frame advance. For a single controller use bizhawk_press_buttons; for long scripted single-controller runs use bizhawk_play_input_sequence. " +
      "Like bizhawk_press_buttons this is ONE-FRAME input — to HOLD across N frames, interleave bizhawk_press_buttons_multi + bizhawk_frame_advance(count=1) N times (do NOT call it once then frame_advance(count=N); only the first frame would see the input). " +
      "BEHAVIOR: Calls joypad.set once per listed controller before the next frame advance, so they all take effect together. For each listed player, anything not in its `buttons` map is released; players you omit entirely are left to whatever the human is holding (usually nothing). Returns an error if the loaded core doesn't expose joypad.set. Button names not valid for the active core are silently ignored by BizHawk. " +
      "RETURNS: Single line e.g. 'Set 2 controller(s) for next frame — P1: A+Right; P2: B'. " +
      `\n\nButton names vary per system. Common names across cores: ${VALID_BUTTONS.join(", ")}.`,
    inputSchema: {
      type: "object",
      required: ["players"],
      properties: {
        players: {
          type: "array",
          minItems: 1,
          description: "One entry per controller to drive this frame. Each entry sets that player's full input for the upcoming frame; list every controller that must act simultaneously.",
          items: {
            type: "object",
            required: ["player", "buttons"],
            additionalProperties: false,
            properties: {
              player: { type: "integer", minimum: 1, description: "Player number (1-based): 1, 2, 3, 4 … Maps to the controller port on multi-controller cores (SNES multitap, N64, Genesis)." },
              buttons: {
                type: "object",
                additionalProperties: { type: "boolean" },
                description: "Map of button name → pressed (boolean). Same semantics as bizhawk_press_buttons.buttons. Empty object = no input for this player on this frame.",
              },
            },
          },
        },
      },
      additionalProperties: false,
    },
  },
  {
    name: "bizhawk_play_input_sequence",
    description:
      "PURPOSE: Play a pre-built sequence of per-frame joypad inputs back-to-back, advancing one frame per element, ENTIRELY SERVER-SIDE in a single bridge round-trip. Optionally captures screenshots AND labeled memory reads at fixed frame intervals during the play, and optionally aborts play early when a specified memory address changes. All observations come back inline so the agent sees the full trajectory + game state in one tool response. " +
      "USAGE: Use whenever you have ≥10 frames of inputs to play in order — TAS movie playback, scripted multi-frame sequences, AI-search of input patterns, agent-driven gameplay. ONE bridge round-trip ships N frames instead of the 2N round-trips you'd pay looping bizhawk_press_buttons + bizhawk_frame_advance(1). For sequences over ~200 frames, CHUNK. " +
      "FOR AGENT-DRIVEN PLAY: combine `screenshot_every`, `observe_memory`, and `stop_on_memory_change` for the killer pattern — 'walk right for up to 200 frames, observing screenshot+x+y+hp every second, but STOP the moment the room ID changes'. The agent sees: where Samus was at each second, AND whether the goal (room transition) was reached, AND screenshots for visual confirmation — all in one tool response. " +
      "BEHAVIOR: For each `frames` element, calls joypad.set with that frame's buttons then emu.frameadvance. The bridge's main poll loop is BLOCKED for the duration of the call (no other RPCs, no heartbeat) until the sequence finishes or fails. With `screenshot_every`, each captured screenshot adds ~1 frame of wall-clock. With `observe_memory`, each observation also reads the listed memory addresses at the same frame the screenshot was taken — values come back labeled by `name` in each observation's `memory` field. With `stop_on_memory_change`, the bridge records the listed address's value before the first frame, re-reads it after each frame, and aborts the sequence the moment it changes (a final observation is captured at the stop frame regardless of cadence). Returns an error if joypad.set / emu.frameadvance / client.screenshot is missing when needed, if any `observe_memory` entry references an unknown domain, if any `width` isn't 'u8' / 'u16' / 'u32', or if any address is out of range. " +
      "RETURNS: A text summary ('Played N frames. Final framecount: M. Stopped early: yes/no [reason]. Captured K observations with their memory values') followed by K inline image content blocks (one per observation, in frame order). Each observation in the text summary includes its frame_offset and labeled memory values so the agent can correlate the visible screenshot with the game state at that exact frame.",
    inputSchema: {
      type: "object",
      required: ["frames"],
      properties: {
        frames: {
          type: "array",
          description:
            "Array of per-frame input objects. Each element describes ONE emulated frame: " +
            "`{\"buttons\": {\"Right\": true, ...}, \"player\": 1}`. Empty `buttons` (or empty object) = no input on that frame. " +
            "`player` defaults to 1 if omitted. Array order = frame playback order. " +
            "Chunk longer sequences across multiple calls (≤200 frames each is a reasonable upper bound) to keep the bridge responsive.",
          minItems: 1,
          items: {
            type: "object",
            additionalProperties: false,
            properties: {
              buttons: {
                type: "object",
                description: "Map of button name → pressed (boolean). Same semantics as bizhawk_press_buttons.buttons. Omit or empty = no input on this frame.",
                additionalProperties: { type: "boolean" },
              },
              player: {
                type: "integer",
                minimum: 1,
                default: 1,
                description: "Player number (1-based). Default 1.",
              },
            },
          },
        },
        screenshot_every: {
          type: "integer",
          minimum: 1,
          description:
            "Optional. If set, capture a PNG screenshot every N frames during playback (and one extra at the final frame regardless of remainder). Each screenshot costs ~1 wall-clock frame for client.screenshot, so 60 (≈1 sec of game time) is a good default — captures meaningful state changes without doubling batch latency. Omit to skip screenshots. If `observe_memory` is also set, screenshots and memory reads happen at the same observation points.",
        },
        screenshot_dir: {
          type: "string",
          description:
            "Optional. Directory to write screenshot PNGs into when `screenshot_every` is set. Default: C:/temp. Must exist and be writable. Files are named `<prefix>-NNNN.png` where NNNN is the frame offset within the batch (zero-padded to 4 digits).",
        },
        screenshot_prefix: {
          type: "string",
          description:
            "Optional. Filename prefix for screenshots when `screenshot_every` is set. Default: 'obs'.",
        },
        observe_memory: {
          type: "array",
          description:
            "Optional. List of memory reads to perform at each observation point (alongside screenshots if `screenshot_every` is also set). Each result lands in the observation's `memory` field keyed by `name`. " +
            "Use this to track game-state values per observation — e.g. on Super Metroid, track HP/X/Y/room-ID at each screenshot so you see how state changes across the play batch.",
          items: {
            type: "object",
            required: ["name", "address", "width"],
            additionalProperties: false,
            properties: {
              name: {
                type: "string",
                description: "Label for this reading in the output observation. Choose something semantic (e.g. 'hp', 'samus_x', 'room').",
              },
              domain: {
                type: "string",
                description: "Optional case-sensitive memory domain. Omit to use BizHawk's currently selected domain. Same semantics as the standalone read tools.",
              },
              address: {
                type: "integer",
                minimum: 0,
                description: "Byte offset within the chosen memory domain (0-based per-domain).",
              },
              width: {
                type: "string",
                enum: ["u8", "u16", "u32"],
                description: "Read width. 'u16'/'u32' are little-endian (BizHawk's default). For big-endian reads, use width 'u8' multiple times and reassemble client-side (rare).",
              },
            },
          },
        },
        stop_on_memory_change: {
          type: "object",
          description:
            "Optional. If set, the bridge reads the specified memory value before the first frame, re-reads it after every frame, and ABORTS the play sequence the moment it changes. The result will have `stopped_early: true` and `stop_reason: 'memory_changed'`. A final observation is captured at the stop frame even if it's not on the normal cadence. " +
            "Killer use case: watch the room ID — Samus walks through a door, room ID changes, play stops at the exact frame of the transition.",
          required: ["address", "width"],
          additionalProperties: false,
          properties: {
            domain: {
              type: "string",
              description: "Optional case-sensitive memory domain. Same semantics as observe_memory[].domain.",
            },
            address: {
              type: "integer",
              minimum: 0,
              description: "Byte offset within the chosen memory domain to monitor.",
            },
            width: {
              type: "string",
              enum: ["u8", "u16", "u32"],
              description: "Read width for the monitored value. Must match the underlying field's actual width (a u8 watch on a u16 field will only trigger on low-byte changes).",
            },
          },
        },
      },
      additionalProperties: false,
    },
  },

  // ── Breakpoints & CPU state ────────────────────────────────────────────

  {
    name: "bizhawk_add_breakpoint",
    description:
      "PURPOSE: Watch memory reads, writes, or instruction execution at an address (or a short range) and log every hit with the CPU registers, the top of the stack, and optional extra memory reads — e.g. to find which code writes a RAM flag. " +
      "USAGE: Add the breakpoint, then run the game (bizhawk_unpause, bizhawk_frame_advance, bizhawk_play_input_sequence) and fetch what happened with bizhawk_get_breakpoint_hits. Use kind 'write' to find the code that changes a value, 'read' to find what checks it, 'exec' to see when a routine runs. On cores with banked memory, pass `capture` to record the mapping registers (e.g. a Game Boy ROM bank byte) with each hit, since the PC alone doesn't say which bank ran. " +
      "BEHAVIOR: These are LOGGING breakpoints: BizHawk's Lua can't halt the CPU mid-instruction. With pause_on_hit, emulation pauses at the end of the frame in which the hit happened. Each address in the range registers its own callback, so wide ranges slow emulation; exec callbacks on hot code are the most expensive. Hits are kept in a ring buffer of max_hits (oldest dropped first; hit_count keeps the true total). Which kinds fire depends on the core (some cores don't implement read or exec callbacks, or report the written value as 0 — check with a known hit). Breakpoints last until removed or until the bridge script stops. Returns an error for an unknown kind, scope or capture domain, a length outside 1..256, or when the build lacks the event callback API. " +
      "RETURNS: The new breakpoint's id and settings.",
    inputSchema: {
      type: "object",
      properties: {
        kind: { type: "string", enum: ["read", "write", "exec"], description: "What triggers a hit: a memory read, a memory write, or execution of an instruction at the address." },
        address: { type: "integer", minimum: 0, description: "Address to watch, in the scope's address space (the system bus by default — CPU addresses like 0xCB56 on Game Boy, NOT per-domain offsets)." },
        length: { type: "integer", minimum: 1, maximum: 256, default: 1, description: "Watch this many consecutive addresses starting at `address` (default 1). Each is a separate callback." },
        scope: { type: "string", description: "Optional memory domain / bus scope the callback applies to (core-specific; omit for the CPU's system bus)." },
        pause_on_hit: { type: "boolean", default: false, description: "Pause emulation at the end of the frame in which a hit occurs (default false)." },
        max_hits: { type: "integer", minimum: 1, maximum: 10000, default: 100, description: "How many hits to buffer (newest kept). Default 100." },
        stack_depth: { type: "integer", minimum: 0, maximum: 16, default: 4, description: "16-bit little-endian words to read from the stack pointer (register SP) at each hit, from stack_domain — on 8/16-bit CPUs these are usually return addresses, i.e. the calling code. Default 4; 0 disables. Ignored if the core has no SP register." },
        stack_domain: { type: "string", description: "Domain to read the stack from. Default 'System Bus' when the core has one." },
        capture: {
          type: "array",
          description: "Extra values to read at each hit, e.g. [{name:'rom_bank', domain:'System Bus', address:0xFF91, width:'u8'}].",
          items: {
            type: "object",
            properties: {
              name: { type: "string" },
              domain: { type: "string" },
              address: { type: "integer", minimum: 0 },
              width: { type: "string", enum: ["u8", "u16", "u32"], default: "u8" },
            },
            required: ["name", "domain", "address"],
          },
        },
        label: { type: "string", description: "Optional note shown in listings." },
      },
      required: ["kind", "address"],
      additionalProperties: false,
    },
  },
  {
    name: "bizhawk_remove_breakpoint",
    description:
      "PURPOSE: Remove one breakpoint by id, or all of them. " +
      "USAGE: Call when done with a breakpoint, since each one slows emulation slightly. Fetch its hits with bizhawk_get_breakpoint_hits first — they're discarded with it. " +
      "BEHAVIOR: Unregisters the breakpoint's callbacks. Returns an error for an unknown id. " +
      "RETURNS: How many breakpoints were removed.",
    inputSchema: {
      type: "object",
      properties: {
        id: { type: "integer", description: "Breakpoint id from bizhawk_add_breakpoint. Omit to remove ALL breakpoints." },
      },
      additionalProperties: false,
    },
  },
  {
    name: "bizhawk_list_breakpoints",
    description:
      "PURPOSE: List the active breakpoints with their settings and hit counts. " +
      "USAGE: Check which breakpoints exist and whether they've been hit before fetching details with bizhawk_get_breakpoint_hits. " +
      "BEHAVIOR: No side effects — pure read. " +
      "RETURNS: One line per breakpoint: id, kind, address range, total hits, buffered hits, and dropped hits.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "bizhawk_get_breakpoint_hits",
    description:
      "PURPOSE: Fetch the logged hits of one breakpoint: frame, address, value, CPU registers, stack words, and captured memory for each. " +
      "USAGE: Call after running the game with a breakpoint set. Pass clear=true to empty the buffer, so the next fetch only shows new hits. " +
      "BEHAVIOR: Pure read unless clear=true. Returns an error for an unknown id. " +
      "RETURNS: A summary line, then one block per hit (newest last), as JSON.",
    inputSchema: {
      type: "object",
      properties: {
        id: { type: "integer", description: "Breakpoint id." },
        limit: { type: "integer", minimum: 1, description: "Return only the newest N buffered hits (default all)." },
        clear: { type: "boolean", default: false, description: "Empty the hit buffer after reading (hit_count keeps counting)." },
      },
      required: ["id"],
      additionalProperties: false,
    },
  },
  {
    name: "bizhawk_get_registers",
    description:
      "PURPOSE: Read the CPU registers of the loaded core right now (e.g. A, F, B, C, D, E, H, L, PC, SP on Game Boy). " +
      "USAGE: Use while paused or between frame advances to inspect CPU state. Register names are core-specific. For the registers at the moment a particular address is touched, use bizhawk_add_breakpoint instead — between frames the PC is usually in the game's idle loop. " +
      "BEHAVIOR: No side effects — pure read. Returns an error if the build lacks emu.getregisters. " +
      "RETURNS: One line per register: name and value in decimal and hex.",
    inputSchema: { type: "object", properties: {} },
  },

  // ── Arbitrary Lua (opt-in) ─────────────────────────────────────────────

  {
    name: "bizhawk_eval_lua",
    description:
      "PURPOSE: Run a short Lua script inside BizHawk's sandbox to read data the fixed tools can't reach in one call — walking pointer chains, decoding packed structs, scanning tables, or drawing labels with gui.*. " +
      "USAGE: Prefer the dedicated tools (read*, read_range, search_memory) when they suffice. Use this for multi-step logic that would otherwise take many round-trips. Write memory with the bizhawk_write* tools; the sandbox is read-only. " +
      "BEHAVIOR: DISABLED BY DEFAULT. While disabled, the call fails and opens a confirmation dialog in BizHawk; only the human at the computer can enable it, by ticking its checkbox and clicking Accept (do not ask or pressure them to). Cancel or closing the window declines, and the dialog then won't reopen for 2 minutes. After Accept it stays enabled until the human clicks Disable or closes the dialog. The script runs as text only, in an environment that contains ONLY: memory/mainmemory read functions, emu info (framecount, getregisters, disassemble, …), gui drawing primitives, client screen sizes, gameinfo, bit, math, string (no dump; rep capped), table, utf8, and basic functions (pairs, ipairs, tostring, pcall, print, …). There is no os, io, require, load, debug, coroutine, comm, forms or .NET access. Runs synchronously within one frame and is stopped after max_instructions VM instructions or max_memory_kb of new allocations. " +
      "RETURNS: The script's return values (tables converted to JSON up to 4 levels), anything it print()ed, and the instruction count used; or the compile/runtime/budget error.",
    inputSchema: {
      type: "object",
      required: ["code"],
      properties: {
        code:             { type: "string", maxLength: 65536, description: "Lua 5.4 source. Use `return` to send values back, e.g. `local p = memory.read_u16_le(0x1234, \"WRAM\") return memory.read_u8(p, \"WRAM\")`. Addresses are per-domain offsets, as with the read tools." },
        max_instructions: { type: "integer", minimum: 1, maximum: 50000000, default: 1000000, description: "VM instruction budget (default 1,000,000; max 50,000,000). Counted in steps of 1000." },
        max_memory_kb:    { type: "integer", minimum: 1, maximum: 65536, default: 16384, description: "Ceiling on new Lua heap use during the run, in KB (default 16384; max 65536)." },
      },
      additionalProperties: false,
    },
  },

  // ── Emulator lifecycle ─────────────────────────────────────────────────

  {
    name: "bizhawk_launch",
    description:
      "PURPOSE: Start the BizHawk emulator with a ROM and the mcp-bizhawk Lua bridge already loaded, so no one has to open it by hand. " +
      "USAGE: Call at the start of a session when bizhawk_ping fails because BizHawk isn't running. Afterwards every other tool works without further setup. Use bizhawk_close to shut it down. " +
      "BEHAVIOR: Starts an OS process. The executable is fixed by the BIZHAWK_EXE setting in the MCP config (it can't be chosen here), and the Lua script is always this package's bridge.lua. The ROM must be an existing file with a known ROM extension (.gb, .gbc, .gba, .nes, .sfc, .z64, .cue, .zip, …). Waits up to wait_seconds for the bridge to start polling. Returns an error if BIZHAWK_EXE is unset or invalid, the ROM fails validation, a BizHawk is already connected, or one launched by this server is still running. " +
      "RETURNS: Lines giving the PID, ROM path, and whether the bridge connected in time.",
    inputSchema: {
      type: "object",
      required: ["rom"],
      properties: {
        rom:          { type: "string", description: "Path to the ROM file to boot. Absolute is safest; relative paths resolve against the MCP server's working directory (usually the project root)." },
        wait_seconds: { type: "integer", minimum: 1, maximum: 120, default: 30, description: "How long to wait for bridge.lua to connect before returning (default 30). BizHawk's first start on a machine can be slow." },
      },
      additionalProperties: false,
    },
  },
  {
    name: "bizhawk_close",
    description:
      "PURPOSE: Shut down the BizHawk emulator cleanly. " +
      "USAGE: Call when a session is finished, or before bizhawk_launch to restart with a different ROM. Save anything you need first (bizhawk_save_state). " +
      "BEHAVIOR: DESTRUCTIVE: closes the emulator; unsaved progress is lost. Asks the bridge to call client.exit() after replying, then waits for the socket to close. If the bridge isn't answering but this server launched the BizHawk process, that process is terminated instead. Returns an error if neither path is available (BizHawk not connected and not launched by this server). " +
      "RETURNS: Single line describing how BizHawk was closed.",
    inputSchema: { type: "object", properties: {} },
  },

  // ── Emulator control ───────────────────────────────────────────────────

  {
    name: "bizhawk_pause",
    description:
      "PURPOSE: Pause emulation — freeze game-logic clocks and hold the current frame on screen. " +
      "USAGE: Use before a sequence of memory-inspect / write / screenshot calls when you need a stable game state across calls (so the game doesn't advance between your reads). Use bizhawk_unpause to resume; use bizhawk_frame_advance to step single frames without leaving pause. " +
      "BEHAVIOR: Modifies emulator run state. The Lua bridge keeps polling the socket while paused, so all other tool calls (memory r/w, screenshot, save_state, etc.) still work. Uses emu.pause where the build has it, else client.pause (BizHawk 2.x). Returns an error if neither exists — check `capabilities.pause` in bizhawk_get_info first to handle that case gracefully. Calling pause when already paused is a no-op. " +
      "RETURNS: Single line 'Emulation paused'.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "bizhawk_unpause",
    description:
      "PURPOSE: Resume emulation after a pause, returning to normal real-time playback. " +
      "USAGE: Counterpart to bizhawk_pause. Use after a paused inspection sequence is complete. To advance only a few frames without resuming full speed, use bizhawk_frame_advance instead. " +
      "BEHAVIOR: Modifies emulator run state. Uses emu.unpause where the build has it, else client.unpause (BizHawk 2.x). Returns an error if neither exists — check `capabilities.unpause` in bizhawk_get_info first. Calling unpause when not paused is a no-op. " +
      "RETURNS: Single line 'Emulation resumed'.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "bizhawk_frame_advance",
    description:
      "PURPOSE: Step emulation by exactly N frames (default 1) and return the new framecount. " +
      "USAGE: Use for frame-precise input automation (combine with bizhawk_press_buttons), animation inspection, or letting the system initialize after a hard reset (RAM is mostly zero in the first ~30 frames after bizhawk_reset). For long jumps (thousands of frames) prefer bizhawk_save_state / bizhawk_load_state of a pre-prepared state — frame_advance scales linearly. Works whether emulation is currently paused or running and does NOT change the pause state (from a pause it steps exactly N frames and stays paused). " +
      "BEHAVIOR: Advances the game-logic clock by N frames. Each step costs roughly one real frame (~16ms at 60Hz) plus one bridge round-trip — so advancing 600 frames takes ~10 seconds wall-clock. Returns an error if the loaded core doesn't expose emu.frameadvance. " +
      "RETURNS: Single line 'Advanced N frame(s). Framecount: NEW_COUNT'.",
    inputSchema: {
      type: "object",
      properties: {
        count: { type: "integer", minimum: 1, default: 1, description: "Number of frames to advance (≥1, default 1). Latency scales linearly: ~16ms per frame at 60Hz. New framecount = previous framecount + count." },
      },
      additionalProperties: false,
    },
  },
  {
    name: "bizhawk_reset",
    description:
      "PURPOSE: Reset the loaded core — equivalent to a hard reset (power cycle) of the emulated console. " +
      "USAGE: Use to start fresh from boot. To return to a specific known-good point instead of boot, use bizhawk_load_state with a previously saved state file. " +
      "BEHAVIOR: DESTRUCTIVE: RAM contents become indeterminate (typically zeroed), CPU returns to the reset vector, framecount resets to 0, joypad state clears, and any in-progress audio/video state is discarded. The loaded ROM stays loaded — only volatile state is cleared. Unsaved game progress is lost. Returns an error if the loaded core doesn't expose client.reboot_core — check `capabilities.reboot_core` in bizhawk_get_info first. " +
      "RETURNS: Single line 'Core reset'.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "bizhawk_screenshot",
    description:
      "PURPOSE: Save a PNG screenshot of the current emulator display to the given file path. " +
      "USAGE: Use to capture visible game state for inspection, comparison across savestates, or sequence documentation. The image captures whatever the emulator is currently rendering — to capture a specific game state, pause / advance frames / load state first to get the frame you want, then call this. BizHawk's underlying client.screenshot requires an explicit path (no temp-file fallback). " +
      "BEHAVIOR: DESTRUCTIVE TO TARGET FILE: overwrites the file at `path` if it exists, with no prompt or backup. Returns an error if the parent directory doesn't exist, the path isn't writable, or the loaded core doesn't expose client.screenshot — check `capabilities.screenshot` in bizhawk_get_info first. " +
      "RETURNS: Single line 'Screenshot saved: PATH'.",
    inputSchema: {
      type: "object",
      required: ["path"],
      properties: {
        path: { type: "string", description: "Absolute filesystem path to write the PNG to (e.g. C:/temp/snap.png on Windows, /tmp/snap.png on Linux/macOS). Parent directory must exist. File is overwritten without prompt if present." },
      },
      additionalProperties: false,
    },
  },

  // ── Save state ─────────────────────────────────────────────────────────

  {
    name: "bizhawk_save_state",
    description:
      "PURPOSE: Save the entire emulator state (RAM, CPU/PPU/APU registers, mapper state, sound chip state, timing) to a file at the given path. " +
      "USAGE: Use as a rollback point before risky writes, to bookmark interesting game states, or to share repro states. The companion bizhawk_load_state can perfectly restore from this file. BizHawk's savestate API is path-based (NOT slot-based like mGBA's). " +
      "BEHAVIOR: DESTRUCTIVE TO TARGET FILE: overwrites the file at `path` if it exists, with no prompt or backup. The state file is bound to the EXACT ROM and BizHawk core version that produced it — loading it on a different ROM or core version usually crashes the core. Returns an error if the parent directory doesn't exist, the path isn't writable, or the core doesn't expose savestate.save. " +
      "RETURNS: Single line 'Saved state to PATH'.",
    inputSchema: {
      type: "object",
      required: ["path"],
      properties: {
        path: { type: "string", description: "Absolute filesystem path to write the .State file to (extension is convention, not required). Parent directory must exist. File is overwritten without prompt if present." },
      },
      additionalProperties: false,
    },
  },
  {
    name: "bizhawk_load_state",
    description:
      "PURPOSE: Restore the emulator from a previously saved .State file at the given path. " +
      "USAGE: Counterpart to bizhawk_save_state. Use to undo a sequence of writes/inputs (the snapshot/experiment/restore workflow), to jump to a bookmarked game state, or to start each tool-call sequence from a known baseline. To start fresh from console boot instead, use bizhawk_reset. " +
      "BEHAVIOR: DESTRUCTIVE TO LIVE STATE: replaces ALL current emulator state (RAM, registers, mapper, audio, framecount) with the file's contents. Anything not previously snapshotted is lost. The state file MUST come from the same ROM and same BizHawk core version that produced it — loading an incompatible state typically crashes the core. Returns an error if the file doesn't exist, isn't a valid BizHawk state, or the core doesn't expose savestate.load. " +
      "RETURNS: Single line 'Loaded state from PATH'.",
    inputSchema: {
      type: "object",
      required: ["path"],
      properties: {
        path: { type: "string", description: "Absolute filesystem path to an existing .State file produced by bizhawk_save_state on this same ROM and BizHawk core version. Loading mismatched files typically crashes the core." },
      },
      additionalProperties: false,
    },
  },
];

function ok(text: string) {
  return { content: [{ type: "text" as const, text }] };
}
function fmtHex(n: unknown): string {
  if (typeof n !== "number") return String(n);
  return `${n} (0x${n.toString(16).toUpperCase()})`;
}
function addrHex(n: number): string {
  return `0x${n.toString(16).toUpperCase().padStart(4, "0")}`;
}

interface BreakpointSummary {
  id: number;
  kind: string;
  address: number;
  length: number;
  scope?: string;
  pause_on_hit?: boolean;
  hit_count: number;
  buffered: number;
  dropped: number;
  max_hits: number;
  label?: string;
}

function describeBreakpoint(b: BreakpointSummary): string {
  const range = b.length > 1 ? `${addrHex(b.address)}-${addrHex(b.address + b.length - 1)}` : addrHex(b.address);
  return `${b.kind} ${range}${b.scope ? ` in ${b.scope}` : ""}${b.pause_on_hit ? ", pauses on hit" : ""}` +
    `${b.label ? ` (${b.label})` : ""} — ${b.hit_count} hit(s), ${b.buffered} buffered` +
    `${b.dropped ? `, ${b.dropped} dropped` : ""} (max ${b.max_hits})`;
}

interface SnapshotInfo {
  name: string; domain?: string; start: number; length: number; frame?: number;
  used_bytes: number; max_bytes: number;
}
export interface CompareResult {
  count: number;
  checked: number;
  results: { address: number; before: number; after: number }[] | Record<string, never>;
  truncated: boolean;
  domain?: string;
  saved_as?: string;
  saved_count?: number;
}
export interface SnapshotList {
  snapshots: { name: string; domain?: string; start: number; length: number; frame?: number }[] | Record<string, never>;
  candidates: { name: string; domain?: string; width: string; count: number }[] | Record<string, never>;
  used_bytes: number;
  max_bytes: number;
}

/**
 * Format a byte count as KiB/MiB for summaries.
 * @returns e.g. "32 KiB" or "1.5 MiB".
 * @requires Nothing.
 */
function kb(n: number): string {
  return n >= 1048576 ? `${(n / 1048576).toFixed(1)} MiB` : `${Math.round(n / 1024)} KiB`;
}

/**
 * json.lua encodes an empty Lua table as {} rather than [], so list fields may arrive as objects.
 * @returns the value as an array (empty for {}).
 * @requires Nothing.
 */
function asList<T>(v: T[] | Record<string, never> | undefined): T[] {
  return Array.isArray(v) ? v : [];
}

/**
 * Format a snapshot comparison for the caller.
 * @returns A summary line plus one "address: before → after" line per listed match.
 * @requires Nothing.
 */
export function describeCompare(p: Record<string, unknown>, r: CompareResult): string {
  const width = (p.width as string) ?? "u8";
  const opText = p.value !== undefined ? `${p.op} ${p.value}` : String(p.op);
  const scope = p.candidates ? `of candidate set '${p.candidates}'` : `in ${r.domain ?? "the snapshot range"}`;
  const results = asList(r.results);
  let head = `${r.count} of ${r.checked} address(es) ${scope} ${opText} (${width}), '${p.before}' → '${p.after}'.`;
  if (r.saved_as) head += ` Saved ${r.saved_count} as candidate set '${r.saved_as}'.`;
  if (r.truncated) head += ` Listing the first ${results.length}.`;
  const lines = results.map((m) => `${addrHex(m.address)}: ${m.before} → ${m.after}`);
  return [head, ...lines].join("\n");
}

/**
 * Format the snapshot store listing.
 * @returns One line per snapshot and per candidate set, plus usage.
 * @requires Nothing.
 */
export function describeSnapshotList(r: SnapshotList): string {
  const snaps = asList(r.snapshots), cands = asList(r.candidates);
  const lines = [`Store: ${kb(r.used_bytes)} of ${kb(r.max_bytes)} used.`];
  lines.push(snaps.length ? "Snapshots:" : "No snapshots.");
  for (const s of snaps) {
    lines.push(`  ${s.name}: ${s.domain ?? "?"} ${addrHex(s.start)}..${addrHex(s.start + s.length - 1)} (${s.length} bytes), frame ${s.frame ?? "?"}`);
  }
  lines.push(cands.length ? "Candidate sets:" : "No candidate sets.");
  for (const c of cands) lines.push(`  ${c.name}: ${c.count} address(es) in ${c.domain ?? "?"} (${c.width})`);
  return lines.join("\n");
}

export interface EvalResult {
  values: unknown[] | Record<string, unknown>;
  count: number;
  output: string;
  instructions: number;
}

/**
 * Format a sandbox run for the caller.
 * @returns Text with each return value as JSON, then any printed output and
 *          the instruction count. json.lua encodes a values list with nil holes
 *          as an object keyed by position, so both shapes are handled.
 * @requires Nothing.
 */
export function describeEval(r: EvalResult): string {
  const lines: string[] = [];
  for (let i = 1; i <= r.count; i++) {
    const v = Array.isArray(r.values) ? r.values[i - 1] : r.values?.[String(i)];
    lines.push(`return[${i}]: ${JSON.stringify(v ?? null, null, 2)}`);
  }
  if (r.count === 0) lines.push("(no return values)");
  if (r.output) lines.push(`--- output ---\n${r.output}`);
  lines.push(`(${r.instructions} instructions)`);
  return lines.join("\n");
}

/**
 * Start BizHawk and wait for the bridge to come up.
 * @returns MCP text result with PID, ROM and connection status. Throws on
 *          validation or spawn failure, or if BizHawk is already connected.
 * @requires BizhawkLauncher (BIZHAWK_EXE configured); a listening BizhawkServer.
 */
async function launchBizhawk(bh: BizhawkServer, launcher: BizhawkLauncher, rom: string, waitSeconds: number) {
  if (bh.isConnected()) throw new Error("BizHawk is already connected. Use bizhawk_close first to restart it.");
  const r = await launcher.launch(rom, bh.target());
  const ready = await bh.waitForBridge(waitSeconds * 1000);
  return ok(
    `Launched BizHawk (PID ${r.pid})\nROM: ${r.rom}\n` +
    (ready
      ? "Bridge connected — ready for tool calls."
      : `Bridge not connected after ${waitSeconds}s. BizHawk may still be starting; retry bizhawk_ping shortly.`),
  );
}

/**
 * Close BizHawk: gracefully through the bridge, else by ending our own process.
 * @returns MCP text result describing which path was taken. Throws if BizHawk
 *          is neither connected nor launched by this server.
 * @requires BizhawkServer bridge (client.exit) or a process started by BizhawkLauncher.
 */
async function closeBizhawk(bh: BizhawkServer, launcher: BizhawkLauncher) {
  if (bh.isBridgeReady()) {
    try {
      await bh.call("close");
      const gone = await bh.waitForDisconnect(10_000);
      await launcher.waitForExit(10_000);
      if (gone) return ok("BizHawk closed (client.exit).");
      if (!launcher.isRunning()) {
        return ok("Close sent, but BizHawk is still connected after 10s. It may be showing a dialog (e.g. save prompt).");
      }
    } catch (err) {
      if (!launcher.isRunning()) throw err;
    }
  }
  if (launcher.isRunning()) {
    const pid = launcher.pid();
    launcher.kill();
    const exited = await launcher.waitForExit(10_000);
    return ok(exited
      ? `Bridge didn't respond; terminated the BizHawk process this server launched (PID ${pid}).`
      : `Sent terminate to PID ${pid}, but it hasn't exited after 10s.`);
  }
  throw new Error("BizHawk isn't connected and wasn't launched by this server, so there's nothing to close.");
}

export interface ToolOptions {
  /** Offer bizhawk_eval_lua (BIZHAWK_EVAL_LUA=1). Off: the tool isn't listed and calls are refused. */
  evalLua: boolean;
}

/** Tools gated behind a server-config switch, by name. */
const OPT_IN_TOOLS: Record<string, keyof ToolOptions> = { bizhawk_eval_lua: "evalLua" };

/**
 * Register the MCP tool list and dispatcher.
 * @returns nothing. Opt-in tools whose switch is off are left out of tools/list,
 *          and calling them throws before anything is sent to BizHawk.
 * @requires A BizhawkServer for bridge calls; a BizhawkLauncher for launch/close.
 */
export function registerTools(server: Server, bh: BizhawkServer, launcher: BizhawkLauncher, opts: ToolOptions): void {
  const offered = TOOLS.filter((t) => !(t.name in OPT_IN_TOOLS) || opts[OPT_IN_TOOLS[t.name]]);
  server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: offered }));

  server.setRequestHandler(CallToolRequestSchema, async (req) => {
    const { name, arguments: args = {} } = req.params;
    if (name in OPT_IN_TOOLS && !opts[OPT_IN_TOOLS[name]]) {
      throw new Error(`${name} is turned off in this server's config. Only the user can enable it ` +
        `(BIZHAWK_EVAL_LUA=1 in the MCP server env). Don't ask them to unless they asked for Lua work.`);
    }
    const p = args as Record<string, unknown>;
    const a = () => p.address as number;
    const dom = () => p.domain ? { domain: p.domain } : {};

    switch (name) {
      case "bizhawk_ping": {
        const r = await bh.call<string>("ping");
        return ok(r);
      }

      case "bizhawk_get_info": {
        const r = await bh.call<{
          rom_name?: string;
          rom_hash?: string;
          framecount?: number;
          memory_domains?: string[];
          current_memory_domain?: string;
          capabilities?: Record<string, boolean>;
        }>("get_info");
        const lines = [
          `ROM:        ${r.rom_name ?? "(unavailable)"}`,
          `ROM hash:   ${r.rom_hash ?? "(unavailable)"}`,
          `Framecount: ${r.framecount ?? "(unavailable)"}`,
        ];
        if (r.memory_domains?.length) {
          lines.push("");
          lines.push(`Memory domains: ${r.memory_domains.join(", ")}`);
          if (r.current_memory_domain) {
            lines.push(`Active domain (used when 'domain' is omitted): ${r.current_memory_domain}`);
          }
        }
        if (r.capabilities) {
          const missing = Object.entries(r.capabilities).filter(([, v]) => !v).map(([k]) => k);
          if (missing.length) {
            lines.push("");
            lines.push(`Missing capabilities on this BizHawk build: ${missing.join(", ")}`);
          }
        }
        return ok(lines.join("\n"));
      }

      case "bizhawk_list_memory_domains": {
        const r = await bh.call<string[]>("list_memory_domains");
        return ok("Memory domains:\n  " + r.join("\n  "));
      }

      case "bizhawk_read8":  return ok(`${addrHex(a())}: ${fmtHex(await bh.call<number>("read8", { address: a(), ...dom() }))}`);
      case "bizhawk_read16": return ok(`${addrHex(a())}: ${fmtHex(await bh.call<number>("read16", { address: a(), ...dom() }))}`);
      case "bizhawk_read32": return ok(`${addrHex(a())}: ${fmtHex(await bh.call<number>("read32", { address: a(), ...dom() }))}`);

      case "bizhawk_read_range": {
        const bytes = await bh.call<number[]>("read_range", { address: a(), length: p.length, ...dom() });
        const hex = bytes.map((b) => b.toString(16).padStart(2, "0").toUpperCase()).join(" ");
        return ok(`${addrHex(a())} [${bytes.length} bytes${p.domain ? `, ${p.domain}` : ""}]:\n${hex}`);
      }

      case "bizhawk_write8": {
        await bh.call("write8", { address: a(), value: p.value, ...dom() });
        return ok(`Wrote ${fmtHex(p.value)} → ${addrHex(a())}${p.domain ? ` (${p.domain})` : ""}`);
      }
      case "bizhawk_write16": {
        await bh.call("write16", { address: a(), value: p.value, ...dom() });
        return ok(`Wrote ${fmtHex(p.value)} → ${addrHex(a())}${p.domain ? ` (${p.domain})` : ""}`);
      }
      case "bizhawk_write32": {
        await bh.call("write32", { address: a(), value: p.value, ...dom() });
        return ok(`Wrote ${fmtHex(p.value)} → ${addrHex(a())}${p.domain ? ` (${p.domain})` : ""}`);
      }
      case "bizhawk_write_range": {
        const r = await bh.call<{ written: number }>("write_range", { address: a(), bytes: p.bytes, ...dom() });
        return ok(`Wrote ${r.written} bytes → ${addrHex(a())}${p.domain ? ` (${p.domain})` : ""}`);
      }

      case "bizhawk_snapshot_memory": {
        const params: Record<string, unknown> = { name: p.name };
        for (const k of ["domain", "start", "length"]) if (p[k] !== undefined) params[k] = p[k];
        const r = await bh.call<SnapshotInfo>("snapshot_memory", params);
        return ok(`Snapshot '${r.name}': ${r.domain ?? "current domain"} ${addrHex(r.start)}..${addrHex(r.start + r.length - 1)} ` +
          `(${r.length} bytes) at frame ${r.frame ?? "?"}. Store: ${kb(r.used_bytes)} of ${kb(r.max_bytes)} used.`);
      }
      case "bizhawk_compare_snapshots": {
        const params: Record<string, unknown> = { before: p.before, after: p.after, op: p.op };
        for (const k of ["value", "width", "big_endian", "aligned", "candidates", "save_as", "max_results"]) {
          if (p[k] !== undefined) params[k] = p[k];
        }
        return ok(describeCompare(p, await bh.call<CompareResult>("compare_snapshots", params)));
      }
      case "bizhawk_list_snapshots": {
        const r = await bh.call<SnapshotList>("list_snapshots");
        return ok(describeSnapshotList(r));
      }
      case "bizhawk_delete_snapshots": {
        const r = await bh.call<{ deleted: number }>("delete_snapshots", p.name !== undefined ? { name: p.name } : {});
        return ok(`Deleted ${r.deleted} ${r.deleted === 1 ? "entry" : "entries"}${p.name !== undefined ? ` named '${p.name}'` : " (everything)"}.`);
      }

      case "bizhawk_search_memory": {
        const params: Record<string, unknown> = { value: p.value, width: p.width ?? "u16" };
        if (p.addresses   !== undefined) params.addresses   = p.addresses;
        if (p.start       !== undefined) params.start       = p.start;
        if (p.length      !== undefined) params.length      = p.length;
        if (p.aligned     !== undefined) params.aligned     = p.aligned;
        if (p.max_results !== undefined) params.max_results = p.max_results;
        if (p.domain      !== undefined) params.domain      = p.domain;
        const r = await bh.call<{
          mode: string;
          addresses: number[];
          count: number;
          candidates?: number;
          scanned?: number;
          truncated?: boolean;
        }>("search_memory", params);
        const width = (p.width as string) ?? "u16";
        const domLabel = p.domain ? ` in ${p.domain}` : "";
        const shown = r.addresses.length;
        const head = r.mode === "next"
          ? `${r.count} of ${r.candidates} candidate(s) still equal ${fmtHex(p.value)} (${width})${domLabel}.`
          : `${r.count} match(es) for ${fmtHex(p.value)} (${width})${domLabel} [scanned ${r.scanned} bytes].`;
        const note = r.truncated
          ? ` Showing first ${shown} — raise max_results or narrow the search to see the rest.`
          : (shown ? ` Showing all ${shown}.` : "");
        const list = shown ? "\n" + r.addresses.map((o) => addrHex(o)).join(" ") : "";
        return ok(`${head}${note}${list}`);
      }

      case "bizhawk_press_buttons": {
        await bh.call("press_buttons", { buttons: p.buttons, player: p.player ?? 1 });
        const pressed = Object.entries(p.buttons as Record<string, boolean>)
          .filter(([, v]) => v).map(([k]) => k);
        return ok(`Set joypad ${p.player ?? 1}: ${pressed.length ? pressed.join("+") : "(all released)"}`);
      }

      case "bizhawk_press_buttons_multi": {
        const players = p.players as Array<{ player: number; buttons: Record<string, boolean> }>;
        await bh.call("press_buttons_multi", { players });
        const summary = players.map((e) => {
          const pressed = Object.entries(e.buttons ?? {}).filter(([, v]) => v).map(([k]) => k);
          return `P${e.player}: ${pressed.length ? pressed.join("+") : "(none)"}`;
        }).join("; ");
        return ok(`Set ${players.length} controller(s) for next frame — ${summary}`);
      }

      case "bizhawk_play_input_sequence": {
        const params: Record<string, unknown> = { frames: p.frames };
        if (p.screenshot_every       !== undefined) params.screenshot_every       = p.screenshot_every;
        if (p.screenshot_dir         !== undefined) params.screenshot_dir         = p.screenshot_dir;
        if (p.screenshot_prefix      !== undefined) params.screenshot_prefix      = p.screenshot_prefix;
        if (p.observe_memory         !== undefined) params.observe_memory         = p.observe_memory;
        if (p.stop_on_memory_change  !== undefined) params.stop_on_memory_change  = p.stop_on_memory_change;
        const r = await bh.call<{
          played: number;
          final_framecount?: number;
          stopped_early?: boolean;
          stop_reason?: string;
          observations?: {
            frame_offset: number;
            path?: string;
            memory?: Record<string, number>;
          }[];
        }>("play_input_sequence", params);

        const obs = r.observations ?? [];
        const lines = [
          `Played ${r.played} frames. Final framecount: ${r.final_framecount ?? "(unavailable)"}.`,
        ];
        if (r.stopped_early) {
          lines.push(`Stopped early — reason: ${r.stop_reason ?? "(unspecified)"}.`);
        }
        lines.push(`Captured ${obs.length} observation${obs.length === 1 ? "" : "s"}.`);
        // Per-observation lines so the agent can correlate inline images with state
        for (let i = 0; i < obs.length; i++) {
          const o = obs[i];
          const memStr = o.memory
            ? ` memory={${Object.entries(o.memory).map(([k, v]) => `${k}=${v}`).join(", ")}}`
            : "";
          const imgStr = o.path ? ` (image ${i + 1})` : "";
          lines.push(`  obs[${i}] frame_offset=${o.frame_offset}${memStr}${imgStr}`);
        }

        // Build the multi-content response: text summary + per-observation
        // inline image blocks. We read each PNG from disk (Lua wrote it),
        // base64-encode for MCP transport.
        const content: ({ type: "text"; text: string } | { type: "image"; data: string; mimeType: string })[] = [
          { type: "text", text: lines.join("\n") },
        ];
        const fs = await import("node:fs");
        for (const o of obs) {
          if (!o.path) continue;
          try {
            const bytes = fs.readFileSync(o.path);
            content.push({
              type: "image",
              data: bytes.toString("base64"),
              mimeType: "image/png",
            });
          } catch (err) {
            content.push({
              type: "text",
              text: `(failed to read observation at frame ${o.frame_offset} from ${o.path}: ${(err as Error).message})`,
            });
          }
        }
        return { content };
      }

      case "bizhawk_add_breakpoint": {
        const r = await bh.call<BreakpointSummary>("add_breakpoint", { ...p });
        return ok(`Added breakpoint ${r.id}: ${describeBreakpoint(r)}`);
      }
      case "bizhawk_remove_breakpoint": {
        const r = await bh.call<{ removed: number }>("remove_breakpoint", p.id === undefined ? {} : { id: p.id });
        return ok(`Removed ${r.removed} breakpoint(s)`);
      }
      case "bizhawk_list_breakpoints": {
        const r = await bh.call<BreakpointSummary[] | Record<string, never>>("list_breakpoints");
        const list = Array.isArray(r) ? r : [];
        if (!list.length) return ok("No breakpoints");
        return ok(list.map((b) => `${b.id}: ${describeBreakpoint(b)}`).join("\n"));
      }
      case "bizhawk_get_breakpoint_hits": {
        const r = await bh.call<BreakpointSummary & { hits?: unknown[] }>("get_breakpoint_hits", {
          id: p.id, ...(p.limit !== undefined ? { limit: p.limit } : {}), ...(p.clear ? { clear: true } : {}),
        });
        const hits = Array.isArray(r.hits) ? r.hits : [];
        const head = `Breakpoint ${r.id}: ${describeBreakpoint(r)}; showing ${hits.length}` + (p.clear ? " (buffer cleared)" : "");
        return ok(hits.length ? `${head}\n${JSON.stringify(hits, null, 1)}` : head);
      }
      case "bizhawk_get_registers": {
        const r = await bh.call<Record<string, number>>("get_registers");
        return ok(Object.entries(r).map(([k, v]) => `${k.padEnd(8)} ${fmtHex(v)}`).join("\n"));
      }

      case "bizhawk_eval_lua": {
        const r = await bh.call<EvalResult>("eval_lua", {
          code: p.code,
          ...(p.max_instructions !== undefined ? { max_instructions: p.max_instructions } : {}),
          ...(p.max_memory_kb    !== undefined ? { max_memory_kb:    p.max_memory_kb }    : {}),
        });
        return ok(describeEval(r));
      }

      case "bizhawk_launch": return launchBizhawk(bh, launcher, p.rom as string, (p.wait_seconds as number) ?? 30);
      case "bizhawk_close":  return closeBizhawk(bh, launcher);

      case "bizhawk_pause":         await bh.call("pause");          return ok("Emulation paused");
      case "bizhawk_unpause":       await bh.call("unpause");        return ok("Emulation resumed");
      case "bizhawk_reset":         await bh.call("reset");          return ok("Core reset");
      case "bizhawk_frame_advance": {
        const f = await bh.call<number>("frame_advance", { count: p.count ?? 1 });
        return ok(`Advanced ${p.count ?? 1} frame(s). Framecount: ${f}`);
      }

      case "bizhawk_screenshot": {
        const path = await bh.call<string>("screenshot", { path: p.path });
        return ok(`Screenshot saved: ${path}`);
      }

      case "bizhawk_save_state": {
        const r = await bh.call<{ path: string }>("save_state", { path: p.path });
        return ok(`Saved state to ${r.path}`);
      }
      case "bizhawk_load_state": {
        const r = await bh.call<{ path: string }>("load_state", { path: p.path });
        return ok(`Loaded state from ${r.path}`);
      }

      default:
        throw new Error(`Unknown tool: ${name}`);
    }
  });
}
