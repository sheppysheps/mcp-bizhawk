#!/usr/bin/env bash
# guard-assets.sh: refuse to commit ROMs, firmware, movies, savestates and
# other copyrighted files, even when renamed.
#
# Purpose: the pre-commit asset guard shared by every repo (one copy per
# repo, so each works on its own when cloned). Run by lefthook's pre-commit
# hook. It checks every staged file twice:
#   1. by name: extensions and paths that are always copyrighted dumps;
#   2. by content: the SHA-1 of the staged blob against a block list.
#
# Returns: exit 0 when every staged file is allowed; exit 1 (with a message
# naming the file) when one is refused. If the block list can't be found, it
# still checks names and prints a warning, so a missing file never silently
# disables the name check.
#
# Depends on: git, sha1sum (Git for Windows, Linux) or shasum (macOS), and
# the block list at $GUARD_BLOCKLIST, or else
# $CLAUDEWORK_ASSETS/blocked-sha1.txt. The list lives outside the repos so
# no repo has to name the files it blocks.
#
# System access: reads staged blobs with `git show`; writes nothing.
# Bypass only deliberately, and only when the repo owner says so:
#   git commit --no-verify

set -u

blocklist="${GUARD_BLOCKLIST:-${CLAUDEWORK_ASSETS:-}/blocked-sha1.txt}"
if [ ! -f "$blocklist" ]; then
    echo "guard-assets: block list not found ($blocklist); checking file names only." >&2
    blocklist=""
fi

if command -v sha1sum >/dev/null 2>&1; then sha1() { sha1sum; }; else sha1() { shasum -a 1; }; fi

status=0
while IFS= read -r -d '' path; do
    lower=$(printf '%s' "$path" | tr '[:upper:]' '[:lower:]')
    case "$lower" in
        *.gb|*.gbc|*.gba|*.rom|*.nes|*.sfc|*.smc|*.z64|*.n64|*.iso|*.bk2|*.tasproj|*.state|*.sav|*.srm|*bios*|*/firmware/*|firmware/*)
            echo "guard-assets: refusing '$path' (ROM, firmware, movie or savestate by name)" >&2
            status=1
            continue ;;
    esac
    [ -n "$blocklist" ] || continue
    sha=$(git show ":$path" | sha1 | cut -c1-40 | tr 'a-f' 'A-F')
    match=$(grep -v '^#' "$blocklist" | grep -m1 "^$sha ")
    if [ -n "$match" ]; then
        echo "guard-assets: refusing '$path': its content is ${match#* }" >&2
        status=1
    fi
done < <(git diff --cached --name-only --diff-filter=ACMR -z)

exit $status
