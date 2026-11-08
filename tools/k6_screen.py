"""Draw a K6 RAM module's screen (module API level 6) as the radio's 128x64 LCD shows it.

The emulator harness (tests/test_k6_i2s.py run(..., lcd={})) records one frame a lcd_flush:
six text lines, a bar a line and the waterfall rows. render() lays them out the way the
firmware's draw_mod_screen does (firmware/k6-aura/aura-pager.patch, ui/pager.rs): an inverted
title bar 11 px tall, FONT_6X10 text at x = 1 and baselines y = 9 + 10 * line, the bar after a
line's text, and the waterfall rows from y = 21 over lines 2-4. png() saves frames side by side,
one square a pixel, for docs and bench reports.

FONT_6X10 is embedded-graphics 0.8's (MIT/Apache-2.0, from the X11 misc-fixed fonts), dumped
from the crate Aura links (project file k6-fonts/fonts.txt): 10 rows a glyph, bit 7 leftmost.
SPDX-License-Identifier: MIT"""
W, H, WF_TOP = 128, 64, 21
FONT = {
    32: "00000000000000000000",
    33: "00202020202000200000",
    34: "00505050000000000000",
    35: "005050f850f850500000",
    36: "002070a0702870200000",
    37: "0048a8502050a8900000",
    38: "0040a0a040a890680000",
    39: "00202020000000000000",
    40: "00102040404020100000",
    41: "00402010101020400000",
    42: "00008850f85088000000",
    43: "00002020f82020000000",
    44: "00000000000030204000",
    45: "00000000f80000000000",
    46: "00000000000020702000",
    47: "00080810204080800000",
    48: "00205088888850200000",
    49: "002060a0202020f80000",
    50: "00708808304080f80000",
    51: "00f80810300888700000",
    52: "0010305090f810100000",
    53: "00f880b0c80888700000",
    54: "00304080b0c888700000",
    55: "00f80810102040400000",
    56: "00708888708888700000",
    57: "00708898680810600000",
    58: "00002070200020702000",
    59: "00002070200030204000",
    60: "00081020402010080000",
    61: "000000f800f800000000",
    62: "00402010081020400000",
    63: "00708810202000200000",
    64: "00708898a8b080700000",
    65: "0020508888f888880000",
    66: "00f04848704848f00000",
    67: "00708880808088700000",
    68: "00f04848484848f00000",
    69: "00f88080f08080f80000",
    70: "00f88080f08080800000",
    71: "00708880809888700000",
    72: "00888888f88888880000",
    73: "00702020202020700000",
    74: "00381010101090600000",
    75: "008890a0c0a090880000",
    76: "00808080808080f80000",
    77: "008888d8a88888880000",
    78: "008888c8a89888880000",
    79: "00708888888888700000",
    80: "00f08888f08080800000",
    81: "007088888888a8700800",
    82: "00f08888f0a090880000",
    83: "00708880700888700000",
    84: "00f82020202020200000",
    85: "00888888888888700000",
    86: "00888888505050200000",
    87: "00888888a8a8d8880000",
    88: "00888850205088880000",
    89: "00888850202020200000",
    90: "00f80810204080f80000",
    91: "00704040404040700000",
    92: "00808040201008080000",
    93: "00701010101010700000",
    94: "00205088000000000000",
    95: "0000000000000000f800",
    96: "20100000000000000000",
    97: "00000070087888780000",
    98: "008080b0c888c8b00000",
    99: "00000070888088700000",
    100: "00080868988898680000",
    101: "0000007088f880700000",
    102: "00304840f04040400000",
    103: "00000078888878088870",
    104: "008080b0c88888880000",
    105: "00200060202020700000",
    106: "00080018080808484830",
    107: "0080808890e090880000",
    108: "00602020202020700000",
    109: "000000d0a8a8a8880000",
    110: "000000b0c88888880000",
    111: "00000070888888700000",
    112: "000000b0c888c8b08080",
    113: "00000068988898680808",
    114: "000000b0c88080800000",
    115: "00000070807008f00000",
    116: "004040f0404048300000",
    117: "00000088888898680000",
    118: "00000088885050200000",
    119: "0000008888a8a8500000",
    120: "00000088502050880000",
    121: "00000088889868088870",
    122: "000000f8102040f80000",
    123: "00182010601020180000",
    124: "00202020202020200000",
    125: "00601020182010600000",
    126: "0048a890000000000000",
}


def _text(fb, s, x, y, ink):
    for i, ch in enumerate(s):
        g = bytes.fromhex(FONT.get(ord(ch), FONT[ord("?")]))
        for r in range(10):
            for c in range(6):
                px, py = x + 6 * i + c, y - 7 + r
                if 0 <= px < W and 0 <= py < H and g[r] & (0x80 >> c):
                    fb[py][px] = ink


def _rect(fb, x, y, w, h, ink, fill=True):
    for yy in range(y, y + h):
        for xx in range(x, x + w):
            if 0 <= xx < W and 0 <= yy < H and (fill or yy in (y, y + h - 1) or xx in (x, x + w - 1)):
                fb[yy][xx] = ink


def render(frame):
    """frame: {"text": six lines, "bar": six (num, den), "wf": rows of 16 bytes} -> 64 rows of 128 0/1"""
    fb = [[0] * W for _ in range(H)]
    _rect(fb, 0, 0, W, 11, 1)
    wf = frame.get("wf") or []
    for i in range(6):
        if wf and 2 <= i < 5:
            continue
        t = frame["text"][i][:21]
        y = 9 + 10 * i
        ink = 0 if i == 0 else 1
        _text(fb, t, 1, y, ink)
        num, den = frame.get("bar", [(0, 0)] * 6)[i]
        x0 = 1 + 6 * len(t) + (3 if t else 0)
        if den and x0 < 118:
            w = 126 - x0
            _rect(fb, x0, y - 7, w + 1, 8, ink, fill=False)
            fill = w * min(num, den) // den
            if fill:
                _rect(fb, x0, y - 7, fill, 8, ink)
    for r, row in enumerate(wf):
        for x in range(W):
            if row[x >> 3] & (0x80 >> (x & 7)):
                fb[WF_TOP + r][x] = 1
    return fb


def png(frames, path, scale=3, captions=None):
    """frames side by side, each scale x scale squares a pixel, an optional caption under each"""
    from PIL import Image, ImageDraw
    on, off, gap = (0x1C, 0x24, 0x1A), (0x9A, 0xA8, 0x8C), 12
    cap = 18 if captions else 0
    img = Image.new("RGB", (len(frames) * (W * scale + gap) + gap, H * scale + 2 * gap + cap), (250, 250, 248))
    d = ImageDraw.Draw(img)
    for n, f in enumerate(frames):
        fb = render(f)
        ox = gap + n * (W * scale + gap)
        for y in range(H):
            for x in range(W):
                x0, y0 = ox + x * scale, gap + y * scale
                d.rectangle([x0, y0, x0 + scale - 1, y0 + scale - 1], fill=on if fb[y][x] else off)
        if captions:
            d.text((ox, gap + H * scale + 4), captions[n], fill=(40, 40, 40))
    img.save(path)


def ascii_art(frame):
    return "\n".join("".join("#" if v else "." for v in row) for row in render(frame))


def surf_frame(lines):
    """The last bargraph a surfauto run says ("bar <MHz> <step>", then lines of a letter a bin:
    a = 1 px .. x = 24 px; c/k6/mod/surf.c) as a frame for render() and png(): the bars as the
    radio drew them, with the window's edges. lines: the run's say lines (with or without the
    "K6 MOD say " prefix); returns None if it says no bargraph."""
    lines = [ln.split("K6 MOD say ", 1)[-1].strip() for ln in lines]
    try:
        i = max(n for n, ln in enumerate(lines) if ln.startswith("bar "))
    except ValueError:
        return None
    _, lo, step = lines[i].split()[:3]
    hs = []
    for ln in lines[i + 1:]:
        if not ln or not all("a" <= c <= "x" for c in ln):
            break
        hs += [ord(c) - ord("a") + 1 for c in ln]
    kstep = float(step.rstrip("k"))
    hi = float(lo) + (len(hs) - 1) * kstep / 1000
    wf = [bytearray(16) for _ in range(30)]
    for b, h in enumerate(hs):
        for r in range(30 - h, 30):
            wf[r][b >> 2] |= 0xC0 >> (2 * (b & 3))
    text = ["SURF bars " + step, "", "", "", "", f"{float(lo):.3f}".ljust(14) + f"{hi:.3f}"]
    return {"text": text, "bar": [(0, 0)] * 6, "wf": [bytes(r) for r in wf], "heights": hs,
            "lo_mhz": float(lo), "step_khz": kstep}


if __name__ == "__main__":
    # python tools/k6_screen.py RUN.txt OUT.png: draw the last bargraph a surfauto run said
    import sys
    f = surf_frame(open(sys.argv[1]).read().splitlines())
    if f is None:
        sys.exit(f"{sys.argv[1]}: no 'bar' lines")
    if len(sys.argv) > 2:
        png([f], sys.argv[2], captions=[f"{f['lo_mhz']:.4f} MHz, {f['step_khz']:g} kHz bins"])
    print(ascii_art(f))
