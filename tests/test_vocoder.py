"""Vocoder lab tests: the Python sinusoidal analysis must match c2sim, the pitch tracker must
undo octave errors, and the sotto encoders must produce 48-bit frames that decode.

Needs the Codec 2 tools (tools/build_codec2.sh); skipped without them.
Run: python -m pytest tests
"""
import os
import sys

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sim"))

import audio_race as ar                                       # noqa: E402

pytestmark = pytest.mark.skipif(not os.path.exists(os.path.join(ar.C2BIN, "c2sim")),
                                reason="Codec 2 tools not built")

import vocoder_lab as vl                                      # noqa: E402
import sinusoid as sn                                         # noqa: E402

FS = 8000


def _vowel(f0, seconds=1.0, formants=(700, 1200, 2600)):
    """Glottal-ish pulse train at f0 through three formant resonators, plus a little noise."""
    from scipy import signal
    n = int(seconds * FS)
    x = np.zeros(n)
    x[(np.arange(0, n, FS / f0)).astype(int)] = 1.0
    for f in formants:
        r = np.exp(-np.pi * 80 / FS)
        x = signal.lfilter([1], [1, -2 * r * np.cos(2 * np.pi * f / FS), r * r], x)
    x += 1e-3 * np.random.default_rng(1).standard_normal(n)
    return 0.3 * x / np.abs(x).max()


def _ve9qrp(seconds=3.0):
    raw = os.path.join(ROOT, "codec", "codec2", "raw", "ve9qrp_10s.raw")
    return np.fromfile(raw, "<i2")[:int(seconds * FS)].astype(float) / 32768


def test_analysis_matches_c2sim():
    x = _ve9qrp()
    m = vl.analyse(x)
    mine = sn.analyse(np.round(x * 32767), m["Wo"], do_refine=False)
    assert len(mine) == len(m)
    for r, q in zip(m, mine):
        L = r["L"]
        assert q["L"] == L
        np.testing.assert_allclose(q["A"][1:L + 1], r["A"][1:L + 1], rtol=1e-3, atol=1e-2)
    assert np.mean(mine["voiced"] == m["voiced"]) > 0.99      # float rounding at the 6 dB edge


def test_tracker_undoes_doubled_pitch():
    x = _vowel(180)
    Sw = sn.spectra(np.round(x * 32767))
    wo_true = 2 * np.pi * 180 / FS
    doubled = np.full(len(Sw), 2 * wo_true)
    doubled[::3] = wo_true                       # an octave error on two frames in three
    wo = sn.track_pitch(Sw, doubled, np.ones(len(Sw), int))
    mid = slice(5, -5)
    assert np.all(np.abs(np.log(wo[mid] / wo_true)) < 0.05)


def test_v37_tracker_undoes_tripled_pitch():
    """Codec 2 saying three times the true pitch for 10 frames running (listening round 10,
    EARS p020 at 0.4 s): v3.6's candidates (x1, x2, x1/2 of three neighbours) are then
    all far from the true pitch, so it played ~3x the voice's pitch. v3.7 adds a third."""
    x = _vowel(120)
    Sw = sn.spectra(np.round(x * 32767))
    wo_true = 2 * np.pi * 120 / FS
    wo0 = np.full(len(Sw), wo_true)
    wo0[40:50] = 3 * wo_true
    track = lambda third: sn.track_pitch_voicing(Sw, wo0, np.ones(len(Sw), int), sub=2.0, lag=8, third=third)
    wo, v = track(False)
    assert np.any((v[40:50] > 0) & (np.abs(np.log(wo[40:50] / wo_true)) > 0.3))   # 180 Hz here
    wo, v = track(True)
    assert np.all(v[40:50] > 0)
    assert np.all(np.abs(np.log(wo[40:50] / wo_true)) < 0.1)


def test_tracker_keeps_low_male_pitch():
    x = _vowel(95)
    Sw = sn.spectra(np.round(x * 32767))
    wo_true = 2 * np.pi * 95 / FS
    wo = sn.track_pitch(Sw, np.full(len(Sw), wo_true), np.ones(len(Sw), int))
    assert np.all(np.abs(np.log(wo[5:-5] / wo_true)) < 0.05)   # no halving below 70 Hz


@pytest.mark.parametrize("pitch", ["c2", "tracked"])
def test_codec_frames_decode(pitch):
    x = _ve9qrp(2.0)
    v = vl.SottoV0(pitch=pitch)
    bits = v.encode(x)
    assert bits.shape == (len(x) // 160, 48) and set(np.unique(bits)) <= {0, 1}
    y = v.decode_audio(bits)
    lag = ar.best_lag(x, y)
    pesq, _ = ar.score(x, y, lag)
    assert pesq > 2.2


def test_joint_tracker_follows_pitch_above_400_hz():
    x = _vowel(440, formants=(800, 1500, 2800))
    Sw = sn.spectra(np.round(x * 32767))
    wo_c2 = np.full(len(Sw), sn.WO_MAX)              # Codec 2 clamps at 400 Hz
    wo, v = sn.track_pitch_voicing(Sw, wo_c2, np.ones(len(Sw), int))
    mid = slice(5, -5)
    assert np.all(v[mid] == 1)
    assert np.all(np.abs(np.log(wo[mid] / (2 * np.pi * 440 / FS))) < 0.05)


def test_joint_tracker_unvoices_noise():
    x = 0.1 * np.random.default_rng(3).standard_normal(FS)
    m = vl.analyse(x)                                 # Codec 2 calls some of it voiced
    Sw = sn.spectra(np.round(x * 32767))
    _, v = sn.track_pitch_voicing(Sw, m["Wo"], m["voiced"])
    assert v.mean() <= m["voiced"].mean() and v.mean() < 0.05


def test_v3_frames_decode():
    x = _ve9qrp(2.0)
    v = vl.SottoV3()
    bits = v.encode(x)
    assert bits.shape == (len(x) // 160, 48)
    pesq, _ = ar.score(x, v.decode_audio(bits), ar.best_lag(x, None))
    assert pesq > 2.5
    idx = v.vq.indices(np.zeros((1, vl.K)))
    assert idx.shape == (1, 3)


def test_v31_only_unvoices_quiet_tails():
    x = _ve9qrp(4.0)
    m3 = vl.analyse(x, "tracked2")
    m31 = vl.analyse(x, "tracked2t")
    gone = (m3["voiced"] > 0) & (m31["voiced"] == 0)
    assert not ((m31["voiced"] > 0) & (m3["voiced"] == 0)).any()
    e = vl.frame_energy_db(m3)
    for k in np.where(gone)[0]:
        assert e[k] < e[max(0, k - 30):k + 1].max() - 20
    bits = vl.SottoV31().encode(x)
    assert bits.shape == (len(x) // 160, 48)


def test_odd_even_flags_half_pitch():
    # harmonics of 250 Hz: at 125 Hz every odd "harmonic" is missing
    t = np.arange(4000) / 8000
    x = sum(np.cos(2 * np.pi * 250 * k * t) / k for k in range(1, 12))
    Sw = sn.spectra(np.round(x / np.abs(x).max() * 16000))
    w = 2 * np.pi * 250 / 8000
    assert sn.odd_even_db(Sw[20], w / 2) < -15
    assert sn.odd_even_db(Sw[20], w) > -6


def test_v34_conceals_lost_frames():
    x = _ve9qrp(2.0)
    v = vl.SottoV34(slope=3.0, max_lost=5)
    bits = v.encode(x)
    good = np.ones(len(bits), bool)
    good[30:40] = False
    m0, m = v.decode(bits), v.decode(bits, good)
    e = vl.frame_energy_db(m)
    assert np.allclose(m["A"][:59], m0["A"][:59])         # frames before the gap untouched
    assert e[2 * 31] > e[2 * 34] + 5                      # level falls through the gap
    assert e[2 * 36] < e[58] - 60                         # silent after max_lost frames


@pytest.mark.skipif(not os.path.exists(os.path.join(ROOT, "c", "sotto_enc")),
                    reason="C port not built (make -C c)")
def test_c_port_matches_python():
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import check_c_port as cc
    x16 = np.round(_ve9qrp(4.0) * 32767).astype(np.int16)
    x16 = x16[:len(x16) // 160 * 160]
    vl.TRACK2["lag"] = 8
    try:
        v = vl.SottoV37()                                 # the C build's default variant
        bp = v.encode(x16 / 32767.0)
    finally:
        vl.TRACK2.pop("lag")
    bc = cc.c_encode(x16)
    assert bc.shape == bp.shape
    assert np.mean(np.all(bp == bc, 1)) > 0.97            # rare pitch near-ties may differ
    good = np.ones(len(bp), bool)
    good[60:64] = False
    yp = vl.synthesise(v.decode(bp, good))
    yc = cc.c_decode(bp, good)
    assert len(yc) == len(yp)
    assert cc.snr_db(yp, yc) > 60


def test_header_level_constants_match_energy_field():
    """c/sotto.h documents the energy field's scale; check it against the analysis."""
    import re
    h = open(os.path.join(ROOT, "c", "sotto.h")).read()
    const = {k: float(v) for k, v in re.findall(r"#define (SOTTO_\w+_DB\w*) \(?(-?[\d.]+)\)?", h)}
    a = 10 ** ((-23.0 + 3.0) / 20)                        # a sine at -23 dBFS RMS
    x = a * np.sin(2 * np.pi * 440 * np.arange(FS) / FS) * 32767 / 32768
    e = vl.frame_energy_db(vl.analyse(x))[20:-5].mean()
    assert abs(e - (-23.0 + const["SOTTO_ENERGY_OFFSET_DB"])) < 0.3
    lo, hi = vl.SottoV0.E_LO, vl.SottoV0.E_HI
    assert (lo, hi) == (const["SOTTO_ENERGY_MIN_DB"], const["SOTTO_ENERGY_MAX_DB"])
    step = (hi - lo) / 31
    assert abs(const["SOTTO_LEVEL_SILENT_DBFS"] - (lo + step / 2 - const["SOTTO_ENERGY_OFFSET_DB"])) < 0.1
    assert abs(const["SOTTO_LEVEL_MAX_DBFS"] - (hi - const["SOTTO_ENERGY_OFFSET_DB"])) < 0.1


@pytest.mark.skipif(not os.path.exists(os.path.join(ROOT, "c", "fixed", "sotto_dec_fx")),
                    reason="fixed-point decoder not built (make -C c fixed)")
def test_fixed_point_decoder_matches_float():
    """c/fixed/sotto_dec_fx (integer only) against the float decoder, same bits. Their
    harmonic levels agree to a few hundredths of a dB, but where a harmonic sits on the
    postfilter's noise threshold one decoder randomises its phase and the other doesn't, and
    the random-phase sequences fall out of step (in v3.8 builds, whose unvoiced halves have
    80 random-phase harmonics, that happens within the first frames of a quiet start). So the
    outputs are compared by level: 500 Hz bands over 80 ms windows, and the total energy."""
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import check_c_port as cc
    import check_fx
    x16 = np.round(_ve9qrp(4.0) * 32767).astype(np.int16)
    bits = cc.c_encode(x16[:len(x16) // 160 * 160])
    good = np.ones(len(bits), bool)
    good[60:64] = False
    yf = cc.c_decode(bits, good)
    yx = check_fx.run_dec(os.path.join(ROOT, "c", "fixed", "sotto_dec_fx"), bits, good)
    assert len(yx) == len(yf)
    win, hop = 640, 320
    w = np.hanning(win)
    f = np.fft.rfftfreq(win, 1 / 8000)
    bf, bx = [], []
    for i in range(0, len(yf) - win, hop):
        for y, out in ((yf, bf), (yx, bx)):
            X = np.abs(np.fft.rfft(y[i:i + win] * w)) ** 2
            out.append([10 * np.log10(X[(f >= lo) & (f < lo + 500)].sum() + 1e-12) for lo in range(0, 4000, 500)])
    bf, bx = np.array(bf), np.array(bx)
    d = np.abs(bf - bx)[bf > -60]
    assert np.median(d) < 1.0 and np.mean(d) < 1.5
    assert abs(10 * np.log10(np.sum(yx ** 2) / np.sum(yf ** 2))) < 0.5


def test_fixed_point_encoder_matches_float():
    """c/fixed/sotto_enc_fx against the float encoder: almost every frame gives the same 48
    bits (the rest are near-ties at float rounding boundaries), and the fixed decoder gets
    the same PESQ from them."""
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import check_c_port as cc
    import check_fx
    x = _ve9qrp(4.0)
    x16 = np.round(x * 32767).astype(np.int16)[:len(x) // 160 * 160]
    bf = cc.c_encode(x16)
    bx = check_fx.run_enc(os.path.join(ROOT, "c", "fixed", "sotto_enc_fx"), x16)
    assert bx.shape == bf.shape
    assert np.mean(np.all(bf == bx, 1)) > 0.9
    y = check_fx.run_dec(os.path.join(ROOT, "c", "fixed", "sotto_dec_fx"), bx)
    pf = ar.score(x[:len(x16)], cc.c_decode(bf), ar.CODEC_DELAY)[0]
    px = ar.score(x[:len(x16)], y, ar.CODEC_DELAY)[0]
    assert px > pf - 0.1



@pytest.mark.skipif(not os.path.exists(os.path.join(ROOT, "c", "fixed", "sotto_enc_fx_arm")),
                    reason="Cortex-M4 code path not built (make -C c fixed)")
def test_fixed_point_arm_path_is_bit_exact():
    """The Cortex-M4 path (FX_ARM_DSP: direct 32 x 32 products, codebook search and dot
    products with SMLAD/SMLALD, emulated here) gives the same bits and the same samples as
    the C55x-shaped C, at 8 kHz and at 16 kHz (v3.8)."""
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import check_fx
    x16 = np.round(_ve9qrp(4.0) * 32767).astype(np.int16)[:4 * FS // 160 * 160]
    fx = os.path.join(ROOT, "c", "fixed")
    b = check_fx.run_enc(os.path.join(fx, "sotto_enc_fx"), x16)
    assert np.array_equal(b, check_fx.run_enc(os.path.join(fx, "sotto_enc_fx_arm"), x16))
    good = np.ones(len(b), bool)
    good[60:64] = False
    assert np.array_equal(check_fx.run_dec(os.path.join(fx, "sotto_dec_fx"), b, good),
                          check_fx.run_dec(os.path.join(fx, "sotto_dec_fx_arm"), b, good))
    # v3.8 at 16 kHz (SMLALD resamplers): the same speech with hiss above 4 kHz
    from scipy import signal
    hiss = signal.sosfilt(signal.butter(6, 4500, "highpass", fs=16000, output="sos"),
                          np.random.default_rng(1).standard_normal(2 * len(x16)))
    y = np.clip(signal.resample_poly(x16, 2, 1) + 1500 * hiss, -32767, 32767).astype(np.int16)
    b = check_fx.run_enc(os.path.join(fx, "sotto_enc_fx"), y, k16=True)
    assert np.array_equal(b, check_fx.run_enc(os.path.join(fx, "sotto_enc_fx_arm"), y, k16=True))
    assert np.array_equal(check_fx.run_dec(os.path.join(fx, "sotto_dec_fx"), b, k16=True),
                          check_fx.run_dec(os.path.join(fx, "sotto_dec_fx_arm"), b, k16=True))

def test_v38_unvoiced_frames_decode_at_50_hz_spacing():
    """v3.8 carries a hiss index in the pitch field of all-unvoiced frames; the decoder must
    not read it as a pitch (sim/hiss_band.py SottoV38._model_hook, run by SottoV0.decode)."""
    import hiss_band as hb
    rng = np.random.default_rng(0)
    bits = np.zeros((6, 48), np.int8)
    bits[:, 2:9] = rng.integers(0, 2, (6, 7))        # UU frames, random 7-bit field
    bits[:, 9:14] = [1, 0, 1, 0, 0]                   # energy code 20
    m = hb.SottoV38(vowel="speaker", vowel_shape="fine", blend=True).decode(bits)
    assert np.all(m["voiced"] == 0)
    assert np.allclose(m["Wo"], hb.W50) and np.all(m["L"] == 80)
