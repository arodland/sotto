"""The band surfing module (c/k6/mod/surf.c) on an emulated Cortex-M0, against the modelled
FD6818 receiver of test_k6_rxmap (RSSI restarts at -173 dBm on every tune and ramps as Phase 0
measured) and the real keypad matrix (test_k6_i2s run(..., keypad=...)): the bargraph on its lens
scale (the listening frequency always in the middle, 2.5 kHz bins within +-20 kHz, wider ones
out to the span) with its caret and ruler, peak jumps with the side keys, UP/DOWN moving the
scale, band and zoom keys, the band's edge, monitor, the squelch's open and hang, and EXIT
putting the audio path back. SPDX-License-Identifier: MIT"""
import os
import struct
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_k6_i2s import ROOT, START, need_clang, run  # noqa: E402
from test_k6_rxmap import Receiver  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "tools"))
import k6_screen  # noqa: E402

SRC = os.path.join(ROOT, "c", "k6", "mod", "surf.c")
AUTO = os.path.join(ROOT, "c", "k6", "mod", "surfauto.c")
FOREVER = 10**12
ARENA = 5376                    # the smallest module arena (the EXPLORE image): header + code + tag
C, KI, NV = 31, 8, 63           # the middle bin, the lens's bins each side, the bins on screen
SPANS = [40, 100, 200, 400, 1000, 2000]     # x 2.5 kHz: +-100 kHz .. +-5 MHz
SEVENTY = (430_000_000, 450_000_000)


def lens(span=400):
    """each bin's offset from the middle (x 2.5 kHz), as surf.c's lens() works it out"""
    u, n = span - C, C - KI
    return list(range(KI + 1)) + [KI + j + u * j * (j + 1) // (n * (n + 1)) for j in range(1, n + 1)]


def scale(cen, span=400, band=SEVENTY):
    """the 63 bins' frequencies around cen, 0 outside the band"""
    off = lens(span)
    hz = [cen + (1 if i > C else -1) * 2500 * off[abs(i - C)] for i in range(NV)]
    return [f if band[0] <= f <= band[1] else 0 for f in hz]


def bin_for(f, cen, span=400, band=SEVENTY):
    return min((abs(h - f), i) for i, h in enumerate(scale(cen, span, band)) if h)[1]


def edges(cen, span=400, band=SEVENTY):
    """line 5: the first and last bins in the band, in MHz to 3 places (truncated, as mhz())"""
    hz = [h for h in scale(cen, span, band) if h]
    return f"{hz[0] // 1000 / 1000:.3f}".ljust(14) + f"{hz[-1] // 1000 / 1000:.3f}"


class Bumpy(Receiver):
    """bumps: [(hz, dBm)] that show only in reads under 5 ms after a tune (the noise bumps bench
    run 1 jumped to on 2026-11-08), not in a settled read"""

    def __init__(self, signals, bumps):
        super().__init__(signals)
        self.bumps, self.now = bumps, 0

    def rssi(self, us):
        self.now = us
        return super().rssi(us)

    def level(self, us):
        import math
        p = 10 ** (super().level(us) / 10)
        if us - self.t < 5000:
            p += sum(10 ** (d / 10) for hz, d in self.bumps if abs(hz - self.f) <= 12_000)
        return 10 * math.log10(p)


def rt_record(slot, rx_hz, call=b"", off=0, tx_tone=0, state=0xFFFC):
    """(slot, 64 bytes): an explorer repeater-table record as c/k6/rt.h lays it out (no CRC: surf
    doesn't check it)"""
    return slot, struct.pack("<HHIiHHHBBI16s10sBBBBHII", state, 1, rx_hz, off, 0, tx_tone, 0, 0, 0, 0,
                             b"", call, 0, 0, 0, 0, slot + 1, slot + 1, 0)


def go(signals, keys, rx_hz=440_600_000, level=7, regs=None, src=SRC, bumps=(), table=()):
    from modem import k6mod
    code, entry = k6mod.build(src)
    m = Bumpy([(f, d, a, b) for f, d, a, b in signals], list(bumps))
    lcd = {}
    mem = bytearray(b"\xff" * 0x110000)
    for slot, rec in table:
        mem[64 * slot:64 * slot + 64] = rec
    r = {**START, 0x67: m.rssi, 0x47: 0x6040, **(regs or {})}
    ret, said, regs, writes = run(r, code=(code, entry), tune=m.tune, level=level, lcd=lcd, keypad=keys,
                                  spare=ARENA - 24 - len(code), count=3_000_000_000, rx_hz=rx_hz,
                                  flash={"mem": mem})
    return ret, said, regs, writes, lcd


def taps(keys, t0, gap=200_000, hold=100_000):
    """keys pressed one after another from t0 (us): [(from, to, key)]"""
    return [(t0 + i * gap, t0 + i * gap + hold, k) for i, k in enumerate(keys)]


def height(frame, i):
    x = 2 * i
    return sum(1 for r in range(6, 30) if frame["wf"][r][x >> 3] & (0x80 >> (x & 7)))


def ticks(frame):
    """the ruler: the pixels lit in both rows 2 and 3 away from the caret (x 59-66)"""
    r2, r3 = frame["wf"][2], frame["wf"][3]
    return {x for x in range(128) if not 59 <= x <= 66
            and r2[x >> 3] & (0x80 >> (x & 7)) and r3[x >> 3] & (0x80 >> (x & 7))}


def caret(frame):
    """the caret's bin: the single pixel pair of its tip (row 3), near the middle"""
    row = frame["wf"][3]
    xs = [x for x in range(56, 72) if row[x >> 3] & (0x80 >> (x & 7))]
    return xs[0] // 2 if xs else None


def marks(frame):
    """the bins with a known-repeater mark (row 0)"""
    row = frame["wf"][0]
    return {x // 2 for x in range(0, 128, 2) if row[x >> 3] & (0x80 >> (x & 7))}


def sels(said):
    return [ln.split()[1] for ln in said if ln.startswith("sel ")]


def at(frames, us):
    return next(f for f in frames if f["us"] >= us)


@need_clang
@pytest.mark.parametrize("src", [SRC, AUTO])
def test_builds_level_6_and_fits_the_store(src):
    from modem import k6mod
    code, entry = k6mod.build(src)
    assert k6mod.source_level(src) == 6 and entry == 0
    assert len(code) <= ARENA - 24 - 200           # the arena, with room to grow (bitmap: stack)


def test_lens_scale_steps_grow_outward_and_end_on_the_span():
    for span in SPANS:
        off = lens(span)
        steps = [b - a for a, b in zip(off, off[1:])]
        assert off[KI] == 8 and steps[:KI] == [1] * KI          # +-20 kHz in 2.5 kHz bins
        assert off[C] == span                                    # the last bin on the span
        assert all(b >= a - 1 for a, b in zip(steps, steps[1:]))    # growing (whole units)
        assert steps[-1] >= steps[KI] and all(st > 0 for st in steps)
    assert lens(400)[C] * 2500 == 1_000_000 and lens(2000)[KI + 1] - lens(2000)[KI] > 1


@need_clang
def test_bench_build_surfs_by_itself_and_wraps():
    # 70cm from 440.600: peaks at 440.850 and 441.200, then up the band, wraps to 430.000
    sig = [(440_850_000, -80, 0, FOREVER), (441_200_000, -95, 0, FOREVER), (430_300_000, -90, 0, FOREVER)]
    ret, said, regs, writes, lcd = go(sig, [], src=AUTO)
    s = sels(said)
    assert s[:2] == ["440.8500", "441.2000"]                 # each with its confirming read
    lv = [ln.split()[2:] for ln in said if ln.startswith("sel ")][:2]
    assert lv[0][0] == "-80" and int(lv[0][1]) < -120 and int(lv[1][0]) > int(lv[1][1]) + 10
    assert "430.3000" in s and said[-1].startswith("end sw=")
    assert lcd["st"]["us"] >= 60_000_000 and regs[0x47] == 0x6040
    # the last bargraph comes back as say lines (under 60 characters), and draws as the screen did
    assert all(len(ln) < 60 for ln in said)
    f = k6_screen.surf_frame(said)
    assert f is not None and len(f["heights"]) == NV
    last = lcd["frames"][-1]
    same = sum(height(f, i) == height(last, i) for i in range(NV))
    assert same >= NV - 4, same                             # (the blinking bar, a last sweep)
    assert max(f["heights"]) > 15 and sorted(f["heights"])[16] <= 1     # a station, the floor


@need_clang
def test_bargraph_peaks_side_keys_and_audio():
    sig = [(440_850_000, -70, 0, FOREVER), (441_200_000, -95, 0, FOREVER), (440_500_000, -105, 0, FOREVER)]
    keys = [(3_000_000, 3_200_000, "A"), (4_000_000, 4_200_000, "A"), (5_000_000, 5_200_000, "M"),
            (6_000_000, 6_200_000, "B"), (6_500_000, 6_700_000, "M"), (8_000_000, 8_300_000, "X")]
    ret, said, regs, writes, lcd = go(sig, keys)
    assert said[0] == "surf 70cm 440.600 25k" and said[-1].startswith("end sw=") and ret > 0
    # SIDE1 twice: the -70 at 440.850 and then the -95 at 441.200; SIDE2 back to 440.850
    assert sels(said) == ["441.2000", "440.8500"]
    assert {r for r, _ in writes} == {0x47} and regs[0x47] == 0x6040    # REG_47 put back
    spk = lcd["st"]["spk"]
    assert spk[0] == (0, 0) and spk[-1][1] == 0                          # speaker amp off again
    # opens on the first jump, once the moved scale has been swept
    assert 3_000_000 < spk[1][0] < 3_700_000 and spk[1][1] == 4
    fr = lcd["frames"]
    quiet = at(fr, 2_000_000)
    assert quiet["text"][0] == "SURF 70cm 25k"
    assert quiet["text"][1] == "440.6000 -137dBm"
    assert quiet["text"][5] == edges(440_600_000) == "439.600       441.600"
    # the three signals stand out in order of level (a carrier between two wide bins lights
    # both), the rest is floor
    hs = {i: height(quiet, i) for i in range(NV)}
    b85, b12, b05 = (bin_for(f, 440_600_000) for f in (440_850_000, 441_200_000, 440_500_000))
    assert hs[b85] == 24 and 15 <= hs[b12] < 24 and 8 <= hs[b05]
    lit = {i for i, h in hs.items() if h > 1} - {C}
    assert {b85, b12, b05} <= lit and len(lit) <= 5, lit
    assert caret(quiet) == C
    on = at(fr, 3_700_000)
    assert on["text"][1] == "440.8500 -70dBm RX" and caret(on) == C
    assert on["text"][5] == edges(440_850_000)
    # the lens: the carrier fills the middle bins, 2.5 kHz apart, within the wide filter
    assert all(height(on, i) >= 20 for i in range(C - 3, C + 4) if i != C)
    assert at(fr, 5_100_000)["text"][1] == "441.2000 -95dBm RX"
    # the listening bar blinks: its column flips between frames
    a, b = on, fr[fr.index(on) + 1]
    ha, hb = height(a, C), height(b, C)
    assert max(ha, hb) >= 20 and min(ha, hb) <= 4


@need_clang
def test_side_keys_skip_bumps_a_settled_read_doesnt_confirm():
    # bumps at 440.700 and 441.000 in the quick reads only; the station at 441.200 is real
    sig = [(441_200_000, -95, 0, FOREVER)]
    # (MENU 0.8 s after each jump: a jump onto a station sweeps the moved scale before the audio)
    keys = taps("AM", 2_000_000, gap=800_000) + taps("BM", 4_000_000, gap=800_000) + [(6_000_000, 6_300_000, "X")]
    ret, said, regs, writes, lcd = go(sig, keys, bumps=[(440_700_000, -90), (441_000_000, -90)])
    fr = lcd["frames"]
    hs = {i: height(at(fr, 1_900_000), i) for i in range(NV)}
    assert hs[bin_for(440_700_000, 440_600_000)] > 8                # the bumps do show as bars
    assert hs[bin_for(441_000_000, 440_600_000)] > 8
    got = sels(said)
    assert got[0] == "441.2000"                              # SIDE1 skips them
    assert got[1] not in ("441.0000", "440.7000")            # SIDE2 too (it hunts on down the band)


@need_clang
def test_known_repeaters_marked_and_named():
    # the explorer's table: 440.850 simplex (AG7EW), 441.200 +5 MHz tone 103.5 (N0CALL), a replaced
    # record at 441.000 and one off the scale (443.525); the bar visits the first two
    table = [rt_record(0, 440_850_000, b"AG7EW"), rt_record(1, 441_000_000, b"OLD", state=0xFFF8),
             rt_record(70, 441_200_000, b"N0CALL", 5_000_000, 1035), rt_record(900, 443_525_000, b"FAR")]
    sig = [(440_850_000, -70, 0, FOREVER), (441_200_000, -95, 0, FOREVER)]
    keys = taps("A", 3_000_000) + taps("A", 4_000_000) + [(5_500_000, 5_800_000, "X")]
    ret, said, regs, writes, lcd = go(sig, keys, table=table)
    fr = lcd["frames"]
    quiet = at(fr, 2_000_000)
    assert marks(quiet) == {bin_for(440_850_000, 440_600_000), bin_for(441_200_000, 440_600_000)}
    assert quiet["text"][5] == edges(440_600_000)           # bar on 440.600: not a known one
    on = at(fr, 3_700_000)
    assert on["text"][5] == "AG7EW" and C in marks(on)
    assert marks(on) == {C, bin_for(441_200_000, 440_850_000)}
    assert at(fr, 4_700_000)["text"][5] == "N0CALL +5.0 103.5"


@need_clang
def test_up_down_move_the_scale_with_the_middle():
    keys = taps("U" * 33 + "M", 1_000_000) + taps("DM", 9_000_000) + [(10_000_000, 10_300_000, "X")]
    ret, said, regs, writes, lcd = go([], keys)
    assert sels(said) == ["441.4250", "441.4000"]           # 33 channels of 25 kHz up, one down
    fr = lcd["frames"]
    assert at(fr, 900_000)["text"][5] == edges(440_600_000)
    late = at(fr, 8_500_000)
    assert late["text"][5] == edges(441_425_000) == "440.425       442.425"
    assert caret(late) == C                                 # the bar stays in the middle
    # a station's bar moves with the scale at once (each bin keeps the old level nearest it)
    keys = taps("UUUU", 2_000_000) + [(3_500_000, 3_800_000, "X")]
    ret, said, regs, writes, lcd = go([(441_200_000, -95, 0, FOREVER)], keys)
    fr = lcd["frames"]
    assert height(at(fr, 1_900_000), bin_for(441_200_000, 440_600_000)) > 15
    moved = next(f for f in fr if f["text"][1].startswith("440.7000 "))
    assert height(moved, bin_for(441_200_000, 440_700_000)) > 15


@need_clang
def test_ruler_marks_20_khz_100_khz_and_1_mhz():
    keys = taps("3", 2_000_000) + taps("1", 3_000_000) + taps("1", 4_000_000) + [(5_000_000, 5_300_000, "X")]
    ret, said, regs, writes, lcd = go([], keys)
    fr = lcd["frames"]

    def want(cen, span):
        xs = set()
        for o in (20_000, 100_000, 1_000_000):
            for e in (-1, 1):
                if o < 2500 * span:
                    j = bin_for(cen + e * o, cen, span)
                    if 0 < j < NV - 1:
                        xs.add(2 * j + (e > 0))
        return xs

    one = at(fr, 1_500_000)                                   # +-1 MHz: the 1 MHz ticks are the edges
    assert ticks(one) == want(440_600_000, 400) and ticks(one) == {46, 79} | {2 * bin_for(440_500_000, 440_600_000), 2 * bin_for(440_700_000, 440_600_000) + 1}
    two = at(fr, 2_500_000)                                   # 3: +-2.5 MHz
    assert two["text"][5] == edges(440_600_000, 1000) and ticks(two) == want(440_600_000, 1000)
    assert len(ticks(two)) == 6
    half = at(fr, 4_500_000)                                  # 1, 1: +-500 kHz
    assert half["text"][5] == edges(440_600_000, 200) and ticks(half) == want(440_600_000, 200)


@need_clang
def test_band_and_zoom_keys():
    keys = taps("N", 1_000_000) + taps("1", 3_000_000) + taps("3", 5_000_000) + taps("NNNN", 7_000_000) \
        + [(9_000_000, 9_300_000, "X")]
    ret, said, regs, writes, lcd = go([], keys)
    fr = lcd["frames"]
    gm = (462_550_000, 467_725_000)
    gmrs = at(fr, 2_000_000)
    assert gmrs["text"][0] == "SURF GMRS 12.5k"
    assert gmrs["text"][1].startswith("465.1375 ")          # mid-band, on the 12.5 kHz raster
    assert gmrs["text"][5] == edges(465_137_500, 1000, gm)  # +-2.5 MHz
    assert at(fr, 4_000_000)["text"][5] == edges(465_137_500, 400, gm)    # 1: +-1 MHz
    assert at(fr, 4_000_000)["text"][1].startswith("465.1375 ")           # the middle stays
    assert at(fr, 6_000_000)["text"][5] == edges(465_137_500, 1000, gm)
    # four more: marine/WX, 2m, 1.25m, 70cm
    assert at(fr, 8_500_000)["text"][0] == "SURF 70cm 25k"
    assert at(fr, 8_500_000)["text"][1].startswith("440.0000 ")


@need_clang
def test_band_edge_leaves_the_bins_past_it_empty():
    keys = taps("DDDDM", 1_000_000) + [(3_000_000, 3_300_000, "X")]
    ret, said, regs, writes, lcd = go([], keys, rx_hz=430_100_000)
    assert sels(said) == ["430.0000"]                        # DOWN stops at the band's bottom
    f = at(fr := lcd["frames"], 2_500_000)
    assert f["text"][1].startswith("430.0000 ") and caret(f) == C
    assert all(height(f, i) == 0 for i in range(C))          # nothing below 430.000
    assert all(height(f, i) >= 1 for i in range(C + 1, NV))
    assert f["text"][5] == "430.000       431.000"


@need_clang
def test_monitor_opens_the_audio_without_a_signal():
    keys = taps("*", 1_000_000) + taps("*", 3_000_000) + [(4_000_000, 4_300_000, "X")]
    ret, said, regs, writes, lcd = go([], keys)
    spk = lcd["st"]["spk"]
    assert [v for _, v in spk] == [0, 4, 0]
    assert 1_000_000 < spk[1][0] < 1_200_000 and 3_000_000 < spk[2][0] < 3_300_000
    fr = lcd["frames"]
    assert at(fr, 2_000_000)["text"][0] == "SURF 70cm 25k MON"
    assert at(fr, 3_500_000)["text"][0] == "SURF 70cm 25k"


@need_clang
def test_squelch_opens_on_a_signal_and_closes_after_the_hang():
    sig = [(440_600_000, -100, 2_000_000, 3_000_000)]
    ret, said, regs, writes, lcd = go(sig, [(6_000_000, 6_300_000, "X")])
    spk = lcd["st"]["spk"]
    assert [v for _, v in spk] == [0, 4, 0]
    assert 2_000_000 < spk[1][0] < 2_150_000                 # a check every 16 bins (~70 ms here)
    assert 4_500_000 <= spk[2][0] < 4_700_000                 # 1.5 s after it dropped


@need_clang
def test_exit_waits_for_the_key_release():
    ret, said, regs, writes, lcd = go([], [(1_000_000, 2_500_000, "X")])
    assert lcd["st"]["us"] >= 2_500_000 and said[-1].startswith("end sw=")


@need_clang
def test_refuses_an_image_without_a_screen():
    ret, said, regs, writes, lcd = go([], [], level=5)
    assert ret == -3 and said == [] and writes == []


def test_screen_renderer_lays_out_like_the_firmware():
    f = {"text": ["SURF", "1", "", "", "", ""], "bar": [(0, 0)] * 6, "wf": [bytes([0x80] + [0] * 15)] * 30}
    fb = k6_screen.render(f)
    assert all(fb[y][127] == 1 for y in range(11)) and fb[11][127] == 0   # inverted title bar
    assert fb[21][0] == 1 and fb[50][0] == 1 and fb[20][0] == 0 and fb[51][0] == 0
    assert sum(fb[y][x] for y in range(12, 20) for x in range(1, 7)) > 0  # the "1" on line 1


if __name__ == "__main__":
    # python tests/test_k6_surf.py OUT.png: the emulated screens of the side-key run, for docs
    sig = [(440_850_000, -70, 0, FOREVER), (441_200_000, -95, 0, FOREVER), (440_500_000, -105, 0, FOREVER),
           (442_900_000, -90, 0, FOREVER)]
    keys = [(3_000_000, 3_200_000, "A"), (4_000_000, 4_200_000, "A"), (5_500_000, 5_700_000, "3"),
            (8_500_000, 8_800_000, "X")]
    table = [rt_record(0, 440_850_000, b"AG7EW"), rt_record(1, 441_200_000, b"N0CALL", 5_000_000, 1035)]
    _, _, _, _, lcd = go(sig, keys, table=table)
    fr = lcd["frames"]
    q = at(fr, 2_000_000)
    k6_screen.png([q, at(fr, 3_700_000), at(fr, 4_700_000), at(fr, 8_000_000)],
                  sys.argv[1] if len(sys.argv) > 1 else "surf.png",
                  captions=["440.600 in the middle, +-1 MHz", "SIDE1: 440.850 moves to the middle",
                            "SIDE1: next peak, a known repeater", "3: zoomed out to +-2.5 MHz"])
