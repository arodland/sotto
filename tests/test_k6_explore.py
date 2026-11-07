"""The K6 explorer's candidate list (c/k6/explore.c): one candidate per frequency from the scan's
overs, the cues that tell a repeater from a simplex station, the offset guess, grades, ranking,
and the repeater table record it hands to Save. Also: it builds for the M0 with no library calls.
SPDX-License-Identifier: MIT"""
import ctypes
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
K6 = os.path.join(ROOT, "c", "k6")
MIN = 60_000
F_IGNORED, F_TRIED, F_OFF_HEARD, F_SIMPLEX, F_SPUR = 1, 2, 4, 8, 16


class Cand(ctypes.Structure):
    _fields_ = [("hz", ctypes.c_uint32), ("first_ms", ctypes.c_uint32), ("last_ms", ctypes.c_uint32),
                ("on_ms", ctypes.c_uint32), ("off_hz", ctypes.c_int32), ("overs", ctypes.c_uint16),
                ("tone", ctypes.c_uint16), ("lo", ctypes.c_int16), ("hi", ctypes.c_int16),
                ("prev_start", ctypes.c_uint32), ("ids", ctypes.c_uint8), ("flags", ctypes.c_uint8),
                ("score", ctypes.c_int8), ("grade", ctypes.c_char), ("call", ctypes.c_char * 10)]


class Ex(ctypes.Structure):
    _fields_ = [("c", ctypes.POINTER(Cand)), ("n", ctypes.c_uint16), ("cap", ctypes.c_uint16),
                ("tol_hz", ctypes.c_uint32)]


class Rec(ctypes.Structure):
    _fields_ = [("state", ctypes.c_uint16), ("seq", ctypes.c_uint16), ("rx_hz", ctypes.c_uint32),
                ("tx_off", ctypes.c_int32), ("rx_tone", ctypes.c_uint16), ("tx_tone", ctypes.c_uint16),
                ("flags", ctypes.c_uint16), ("best_rssi", ctypes.c_uint8), ("score", ctypes.c_uint8),
                ("last_heard", ctypes.c_uint32), ("name", ctypes.c_char * 16), ("call", ctypes.c_char * 10),
                ("call_flags", ctypes.c_uint8), ("cw", ctypes.c_uint8), ("group", ctypes.c_uint8),
                ("spare0", ctypes.c_uint8), ("id", ctypes.c_uint16), ("wseq", ctypes.c_uint32),
                ("crc", ctypes.c_uint32)]


@pytest.fixture(scope="module")
def lib(tmp_path_factory):
    if not shutil.which("cc"):
        pytest.skip("needs a C compiler")
    so = str(tmp_path_factory.mktemp("ex") / "libex.so")
    subprocess.run(["cc", "-O1", "-g", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC", "-o", so,
                    os.path.join(K6, "explore.c")], check=True)
    L = ctypes.CDLL(so)
    P = ctypes.POINTER(Ex)
    L.ex_init.argtypes = [P, ctypes.POINTER(Cand), ctypes.c_uint]
    L.ex_over.argtypes = [P, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int]
    L.ex_over.restype = ctypes.POINTER(Cand)
    L.ex_spur.argtypes = [P, ctypes.c_uint32]
    L.ex_input.argtypes = [P, ctypes.c_uint32, ctypes.c_int32]
    L.ex_tone.argtypes = [P, ctypes.c_uint32, ctypes.c_uint16]
    L.ex_cw.argtypes = [P, ctypes.c_uint32, ctypes.c_char_p]
    L.ex_find.argtypes = [P, ctypes.c_uint32]
    L.ex_find.restype = ctypes.POINTER(Cand)
    L.ex_rank.argtypes = [P, ctypes.c_uint]
    L.ex_rank.restype = ctypes.POINTER(Cand)
    L.ex_to_rt.argtypes = [ctypes.POINTER(Cand), ctypes.POINTER(Rec)]
    return L


class List:
    def __init__(self, L, cap=16):
        self.L, self.store, self.e = L, (Cand * cap)(), Ex()
        L.ex_init(ctypes.byref(self.e), self.store, cap)

    def over(self, hz, t, dur, lvl):
        p = self.L.ex_over(ctypes.byref(self.e), hz, t, dur, lvl)
        return p.contents if p else None

    def find(self, hz):
        p = self.L.ex_find(ctypes.byref(self.e), hz)
        return p.contents if p else None

    def ranked(self):
        out, i = [], 0
        while True:
            p = self.L.ex_rank(ctypes.byref(self.e), i)
            if not p:
                return out
            out.append(p.contents.hz)
            i += 1

    def __getattr__(self, name):
        f = getattr(self.L, "ex_" + name)
        return lambda *a: f(ctypes.byref(self.e), *a)


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
    assert c.grade == b"s" and c.flags & F_SPUR


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
