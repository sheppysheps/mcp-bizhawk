-- sandbox.lua: opt-in, sandboxed execution of caller-supplied Lua for mcp-bizhawk
--
-- WHY THIS FILE EXISTS
-- Some games hide their data behind pointer chains, bank switching or packed
-- structures that the fixed read/write tools can't walk in one call. Running a
-- short script inside BizHawk solves that, but "run any code" is exactly what
-- malware wants. This module keeps the feature as small as possible:
--
--   • OFF BY DEFAULT. Code runs only while the human has ticked the checkbox in
--     the confirmation dialog this module opens inside BizHawk. The state lives
--     in a local, is never written to disk, and resets when the bridge restarts.
--     Nothing on the MCP/Node side can tick the box: the sandbox has no `forms`.
--   • TEXT ONLY. load(..., "t", env) refuses precompiled bytecode, a known way
--     to break out of Lua sandboxes.
--   • WHITELISTED ENVIRONMENT. The code sees a fresh table holding only the
--     functions listed below: memory READS, gui drawing, emulator/game info and
--     pure math/string/table helpers. No os, io, require, dofile, loadfile,
--     load, debug, coroutine, comm, forms, getmetatable, or NLua's luanet
--     (verified present in BizHawk 2.11 — it gives full .NET access).
--   • NO .NET OBJECTS. BizHawk's API functions are luaNet_function userdata.
--     Each one is wrapped in a plain Lua closure, so sandboxed code never holds
--     a host object it could probe.
--   • BOUNDED. An instruction budget and a memory ceiling stop runaway code.
--     The sandbox's own pcall/xpcall re-raise once the budget is blown, so a
--     script can't swallow the abort and keep looping.
--
-- Memory WRITES are deliberately absent: the dedicated write tools already
-- cover them with clear, auditable inputs. Add them here only for a concrete
-- need, with a comment saying why.

local M = {}

-- The confirmation text is fixed by project policy (see CLAUDE.md). Don't edit
-- it without the maintainer's sign-off.
local WARNING_TEXT =
    "Warning: Enabling this can cause damage to your system. If someone asked you to " ..
    "enable this, they may be trying to scam or phish you. Run arbitrary LUA code at your " ..
    "own risk. Click the checkbox if you understand and you know what you are doing."

local CHECKBOX_TEXT = "I understand, and I know what I am doing"

local HOOK_EVERY     = 1000     -- VM instructions between budget checks
local MAX_OUTPUT     = 16384    -- bytes of print() output kept
local MAX_REP        = 1048576  -- longest string string.rep may build (1 MiB)
local PLAIN_DEPTH    = 4        -- nesting kept when returning tables
local PLAIN_ENTRIES  = 1000     -- entries kept per returned table

-- API functions exposed per BizHawk library. Anything not named here is absent.
local HOST_WHITELIST = {
    memory = {
        "getmemorydomainlist", "getcurrentmemorydomain", "getcurrentmemorydomainsize",
        "getmemorydomainsize", "hash_region",
        "read_u8", "read_s8", "readbyte",
        "read_u16_le", "read_u16_be", "read_s16_le", "read_s16_be",
        "read_u24_le", "read_u24_be", "read_s24_le", "read_s24_be",
        "read_u32_le", "read_u32_be", "read_s32_le", "read_s32_be",
        "readfloat", "readbyterange", "read_bytes_as_array", "read_bytes_as_dict",
    },
    mainmemory = {
        "getname", "getcurrentmemorydomainsize",
        "read_u8", "read_s8", "readbyte",
        "read_u16_le", "read_u16_be", "read_s16_le", "read_s16_be",
        "read_u24_le", "read_u24_be", "read_s24_le", "read_s24_be",
        "read_u32_le", "read_u32_be", "read_s32_le", "read_s32_be",
        "readfloat", "readbyterange", "read_bytes_as_array", "read_bytes_as_dict",
    },
    emu = {
        "framecount", "lagcount", "islagged", "getregister", "getregisters",
        "getsystemid", "getboardname", "getdisplaytype", "totalexecutedcycles", "disassemble",
    },
    -- Drawing only. drawImage / drawImageRegion / drawIcon are left out because
    -- they read image files from disk; createcanvas opens new windows.
    gui = {
        "drawBox", "drawRectangle", "drawLine", "drawPixel", "drawEllipse", "drawPolygon",
        "drawBezier", "drawAxis", "drawPie", "drawString", "drawText", "pixelText", "text",
        "clearGraphics", "cleartext", "addmessage", "use_surface",
    },
    client = {
        "bufferwidth", "bufferheight", "screenwidth", "screenheight",
        "borderwidth", "borderheight", "getwindowsize", "transformPoint",
    },
    gameinfo = { "getromname", "getromhash", "getboardtype", "getstatus", "indatabase" },
}

--- Wrap a host function in a plain Lua closure so no userdata reaches the sandbox.
--- @return function forwarding its arguments and results to `fn`.
--- @requires Nothing.
local function wrap(fn)
    return function(...) return fn(...) end
end

--- Copy the whitelisted, present functions of one host library.
--- @return table of wrapped functions (empty if the library is missing).
--- @requires `has(t, name)` from the bridge to detect userdata-backed methods.
local function copy_host_lib(lib, names, has)
    local t = {}
    if not lib then return t end
    for _, name in ipairs(names) do
        if has(lib, name) then t[name] = wrap(lib[name]) end
    end
    return t
end

--- Shallow-copy a standard Lua library, skipping `skip` names.
--- @return a fresh table, so sandboxed code can't modify the host's library.
--- @requires Nothing.
local function copy_std_lib(lib, skip)
    local t = {}
    for k, v in pairs(lib or {}) do
        if not (skip and skip[k]) then t[k] = v end
    end
    return t
end

--- Escape control bytes that the bridge's json.lua would pass through raw
--- (it only escapes \n \r \t), which would make the RESULT line invalid JSON.
--- @return the string with each such byte replaced by a visible \xNN.
--- @requires Nothing.
local function json_safe(s)
    return (s:gsub("[%z\1-\8\11\12\14-\31\127]", function(c)
        return string.format("\\x%02X", c:byte())
    end))
end

--- Convert a value into something json.encode can carry back to the caller.
--- @return numbers/booleans/nil unchanged, strings made JSON-safe, tables copied
---         up to PLAIN_DEPTH levels and PLAIN_ENTRIES keys, anything else as tostring().
--- @requires Nothing.
local function to_plain(v, depth)
    local tv = type(v)
    if tv == "nil" or tv == "boolean" or tv == "number" then return v end
    if tv == "string" then return json_safe(v) end
    if tv ~= "table" then return "<" .. tostring(v) .. ">" end
    if depth >= PLAIN_DEPTH then return "<table: max depth>" end
    local out, n = {}, 0
    for k, val in pairs(v) do
        n = n + 1
        if n > PLAIN_ENTRIES then out["..."] = "truncated"; break end
        local key = (type(k) == "number" or type(k) == "string") and k or tostring(k)
        out[key] = to_plain(val, depth + 1)
    end
    return out
end

--- Create the opt-in gate: the confirmation dialog plus the enabled check.
--- @return table with `enabled()` → boolean and `request()` → opens the dialog
---         if it isn't already showing. `enabled()` is false whenever the box
---         is unticked, the dialog was closed, or `forms` is unavailable.
--- @requires BizHawk `forms` (newform, label, checkbox, ischecked); `has` helper.
function M.new_gate(forms, has)
    local form_handle, box_handle = nil, nil
    local can_prompt = forms and has(forms, "newform") and has(forms, "checkbox")
                       and has(forms, "label") and has(forms, "ischecked")

    local gate = {}

    function gate.enabled()
        if not box_handle then return false end
        local ok, checked = pcall(forms.ischecked, box_handle)
        return ok and checked == true
    end

    function gate.request()
        if not can_prompt then return false end
        if form_handle then return true end
        -- onClose drops the handles, which turns the feature back off.
        form_handle = forms.newform(420, 190, "mcp-bizhawk: arbitrary Lua", function()
            form_handle, box_handle = nil, nil
        end)
        forms.label(form_handle, WARNING_TEXT, 12, 12, 390, 100, true)
        box_handle = forms.checkbox(form_handle, CHECKBOX_TEXT, 12, 120)
        -- The caption is wider than the default checkbox; cosmetic, so ignore failure.
        if has(forms, "setproperty") then pcall(forms.setproperty, box_handle, "Width", 390) end
        return true
    end

    gate.can_prompt = can_prompt
    return gate
end

--- Run caller-supplied Lua source in the sandbox.
--- @return table `{ values = {...}, output = "printed text", instructions = n }`
---         on success. Raises an error string for a compile error, a runtime
---         error, or when the instruction or memory budget is exceeded.
--- @requires `host` table with the BizHawk libraries (memory, mainmemory, emu,
---           gui, client, gameinfo, bit) and `has`; Lua's debug.sethook and
---           coroutine, used here by the host side only (never exposed).
function M.run(code, opts, host)
    local budget    = opts.max_instructions
    local mem_limit = opts.max_memory_kb
    local used      = 0
    local aborted   = nil
    local printed, printed_len = {}, 0

    local env = {}
    for name, names in pairs(HOST_WHITELIST) do
        env[name] = copy_host_lib(host[name], names, host.has)
    end
    -- BizHawk's `bit` library: pure integer helpers older scripts expect.
    if host.bit then
        local b = {}
        for k, v in pairs(host.bit) do
            if type(v) == "function" or type(v) == "userdata" then b[k] = wrap(v) end
        end
        env.bit = b
    end

    env.math   = copy_std_lib(math)
    env.table  = copy_std_lib(table)
    env.utf8   = copy_std_lib(utf8)
    -- string.rep can allocate gigabytes in one C call, which the instruction
    -- hook can't interrupt. Method calls like ("a"):rep(n) go through the
    -- shared string metatable to the HOST string table, so the cap is swapped
    -- into the host table for the duration of the run (restored below).
    local host_rep = string.rep
    local capped_rep = function(s, n, sep)
        local unit = #tostring(s) + (sep and #tostring(sep) or 0)
        if unit * (tonumber(n) or 0) > MAX_REP then
            error("string.rep result too large (limit " .. MAX_REP .. " bytes)", 2)
        end
        return host_rep(s, n, sep)
    end
    env.string = copy_std_lib(string, { dump = true })
    env.string.rep = capped_rep

    for _, name in ipairs({ "assert", "error", "ipairs", "next", "pairs", "select",
                            "tonumber", "tostring", "type", "rawequal", "rawlen" }) do
        env[name] = _G[name]
    end
    env.unpack = table.unpack
    -- A __gc finalizer runs later, outside the coroutine and its instruction
    -- hook, where an infinite loop would freeze BizHawk. Refuse it.
    env.setmetatable = function(t, mt)
        if type(mt) == "table" and rawget(mt, "__gc") ~= nil then
            error("__gc metamethods are not allowed in the sandbox", 2)
        end
        return setmetatable(t, mt)
    end

    -- pcall/xpcall that can't hide a budget abort.
    env.pcall = function(f, ...)
        local r = table.pack(pcall(f, ...))
        if aborted then error(aborted, 0) end
        return table.unpack(r, 1, r.n)
    end
    env.xpcall = function(f, handler, ...)
        local r = table.pack(xpcall(f, handler, ...))
        if aborted then error(aborted, 0) end
        return table.unpack(r, 1, r.n)
    end

    env.print = function(...)
        local parts = table.pack(...)
        for i = 1, parts.n do parts[i] = tostring(parts[i]) end
        local line = table.concat(parts, "\t", 1, parts.n)
        if printed_len < MAX_OUTPUT then
            printed[#printed + 1] = line
            printed_len = printed_len + #line + 1
        end
    end

    local chunk, compile_err = load(code, "=eval", "t", env)
    if not chunk then error("compile error: " .. tostring(compile_err), 0) end

    local base_kb = collectgarbage("count")
    local co = coroutine.create(chunk)
    debug.sethook(co, function()
        used = used + HOOK_EVERY
        if not aborted then
            if used > budget then
                aborted = "instruction budget exceeded (" .. budget .. ")"
            elseif collectgarbage("count") - base_kb > mem_limit then
                aborted = "memory limit exceeded (" .. mem_limit .. " KB)"
            end
        end
        if aborted then error(aborted, 0) end
    end, "", HOOK_EVERY)

    string.rep = capped_rep
    local results = table.pack(pcall(coroutine.resume, co))
    string.rep = host_rep
    -- results: pcall ok, resume ok, values... (or an error message at [2]/[3])
    if not results[1] then error(tostring(results[2]), 0) end
    if not results[2] then error(json_safe(tostring(results[3])), 0) end
    if coroutine.status(co) ~= "dead" then error("script yielded; coroutines are not supported", 0) end

    local values = {}
    for i = 3, results.n do values[i - 2] = to_plain(results[i], 0) end
    local output = json_safe(table.concat(printed, "\n"))
    if printed_len >= MAX_OUTPUT then output = output .. "\n[output truncated]" end
    return { values = values, count = results.n - 2, output = output, instructions = used }
end

return M
