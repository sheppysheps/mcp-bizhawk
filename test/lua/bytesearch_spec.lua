-- Layer 2: lua/bytesearch.lua (hex / text / relative search). Pure Lua.

--- Run every check.
--- @return list of `{ name, pass, detail }`.
--- @requires the `bytesearch` module (lua/bytesearch.lua).
return function(bs)
    local results = {}
    local function check(name, pass, detail)
        results[#results + 1] = { name = "bytesearch: " .. name, pass = pass and true or false, detail = detail }
    end
    local function raises(name, expect, f)
        local ok, err = pcall(f)
        check(name, not ok and tostring(err):find(expect, 1, true) ~= nil, ok and "no error" or tostring(err))
    end
    local function addrs(r) local t = {} for i, m in ipairs(r.matches) do t[i] = string.format("%X", m.address) end return table.concat(t, ",") end
    local B = string.char

    -- Hex pattern parsing.
    local p = bs.parse_hex("CD 40 05")
    check("parse spaced hex", #p == 3 and p[1] == 0xCD and p[2] == 0x40 and p[3] == 0x05)
    p = bs.parse_hex("c3??40")
    check("parse compact hex with a wildcard", #p == 3 and p[1] == 0xC3 and p[2] == false and p[3] == 0x40)
    raises("odd number of hex digits", "whole bytes", function() bs.parse_hex("C3 4") end)
    raises("non-hex byte", "bad byte \"ZZ\"", function() bs.parse_hex("C3 ZZ") end)
    raises("empty pattern", "whole bytes", function() bs.parse_hex("  ") end)

    -- Hex search over data based at 0x1000.
    local data = B(0x00, 0xCD, 0x40, 0x05, 0x11, 0xCD, 0x41, 0x05, 0xCD, 0x40, 0x05)
    local r = bs.search(data, 0x1000, { mode = "hex", pattern = "CD 40 05" })
    check("exact bytes", r.count == 2 and addrs(r) == "1001,1008", addrs(r))
    r = bs.search(data, 0x1000, { mode = "hex", pattern = "CD ?? 05" })
    check("?? matches any byte", r.count == 3 and addrs(r) == "1001,1005,1008", addrs(r))
    check("matches carry a byte preview", r.matches[1].preview:sub(1, 8) == "CD 40 05")
    check("scanned reports the data size", r.scanned == #data)
    r = bs.search(B(0xAA, 0xAA, 0xAA, 0xAA), 0, { mode = "hex", pattern = "AA AA" })
    check("overlapping matches are all counted", r.count == 3)
    raises("pattern starting with ??", "can't start with ??", function() bs.search(data, 0, { mode = "hex", pattern = "?? 40" }) end)
    r = bs.search(data, 0, { mode = "hex", pattern = "CD", max_results = 1 })
    check("max_results caps the list, not the count", r.count == 3 and #r.matches == 1)

    -- Bytes that are Lua pattern characters, and zero bytes, match literally.
    local magic = B(0x41, 0x25, 0x2E, 0x5B, 0x00, 0x2A, 0x41)  -- A % . [ \0 * A
    r = bs.search(magic, 0, { mode = "hex", pattern = "25 2E 5B 00 2A" })
    check("%, ., [, NUL and * are literal", r.count == 1 and r.matches[1].address == 1)
    r = bs.search(B(0x41, 0x2E, 0x42), 0, { mode = "hex", pattern = "2E" })
    check("'.' doesn't act as a wildcard", r.count == 1)
    r = bs.search(magic, 0, { mode = "hex", pattern = "25 ?? 5B" })
    check("escaped bytes combine with wildcards", r.count == 1 and r.matches[1].address == 1)

    -- Text mode.
    r = bs.search("....SHANTAE....SHANTAE", 0x134, { mode = "text", text = "SHANTAE" })
    check("text finds exact ASCII", r.count == 2 and addrs(r) == "138,143", addrs(r))
    raises("text mode needs text", "text required", function() bs.search("x", 0, { mode = "text" }) end)

    -- Relative search: encode text with a custom table where 'A' = 0x80.
    local function encode(s, a_value)
        local out = {}
        for i = 1, #s do
            local c = s:byte(i)
            if s:sub(i, i):match("%u") then out[i] = (c - 65 + a_value) % 256 else out[i] = 0xFF end  -- space etc. = 0xFF
        end
        return B(table.unpack(out))
    end
    local rom = "\1\2\3" .. encode("HELLO WORLD", 0x80) .. "\9\9" .. encode("HELLO", 0x80)
    r = bs.search(rom, 0x4000, { mode = "relative", text = "HELLO WORLD" })
    check("relative finds re-encoded text", r.count == 1 and r.matches[1].address == 0x4003, addrs(r))
    check("relative reports the mapping", r.matches[1] and r.matches[1].mapping == "H = 0x87, A = 0x80", r.matches[1] and r.matches[1].mapping)
    r = bs.search(rom, 0x4000, { mode = "relative", text = "HELLO" })
    check("relative finds every occurrence", r.count == 2 and addrs(r) == "4003,4010", addrs(r))
    local wrap = encode("ZEBRA", 0xF0)  -- 'Z' = 0xF0 + 25 wraps past 0xFF
    r = bs.search(wrap, 0, { mode = "relative", text = "ZEBRA" })
    check("relative handles values that wrap past 0xFF", r.count == 1 and r.matches[1].mapping == "Z = 0x09, A = 0xF0",
          r.matches[1] and r.matches[1].mapping)
    r = bs.search(encode("HELLO", 0x80), 0, { mode = "relative", text = "HELLA" })
    check("relative rejects a wrong letter", r.count == 0)
    local lower = B(0x10 + 7, 0x10 + 4, 0x10 + 11, 0x10 + 11, 0x10 + 14)  -- "hello" with 'a' = 0x10
    r = bs.search(lower, 0, { mode = "relative", text = "hello" })
    check("lowercase mapping", r.count == 1 and r.matches[1].mapping == "h = 0x17, a = 0x10", r.matches[1] and r.matches[1].mapping)
    raises("relative needs 3+ letters", "at least 3 letters", function() bs.search(rom, 0, { mode = "relative", text = "HI" }) end)

    raises("unknown mode", "mode must be 'hex', 'text' or 'relative'", function() bs.search("x", 0, { mode = "fuzzy" }) end)

    return results
end
