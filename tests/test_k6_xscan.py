"""The EXPLORE app's C engine for the core image (Phase 3 of the K6 signal explorer), on this
machine against the same modelled receiver as the scan module's tests:

  - xscan (c/k6/xscan.c): the sweep a block at a time finds carriers that come and go, each
    over goes into the candidate list (explore.c) and out as the scan module's H line, so the
    serial log of the app replays to the same list (tools/k6_explore.py);
  - a step never runs long (the keys and the screen get the CPU back every block), a key stops
    a step before it starts, and segments it can't hold are refused;
  - xlisten (c/k6/xlisten.c): Listen for ID decodes a modelled tone detector keyed with a CW ID
    and confirms the call on the second ID, as cwlisten did on air;
  - all of it builds for the M0 with no library calls and no writable statics.
SPDX-License-Identifier: MIT"""
import ctypes
import os
import random
import shutil
import subprocess
import sys
import tempfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
K6 = os.path.join(ROOT, "c", "k6")
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "tools"))
from test_k6_scan import Receiver  # noqa: E402
from test_k6_cwid import MORSE  # noqa: E402
import k6_explore as kx  # noqa: E402
import k6_scan  # noqa: E402

SRCS = ["xscan.c", "xlisten.c", "explore.c", "cwid.c", "rt.c"]
U32, I32, U16, U8 = ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint16, ctypes.c_uint8
SAY = ctypes.CFUNCTYPE(None, ctypes.c_char_p)
TICKS = ctypes.CFUNCTYPE(U32)
FDR = ctypes.CFUNCTYPE(U16, U8)
FDW = ctypes.CFUNCTYPE(ctypes.c_int, U8, U16)
DLY = ctypes.CFUNCTYPE(None, U32)
TUNE = ctypes.CFUNCTYPE(None, U32, ctypes.c_int)
INT = ctypes.CFUNCTYPE(ctypes.c_int)
SCR = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.POINTER(U32))
TXON = ctypes.CFUNCTYPE(ctypes.c_int, U32)
VOID = ctypes.CFUNCTYPE(None)
TXPA = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int)
CALR = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, U32)


class Api(ctypes.Structure):
    _fields_ = [("abi", U16), ("level", U16), ("say", SAY), ("ticks", TICKS), ("fd_read", FDR),
                ("fd_write", FDW), ("delay_us", DLY), ("tune", TUNE), ("rx_hz", TICKS), ("ms", TICKS),
                ("key_down", INT), ("scratch", SCR), ("tx_on", TXON), ("tx_off", VOID), ("tx_pa", TXPA),
                ("key_poll", INT), ("cal_read", CALR), ("cal_write", CALR)]


class Seg(ctypes.Structure):
    _fields_ = [("lo_hz", U32), ("step_hz", U32), ("nbins", U16), ("snap_hz", U16)]


class XsHead(ctypes.Structure):
    """the settings at the start of xs_t (xscan.h)"""
    _fields_ = [("nseg", U8), ("wide", U8), ("noise_db", U8), ("hit_db", U8), ("refine", U8),
                ("say_rows", U8), ("quiet", U8), ("refloor_s", U8), ("quick_us", U16), ("confirm_us", U16),
                ("hot_ms", U16), ("spur_s", U16), ("seg", Seg * 4)]


class XsFloors(ctypes.Structure):
    """xs_t up to its floors, to drag them down as a strong carrier did on air"""
    _fields_ = [("head", XsHead), ("api", ctypes.c_void_p), ("ex", ctypes.c_void_p), ("fl", ctypes.c_void_p),
                ("row", ctypes.c_void_p), ("sweeps", U32), ("bins", U32), ("rev", U32), ("t0", U32),
                ("sfl", U8 * 4), ("qfl", U8 * 4), ("fl_t", U32 * 4)]


class XsPos(ctypes.Structure):
    """xs_t up to where the sweep is"""
    _fields_ = XsFloors._fields_ + [("phase", U8), ("g", U8), ("cols", U8), ("last_g", U8), ("b", U16),
                                    ("base", U16)]


class XsInfo(ctypes.Structure):
    _fields_ = [("sweeps", U32), ("bins", U32), ("ms", U32), ("last_ms", U16), ("g", U8), ("phase", U8),
                ("floor_dbm", ctypes.c_int16 * 4), ("hot", U8), ("up", U8), ("pad", U8 * 2)]


@pytest.fixture(scope="module")
def lib():
    if not shutil.which("cc"):
        pytest.skip("needs a C compiler")
    so = os.path.join(tempfile.mkdtemp(prefix="k6x"), "libx.so")
    subprocess.run(["cc", "-O1", "-g", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC", "-I", K6, "-o", so]
                   + [os.path.join(K6, f) for f in SRCS], check=True)
    L = ctypes.CDLL(so)
    for f in ("xs_state_bytes", "xl_state_bytes", "xs_mem_bytes"):
        getattr(L, f).restype = U32
    L.xs_init.argtypes = [ctypes.c_void_p, ctypes.POINTER(Api), ctypes.c_void_p, ctypes.c_void_p, U32]
    L.xs_step.argtypes = L.xs_close.argtypes = L.xs_summary.argtypes = L.xs_resume.argtypes = [ctypes.c_void_p]
    L.xs_info.argtypes = [ctypes.c_void_p, ctypes.POINTER(XsInfo)]
    L.xl_start.argtypes = [ctypes.c_void_p, ctypes.POINTER(Api), U32, U32]
    L.xl_step.argtypes = [ctypes.c_void_p]
    L.xl_stop.argtypes = [ctypes.c_void_p, ctypes.c_int]
    # explore.c through k6_explore's typing
    kx_lib = kx.load(os.path.join(os.path.dirname(so), "libex.so"))
    return L, kx_lib


class Radio:
    """a k6_api_t over a model: time in us, moved only by delay_us (and 20 us a register read)"""

    def __init__(self, model, keys_at=()):
        self.m, self.us, self.lines, self.keys_at, self.writes = model, 0, [], list(keys_at), []
        self.longest = 0
        api = Api()
        api.abi, api.level = 1, 5
        self._keep = [SAY(self.say), TICKS(lambda: (self.us // 100) & 0xFFFF), FDR(self.fd_read),
                      FDW(self.fd_write), DLY(self.delay), TUNE(self.tune), TICKS(lambda: 440_850_000),
                      TICKS(lambda: self.us // 1000), INT(self.key), SCR(lambda b: None), TXON(lambda m: -1),
                      VOID(lambda: None), TXPA(lambda o: -1), INT(self.key), CALR(lambda b, n: -1),
                      CALR(lambda b, n: -1)]
        for (name, _), f in zip(Api._fields_[2:], self._keep):
            setattr(api, name, f)
        self.api = api

    def say(self, s):
        self.lines.append(f"{self.us / 1e6:.3f} K6 MOD say {s.decode()}")

    def fd_read(self, reg):
        self.us += 20
        if reg == 0x67:
            return self.m.rssi(self.us)
        return self.m.reg(reg, self.us) if hasattr(self.m, "reg") else 0

    def fd_write(self, reg, v):
        self.writes.append((reg, v))
        return 0

    def delay(self, us):
        self.us += us

    def tune(self, hz, wide):
        self.us += 300
        self.m.tune(hz, wide, self.us)

    def key(self):
        return int(any(a <= self.us < b for a, b in self.keys_at))


def explorer(L, kxl, segs, radio, cap=64):
    head = XsHead()
    buf = ctypes.create_string_buffer(L.xs_state_bytes())
    L.xs_defaults(buf)
    ctypes.memmove(ctypes.byref(head), buf, ctypes.sizeof(head))
    head.nseg = len(segs)
    for i, (lo, step, n, snap) in enumerate(segs):
        head.seg[i] = Seg(lo, step, n, snap)
    ctypes.memmove(buf, ctypes.byref(head), ctypes.sizeof(head))
    mem = ctypes.create_string_buffer(L.xs_mem_bytes(buf))
    ex = kx.Explorer(kxl, cap)
    return buf, mem, ex


def run_steps(L, buf, radio, seconds):
    out = []
    while radio.us < seconds * 1e6:
        t = radio.us
        out.append(L.xs_step(buf))
        radio.longest = max(radio.longest, radio.us - t)
        if out[-1] == 2:
            break
    return out


def s_(sec):
    return int(sec * 1e6)


def test_builds_for_the_m0_without_library_calls(tmp_path):
    if not (shutil.which("clang") and shutil.which("llvm-nm")):
        pytest.skip("needs clang")
    sizes = {}
    for f in ("xscan.c", "xlisten.c"):
        o = str(tmp_path / (f + ".o"))
        subprocess.run(["clang", "--target=thumbv6m-none-eabi", "-mcpu=cortex-m0", "-mthumb", "-Oz", "-ffreestanding",
                        "-fno-builtin", "-nostdlib", "-Wall", "-Wextra", "-Werror", "-I", K6, "-c",
                        os.path.join(K6, f), "-o", o], check=True)
        und = set(subprocess.run(["llvm-nm", "-u", o], capture_output=True, text=True, check=True).stdout.split())
        und -= {"U"}
        assert und <= {"ex_over", "ex_spur", "ex_own_clock", "cw_call", "cw_div", "cw_feed", "cw_flush", "cw_init", "cw_utoa"}, und
        size = subprocess.run(["llvm-size", o], capture_output=True, text=True, check=True).stdout.split("\n")[1]
        text, data, bss = (int(x) for x in size.split()[:3])
        assert data == 0 and bss == 0
        sizes[f] = text
    assert sizes["xscan.c"] < 4900 and sizes["xlisten.c"] < 1700, sizes


def test_finds_a_repeater_feeds_the_list_and_the_log_replays_the_same(lib):
    L, kxl = lib
    # a repeater output keyed four times, a simplex station once, on the 2 m output sub-band
    rep = [(s_(5 + 20 * k), s_(14 + 20 * k)) for k in range(4)]
    model = Receiver([(146_940_000, -80, rep), (146_520_000, -70, [(s_(30), s_(36))])])
    radio = Radio(model)
    buf, mem, ex = explorer(L, kxl, [(146_400_000, 10_000, 60, 5_000), (146_600_000, 10_000, 80, 5_000)], radio)
    assert L.xs_init(buf, ctypes.byref(radio.api), ctypes.byref(ex.e), mem, len(mem)) == 0
    res = run_steps(L, buf, radio, 90)
    L.xs_close(buf)
    L.xs_summary(buf)
    assert 2 not in res and res.count(1) >= 40           # 140 bins a pass, many passes
    assert radio.longest < 200_000, radio.longest         # no step holds the CPU long
    c = ex.find(146_940_000)
    assert c is not None and c.overs == 4 and c.grade == b"r" and c.off_hz == -600_000, kx.row_text(c)
    assert all(abs(c.hi - -80) <= 2 for _ in [0])
    s = ex.find(146_520_000)
    assert s is not None and s.overs == 1 and s.flags & kx.F_SIMPLEX
    log = "\n".join(radio.lines)
    dec = k6_scan.decode(log)
    assert len([o for o in dec["overs"] if o["f"] == 146_940_000]) == 4
    assert dec["z"] and dec["z"]["sweeps"] >= 20
    # the app's serial log replays to the same list on the PC
    again = kx.Explorer(kxl)
    kx.replay(again, log)
    assert [(c.hz, c.grade, c.overs) for c in again.ranked()] == [(c.hz, c.grade, c.overs) for c in ex.ranked()]
    info = XsInfo()
    L.xs_info(buf, ctypes.byref(info))
    assert info.phase == 1 and info.sweeps >= 20 and info.floor_dbm[1] <= -130


def test_a_key_stops_a_step_and_bad_segments_are_refused(lib):
    L, kxl = lib
    radio = Radio(Receiver([]), keys_at=[(s_(3), s_(4))])
    buf, mem, ex = explorer(L, kxl, [(146_600_000, 10_000, 80, 5_000)], radio)
    assert L.xs_init(buf, ctypes.byref(radio.api), ctypes.byref(ex.e), mem, len(mem)) == 0
    res = run_steps(L, buf, radio, 10)
    assert res[-1] == 2 and s_(3) <= radio.us < s_(3.3)
    assert not [w for w in radio.writes]                  # the sweep never writes a register
    # too many bins, and too little memory
    big, mem2, _ = explorer(L, kxl, [(144_000_000, 10_000, 1100, 5_000)], radio)
    assert L.xs_init(big, ctypes.byref(radio.api), None, mem2, len(mem2)) == -8
    ok, mem3, _ = explorer(L, kxl, [(144_000_000, 10_000, 400, 5_000)], radio)
    assert L.xs_init(ok, ctypes.byref(radio.api), None, mem3, len(mem3) - 1) == -8
    bad, mem4, _ = explorer(L, kxl, [(144_000_000, 0, 400, 5_000)], radio)
    assert L.xs_init(bad, ctypes.byref(radio.api), None, mem4, len(mem4)) == -11


def test_the_field_test_bands_fit_the_borrowed_ram(lib):
    L, _ = lib
    # the EXPLORE app's default set: 2 m, 1.25 m outputs and 70 cm 440-445 (docs/k6-explore-app.md)
    head = XsHead()
    buf = ctypes.create_string_buffer(L.xs_state_bytes())
    L.xs_defaults(buf)
    ctypes.memmove(ctypes.byref(head), buf, ctypes.sizeof(head))
    head.nseg = 3
    head.seg[0] = Seg(144_000_000, 10_000, 400, 5_000)
    head.seg[1] = Seg(223_800_000, 20_000, 60, 20_000)
    head.seg[2] = Seg(440_000_000, 12_500, 400, 12_500)
    ctypes.memmove(buf, ctypes.byref(head), ctypes.sizeof(head))
    # the floors and the row go in the PWM ring (2,560 B) beside Listen for ID's state
    assert L.xs_mem_bytes(buf) + L.xl_state_bytes() <= 2560
    # the state and 64 candidates go in the 4 KB message store
    assert L.xs_state_bytes() + 12 + 64 * 48 <= 4096, L.xs_state_bytes()


def test_floors_dragged_down_by_a_strong_carrier_come_back(lib):
    """explorer-scan-6 on the bench: the IC-7100's own ID (-23 dBm on 440.850) pulled 70 cm's
    floor from -104 to -119, and with the band quiet afterwards nothing brought it back, so three
    bins at the old floor read 13 dB up and stayed 'keyed' for the last 20 minutes. The sweep now
    reads each segment's floor again every refloor_s (60 s) and lifts any bin's floor that sits
    under it, so they end. Here the floors are dragged 15 dB down by hand at 20 s."""
    L, kxl = lib
    segs = [(146_400_000, 10_000, 60, 5_000), (146_600_000, 10_000, 80, 5_000)]

    def run(refloor_s):
        radio = Radio(Receiver([]))
        buf, mem, ex = explorer(L, kxl, segs, radio)
        head = XsHead.from_buffer(buf)
        head.refloor_s = refloor_s
        assert L.xs_init(buf, ctypes.byref(radio.api), ctypes.byref(ex.e), mem, len(mem)) == 0
        run_steps(L, buf, radio, 20)
        st = XsFloors.from_buffer(buf)
        for g in range(2):
            st.sfl[g] -= 15
        for b in range(140):
            mem[b] = bytes([max(0, mem[b][0] - 15)])
        res = run_steps(L, buf, radio, 160)
        info = XsInfo()
        L.xs_info(buf, ctypes.byref(info))
        return info, ex, radio, res

    def tally(radio):
        said = lambda k: {l.split()[5] for l in radio.lines if f" say {k} " in l}
        return said("N"), said("H"), said("X")

    # the old way: the phantoms stay up until they pass for spurs (on air, 20 minutes up)
    _, _, radio0, _ = run(0)
    n, h, x = tally(radio0)
    assert len(n) >= 5 and not h and x == n
    info, ex, radio, res = run(60)
    n, h, x = tally(radio)
    assert len(n) >= 5 and h == n and not x and info.up == 0, (n, h, x)   # every phantom ends
    assert all(-140 <= info.floor_dbm[g] <= -134 for g in range(2)), list(info.floor_dbm)
    assert 2 not in res and radio.longest < 200_000, radio.longest   # the re-read is a step of its own
    assert len([l for l in radio.lines if " F0 " in l]) >= 3            # at the start and every minute


class Rising(Receiver):
    """the floor db louder from at_us on (bench 3: 2 m's floor rose 8 dB during a listen)"""

    def __init__(self, signals, at_us, db):
        super().__init__(signals)
        self.at, self.db = at_us, db

    def level(self, us):
        return super().level(us) + (self.db if us >= self.at else 0)


def test_back_from_a_listen_the_segment_reads_its_floor_again(lib):
    """explore-bench-3 (20:25): an automatic listen held the receiver 2 minutes, 2 m's floor came
    back 8 dB up, and the sweep went on from the middle of the segment against the old floor: a
    wall of 23 new hits (146.900 to 147.990, -88 to -91 dBm, fl-101), and the next automatic listen
    sat 2 minutes on noise. xs_resume reads the segment's floor again and sweeps it from its start."""
    L, kxl = lib
    segs = [(146_400_000, 10_000, 60, 5_000), (146_600_000, 10_000, 80, 5_000)]

    def run(resume):
        radio = Radio(Rising([], at_us=s_(40), db=13))
        buf, mem, ex = explorer(L, kxl, segs, radio)
        assert L.xs_init(buf, ctypes.byref(radio.api), ctypes.byref(ex.e), mem, len(mem)) == 0
        run_steps(L, buf, radio, 30)
        pos = XsPos.from_buffer(buf)
        while not (pos.g == 1 and 20 <= pos.b < 40):      # stopped in the middle of a segment
            L.xs_step(buf)
        radio.us += s_(120)                              # the listen: the floor rises meanwhile
        mark = len(radio.lines)
        if resume:
            L.xs_resume(buf)
            assert pos.b == 0 and pos.g == 1
        res = run_steps(L, buf, radio, radio.us / 1e6 + 20)
        after = radio.lines[mark:]
        return [l for l in after if " say N " in l], [l for l in after if " say F" in l], res, radio

    n, _, _, _ = run(False)
    assert len(n) >= 3, n                                # the old way: phantoms
    n, f, res, radio = run(True)
    assert not n, n
    assert f and " F1 " in f[0], f                       # the segment it was in reads first
    assert 2 not in res and radio.longest < 200_000


class Dip(Receiver):
    """every read db lower from at_us to until_us: a strong carrier's desense"""

    def __init__(self, signals, at_us, until_us, db):
        super().__init__(signals)
        self.at, self.until, self.db = at_us, until_us, db

    def level(self, us):
        return super().level(us) - (self.db if self.at <= us < self.until else 0)


def test_a_floor_read_under_desense_is_held(lib):
    """explore-bench-4 (21:09): the listen on 440.850 ended with the -18 dBm ID still up, the floor
    read after it came back 30 dB low (F2 -139 for -109), and 440.862, 440.900, 444.550 (-116) ...
    came up as new against it. A read 15 dB under the last is held once and tried again in 5 s; a
    floor that really dropped is taken on the second read."""
    L, kxl = lib
    segs = [(440_000_000, 12_500, 100, 12_500)]

    def run(model, gap_at):
        radio = Radio(model)
        buf, mem, ex = explorer(L, kxl, segs, radio)
        assert L.xs_init(buf, ctypes.byref(radio.api), ctypes.byref(ex.e), mem, len(mem)) == 0
        run_steps(L, buf, radio, gap_at)
        L.xs_resume(buf)                              # back from a listen, during the dip
        mark = len(radio.lines)
        run_steps(L, buf, radio, gap_at + 30)
        info = XsInfo()
        L.xs_info(buf, ctypes.byref(info))
        return radio.lines[mark - 1:], info

    lines, info = run(Dip([], s_(20), s_(21), 30), 20.2)
    assert any(" F0 held " in l for l in lines), lines[:3]
    assert not [l for l in lines if " say N " in l], [l for l in lines if " say N " in l]
    assert -140 <= info.floor_dbm[0] <= -134, list(info.floor_dbm)
    # a band that really went 20 dB quieter: the second read stands
    lines, info = run(Dip([], s_(20), s_(1000), 20), 20.2)
    assert any(" F0 held " in l for l in lines) and info.floor_dbm[0] <= -150, list(info.floor_dbm)


def test_a_spur_does_not_hold_the_floor_off(lib):
    """a carrier 30 dB over the floor puts the segment's floor read off (its desense pulls the
    reads down), but a spur is up for good: on air the 144.000 birdie held 2 m's read off until
    the 4-minute limit. Once it's a spur the floor is read every minute again."""
    L, kxl = lib
    radio = Radio(Receiver([(146_700_000, -70, [(0, s_(1000))])]))
    buf, mem, ex = explorer(L, kxl, [(146_600_000, 10_000, 80, 5_000)], radio)
    assert L.xs_init(buf, ctypes.byref(radio.api), ctypes.byref(ex.e), mem, len(mem)) == 0
    run_steps(L, buf, radio, 200)
    log = radio.lines
    x = [float(l.split()[0]) for l in log if " say X " in l]
    f = [float(l.split()[0]) for l in log if " say F0 " in l]
    assert x and 55 < x[0] < 70, x                      # it passes for a spur after spur_s
    assert len([t for t in f if t > x[0]]) >= 2, (x, f)  # and the floor is read each minute after


def test_the_k6_s_own_clock_is_a_spur_at_once(lib):
    """bench 2: the 144.000 birdie (6 x the MCU's 24 MHz) sat on the list for a minute until it
    passed for a spur; birdie-path-1 showed the front end off takes 55 dB off it as off a station,
    so its frequency is what gives it away. Now it's a spur on its first hit, and never up."""
    L, kxl = lib
    L.xs_up.argtypes = [ctypes.c_void_p, ctypes.POINTER(U32), ctypes.c_uint]
    L.xs_up.restype = ctypes.c_uint
    radio = Radio(Receiver([(144_000_000, -72, [(0, s_(1000))]), (144_150_000, -80, [(s_(2), s_(30))])]))
    buf, mem, ex = explorer(L, kxl, [(143_900_000, 10_000, 60, 5_000)], radio)
    assert L.xs_init(buf, ctypes.byref(radio.api), ctypes.byref(ex.e), mem, len(mem)) == 0
    run_steps(L, buf, radio, 5)
    x = [l for l in radio.lines if " say X " in l]
    assert len(x) == 1 and " X 144.000 " in x[0] and float(x[0].split()[0]) < 1.0, x
    c = ex.find(144_000_000)
    assert c is not None and c.flags & kx.F_SPUR and abs(c.hi + 72) <= 2 and c.overs == 0, kx.row_text(c)
    up = (U32 * 10)()
    assert list(up[:L.xs_up(buf, up, 10)]) == [144_150_000]    # the station is up, the birdie never


def test_an_over_carries_its_level_after_the_transmitter_comes_up(lib):
    """explorer-scan-6: the 13:23 ID's first read caught the IC-7100 coming up (-72 dBm, not
    -23), so its skirts weren't 25 dB under it and listed as stations. A quick read above the
    over's highest takes a full read again."""
    L, kxl = lib
    hz = 146_940_000
    model = Receiver([(hz, -72, [(s_(5), s_(15))]), (hz, -25, [(s_(5.6), s_(15))])])
    radio = Radio(model)
    buf, mem, ex = explorer(L, kxl, [(146_600_000, 10_000, 80, 5_000)], radio)
    assert L.xs_init(buf, ctypes.byref(radio.api), ctypes.byref(ex.e), mem, len(mem)) == 0
    run_steps(L, buf, radio, 25)
    overs = k6_scan.decode("\n".join(radio.lines))["overs"]
    assert [o["lvl"] for o in overs if o["f"] == hz] and all(abs(o["lvl"] + 25) <= 2 for o in overs if o["f"] == hz), overs


def test_up_list_leaves_out_a_louder_carrier_s_skirts(lib):
    """the EXPLORE bench run (17:57): the -24 dBm ID showed at 440.900 too (-81), and the
    automatic listener went there; xs_up now leaves out what's 20 dB under one up within 100 kHz"""
    L, kxl = lib
    L.xs_up.argtypes = [ctypes.c_void_p, ctypes.POINTER(U32), ctypes.c_uint]
    L.xs_up.restype = ctypes.c_uint
    on = [(s_(5), s_(25))]
    radio = Radio(Receiver([(440_850_000, -24, on), (440_900_000, -81, on), (442_000_000, -85, on)]))
    buf, mem, ex = explorer(L, kxl, [(440_000_000, 12_500, 240, 12_500)], radio)
    assert L.xs_init(buf, ctypes.byref(radio.api), ctypes.byref(ex.e), mem, len(mem)) == 0
    run_steps(L, buf, radio, 15)
    up = (U32 * 10)()
    n = L.xs_up(buf, up, 10)
    assert sorted(up[:n]) == [440_850_000, 442_000_000], list(up[:n])


class Detector:
    """the FD6818's tone detector keyed by an ID: REG_0B bit 12 set (tone 4, 800 Hz, in [11:8])
    while a mark is on, 10 ms late on and off as on air"""

    def __init__(self, spans):
        self.spans = spans

    def tune(self, *a):
        pass

    def rssi(self, us):
        return (-60 + 174) << 1

    def reg(self, reg, us):
        if reg == 0x0B:
            on = any(a + 10_000 <= us < b + 10_000 for a, b in self.spans)
            return 0xC006 | (0x1000 | 0x0400 if on else 0)
        return 0


def id_spans(text, wpm, t0_us):
    u = 1_200_000 // wpm
    t, out = t0_us, []
    for w, word in enumerate(text.split()):
        if w:
            t += 4 * u
        for i, ch in enumerate(word):
            if i:
                t += 2 * u
            for j, el in enumerate(MORSE[ch]):
                if j:
                    t += u
                d = u if el == "." else 3 * u
                out.append((t, t + d))
                t += d
    return out


def test_listen_for_id_confirms_the_call_on_the_second_id(lib):
    L, _ = lib
    spans = id_spans("AG7EW SOTTO TEST", 20, s_(2)) + id_spans("AG7EW SOTTO TEST", 20, s_(40))
    radio = Radio(Detector(spans))
    x = ctypes.create_string_buffer(L.xl_state_bytes())
    L.xl_start(x, ctypes.byref(radio.api), 440_850_000, 120)
    res = []
    while radio.us < s_(120):
        t = radio.us
        r = L.xl_step(x)
        radio.longest = max(radio.longest, radio.us - t)
        res.append(r)
        if r in (1, 2):
            break
    L.xl_stop(x, res[-1])
    assert res[-1] == 1 and res.count(3) == 1 and s_(46) < radio.us < s_(50)
    assert radio.longest < 100_000
    ok = [ln for ln in radio.lines if "CWID ok" in ln]
    assert len(ok) == 1 and "CWID ok AG7EW fl=0 rx=440850000 tone=800 wpm=20" in ok[0], radio.lines
    assert radio.lines[-1].endswith("CWID end ok msgs=2 short=0")
    assert (0x24, 0) in radio.writes                      # the detector off at the end
    # the replay tool reads the listener's ok line as cwlisten's
    assert kx.CWID.search(ok[0]).group(1) == "AG7EW"


def test_listen_times_out_with_no_id(lib):
    L, _ = lib
    radio = Radio(Detector([]))
    x = ctypes.create_string_buffer(L.xl_state_bytes())
    L.xl_start(x, ctypes.byref(radio.api), 440_850_000, 5)
    r = 0
    while r == 0:
        r = L.xl_step(x)
    assert r == 2 and s_(5) <= radio.us < s_(5.2)
