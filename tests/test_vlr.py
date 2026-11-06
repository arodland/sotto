"""Very-low-rate sotto (sim/sotto_vlr.py): sotto 600 (48 bits / 80 ms) and sotto 400 (48 bits /
120 ms). Frame layout, tables, encode/decode round trip and lost-superframe rebuild.
SPDX-License-Identifier: MIT"""
import os
import sys

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sim"))
import vocoder_lab as vl  # noqa: E402
import sotto_vlr as vr  # noqa: E402

HTS1A = os.path.join(ROOT, "codec", "codec2", "raw", "hts1a.raw")


@pytest.mark.parametrize("mode,n", [("600", 4), ("400", 6)])
def test_layout_and_tables(mode, n):
    v = vr.SottoVLR(mode)
    assert v.N == n and v.BITS == 48
    assert sum(w for _, w in v.layout()) == 48
    c = v.cfg
    assert v.vpat.shape == (2 ** c["bv"], n) and v.vpat.min() >= 0 and v.vpat.max() <= 3
    assert v.slopes.shape == (2 ** c["bs"],) and np.all(np.diff(v.slopes) > 0)
    assert v.eres.shape == (2 ** c["br"], n)
    for j, (_, bits) in enumerate(c["anchors"]):
        assert [len(cb) for cb in v.scb[j]] == [2 ** b for b in bits]
        assert all(cb.shape[1] == vl.K - 1 for cb in v.scb[j])


def test_voicing_cost_is_a_distance():
    assert np.all(np.diag(vr.V_COST) == 0)
    assert np.allclose(vr.V_COST, vr.V_COST.T)
    assert vr.V_COST[0, 1] == vr.V_COST.max()          # unvoiced for voiced is the worst miss


@pytest.mark.parametrize("mode", ["600", "400"])
def test_round_trip_and_rebuild(mode):
    x = np.fromfile(HTS1A, "<i2").astype(float) / 32768
    v = vr.SottoVLR(mode)
    p = v.params(x)
    bits = v.encode_params(p)
    assert bits.shape == (int(np.ceil(len(p["code"]) / v.N)), 48)
    fp = v.frame_params(bits)
    assert len(fp) == len(bits) * v.N
    # the decoded fields are the tables' values
    f = v.unpack(bits[3])
    assert [q["code"] for q in fp[3 * v.N:4 * v.N]] == list(v.vpat[f["v"]])
    assert [q["ei"] for q in fp[3 * v.N:4 * v.N]] == list(v.energy_frames(f["em"], f["er"]))
    # anchors decode to their codewords
    off = v.cfg["anchors"][0][0]
    c = (fp[3 * v.N + off]["shape"] @ vl.DCT.T)[1:]
    assert np.allclose(c, v.anchor_value(0, f), atol=1e-9)
    # a lone lost superframe is rebuilt and its frames lie between the neighbours'
    good = np.ones(len(bits), bool)
    good[5] = False
    fl = v.frame_params(bits, good)
    assert v.rebuilt[5] and v.rebuilt.sum() == 1
    a, b = fl[5 * v.N - 1]["ei"], fl[6 * v.N]["ei"]
    for q in fl[5 * v.N:6 * v.N]:
        assert min(a, b) <= q["ei"] <= max(a, b)
    y = v.decode_audio(bits, good)
    assert len(y) == len(bits) * v.N * 160 and np.isfinite(y).all()
    assert np.sqrt(np.mean(y ** 2)) > 1e-3


EXE = os.path.join(ROOT, "c", "fixed", "sotto_vlr_dec_fx")


def _bands(y):
    n = len(y) // 320 * 320
    f = np.abs(np.fft.rfft(y[:n].reshape(-1, 320) * np.hanning(320), axis=1)) ** 2
    return 10 * np.log10(f[:, :160].reshape(len(f), 16, 10).sum(2) + 1e-9)


@pytest.mark.skipif(not os.path.exists(EXE), reason="needs make -C c VARIANT=v3.7 all fixed")
@pytest.mark.parametrize("mode", ["600", "400"])
def test_fixed_decoder_follows_python(mode):
    """The fixed-point decoder (c/fixed/sotto_fx_vlr.c) builds the same per-frame fields; the
    v3.7 core's waveform drifts in phase from the Python synthesis (as for sotto 1200), so
    compare band energies: within 1 dB on average, 160 samples (the core's frame) behind."""
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import check_vlr as cv
    x = np.fromfile(HTS1A, "<i2").astype(float) / 32768
    v = vr.SottoVLR(mode)
    bits = v.encode(x)
    for good in (None, np.arange(len(bits)) % 5 != 3):
        yp = v.decode_audio(bits, good)
        yc = cv.c_decode(mode, bits, good)
        assert cv.offset(yp, yc) == 160
        yc = yc[160:160 + len(yp)]
        bp, bc = _bands(yp), _bands(yc)
        act = bp.max(1) > bp.max() - 40
        assert np.abs(bp - bc)[act].mean() < 1.0


ENC = os.path.join(ROOT, "c", "sotto_enc")


@pytest.mark.skipif(not os.path.exists(ENC), reason="needs make -C c")
@pytest.mark.parametrize("mode", ["600", "400"])
def test_c_encoder_follows_python(mode, monkeypatch):
    """The float C encoder (c/sotto_vlr.c, sotto_enc -600/-400) writes the Python encoder's bits
    (with the C core's fixed-lag pitch tracker): its anchors settle as soon as every Viterbi
    path agrees, which gives the whole-utterance search's choice."""
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import check_vlr as cv
    monkeypatch.setitem(vl.TRACK2, "lag", 8)
    x16 = np.fromfile(HTS1A, "<i2")
    x16 = x16[:len(x16) // 80 * 80]
    v = vr.SottoVLR(mode)
    bp = v.encode(x16 / 32767.0)
    bc = cv.c_encode(mode, x16)
    assert bc.shape == bp.shape
    assert np.all(bp == bc, 1).mean() >= 0.98
