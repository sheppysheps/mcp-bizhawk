-- snapshot.lua: memory snapshots and comparisons for mcp-bizhawk
--
-- WHY THIS FILE EXISTS
-- Finding an unknown value ("which byte is HP?") means comparing memory
-- before and after something happens, the way BizHawk's RAM Search does. Its
-- GUI can't be driven from Lua, so this rebuilds the logic. Snapshots stay
-- here inside BizHawk as compact strings (one byte per byte), and comparisons
-- run here too; only counts and short candidate lists go back to the agent,
-- so a 32 KB WRAM diff never passes through its context.
--
-- This module is pure Lua: no BizHawk APIs. bridge.lua reads memory and hands
-- the bytes in, which lets test/lua run these checks on stock Lua 5.4.

local M = {}

-- Comparison operators. `value` is the extra argument where one is needed.
local OPS = {
    changed    = function(a, b)    return a ~= b end,
    unchanged  = function(a, b)    return a == b end,
    increased  = function(a, b)    return b > a end,
    decreased  = function(a, b)    return b < a end,
    changed_by = function(a, b, v) return b - a == v end,
    equal_to   = function(_, b, v) return b == v end,
}
M.OPS = OPS

-- string.unpack formats per width: unsigned / signed.
local FORMATS = {
    u8 = "I1", u16 = "I2", u32 = "I4",
    s8 = "i1", s16 = "i2", s32 = "i4",
}

--- Size in bytes of a width name.
--- @return 1, 2 or 4; raises for an unknown width.
--- @requires Nothing.
local function width_bytes(width)
    local f = FORMATS[width]
    if not f then error("width must be one of u8, u16, u32, s8, s16, s32 (got " .. tostring(width) .. ")", 0) end
    return tonumber(f:sub(2))
end
M.width_bytes = width_bytes

--- Create a snapshot store with fixed memory limits.
--- @return store with put/get/delete/list for snapshots and candidate sets.
---         put raises when a limit would be exceeded (nothing is stored then).
--- @requires Nothing.
function M.new_store(limits)
    local max_total  = limits.max_total_bytes
    local max_cands  = limits.max_candidates
    local snaps, cands = {}, {}
    local total = 0
    local store = {}

    --- Store a snapshot, replacing any with the same name.
    --- @return the stored record. Raises if it would exceed max_total_bytes.
    function store.put(name, rec)
        local old = snaps[name]
        local next_total = total - (old and #old.data or 0) + #rec.data
        if next_total > max_total then
            error(string.format("snapshot store is full: keeping '%s' (%d bytes) would use %d of %d bytes. " ..
                "Delete snapshots you no longer need (bizhawk_delete_snapshots).", name, #rec.data, next_total, max_total), 0)
        end
        snaps[name], total = rec, next_total
        return rec
    end

    function store.get(name)
        local rec = snaps[name]
        if not rec then error("no snapshot named '" .. tostring(name) .. "' (see bizhawk_list_snapshots)", 0) end
        return rec
    end

    --- Save a candidate set (absolute addresses from a comparison).
    --- @return nothing. Raises if it holds more than max_candidates addresses.
    function store.put_candidates(name, rec)
        if #rec.addresses > max_cands then
            error(string.format("candidate set '%s' would hold %d addresses (limit %d); narrow the range or compare again first",
                name, #rec.addresses, max_cands), 0)
        end
        cands[name] = rec
    end

    function store.get_candidates(name)
        local rec = cands[name]
        if not rec then error("no candidate set named '" .. tostring(name) .. "' (see bizhawk_list_snapshots)", 0) end
        return rec
    end

    --- Delete one name (snapshot and/or candidate set), or everything.
    --- @return number of entries removed.
    function store.delete(name)
        local n = 0
        if name == nil then
            for _ in pairs(snaps) do n = n + 1 end
            for _ in pairs(cands) do n = n + 1 end
            snaps, cands, total = {}, {}, 0
            return n
        end
        if snaps[name] then total = total - #snaps[name].data; snaps[name] = nil; n = n + 1 end
        if cands[name] then cands[name] = nil; n = n + 1 end
        return n
    end

    --- Describe everything stored.
    --- @return `{ snapshots = {...}, candidates = {...}, used_bytes, max_bytes }`, sorted by name.
    function store.list()
        local s, c = {}, {}
        for name, r in pairs(snaps) do
            s[#s + 1] = { name = name, domain = r.domain, start = r.start, length = #r.data, frame = r.frame }
        end
        for name, r in pairs(cands) do
            c[#c + 1] = { name = name, domain = r.domain, width = r.width, count = #r.addresses }
        end
        table.sort(s, function(x, y) return x.name < y.name end)
        table.sort(c, function(x, y) return x.name < y.name end)
        return { snapshots = s, candidates = c, used_bytes = total, max_bytes = max_total }
    end

    return store
end

--- Compare two snapshots of the same memory region.
--- @return `{ count, addresses (all matches when count <= max_candidates, else nil),
---           results (first max_results matches with before/after values), checked }`.
---         Raises for mismatched snapshots, an unknown op/width, a missing
---         `value` for changed_by/equal_to, or candidates outside the region.
--- @requires Nothing (pure). opts: op, width, big_endian, aligned, value,
---           candidates (array of absolute addresses or nil), max_results, max_candidates.
function M.compare(before, after, opts)
    if before.domain ~= after.domain or before.start ~= after.start or #before.data ~= #after.data then
        error(string.format("snapshots cover different memory ('%s' %s+%d vs '%s' %s+%d); take both over the same domain and range",
            tostring(before.domain), tostring(before.start), #before.data,
            tostring(after.domain), tostring(after.start), #after.data), 0)
    end
    local test = OPS[opts.op]
    if not test then error("op must be one of changed, unchanged, increased, decreased, changed_by, equal_to (got " .. tostring(opts.op) .. ")", 0) end
    local value = opts.value
    if (opts.op == "changed_by" or opts.op == "equal_to") and type(value) ~= "number" then
        error(opts.op .. " needs a numeric `value`", 0)
    end
    local width = opts.width or "u8"
    local w = width_bytes(width)
    local fmt = (opts.big_endian and ">" or "<") .. FORMATS[width]
    local a_data, b_data, base, len = before.data, after.data, before.start, #before.data
    local max_results, max_cands = opts.max_results or 200, opts.max_candidates
    local unpack = string.unpack

    local count, results, addresses, checked = 0, {}, {}, 0
    local keep_all = true

    --- Test the value at 1-based string position `pos` (absolute address `addr`).
    local function visit(pos, addr)
        checked = checked + 1
        local a = unpack(fmt, a_data, pos)
        local b = unpack(fmt, b_data, pos)
        if test(a, b, value) then
            count = count + 1
            if #results < max_results then results[#results + 1] = { address = addr, before = a, after = b } end
            if keep_all then
                if max_cands and count > max_cands then keep_all, addresses = false, nil
                else addresses[count] = addr end
            end
        end
    end

    if opts.candidates then
        for _, addr in ipairs(opts.candidates) do
            local pos = addr - base + 1
            if pos < 1 or pos + w - 1 > len then
                error(string.format("candidate 0x%X is outside the snapshots (0x%X..0x%X)", addr, base, base + len - 1), 0)
            end
            visit(pos, addr)
        end
    else
        local step = opts.aligned and w or 1
        for pos = 1, len - w + 1, step do visit(pos, base + pos - 1) end
    end

    return { count = count, addresses = addresses, results = results, checked = checked }
end

return M
