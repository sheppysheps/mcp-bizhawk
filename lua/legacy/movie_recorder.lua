-- movie_recorder.lua: record what a BizHawk movie does, frame by frame, to files.
--
-- Runs inside BizHawk 1.x (Lua 5.1 / LuaInterface; tested on 1.11.7) and needs
-- nothing from the mcp-bizhawk bridge, because those BizHawk builds have no
-- socket API. Core-agnostic: works with any movie, game and core; memory
-- domains and watch addresses come from the config file.
--
-- What it records (all into the `out` folder named in the config):
--   frames.tsv     every frame: frame counter, lag flag, the movie's input
--   snap_f<frame>_<domain>.bin + snapshots.tsv   memory dumps at chosen frames
--   shot_f<frame>.png    screenshots at the same frames
--   writes.tsv     for watched System Bus addresses: frame, address, old and
--                  new value, and the code address (PC) that wrote it
--                  ("init" rows give each watched byte's value at the start)
--   run.tsv        metadata: ROM hash, movie length, config, totals, end reason
--
-- How to run (BizHawk 1.x ignores --lua on the command line):
--   1. Emulation > Pause, then File > Movie > Play Movie, tick Read-only.
--   2. Tools > Lua Console > Script > Open Script > this file.
--      It reads movie_recorder.cfg from the same folder (see the example).
--   3. Check the script is switched ON in the Lua Console (a listed script can
--      be off), then unpause. It prints "RECORDING" at the start, a progress
--      line every progress_every frames, and "done" at the end. Silence means
--      it isn't running.
--
-- SYSTEM ACCESS (why, and the limits): this file writes files with `io`,
-- because BizHawk 1.x has no other way to get data out. It only writes inside
-- the configured `out` folder, using fixed file names; it never reads or runs
-- other files, never uses `os.execute`, `luanet` or the network, and never
-- executes its config (the config is parsed as plain `key = value` text).
-- BizHawk 1.x Lua is NOT sandboxed: only run scripts you have reviewed.

local M = {}
M.VERSION = "2"

-- ── Pure helpers (unit-tested on stock Lua; no BizHawk needed) ───────────

--- Trim spaces from both ends.
--- @return the trimmed string.
--- @requires Nothing.
function M.trim(s)
    return (s:gsub("^%s+", ""):gsub("%s+$", ""))
end

--- Parse a number written as decimal or 0x-prefixed hex.
--- @return the integer, or nil if `s` isn't a whole number.
--- @requires Nothing.
function M.parse_int(s)
    s = M.trim(s)
    local n
    if s:match("^0[xX]%x+$") then n = tonumber(s:sub(3), 16)
    elseif s:match("^%d+$") then n = tonumber(s, 10) end
    return n
end

--- Parse the recorder config: `key = value` lines, `#` comments.
--- Keys: out (required), domains, snapshot_every, snapshot_frames,
--- screenshots, watch (repeatable: "<start> <length>" on the System Bus),
--- snapshot_on_change (repeatable: "<start> <length> [delay]": watch these
--- bytes AND snapshot `delay` frames after any of them changes, default 30,
--- e.g. a "current room" variable, so each room gets a picture once drawn),
--- max_watch_bytes, turbo, exit_at_end, flush_every, progress_every (print a
--- progress line to the Lua Console every N frames; 0 = off).
--- @return config table, or nil + error message (with the line number).
--- @requires Nothing.
function M.parse_config(text)
    local cfg = {
        out = nil, domains = {}, snapshot_every = 0, snapshot_frames = {},
        screenshots = true, watch = {}, on_change = {}, max_watch_bytes = 4096,
        turbo = true, exit_at_end = false, flush_every = 600, progress_every = 10000,
    }
    local function bool(v, line)
        v = v:lower()
        if v == "true" or v == "yes" or v == "1" then return true end
        if v == "false" or v == "no" or v == "0" then return false end
        error("line " .. line .. ": expected true or false, got '" .. v .. "'", 0)
    end
    local ok, err = pcall(function()
        local line = 0
        for raw in (text .. "\n"):gmatch("([^\n]*)\n") do
            line = line + 1
            local s = M.trim((raw:gsub("#.*$", "")))
            if s ~= "" then
                local key, value = s:match("^([%w_]+)%s*=%s*(.-)$")
                if not key then error("line " .. line .. ": expected 'key = value'", 0) end
                if key == "out" then
                    cfg.out = (value:gsub("\\", "/"):gsub("/+$", ""))
                elseif key == "domains" then
                    for d in value:gmatch("[^,]+") do
                        local name = M.trim(d)
                        if name ~= "" then cfg.domains[#cfg.domains + 1] = name end
                    end
                elseif key == "snapshot_frames" then
                    for f in value:gmatch("[^,]+") do
                        local n = M.parse_int(f)
                        if not n then error("line " .. line .. ": bad frame number '" .. M.trim(f) .. "'", 0) end
                        cfg.snapshot_frames[n] = true
                    end
                elseif key == "watch" then
                    local a, b = value:match("^(%S+)%s+(%S+)$")
                    local start, len = a and M.parse_int(a), b and M.parse_int(b)
                    if not start or not len or len < 1 then
                        error("line " .. line .. ": watch needs '<start> <length>', e.g. 0xC000 0x100", 0)
                    end
                    cfg.watch[#cfg.watch + 1] = { start = start, length = len }
                elseif key == "snapshot_on_change" then
                    local a, b, c = value:match("^(%S+)%s+(%S+)%s*(%S*)$")
                    local start, len = a and M.parse_int(a), b and M.parse_int(b)
                    local delay = (c and c ~= "") and M.parse_int(c) or 30
                    if not start or not len or len < 1 or not delay then
                        error("line " .. line .. ": snapshot_on_change needs '<start> <length> [delay]', "
                            .. "e.g. 0xC9F9 2 30", 0)
                    end
                    cfg.on_change[#cfg.on_change + 1] = { start = start, length = len, delay = delay }
                elseif key == "snapshot_every" or key == "max_watch_bytes" or key == "flush_every"
                    or key == "progress_every" then
                    local n = M.parse_int(value)
                    if not n then error("line " .. line .. ": " .. key .. " needs a whole number", 0) end
                    cfg[key] = n
                elseif key == "screenshots" or key == "turbo" or key == "exit_at_end" then
                    cfg[key] = bool(value, line)
                else
                    error("line " .. line .. ": unknown key '" .. key .. "'", 0)
                end
            end
        end
    end)
    if not ok then return nil, err end
    if not cfg.out or cfg.out == "" then return nil, "config needs 'out = <folder>'" end
    local total = 0
    for _, w in ipairs(cfg.watch) do total = total + w.length end
    for _, w in ipairs(cfg.on_change) do total = total + w.length end
    if total > cfg.max_watch_bytes then
        return nil, ("watch ranges cover %d bytes, over max_watch_bytes (%d): every watched byte "
            .. "costs a callback, so keep ranges narrow or raise the limit"):format(total, cfg.max_watch_bytes)
    end
    return cfg
end

--- Decide whether to snapshot at this frame, and why.
--- `pending` maps frame -> reason for snapshots scheduled by snapshot_on_change.
--- @return a reason ("listed", "every", or the pending reason), or nil for no snapshot.
--- @requires Nothing.
function M.snapshot_reason(cfg, frame, pending)
    if pending and pending[frame] then return pending[frame] end
    if cfg.snapshot_frames[frame] then return "listed" end
    if cfg.snapshot_every > 0 and frame > 0 and frame % cfg.snapshot_every == 0 then return "every" end
    return nil
end

--- Count the keys of a table (sets like snapshot_frames have no length).
--- @return the number of keys.
--- @requires Nothing.
function M.count_keys(t)
    local n = 0
    for _ in pairs(t) do n = n + 1 end
    return n
end

--- Backwards-compatible yes/no form of snapshot_reason (no pending list).
--- @return true when `frame` is listed or a multiple of snapshot_every.
--- @requires Nothing.
function M.should_snapshot(cfg, frame)
    return M.snapshot_reason(cfg, frame, nil) ~= nil
end

--- Every watched address with its snapshot delay (nil = plain watch).
--- @return table address -> delay or false, covering watch and snapshot_on_change.
--- @requires Nothing.
function M.watched_addresses(cfg)
    local out = {}
    for _, w in ipairs(cfg.watch) do
        for a = w.start, w.start + w.length - 1 do if out[a] == nil then out[a] = false end end
    end
    for _, w in ipairs(cfg.on_change) do
        for a = w.start, w.start + w.length - 1 do out[a] = w.delay end
    end
    return out
end

--- Format one input table ({Button = true/false, ...}) as text.
--- @return pressed button names joined by '|' in sorted order, or "." for none.
--- @requires Nothing.
function M.input_string(input)
    if type(input) ~= "table" then return "." end
    local pressed = {}
    for name, value in pairs(input) do
        if value == true then pressed[#pressed + 1] = tostring(name) end
    end
    if #pressed == 0 then return "." end
    table.sort(pressed)
    return table.concat(pressed, "|")
end

--- File name for a snapshot or screenshot.
--- @return e.g. "snap_f0000146_WRAM.bin" or "shot_f0000146.png".
--- @requires Nothing.
function M.snap_name(frame, domain)
    if domain then return ("snap_f%07d_%s.bin"):format(frame, (domain:gsub("[^%w]", "_"))) end
    return ("shot_f%07d.png"):format(frame)
end

--- Turn a readbyterange() result into a byte string.
--- BizHawk versions differ in whether keys start at 0 or 1, so both are tried.
--- @return string of `len` bytes (missing bytes become 0).
--- @requires Nothing.
function M.bytes_to_string(t, len)
    local parts, chunk = {}, {}
    local base = (t[0] ~= nil) and 0 or 1
    for i = 0, len - 1 do
        chunk[#chunk + 1] = string.char((t[base + i] or 0) % 256)
        if #chunk == 4096 then parts[#parts + 1] = table.concat(chunk); chunk = {} end
    end
    parts[#parts + 1] = table.concat(chunk)
    return table.concat(parts)
end

-- ── BizHawk side ──────────────────────────────────────────────────────────

--- Buffered writer for one text file (keeps disk writes rare at high speed).
--- @return object with :add(line), :flush(), :close().
--- @requires io (writes only the given path).
local function buffered(path)
    local f = assert(io.open(path, "w"), "can't write " .. path .. " (does the out folder exist?)")
    local buf = {}
    local w = {}
    function w:add(line) buf[#buf + 1] = line end
    function w:flush()
        if #buf > 0 then f:write(table.concat(buf, "\n"), "\n"); buf = {} end
        f:flush()
    end
    function w:close() self:flush(); f:close() end
    return w
end

--- Call a BizHawk function if it exists, ignoring errors.
--- @return the function's result, or nil.
--- @requires Nothing.
local function try(f, ...)
    if type(f) ~= "function" then return nil end
    local ok, r = pcall(f, ...)
    if ok then return r end
    return nil
end

--- Folder holding this script (for the config file next to it).
--- @return path with forward slashes and a trailing slash, or "" if unknown.
--- @requires debug.getinfo (available in BizHawk 1.x Lua).
local function script_dir()
    local src = debug and debug.getinfo and debug.getinfo(1, "S").source or ""
    src = src:gsub("^@", ""):gsub("\\", "/")
    return src:match("^(.*/)") or ""
end

--- Record the playing movie until it ends. Entry point inside BizHawk.
--- @return nothing; writes the files described at the top of this file.
--- @requires BizHawk Lua: emu, movie, memory, client, event, gameinfo; io.
local function run()
    local cfg_path = script_dir() .. "movie_recorder.cfg"
    local fh = io.open(cfg_path, "r")
    if not fh then error("movie_recorder: no config at " .. cfg_path .. " (copy movie_recorder.example.cfg)") end
    local text = fh:read("*a"); fh:close()
    local cfg, err = M.parse_config(text)
    if not cfg then error("movie_recorder: " .. err) end

    local mode = try(movie.mode)
    if mode ~= "PLAY" then
        error("movie_recorder: a movie must be playing in read-only mode (movie.mode() = " .. tostring(mode)
            .. "). Recording mode would let inputs overwrite the movie.")
    end

    local out = cfg.out .. "/"
    local start_frame = emu.framecount()
    local frames = buffered(out .. "frames.tsv")
    local snaps = buffered(out .. "snapshots.tsv")
    local writes = buffered(out .. "writes.tsv")
    frames:add("frame\tlag\tinput")
    snaps:add("frame\tdomain\tfile\tbytes\treason")
    writes:add("frame\taddress\told\tnew\tpc")

    local sizes = {}
    for _, d in ipairs(cfg.domains) do
        sizes[d] = try(memory.getmemorydomainsize, d)
        if not sizes[d] then error("movie_recorder: unknown memory domain '" .. d .. "'") end
    end

    -- Write-watches: callbacks only note "written, by this PC"; values are
    -- read once at frame end (some cores report written values as 0).
    -- Starting values are logged as "init" rows, so a timeline knows the
    -- value before the first change.
    local shadow, dirty, pending = {}, {}, {}
    local delays = M.watched_addresses(cfg)
    local function read_bus(addr)
        memory.usememorydomain("System Bus")
        return memory.readbyte(addr)
    end
    local addrs = {}
    for a in pairs(delays) do addrs[#addrs + 1] = a end
    table.sort(addrs)
    for _, a in ipairs(addrs) do
        shadow[a] = read_bus(a)
        writes:add(("%d\t0x%04X\t%d\t%d\tinit"):format(start_frame, a, shadow[a], shadow[a]))
        event.onmemorywrite(function()
            dirty[a] = try(emu.getregister, "PC") or -1
        end, a)
    end

    if cfg.turbo then
        -- Speed never changes what a frame computes, so this only saves time.
        -- If this BizHawk build lacks these calls, say so instead of failing silently.
        local ok1 = type(client.speedmode) == "function" and pcall(client.speedmode, 6400)
        local ok2 = type(emu.limitframerate) == "function" and pcall(emu.limitframerate, false)
        if not (ok1 or ok2) then
            print("movie_recorder: WARNING couldn't lift the speed limit from Lua; untick "
                .. "Config > Speed/Skip > Clock Throttle to record faster")
        end
    end

    local lag_total, rows = 0, 0
    local length = try(movie.length) or -1
    local reason = "movie ended"
    -- Say loudly that recording started: a script that's listed in the Lua
    -- Console but switched off prints nothing, so silence means "not running".
    print(("movie_recorder v%s: RECORDING from frame %d of %d to %s"):format(M.VERSION, start_frame, length, out))
    print(("movie_recorder: %d domain(s) per snapshot, every %d frames + %d listed, %d watched byte(s)%s")
        :format(#cfg.domains, cfg.snapshot_every, M.count_keys(cfg.snapshot_frames), #addrs,
            cfg.screenshots and ", screenshots on" or ""))
    while true do
        emu.frameadvance()
        local frame = emu.framecount()
        local lag = emu.islagged() and 1 or 0
        lag_total = lag_total + lag
        frames:add(("%d\t%d\t%s"):format(frame, lag, M.input_string(try(movie.getinput, frame - 1))))
        rows = rows + 1

        for a, pc in pairs(dirty) do
            local v = read_bus(a)
            if v ~= shadow[a] then
                local pc_text = (pc >= 0) and ("0x%04X"):format(pc % 0x10000) or "?"
                writes:add(("%d\t0x%04X\t%d\t%d\t%s"):format(frame, a, shadow[a], v, pc_text))
                shadow[a] = v
                if delays[a] then pending[frame + delays[a]] = ("change:0x%04X"):format(a) end
            end
            dirty[a] = nil
        end

        local snap_reason = M.snapshot_reason(cfg, frame, pending)
        pending[frame] = nil
        if snap_reason then
            for _, d in ipairs(cfg.domains) do
                memory.usememorydomain(d)
                local data = M.bytes_to_string(memory.readbyterange(0, sizes[d]), sizes[d])
                local name = M.snap_name(frame, d)
                local f = assert(io.open(out .. name, "wb"))
                f:write(data); f:close()
                snaps:add(("%d\t%s\t%s\t%d\t%s"):format(frame, d, name, #data, snap_reason))
            end
            if cfg.screenshots then try(client.screenshot, out .. M.snap_name(frame)) end
        end

        if rows % cfg.flush_every == 0 then frames:flush(); writes:flush(); snaps:flush() end
        if cfg.progress_every > 0 and frame % cfg.progress_every == 0 then
            print(("movie_recorder: frame %d / %d, %d lag frames so far"):format(frame, length, lag_total))
        end
        local now = try(movie.mode)
        if now ~= "PLAY" then reason = "movie mode became " .. tostring(now); break end
        if length > 0 and frame >= length then break end
    end

    frames:close(); writes:close(); snaps:close()
    local meta = buffered(out .. "run.tsv")
    meta:add("key\tvalue")
    meta:add("recorder_version\t" .. M.VERSION)
    meta:add("lua\t" .. tostring(_VERSION))
    meta:add("rom_name\t" .. tostring(try(gameinfo.getromname)))
    meta:add("rom_hash\t" .. tostring(try(gameinfo.getromhash)))
    meta:add("movie_length\t" .. tostring(length))
    meta:add("started_at_frame\t" .. start_frame)
    meta:add("ended_at_frame\t" .. emu.framecount())
    meta:add("frames_recorded\t" .. rows)
    meta:add("lag_frames\t" .. lag_total)
    meta:add("end_reason\t" .. reason)
    for d, n in pairs(sizes) do meta:add("domain_size." .. d .. "\t" .. n) end
    meta:add("config\t" .. (text:gsub("\r", ""):gsub("\n", " | ")))
    meta:close()
    print(("movie_recorder: done, %d frames (%d lag) from frame %d; files in %s"):format(rows, lag_total, start_frame, out))
    if start_frame > 0 then print("movie_recorder: WARNING started at frame " .. start_frame .. ", not 0 (see the how-to at the top)") end
    if cfg.exit_at_end then try(client.exit) end
end

-- Opened as a script inside BizHawk: record. Loaded with require() (tests,
-- in or out of BizHawk), `...` holds the module name: return the helpers only.
local required_as = ...
if required_as == nil and emu ~= nil and memory ~= nil and movie ~= nil then
    run()
else
    return M
end
