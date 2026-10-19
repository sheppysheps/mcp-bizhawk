-- Layer 2: lua/snapshot.lua (snapshot store and comparisons). Pure Lua, so
-- it runs the same on stock Lua 5.4 and inside BizHawk.

--- Run every check.
--- @return list of `{ name, pass, detail }`.
--- @requires the `snapshot` module (lua/snapshot.lua).
return function(snapshot)
    local results = {}
    local function check(name, pass, detail)
        results[#results + 1] = { name = "snapshot: " .. name, pass = pass and true or false, detail = detail }
    end
    --- Run f; pass if it raises an error containing `expect`.
    local function raises(name, expect, f)
        local ok, err = pcall(f)
        check(name, not ok and tostring(err):find(expect, 1, true) ~= nil, ok and "no error" or tostring(err))
    end
    local function snap(data, start, domain) return { domain = domain or "WRAM", start = start or 0x100, data = data } end
    local function bytes(...) return string.char(...) end
    local function addrs(r) local t = {} for i, m in ipairs(r.results) do t[i] = m.address end return table.concat(t, ",") end

    -- 8-bit comparisons over bytes at 0x100..0x105.
    local A = snap(bytes(10, 20, 30, 40, 50, 60))
    local B = snap(bytes(10, 21, 29, 40, 55, 0))
    local function cmp(op, extra)
        local o = { op = op, width = "u8" }
        for k, v in pairs(extra or {}) do o[k] = v end
        return snapshot.compare(A, B, o)
    end
    check("changed",              addrs(cmp("changed"))   == "257,258,260,261")
    check("unchanged",            addrs(cmp("unchanged")) == "256,259")
    check("increased",            addrs(cmp("increased")) == "257,260")
    check("decreased",            addrs(cmp("decreased")) == "258,261")
    check("changed_by +5",        addrs(cmp("changed_by", { value = 5 })) == "260")
    check("changed_by -60",       addrs(cmp("changed_by", { value = -60 })) == "261")
    check("equal_to",             addrs(cmp("equal_to", { value = 40 })) == "259")
    check("before/after values are reported", (function()
        local m = cmp("decreased").results[1]
        return m.address == 0x102 and m.before == 30 and m.after == 29
    end)())
    check("count and checked",    (function() local r = cmp("changed") return r.count == 4 and r.checked == 6 end)())

    -- Signedness matters for increased/decreased across zero.
    local S1, S2 = snap(bytes(0x01)), snap(bytes(0xFF))
    check("u8: 1 -> 255 increased", snapshot.compare(S1, S2, { op = "increased", width = "u8" }).count == 1)
    check("s8: 1 -> -1 decreased",  snapshot.compare(S1, S2, { op = "decreased", width = "s8" }).count == 1)
    check("s8: changed_by -2",      snapshot.compare(S1, S2, { op = "changed_by", width = "s8", value = -2 }).count == 1)

    -- Multi-byte widths, endianness and alignment.
    local W1 = snap(bytes(0x34, 0x12, 0x00, 0x00), 0)
    local W2 = snap(bytes(0x35, 0x12, 0x00, 0x00), 0)
    check("u16 little-endian value", (function()
        local m = snapshot.compare(W1, W2, { op = "changed", width = "u16", aligned = true }).results[1]
        return m and m.address == 0 and m.before == 0x1234 and m.after == 0x1235
    end)())
    check("u16 big-endian value", (function()
        local m = snapshot.compare(W1, W2, { op = "changed", width = "u16", aligned = true, big_endian = true }).results[1]
        return m and m.before == 0x3412 and m.after == 0x3512
    end)())
    check("aligned u16 checks 2 offsets of 4 bytes",  snapshot.compare(W1, W2, { op = "unchanged", width = "u16", aligned = true }).checked == 2)
    check("unaligned u16 checks 3 offsets of 4 bytes", snapshot.compare(W1, W2, { op = "unchanged", width = "u16", aligned = false }).checked == 3)
    check("u32 value", (function()
        local m = snapshot.compare(W1, W2, { op = "increased", width = "u32", aligned = true }).results[1]
        return m and m.before == 0x1234 and m.after == 0x1235
    end)())

    -- Narrowing with a candidate set; results and candidate caps.
    check("candidates limit what's checked", (function()
        local r = snapshot.compare(A, B, { op = "changed", width = "u8", candidates = { 0x100, 0x101, 0x104 } })
        return r.checked == 3 and addrs(r) == "257,260"
    end)())
    check("max_results caps the listing, not the count", (function()
        local r = snapshot.compare(A, B, { op = "changed", width = "u8", max_results = 2 })
        return r.count == 4 and #r.results == 2
    end)())
    check("addresses dropped when over max_candidates", (function()
        local r = snapshot.compare(A, B, { op = "changed", width = "u8", max_candidates = 3 })
        return r.count == 4 and r.addresses == nil
    end)())
    check("addresses kept when within max_candidates", (function()
        local r = snapshot.compare(A, B, { op = "changed", width = "u8", max_candidates = 10 })
        return r.addresses and #r.addresses == 4 and r.addresses[1] == 0x101
    end)())

    -- Errors.
    raises("snapshots from different domains", "different memory", function()
        snapshot.compare(snap(bytes(1), 0, "WRAM"), snap(bytes(1), 0, "HRAM"), { op = "changed" }) end)
    raises("snapshots of different lengths", "different memory", function()
        snapshot.compare(snap(bytes(1, 2)), snap(bytes(1)), { op = "changed" }) end)
    raises("unknown op", "op must be one of", function() snapshot.compare(A, B, { op = "bigger" }) end)
    raises("unknown width", "width must be one of", function() snapshot.compare(A, B, { op = "changed", width = "u64" }) end)
    raises("changed_by needs a value", "needs a numeric `value`", function() snapshot.compare(A, B, { op = "changed_by" }) end)
    raises("candidate outside the range", "outside the snapshots", function()
        snapshot.compare(A, B, { op = "changed", candidates = { 0x999 } }) end)

    -- Store limits and bookkeeping.
    local store = snapshot.new_store({ max_total_bytes = 10, max_candidates = 2 })
    store.put("a", snap(string.rep("x", 6)))
    raises("store refuses to go over its byte limit", "snapshot store is full", function() store.put("b", snap(string.rep("x", 5))) end)
    check("replacing a snapshot frees its old size", pcall(store.put, "a", snap(string.rep("x", 10))))
    check("list reports usage", store.list().used_bytes == 10 and #store.list().snapshots == 1)
    raises("unknown snapshot name", "no snapshot named 'zzz'", function() store.get("zzz") end)
    raises("candidate sets have a size limit", "would hold 3 addresses", function()
        store.put_candidates("c", { domain = "WRAM", width = "u8", addresses = { 1, 2, 3 } }) end)
    store.put_candidates("c", { domain = "WRAM", width = "u8", addresses = { 1, 2 } })
    check("candidate sets are listed", store.list().candidates[1].count == 2)
    check("delete by name removes it", store.delete("a") == 1 and store.list().used_bytes == 0)
    check("delete with no name clears everything", store.delete(nil) == 1 and #store.list().candidates == 0)

    return results
end
