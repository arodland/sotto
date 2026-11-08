"""The DMR metadata decoder (sim/k6if/dmrdec.py): bursts it builds itself come back out, from
clean audio and through the K6's modelled GP0 path. Whether its ETSI layouts match real
repeaters is checked on recordings (docs/k6-dmr.md), not here.
SPDX-License-Identifier: MIT"""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sim.k6if import dmr, dmrdec as dd  # noqa: E402


def deviation(sym, fs=48000):
    sps = fs // dmr.BAUD
    up = np.zeros(len(sym) * sps)
    up[::sps] = sym * sps
    return np.convolve(up, dmr.rrc(sps), "same") * dmr.UNIT


def test_codes_round_trip():
    rng = np.random.default_rng(1)
    for _ in range(20):
        d = rng.integers(0, 2, 96).astype(np.uint8)
        b = dd.bptc196_encode(d)
        assert np.array_equal(dd.bptc196(b)[0], d)
        b[rng.integers(196)] ^= 1                         # one error anywhere: corrected
        out, rows, cols = dd.bptc196(b)
        assert np.array_equal(out, d) and rows == cols == 0
    cw = dd.rs_encode(list(range(9)), "term")
    assert dd.rs_syndromes(cw[:9] + [c ^ 0x99 for c in cw[9:]]) == [0, 0, 0]
    for v in (0, 0x5A, 0xFF):
        w = int(dd.GOLAY["dpp"][v])
        b = np.array([(w >> (19 - i)) & 1 for i in range(20)], np.uint8)
        b[[2, 9, 17]] ^= 1                                # three errors: still the same byte
        assert dd.golay_decode(b) == (v, 3)


def headers(y):
    out = []
    for _, _, _, _, pol, sym, _ in dd.bursts_from_audio_y(y):
        if pol == 1:
            out.append(dd.decode_burst(sym))
    return out


def test_voice_headers_from_audio():
    sym = dd.voice_header_symbols(cc=3, tg=3100, src=3112345, n=3)
    got = [d for d in headers(dd.audio_symbols(deviation(sym), 48000)) if "lc" in d]
    assert len(got) == 3
    assert all(d["cc"] == 3 and d["lc"]["rs_ok"] and (d["lc"]["dst"], d["lc"]["src"]) == (3100, 3112345)
               for d in got)


def test_voice_headers_through_the_k6():
    """the K6's GP0 path as modelled at 56 dB-Hz: the frozen-gain bench level is ~48, where
    single bursts fail and the call's repeats have to be combined (later)"""
    sym = dd.voice_header_symbols(cc=1, tg=91, src=2345678, n=3, seed=2)
    s, _ = dmr.dmr_baseband(sym)
    y = dd.counts_symbols(dmr.to_counts(s, 56.0, seed=4))
    got = [d for d in headers(y) if d.get("lc", {}).get("rs_ok")]
    assert len(got) >= 2
    assert all(d["cc"] == 1 and (d["lc"]["dst"], d["lc"]["src"]) == (91, 2345678) for d in got)


def test_embedded_codes_round_trip():
    assert min(bin(int(a) ^ int(b)).count("1") for i, a in enumerate(dd.QR1676)
               for b in dd.QR1676[i + 1:]) == 6
    rng = np.random.default_rng(3)
    for cc in range(16):
        for lcss in range(4):
            e = dd.emb_encode(cc, lcss)
            e[rng.integers(16, size=2)] ^= 1                 # two errors: corrected
            mid = np.concatenate([e[:8], np.zeros(32, np.uint8), e[8:]])
            assert dd.emb_decode(mid)[:3] == (cc, 0, lcss)
    for _ in range(10):
        by = list(rng.integers(0, 256, 9))
        f = dd.emb_lc_encode(by)
        assert dd.emb_lc_decode(f) == (by, True)
        f[rng.integers(128)] ^= 1
        assert dd.emb_lc_decode(f) == (by, True)
        f = dd.emb_lc_encode(by)
        f[[0, 1, 2]] ^= 1                                     # 3 in one column... of 3 rows
        assert dd.emb_lc_decode(f)[0] == by
    assert dd.crc_ccitt(b"123456789") == 0x31C3 and dd.crc_ccitt(b"123456789", 0xFFFF) == 0x29B1


def call_of(y):
    pol, ev = dd.decode_stream(y)
    return pol, [c for c in dd.calls(ev) if c["slot"] == 1]


def test_voice_call_from_inverted_audio():
    """the IC-7100's audio is inverted: the decoder finds the polarity, the superframes' embedded
    LC and the talker alias"""
    sym = dd.voice_call_symbols(cc=1, tg=3027, src=3025107, superframes=8, alias="AG7EW TEST")
    pol, c = call_of(dd.audio_symbols(-deviation(sym), 48000))
    assert pol == -1 and len(c) == 1
    assert c[0]["lc"] == (0, 0, 3027, 3025107) and c[0]["alias"] == "AG7EW TEST"
    assert c[0]["copies"][(0, 0, 3027, 3025107)] == 8           # 3 headers, 4 embedded, 1 term


def test_combining_at_the_k6_bench_level():
    """~48 dB-Hz, the K6's frozen-gain level on 440.925: single copies mostly fail, the
    combined headers and terminator come out right"""
    sym = dd.voice_call_symbols(cc=1, tg=3027, src=3027436, superframes=4)
    s, _ = dmr.dmr_baseband(sym)
    _, c = call_of(dd.counts_symbols(dmr.to_counts(s, 48.0, seed=4)))
    cb = c[0]["combined"]
    assert cb["rs_ok"] and (cb["dst"], cb["src"]) == (3027, 3027436) and c[0]["n_comb"] >= 3


def burst_lib():
    """c/k6/dmrburst.h built for the host, or None without a compiler"""
    import ctypes
    import shutil
    import subprocess
    import tempfile
    if not shutil.which("cc"):
        return None
    k6 = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "c", "k6")
    so = os.path.join(tempfile.mkdtemp(), "dmrburst.so")
    subprocess.run(["cc", "-O2", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC", "-o", so,
                    os.path.join(k6, "dmrburst_host.c")], check=True)
    return ctypes.CDLL(so)


def test_c_codes_match_python():
    import ctypes
    import pytest
    lib = burst_lib()
    if lib is None:
        pytest.skip("no cc")
    u8 = lambda a: (ctypes.c_uint8 * len(a))(*[int(v) for v in a])     # noqa: E731
    err = ctypes.c_int()
    rng = np.random.default_rng(5)
    for v in rng.integers(0, 256, 30):
        w = int(dd.GOLAY["dpp"][v])
        b = np.array([(w >> (19 - i)) & 1 for i in range(20)], np.uint8)
        b[rng.integers(20, size=3)] ^= 1
        assert lib.h_slot(u8(b), ctypes.byref(err)) == dd.golay_decode(b)[0] and err.value == dd.golay_decode(b)[1]
    for d in rng.integers(0, 128, 30):
        e = dd.emb_encode(d >> 3, d & 3, (d >> 2) & 1)
        e[rng.integers(16)] ^= 1
        mid = np.concatenate([e[:8], rng.integers(0, 2, 32), e[8:]])
        assert lib.h_emb(u8(mid), ctypes.byref(err)) == d and err.value == 1
    out = (ctypes.c_uint8 * 12)()
    for k in range(20):
        kind = ("voice_hdr", "term")[k & 1]
        by = rng.integers(0, 256, 9)
        b = dd.bptc196_encode(dd.lc_encode(by[0] & 63, int(by[3]) << 16 | int(by[4]) << 8 | int(by[5]),
                                           int(by[6]) << 16 | int(by[7]) << 8 | int(by[8]), int(by[1]),
                                           int(by[2]), kind))
        if k % 3:
            b[rng.integers(196)] ^= 1
        assert lib.h_bptc196(u8(b), out) == 0
        data, _, _ = dd.bptc196(b)
        assert list(out) == dd.lc_bytes(data)
        assert lib.h_rs_ok(out, dd.MASK[kind]) == 1 and lib.h_rs_ok(out, dd.MASK[kind] ^ 1) == 0
    o9 = (ctypes.c_uint8 * 9)()
    for k in range(20):
        by = [int(v) for v in rng.integers(0, 256, 9)]
        f = dd.emb_lc_encode(by)
        if k % 2:
            f[rng.integers(128)] ^= 1
        assert lib.h_emb_lc(u8(f), o9) == 1 and list(o9) == by
        f[rng.integers(128, size=4)] ^= 1
        assert lib.h_emb_lc(u8(f), o9) == int(dd.emb_lc_decode(f)[1])
    by = [int(v) for v in rng.integers(0, 256, 9)]
    f = dd.emb_lc_encode(by)
    where = {int(m): a for a, m in enumerate(dd.EMB_ORDER)}   # matrix bit -> place sent
    for r in range(7):                                      # two errors in one row: the columns
        g = f.copy()
        g[[where[16 * r + 3], where[16 * r + 12]]] ^= 1
        assert lib.h_emb_lc(u8(g), o9) == 1 and list(o9) == by, r
        assert dd.emb_lc_decode(g) == (by, True), r
    for _ in range(10):
        by = [int(v) for v in rng.integers(0, 256, 10)]
        for i, x in ((0, 0), (0, 0xFFFF), (0xFFFF, 0)):
            assert lib.h_crc16(u8(by), 10, i, x) == dd.crc_ccitt(bytes(by), i, x)
    cach = np.zeros(24, np.uint8)
    t = np.array([1, 0, 1, 1], np.uint8)
    cach[dd.TACT_POS] = list(t) + [t[0] ^ t[1] ^ t[2], t[1] ^ t[2] ^ t[3], t[0] ^ t[1] ^ t[3]]
    assert lib.h_tact(u8(cach)) == 0b1011
    for k in dd.TACT_POS:                                   # one error: corrected, flagged
        cach[k] ^= 1
        assert lib.h_tact(u8(cach)) == 0b1011 | 0x10, k
        cach[k] ^= 1


# ---- the K6's decoder: c/k6/mod/dmrdec.inc on the host, and the dmrrx module emulated ----

def k6_counts(sym, cn0, seed=1):
    """the K6's GP0 counts (2 * ones - 64 per 64 DMA bytes) for a call, as the modules see them"""
    import test_k6_dmr as T
    s, _ = dmr.dmr_baseband(sym, pre_s=0.0, post_s=0.0)
    ib, on, off = T.stream(s, cn0=cn0, seed=seed)
    b = (ib >> 1) & 1
    n = len(b) // 64
    return (b[:n * 64].reshape(n, 64).sum(1) * 2 - 64).astype(np.int16), (ib, on, off)


CALL = dict(cc=1, tg=3027, src=3025107, superframes=6, alias="AG7EW TEST", seed=1)


def test_c_decoder_on_the_k6_model():
    """dmrsync.h's grid, its matched filter and dmrdec.inc at 52 dB-Hz (about the K6's level on
    440.925 with the gain frozen): the LC, every voice burst's EMB, the talker alias"""
    import shutil
    import subprocess
    import tempfile
    import pytest
    if not shutil.which("cc"):
        pytest.skip("no cc")
    k6 = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "c", "k6")
    exe = os.path.join(tempfile.mkdtemp(), "dmrrx_host")
    subprocess.run(["cc", "-O2", "-Wall", "-Wextra", "-Werror", "-I", k6, "-o", exe,
                    os.path.join(k6, "dmrrx_host.c")], check=True)
    alias = mids = ok = 0
    for seed in range(1, 5):                # the alias's blocks go once each: a few calls
        cnt, _ = k6_counts(dd.voice_call_symbols(**dict(CALL, seed=seed)), 52.0)
        out = subprocess.run([exe, "4476"], input=cnt.tobytes(), capture_output=True, check=True).stdout.decode()
        print(out)
        lines = out.splitlines()
        assert any(ln.startswith("L TS1 CC1 TG 3027 src 3025107 F0") for ln in lines), out
        assert not any(ln[:2] in ("L ", "E ") and "TG 3027 src 3025107" not in ln for ln in lines), out
        n = dict(kv.split("=") for kv in lines[-1].split()[1:])
        assert int(n["L"]) >= 3, lines[-1]
        alias += "A AG7EW TEST" in lines
        m, k = map(int, n["mids"].split("/"))
        mids, ok = mids + m, ok + k
    assert alias >= 2 and ok >= 88, (alias, mids, ok)


def test_dmrrx_module_on_the_emulated_k6():
    """the dmrrx module (dmrdet + the decoder) on the emulated M0 with the modelled DMA: the
    call's talkgroup and source on the serial line and the screen, its alias, in the shared
    module block, the receiver's registers put back. The filter's ring, the decoder's state and
    the DMA ring all go in the scratch past the code, never on the stack (the module call gets
    3,112 B of it on the vlrh image: the first bench run overran it): a block too small for them
    and it says so and returns -6. FD6818 register reads take 300 us here, as slow as the
    radio's housekeeping seemed on the first bench run (its calls outlast the DMA ring: not
    counted as lost)"""
    import shutil
    if not (shutil.which("clang") and shutil.which("ld.lld")):
        pytest.skip("needs clang and ld.lld")
    pytest.importorskip("unicorn")
    import test_k6_dmr as T
    import k6_live_emu as le
    from sim.k6if import frontend as fe
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "c", "k6", "mod", "dmrrx.c")
    code, entry = T.build([("RUN_S", "3u"), ("S_MS", "1000u")], src)
    _, (ib, on, off) = k6_counts(dd.voice_call_symbols(**CALL), 56.0)

    def emu(block, ib, sig=True):
        e = le.Emu(code, entry, ib, on, fe.FDMA, 1.5, max_s=len(ib) / fe.FDMA + 0.5, level=6, stack_win=7000,
                   scratch=(block - 24 - len(code)) & ~3, fd_us=300)
        e.rssi = lambda: -60 if sig and on <= e.sample_now() < off else -125
        ret, stack = e.run()
        return e, ret, stack

    e, ret, _ = emu(len(code) + 24 + 1024, ib[:on])   # too small a block: refused, and why
    assert ret - (1 << 32) == -6 and e.said and e.said[0].startswith("dmr scratch "), (ret, e.said)
    need = int(e.said[0].split(" need ")[1])
    assert len(code) + 24 + need <= 11776 - 256, (len(code), need)  # the shared block, 256 B to spare
    e, ret, stack = emu(11776, ib)
    txt = "\n".join(e.said)
    print(txt, e.lcd, stack)
    assert ret >= 1, txt
    assert all(f"TS{n}" not in ln for ln in e.said for n in (0, 3)), txt       # the slot named
    assert any(ln.startswith("L TS") and "TG 3027 src 3025107" in ln for ln in e.said), txt
    assert any(ln.startswith("A AG7EW T") for ln in e.said), txt
    assert e.lcd.get(1) == "TG 3027" and e.lcd.get(2) == "ID 3025107", e.lcd
    assert e.writes[0x13][-1] == 0 and e.writes[0x7E][-1] == 0               # put back (reads 0 here)
    assert stack < 1500, stack                         # of the 3,112 B
    lost = [int(ln.split("lost=")[1].split()[0]) for ln in e.said if ln.startswith("s ")]
    ks = [int(ln.split("ks=")[1].split()[0]) for ln in e.said if ln.startswith("s ")]
    assert lost and lost[-1] <= 80, txt               # overruns while a burst is read (and the
                                                       # bursts are in the filter's ring by then)
    assert abs(ks[0] - fe.FDMA / 1000) < 10, txt      # the DMA's rate measured, housekeeping or no
    # no signal: housekeeping's long calls aren't lost samples
    e, ret, _ = emu(11776, np.random.default_rng(3).integers(0, 2, len(ib)).astype(np.uint8) * 2, sig=False)
    lost = [int(ln.split("lost=")[1].split()[0]) for ln in e.said if ln.startswith("s ")]
    assert lost and lost[-1] == 0, e.said


def test_dmrscore_against_a_key(tmp_path):
    """tools/k6_dmrscore.py: the K6's lines lined up with the answer key by themselves, a
    right call, a missed one, a wrong line, a stray one, a data header, the alias"""
    import json
    import subprocess
    t0 = 1794189600.0                                       # 2026-11-09T05:20:00Z
    iso = lambda t: __import__("datetime").datetime.fromtimestamp(t, __import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    key = [dict(file="a.wav", kind="call", at=iso(t0 + 10), dur=4.0, slot=1, alias="AG7EW TEST",
                lc=dict(flco=0, fid=16, dst=3027, src=3025107), combined=None),
           dict(file="a.wav", kind="call", at=iso(t0 + 30), dur=3.0, slot=1, alias=None,
                lc=dict(flco=0, fid=16, dst=3027, src=3027436), combined=None),
           dict(file="b.wav", kind="call", at=iso(t0 + 60), dur=5.0, slot=2, alias=None,
                lc=dict(flco=0, fid=16, dst=91, src=1234567), combined=None),
           dict(file="b.wav", kind="data", at=iso(t0 + 80), slot=1, crc=True, payload="",
                hdr=dict(dst=311999, src=3027436))]
    (tmp_path / "key.jsonl").write_text("".join(json.dumps(e) + "\n" for e in key))
    start = t0 + 3.2                                        # the module started 3.2 s in
    at = lambda t: f" @{round((t - start) * 10)}"
    hdr = "02" + "30" + "04c2bf" + "2e31ec" + "08" + "00"   # DPF 2, SAP 3, 311999 <- 3027436
    log = [f"05:20:03 dmr start f=440925000 floor=-121",
           f"05:20:10 L TS1 CC1 TG 3027 src 3025107 F0{at(t0 + 10.3)}",
           f"05:20:12 A AG7EW TEST{at(t0 + 12.0)}",
           f"05:20:13 E TS1 TG 3027 src 3025107{at(t0 + 13.1)}",
           f"05:20:31 L TS1 CC1 TG 3027 src 3025107 F0{at(t0 + 31.0)}",   # wrong: src 3027436
           f"05:20:31 L TS1 CC1 TG 3027 src 3027436 F0{at(t0 + 31.2)}",
           f"05:20:45 L TS2 CC1 TG 9 src 7 F0{at(t0 + 45.0)}",             # stray
           f"05:21:20 H TS1 {hdr}{at(t0 + 80.4)}"]
    (tmp_path / "k6.log").write_text("\n".join(log) + "\n")
    tool = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "k6_dmrscore.py")
    out = subprocess.run([sys.executable, tool, "--key", str(tmp_path / "key.jsonl"), "--log",
                          str(tmp_path / "k6.log"), "--json"], capture_output=True, text=True, check=True).stdout
    r = json.loads(out)
    assert abs(__import__("datetime").datetime.fromisoformat(r["start"].replace("Z", "+00:00")).timestamp() - start) < 0.6, r
    assert (r["calls"], r["named"], r["right"], r["wrong"], r["stray"]) == (3, 2, 2, 1, 1), r
    assert (r["data"], r["data_hit"], r["alias"], r["alias_hit"]) == (1, 1, 1, 1), r
    assert len(r["miss"]) == 1 and "TG 91" in r["miss"][0], r



def test_dmrwatch_on_a_recording_with_no_calls(tmp_path):
    """tools/k6_dmrwatch.py: a cut with nothing in it gives one "none" line (it raised
    KeyError('t') on hamlet, 2026-11-08)"""
    sf = pytest.importorskip("soundfile")
    import importlib.util
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location("k6_dmrwatch", os.path.join(root, "tools", "k6_dmrwatch.py"))
    w = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(w)
    p = tmp_path / "440.9250_20261108T042456Z.wav"
    sf.write(str(p), np.random.default_rng(1).normal(0, 0.05, 48000), 48000)
    out = w.decode_file(str(p))
    assert [e["kind"] for e in out] == ["none"] and out[0]["at"] == "2026-11-08T04:24:56.000Z", out
