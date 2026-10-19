-- Layer 2: sandbox, consent gate and json.lua checks.
--
-- Runs on stock Lua 5.4 (CI: test/lua/run.lua) and inside BizHawk's NLua
-- (locally: test/lua/run-in-bizhawk.mjs). BizHawk libraries are replaced by
-- stand-ins so results don't depend on a ROM. The stand-ins deliberately
-- include functions the sandbox must NOT expose (memory.write_u8,
-- gui.drawImage), so these tests prove the whitelist filters them out.

--- Build stand-in BizHawk libraries.
--- @return host table in the shape bridge.lua passes to sandbox.run.
--- @requires Nothing.
local function stub_host()
    local function zeros(_, n) local t = {} for i = 1, n do t[i] = 0 end return t end
    local function has(t, name)
        if not t then return false end
        local tv = type(t[name])
        return tv == "function" or tv == "userdata"
    end
    return {
        memory = {
            read_u8 = function() return 7 end,
            read_bytes_as_array = zeros,
            getmemorydomainsize = function() return 32768 end,
            write_u8 = function() error("write_u8 must never be reachable") end,
        },
        mainmemory = { read_u8 = function() return 7 end },
        emu = {
            framecount = function() return 42 end,
            getsystemid = function() return "GBC" end,
            getregisters = function() return { PC = 64 } end,
            frameadvance = function() error("frameadvance must never be reachable") end,
        },
        gui = {
            drawRectangle = function() end,
            drawImage = function() error("drawImage must never be reachable") end,
        },
        client = { screenwidth = function() return 160 end, exit = function() error("exit must never be reachable") end },
        gameinfo = { getromname = function() return "Test ROM" end },
        has = has,
    }
end

--- Run every check.
--- @return list of `{ name, pass, detail }`.
--- @requires `sandbox` (lua/sandbox.lua) and `json` (lua/json.lua) modules.
return function(sandbox, json)
    local results = {}
    local host = stub_host()
    local DEFAULT = { max_instructions = 1000000, max_memory_kb = 16384 }

    --- Record one result.
    local function check(name, pass, detail)
        results[#results + 1] = { name = name, pass = pass and true or false, detail = detail }
    end

    --- Run `code` in the sandbox; pass if the outcome contains `expect`.
    local function case(name, expect, code, opts)
        local ok, r = pcall(sandbox.run, code, opts or DEFAULT, host)
        local got = ok and ("OK " .. json.encode(r)) or ("ERR " .. tostring(r))
        check("sandbox: " .. name, got:find(expect, 1, true) ~= nil, got:sub(1, 200))
    end

    -- What scripts CAN do.
    case("basic return",              '"values":[2]',           "return 1 + 1")
    case("print is captured",         '"output":"hi\\t3"',      "print('hi', 3) return nil")
    case("tables come back as JSON",  '"b":[2,3]',              "return {a = 1, b = {2, 3}}")
    case("whitelisted read works",    '"values":[7]',           "return memory.read_u8(0, 'WRAM')")
    case("emu info works",            '"values":[42,"GBC"]',    "return emu.framecount(), emu.getsystemid()")
    case("API functions are plain Lua functions", '"values":["function"]', "return type(memory.read_u8)")
    case("returning nil counts",      '"count":1',              "return nil")

    -- What scripts must NOT reach.
    for _, global in ipairs({ "os", "io", "luanet", "require", "load", "loadfile", "dofile", "debug",
                              "getmetatable", "coroutine", "forms", "comm", "package", "_G", "collectgarbage" }) do
        case("no " .. global, '"values":[true]', "return " .. global .. " == nil")
    end
    case("no memory writes",          '"values":[true]',        "return memory.write_u8 == nil")
    case("no gui.drawImage",          '"values":[true]',        "return gui.drawImage == nil")
    case("no emu.frameadvance",       '"values":[true]',        "return emu.frameadvance == nil")
    case("no client.exit",            '"values":[true]',        "return client.exit == nil")
    case("no string.dump",            '"values":[true]',        "return string.dump == nil")
    -- Bytecode needs both a dumper and a loader; neither is reachable.
    case("no way to produce bytecode", "field 'dump'",          "return string.dump(print)")
    case("no way to load chunks",     "global 'load'",          "return load('return 1')")

    -- Limits.
    case("infinite loop is stopped",  "instruction budget exceeded", "while true do end")
    case("pcall can't swallow the abort", "instruction budget exceeded",
         "while true do pcall(function() while true do end end) end")
    case("xpcall can't swallow the abort", "instruction budget exceeded",
         "while true do xpcall(function() while true do end end, function() end) end")
    case("string.rep is capped",      "string.rep result too large", "return string.rep('ab', 1e8)")
    case("('x'):rep is capped too",   "string.rep result too large", "return ('a'):rep(1e9)")
    case("__gc metatables refused",   "__gc metamethods are not allowed",
         "setmetatable({}, {__gc = function() end})")
    case("memory ceiling",            "memory limit exceeded",
         "local t = {} for i = 1, 1e8 do t[i] = {} end", { max_instructions = 50000000, max_memory_kb = 4096 })
    case("bulk reads over 64 KB refused", "length over 65536",
         "return #memory.read_bytes_as_array(0, 100000, 'ROM')")
    case("bulk reads within the cap work", '"values":[16]',
         "return #memory.read_bytes_as_array(0, 16, 'ROM')")
    case("planted __tostring error doesn't run", "<error object: table>",
         "error(setmetatable({}, {__tostring = function() while true do end end}))")
    case("planted __pairs return doesn't run", '"values":[{"a":1}]',
         "return setmetatable({a = 1}, {__pairs = function() while true do end end})")
    case("table keys aren't stringified", "<table key>",
         "local k = setmetatable({}, {__tostring = function() while true do end end}) return {[k] = 1}")
    case("control bytes are escaped", "\\\\x00",               "return 'a\\0b'")
    case("compile errors are reported", "compile error",       "return +")

    -- Host isolation.
    local host_rep = string.rep
    pcall(sandbox.run, "return ('a'):rep(1e9)", DEFAULT, host)
    check("sandbox: host string.rep restored after an error", string.rep == host_rep)
    pcall(sandbox.run, "leaked_global = 1", DEFAULT, host)
    check("sandbox: script globals don't leak to the host", rawget(_G, "leaked_global") == nil)
    pcall(sandbox.run, "math.floor = nil", DEFAULT, host)
    check("sandbox: scripts can't modify the host's math library", type(math.floor) == "function")

    -- Consent gate, with stand-in forms so a test can click the controls.
    do
        local t = 0
        local ctl, checked, enabled, text, on_close, nexth = {}, {}, {}, {}, nil, 0
        local function h() nexth = nexth + 1; return nexth end
        local forms = {
            newform     = function(_, _, _, oc) on_close = oc; return h() end,
            label       = function() return h() end,
            checkbox    = function() local x = h(); checked[x] = false; return x end,
            button      = function(_, cap, click) local x = h(); ctl[cap] = { handle = x, click = click }; enabled[x] = true; return x end,
            addclick    = function(x, fn) ctl.box = { handle = x, click = fn } end,
            ischecked   = function(x) return checked[x] end,
            setproperty = function(x, p, v) if p == "Enabled" then enabled[x] = v elseif p == "Text" then text[x] = v end end,
            destroy     = function() if on_close then on_close() end end,
        }
        local notified = 0
        local gate = sandbox.new_gate({ forms = forms, has = host.has, now = function() return t end,
                                        notify = function() notified = notified + 1 end })
        local function tick(v) checked[ctl.box.handle] = v; ctl.box.click() end
        local function g(name, cond) check("gate: " .. name, cond) end

        g("first request opens the dialog",       gate.request() == "opened")
        g("the attention sound plays once",        notified == 1)
        g("a second request doesn't open another", gate.request() == "open" and notified == 1)
        g("disabled before anything is clicked",   gate.enabled() == false)
        g("Accept starts grayed out",              enabled[ctl.Accept.handle] == false)
        ctl.Accept.click()
        g("Accept while unticked does nothing",    gate.enabled() == false)
        tick(true)
        g("ticking the box enables Accept",        enabled[ctl.Accept.handle] == true)
        g("ticking alone is not consent",          gate.enabled() == false)
        tick(false)
        g("unticking grays Accept out again",      enabled[ctl.Accept.handle] == false)
        tick(true); ctl.Accept.click()
        g("tick + Accept is consent",              gate.enabled() == true)
        g("Cancel becomes Disable",                text[ctl.Cancel.handle] == "Disable")
        ctl.Cancel.click()
        g("Disable withdraws consent",             gate.enabled() == false)
        g("closing starts the cooldown",           gate.request() == "cooldown")
        t = t + sandbox.REPROMPT_COOLDOWN - 1
        g("still cooling down 1 s before the end", gate.request() == "cooldown")
        t = t + 2
        g("reopens after the cooldown",            gate.request() == "opened")
        tick(true)
        on_close()
        g("closing with the X is never consent",   gate.enabled() == false)
        g("the X starts the cooldown too",         gate.request() == "cooldown")
        t = t + sandbox.REPROMPT_COOLDOWN + 1
        gate.request(); ctl.Cancel.click()
        g("Cancel before Accept is not consent",   gate.enabled() == false)

        local no_forms = sandbox.new_gate({ forms = nil, has = host.has, now = function() return 0 end, notify = function() end })
        g("no forms API: unavailable, never enabled", no_forms.request() == "unavailable" and no_forms.enabled() == false)
    end

    -- json.lua.
    do
        local function j(name, cond) check("json: " .. name, cond) end
        j("arrays",           json.encode({ 1, 2, 3 }) == "[1,2,3]")
        j("nil holes become an object", json.encode({ [1] = "a", [3] = "c" }):find('"3":"c"', 1, true) ~= nil)
        j("empty table",      json.encode({}) == "{}")
        j("non-finite numbers become null", json.encode({ 1 / 0, 0 / 0 }) == "[null,null]")
        j("quotes and newlines escaped", json.encode('a"b\nc') == '"a\\"b\\nc"')
        local back = json.decode('{"id":3,"params":{"list":[1,2],"s":"x\\"y"}}')
        j("decode objects, arrays and escapes", back.id == 3 and back.params.list[2] == 2 and back.params.s == 'x"y')
        j("decode rejects garbage", not pcall(json.decode, "{oops"))
    end

    return results
end
