-- bytesearch.lua: byte-pattern, text and relative search for mcp-bizhawk
--
-- WHY THIS FILE EXISTS
-- Mapping a ROM (headers, code idioms, data tables) and finding text in an
-- unknown encoding both start with "where do these bytes occur?". BizHawk's
-- Hex Editor search can't be driven from Lua, so this provides it. Three modes:
--
--   hex       "CD 40 05", "C3 ?? 40": exact bytes, `??` matches any byte.
--   text      exact bytes of a string (ASCII / UTF-8), e.g. a header title.
--   relative  the classic ROM-hacking "relative search" for text in an
--             unknown encoding: find runs whose byte DIFFERENCES match the
--             letters' differences ("SHANTAE": S→H is −11, H→A is −7, ...),
--             whatever value each letter is stored as. Each match reports
--             the inferred mapping (e.g. A = 0x8B), the start of a text table.
--
-- Pure Lua, no BizHawk APIs: bridge.lua passes the bytes in as a string, so
-- test/lua runs these checks on stock Lua 5.4.

local M = {}

local MAGIC = "^$()%.[]*+-?"  -- Lua pattern characters that need escaping

--- Parse a hex pattern like "CD 40 05" or "c3??40".
--- @return array of byte values (0..255) or false for a `??` wildcard.
---         Raises on anything that isn't pairs of hex digits or `??`.
--- @requires Nothing.
function M.parse_hex(s)
    if type(s) ~= "string" then error("pattern must be a string of hex bytes, e.g. \"CD 40 05\"", 0) end
    local compact = s:gsub("[%s,]", "")
    if #compact == 0 or #compact % 2 ~= 0 then
        error("pattern must be whole bytes written as two hex digits or ?? (got \"" .. s .. "\")", 0)
    end
    local out = {}
    for i = 1, #compact, 2 do
        local pair = compact:sub(i, i + 1)
        if pair == "??" then
            out[#out + 1] = false
        elseif pair:match("^%x%x$") then
            out[#out + 1] = tonumber(pair, 16)
        else
            error("bad byte \"" .. pair .. "\" in pattern; use two hex digits or ??", 0)
        end
    end
    return out
end

--- Turn parsed bytes into a Lua pattern (wildcards become ".").
--- @return a Lua pattern string matching exactly those bytes.
--- @requires Nothing.
local function to_lua_pattern(bytes)
    local parts = {}
    for i, b in ipairs(bytes) do
        if b == false then
            parts[i] = "."
        else
            local c = string.char(b)
            parts[i] = MAGIC:find(c, 1, true) and ("%" .. c) or c
        end
    end
    return table.concat(parts)
end

--- Hex preview of up to `n` bytes of `data` starting at 1-based `pos`.
--- @return e.g. "53 48 41 4E".
--- @requires Nothing.
local function preview(data, pos, n)
    local t = {}
    for i = pos, math.min(#data, pos + n - 1) do t[#t + 1] = string.format("%02X", data:byte(i)) end
    return table.concat(t, " ")
end

--- Find a byte sequence (with optional wildcards).
--- @return `{ count, matches = {{address, preview}...} }`; count is the true
---         total, matches holds at most max_results. Matches may overlap.
--- @requires Nothing. data: binary string; base: address of data's first byte.
function M.find_bytes(data, base, bytes, opts)
    if #bytes == 0 then error("pattern is empty", 0) end
    if bytes[1] == false then error("pattern can't start with ??; drop the leading wildcard", 0) end
    local max, plen = opts.max_results or 50, opts.preview or 16
    local pat = to_lua_pattern(bytes)
    local plain = not pat:find("[%%.]")  -- no wildcards or escapes: use fast plain find
    local count, matches, init = 0, {}, 1
    while true do
        local s = string.find(data, pat, init, plain)
        if not s then break end
        count = count + 1
        if #matches < max then matches[#matches + 1] = { address = base + s - 1, preview = preview(data, s, plen) } end
        init = s + 1
    end
    return { count = count, matches = matches }
end

--- Relative search: find runs whose byte differences match `text`'s letters.
--- Letters and digits constrain the match; other characters (spaces,
--- punctuation) are wildcards, since games rarely store them next to letters.
--- @return `{ count, matches = {{address, preview, mapping}...} }` where mapping
---         says which byte the first constrained character is stored as
---         (e.g. "S = 0x9C"), plus 'A'/'a'/'0' when the text has that class.
---         Raises when fewer than 3 characters constrain the search.
--- @requires Nothing.
function M.relative(data, base, text, opts)
    if type(text) ~= "string" then error("text required for relative search", 0) end
    local max, plen = opts.max_results or 50, opts.preview or 16
    local idx, code = {}, {}
    for i = 1, #text do
        local c = text:sub(i, i)
        if c:match("%w") then idx[#idx + 1] = i - 1; code[#code + 1] = c:byte() end
    end
    if #idx < 3 then error("relative search needs at least 3 letters or digits to be useful", 0) end
    local span = #text
    local k0, c0 = idx[1], code[1]
    local has_upper, has_lower, has_digit = text:find("%u"), text:find("%l"), text:find("%d")
    local count, matches = 0, {}
    local byte = string.byte
    for pos = 1, #data - span + 1 do
        local off = byte(data, pos + k0) - c0   -- stored value minus character code
        local ok = true
        for j = 2, #idx do
            if (byte(data, pos + idx[j]) - code[j] - off) % 256 ~= 0 then ok = false; break end
        end
        if ok then
            count = count + 1
            if #matches < max then
                local function map(ch) return string.format("%s = 0x%02X", ch, (ch:byte() + off) % 256) end
                local m = { map(text:sub(k0 + 1, k0 + 1)) }
                if has_upper then m[#m + 1] = map("A") end
                if has_lower then m[#m + 1] = map("a") end
                if has_digit then m[#m + 1] = map("0") end
                matches[#matches + 1] = { address = base + pos - 1, preview = preview(data, pos, math.max(plen, span)),
                                          mapping = table.concat(m, ", ") }
            end
        end
    end
    return { count = count, matches = matches }
end

--- Run a search in one of the three modes.
--- @return find_bytes / relative result plus `scanned` (bytes searched).
--- @requires Nothing. opts: mode ("hex" | "text" | "relative"), pattern or text, max_results.
function M.search(data, base, opts)
    local r
    if opts.mode == "hex" then
        r = M.find_bytes(data, base, M.parse_hex(opts.pattern), opts)
    elseif opts.mode == "text" then
        if type(opts.text) ~= "string" or #opts.text == 0 then error("text required for mode 'text'", 0) end
        local bytes = { opts.text:byte(1, -1) }
        r = M.find_bytes(data, base, bytes, opts)
    elseif opts.mode == "relative" then
        r = M.relative(data, base, opts.text, opts)
    else
        error("mode must be 'hex', 'text' or 'relative' (got " .. tostring(opts.mode) .. ")", 0)
    end
    r.scanned = #data
    return r
end

return M
