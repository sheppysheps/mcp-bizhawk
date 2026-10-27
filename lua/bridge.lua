-- bridge.lua: BizHawk-side polling client for mcp-bizhawk
--
-- Architecture (inverted from mcp-mgba):
--
--   mcp-bizhawk (Node.js, runs the TCP server)
--          ▲
--          │  TCP — newline-delimited JSON
--          │
--   bridge.lua (BizHawk Lua, polls every frame)
--
-- Each frame, this script does ONE round-trip with the external server:
--   1. Lua sends "READY\n"  (or "RESULT <json>\n" if there's a pending result)
--   2. Server responds with EITHER "NONE\n" OR a JSON command "{id,method,params}\n"
--   3. If a command came back, execute it locally and stash the result so
--      the next frame's send carries it back.
--
-- Wire format: newline-delimited UTF-8.
--   - Lua → server: "READY\n" | "RESULT <json>\n"
--   - Server → Lua: "NONE\n"  | "<json command>\n"
--
-- Setup in BizHawk:
--   1. Tools → External Tool → Lua Console
--   2. Settings → Customize → External Tools → "Custom Tools" — set socket
--      server IP/port to 127.0.0.1:8766 (or however you launched mcp-bizhawk)
--      OR launch BizHawk with --socket_ip=127.0.0.1 --socket_port=8766
--   3. Tools → Lua Console → Open this script
--
-- Verify with `mcp-bizhawk` startup log: should print "BizHawk client connected".

local json = require("json")
-- Memory snapshot store and comparisons (pure Lua, no system access).
local snapshot = require("snapshot")
-- Byte-pattern, text and relative search (pure Lua, no system access).
local bytesearch = require("bytesearch")
-- lua/sandbox.lua (opt-in arbitrary Lua) is NOT loaded here: it's required
-- lazily on the first eval_lua call, so ordinary sessions never load it.

-- Pending result from last frame's command. Sent back at the start of the
-- next frame's round-trip (instead of a bare "READY").
local pending_result = nil

-- ── Capability detection ────────────────────────────────────────────────────
-- BizHawk's Lua API is largely stable but a few of the higher-level helpers
-- vary by build. Probe at startup so we can return clean errors instead of
-- "attempt to call a nil value" if something's missing.
--
-- IMPORTANT: BizHawk exposes .NET methods to Lua as USERDATA with __call
-- metamethods, not as plain Lua functions. Accept either.
local function has(t, name)
    if not t then return false end
    local v = rawget(t, name)
    if v == nil then v = t[name] end  -- fallback for proxy tables
    if v == nil then return false end
    local tv = type(v)
    return tv == "function" or tv == "userdata"
end

local CAPS = {
    -- emu
    framecount   = emu and has(emu, "framecount"),
    -- BizHawk 2.x pauses through client.pause/unpause; emu.pause is checked
    -- too, for builds that have it.
    pause        = (emu and has(emu, "pause")) or (client and has(client, "pause")),
    unpause      = (emu and has(emu, "unpause")) or (client and has(client, "unpause")),
    ispaused     = client and has(client, "ispaused"),
    yield        = emu and has(emu, "yield"),
    frameadvance = emu and has(emu, "frameadvance"),
    -- client
    reboot_core  = client and has(client, "reboot_core"),
    screenshot   = client and has(client, "screenshot"),
    exit         = client and has(client, "exit"),
    -- savestate
    savestate_save = savestate and has(savestate, "save"),
    savestate_load = savestate and has(savestate, "load"),
    -- joypad
    joypad_set = joypad and has(joypad, "set"),
    joypad_get = joypad and has(joypad, "get"),
    -- memory
    memory_read_u8     = memory and has(memory, "read_u8"),
    memory_read_u16_le = memory and has(memory, "read_u16_le"),
    memory_read_u32_le = memory and has(memory, "read_u32_le"),
    memory_write_u8    = memory and has(memory, "write_u8"),
    memory_write_u16_le = memory and has(memory, "write_u16_le"),
    memory_write_u32_le = memory and has(memory, "write_u32_le"),
    memory_get_memorydomain_list   = memory and has(memory, "getmemorydomainlist"),
    memory_get_current_memorydomain = memory and has(memory, "getcurrentmemorydomain"),
    memory_use_memory_domain       = memory and has(memory, "usememorydomain"),
    memory_get_memorydomainsize    = memory and has(memory, "getmemorydomainsize"),
    -- bulk readers (vary by build) — used to make memory search fast; we fall
    -- back to a read_u8 loop when neither is present.
    memory_read_bytes_as_array     = memory and has(memory, "read_bytes_as_array"),
    memory_readbyterange           = memory and has(memory, "readbyterange"),
    -- one byte per byte as a Lua string: the compact form snapshots are kept in
    memory_read_bytes_as_binary_string = memory and has(memory, "read_bytes_as_binary_string"),
    -- mainmemory (subset of memory, scoped to system main RAM)
    mainmemory_read_u8 = mainmemory and has(mainmemory, "read_u8"),
    -- gameinfo
    gameinfo_getromname = gameinfo and has(gameinfo, "getromname"),
    gameinfo_getromhash = gameinfo and has(gameinfo, "getromhash"),
    -- breakpoints (memory callbacks) and CPU registers. Which callback kinds
    -- actually fire depends on the core: some don't implement read or exec hooks.
    on_bus_read      = event and has(event, "on_bus_read"),
    on_bus_write     = event and has(event, "on_bus_write"),
    on_bus_exec      = event and has(event, "on_bus_exec"),
    unregisterbyid   = event and has(event, "unregisterbyid"),
    getregisters     = emu and has(emu, "getregisters"),
    -- arbitrary Lua needs forms to show the human its confirmation dialog
    eval_dialog      = forms and has(forms, "newform") and has(forms, "checkbox") and has(forms, "ischecked"),
}

-- ── Command handlers ────────────────────────────────────────────────────────

local function cmd_ping(p) return "pong" end

-- BizHawk's memory.getmemorydomainlist() hands us a 0-indexed Lua table. The
-- JSON encoder (correctly) sees non-1-indexed keys and serializes it as an
-- OBJECT (`{"0":"CARTRAM","1":"WRAM",...}`) rather than an ARRAY. Re-pack into
-- a contiguous 1-indexed array so the client side sees a clean JSON array.
local function memory_domain_list()
    if not CAPS.memory_get_memorydomain_list then return nil end
    local raw = memory.getmemorydomainlist()
    local out = {}
    -- Could be 0-indexed or 1-indexed depending on build; walk by ipairs from
    -- 0 in case key 0 is the first slot, then continue with positive keys.
    local i = raw[0] ~= nil and 0 or 1
    while raw[i] ~= nil do
        out[#out + 1] = raw[i]
        i = i + 1
    end
    return out
end

local function cmd_get_info(p)
    return {
        rom_name      = CAPS.gameinfo_getromname and gameinfo.getromname() or nil,
        rom_hash      = CAPS.gameinfo_getromhash and gameinfo.getromhash() or nil,
        framecount    = CAPS.framecount and emu.framecount() or nil,
        memory_domains = memory_domain_list(),
        -- Which domain is read/written when a memory tool call omits `domain`.
        -- Useful because BizHawk's "current" domain can drift after savestate
        -- loads or other Lua scripts changing it underneath us.
        current_memory_domain = CAPS.memory_get_current_memorydomain
            and memory.getcurrentmemorydomain() or nil,
        capabilities  = CAPS,
    }
end

-- Memory r/w. By default operates on the current "main memory" domain;
-- pass `domain` in params to target a specific named domain (use
-- list_memory_domains to discover names — they vary per system).
local function in_domain(domain, fn)
    if not domain then return fn() end
    if not CAPS.memory_use_memory_domain then
        error("memory.usememorydomain not available on this BizHawk build")
    end
    local prev = memory.getcurrentmemorydomain and memory.getcurrentmemorydomain() or nil
    local ok = memory.usememorydomain(domain)
    if not ok then error("unknown memory domain: " .. tostring(domain)) end
    local r = fn()
    if prev then memory.usememorydomain(prev) end
    return r
end

-- Read a single value of the given width ("u8"/"u16"/"u32") at `address` in the
-- CURRENT memory domain. Little-endian (BizHawk's default), matching the
-- read16/read32 tools. Shared by read_memory_widthed and the memory search.
local function read_at_width(address, width)
    if     width == "u8"  then return memory.read_u8(address)
    elseif width == "u16" then return memory.read_u16_le(address)
    elseif width == "u32" then return memory.read_u32_le(address)
    else error("width must be 'u8', 'u16', or 'u32' (got " .. tostring(width) .. ")") end
end

local function cmd_read8(p)
    local addr = assert(p.address, "address required")
    return in_domain(p.domain, function() return memory.read_u8(addr) end)
end
local function cmd_read16(p)
    local addr = assert(p.address, "address required")
    return in_domain(p.domain, function() return memory.read_u16_le(addr) end)
end
local function cmd_read32(p)
    local addr = assert(p.address, "address required")
    return in_domain(p.domain, function() return memory.read_u32_le(addr) end)
end
local function cmd_write8(p)
    local addr = assert(p.address, "address required")
    local val  = assert(p.value,   "value required")
    in_domain(p.domain, function() memory.write_u8(addr, val) end)
    return true
end
local function cmd_write16(p)
    local addr = assert(p.address, "address required")
    local val  = assert(p.value,   "value required")
    in_domain(p.domain, function() memory.write_u16_le(addr, val) end)
    return true
end
local function cmd_write32(p)
    local addr = assert(p.address, "address required")
    local val  = assert(p.value,   "value required")
    in_domain(p.domain, function() memory.write_u32_le(addr, val) end)
    return true
end

local function cmd_read_range(p)
    local addr = assert(p.address, "address required")
    local len  = assert(p.length,  "length required")
    if len > 4096 then error("length exceeds 4096 byte limit") end
    return in_domain(p.domain, function()
        local bytes = {}
        for i = 0, len - 1 do bytes[i + 1] = memory.read_u8(addr + i) end
        return bytes
    end)
end

local function cmd_write_range(p)
    local addr  = assert(p.address, "address required")
    local bytes = assert(p.bytes,   "bytes required")
    if #bytes > 4096 then error("byte count exceeds 4096 limit") end
    return in_domain(p.domain, function()
        for i, b in ipairs(bytes) do memory.write_u8(addr + i - 1, b) end
        return { written = #bytes }
    end)
end

-- ── Memory search ───────────────────────────────────────────────────────────
--
-- Two modes, mirroring a Cheat-Engine "first scan" / "next scan" workflow:
--   * FIRST scan: no `addresses` given — sweep a contiguous region of the
--     domain for cells equal to `value` and return the matching offsets.
--   * NEXT scan: `addresses` given (the offsets a prior scan returned) — re-read
--     only those and keep the ones that STILL equal `value`. Exact and cheap.
--
-- The first scan briefly stalls the emulator (it runs synchronously inside one
-- bridge tick). We bulk-read the region in one engine call when the build
-- exposes a bulk reader, and fall back to a read_u8 loop (with a tighter cap)
-- otherwise.
local SEARCH_BULK_CAP   = 16 * 1024 * 1024  -- max bytes/scan via bulk read
local SEARCH_LOOP_CAP   = 262144            -- max bytes/scan via read_u8 fallback
local SEARCH_RESULT_MAX = 5000              -- hard ceiling on returned offsets

local function width_meta(width)
    if     width == "u8"  then return 1, 0xFF
    elseif width == "u16" then return 2, 0xFFFF
    elseif width == "u32" then return 4, 0xFFFFFFFF
    else error("width must be 'u8', 'u16', or 'u32' (got " .. tostring(width) .. ")") end
end

-- Bulk-read `len` bytes from `addr` in the CURRENT domain into a 1-indexed Lua
-- array, or return nil if this build exposes no bulk reader (caller loops).
-- BizHawk's bulk readers are 0-indexed; re-pack to 1-indexed.
local function bulk_read_bytes(addr, len)
    local raw
    if     CAPS.memory_read_bytes_as_array then raw = memory.read_bytes_as_array(addr, len)
    elseif CAPS.memory_readbyterange       then raw = memory.readbyterange(addr, len)
    else return nil end
    local out = {}
    local base = (raw[0] ~= nil) and 0 or 1
    for i = 0, len - 1 do out[i + 1] = raw[base + i] end
    return out
end

local function cmd_search_memory(p)
    local value = assert(p.value, "value required")
    local width = p.width or "u16"
    local wbytes, wmax = width_meta(width)
    if value < 0 or value > wmax then
        error(string.format("value %d out of range for width %s (0..%d)", value, width, wmax))
    end
    local cap = p.max_results or 200
    if cap < 1 then cap = 1 elseif cap > SEARCH_RESULT_MAX then cap = SEARCH_RESULT_MAX end

    -- NEXT scan: filter a caller-supplied candidate set.
    if p.addresses ~= nil then
        if type(p.addresses) ~= "table" then error("addresses must be an array of domain offsets") end
        return in_domain(p.domain, function()
            local kept, total = {}, 0
            for _, addr in ipairs(p.addresses) do
                if read_at_width(addr, width) == value then
                    total = total + 1
                    if #kept < cap then kept[#kept + 1] = addr end
                end
            end
            return { mode = "next", addresses = kept, count = total,
                     candidates = #p.addresses, truncated = total > #kept }
        end)
    end

    -- FIRST scan: sweep a contiguous region.
    local aligned = p.aligned
    if aligned == nil then aligned = true end
    local step  = aligned and wbytes or 1
    local start = p.start or 0
    if start < 0 then error("start must be >= 0") end

    local length = p.length
    if not length then
        if not CAPS.memory_get_memorydomainsize then
            error("no `length` given and memory.getmemorydomainsize is unavailable on this build — pass start+length to bound the scan")
        end
        local size = p.domain and memory.getmemorydomainsize(p.domain) or memory.getmemorydomainsize()
        length = size - start
    end
    if length < wbytes then error("scan range smaller than one " .. width .. " value") end

    local have_bulk = CAPS.memory_read_bytes_as_array or CAPS.memory_readbyterange
    local cap_bytes = have_bulk and SEARCH_BULK_CAP or SEARCH_LOOP_CAP
    if length > cap_bytes then
        error(string.format("scan length %d exceeds the per-call cap of %d bytes (%s) — narrow with start+length and chunk",
            length, cap_bytes, have_bulk and "bulk read" or "no bulk-read API on this build"))
    end

    return in_domain(p.domain, function()
        local bytes = bulk_read_bytes(start, length)  -- 1-indexed, or nil
        local matches, total = {}, 0
        local o = 0
        while o <= length - wbytes do
            local v
            if bytes then
                if     width == "u8"  then v = bytes[o + 1]
                elseif width == "u16" then v = bytes[o + 1] + bytes[o + 2] * 256
                else                       v = bytes[o + 1] + bytes[o + 2] * 256 + bytes[o + 3] * 65536 + bytes[o + 4] * 16777216 end
            else
                v = read_at_width(start + o, width)
            end
            if v == value then
                total = total + 1
                if #matches < cap then matches[#matches + 1] = start + o end
            end
            o = o + step
        end
        return { mode = "first", addresses = matches, count = total,
                 scanned = length, truncated = total > #matches }
    end)
end

-- ── Memory snapshots ────────────────────────────────────────────────────────
--
-- Named copies of a memory region, kept here in the bridge so unknown-value
-- searches (changed / increased / ...) never ship whole RAMs to the agent.
-- Comparison logic lives in lua/snapshot.lua (pure Lua, tested in CI).
-- Snapshots survive load_state: they live in this script, not the emulator.

local SNAPSHOT_MAX_BYTES     = 16 * 1024 * 1024  -- one snapshot
local SNAPSHOT_LOOP_MAX      = 262144            -- one snapshot without a bulk reader
local SNAPSHOT_STORE_BYTES   = 64 * 1024 * 1024  -- all snapshots together
local SNAPSHOT_MAX_CANDIDATES = 1000000          -- addresses per saved candidate set
local SNAPSHOT_RESULT_MAX    = 5000              -- matches listed per comparison

local snapshots = snapshot.new_store({
    max_total_bytes = SNAPSHOT_STORE_BYTES,
    max_candidates  = SNAPSHOT_MAX_CANDIDATES,
})

--- Read `length` bytes at `start` of the current domain as a binary string.
--- @return a Lua string of exactly `length` bytes. Raises when the region is
---         over the cap for the read method this build supports.
--- @requires memory.read_bytes_as_binary_string, else a bulk reader, else read_u8.
local function read_region_string(start, length)
    if CAPS.memory_read_bytes_as_binary_string then
        if length > SNAPSHOT_MAX_BYTES then
            error(string.format("snapshot length %d exceeds the %d-byte cap; snapshot a smaller range", length, SNAPSHOT_MAX_BYTES))
        end
        return memory.read_bytes_as_binary_string(start, length)
    end
    local bytes = (length <= SNAPSHOT_MAX_BYTES) and bulk_read_bytes(start, length) or nil
    if not bytes then
        if length > SNAPSHOT_LOOP_MAX then
            error(string.format("snapshot length %d exceeds the %d-byte cap for builds without a bulk reader", length, SNAPSHOT_LOOP_MAX))
        end
        bytes = {}
        for i = 1, length do bytes[i] = memory.read_u8(start + i - 1) end
    end
    local parts = {}
    for i = 1, length, 4096 do
        parts[#parts + 1] = string.char(table.unpack(bytes, i, math.min(i + 4095, length)))
    end
    return table.concat(parts)
end

--- Take a named snapshot of a domain region (default: the whole domain).
--- @return `{ name, domain, start, length, frame, used_bytes, max_bytes }`.
---         Raises for a missing name, bad range, or a full store.
--- @requires memory domain APIs; lua/snapshot.lua store.
local function cmd_snapshot_memory(p)
    local name = p.name
    if type(name) ~= "string" or name == "" then error("name required (a label like 'before_jump')") end
    local start = p.start or 0
    if start < 0 then error("start must be >= 0") end
    local domain = p.domain or (CAPS.memory_get_current_memorydomain and memory.getcurrentmemorydomain()) or nil
    return in_domain(p.domain, function()
        local size = CAPS.memory_get_memorydomainsize and memory.getmemorydomainsize() or nil
        local length = p.length or (size and size - start)
        if not length then error("no `length` given and this build can't report the domain size") end
        if length < 1 then error("length must be >= 1") end
        if size and start + length > size then
            error(string.format("range 0x%X+%d runs past the end of the domain (size %d)", start, length, size))
        end
        local rec = snapshots.put(name, {
            domain = domain, start = start, data = read_region_string(start, length),
            frame = CAPS.framecount and emu.framecount() or nil,
        })
        local info = snapshots.list()
        return { name = name, domain = rec.domain, start = start, length = #rec.data, frame = rec.frame,
                 used_bytes = info.used_bytes, max_bytes = info.max_bytes }
    end)
end

--- Compare two snapshots, optionally only at a saved candidate set's addresses.
--- @return `{ count, checked, results = {{address, before, after}...}, truncated, saved_as, saved_count }`.
---         Raises on unknown names, mismatched snapshots, or bad options.
--- @requires lua/snapshot.lua.
local function cmd_compare_snapshots(p)
    local before, after = snapshots.get(p.before), snapshots.get(p.after)
    local width = p.width or "u8"
    local cand_list
    if p.candidates then
        local c = snapshots.get_candidates(p.candidates)
        if c.domain ~= before.domain then
            error(string.format("candidate set '%s' is for domain '%s', but the snapshots are '%s'", p.candidates, tostring(c.domain), tostring(before.domain)))
        end
        cand_list = c.addresses
    end
    local max_results = p.max_results or 200
    if max_results < 1 then max_results = 1 elseif max_results > SNAPSHOT_RESULT_MAX then max_results = SNAPSHOT_RESULT_MAX end
    local aligned = p.aligned
    if aligned == nil then aligned = true end
    local r = snapshot.compare(before, after, {
        op = p.op, width = width, big_endian = p.big_endian == true, aligned = aligned, value = p.value,
        candidates = cand_list, max_results = max_results, max_candidates = SNAPSHOT_MAX_CANDIDATES,
    })
    local out = { count = r.count, checked = r.checked, results = r.results, truncated = r.count > #r.results,
                  domain = before.domain }
    if p.save_as then
        if not r.addresses then
            error(string.format("%d matches is over the %d-address limit for a candidate set; narrow the range first",
                r.count, SNAPSHOT_MAX_CANDIDATES))
        end
        snapshots.put_candidates(p.save_as, { domain = before.domain, width = width, addresses = r.addresses })
        out.saved_as, out.saved_count = p.save_as, #r.addresses
    end
    return out
end

--- List stored snapshots and candidate sets.
--- @return store.list() output.
--- @requires lua/snapshot.lua.
local function cmd_list_snapshots(p) return snapshots.list() end

--- Delete one snapshot / candidate set by name, or everything when name is omitted.
--- @return `{ deleted = n }`.
--- @requires lua/snapshot.lua.
local function cmd_delete_snapshots(p) return { deleted = snapshots.delete(p.name) } end

-- ── Byte-pattern / text / relative search ───────────────────────────────────
--
-- Searches live memory (read with the same reader and 16 MiB cap as
-- snapshots) or a stored snapshot. Logic lives in lua/bytesearch.lua.

local SEARCH_BYTES_RESULT_MAX = 1000

--- Read either a stored snapshot or a live domain region as a binary string.
--- @return data (string), base address, domain name. Raises on an unknown
---         snapshot, a negative start, or a range past the end of the domain.
--- @requires lua/snapshot.lua store; memory domain APIs for live reads.
local function region_or_snapshot(p)
    if p.snapshot then
        local rec = snapshots.get(p.snapshot)
        return rec.data, rec.start, rec.domain
    end
    local domain = p.domain or (CAPS.memory_get_current_memorydomain and memory.getcurrentmemorydomain()) or nil
    local start = p.start or 0
    if start < 0 then error("start must be >= 0") end
    local data
    in_domain(p.domain, function()
        local size = CAPS.memory_get_memorydomainsize and memory.getmemorydomainsize() or nil
        local length = p.length or (size and size - start)
        if not length then error("no `length` given and this build can't report the domain size") end
        if length < 1 then error("length must be >= 1") end
        if size and start + length > size then
            error(string.format("range 0x%X+%d runs past the end of the domain (size %d)", start, length, size))
        end
        data = read_region_string(start, length)
    end)
    return data, start, domain
end

--- Write a memory region (or a stored snapshot) to a file.
---
--- WHY THIS USES io: dumps exist so external tools can read raw memory
--- without the bytes crossing the socket or the agent's context. The Node
--- side (src/dumps.ts) only sends paths inside the user's BIZHAWK_DUMP_DIR,
--- with a data extension, after its own checks; this handler writes exactly
--- the requested bytes in binary mode and nothing else. The sandbox never
--- sees `io`.
--- @return `{ bytes, domain, start, frame, snapshot }`. Raises when the path is
---         missing, the file can't be opened, or the region is invalid.
--- @requires io (host side only); memory APIs or the snapshot store.
local function cmd_dump_memory(p)
    if type(p.path) ~= "string" or p.path == "" then error("path required") end
    local data, start, domain = region_or_snapshot(p)
    local f, err = io.open(p.path, "wb")
    if not f then error("can't open dump file: " .. tostring(err)) end
    local ok, werr = f:write(data)
    f:close()
    if not ok then error("writing the dump failed: " .. tostring(werr)) end
    return { bytes = #data, domain = domain, start = start, snapshot = p.snapshot,
             frame = CAPS.framecount and emu.framecount() or nil }
end

--- Search a domain region or a stored snapshot for bytes, text or relative text.
--- @return `{ count, matches = {{address, preview, mapping?}...}, scanned, domain, truncated }`.
---         Raises on a bad pattern/mode, an unknown snapshot, or a bad range.
--- @requires lua/bytesearch.lua; memory domain APIs for live searches.
local function cmd_search_bytes(p)
    local max = p.max_results or 50
    if max < 1 then max = 1 elseif max > SEARCH_BYTES_RESULT_MAX then max = SEARCH_BYTES_RESULT_MAX end
    local opts = { mode = p.mode, pattern = p.pattern, text = p.text, max_results = max }

    local data, base, domain = region_or_snapshot(p)
    local r = bytesearch.search(data, base, opts)
    r.domain, r.truncated = domain, r.count > #r.matches
    return r
end

local function cmd_list_memory_domains(p)
    if not CAPS.memory_get_memorydomain_list then
        error("memory.getmemorydomainlist not available")
    end
    return memory_domain_list()
end

-- Joypad. p.buttons is a table like {A=true, Up=true, Start=true}.
-- p.player defaults to 1.
--
-- joypad.set(buttons, N) prefixes every name with "PN " ("P1 Start"). Cores
-- with one controller (Game Boy / GBC on Gambatte, among others) name their
-- buttons without a prefix ("Start"), so passing a player number there
-- silently drops ALL input. Detect the naming once per call from joypad.get().

--- Whether the loaded core names buttons per player ("P1 Start").
--- @return true for multi-controller cores, false for single-controller ones
---         (and when joypad.get is unavailable, where we keep the old behavior).
--- @requires joypad.get (optional).
local function core_has_player_prefixes()
    if not CAPS.joypad_get then return true end
    local ok, keys = pcall(joypad.get)
    if not ok or type(keys) ~= "table" then return true end
    for name in pairs(keys) do
        if type(name) == "string" and name:match("^P%d+ ") then return true end
    end
    return false
end

--- Apply one player's buttons for the next frame.
--- @return nothing. Raises when the core has no player `player` (player > 1
---         on a single-controller core) or joypad.set is unavailable.
--- @requires joypad.set; joypad.get to detect the naming scheme.
local function set_joypad(buttons, player)
    if not CAPS.joypad_set then error("joypad.set not available") end
    player = player or 1
    if core_has_player_prefixes() then
        joypad.set(buttons or {}, player)
    elseif player == 1 then
        joypad.set(buttons or {})
    else
        error(string.format("this core has a single controller; player %d doesn't exist", player))
    end
end

local function cmd_press_buttons(p)
    local buttons = assert(p.buttons, "buttons required (table like {A=true, Up=true})")
    set_joypad(buttons, p.player or 1)
    return true
end

-- Set several controllers for the SAME upcoming frame. `p.players` is an array
-- of { player = N, buttons = {A=true, ...} }. The bridge does exactly one
-- emu.frameadvance() per tick, so two separate press_buttons calls land on
-- DIFFERENT frames; applying them all here, before the next single frame
-- advance, makes them act simultaneously (e.g. P1 + P2 in a 2-player game).
local function cmd_press_buttons_multi(p)
    local players = assert(p.players, "players required (array of {player, buttons})")
    if type(players) ~= "table" then error("players must be an array") end
    local applied = {}
    for _, entry in ipairs(players) do
        local player  = (type(entry) == "table" and entry.player)  or 1
        local buttons = (type(entry) == "table" and entry.buttons) or {}
        set_joypad(buttons, player)
        applied[#applied + 1] = player
    end
    return { players = applied }
end

-- Batched input playback. Takes a list of per-frame inputs and runs them
-- entirely server-side — one bridge round-trip per BATCH instead of per
-- frame. Each frame entry is `{ buttons = {A=true,...}, player = 1 }`.
-- The bridge does NOT poll its outer loop during this call, so other RPCs
-- (and the heartbeat) are stalled until the batch finishes. For
-- responsiveness, callers should chunk long sequences into multiple
-- play_input_sequence calls of a few hundred frames each.
--
-- Optional observation params (since v0.1.4):
--   p.screenshot_every  — capture a PNG every N frames (also captures the
--                         final frame regardless of remainder). Requires
--                         client.screenshot to be available on this build.
--   p.screenshot_dir    — directory for PNGs (default: C:/temp on Windows,
--                         /tmp elsewhere; caller should ensure it exists)
--   p.screenshot_prefix — filename prefix (default: "obs")
--
-- Optional observation params (since v0.1.5):
--   p.observe_memory    — array of {name, domain, address, width} reads
--                         to perform at each observation point. width is
--                         "u8" | "u16" | "u32". Results land in each
--                         observation as `memory = { name = value, ... }`.
--   p.stop_on_memory_change — {domain, address, width} — record this
--                         value at the start, abort play the moment it
--                         changes. Result includes stopped_early=true and
--                         stop_reason='memory_changed'. The final
--                         observation (if screenshot/memory enabled) is
--                         always captured at the stop frame.
local function pause()
    if not CAPS.pause then error("neither emu.pause nor client.pause is available") end
    if emu and has(emu, "pause") then emu.pause() else client.pause() end
end
local function unpause()
    if not CAPS.unpause then error("neither emu.unpause nor client.unpause is available") end
    if emu and has(emu, "unpause") then emu.unpause() else client.unpause() end
end
local function is_paused()
    return CAPS.ispaused and client.ispaused() == true
end

-- emu.frameadvance() waits while emulation is paused, so stepping from a
-- paused state unpauses for the steps and pauses again afterwards.
local function while_running(fn)
    local was_paused = is_paused()
    if was_paused then unpause() end
    local ok, r = pcall(fn)
    if was_paused then pause() end
    if not ok then error(r, 0) end
    return r
end

local function read_memory_widthed(domain, address, width)
    return in_domain(domain, function() return read_at_width(address, width) end)
end

local function cmd_play_input_sequence(p)
    if not CAPS.joypad_set    then error("joypad.set not available") end
    if not CAPS.frameadvance  then error("emu.frameadvance not available") end
    local frames = assert(p.frames, "frames required (array of {buttons, player?} objects)")
    if type(frames) ~= "table" then error("frames must be an array") end

    local screenshot_every  = p.screenshot_every
    local screenshot_dir    = p.screenshot_dir    or "C:/temp"
    local screenshot_prefix = p.screenshot_prefix or "obs"
    if screenshot_every and not CAPS.screenshot then
        error("screenshot_every requested but client.screenshot is not available on this build")
    end

    local observe_memory = p.observe_memory  -- nil or array of {name, domain, address, width}
    local stop_spec      = p.stop_on_memory_change  -- nil or {domain, address, width}

    -- Capture initial value for stop-on-change
    local stop_initial = nil
    if stop_spec then
        stop_initial = read_memory_widthed(stop_spec.domain, stop_spec.address, stop_spec.width)
    end

    local observations = {}
    local count   = 0
    local total   = #frames
    local stopped = false
    local stop_reason = nil

    -- Helper: capture an observation at the current frame.
    local function capture_observation()
        local obs = { frame_offset = count }
        if screenshot_every then
            local path = string.format("%s/%s-%04d.png", screenshot_dir, screenshot_prefix, count)
            client.screenshot(path)
            obs.path = path
        end
        if observe_memory then
            local mem = {}
            for _, spec in ipairs(observe_memory) do
                mem[spec.name] = read_memory_widthed(spec.domain, spec.address, spec.width)
            end
            obs.memory = mem
        end
        table.insert(observations, obs)
    end

    local was_paused = is_paused()
    if was_paused then unpause() end
    for i, frame in ipairs(frames) do
        local buttons = (type(frame) == "table" and frame.buttons) or {}
        local player  = (type(frame) == "table" and frame.player) or 1
        set_joypad(buttons, player)
        emu.frameadvance()
        count = count + 1

        -- Check stop condition AFTER the frame advances
        if stop_spec then
            local cur = read_memory_widthed(stop_spec.domain, stop_spec.address, stop_spec.width)
            if cur ~= stop_initial then
                stopped = true
                stop_reason = "memory_changed"
                -- Always capture the stop frame as an observation (regardless of screenshot_every cadence)
                if screenshot_every or observe_memory then
                    capture_observation()
                end
                break
            end
        end

        -- Periodic observation (only if we didn't already capture for stop)
        if (screenshot_every or observe_memory) and (count % (screenshot_every or 0xFFFF) == 0 or count == total) then
            capture_observation()
        end
    end

    if was_paused then pause() end
    return {
        played = count,
        final_framecount = CAPS.framecount and emu.framecount() or nil,
        observations = observations,
        stopped_early = stopped,
        stop_reason = stop_reason,
    }
end

local function cmd_pause(p) pause(); return true end
local function cmd_unpause(p) unpause(); return true end
local function cmd_frame_advance(p)
    if not CAPS.frameadvance then error("emu.frameadvance not available") end
    local n = p.count or 1
    return while_running(function()
        for _ = 1, n do emu.frameadvance() end
        return CAPS.framecount and emu.framecount() or nil
    end)
end
-- Defined in the breakpoint section below (which comes later in this file):
-- re-registers every breakpoint's callbacks after the core is rebuilt.
local rearm_all_breakpoints

--- Hard-reset the core, then re-register every breakpoint: rebooting the core
--- discards its memory callbacks, which would otherwise leave breakpoints
--- listed but silent.
--- @return table { rearmed = N, failed = {[id] = message} }; errors if
---         client.reboot_core is missing.
--- @requires BizHawk `client.reboot_core`; rearm_all_breakpoints.
local function cmd_reset(p)
    if not CAPS.reboot_core then error("client.reboot_core not available") end
    client.reboot_core()
    local rearmed, failed = rearm_all_breakpoints()
    return { rearmed = rearmed, failed = failed }
end

-- Set by cmd_close; the main loop calls client.exit() once the RESULT for the
-- close command has gone out, so the caller gets a reply instead of a timeout.
local exit_requested = false

--- Ask BizHawk to shut down after this command's reply has been sent.
--- @return true once the exit is scheduled; errors if client.exit is missing.
--- @requires BizHawk `client.exit` (2.x); the actual exit happens in the main loop.
local function cmd_close(p)
    if not CAPS.exit then error("client.exit not available") end
    exit_requested = true
    return true
end

local function cmd_screenshot(p)
    if not CAPS.screenshot then error("client.screenshot not available") end
    local path = assert(p.path, "path required (BizHawk's client.screenshot needs an explicit path)")
    client.screenshot(path)
    return path
end

local function cmd_save_state(p)
    if not CAPS.savestate_save then error("savestate.save not available") end
    local path = assert(p.path, "path required (BizHawk savestates are file-based)")
    savestate.save(path)
    return { path = path }
end
local function cmd_load_state(p)
    if not CAPS.savestate_load then error("savestate.load not available") end
    local path = assert(p.path, "path required")
    savestate.load(path)
    return { path = path }
end

-- ── Breakpoints ─────────────────────────────────────────────────────────────
--
-- BizHawk's Lua can't halt the CPU mid-instruction, so these are LOGGING
-- breakpoints: a memory callback (event.on_bus_read/write/exec) records every
-- hit with the CPU registers, the top of the stack and any extra memory reads
-- the caller asked for, and can pause emulation (which takes effect at the end
-- of the current frame). Hits are kept in a ring buffer per breakpoint; fetch
-- them with get_breakpoint_hits. Callbacks run mid-frame, so they read memory
-- with an explicit domain argument rather than switching the current domain.
local BREAKPOINTS = {}       -- id -> breakpoint
local next_breakpoint_id = 1
local BREAKPOINT_MAX_HITS = 10000
local BREAKPOINT_MAX_LENGTH = 256
local BREAKPOINT_KINDS = { read = "on_bus_read", write = "on_bus_write", exec = "on_bus_exec" }

local function read_in(domain, address, width)
    if     width == "u8"  then return memory.read_u8(address, domain)
    elseif width == "u16" then return memory.read_u16_le(address, domain)
    elseif width == "u32" then return memory.read_u32_le(address, domain)
    else error("width must be 'u8', 'u16', or 'u32' (got " .. tostring(width) .. ")") end
end

local function list_domains_set()
    local set = {}
    for _, d in ipairs(memory_domain_list() or {}) do set[d] = true end
    return set
end

local function record_hit(bp, addr, val, flags)
    bp.hit_count = bp.hit_count + 1
    local hit = { n = bp.hit_count, address = addr, value = val, flags = flags,
                  frame = CAPS.framecount and emu.framecount() or nil }
    if CAPS.getregisters then
        local ok, regs = pcall(emu.getregisters)
        if ok then hit.registers = regs end
    end
    if bp.stack_depth > 0 and hit.registers and hit.registers.SP and bp.stack_domain then
        local stack = {}
        for i = 0, bp.stack_depth - 1 do
            local ok, w = pcall(memory.read_u16_le, hit.registers.SP + 2 * i, bp.stack_domain)
            if not ok then break end
            stack[#stack + 1] = w
        end
        hit.stack = stack
    end
    if bp.capture then
        local mem = {}
        for _, spec in ipairs(bp.capture) do
            local ok, v = pcall(read_in, spec.domain, spec.address, spec.width or "u8")
            mem[spec.name] = ok and v or nil
        end
        hit.memory = mem
    end
    local hits = bp.hits
    hits[#hits + 1] = hit
    if #hits > bp.max_hits then table.remove(hits, 1); bp.dropped = bp.dropped + 1 end
    if bp.pause_on_hit and CAPS.pause then pause() end
end

local function breakpoint_summary(bp)
    return { id = bp.id, kind = bp.kind, address = bp.address, length = bp.length, scope = bp.scope,
             pause_on_hit = bp.pause_on_hit, hit_count = bp.hit_count, buffered = #bp.hits,
             dropped = bp.dropped, max_hits = bp.max_hits, label = bp.label }
end

--- Register a breakpoint's memory callbacks (one per address) with BizHawk.
--- Memory callbacks belong to the emulator core, so they are lost when the
--- core is rebuilt (client.reboot_core); cmd_reset calls this again for every
--- breakpoint. Old callback ids are unregistered first, in case they survive.
--- @return nil; errors (after unregistering any half-done registration)
---         if a callback can't be registered.
--- @requires BizHawk `event` callbacks (bp.register) and event.unregisterbyid.
local function arm_breakpoint(bp)
    for _, g in ipairs(bp.guids) do pcall(event.unregisterbyid, g) end
    bp.guids = {}
    local function on_hit(addr, val, flags)
        pcall(record_hit, bp, addr, val, flags)
    end
    for a = bp.address, bp.address + bp.length - 1 do
        local name = string.format("mcp-bizhawk bp %d %s %X", bp.id, bp.kind, a)
        local ok, guid = pcall(event[bp.register], on_hit, a, name, bp.scope)
        if not ok then
            for _, g in ipairs(bp.guids) do pcall(event.unregisterbyid, g) end
            bp.guids = {}
            error("couldn't register the " .. bp.kind .. " callback at " .. string.format("0x%X", a) .. ": " .. tostring(guid))
        end
        bp.guids[#bp.guids + 1] = guid
    end
end

--- Re-register the callbacks of every breakpoint (after client.reboot_core).
--- @return number of breakpoints re-armed, and a table {[id] = message} of
---         those that couldn't be (they stay listed, without callbacks).
--- @requires BREAKPOINTS, arm_breakpoint.
rearm_all_breakpoints = function()
    local rearmed, failed = 0, {}
    for id, bp in pairs(BREAKPOINTS) do
        local ok, err = pcall(arm_breakpoint, bp)
        if ok then rearmed = rearmed + 1 else failed[tostring(id)] = tostring(err) end
    end
    return rearmed, failed
end

local function cmd_add_breakpoint(p)
    local kind = assert(p.kind, "kind required: 'read', 'write', or 'exec'")
    local register = BREAKPOINT_KINDS[kind]
    if not register then error("kind must be 'read', 'write', or 'exec' (got " .. tostring(kind) .. ")") end
    if not CAPS[register] then error("event." .. register .. " not available on this BizHawk build") end
    if not CAPS.unregisterbyid then error("event.unregisterbyid not available, so breakpoints couldn't be removed") end
    local address = assert(p.address, "address required")
    local length = p.length or 1
    if length < 1 or length > BREAKPOINT_MAX_LENGTH then
        error(string.format("length must be 1..%d (got %s)", BREAKPOINT_MAX_LENGTH, tostring(length)))
    end
    local max_hits = p.max_hits or 100
    if max_hits < 1 or max_hits > BREAKPOINT_MAX_HITS then
        error(string.format("max_hits must be 1..%d (got %s)", BREAKPOINT_MAX_HITS, tostring(max_hits)))
    end
    local domains = list_domains_set()
    if p.scope and not domains[p.scope] then error("unknown scope (memory domain): " .. tostring(p.scope)) end
    local stack_domain = p.stack_domain or (domains["System Bus"] and "System Bus" or nil)
    if p.capture ~= nil then
        if type(p.capture) ~= "table" then error("capture must be an array of {name, domain, address, width}") end
        for _, spec in ipairs(p.capture) do
            if type(spec) ~= "table" or not spec.name or not spec.domain or not spec.address then
                error("each capture entry needs name, domain and address")
            end
            if not domains[spec.domain] then error("unknown capture domain: " .. tostring(spec.domain)) end
        end
    end
    local bp = {
        id = next_breakpoint_id, kind = kind, register = register, address = address, length = length,
        scope = p.scope, pause_on_hit = p.pause_on_hit == true, max_hits = max_hits, label = p.label,
        stack_depth = math.max(0, math.min(p.stack_depth or 4, 16)), stack_domain = stack_domain,
        capture = p.capture, hits = {}, hit_count = 0, dropped = 0, guids = {},
    }
    arm_breakpoint(bp)
    next_breakpoint_id = next_breakpoint_id + 1
    BREAKPOINTS[bp.id] = bp
    return breakpoint_summary(bp)
end

local function remove_breakpoint(bp)
    for _, g in ipairs(bp.guids) do pcall(event.unregisterbyid, g) end
    BREAKPOINTS[bp.id] = nil
end

local function cmd_remove_breakpoint(p)
    if p.id == nil then
        local removed = 0
        for _, bp in pairs(BREAKPOINTS) do remove_breakpoint(bp); removed = removed + 1 end
        return { removed = removed }
    end
    local bp = BREAKPOINTS[p.id]
    if not bp then error("no breakpoint with id " .. tostring(p.id)) end
    remove_breakpoint(bp)
    return { removed = 1 }
end

local function sorted_breakpoints()
    local list = {}
    for _, bp in pairs(BREAKPOINTS) do list[#list + 1] = bp end
    table.sort(list, function(x, y) return x.id < y.id end)
    return list
end

local function cmd_list_breakpoints(p)
    local out = {}
    for _, bp in ipairs(sorted_breakpoints()) do out[#out + 1] = breakpoint_summary(bp) end
    return out
end

local function cmd_get_breakpoint_hits(p)
    local bp = BREAKPOINTS[assert(p.id, "id required")]
    if not bp then error("no breakpoint with id " .. tostring(p.id)) end
    local limit = p.limit or #bp.hits
    local hits = {}
    for i = math.max(1, #bp.hits - limit + 1), #bp.hits do hits[#hits + 1] = bp.hits[i] end
    local summary = breakpoint_summary(bp)
    summary.hits = hits
    if p.clear then bp.hits = {}; bp.dropped = 0 end
    return summary
end

-- ── Arbitrary Lua (opt-in, sandboxed) ───────────────────────────────────────
--
-- Two switches, both off by default:
--   1. The MCP server only offers bizhawk_eval_lua when BIZHAWK_EVAL_LUA=1 is
--      set in its config; otherwise eval_lua is never sent here at all.
--   2. Here, sandbox.lua is loaded on the first eval_lua call. That call opens
--      the confirmation dialog and fails; only a person ticking the checkbox
--      and clicking Accept opens the gate. See lua/sandbox.lua for every limit.

--- Play the OS "Asterisk" sound so the human notices the confirmation dialog.
---
--- WHY THIS NEEDS .NET ACCESS: BizHawk's Lua API has no sound function, so this
--- reaches System.Media.SystemSounds through NLua's `luanet`. It runs on the
--- bridge (host) side only, imports exactly one type, plays one fixed sound, and
--- takes no input. `luanet` itself is never handed to the sandbox.
--- @return nothing. Silently does nothing if luanet or the type is missing
---         (e.g. on Linux/Mono, where the call may also be silent).
--- @requires NLua `luanet` (load_assembly, import_type); Windows for audible output.
local function play_attention_sound()
    if type(luanet) ~= "table" then return end
    pcall(function()
        luanet.load_assembly("System")
        local SystemSounds = luanet.import_type("System.Media.SystemSounds")
        if SystemSounds then SystemSounds.Asterisk:Play() end
    end)
end

local sandbox, eval_gate = nil, nil

--- Load lua/sandbox.lua and build the consent gate on first use.
--- @return the sandbox module and its gate (the same ones on every later call).
---         Raises if sandbox.lua can't be found next to bridge.lua.
--- @requires lua/sandbox.lua; BizHawk `forms`; play_attention_sound.
local function load_sandbox()
    if not sandbox then
        sandbox = require("sandbox")
        eval_gate = sandbox.new_gate({
            forms  = forms,
            has    = has,
            now    = os.time,
            notify = play_attention_sound,
        })
    end
    return sandbox, eval_gate
end

-- Libraries handed to the sandbox. sandbox.lua copies only whitelisted names.
local SANDBOX_HOST = {
    memory = memory, mainmemory = mainmemory, emu = emu, gui = gui,
    client = client, gameinfo = gameinfo, bit = bit, has = has,
}

local EVAL_DEFAULT_INSTRUCTIONS = 1000000
local EVAL_MAX_INSTRUCTIONS     = 50000000
local EVAL_DEFAULT_MEMORY_KB    = 16384
local EVAL_MAX_MEMORY_KB        = 65536

--- Clamp an optional numeric option to [1, max], falling back to `default`.
--- @return an integer within range.
--- @requires Nothing.
local function clamp_opt(v, default, max)
    v = tonumber(v) or default
    if v < 1 then v = 1 end
    if v > max then v = max end
    return math.floor(v)
end

--- Run caller-supplied Lua in the sandbox, if the human has enabled it.
--- @return `{ values, count, output, instructions }` from sandbox.run. Errors
---         when the gate is closed (opening the dialog), when `code` is missing,
---         or with the script's own compile/runtime/budget error.
--- @requires lua/sandbox.lua; BizHawk `forms` for the confirmation dialog.
local function cmd_eval_lua(p)
    local sandbox, eval_gate = load_sandbox()
    if not eval_gate.enabled() then
        local state, wait = eval_gate.request()
        if state == "opened" or state == "open" then
            error("arbitrary Lua is disabled. A confirmation dialog is open in BizHawk; only the person " ..
                  "at the computer can enable it (tick the checkbox, then click Accept). Retry afterwards.")
        elseif state == "cooldown" then
            error(string.format("arbitrary Lua is disabled: the confirmation dialog was closed or " ..
                  "declined, and it won't reopen for another %d s. Don't ask the user to enable it.", wait))
        end
        error("arbitrary Lua is unavailable: this BizHawk build has no forms API for the confirmation dialog")
    end
    if type(p.code) ~= "string" or p.code == "" then error("code required (a string of Lua source)") end
    return sandbox.run(p.code, {
        max_instructions = clamp_opt(p.max_instructions, EVAL_DEFAULT_INSTRUCTIONS, EVAL_MAX_INSTRUCTIONS),
        max_memory_kb    = clamp_opt(p.max_memory_kb,    EVAL_DEFAULT_MEMORY_KB,    EVAL_MAX_MEMORY_KB),
    }, SANDBOX_HOST)
end

local function cmd_get_registers(p)
    if not CAPS.getregisters then error("emu.getregisters not available on this BizHawk build") end
    return emu.getregisters()
end

-- ── Dispatch table ──────────────────────────────────────────────────────────

local HANDLERS = {
    ping                 = cmd_ping,
    get_info             = cmd_get_info,
    read8                = cmd_read8,
    read16               = cmd_read16,
    read32               = cmd_read32,
    write8               = cmd_write8,
    write16              = cmd_write16,
    write32              = cmd_write32,
    read_range           = cmd_read_range,
    write_range          = cmd_write_range,
    search_memory        = cmd_search_memory,
    snapshot_memory      = cmd_snapshot_memory,
    compare_snapshots    = cmd_compare_snapshots,
    list_snapshots       = cmd_list_snapshots,
    delete_snapshots     = cmd_delete_snapshots,
    search_bytes         = cmd_search_bytes,
    dump_memory          = cmd_dump_memory,
    list_memory_domains  = cmd_list_memory_domains,
    press_buttons        = cmd_press_buttons,
    press_buttons_multi  = cmd_press_buttons_multi,
    play_input_sequence  = cmd_play_input_sequence,
    pause                = cmd_pause,
    unpause              = cmd_unpause,
    frame_advance        = cmd_frame_advance,
    reset                = cmd_reset,
    close                = cmd_close,
    screenshot           = cmd_screenshot,
    save_state           = cmd_save_state,
    load_state           = cmd_load_state,
    add_breakpoint       = cmd_add_breakpoint,
    remove_breakpoint    = cmd_remove_breakpoint,
    list_breakpoints     = cmd_list_breakpoints,
    get_breakpoint_hits  = cmd_get_breakpoint_hits,
    get_registers        = cmd_get_registers,
    eval_lua             = cmd_eval_lua,
}

local function dispatch(cmd)
    if not cmd.method then
        return nil, { code = -32600, message = "missing method field" }
    end
    local handler = HANDLERS[cmd.method]
    if not handler then
        return nil, { code = -32601, message = "unknown method: " .. cmd.method }
    end
    local ok, result = pcall(handler, cmd.params or {})
    if not ok then
        return nil, { code = -32603, message = tostring(result) }
    end
    return result, nil
end

-- ── Per-frame round trip with the external server ──────────────────────────
--
-- API notes (from BizHawk 2.11's Lua/_docs_luacats/comm.d.lua):
--   * `comm.socketServerSend(s)` returns an INTEGER (status / bytes sent).
--     Pure send — does NOT return the reply.
--   * `comm.socketServerResponse()` reads the next message from the socket.
--     CRITICAL: "all responses must be of the form `{msg.Length:D} {msg}`
--     i.e. prefixed with the length in base-10 and a space." That applies
--     to INCOMING messages too — BizHawk's parser silently discards lines
--     that aren't length-prefixed. The Node side prefixes everything before
--     writing, so by the time the response gets here BizHawk has already
--     stripped the prefix and we receive just `<msg>` (no length, no newline).
--
-- Pattern: receive at start of frame (reads response to previous frame's
-- send), then send for this frame. Decouples send and receive across a full
-- frame boundary (~16ms) so the receive timeout doesn't have to be tuned to
-- network round-trip time.
local function tick()
    -- Step 1: receive response from PREVIOUS frame's send (if any).
    local incoming = comm.socketServerResponse()

    if incoming and type(incoming) == "string" and #incoming > 0 then
        incoming = incoming:gsub("[\r\n]+$", "")
        if incoming ~= "NONE" and #incoming > 0 then
            local parse_ok, cmd = pcall(json.decode, incoming)
            if parse_ok and type(cmd) == "table" then
                local result, rpc_err = dispatch(cmd)
                if rpc_err then
                    pending_result = { id = cmd.id, error = rpc_err }
                else
                    pending_result = { id = cmd.id, result = result }
                end
            else
                pending_result = { id = nil, error = { code = -32700, message = "parse error" } }
            end
        end
    end

    -- Step 2: send for THIS frame.
    local outgoing
    if pending_result then
        outgoing = "RESULT " .. json.encode(pending_result)
        pending_result = nil
    else
        outgoing = "READY"
    end
    comm.socketServerSend(outgoing .. "\n")
end

-- ── Startup ────────────────────────────────────────────────────────────────

console.log("[mcp-bizhawk] bridge starting")

if not (comm and comm.socketServerSend and comm.socketServerResponse) then
    console.log("[mcp-bizhawk] FATAL: comm.socketServer* not available — launch BizHawk with --socket_ip / --socket_port flags pointing at a running mcp-bizhawk server")
    return
end

-- BizHawk 2.11 only creates its socket client when EmuHawk is started with
-- --socket_ip / --socket_port. Opened from the GUI, there is no socket at all:
-- socketServerGetIp returns nil and the other socket calls throw. Lua can't
-- create it (socketServerSetIp/SetPort throw too), so explain and stop.
local ip   = comm.socketServerGetIp   and comm.socketServerGetIp()   or nil
local port = comm.socketServerGetPort and comm.socketServerGetPort() or nil
if ip == nil then
    console.log("[mcp-bizhawk] STOPPED: BizHawk's socket isn't set up. It only exists when EmuHawk is")
    console.log("[mcp-bizhawk] started with these flags, which can't be set from the menus or from Lua:")
    console.log("[mcp-bizhawk]     EmuHawk.exe --socket_ip=127.0.0.1 --socket_port=8766 --lua=<path>\\bridge.lua <rom>")
    console.log("[mcp-bizhawk] Easiest fixes: use a shortcut whose Target has those flags (see the README),")
    console.log("[mcp-bizhawk] or let the AI start BizHawk with the bizhawk_launch tool.")
    console.log("[mcp-bizhawk] Use the port your mcp-bizhawk server listens on (BIZHAWK_PORT, default 8766).")
    return
end
console.log(string.format("[mcp-bizhawk] socket server target: %s:%s", tostring(ip), tostring(port)))

-- Receive timeout for socketServerResponse(). Default BizHawk timeout (5ms)
-- is fine in steady state — replies are sitting in the socket buffer by the
-- time we poll for them — but a slightly longer window costs us nothing and
-- absorbs occasional jitter (GC pause on the Node side, OS scheduling, etc).
if comm.socketServerSetTimeout then
    comm.socketServerSetTimeout(50)
    console.log("[mcp-bizhawk] socket receive timeout set to 50ms")
end

console.log("[mcp-bizhawk] frame loop active — bridge is polling once per frame")

-- Per-frame poll. Run forever.
--- Ask BizHawk whether the socket is connected, without letting a .NET
--- exception (e.g. a missing socket client) kill the bridge.
--- @return boolean, or nil when BizHawk can't say.
--- @requires comm.socketServerIsConnected (optional).
local function socket_connected()
    if not comm.socketServerIsConnected then return nil end
    local ok, connected = pcall(comm.socketServerIsConnected)
    if ok then return connected end
    return nil
end

local last_connection_state = socket_connected()
local tick_count = 0
while true do
    tick_count = tick_count + 1

    -- Watch for connection state changes (cheap to check once per second).
    if tick_count % 60 == 0 then
        local connected = socket_connected()
        if connected ~= nil and connected ~= last_connection_state then
            console.log("[mcp-bizhawk] socket connected = " .. tostring(connected))
            last_connection_state = connected
        end
    end

    tick()
    -- tick() has already sent the RESULT for a close command, so it's safe to go.
    if exit_requested then
        console.log("[mcp-bizhawk] close requested — exiting BizHawk")
        client.exit()
        break
    end
    -- While paused, emu.frameadvance() doesn't return, which would stop the
    -- bridge answering until someone unpaused by hand; emu.yield() lets the
    -- script keep polling without advancing a frame.
    if CAPS.yield and is_paused() then emu.yield() else emu.frameadvance() end
end
