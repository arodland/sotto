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


@pytest.mark.parametrize("mode", ["600", "400"])
def test_hiss_band_rides_free_pitch_bits(mode):
    """sotto 600 / 400 hiss (sim/sotto_vlr_hiss.py): 48 bits, 16 kHz out; superframes with no
    pitched frame carry hiss in the pitch fields, and the 8 kHz part decodes as before."""
    import sotto_vlr_hiss as vh
    from scipy.signal import resample_poly
    x = np.fromfile(HTS1A, "<i2").astype(float) / 32768
    x16 = resample_poly(x, 2, 1)
    x16 = x16[:len(x16) // 640 * 640]
    c = vh.SottoVLRHiss(mode)
    b = c.encode(x16)
    assert b.shape[1] == 48
    kinds = [c.kind(c.unpack(r)) for r in b]
    assert 0 in kinds
    y = c.decode_audio16(b, n16=len(x16))
    assert len(y) == len(x16) and np.all(np.isfinite(y))
    hi = np.abs(np.fft.rfft(y))[len(y) // 4:].sum() / np.abs(np.fft.rfft(y)).sum()
    assert hi > 0.005                      # energy above 4 kHz exists
    # a one-pitched-frame superframe's pitch is its centre (the slope field is hiss)
    for r in b:
        f = c.unpack(r)
        if c.kind(f) == 1:
            assert len(set(c.sf_pitch(f))) == 1


def _c_has_vlr_hiss():
    import subprocess
    exe = os.path.join(ROOT, "c", "sotto_dec")
    return os.path.exists(exe) and subprocess.run([exe, "-16", "-600", "/dev/null", "/dev/null"],
                                                  capture_output=True).returncode == 0


need_c38 = pytest.mark.skipif(not _c_has_vlr_hiss(), reason="needs the VARIANT=v3.8 C build (make -C c)")


@need_c38
@pytest.mark.parametrize("mode", ["600", "400"])
def test_c_hiss_decoder_follows_python(mode):
    """sotto 600 / 400 hiss in C (c/sotto_hiss.c sotto_vlr_dec16, sotto_dec -16 -600/-400) on the
    Python encoder's bits, with lone and repeated losses: the 0-4 kHz parts agree sample by
    sample, the 4-8 kHz noise (random phase) by level (tools/check_vlr_hiss.py)."""
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import check_vlr_hiss as ch
    import sotto_vlr_hiss as vh
    from scipy.signal import resample_poly
    from test_lr_hiss import band_db
    x16 = resample_poly(np.fromfile(HTS1A, "<i2").astype(float) / 32768, 2, 1)
    x16 = x16[:len(x16) // 320 * 320]
    c = vh.SottoVLRHiss(mode)
    b = c.encode(x16)
    good = np.ones(len(b), bool)
    good[[5, 9, 10]] = False
    for g in (None, good):
        yp = c.decode_audio16(b, g, n16=len(x16))
        yc = ch.c_decode(b, mode, g)
        n = min(len(yp), len(yc))
        assert len(yc) >= len(yp)
        lp, lc = resample_poly(yp[:n], 1, 2), resample_poly(yc[:n], 1, 2)
        assert 10 * np.log10(np.sum(lp ** 2) / np.sum((lp - lc) ** 2)) > 20
        hp, hc = band_db(yp[:n], 4000, 8000), band_db(yc[:n], 4000, 8000)
        live = hp > -60
        assert live.sum() > 50 and np.mean(np.abs(hp - hc)[live]) < 1.5


@need_c38
@pytest.mark.parametrize("mode", ["600", "400"])
def test_c_hiss_encoder_follows_python(mode, monkeypatch):
    """The C hiss encoder (sotto_vlr_enc16, sotto_enc -16 -600/-400) writes the Python's bits,
    with the Python running the C's fixed-lag pitch tracker and whisper rule."""
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import check_vlr_hiss as ch
    import sotto_vlr_hiss as vh
    from scipy.signal import resample_poly
    monkeypatch.setitem(vl.TRACK2, "lag", 8)
    x16 = np.round(resample_poly(np.fromfile(HTS1A, "<i2").astype(float) / 32768, 2, 1) * 32767).astype(np.int16)
    x16 = x16[:len(x16) // 320 * 320]
    bp = vh.SottoVLRHiss(mode, whisper_lag=10).encode(x16 / 32767.0)
    bc = ch.c_encode(x16, mode)
    assert bc.shape == bp.shape
    assert np.all(bp == bc, 1).mean() >= 0.95


def _hiss_burst_db(mode, clip="kristoff", **cfg):
    """Speech plus 4-8 kHz noise as loud as the speech (Ragtime's extreme input) through sotto
    600 / 400 hiss: the worst 120 ms of decoded 4-8 kHz level over the input's (dB)."""
    import sotto_vlr_hiss as vh
    from scipy.signal import butter, lfilter, resample_poly, sosfilt
    x = resample_poly(np.fromfile(os.path.join(ROOT, "codec", "codec2", "raw", f"{clip}.raw"), "<i2") / 32768.0, 2, 1)
    x = x / np.sqrt(np.mean(x ** 2)) * 10 ** (-26 / 20)
    n = lfilter(*butter(4, 0.5, "high"), np.random.default_rng(1).standard_normal(len(x)))
    x = (x + n * np.sqrt(np.mean(x ** 2) / np.mean(n ** 2)))[:len(x) // 640 * 640]
    b = vh.SottoVLRHiss(mode).encode(x)
    d = vh.SottoVLRHiss(mode)
    for k, v in cfg.items():
        setattr(d, k, v)
    y = d.decode_audio16(b, n16=len(x))
    hi = butter(8, 4200 / 8000, "high", output="sos")

    def lv(v):
        return np.mean(v[:len(v) // 320 * 320].reshape(-1, 320) ** 2, 1) + 1e-12
    r = lv(sosfilt(hi, y)) / lv(sosfilt(hi, x))
    return 10 * np.log10(np.convolve(r, np.ones(6) / 6, "valid").max())


def test_hiss_no_bits_frames_stay_level_in_background_hiss():
    """A no-bits frame's hiss is a residual over its own guess, which follows the 0-4 kHz band;
    at a voiced onset in steady background hiss it used to jump with that band (Ragtime: sotto
    400 about 10 dB too loud). The decoder now caps it at the last coded frame's level."""
    assert _hiss_burst_db("400", CAP_DB=1e9, RES_CAP=1e9) > 9    # the old decoder
    assert _hiss_burst_db("400") < 6


def test_creak_tail_keeps_the_modal_frame():
    """LibriSpeech 198 at 13.32 s ("my dear"): one modal frame then four creak frames at half
    its pitch. The 400 line used to follow the creak and play the modal frame an octave low;
    now it starts near the modal frame and falls (sotto_vlr.SottoVLR.creak_mask)."""
    v = vr.SottoVLR("400")
    code = np.array([1, 1, 1, 3, 1, 1, 1, 0])
    wi = np.array([65, 66, 66, 32, 34, 36, 36, 58])
    cr = v.creak_mask(code, wi)
    assert cr.tolist() == [False, False, False, True, True, True, True, False]
    voiced = np.array([True, True, True, True, True, False])
    wc, ws = v.q_pitch(wi[2:], voiced, 64, cr[2:])
    line = v.pitch_frames(wc, ws)
    assert abs(line[0] - 66) <= 4 and line[4] < line[0] - 15
    v.CREAK = False
    wc, ws = v.q_pitch(wi[2:], voiced, 64, None)
    assert v.pitch_frames(wc, ws)[0] < 45            # the old behaviour, an octave low


def test_creak_needs_a_steady_voice_before_the_drop():
    """An onset's junk pitch (LibriSpeech 251 at 6.38 s: 500 Hz on the edge frame) or mixed
    onset frames don't make the real voice after them creak."""
    v = vr.SottoVLR("400")
    assert not v.creak_mask(np.array([0, 3, 3, 1, 1]), np.array([50, 127, 36, 36, 35])).any()
    assert not v.creak_mask(np.array([0, 2, 3, 2, 1]), np.array([0, 89, 88, 21, 23])).any()
    assert not v.creak_mask(np.array([1, 1, 0, 1, 1]), np.array([70, 70, 0, 30, 30])).any()


def test_line_search_reaches_past_a_skewed_fit():
    """LibriSpeech 198 "all" at 9.48 s: a creaky onset frame (117 Hz) just inside OUTLIER pulls
    the least-squares centre down; searching only +-1 around it played the vowel 2 semitones
    flat. Every centre is tried now (SottoVLR.FULL_SEARCH)."""
    v = vr.SottoVLR("400")
    wi = np.array([47, 87, 87, 85, 85, 84])
    wc, ws = v.q_pitch(wi, np.ones(6, bool), 64)
    assert np.all(np.abs(v.pitch_frames(wc, ws)[1:] - wi[1:]) <= 2)
    wc, ws = v.q_pitch(np.array([0, 0, 70, 0, 0, 0]), np.array([0, 0, 1, 0, 0, 0], bool), 64)
    assert np.ptp(v.pitch_frames(wc, ws)) <= 1         # one pitched frame: a flat line
