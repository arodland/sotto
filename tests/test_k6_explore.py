"""The K6 explorer's candidate list (c/k6/explore.c): one candidate per frequency from the scan's
overs, the cues that tell a repeater from a simplex station, the offset guess, grades, ranking,
and the repeater table record it hands to Save. Also: it builds for the M0 with no library calls.
SPDX-License-Identifier: MIT"""
import ctypes
import os
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
K6 = os.path.join(ROOT, "c", "k6")
MIN = 60_000
sys.path.insert(0, os.path.join(ROOT, "tools"))
import k6_explore as kx  # noqa: E402
from k6_explore import F_IGNORED, F_OFF_HEARD, F_SIMPLEX, F_SPUR, F_TRIED, Rec  # noqa: E402


@pytest.fixture(scope="module")
def lib(tmp_path_factory):
    if not shutil.which("cc"):
        pytest.skip("needs a C compiler")
    return kx.load(str(tmp_path_factory.mktemp("ex") / "libex.so"))


class List(kx.Explorer):
    def __init__(self, L, cap=16):
        super().__init__(L, cap)

    def ranked(self):
        return [c.hz for c in super().ranked()]


def test_builds_for_the_m0_without_library_calls(tmp_path):
    if not (shutil.which("clang") and shutil.which("llvm-nm")):
        pytest.skip("needs clang")
    o = str(tmp_path / "explore.o")
    subprocess.run(["clang", "--target=thumbv6m-none-eabi", "-mcpu=cortex-m0", "-mthumb", "-Oz", "-ffreestanding",
                    "-fno-builtin", "-nostdlib", "-Wall", "-Wextra", "-Werror", "-I", K6, "-c",
                    os.path.join(K6, "explore.c"), "-o", o], check=True)
    und = subprocess.run(["llvm-nm", "-u", o], capture_output=True, text=True, check=True).stdout.split()
    assert und == [], und
    size = subprocess.run(["llvm-size", o], capture_output=True, text=True, check=True).stdout.split("\n")[1]
    text, data, bss = (int(x) for x in size.split()[:3])
    assert text < 2048 and data == 0 and bss == 0


def test_a_repeater_output_grades_up_with_its_cw_id(lib):
    x = List(lib)
    for k, lvl in enumerate([-80, -81, -79]):
        c = x.over(146_940_000, k * 3 * MIN, 20_000, lvl)
    # same level (+3), on a 2 m output sub-band (+2): a likely repeater, offset from the plan
    assert (c.grade, c.score, c.off_hz, c.overs) == (b"r", 5, -600_000, 3)
    assert not c.flags & F_OFF_HEARD
    x.cw(146_941_000, b"W7ABC/R")      # within the 5 kHz tolerance
    c = x.find(146_940_000)
    assert (c.grade, c.score, c.call) == (b"R", 8, b"W7ABC/R")
    r = Rec()
    lib.ex_to_rt(ctypes.byref(c), ctypes.byref(r))
    want = (146_940_000, -600_000, b"W7ABC/R", 8, ord("R"), 1)
    assert (r.rx_hz, r.tx_off, r.call, r.call_flags, r.score, r.flags) == want
    assert r.best_rssi == -79 + 200 and r.id == 0 and r.state == 0


def test_simplex_calling_with_wandering_levels_is_simplex(lib):
    x = List(lib)
    for k, lvl in enumerate([-60, -95, -78, -88]):
        c = x.over(146_520_000, k * MIN, 8000, lvl)
    assert c.grade == b"s" and c.score == -5 and c.off_hz == 0 and c.flags & F_SIMPLEX
    c = x.over(446_000_000, 0, 5000, -70)
    c = x.over(446_000_000, MIN, 5000, -70)
    assert c.grade == b"s" and c.off_hz == 0


def test_one_over_is_unknown_and_a_spur_stays_a_spur(lib):
    x = List(lib)
    assert x.over(442_500_000, 0, 4000, -90).grade == b"?"
    x.spur(442_500_000)
    for k in range(5):
        c = x.over(442_500_000, (k + 1) * MIN, 30_000, -90)
    assert c.grade == b"s" and c.flags & F_SPUR and c.score == -100     # last in the list


def test_an_input_heard_at_the_offset_wins_over_the_plan(lib):
    x = List(lib)
    x.over(443_100_000, 0, 30_000, -85)
    assert x.find(443_100_000).grade == b"?"
    x.input(443_100_000, -5_000_000)    # this one runs a reverse split; the user was heard 5 MHz down
    c = x.find(443_100_000)
    assert c.flags & F_OFF_HEARD and c.off_hz == -5_000_000
    assert c.grade == b"R" and c.score == 6     # input +4, output sub-band +2, even with one over
    x.over(443_100_000, MIN, 30_000, -84)
    assert c.off_hz == -5_000_000               # a regrade keeps the heard offset
    x.tone(443_100_000, 1000)
    assert c.score == 7 and c.tone == 1000
    r = Rec()
    lib.ex_to_rt(ctypes.byref(c), ctypes.byref(r))
    assert r.flags == 1 | 2 and r.rx_tone == r.tx_tone == 1000 and r.call_flags == 0


def test_steady_off_plan_is_a_maybe_and_far_apart_is_two(lib):
    x = List(lib)
    for k in range(3):
        x.over(433_500_000, k * MIN, 10_000, -70)
        x.over(433_510_000, k * MIN + 1, 10_000, -100 + 20 * k)
    a, b = x.find(433_500_000), x.find(433_510_000)
    assert x.e.n == 2
    assert (a.grade, a.score, a.off_hz) == (b"r", 3, 0)
    assert (b.grade, b.score) == (b"s", -2)


def test_short_overs_at_an_id_rhythm(lib):
    x = List(lib)
    # an ID every 9 minutes (like the bench's AG7EW keeper) between long overs
    t = 0
    for k in range(3):
        x.over(147_200_000, t, 6000, -75)
        x.over(147_200_000, t + 2 * MIN, 60_000, -75)
        t += 9 * MIN
    c = x.find(147_200_000)
    assert c.ids == 2 and c.off_hz == 600_000
    assert c.score == 3 + 2 + 1 and c.grade == b"R"
    # one short over 30 s after another is not an ID
    y = List(lib)
    for k in range(3):
        y.over(147_200_000, k * 30_000, 6000, -75)
    assert y.find(147_200_000).ids == 0


def test_rank_by_score_then_overs_and_ignored_last(lib):
    x = List(lib)
    for k in range(3):
        x.over(146_520_000, k * MIN, 5000, -60 - 15 * k)    # simplex: -5
        x.over(146_940_000, k * MIN, 5000, -80)             # +5
        x.over(433_500_000, k * MIN, 5000, -80)             # +3
        x.over(433_600_000, k * MIN, 5000, -80)             # +3, a tie: more overs first
    x.over(433_600_000, 4 * MIN, 5000, -80)
    assert x.ranked() == [146_940_000, 433_600_000, 433_500_000, 146_520_000]
    x.find(146_940_000).flags |= F_IGNORED
    assert x.ranked() == [433_600_000, 433_500_000, 146_520_000, 146_940_000]
    # an exact tie keeps both, in list order
    y = List(lib)
    for hz in (433_500_000, 433_600_000, 433_700_000):
        y.over(hz, 0, 5000, -80)
    assert y.ranked() == [433_500_000, 433_600_000, 433_700_000]


def test_a_full_list_drops_the_weakest_but_keeps_ignored_and_tried(lib):
    x = List(lib, cap=3)
    for k in range(2):
        x.over(146_520_000, k * MIN, 5000, -60 - 30 * k)    # -5
        x.over(146_550_000, k * MIN, 5000, -60 - 30 * k)    # -5, ignored
        x.over(433_500_000, k * MIN, 5000, -80)
    x.find(146_550_000).flags |= F_IGNORED
    c = x.over(433_700_000, 5 * MIN, 5000, -80)
    assert c is not None and x.e.n == 3
    assert x.find(146_520_000) is None and x.find(146_550_000) is not None
    x.find(433_500_000).flags |= F_TRIED
    x.find(433_700_000).flags |= F_TRIED
    assert x.over(445_000_000, 6 * MIN, 5000, -80) is None


def test_replays_a_scan_and_cwlisten_log(lib):
    log = "\n".join([
        "08:10:00 K6 MOD say H 146.940 20000 -80 a10000",
        "08:13:00 K6 MOD say H 146.940 30000 -81 a190000",
        "08:16:00 K6 MOD say H 146.940 25000 -79 a370000",
        "08:10:20 K6 MOD say H 146.520 8000 -60 n1",         # the first run's form: timed by the log
        "08:10:40 K6 MOD say H 146.520 8000 -95 n2",
        "08:11:00 K6 MOD say H 146.520 8000 -70 n3",
        "08:11:00 K6 MOD say X 146.610 -100",
        "08:20:00 K6 MOD say CWID ok W7ABC/R fl=4 rx=146940000 tone=800 wpm=20"])
    x = kx.Explorer(lib)
    assert kx.replay(x, log) == [6, 1, 1]
    rows = x.ranked()
    assert [(c.hz, c.grade) for c in rows] == [(146_940_000, b"R"), (146_520_000, b"s"), (146_610_000, b"s")]
    assert rows[1].first_ms == (8 * 3600 + 10 * 60 + 12) * 1000
    assert kx.row_text(rows[0]).startswith("R  146.9400 -0.6 MHz W7ABC/R  score 8, 3 overs")
    assert kx.row_text(rows[2]).endswith("spur")
    assert kx.plan_rows(lib)[2] == (146_610_000, 146_970_000, -600_000, False)


def test_a_strong_carrier_s_skirts_are_marked(lib):
    # scan-5 on air (2026-11-07): the CW ID at -22 dBm on 440.850, and 440.862 / .900 / .912 at
    # -58 to -79, ending with it
    x = kx.Explorer(lib)
    for k, t in enumerate((1000, 21000)):
        x.over(440_862_500, t + 300, 7400, -58)          # its H line comes first
        x.over(440_850_000, t, 7850, -22)
        x.over(440_912_500, t + 500, 7000, -76)
    x.over(440_900_000, 40_000, 3000, -79)              # this one alone: no louder neighbour then
    rows = {c.hz: c for c in x.ranked()}
    assert rows[440_850_000].score > -100 and rows[440_850_000].skirts == 0
    for hz in (440_862_500, 440_912_500):
        assert rows[hz].skirts == rows[hz].overs == 2 and rows[hz].grade == b"s" and rows[hz].score == -100
        assert "skirt" in kx.row_text(rows[hz])
    assert rows[440_900_000].skirts == 0 and rows[440_900_000].score != -100
    assert x.ranked()[0].hz == 440_850_000
