"""K6 CW ID decoder (c/k6/cwid.c) and the CW probe module (c/k6/mod/cwprobe.c).

The decoder is built for this machine and fed keying made from text at many speeds, with a
detector lag, jitter and odd poll periods; the level tracker gets a modelled audio-level
register (attack and release lag, noise, 1 dB steps); cw_call picks callsigns out of text.
Then the probe module runs on an emulated Cortex-M0 (unicorn) against a modelled FD6818 that
sends two IDs, and must decode both on both level registers, inside the stack and store it
has on the radio. Needs a C compiler; the module test also clang, ld.lld and unicorn.
SPDX-License-Identifier: MIT"""
import ctypes
import math
import os
import random
import shutil
import subprocess
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
K6 = os.path.join(ROOT, "c", "k6")
PROBE = os.path.join(K6, "mod", "cwprobe.c")
LISTEN = os.path.join(K6, "mod", "cwlisten.c")
sys.path.insert(0, ROOT)
from modem import k6mod  # noqa: E402

need_cc = pytest.mark.skipif(not shutil.which("cc"), reason="needs a C compiler")
need_clang = pytest.mark.skipif(not (shutil.which("clang") and shutil.which("ld.lld")),
                                reason="needs clang and ld.lld")

MORSE = {"A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.", "G": "--.",
         "H": "....", "I": "..", "J": ".---", "K": "-.-", "L": ".-..", "M": "--", "N": "-.",
         "O": "---", "P": ".--.", "Q": "--.-", "R": ".-.", "S": "...", "T": "-", "U": "..-",
         "V": "...-", "W": ".--", "X": "-..-", "Y": "-.--", "Z": "--..", "0": "-----",
         "1": ".----", "2": "..---", "3": "...--", "4": "....-", "5": ".....", "6": "-....",
         "7": "--...", "8": "---..", "9": "----.", "/": "-..-."}
NDUR = 96          # cwid.h defaults
TEXT = 40
LOOK = 16


class CW(ctypes.Structure):
    _fields_ = [("edge_t", ctypes.c_uint32), ("cand_t", ctypes.c_uint32), ("key", ctypes.c_uint8),
                ("cand", ctypes.c_uint8), ("active", ctypes.c_uint8), ("why", ctypes.c_uint8),
                ("n", ctypes.c_uint16), ("nd", ctypes.c_uint16), ("unit", ctypes.c_uint16),
                ("lag", ctypes.c_int16), ("wpm", ctypes.c_uint8), ("tlen", ctypes.c_uint8),
                ("text", ctypes.c_char * TEXT), ("d", ctypes.c_uint8 * NDUR)]


class LV(ctypes.Structure):
    _fields_ = [("lo", ctypes.c_int32), ("hi", ctypes.c_int32), ("dev", ctypes.c_int32),
                ("sm", ctypes.c_int32), ("ring", ctypes.c_int16 * LOOK), ("ri", ctypes.c_uint8),
                ("fill", ctypes.c_uint8), ("on", ctypes.c_uint8), ("init", ctypes.c_uint8)]


@pytest.fixture(scope="module")
def lib():
    if not shutil.which("cc"):
        pytest.skip("needs a C compiler")
    d = tempfile.mkdtemp()
    so = os.path.join(d, "libcwid.so")
    subprocess.run(["cc", "-O1", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC",
                    os.path.join(K6, "cwid.c"), "-o", so], check=True)
    L = ctypes.CDLL(so)
    L.cw_feed.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int]
    L.cw_lvl.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32]
    L.cw_db.argtypes = [ctypes.c_uint32]
    L.cw_div.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    L.cw_div.restype = ctypes.c_uint32
    return L


def keying(text, wpm, lag=0.0, jit_ms=0.0, jit_rel=0.0, fw=None, rnd=random):
    """[(key, ms)] for text: dot 1200/wpm ms; fw = Farnsworth character spacing speed."""
    u = 1200 / wpm
    uf = 1200 / fw if fw else u

    def j(x):
        return max(1.0, x * (1 + rnd.gauss(0, jit_rel)) + rnd.uniform(-jit_ms, jit_ms))
    ev = []
    for w in text.split(" "):
        for li, ch in enumerate(w):
            code = MORSE[ch]
            for ei, s in enumerate(code):
                ev.append((1, j((1 if s == "." else 3) * u + lag)))
                if ei < len(code) - 1:
                    ev.append((0, j(u - lag)))
            if li < len(w) - 1:
                ev.append((0, j(3 * uf - lag)))
        ev.append((0, j(7 * uf - lag)))
    return ev


def feed(L, ev, period=2.0, lead=500.0, tail=3000.0, t0=1000):
    c = CW()
    L.cw_init(ctypes.byref(c))
    out, t = [], 0.0
    for st, d in [(0, lead)] + ev + [(0, tail)]:
        end = t + d
        while t < end:
            if L.cw_feed(ctypes.byref(c), t0 + int(t), st):
                out.append((c.text.decode(), c.wpm))
            t += period
    return out


def call_of(L, text):
    buf, fl = ctypes.create_string_buffer(11), ctypes.c_uint8()
    n = L.cw_call(text.encode(), buf, ctypes.byref(fl))
    return buf.value.decode() if n else None, fl.value


def test_div_utoa_db(lib):
    rnd = random.Random(1)
    for _ in range(2000):
        n, d = rnd.getrandbits(32), rnd.getrandbits(rnd.randint(1, 32)) or 1
        assert lib.cw_div(n, d) == n // d
    buf = ctypes.create_string_buffer(16)
    for v in (0, 7, 10, 99, 1200, 4294967295):
        end = lib.cw_utoa(buf, v)
        assert buf.value.decode() == str(v) and end
    for v in list(range(2, 70000, 7)) + [1 << 20, 123456789, (1 << 32) - 1]:
        assert abs(lib.cw_db(v) - 20 * math.log10(v)) < 0.7
    assert lib.cw_db(0) == 0 and lib.cw_db(1) == 0


TEXTS = ["AG7EW SOTTO TEST", "WA7ABC/R", "VE9QRP/RPT", "KH6/AG7EW/P", "DE W1AW", "N7XYZ R",
         "GB100RSGB"]


def test_any_speed_and_lag(lib):
    """10-35 wpm, detector lag -10..+20 ms, +-6 ms and 5 % jitter, 2-5 ms polls: at least 98 %
    decode exactly; the misses are all at the fastest speeds with the most lag (the gaps in a
    letter are then under the glitch filter)."""
    rnd = random.Random(2)
    tot = bad = 0
    for wpm in (10, 13, 15, 18, 20, 22, 25, 28, 30, 35):
        for lag in (-10, 0, 10, 20):
            for text in TEXTS:
                for _ in range(3):
                    ev = keying(text, wpm, lag, 6, 0.05, rnd=rnd)
                    o = feed(lib, ev, period=rnd.choice([2, 3, 4, 5]), lead=rnd.randint(100, 900))
                    tot += 1
                    if [t for t, _ in o] != [text]:
                        bad += 1
                        assert wpm >= 28 and lag >= 20, (wpm, lag, text, o)
    assert bad <= tot // 50
    for wpm in (13, 20, 30):
        assert feed(lib, keying("AG7EW SOTTO TEST", wpm)) == [("AG7EW SOTTO TEST", wpm)]


def test_farnsworth_and_glitches(lib):
    assert [t for t, _ in feed(lib, keying("WA7ABC/R", 18, fw=8))] == ["WA7ABC/R"]
    assert [t for t, _ in feed(lib, keying("VE9QRP RPT", 25, fw=12))] == ["VE9QRP RPT"]
    rnd = random.Random(3)
    c = CW()
    lib.cw_init(ctypes.byref(c))
    out, t = [], 0
    for st, d in [(0, 400)] + keying("AG7EW SOTTO TEST", 20) + [(0, 3000)]:
        for _ in range(int(d / 2)):
            k = 1 - st if rnd.random() < 0.01 else st      # 1 % of polls flipped
            if lib.cw_feed(ctypes.byref(c), 5000 + t, k):
                out.append(c.text.decode())
            t += 2
    assert out == ["AG7EW SOTTO TEST"]


def test_ends_and_long_marks(lib):
    """Two IDs 3 s apart are two transmissions; a 2 s tone is flagged CW_LONG."""
    ev = keying("AG7EW", 20) + [(0, 3000)] + keying("AG7EW", 20)
    assert [t for t, _ in feed(lib, ev)] == ["AG7EW", "AG7EW"]
    c = CW()
    lib.cw_init(ctypes.byref(c))
    whys = []
    for i in range(3000):
        if lib.cw_feed(ctypes.byref(c), i * 2, 1 if 100 < i < 1100 else 0):
            whys.append(c.why)
    assert whys == [2]


def test_squelch_tails_dropped(lib):
    """A long mark well apart from the ID at either end (the IC-7100's squelch tail, bench
    2026-11-08 08:21Z: +596 ms a second after an 800 Hz ID gave '#T' at 5 wpm) isn't Morse;
    a real last dash after a word gap stays."""
    ev = keying("AG7EW TEST", 20)
    assert feed(lib, ev[:-1] + [(0, 1020), (1, 596)]) == [("AG7EW TEST", 20)]
    assert feed(lib, [(1, 600), (0, 900)] + ev) == [("AG7EW TEST", 20)]
    assert feed(lib, keying("AG7EW T", 20)) == [("AG7EW T", 20)]
    assert [t for t, _ in feed(lib, ev[:-1] + [(0, 1020), (1, 300)])] != ["AG7EW TEST"]


def test_callsigns(lib):
    cases = {"AG7EW SOTTO TEST": ("AG7EW", 0), "DE WA7ABC/R": ("WA7ABC/R", 4),
             "VE9QRP/RPT": ("VE9QRP/RPT", 4), "KH6/AG7EW/P": ("AG7EW", 1), "N7XYZ R": ("N7XYZ", 4),
             "GB100RSGB": ("GB100RSGB", 0), "3DA0ABC": ("3DA0ABC", 0),
             "QRZ DE K1ABC/M": ("K1ABC/M", 2), "W1AW/QRP": ("W1AW/QRP", 1),
             "TEST 5NN": (None, 0), "SOTTO": (None, 0), "EE7": (None, 0), "73": (None, 0),
             "": (None, 0)}
    for text, want in cases.items():
        assert call_of(lib, text) == want, text


def level_feed(L, ev, floor, top, noise, tau_a, tau_r, rnd, upd=4.5, period=2, lead=3000):
    """The keying through a modelled level register (dB, 1 dB steps, updated every upd ms)."""
    c, lv = CW(), LV()
    L.cw_init(ctypes.byref(c))
    L.cw_lvl_init(ctypes.byref(lv))
    edges, t = [], 0.0
    for st, d in [(0, lead)] + ev + [(0, 3000)]:
        edges.append((t, st))
        t += d
    end, out = t, []
    env, t, i, reg, nxt = float(floor), 0.0, 0, floor, 0.0
    while t < end:
        while i + 1 < len(edges) and edges[i + 1][0] <= t:
            i += 1
        k = edges[i][1]
        env += ((top if k else floor) - env) * (1 - math.exp(-0.5 / (tau_a if k else tau_r)))
        if t >= nxt:
            reg = min(63, max(0, int(round(env + rnd.gauss(0, noise)))))
            nxt += upd
        if int(t * 2) % (period * 2) == 0:
            if L.cw_feed(ctypes.byref(c), 5000 + int(t), L.cw_lvl(ctypes.byref(lv), reg, period)):
                out.append(c.text.decode())
        t += 0.5
    return out


def test_level_tracker(lib):
    """A clean ID (25-40 dB above the gaps, noise 2-4 dB, detector lag up to 5/15 ms) gives the
    callsign at 13-30 wpm; noise before it may make junk transmissions but no callsign."""
    rnd = random.Random(3)
    good = tot = 0
    for wpm in (13, 20, 25, 30):
        for floor, top, noise in ((20, 45, 2.0), (10, 50, 4.0)):
            for ta, tr in ((2, 5), (5, 15)):
                out = level_feed(lib, keying("AG7EW SOTTO TEST", wpm), floor, top, noise, ta, tr, rnd)
                calls = [c for c in (call_of(lib, t)[0] for t in out) if c]
                tot += 1
                good += calls == ["AG7EW"]
    assert good >= tot - 2, (good, tot)
    # noise alone, 30 s of it: nothing that reads as a callsign
    out = level_feed(lib, [], 20, 20, 3.0, 2, 5, rnd, lead=30000)
    assert not [t for t in out if call_of(lib, t)[0]]


# ---- the probe module on an emulated M0 ----

@need_clang
def test_probe_builds_fits_level_2():
    code, entry = k6mod.build(PROBE)
    assert entry == 0 and k6mod.source_level(PROBE) == 2
    assert 16 + len(code) + 8 <= 4096, len(code)          # the K6's 4 KB message store


def run_m0(code, entry, fd_read, key_at_ms, spare, max_ms=None):
    """Run a module on an emulated Cortex-M0 with an API level 2 table: fd_read(reg, ms) models
    the FD6818, a key goes down at key_at_ms, spare bytes of scratch RAM. Returns
    (ret, said, writes, stack bytes, scratch address)."""
    unicorn = pytest.importorskip("unicorn")
    from unicorn import arm_const as A
    st = {"us": 0, "said": [], "writes": {}}
    mu = unicorn.Uc(unicorn.UC_ARCH_ARM, unicorn.UC_MODE_THUMB | unicorn.UC_MODE_MCLASS)
    mu.ctl_set_cpu_model(A.UC_CPU_ARM_CORTEX_M0)
    ram = 0x20000000
    mu.mem_map(ram, 1 << 20)
    base, api, stubs, stop, sp0 = ram + 0x1000, ram + 0x8000, ram + 0x9000, ram + 0x9F00, ram + 0x7F00
    mu.mem_write(base, code)
    names = ["say", "ticks", "fd_read", "fd_write", "delay_us", "tune", "rx_hz", "ms", "key_down", "scratch"]
    for i in range(len(names)):
        mu.mem_write(stubs + 16 * i, b"\x70\x47")
    mu.mem_write(stop, b"\x00\xbf" * 8)
    mu.mem_write(api, (1).to_bytes(2, "little") + (2).to_bytes(2, "little")
                 + b"".join((stubs + 16 * i | 1).to_bytes(4, "little") for i in range(len(names))))
    low = [sp0]

    def on_write(uc, access, addr, size, value, _):
        if addr < low[0]:
            low[0] = addr
    mu.hook_add(unicorn.UC_HOOK_MEM_WRITE, on_write, begin=ram + 0x7000, end=sp0)

    def on_stub(uc, addr, size, _):
        name = names[(addr - stubs) // 16]
        r0, r1 = uc.reg_read(A.UC_ARM_REG_R0), uc.reg_read(A.UC_ARM_REG_R1)
        ret = 0
        if name == "say":
            st["said"].append(bytes(uc.mem_read(r0, 64)).split(b"\0")[0].decode())
            st["us"] += 5000                         # 60 characters at 115200 baud
        elif name == "fd_read":
            st["us"] += 20
            ret = fd_read(r0, st["us"] / 1000.0)
        elif name == "fd_write":
            st["writes"][r0] = r1
        elif name == "delay_us":
            st["us"] += r0
        elif name == "rx_hz":
            ret = 440_850_000
        elif name == "ms":
            ret = st["us"] // 1000
        elif name == "key_down":
            ret = 1 if st["us"] >= key_at_ms * 1000 else 0
        elif name == "scratch":
            ret = ram + 0x6000
            uc.mem_write(r0, spare.to_bytes(4, "little"))
        uc.reg_write(A.UC_ARM_REG_R0, ret & 0xFFFFFFFF)
    mu.hook_add(unicorn.UC_HOOK_CODE, on_stub, begin=stubs, end=stubs + 16 * len(names))
    mu.reg_write(A.UC_ARM_REG_R0, api)
    mu.reg_write(A.UC_ARM_REG_SP, sp0)
    mu.reg_write(A.UC_ARM_REG_LR, stop | 1)
    mu.emu_start(base + entry | 1, stop, count=400_000_000)
    return mu.reg_read(A.UC_ARM_REG_R0), st["said"], st["writes"], sp0 - low[0], ram + 0x6000


def edges_of(ids, rnd, lag=0.0):
    """[(ms, key)] for [(start ms, text, wpm)], each mark lag ms late at its end."""
    edges = []
    for start, text, wpm in ids:
        t = start
        for k, d in keying(text, wpm, lag=lag, jit_ms=1, rnd=rnd):
            edges.append((t, k))
            t += d
    return edges


def key_at(edges, ms):
    k = 0
    for e_t, e_k in edges:
        if e_t > ms:
            break
        k = e_k
    return k


@need_clang
@pytest.mark.parametrize("scratch", [True, False])
def test_probe_decodes_two_ids_on_m0(scratch):
    """The module against a modelled FD6818: an 800 Hz MCW ID at 20 wpm at 2 s and at 13 s.
    REG_6F follows the tone in dB (12 dB gaps, 40 dB key down, attack 3 ms, release 8 ms, noise),
    REG_64 the same in linear amplitude, REG_0B shows the tone detector's symbol 4 (800 Hz)
    with the ready bit while the tone is on. A key press at 25 s ends it. The two registers
    take turns: A must give the first ID and B the second. With scratch RAM the stack stays under
    520 bytes; without it (the K6 gives 4096 - 16 - code - 8 bytes, about 200, which may be
    too few) the decoder goes on the stack, under 760 (the K6 had 3.5 KB on 612fb72)."""
    code, entry = k6mod.build(PROBE)
    rnd = random.Random(4)
    text = "AG7EW SOTTO TEST"
    edges = edges_of([(2000.0, text, 20), (13000.0, text, 20)], rnd)   # an ID is 8.6 s long
    env = {"v": 12.0, "last": 0.0}

    def fd_read(reg, ms):
        dt = ms - env["last"]
        if dt > 0:
            k = key_at(edges, ms)
            tau = 3.0 if k else 8.0
            env["v"] += ((40.0 if k else 12.0) - env["v"]) * (1 - math.exp(-dt / tau))
            env["last"] = ms
        e = env["v"]
        if reg == 0x6F:
            return max(0, min(127, int(round(e + rnd.gauss(0, 1.0)))))
        if reg == 0x64:
            return int(10 ** ((e + rnd.gauss(0, 1.0)) / 20) * 50)
        if reg == 0x0B:
            return 0x1400 if key_at(edges, ms) else 0
        if reg == 0x67:
            return (-60 + 174) << 1
        return 0
    spare = 1024 if scratch else 0
    ret, said, writes, stack, ram_w = run_m0(code, entry, fd_read, 25_000, spare)
    assert said[0].startswith("cwprobe sp=0x") and said[1].startswith("cwprobe rx=440850000 th=24 ")
    texts = [s for s in said if s[:2] in ("A ", "B ") and "wpm" not in s and " d " not in s]
    assert texts == ["A " + text, "B " + text]          # one ID on each register, by turns
    assert all(s.startswith(("A 20wpm", "A 19wpm", "A 21wpm")) for s in said if s.startswith("A ") and "wpm" in s)
    assert ret == 2 and said[-1] == "cwprobe end key msgs=2"
    assert any(s.startswith("T ") and "b=1400" in s for s in said)
    assert any(s.startswith("R 6f ") for s in said)
    assert all(len(s) <= 60 for s in said)
    assert writes[0x24] & 0x10 == 0 and writes[0x3F] == 0x0800
    print(f"cwprobe stack {stack} B, code {len(code)} B, scratch {spare} B")
    assert stack < (520 if scratch else 760), stack
    w = int(said[0].split(" w=0x")[1].split()[0], 16)
    assert (w == ram_w) == scratch


# ---- the listener module: the tone detector as the probe run found it ----

def tone_reg(edges, idx, lag_on=10.0, lag_off=20.0, junk=()):
    """REG_0B as on air: 0xC006, bit 12 while a programmed tone is present (lag_on ms after the
    key goes down, lag_off after it goes up), [11:8] the tone. junk: (ms, len, idx) blips.
    The default makes every mark 10 ms longer and every space 10 ms shorter, as the real
    detector did on air (tests/test_k6_air.py checks it against the probe's raw edges)."""
    def f(reg, ms):
        if reg != 0x0B:
            return 0x0200 if reg == 0x0C else 0
        for t, n, i in junk:
            if t <= ms < t + n:
                return 0xD006 | (i << 8)
        a, b = key_at(edges, ms - lag_on), key_at(edges, ms - lag_off)
        on = (a or b) if lag_off >= lag_on else (a and b)
        return (0xD006 | (idx << 8)) if on else (0xC006 | (idx << 8))
    return f


@need_clang
def test_listener_builds_fits_level_2():
    code, entry = k6mod.build(LISTEN)
    assert entry == 0 and k6mod.source_level(LISTEN) == 2
    assert 16 + len(code) + 8 <= 4096, len(code)


@need_clang
@pytest.mark.parametrize("wpm,idx,scratch", [(20, 4, True), (13, 0, False), (25, 12, False)])
def test_listener_confirms_call_on_m0(wpm, idx, scratch):
    """Short bursts on tone 15 at the start, as a module upload's FFSK gave on air (1-7 marks of
    16-50 ms, a second or two apart), then the ID twice: the bursts are only counted, the second
    ID confirms the call, with the tone and speed, and the module returns 1."""
    code, entry = k6mod.build(LISTEN)
    rnd = random.Random(wpm)
    text = "AG7EW SOTTO TEST"
    n = 129 * 1200 / wpm                               # the ID is 129 dots long
    edges = edges_of([(10000.0, text, wpm), (10000.0 + n + 4000, text, wpm)], rnd)
    junk = [(200.0 + 1900 * i + 60 * j, 24 + 4 * j, 15) for i in range(5) for j in range(1 + 3 * (i % 3))]
    ret, said, writes, stack, _ = run_m0(code, entry, tone_reg(edges, idx, junk=junk), 60_000,
                                         1024 if scratch else 0)
    f = 600 + 50 * idx
    assert said[0] == "CWID listen rx=440850000 th=24 s=1500"
    assert said.count("CWID text " + text) == 2
    assert "CWID call=AG7EW fl=0 seen=1" in said and "CWID call=AG7EW fl=0 seen=2" in said
    ok = [s for s in said if s.startswith("CWID ok ")]
    assert len(ok) == 1 and ok[0].startswith(f"CWID ok AG7EW fl=0 rx=440850000 tone={f} wpm=")
    assert abs(int(ok[0].split("wpm=")[1]) - wpm) <= 1
    head = [s for s in said if s.startswith("CWID ") and "wpm lag=" in s]
    assert all(f"tone={f} q=100 " in s for s in head[-2:]), head
    assert ret == 1 and said[-1] == "CWID end ok msgs=2 short=5"
    assert all(len(s) <= 60 for s in said)
    assert writes[0x24] & 0x10 == 0 and writes[0x3F] == 0x0800
    print(f"cwlisten stack {stack} B, code {len(code)} B")
    assert stack < (400 if scratch else 600), stack


@need_clang
def test_listener_needs_two_identical():
    """Two different calls, then a key press: nothing confirmed, returns 0."""
    code, entry = k6mod.build(LISTEN)
    rnd = random.Random(7)
    edges = edges_of([(1000.0, "AG7EW", 20), (7000.0, "VE9QRP", 20)], rnd)
    ret, said, _, _, _ = run_m0(code, entry, tone_reg(edges, 4), 12_000, 1024)
    assert "CWID call=AG7EW fl=0 seen=1" in said and "CWID call=VE9QRP fl=0 seen=1" in said
    assert not any(s.startswith("CWID ok") for s in said)
    assert ret == 0 and said[-1] == "CWID end key msgs=2 short=0"
