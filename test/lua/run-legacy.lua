-- Runs the specs for lua/legacy/ (scripts for BizHawk 1.x, which embeds Lua 5.1).
-- Usage (from the repo root): lua5.1 test/lua/run-legacy.lua   (also works on 5.4)
-- Exits 0 when every check passes, 1 otherwise.

package.path = "lua/legacy/?.lua;test/lua/?.lua;" .. package.path
local results = require("movie_recorder_spec")(require("movie_recorder"))

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
