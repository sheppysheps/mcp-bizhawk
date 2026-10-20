-- Command-line runner for test/lua/sandbox_spec.lua on stock Lua 5.4.
-- Usage (from the repo root): lua5.4 test/lua/run.lua
-- Exits 0 when every check passes, 1 otherwise.

package.path = "lua/?.lua;lua/legacy/?.lua;test/lua/?.lua;" .. package.path
local results = require("sandbox_spec")(require("sandbox"), require("json"))
for _, r in ipairs(require("snapshot_spec")(require("snapshot"))) do results[#results + 1] = r end
for _, r in ipairs(require("bytesearch_spec")(require("bytesearch"))) do results[#results + 1] = r end
for _, r in ipairs(require("movie_recorder_spec")(require("movie_recorder"))) do results[#results + 1] = r end

local failed = 0
for _, r in ipairs(results) do
    if r.pass then
        print("ok   " .. r.name)
    else
        failed = failed + 1
        print("FAIL " .. r.name .. (r.detail and ("\n     " .. r.detail) or ""))
    end
end
print(string.format("\n%d checks, %d passed, %d failed (%s)", #results, #results - failed, failed, _VERSION))
os.exit(failed == 0 and 0 or 1)
