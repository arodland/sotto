"""The explorer's repeater table (c/k6/rt.c) on this machine, against NOR flash rules
(c/k6/rt_host.c): records, edits as a log, compaction into the spare sector, the region check,
power cut at every flash operation of an edit and of a compaction, and the CW ID policy (Jeff,
2026-11-07: fill a blank call by itself, a changed call only after a key press). Also: it builds
for the M0 with no library calls (it has to fit the core or a RAM module).
SPDX-License-Identifier: MIT"""
import ctypes
import os
import random
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
K6 = os.path.join(ROOT, "c", "k6")
need_cc = pytest.mark.skipif(not shutil.which("cc"), reason="needs a C compiler")
BASE = 0x0F0000
SLOTS, PER = 1024, 64


class Rec(ctypes.Structure):
    _fields_ = [("state", ctypes.c_uint16), ("seq", ctypes.c_uint16), ("rx_hz", ctypes.c_uint32),
                ("tx_off", ctypes.c_int32), ("rx_tone", ctypes.c_uint16), ("tx_tone", ctypes.c_uint16),
                ("flags", ctypes.c_uint16), ("best_rssi", ctypes.c_uint8), ("score", ctypes.c_uint8),
                ("last_heard", ctypes.c_uint32), ("name", ctypes.c_char * 16), ("call", ctypes.c_char * 10),
                ("call_flags", ctypes.c_uint8), ("cw", ctypes.c_uint8), ("group", ctypes.c_uint8),
                ("spare0", ctypes.c_uint8), ("id", ctypes.c_uint16), ("wseq", ctypes.c_uint32),
                ("crc", ctypes.c_uint32)]


class RT(ctypes.Structure):
    _fields_ = [("fl", ctypes.c_void_p), ("base", ctypes.c_uint32), ("live", ctypes.c_uint16),
                ("dead", ctypes.c_uint16), ("free", ctypes.c_uint16), ("max_id", ctypes.c_uint16),
                ("wseq", ctypes.c_uint32), ("spare", ctypes.c_uint8), ("gcs", ctypes.c_uint8),
                ("repairs", ctypes.c_uint8)]


@pytest.fixture(scope="module")
def lib(tmp_path_factory):
    if not shutil.which("cc"):
        pytest.skip("needs a C compiler")
    so = str(tmp_path_factory.mktemp("rt") / "librt.so")
    subprocess.run(["cc", "-O1", "-g", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC", "-o", so,
                    os.path.join(K6, "rt.c"), os.path.join(K6, "rt_host.c")], check=True)
    L = ctypes.CDLL(so)
    L.rth_flash.restype = ctypes.c_void_p
    L.rth_mem.restype = ctypes.POINTER(ctypes.c_uint8)
    L.rt_mount.argtypes = [ctypes.POINTER(RT), ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int]
    L.rt_find.argtypes = [ctypes.POINTER(RT), ctypes.c_uint16, ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(Rec)]
    L.rt_next.argtypes = [ctypes.POINTER(RT), ctypes.c_int, ctypes.POINTER(Rec)]
    L.rt_put.argtypes = [ctypes.POINTER(RT), ctypes.POINTER(Rec)]
    L.rt_delete.argtypes = [ctypes.POINTER(RT), ctypes.c_uint16]
    L.rt_cw_check.argtypes = [ctypes.POINTER(RT), ctypes.c_uint32, ctypes.c_uint32, ctypes.c_char_p,
                              ctypes.POINTER(Rec)]
    L.rt_cw_apply.argtypes = [ctypes.POINTER(RT), ctypes.c_uint32, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint8,
                              ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.POINTER(Rec)]
    L.rt_heard_apply.argtypes = [ctypes.POINTER(RT), ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint16,
                                 ctypes.c_int, ctypes.POINTER(Rec)]
    L.rt_cw_byte.restype = ctypes.c_uint8
    L.rt_crc.restype = ctypes.c_uint32
    L.rt_crc.argtypes = [ctypes.POINTER(Rec)]
    assert L.rth_sizeof_rec() == 64 == ctypes.sizeof(Rec)
    assert L.rth_sizeof_t() == ctypes.sizeof(RT)
    return L


class Table:
    def __init__(self, L, fill=0xFF):
        self.L = L
        L.rth_reset(fill)
        self.t = RT()

    def mount(self, force=0):
        return self.L.rt_mount(ctypes.byref(self.t), self.L.rth_flash(), BASE, force)

    def put(self, rx, name=b"", call=b"", id=0, **kw):
        r = Rec(rx_hz=rx, name=name, call=call, id=id, **kw)
        s = self.L.rt_put(ctypes.byref(self.t), ctypes.byref(r))
        return s, r

    def find(self, id=0, rx=0, tol=0):
        r = Rec()
        s = self.L.rt_find(ctypes.byref(self.t), id, rx, tol, ctypes.byref(r))
        return (s, r) if s >= 0 else (s, None)

    def all(self):
        out, s, r = [], -1, Rec()
        while True:
            s = self.L.rt_next(ctypes.byref(self.t), s, ctypes.byref(r))
            if s < 0:
                return out
            out.append((r.id, r.rx_hz, r.name, r.call))

    def mem(self):
        return bytes(self.L.rth_mem()[:16 * 4096])


def test_builds_for_the_m0_without_library_calls(tmp_path):
    if not (shutil.which("clang") and shutil.which("llvm-nm")):
        pytest.skip("needs clang")
    o = str(tmp_path / "rt.o")
    subprocess.run(["clang", "--target=thumbv6m-none-eabi", "-mcpu=cortex-m0", "-mthumb", "-Oz", "-ffreestanding",
                    "-fno-builtin", "-nostdlib", "-Wall", "-Wextra", "-Werror", "-c", os.path.join(K6, "rt.c"),
                    "-o", o], check=True)
    und = subprocess.run(["llvm-nm", "-u", o], capture_output=True, text=True, check=True).stdout.split()
    assert und == [], und
    size = subprocess.run(["llvm-size", o], capture_output=True, text=True, check=True).stdout.split("\n")[1]
    assert int(size.split()[0]) < 3200


@need_cc
def test_put_find_edit_delete(lib):
    t = Table(lib)
    assert t.mount() == 0 and t.t.spare == 15 and t.t.free == 15 * 64
    s, r = t.put(146_940_000, b"Lynden", tx_off=-600_000, rx_tone=1035)
    assert s == 0 and r.id == 1 and r.seq == 1 and r.state == 0xFFFC
    s2, r2 = t.put(147_160_000, b"Bellingham", tx_off=600_000)
    assert r2.id == 2
    assert t.find(rx=146_942_000, tol=5000)[1].name == b"Lynden"
    assert t.find(rx=146_960_000, tol=5000)[0] < 0
    # an edit is a new version; the old one is marked, nothing erased
    s3, r3 = t.put(146_940_000, b"Lynden Hill", id=1, tx_off=-600_000)
    assert r3.seq == 2 and s3 == 2
    assert t.find(id=1)[1].name == b"Lynden Hill"
    assert lib.rth_erases() == 0 and t.t.live == 2 and t.t.dead == 1
    assert [x[0] for x in t.all()] == [2, 1]
    assert lib.rt_delete(ctypes.byref(t.t), 2) == 0 and t.all() == [(1, 146_940_000, b"Lynden Hill", b"")]
    # it all survives a remount
    t2 = RT()
    assert lib.rt_mount(ctypes.byref(t2), lib.rth_flash(), BASE, 0) == 1 and t2.max_id == 2
    assert t2.dead == 2 and t2.repairs == 0


@need_cc
def test_region_with_other_data_is_refused_unless_forced(lib):
    t = Table(lib, fill=0x00)
    assert t.mount() == -5                      # RT_ERR_NOT_BLANK: never write over it
    assert lib.rth_writes() == 0 and lib.rth_erases() == 0
    assert t.mount(force=1) == 0                # forced: a sector is erased for the spare
    assert t.t.spare != 0xFF
    s, r = t.put(146_520_000, b"call")
    assert s >= 0 or s == -2                    # all other sectors are dead: put compacts first
    assert t.find(rx=146_520_000)[1].name == b"call"


@need_cc
def test_many_edits_compact_and_stay_within_the_region(lib):
    t = Table(lib)
    t.mount()
    rnd = random.Random(7)
    want = {}
    for i in range(300):                         # 300 entries, then 3000 edits: ~3 compactions a sector
        s, r = t.put(144_000_000 + i * 5000, f"r{i}".encode())
        assert s >= 0
        want[r.id] = f"r{i}".encode()
    for k in range(3000):
        i = rnd.choice(list(want))
        nm = f"e{k}".encode()
        s, r = t.put(144_000_000 + (i - 1) * 5000, nm, id=i)
        assert s >= 0, (k, s)
        want[i] = nm
    assert t.t.gcs > 30 and t.t.live == 300
    got = {x[0]: x[2] for x in t.all()}
    assert got == want
    assert lib.rth_erases() == t.t.gcs           # one erase a compaction, no more
    # every write landed in the region, and a remount sees the same table
    t2 = Table.__new__(Table)
    t2.L, t2.t = lib, RT()
    assert t2.mount() == 300 and {x[0]: x[2] for x in t2.all()} == want


@need_cc
def test_full_table(lib):
    t = Table(lib)
    t.mount()
    n = 0
    while True:
        s, r = t.put(144_000_000 + n * 1000, b"x")
        if s < 0:
            break
        n += 1
    assert s == -2 and n == 15 * 64             # every slot but the spare sector's
    assert lib.rt_delete(ctypes.byref(t.t), 5) == 0
    s, r = t.put(150_000_000, b"after a delete")
    assert s >= 0 and t.t.gcs == 1


def fresh(lib, setup):
    t = Table(lib)
    t.mount()
    setup(t)
    return t


def power_cut_case(lib, setup, action, check):
    """cut the power at each flash operation of action in turn (that write half done, that
    erase half done, everything after it failing), bring it back, remount, check. Returns how
    many operations the action takes."""
    t = fresh(lib, setup)
    before = lib.rth_ops()
    action(t)
    total = lib.rth_ops() - before
    for n in range(1, total + 1):
        t = fresh(lib, setup)
        lib.rth_cut(n)
        action(t)
        lib.rth_cut(-1)
        t2 = Table.__new__(Table)
        t2.L, t2.t = lib, RT()
        assert t2.mount() >= 0, n
        check(t2, n)
    return total


@need_cc
def test_power_cut_during_an_edit(lib):
    def setup(t):
        t.put(146_940_000, b"old")
        t.put(147_160_000, b"other")

    def action(t):
        t.put(146_940_000, b"new", id=1)

    def check(t, n):
        es = t.all()
        assert sorted(x[0] for x in es) == [1, 2], (n, es)
        assert dict((x[0], x[2]) for x in es)[1] in (b"old", b"new"), n
        assert t.t.live == 2
        # and the table still takes edits
        s, r = t.put(146_940_000, b"later", id=1)
        assert s >= 0 and t.find(id=1)[1].name == b"later"
    assert power_cut_case(lib, setup, action, check) >= 4


@need_cc
def test_power_cut_during_a_compaction(lib):
    def setup(t):
        for i in range(15 * 64 - 10):           # fill every sector but the spare ...
            t.put(144_000_000 + i * 1000, b"a%d" % i)
        for i in range(1, 30):                  # ... then make sector 0 mostly dead
            lib.rt_delete(ctypes.byref(t.t), i)
        for i in range(10):
            t.put(160_000_000 + i * 1000, b"fill")

    def action(t):
        t.put(170_000_000, b"needs room")       # compacts sector 0 into the spare first

    want_old = None

    def check(t, n):
        nonlocal want_old
        es = {x[0]: x[2] for x in t.all()}
        if want_old is None:
            want_old = {k: v for k, v in es.items() if v != b"needs room"}
        old = {k: v for k, v in es.items() if v != b"needs room"}
        assert old == want_old, n                # no entry lost, none doubled
        assert len(es) - len(old) in (0, 1)
        assert t.t.spare != 0xFF
        s, r = t.put(171_000_000, b"after")
        assert s >= 0, (n, s)
    assert power_cut_case(lib, setup, action, check) > 60


@need_cc
def test_cw_id_policy(lib):
    t = Table(lib)
    t.mount()
    t.put(146_940_000, b"Lynden")
    t.put(147_160_000, b"Bham", call=b"W7ABC", flags=0x10)
    L, T = lib, ctypes.byref(t.t)
    r = Rec()
    w0 = lib.rth_writes()
    # no saved entry there: nothing written
    assert L.rt_cw_apply(T, 145_000_000, 5000, b"K7XYZ", 4, 800, 20, 0, ctypes.byref(r)) == 0
    # blank call: filled by itself, with the tone and speed
    assert L.rt_cw_apply(T, 146_941_000, 5000, b"WA7ABC/R", 4, 800, 20, 0, ctypes.byref(r)) == 2
    s, e = t.find(rx=146_940_000, tol=5000)
    assert e.call == b"WA7ABC/R" and e.call_flags == 4 | 8 and e.cw == (6 | 6 << 4) and e.name == b"Lynden"
    w1 = lib.rth_writes()
    assert w1 > w0
    # the same call again: no write
    assert L.rt_cw_apply(T, 146_940_000, 5000, b"WA7ABC/R", 4, 800, 20, 0, ctypes.byref(r)) == 1
    assert lib.rth_writes() == w1
    # a typed call that differs: ask, no write; written only when confirmed
    assert L.rt_cw_check(T, 147_160_000, 5000, b"W7XYZ", ctypes.byref(r)) == 3
    assert L.rt_cw_apply(T, 147_160_000, 5000, b"W7XYZ", 0, 600, 13, 0, ctypes.byref(r)) == 3
    assert lib.rth_writes() == w1 and t.find(rx=147_160_000)[1].call == b"W7ABC"
    assert L.rt_cw_apply(T, 147_160_000, 5000, b"W7XYZ", 0, 600, 13, 1, ctypes.byref(r)) == 4
    e = t.find(rx=147_160_000)[1]
    assert e.call == b"W7XYZ" and not (e.flags & 0x10) and e.cw == (3 | 4 << 4)
    # a 10-character call fits; byte 51's packing
    assert L.rt_cw_apply(T, 146_940_000, 5000, b"VE9QRP/RPT", 4, 1200, 25, 1, ctypes.byref(r)) == 4
    assert t.find(rx=146_940_000)[1].call == b"VE9QRP/RPT"
    assert [L.rt_cw_byte(f, w) for f, w in ((600, 13), (1350, 35), (800, 1), (2000, 99))] == \
        [0x43, 0xBF, 0x16, 0xFF]


def test_heard_tone_and_dmr_policy(lib):
    """a listen's CTCSS tone fills a blank one (and a blank TX tone), never overwrites a stored
    one; DMR sets its flag; nothing new heard writes nothing"""
    t = Table(lib)
    t.mount()
    t.put(440_925_000, b"DMR rptr")
    L, T = lib, ctypes.byref(t.t)
    r = Rec()
    assert L.rt_heard_apply(T, 441_500_000, 5000, 1000, 1, ctypes.byref(r)) == 0     # no entry
    w0 = lib.rth_writes()
    assert L.rt_heard_apply(T, 440_925_000, 5000, 0, 0, ctypes.byref(r)) == 1        # nothing heard
    assert lib.rth_writes() == w0
    assert L.rt_heard_apply(T, 440_925_000, 5000, 1000, 1, ctypes.byref(r)) == 2
    e = t.find(rx=440_925_000)[1]
    assert e.rx_tone == e.tx_tone == 1000 and e.flags & 0x20 and e.name == b"DMR rptr"
    w1 = lib.rth_writes()
    assert w1 > w0
    assert L.rt_heard_apply(T, 440_925_000, 5000, 1000, 1, ctypes.byref(r)) == 1     # same again
    assert lib.rth_writes() == w1
    assert L.rt_heard_apply(T, 440_925_000, 5000, 1413, 0, ctypes.byref(r)) == 3     # differs: kept
    assert lib.rth_writes() == w1 and t.find(rx=440_925_000)[1].rx_tone == 1000


# ---- rtls (c/k6/mod/rtls.c): the table read by a RAM module through API level 5 ----

def test_rtls_module_lists_the_table_on_an_emulated_m0(lib):
    if not (shutil.which("clang") and shutil.which("ld.lld")):
        pytest.skip("needs clang and ld.lld")
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from modem import k6mod
    from test_k6_i2s import START, run
    src = os.path.join(K6, "mod", "rtls.c")
    code, entry = k6mod.build(src)
    assert k6mod.source_level(src) == 5 and 16 + len(code) + 8 <= 4096, len(code)
    # an empty (erased) region: an empty table, nothing written
    fl = {}
    ret, said, _, _ = run(START, src=src, level=5, flash=fl, code=(code, entry), spare=4072 - len(code))
    assert ret == 0 and said == ["RT live=0 dead=0 free=960 spare=15 repairs=0 r=0"], said
    assert fl["progs"] == fl["erases"] == fl["refused"] == 0
    # a table the host library wrote: the module reads the same entries
    t = Table(lib)
    t.mount()
    t.put(146_940_000, b"Lynden", call=b"W7ABC/R")
    t.put(443_100_000, b"")
    mem = bytearray(b"\xff" * (0x200000 - BASE))
    mem[:16 * 4096] = t.mem()
    fl = {"mem": mem}
    ret, said, _, _ = run(START, src=src, level=5, flash=fl, code=(code, entry), spare=4072 - len(code))
    assert ret == 2 and said[0].startswith("RT live=2 dead=0") and said[0].endswith("r=2"), said
    assert said[1:] == ["RT 1 146940000 0 T0 W7ABC/R - -200 Lynden", "RT 2 443100000 0 T0 - - -200"], said
    assert fl["progs"] == fl["erases"] == 0
    # an image below level 5 is refused before any flash call
    ret, said, _, _ = run(START, src=src, level=4, flash={}, code=(code, entry))
    assert ret == -3 and said == []
    # a region that holds something else is left alone
    mem = bytearray(b"\xff" * (0x200000 - BASE))
    mem[100:104] = b"AURA"
    fl = {"mem": mem}
    ret, said, _, _ = run(START, src=src, level=5, flash=fl, code=(code, entry), spare=4072 - len(code))
    assert ret == -5 and fl["progs"] == fl["erases"] == 0
