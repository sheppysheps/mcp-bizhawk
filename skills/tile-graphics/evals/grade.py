"""Programmatic grading for tile-graphics iteration runs.

Independent of the skill: its own tiny PNG reader and tile decoders, written
fresh from the hardware docs, so a bug in the skill can't grade itself as
correct. Ground truth for screens is BizHawk's own screenshot.

Usage: python grade.py <workspace>/iteration-N
Inputs are read from $CLAUDEWORK_ASSETS/dumps/tile-graphics-inputs when that
variable is set, else from <workspace>/inputs (see evals.json "notes").
Writes grading_auto.json (programmatic checks) into each run directory.
"""

import glob
import json
import os
import struct
import sys
import zlib

INPUTS = None  # set in main() by inputs_dir()


def inputs_dir(iteration):
    """Folder holding the eval inputs (game-derived dumps, never in the repo).

    Returns $CLAUDEWORK_ASSETS/dumps/tile-graphics-inputs when the variable is
    set, else the older <workspace>/inputs folder next to the iteration folder.
    Depends on the CLAUDEWORK_ASSETS environment variable (asset store root).
    """
    assets = os.environ.get("CLAUDEWORK_ASSETS")
    if assets:
        return os.path.join(assets, "dumps", "tile-graphics-inputs")
    return os.path.join(os.path.dirname(os.path.abspath(iteration)), "inputs")


def read_png(path):
    """Independent PNG reader (8-bit RGB/RGBA/gray/palette, filters 0-4). Returns (w, h, [(r,g,b)...]) or None."""
    try:
        d = open(path, "rb").read()
        pos, idat, pal = 8, b"", None
        while pos < len(d):
            n, k = struct.unpack(">I4s", d[pos:pos + 8])
            body = d[pos + 8:pos + 8 + n]
            pos += 12 + n
            if k == b"IHDR":
                w, h, depth, ct, _, _, il = struct.unpack(">IIBBBBB", body)
            elif k == b"PLTE":
                pal = [tuple(body[i:i + 3]) for i in range(0, len(body), 3)]
            elif k == b"IDAT":
                idat += body
        if depth != 8 or il:
            return None
        ch = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ct]
        raw, stride, prev, rows, i = zlib.decompress(idat), w * ch, bytearray(w * ch), [], 0
        for _ in range(h):
            f, line = raw[i], bytearray(raw[i + 1:i + 1 + stride])
            i += 1 + stride
            for x in range(stride):
                a = line[x - ch] if x >= ch else 0
                b, c = prev[x], (prev[x - ch] if x >= ch else 0)
                if f == 1: line[x] = (line[x] + a) & 255
                elif f == 2: line[x] = (line[x] + b) & 255
                elif f == 3: line[x] = (line[x] + ((a + b) >> 1)) & 255
                elif f == 4:
                    p = a + b - c; pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                    line[x] = (line[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
            rows.append(line); prev = line
        px = []
        for line in rows:
            for x in range(w):
                v = line[x * ch:(x + 1) * ch]
                px.append(pal[v[0]] if ct == 3 else (v[0],) * 3 if ct in (0, 4) else tuple(v[:3]))
        return w, h, px
    except Exception:
        return None


def gb_tile(data, off):
    """Game Boy 2bpp per Pan Docs: rows of (low byte, high byte), bit 7 leftmost."""
    return tuple(((data[off + 2 * r] >> b) & 1) | (((data[off + 2 * r + 1] >> b) & 1) << 1) for r in range(8) for b in range(7, -1, -1))


def nes_tile(data, off):
    """NES 2bpp: 8 bytes plane 0, then 8 bytes plane 1, bit 7 leftmost."""
    return tuple(((data[off + r] >> b) & 1) | (((data[off + 8 + r] >> b) & 1) << 1) for r in range(8) for b in range(7, -1, -1))


def canon(values):
    """Relabel values by first appearance so patterns compare regardless of palette."""
    seen = {}
    return tuple(seen.setdefault(v, len(seen)) for v in values)


def cells_in_image(img):
    """All 8x8 cells of an image at scale 1-4, canonicalized. Returns {scale: [patterns]}."""
    w, h, px = img
    out = {}
    for s in (1, 2, 3, 4):
        if w % (8 * s) or h % (8 * s):
            continue
        pats = []
        for ty in range(h // (8 * s)):
            for tx in range(w // (8 * s)):
                pats.append(canon([px[(ty * 8 * s + y * s) * w + tx * 8 * s + x * s] for y in range(8) for x in range(8)]))
        out[s] = pats
    return out


def tile_coverage(png_paths, reference):
    """Best fraction of reference tile patterns (canonical) present in any of the PNGs."""
    ref = [canon(t) for t in reference]
    best = 0.0
    for p in png_paths:
        img = read_png(p)
        if not img:
            continue
        for pats in cells_in_image(img).values():
            have = set(pats)
            best = max(best, sum(1 for t in ref if t in have) / len(ref))
    return best


def check(text, passed, evidence):
    return {"text": text, "passed": bool(passed), "evidence": evidence}


def pngs(outdir):
    return sorted(glob.glob(os.path.join(outdir, "**", "*.png"), recursive=True))


def grade_run(eval_name, run_dir):
    out = os.path.join(run_dir, "outputs")
    files = pngs(out)
    results = []
    if eval_name == "nes-chr-sheet":
        data = open(os.path.join(INPUTS, "nes_chr", "chr.bin"), "rb").read()
        ref = [nes_tile(data, i * 16) for i in range(512)]
        sheet = os.path.join(out, "sheet.png")
        img = read_png(sheet) if os.path.exists(sheet) else None
        results.append(check("sheet.png is 128x256 (16 tiles wide, 512 tiles)", img and img[:2] == (128, 256),
                             f"size {img[:2] if img else 'missing/unreadable'}"))
        if img:
            ok = img[:2] == (128, 256) and all(canon([img[2][(t // 16 * 8 + y) * 128 + (t % 16) * 8 + x] for y in range(8) for x in range(8)]) == canon(ref[t]) for t in range(512))
            results.append(check("every tile matches an independent NES planar decode, in order", ok, "all 512 tiles compared in sheet order"))
        else:
            results.append(check("every tile matches an independent NES planar decode, in order", False, "no sheet"))
        gb_wrong = [gb_tile(data, i * 16) for i in range(512)]
        cov_gb = tile_coverage([sheet] if img else [], gb_wrong)
        results.append(check("sheet is not decoded with the Game Boy (interleaved) layout by mistake", cov_gb < 0.5, f"GB-layout coverage {cov_gb:.2%}"))
    elif eval_name == "rip-title-graphics":
        vram = open(os.path.join(INPUTS, "shantae_title", "vram.bin"), "rb").read()
        for bank in (0, 1):
            ref = [gb_tile(vram, bank * 0x2000 + i * 16) for i in range(384)]
            cov = tile_coverage(files, ref)
            results.append(check(f"a PNG contains >=95% of VRAM bank {bank}'s 384 tiles, decoded correctly", cov >= 0.95, f"best coverage {cov:.1%}"))
        shot = read_png(os.path.join(INPUTS, "shantae_title", "screenshot.png"))
        shot_colors = set(shot[2])
        best = None
        for p in files:
            img = read_png(p)
            if img:
                cols = set(img[2]) - {(0, 0, 0)}
                real = cols & shot_colors
                if len(real) >= 5 and (best is None or len(real) > best[1]):
                    best = (os.path.basename(p), len(real))
        results.append(check("at least one output uses >=5 of the screenshot's real colors (not just grayscale)", best is not None,
                             f"{best[0]} uses {best[1]} screenshot colors" if best else "no output uses real colors"))
    elif eval_name == "verify-title-decode":
        shot = read_png(os.path.join(INPUTS, "shantae_title", "screenshot.png"))
        exact = [os.path.basename(p) for p in files if (img := read_png(p)) and img[:2] == (160, 144) and img[2] == shot[2]]
        results.append(check("a rendered 160x144 screen in outputs is pixel-identical to the screenshot", exact, f"exact matches: {exact or 'none'}"))
    elif eval_name == "black-frame-trap":
        shot = read_png(os.path.join(INPUTS, "shantae_black", "screenshot.png"))
        results.append(check("(fact) the screenshot is a single uniform color", len(set(shot[2])) == 1, f"{len(set(shot[2]))} distinct colors"))
    return results


def main(iteration):
    global INPUTS
    INPUTS = inputs_dir(iteration)
    for meta in glob.glob(os.path.join(iteration, "eval-*", "eval_metadata.json")):
        name = json.load(open(meta))["eval_name"]
        for run_dir in glob.glob(os.path.join(os.path.dirname(meta), "*_skill", "run-*")):
            res = grade_run(name, run_dir)
            json.dump({"auto": res}, open(os.path.join(run_dir, "grading_auto.json"), "w"), indent=2)
            print(f"{name:22} {os.path.basename(run_dir):14} " + "  ".join(("PASS" if r["passed"] else "FAIL") for r in res))


if __name__ == "__main__":
    main(sys.argv[1])
