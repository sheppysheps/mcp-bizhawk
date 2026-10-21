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
--   shot_f<frame>_<reason>.png    screenshots at the same frames, named by
--                  why they were taken (every, listed, change-0x<address>)
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
--   4. Every stage (and any error, with its traceback) is also written to
--      movie_recorder.log next to this script, so a run that stops early can
--      be diagnosed from the file even when the console shows nothing.
--
-- SYSTEM ACCESS (why, and the limits): this file writes files with `io`,
-- because BizHawk 1.x has no other way to get data out. It only writes inside
-- the configured `out` folder, using fixed file names, plus its own
-- movie_recorder.log next to the script (stage log); it never reads or runs
-- other files, never uses `os.execute`, `luanet` or the network, and never
-- executes its config (the config is parsed as plain `key = value` text).
-- BizHawk 1.x Lua is NOT sandboxed: only run scripts you have reviewed.

local M = {}
M.VERSION = "12"

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
--- snapshot_range (repeatable: "<start> <end> <step>": snapshot every <step>
--- frames from <start> to <end>, for dense coverage of a window), stop_after
--- (end the recording after this frame; 0 = play to the end), screenshots, watch (repeatable: "<start> <length>" on the System Bus),
--- snapshot_on_change (repeatable: "<start> <length> [delay]": watch these
--- bytes AND snapshot `delay` frames after any of them changes, default 30,
--- e.g. a "current room" variable, so each room gets a picture once drawn),
--- max_watch_bytes, turbo, exit_at_end, flush_every, progress_every (print a
--- progress line to the Lua Console every N frames; 0 = off), heartbeat_every
--- (write a line to movie_recorder.log only, every N frames, to locate a hang;
--- 0 = off), trace_snapshots (log each snapshot step; default true),
--- exec_watch (repeatable: "<address> [stack <words>] [capture <start> <length>]
--- [max <hits>]": log registers each time the address executes; see
--- M.parse_exec_watch).
--- @return config table, or nil + error message (with the line number).
--- @requires Nothing.
function M.parse_config(text)
    local cfg = {
        out = nil, domains = {}, snapshot_every = 0, snapshot_frames = {},
        screenshots = true, watch = {}, on_change = {}, max_watch_bytes = 4096,
        turbo = true, exit_at_end = false, flush_every = 600, progress_every = 10000,
        heartbeat_every = 0, trace_snapshots = true, exec = {}, snapshot_ranges = {}, stop_after = 0,
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
                elseif key == "snapshot_range" then
                    local a, b, c = value:match("^(%S+)%s+(%S+)%s+(%S+)$")
                    local first, last = a and M.parse_int(a), b and M.parse_int(b)
                    local step = c and M.parse_int(c)
                    if not first or not last or not step or step < 1 or last < first then
                        error("line " .. line .. ": snapshot_range needs '<start> <end> <step>' with "
                            .. "start <= end and step >= 1, e.g. 195800 198300 10", 0)
                    end
                    cfg.snapshot_ranges[#cfg.snapshot_ranges + 1] = { start = first, stop = last, step = step }
                elseif key == "snapshot_frames" then
                    for f in value:gmatch("[^,]+") do
                        local n = M.parse_int(f)
                        if not n then error("line " .. line .. ": bad frame number '" .. M.trim(f) .. "'", 0) end
                        cfg.snapshot_frames[n] = true
                    end
                elseif key == "watch" then
                    local w, werr = M.parse_watch(value)
                    if not w then error("line " .. line .. ": " .. werr, 0) end
                    cfg.watch[#cfg.watch + 1] = w
                elseif key == "snapshot_on_change" then
                    local a, b, c = value:match("^(%S+)%s+(%S+)%s*(%S*)$")
                    local start, len = a and M.parse_int(a), b and M.parse_int(b)
                    local delay = (c and c ~= "") and M.parse_int(c) or 30
                    if not start or not len or len < 1 or not delay then
                        error("line " .. line .. ": snapshot_on_change needs '<start> <length> [delay]', "
                            .. "e.g. 0xC9F9 2 30", 0)
                    end
                    cfg.on_change[#cfg.on_change + 1] = { start = start, length = len, delay = delay }
                elseif key == "exec_watch" then
                    local w, werr = M.parse_exec_watch(value)
                    if not w then error("line " .. line .. ": " .. werr, 0) end
                    if #cfg.exec >= M.EXEC_MAX_WATCHES then
                        error(("line %d: at most %d exec_watch lines"):format(line, M.EXEC_MAX_WATCHES), 0)
                    end
                    cfg.exec[#cfg.exec + 1] = w
                elseif key == "snapshot_every" or key == "max_watch_bytes" or key == "flush_every"
                    or key == "progress_every" or key == "heartbeat_every" or key == "stop_after" then
                    local n = M.parse_int(value)
                    if not n then error("line " .. line .. ": " .. key .. " needs a whole number", 0) end
                    cfg[key] = n
                elseif key == "screenshots" or key == "turbo" or key == "exit_at_end"
                    or key == "trace_snapshots" then
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

-- Limits for exec_watch: each watch is a callback on every execution of its
-- address, and each hit reads registers and memory, so keep them bounded.
M.EXEC_MAX_WATCHES = 32
M.EXEC_MAX_STACK = 16      -- stack words per hit
M.EXEC_MAX_CAPTURE = 64    -- captured bytes per range
M.EXEC_DEFAULT_MAX = 1000  -- hits logged per address (the rest are counted)

--- Parse the hit options shared by exec_watch and register-logging watch
--- lines, starting at words[i]: "registers" (a flag, watch only), "stack
--- <words>", "capture <start> <length>" (repeatable), "max <hits>". `stack`
--- reads that many 2-byte words at the SP register (the return addresses, i.e.
--- who called the code); `capture` reads System Bus bytes (e.g. a game's
--- "current ROM bank" variable); `max` caps logged hits (default EXEC_DEFAULT_MAX).
--- @return options table {stack, captures = {{start, length}...}, max, any}
---         (`any` is true when an option was given), or nil + error text.
--- @requires Nothing.
function M.parse_hit_options(words, i, what, allow_registers)
    local o = { stack = 0, captures = {}, max = M.EXEC_DEFAULT_MAX, any = false }
    while i <= #words do
        local opt = words[i]:lower()
        if opt == "registers" and allow_registers then
            i = i + 1
        elseif opt == "stack" then
            local n = words[i + 1] and M.parse_int(words[i + 1])
            if not n or n > M.EXEC_MAX_STACK then
                return nil, ("%s: stack needs a word count 0..%d"):format(what, M.EXEC_MAX_STACK)
            end
            o.stack, i = n, i + 2
        elseif opt == "capture" then
            local s = words[i + 1] and M.parse_int(words[i + 1])
            local n = words[i + 2] and M.parse_int(words[i + 2])
            if not s or not n or n < 1 or n > M.EXEC_MAX_CAPTURE then
                return nil, ("%s: capture needs '<start> <length>' with length 1..%d"):format(what, M.EXEC_MAX_CAPTURE)
            end
            o.captures[#o.captures + 1] = { start = s, length = n }
            i = i + 3
        elseif opt == "max" then
            local n = words[i + 1] and M.parse_int(words[i + 1])
            if not n or n < 1 then return nil, what .. ": max needs a whole number of hits >= 1" end
            o.max, i = n, i + 2
        else
            return nil, ("%s: unknown option '%s' (expected %sstack, capture or max)"):format(
                what, words[i], allow_registers and "registers, " or "")
        end
        o.any = true
    end
    return o
end

--- Split a config value into words.
--- @return list of the space-separated words.
--- @requires Nothing.
local function split_words(value)
    local words = {}
    for w in value:gmatch("%S+") do words[#words + 1] = w end
    return words
end

--- Parse one exec_watch value: "<address> [stack <words>] [capture <start>
--- <length>]... [max <hits>]" (options as in M.parse_hit_options).
--- @return table {address, stack, captures, max}, or nil + error text.
--- @requires Nothing.
function M.parse_exec_watch(value)
    local words = split_words(value)
    local addr = words[1] and M.parse_int(words[1])
    if not addr then
        return nil, "exec_watch needs '<address> [stack <words>] [capture <start> <length>] [max <hits>]'"
    end
    local o, err = M.parse_hit_options(words, 2, "exec_watch", false)
    if not o then return nil, err end
    o.address = addr
    return o
end

--- Parse one watch value: "<start> <length> [registers] [stack <words>]
--- [capture <start> <length>]... [max <hits>]". With any option, every write
--- to the range is also logged with the registers at that moment (to
--- write_hits.tsv), not only value changes: the registers of the code doing
--- a copy show where it copies from.
--- @return table {start, length, registers, stack, captures, max}, or nil + error text.
--- @requires Nothing.
function M.parse_watch(value)
    local words = split_words(value)
    local start, len = words[1] and M.parse_int(words[1]), words[2] and M.parse_int(words[2])
    if not start or not len or len < 1 then
        return nil, "watch needs '<start> <length> [registers] [stack <words>] [capture <start> <length>] "
            .. "[max <hits>]', e.g. 0xC000 0x100"
    end
    local o, err = M.parse_hit_options(words, 3, "watch", true)
    if not o then return nil, err end
    o.start, o.length, o.registers = start, len, o.any
    return o
end

--- Format a register table ({A = 18, PC = 24566, ...}) as text.
--- @return "A=0x12 B=0x00 ... PC=0x5FF6" with names sorted, or "?" when empty or not a table.
--- @requires Nothing.
function M.registers_string(regs)
    if type(regs) ~= "table" then return "?" end
    local names = {}
    for k, v in pairs(regs) do
        if type(v) == "number" then names[#names + 1] = tostring(k) end
    end
    if #names == 0 then return "?" end
    table.sort(names)
    local parts = {}
    for _, k in ipairs(names) do parts[#parts + 1] = ("%s=0x%X"):format(k, regs[k]) end
    return table.concat(parts, " ")
end

--- Format a list of byte values as hex text.
--- @return e.g. "0A1BFF", or "" for an empty list.
--- @requires Nothing.
function M.hex_bytes(bytes)
    local parts = {}
    for i = 1, #bytes do parts[i] = ("%02X"):format(bytes[i] % 256) end
    return table.concat(parts)
end

--- Decide whether to snapshot at this frame, and why.
--- `pending` maps frame -> reason for snapshots scheduled by snapshot_on_change.
--- @return a reason ("listed", "range", "every", or the pending reason), or nil for no snapshot.
--- @requires Nothing.
function M.snapshot_reason(cfg, frame, pending)
    if pending and pending[frame] then return pending[frame] end
    if cfg.snapshot_frames[frame] then return "listed" end
    for _, r in ipairs(cfg.snapshot_ranges or {}) do
        if frame >= r.start and frame <= r.stop and (frame - r.start) % r.step == 0 then return "range" end
    end
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

--- File name for a snapshot or screenshot. A screenshot name carries the
--- snapshot reason when given, so a folder listing shows why it was taken.
--- @return e.g. "snap_f0000146_WRAM.bin", "shot_f0000146.png" or
---         "shot_f0006339_change-0xC3F0.png" (reason "change:0xC3F0").
--- @requires Nothing.
function M.snap_name(frame, domain, reason)
    if domain then return ("snap_f%07d_%s.bin"):format(frame, (domain:gsub("[^%w]", "_"))) end
    if reason then return ("shot_f%07d_%s.png"):format(frame, (reason:gsub("[^%w]+", "-"))) end
    return ("shot_f%07d.png"):format(frame)
end

--- Detect the shape of a readbyterange(0, n) result. BizHawk 2.x and 1.11.7
--- return numbers keyed from 0 (or 1); some older builds are documented as
--- returning hex strings for both keys and values, e.g. t["1F"] = "A0".
--- @return "zero", "one" or "hex", or nil when the shape is unknown (e.g. empty).
--- @requires Nothing.
function M.range_style(t)
    if type(t) ~= "table" then return nil end
    if t[0] ~= nil then return "zero" end
    if t[1] ~= nil then return "one" end
    if t["00"] ~= nil or t["0"] ~= nil then return "hex" end
    return nil
end

--- Describe the first few entries of a table, for the log.
--- @return text like "00(string)=FF(string), ...", or the type for non-tables.
--- @requires Nothing.
function M.describe_table(t, n)
    if type(t) ~= "table" then return "a " .. type(t) end
    local parts = {}
    for k, v in pairs(t) do
        parts[#parts + 1] = ("%s(%s)=%s(%s)"):format(tostring(k), type(k), tostring(v), type(v))
        if #parts >= n then break end
    end
    return #parts > 0 and table.concat(parts, ", ") or "empty"
end

--- Turn a readbyterange() result into a byte string (any style above).
--- @return string of `len` bytes (missing bytes become 0).
--- @requires Nothing.
function M.bytes_to_string(t, len)
    local parts, chunk = {}, {}
    local style = M.range_style(t)
    local base = (style == "zero") and 0 or 1
    for i = 0, len - 1 do
        local v
        if style == "hex" then
            v = t[("%02X"):format(i)]
            v = v and tonumber(v, 16)
        else
            v = t[base + i]
        end
        chunk[#chunk + 1] = string.char((v or 0) % 256)
        if #chunk == 4096 then parts[#parts + 1] = table.concat(chunk); chunk = {} end
    end
    parts[#parts + 1] = table.concat(chunk)
    return table.concat(parts)
end

-- ── BizHawk side ──────────────────────────────────────────────────────────

local log_path = nil -- set when running inside BizHawk; nil = console only

--- Report a stage to the Lua Console and append it to movie_recorder.log.
--- The log is opened, appended and closed per line, so it survives a crash
--- and shows the last stage reached even when the console shows nothing.
--- With `quiet`, it skips the console (for frequent lines like heartbeats).
--- @return nothing.
--- @requires print; io (appends to log_path only, next to this script).
local function trace(msg, quiet)
    local line = "movie_recorder: " .. msg
    if not quiet then print(line) end
    if not log_path then return end
    local f = io.open(log_path, "a")
    if f then f:write(line, "\n"); f:close() end
end

--- xpcall error handler that keeps the stack trace.
--- @return the error message followed by a traceback (or the bare message).
--- @requires debug.traceback when available.
local function traceback(e)
    if debug and debug.traceback then return debug.traceback(tostring(e), 2) end
    return tostring(e)
end

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

--- Whether `f` can be called. BizHawk 1.x (LuaInterface) exposes its API
--- functions as userdata, not Lua functions, so both count.
--- @return true for a function or userdata.
--- @requires Nothing.
local function callable(f)
    local t = type(f)
    return t == "function" or t == "userdata"
end

--- Call a BizHawk function if it exists, ignoring errors.
--- @return the function's result, or nil (missing function or error).
--- @requires Nothing.
local function try(f, ...)
    if not callable(f) then return nil end
    local ok, r = pcall(f, ...)
    if ok then return r end
    return nil
end

--- Read the movie mode, upper-cased so builds that differ in case agree.
--- @return e.g. "PLAY", or nil + a reason (missing function or the error text).
--- @requires BizHawk movie.mode().
local function movie_mode()
    if not callable(movie.mode) then return nil, "movie.mode is a " .. type(movie.mode) end
    local ok, r = pcall(movie.mode)
    if not ok then return nil, "movie.mode() failed: " .. tostring(r) end
    return tostring(r):upper()
end

--- Read a whole memory domain as a byte string. Reads `read_len[d]` bytes
--- with readbyterange and, when that is one short of the domain (builds
--- that refuse any range reaching the domain end), the last byte with readbyte.
--- @return string of exactly sizes[d] bytes.
--- @requires BizHawk memory API; st.sizes and st.read_len filled by setup().
local function read_domain(st, d)
    local size, n = st.sizes[d], st.read_len[d]
    memory.usememorydomain(d)
    local data = M.bytes_to_string(memory.readbyterange(0, n), n)
    if n < size then data = data .. string.char(memory.readbyte(size - 1) % 256) end
    return data
end

--- Folder holding this script (for the config file next to it).
--- @return path with forward slashes and a trailing slash, or "" if unknown.
--- @requires debug.getinfo (available in BizHawk 1.x Lua).
local function script_dir()
    local src = debug and debug.getinfo and debug.getinfo(1, "S").source or ""
    src = src:gsub("^@", ""):gsub("\\", "/")
    return src:match("^(.*/)") or ""
end

--- Read one byte from the System Bus.
--- @return the byte value.
--- @requires BizHawk memory API with a "System Bus" domain.
local function read_bus(addr)
    memory.usememorydomain("System Bus")
    return memory.readbyte(addr)
end

--- Read the config, check the movie, open the output files and register the
--- write-watches. Never yields, so it can run inside xpcall (Lua 5.1 can't
--- yield across pcall, which is why the frame loop is split out).
--- @return state table used by step() and finish(); raises on any problem.
--- @requires BizHawk Lua: emu, movie, memory, client, event; io.
local function setup()
    local cfg_path = script_dir() .. "movie_recorder.cfg"
    trace("reading config " .. cfg_path)
    local fh = io.open(cfg_path, "r")
    if not fh then error("no config at " .. cfg_path .. " (copy movie_recorder.example.cfg)", 0) end
    local text = fh:read("*a"); fh:close()
    local cfg, err = M.parse_config(text)
    if not cfg then error("bad config: " .. err, 0) end
    trace("config ok, out = " .. cfg.out)

    local mode, why = movie_mode()
    trace("movie.mode() = " .. tostring(mode) .. (why and (" (" .. why .. ")") or ""))
    if mode ~= "PLAY" then
        error("a movie must be playing in read-only mode (movie.mode() = " .. tostring(mode)
            .. "). Recording mode would let inputs overwrite the movie.", 0)
    end

    local st = {
        cfg = cfg, text = text, out = cfg.out .. "/", start_frame = emu.framecount(),
        sizes = {}, shadow = {}, dirty = {}, pending = {}, delays = M.watched_addresses(cfg),
        lag_total = 0, rows = 0, length = try(movie.length) or -1, reason = "movie ended",
    }
    trace("opening output files in " .. st.out)
    st.frames = buffered(st.out .. "frames.tsv")
    st.snaps = buffered(st.out .. "snapshots.tsv")
    st.writes = buffered(st.out .. "writes.tsv")
    st.frames:add("frame\tlag\tinput")
    st.snaps:add("frame\tdomain\tfile\tbytes\treason")
    st.writes:add("frame\taddress\told\tnew\tpc")

    -- Probe readbyterange on each domain with a full-size read, the way
    -- snapshots use it. Some builds (BizHawk 1.11.7) refuse any range that
    -- reaches the domain end, warn, and return nothing; then read one byte
    -- less and fetch the last byte with readbyte (see read_domain). Finally
    -- compare a few bytes with readbyte, so a result this script can't
    -- decode stops the run instead of saving zeros.
    trace("checking memory domains (a one-off 'outside memory domain' warning here is expected on some builds)")
    st.read_len = {}
    for _, d in ipairs(cfg.domains) do
        local size = try(memory.getmemorydomainsize, d)
        if not size then error("unknown memory domain '" .. d .. "'", 0) end
        st.sizes[d] = size
        memory.usememorydomain(d)
        local full = memory.readbyterange(0, size)
        local got_full = M.count_keys(type(full) == "table" and full or {})
        if got_full >= size then
            st.read_len[d] = size
        else
            local short = memory.readbyterange(0, size - 1)
            local got_short = M.count_keys(type(short) == "table" and short or {})
            if got_short < size - 1 then
                error(("readbyterange on %s returned %d of %d bytes (and %d of %d one byte shorter): "
                    .. "can't take snapshots"):format(d, got_full, size, got_short, size - 1), 0)
            end
            st.read_len[d] = size - 1
        end
        local data = read_domain(st, d)
        memory.usememorydomain(d)
        local bad = {}
        for _, i in ipairs({ 0, 1, math.floor(size / 2), size - 2, size - 1 }) do
            if i >= 0 and data:byte(i + 1) ~= memory.readbyte(i) % 256 then bad[#bad + 1] = i end
        end
        trace(("domain %s: %d bytes, full read returned %d entries, reading %d + %s; style %s, sample %s")
            :format(d, size, got_full, st.read_len[d], st.read_len[d] < size and "last byte via readbyte" or "none",
                tostring(M.range_style(full) or M.range_style(memory.readbyterange(0, 4))), M.describe_table(full, 3)))
        if #data ~= size or #bad > 0 then
            error(("snapshot self-check failed on %s: %d bytes, mismatches at offsets %s"):format(
                d, #data, table.concat(bad, ", ")), 0)
        end
    end

    -- Write-watches: callbacks only note "written, by this PC"; values are
    -- read once at frame end (some cores report written values as 0).
    -- Starting values are logged as "init" rows, so a timeline knows the
    -- value before the first change.
    -- Hit loggers for exec_watch and register-logging watch lines. A hit logs
    -- registers, optional stack words and captured bytes. Callbacks run
    -- mid-frame; on BizHawk 1.11.7 the frame counter already shows the frame
    -- being emulated there (checked against writes.tsv), so hits are stamped
    -- framecount() as frames.tsv and writes.tsv are. Hits past `max`
    -- are only counted. The callback never raises: on BizHawk 1.x an error
    -- inside a callback can surface as a modal dialog that blocks the
    -- emulator, so errors are counted and the first one is logged instead.
    st.hit_stats = {}
    local function hit_logger(file, kind, label, o)
        local stat = { hits = 0, logged = 0, errors = 0 }
        st.hit_stats[#st.hit_stats + 1] = { kind = kind, label = label, stat = stat }
        local function record(addr)
            local regs = try(emu.getregisters)
            memory.usememorydomain("System Bus")
            local stack = {}
            local sp = type(regs) == "table" and regs.SP or nil
            if o.stack > 0 and type(sp) == "number" then
                for i = 0, 2 * o.stack - 1 do stack[#stack + 1] = memory.readbyte(sp + i) end
            end
            local caps = {}
            for _, c in ipairs(o.captures) do
                local b = {}
                for a = c.start, c.start + c.length - 1 do b[#b + 1] = memory.readbyte(a) end
                caps[#caps + 1] = M.hex_bytes(b)
            end
            file:add(("%d\t0x%04X\t%d\t%s\t%s\t%s"):format(emu.framecount(), addr, stat.hits,
                M.registers_string(regs), M.hex_bytes(stack), table.concat(caps, " ")))
        end
        return function(addr)
            stat.hits = stat.hits + 1
            if stat.logged >= o.max then return end
            stat.logged = stat.logged + 1
            local fine, rec_err = pcall(record, addr)
            if not fine then
                stat.errors = stat.errors + 1
                if stat.errors == 1 then trace(("%s %s: %s"):format(kind, label, tostring(rec_err)), true) end
            end
        end
    end
    local HIT_HEADER = "frame\taddress\thit\tregisters\tstack\tcapture"

    -- Register-logging watch lines: one logger per line, shared by its bytes.
    local wlog = {}
    for _, w in ipairs(cfg.watch) do
        if w.registers then
            if not st.write_hits then
                st.write_hits = buffered(st.out .. "write_hits.tsv")
                st.write_hits:add(HIT_HEADER)
            end
            local log = hit_logger(st.write_hits, "watch", ("0x%04X"):format(w.start), w)
            for a = w.start, w.start + w.length - 1 do wlog[a] = log end
        end
    end

    local addrs = {}
    for a in pairs(st.delays) do addrs[#addrs + 1] = a end
    table.sort(addrs)
    trace(("registering %d write-watch(es)"):format(#addrs))
    local dirty = st.dirty
    for _, a in ipairs(addrs) do
        st.shadow[a] = read_bus(a)
        st.writes:add(("%d\t0x%04X\t%d\t%d\tinit"):format(st.start_frame, a, st.shadow[a], st.shadow[a]))
        local log = wlog[a]
        event.onmemorywrite(function()
            dirty[a] = try(emu.getregister, "PC") or -1
            if log then log(a) end
        end, a)
    end

    -- Exec-watches: the same hit logger, on execution of an address.
    if #cfg.exec > 0 then
        if not callable(event.onmemoryexecute) then
            error("this BizHawk build has no event.onmemoryexecute; remove the exec_watch lines", 0)
        end
        st.execs = buffered(st.out .. "execs.tsv")
        st.execs:add(HIT_HEADER)
        trace(("registering %d exec-watch(es)"):format(#cfg.exec))
        for _, w in ipairs(cfg.exec) do
            local log = hit_logger(st.execs, "exec_watch", ("0x%04X"):format(w.address), w)
            local addr = w.address
            local reg_ok, reg_err = pcall(event.onmemoryexecute, function() log(addr) end, addr)
            if not reg_ok then error(("can't watch execution of 0x%04X: %s"):format(addr, tostring(reg_err)), 0) end
        end
    end

    if cfg.turbo then
        -- Speed never changes what a frame computes, so this only saves time.
        -- If this BizHawk build lacks these calls, say so instead of failing silently.
        -- 3200%: BizHawk 1.11.7 rejects 6400 ("Invalid speed value").
        local ok1 = callable(client.speedmode) and pcall(client.speedmode, 3200)
        local ok2 = callable(emu.limitframerate) and pcall(emu.limitframerate, false)
        if not (ok1 or ok2) then
            trace("WARNING couldn't lift the speed limit from Lua; untick "
                .. "Config > Speed/Skip > Clock Throttle to record faster")
        end
    end

    -- Say loudly that recording started: a script that's listed in the Lua
    -- Console but switched off prints nothing, so silence means "not running".
    trace(("v%s: RECORDING from frame %d of %d to %s"):format(M.VERSION, st.start_frame, st.length, st.out))
    trace(("%d domain(s) per snapshot, every %d frames + %d listed, %d watched byte(s)%s")
        :format(#cfg.domains, cfg.snapshot_every, M.count_keys(cfg.snapshot_frames), #addrs,
            cfg.screenshots and ", screenshots on" or ""))
    return st
end

--- Record the frame that just ran: input, lag, watched writes, snapshots.
--- Never yields (called inside xpcall after emu.frameadvance()).
--- @return true when recording should stop (movie ended or left PLAY mode).
--- @requires BizHawk Lua: emu, movie, memory, client; io.
local function step(st)
    local cfg = st.cfg
    local frame = emu.framecount()
    local lag = emu.islagged() and 1 or 0
    st.lag_total = st.lag_total + lag
    st.frames:add(("%d\t%d\t%s"):format(frame, lag, M.input_string(try(movie.getinput, frame - 1))))
    st.rows = st.rows + 1

    for a, pc in pairs(st.dirty) do
        local v = read_bus(a)
        if v ~= st.shadow[a] then
            local pc_text = (pc >= 0) and ("0x%04X"):format(pc % 0x10000) or "?"
            st.writes:add(("%d\t0x%04X\t%d\t%d\t%s"):format(frame, a, st.shadow[a], v, pc_text))
            st.shadow[a] = v
            if st.delays[a] then st.pending[frame + st.delays[a]] = ("change:0x%04X"):format(a) end
        end
        st.dirty[a] = nil
    end

    local snap_reason = M.snapshot_reason(cfg, frame, st.pending)
    st.pending[frame] = nil
    if snap_reason then
        -- Flush first and log each step, so a hang inside a snapshot leaves
        -- complete data files and names the call it stopped in.
        local tr = cfg.trace_snapshots
        st.frames:flush(); st.writes:flush(); st.snaps:flush()
        if st.execs then st.execs:flush() end
        if st.write_hits then st.write_hits:flush() end
        if tr then trace(("snapshot f%d (%s): start"):format(frame, snap_reason), true) end
        for _, d in ipairs(cfg.domains) do
            if tr then trace(("snapshot f%d: reading %s"):format(frame, d), true) end
            local data = read_domain(st, d)
            local name = M.snap_name(frame, d)
            local f = assert(io.open(st.out .. name, "wb"))
            f:write(data); f:close()
            st.snaps:add(("%d\t%s\t%s\t%d\t%s"):format(frame, d, name, #data, snap_reason))
        end
        if cfg.screenshots then
            if tr then trace(("snapshot f%d: screenshot"):format(frame), true) end
            try(client.screenshot, st.out .. M.snap_name(frame, nil, snap_reason))
        end
        if tr then trace(("snapshot f%d: done"):format(frame), true) end
    end
    if cfg.heartbeat_every > 0 and frame % cfg.heartbeat_every == 0 then
        trace(("heartbeat frame %d, lag %d"):format(frame, st.lag_total), true)
    end

    if st.rows % cfg.flush_every == 0 then
        st.frames:flush(); st.writes:flush(); st.snaps:flush()
        if st.execs then st.execs:flush() end
        if st.write_hits then st.write_hits:flush() end
    end
    if cfg.progress_every > 0 and frame % cfg.progress_every == 0 then
        trace(("frame %d / %d, %d lag frames so far"):format(frame, st.length, st.lag_total))
    end
    local now = movie_mode()
    if now ~= "PLAY" then st.reason = "movie mode became " .. tostring(now); return true end
    if cfg.stop_after > 0 and frame >= cfg.stop_after then st.reason = "stop_after " .. cfg.stop_after; return true end
    return st.length > 0 and frame >= st.length
end

--- Close the data files and write run.tsv.
--- @return nothing.
--- @requires BizHawk Lua: emu, gameinfo, client; io.
local function finish(st)
    st.frames:close(); st.writes:close(); st.snaps:close()
    if st.execs then st.execs:close() end
    if st.write_hits then st.write_hits:close() end
    local meta = buffered(st.out .. "run.tsv")
    meta:add("key\tvalue")
    meta:add("recorder_version\t" .. M.VERSION)
    meta:add("lua\t" .. tostring(_VERSION))
    meta:add("rom_name\t" .. tostring(try(gameinfo.getromname)))
    meta:add("rom_hash\t" .. tostring(try(gameinfo.getromhash)))
    meta:add("movie_length\t" .. tostring(st.length))
    meta:add("started_at_frame\t" .. st.start_frame)
    meta:add("ended_at_frame\t" .. emu.framecount())
    meta:add("frames_recorded\t" .. st.rows)
    meta:add("lag_frames\t" .. st.lag_total)
    meta:add("end_reason\t" .. st.reason)
    for _, e in ipairs(st.hit_stats) do
        local key = (e.kind == "watch" and "write_hits." or "exec.") .. e.label .. "."
        meta:add(key .. "hits\t" .. e.stat.hits)
        meta:add(key .. "logged\t" .. e.stat.logged)
        meta:add(key .. "errors\t" .. e.stat.errors)
    end
    for d, n in pairs(st.sizes) do meta:add("domain_size." .. d .. "\t" .. n) end
    meta:add("config\t" .. (st.text:gsub("\r", ""):gsub("\n", " | ")))
    meta:close()
    trace(("done, %d frames (%d lag) from frame %d; files in %s"):format(st.rows, st.lag_total, st.start_frame, st.out))
    if st.start_frame > 0 then trace("WARNING started at frame " .. st.start_frame .. ", not 0 (see the how-to at the top)") end
    if st.cfg.exit_at_end then try(client.exit) end
end

--- Record the playing movie until it ends. Entry point inside BizHawk.
--- Each stage runs under xpcall so a failure is logged with its traceback;
--- emu.frameadvance() stays outside xpcall because Lua 5.1 can't yield
--- across it. After a failure in the loop the data so far is still saved.
--- @return nothing; writes the files described at the top of this file.
--- @requires BizHawk Lua (see setup, step, finish).
local function run()
    local ok, st = xpcall(setup, traceback)
    if not ok then trace("FAILED during setup: " .. tostring(st)); return end
    trace("entering the frame loop (unpause if paused)")
    local first = true
    while true do
        emu.frameadvance()
        if first then trace("first frame advanced, now at frame " .. tostring(emu.framecount())); first = false end
        local ok2, done = xpcall(function() return step(st) end, traceback)
        if not ok2 then
            st.reason = "error: " .. tostring(done)
            trace("FAILED at frame " .. tostring(emu.framecount()) .. ": " .. tostring(done))
            break
        end
        if done then break end
    end
    local ok3, err = xpcall(function() finish(st) end, traceback)
    if not ok3 then trace("FAILED while finishing: " .. tostring(err)) end
end

-- Opened as a script inside BizHawk: record. Loaded with require() (tests,
-- in or out of BizHawk), `...` holds the module name: return the helpers only.
-- Only that exact name counts, since a BizHawk build may pass other values.
local required_as = ...
if required_as ~= "movie_recorder" and emu ~= nil and memory ~= nil and movie ~= nil then
    log_path = script_dir() .. "movie_recorder.log"
    local f = io.open(log_path, "w")
    if f then f:close() else log_path = nil end
    trace(("v%s started (%s); log file: %s; '...' = %s"):format(
        M.VERSION, tostring(_VERSION), tostring(log_path), tostring(required_as)))
    run()
else
    if emu ~= nil then print("movie_recorder: loaded as a module ('...' = " .. tostring(required_as) .. "), not recording") end
    return M
end

