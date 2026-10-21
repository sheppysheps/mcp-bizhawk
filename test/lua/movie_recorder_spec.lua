-- lua/legacy/movie_recorder.lua: config parsing and formatting helpers. Pure Lua.

--- Run every check.
--- @return list of `{ name, pass, detail }`.
--- @requires the movie_recorder module (returns its helpers outside BizHawk).
return function(mr)
    local results = {}
    local function check(name, pass, detail)
        results[#results + 1] = { name = "movie_recorder: " .. name, pass = pass and true or false, detail = detail }
    end
    local function rejects(name, text, expect)
        local cfg, err = mr.parse_config(text)
        check(name, cfg == nil and tostring(err):find(expect, 1, true) ~= nil, tostring(err))
    end

    -- Diagnostics keys.
    local diag = mr.parse_config("out = x\nheartbeat_every = 25\ntrace_snapshots = no")
    check("diagnostics keys parse", diag and diag.heartbeat_every == 25 and diag.trace_snapshots == false)
    local defaults = mr.parse_config("out = x")
    check("diagnostics defaults", defaults and defaults.heartbeat_every == 0 and defaults.trace_snapshots == true)

    -- exec_watch.
    local ew = mr.parse_exec_watch("0x6BF6 stack 2 capture 0xFF80 0x10 capture 0xC000 1 max 50")
    check("exec_watch parses all options", ew and ew.address == 0x6BF6 and ew.stack == 2 and ew.max == 50
        and #ew.captures == 2 and ew.captures[1].start == 0xFF80 and ew.captures[1].length == 16
        and ew.captures[2].length == 1)
    local ew0 = mr.parse_exec_watch("0x0150")
    check("exec_watch defaults", ew0 and ew0.stack == 0 and #ew0.captures == 0 and ew0.max == mr.EXEC_DEFAULT_MAX)
    local function ew_rejects(name, value, expect)
        local w, e = mr.parse_exec_watch(value)
        check(name, w == nil and tostring(e):find(expect, 1, true) ~= nil, tostring(e))
    end
    ew_rejects("exec_watch rejects a missing address", "", "needs '<address>")
    ew_rejects("exec_watch rejects an unknown option", "0x100 bank 3", "unknown option 'bank'")
    ew_rejects("exec_watch caps stack words", "0x100 stack 99", "stack needs")
    ew_rejects("exec_watch caps capture length", "0x100 capture 0xC000 999", "capture needs")
    ew_rejects("exec_watch rejects max 0", "0x100 max 0", "max needs")
    local ecfg = mr.parse_config("out = x\nexec_watch = 0x6BF6 stack 1\nexec_watch = 0x6C1C")
    check("config collects exec_watch lines", ecfg and #ecfg.exec == 2 and ecfg.exec[2].address == 0x6C1C)
    rejects("config reports a bad exec_watch with its line", "out = x\nexec_watch = 0x100 nope", "line 2: exec_watch")
    check("registers_string sorts names and skips non-numbers",
        mr.registers_string({ PC = 0x5FF6, A = 18, Flags = "z" }) == "A=0x12 PC=0x5FF6"
        and mr.registers_string({}) == "?" and mr.registers_string(nil) == "?")
    check("hex_bytes", mr.hex_bytes({ 10, 27, 255, 256 }) == "0A1BFF00" and mr.hex_bytes({}) == "")

    -- snapshot_range and stop_after.
    local rc = mr.parse_config("out = x\nsnapshot_range = 100 130 10\nsnapshot_range = 0x200 0x200 1\nstop_after = 140")
    check("snapshot_range and stop_after parse", rc and #rc.snapshot_ranges == 2 and rc.snapshot_ranges[2].start == 0x200
        and rc.stop_after == 140)
    if rc then
        check("snapshot_range hits start, steps and end only",
            mr.snapshot_reason(rc, 100, nil) == "range" and mr.snapshot_reason(rc, 120, nil) == "range"
            and mr.snapshot_reason(rc, 130, nil) == "range" and mr.snapshot_reason(rc, 125, nil) == nil
            and mr.snapshot_reason(rc, 140, nil) == nil and mr.snapshot_reason(rc, 0x200, nil) == "range")
    end
    rejects("snapshot_range rejects end before start", "out = x\nsnapshot_range = 10 5 1", "snapshot_range needs")
    rejects("snapshot_range rejects step 0", "out = x\nsnapshot_range = 1 5 0", "snapshot_range needs")
    local d0 = mr.parse_config("out = x")
    check("stop_after defaults to 0 (play to the end)", d0 and d0.stop_after == 0 and #d0.snapshot_ranges == 0)

    -- watch lines with register logging.
    local pw = mr.parse_watch("0xCC30 0x30")
    check("plain watch logs no registers", pw and pw.start == 0xCC30 and pw.length == 0x30 and pw.registers == false)
    local rw = mr.parse_watch("0xCC30 0x30 registers")
    check("watch ... registers enables register logging", rw and rw.registers == true and rw.stack == 0)
    local ow = mr.parse_watch("0xC3EF 3 stack 2 capture 0xFF80 4 max 300")
    check("hit options imply registers", ow and ow.registers == true and ow.stack == 2 and ow.max == 300
        and #ow.captures == 1)
    local bw, berr = mr.parse_watch("0xC000 4 bogus")
    check("watch rejects unknown options", bw == nil and tostring(berr):find("unknown option 'bogus'", 1, true) ~= nil, berr)
    local xw, xerr = mr.parse_exec_watch("0x100 registers")
    check("exec_watch doesn't take 'registers' (it always logs them)", xw == nil and tostring(xerr):find("unknown option", 1, true) ~= nil, xerr)
    local wcfg = mr.parse_config("out = x\nwatch = 0xCC30 0x30 registers stack 2")
    check("config keeps watch hit options", wcfg and wcfg.watch[1].registers and wcfg.watch[1].stack == 2)

    -- Numbers.
    check("parse decimal", mr.parse_int(" 146 ") == 146)
    check("parse hex", mr.parse_int("0xC000") == 0xC000)
    check("reject non-numbers", mr.parse_int("12a") == nil and mr.parse_int("") == nil)

    -- A full config.
    local cfg, err = mr.parse_config(table.concat({
        "# comment line",
        "out = C:\\path\\to\\exports\\x\\   # trailing comment",
        "domains = WRAM, HRAM ,VRAM",
        "snapshot_every = 1000",
        "snapshot_frames = 147, 0x100",
        "screenshots = no",
        "watch = 0xC000 0x10",
        "watch = 0xFF80 4",
        "turbo = false",
        "exit_at_end = yes",
    }, "\n"))
    check("config parses", cfg ~= nil, err)
    if cfg then
        check("out uses forward slashes, no trailing slash", cfg.out == "C:/path/to/exports/x", cfg.out)
        check("domains are trimmed, in order", table.concat(cfg.domains, ",") == "WRAM,HRAM,VRAM")
        check("snapshot frames as a set", cfg.snapshot_frames[147] and cfg.snapshot_frames[256] and not cfg.snapshot_frames[148])
        check("watch ranges", #cfg.watch == 2 and cfg.watch[1].start == 0xC000 and cfg.watch[1].length == 16
            and cfg.watch[2].length == 4)
        check("booleans", cfg.screenshots == false and cfg.turbo == false and cfg.exit_at_end == true)
        check("defaults kept", cfg.flush_every == 600 and cfg.max_watch_bytes == 4096 and cfg.progress_every == 10000)
        check("count_keys counts a frame set", mr.count_keys(cfg.snapshot_frames) == 2)

        check("snapshot at a listed frame", mr.should_snapshot(cfg, 147))
        check("snapshot every N", mr.should_snapshot(cfg, 2000) and not mr.should_snapshot(cfg, 1999))
        check("no snapshot at frame 0 from snapshot_every", not mr.should_snapshot(cfg, 0))
    end

    -- Rejections, with useful messages.
    rejects("out is required", "domains = WRAM", "needs 'out")
    rejects("unknown keys are refused", "out = x\nfoo = 1", "unknown key 'foo'")
    rejects("bad lines name their line number", "out = x\njust words", "line 2")
    rejects("bad watch", "out = x\nwatch = 0xC000", "watch needs")
    rejects("bad boolean", "out = x\nturbo = maybe", "expected true or false")
    rejects("watch total over the cap", "out = x\nwatch = 0xC000 0x2000", "max_watch_bytes")
    check("config is never executed", mr.parse_config("out = x\nos.exit(1)") == nil)

    -- snapshot_on_change: parsing, the shared cap, and scheduling.
    local oc = mr.parse_config("out = x\nwatch = 0xC000 2\nsnapshot_on_change = 0xC9F9 2 45\nsnapshot_on_change = 0xFF80 1")
    check("snapshot_on_change parses with and without a delay", oc ~= nil and #oc.on_change == 2
        and oc.on_change[1].delay == 45 and oc.on_change[2].delay == 30)
    if oc then
        local w = mr.watched_addresses(oc)
        check("watched set covers both kinds", w[0xC000] == false and w[0xC001] == false and w[0xC9F9] == 45
            and w[0xC9FA] == 45 and w[0xFF80] == 30 and w[0xC002] == nil)
        check("pending snapshots win, with their reason",
            mr.snapshot_reason(oc, 77, { [77] = "change:0xC9F9" }) == "change:0xC9F9")
        check("no snapshot without a reason", mr.snapshot_reason(oc, 77, {}) == nil)
    end
    local pe = mr.parse_config("out = x\nprogress_every = 0")
    check("progress_every can be turned off", pe ~= nil and pe.progress_every == 0)
    rejects("bad snapshot_on_change", "out = x\nsnapshot_on_change = 0xC9F9", "snapshot_on_change needs")
    rejects("snapshot_on_change counts toward the cap", "out = x\nmax_watch_bytes = 2\nsnapshot_on_change = 0xC9F9 3",
        "max_watch_bytes")
    check("snapshot_reason labels listed and periodic frames", cfg and mr.snapshot_reason(cfg, 147, nil) == "listed"
        and mr.snapshot_reason(cfg, 3000, nil) == "every")

    -- Formatting.
    check("input with presses", mr.input_string({ Start = true, A = true, B = false }) == "A|Start")
    check("input with none", mr.input_string({ A = false }) == "." and mr.input_string(nil) == ".")
    check("snapshot file names", mr.snap_name(146, "System Bus") == "snap_f0000146_System_Bus.bin"
        and mr.snap_name(7) == "shot_f0000007.png")
    check("screenshot names carry the reason", mr.snap_name(6339, nil, "change:0xC3F0") == "shot_f0006339_change-0xC3F0.png"
        and mr.snap_name(6300, nil, "every") == "shot_f0006300_every.png")

    -- readbyterange() results with 0- or 1-based keys.
    local zero = { [0] = 65, [1] = 66, [2] = 300 }
    local one = { 65, 66 }
    check("bytes from 0-based keys", mr.bytes_to_string(zero, 3) == "AB" .. string.char(300 % 256))
    check("bytes from 1-based keys", mr.bytes_to_string(one, 2) == "AB")
    check("missing bytes become 0", mr.bytes_to_string({}, 2) == "\0\0")
    local hex = { ["00"] = "41", ["01"] = "42", ["0A"] = "FF", ["0B"] = "00" }
    check("bytes from BizHawk 1.x hex-string tables",
          mr.bytes_to_string(hex, 12) == "AB" .. string.rep("\0", 8) .. "\255\0")
    check("range_style tells the formats apart",
          mr.range_style(zero) == "zero" and mr.range_style(one) == "one" and mr.range_style(hex) == "hex"
          and mr.range_style({}) == nil and mr.range_style("x") == nil)
    check("describe_table names key and value types",
          mr.describe_table({ ["00"] = "41" }, 3) == "00(string)=41(string)" and mr.describe_table(nil, 3) == "a nil")
    local big = {}
    for i = 0, 9999 do big[i] = i % 256 end
    local s = mr.bytes_to_string(big, 10000)
    check("large ranges cross chunk boundaries", #s == 10000 and s:byte(4097) == 4096 % 256 and s:byte(10000) == 9999 % 256)

    return results
end
