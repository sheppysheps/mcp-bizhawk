-- Runs test/lua/sandbox_spec.lua inside BizHawk's NLua, plus checks that only
-- mean something there. Started by test/lua/run-in-bizhawk.mjs, which passes the
-- repo root and an output file through MCP_BIZHAWK_TEST_* globals set in the
-- generated launcher script. Writes one line per check, then exits BizHawk.

local root, out_path = MCP_BIZHAWK_TEST_ROOT, MCP_BIZHAWK_TEST_OUT
package.path = root .. "/lua/?.lua;" .. root .. "/lua/legacy/?.lua;" .. root .. "/test/lua/?.lua;" .. package.path
local sandbox, json = require("sandbox"), require("json")
local results = require("sandbox_spec")(sandbox, json)
for _, r in ipairs(require("snapshot_spec")(require("snapshot"))) do results[#results + 1] = r end
for _, r in ipairs(require("bytesearch_spec")(require("bytesearch"))) do results[#results + 1] = r end
-- require() hands the module its name, so the recorder returns its helpers here instead of recording.
for _, r in ipairs(require("movie_recorder_spec")(require("movie_recorder"))) do results[#results + 1] = r end

--- Record an NLua-only check.
local function check(name, pass, detail)
    results[#results + 1] = { name = "nlua: " .. name, pass = pass and true or false, detail = detail }
end

local function has(t, n) local v = t and t[n]; return type(v) == "function" or type(v) == "userdata" end
local real = { memory = memory, mainmemory = mainmemory, emu = emu, gui = gui,
               client = client, gameinfo = gameinfo, bit = bit, has = has }
local DEFAULT = { max_instructions = 1000000, max_memory_kb = 16384 }

check("luanet exists on the host (so blocking it matters)", type(luanet) == "table")
local ok, r = pcall(sandbox.run, "return luanet == nil, type(memory.read_u8), type(emu.framecount)", DEFAULT, real)
check("real BizHawk functions arrive wrapped, luanet stays out",
      ok and r.values[1] == true and r.values[2] == "function" and r.values[3] == "function",
      ok and json.encode(r) or tostring(r))
ok, r = pcall(sandbox.run, "return emu.framecount() >= 0, gameinfo.getromname() ~= nil", DEFAULT, real)
check("real reads work through the sandbox", ok and r.values[1] == true and r.values[2] == true,
      ok and json.encode(r) or tostring(r))
ok, r = pcall(sandbox.run, "return memory.read_bytes_as_array(0, 70000, 'ROM')", DEFAULT, real)
check("bulk-read cap holds on real BizHawk", not ok and tostring(r):find("length over", 1, true) ~= nil, tostring(r))

local lines = {}
for _, x in ipairs(results) do
    lines[#lines + 1] = (x.pass and "ok   " or "FAIL ") .. x.name .. ((not x.pass and x.detail) and (" | " .. x.detail) or "")
end
lines[#lines + 1] = "DONE " .. _VERSION
local f = assert(io.open(out_path, "w"))
f:write(table.concat(lines, "\n")); f:close()
client.exit()
