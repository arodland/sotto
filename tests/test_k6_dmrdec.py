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
