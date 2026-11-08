"""The DMR metadata decoder (sim/k6if/dmrdec.py): bursts it builds itself come back out, from
clean audio and through the K6's modelled GP0 path. Whether its ETSI layouts match real
repeaters is checked on recordings (docs/k6-dmr.md), not here.
SPDX-License-Identifier: MIT"""
import os
import sys

import numpy as np

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
    for _ in range(10):
        by = [int(v) for v in rng.integers(0, 256, 10)]
        for i, x in ((0, 0), (0, 0xFFFF), (0xFFFF, 0)):
            assert lib.h_crc16(u8(by), 10, i, x) == dd.crc_ccitt(bytes(by), i, x)
    cach = np.zeros(24, np.uint8)
    t = np.array([1, 0, 1, 1], np.uint8)
    cach[dd.TACT_POS] = list(t) + [t[0] ^ t[1] ^ t[2], t[1] ^ t[2] ^ t[3], t[0] ^ t[1] ^ t[3]]
    assert lib.h_tact(u8(cach)) == 0b1011
    cach[4] ^= 1
    assert lib.h_tact(u8(cach)) == -1
