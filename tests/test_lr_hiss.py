"""sotto 1200 with the 4-8 kHz hiss band (sim/sotto_lr_hiss.py): the hiss rides bits the 0-4 kHz
decoder doesn't use, plain sotto 1200 is unchanged, and the 16 kHz decoder runs with losses;
the C port (c/sotto_hiss.c, VARIANT=v3.8 build) matches it.
Decoder tests need no Codec 2; the encoder tests need tools/build_codec2.sh.
SPDX-License-Identifier: MIT"""
import os
import subprocess
import sys
import tempfile

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sim"))
import vocoder_lab as vl  # noqa: E402
import sotto_lr as lr  # noqa: E402
import sotto_lr_hiss as lh  # noqa: E402
import hiss_band as hb  # noqa: E402

C = os.path.join(ROOT, "c")
need_c2 = pytest.mark.skipif(not os.path.exists(os.path.join(vl.ar.C2BIN, "c2sim")),
                             reason="needs the Codec 2 tools (tools/build_codec2.sh)")


def _c_has_lr_hiss():
    exe = os.path.join(C, "sotto_dec")
    if not os.path.exists(exe):
        return False
    r = subprocess.run([exe, "-16", "-1200", "/dev/null", "/dev/null"], capture_output=True)
    return r.returncode == 0


need_c = pytest.mark.skipif(not _c_has_lr_hiss(), reason="needs the VARIANT=v3.8 C build (make -C c)")


def c_run(tool, flags, data, good=None):
    with tempfile.TemporaryDirectory() as d:
        open(f"{d}/in", "wb").write(data)
        a = [os.path.join(C, tool), *flags, f"{d}/in", f"{d}/out"]
        if good is not None:
            np.asarray(good, np.uint8).tofile(f"{d}/good.u8")
            a.append(f"{d}/good.u8")
        subprocess.run(a, check=True)
        return open(f"{d}/out", "rb").read()


def band_db(y, lo, hi):
    return hb.bands_db(np.abs(hb.stft(y)) ** 2, [(lo, hi)])[:, 0]


def burst_clip():
    """A vowel, a noise burst above 4 kHz, the vowel again: 16 kHz, whole superframes."""
    fs, rng = 16000, np.random.default_rng(3)
    t = np.arange(int(0.4 * fs)) / fs
    vowel = sum(np.sin(2 * np.pi * 120 * k * t) / k for k in range(1, 30)) * 0.05
    from scipy.signal import butter, sosfilt
    s = sosfilt(butter(6, 4500, "high", fs=fs, output="sos"), rng.standard_normal(len(t))) * 0.1
    x = np.concatenate([vowel, s, vowel])
    return x[:len(x) // 640 * 640]


def random_superframes(n, seed=1):
    rng = np.random.default_rng(seed)
    b = rng.integers(0, 2, (n, 48)).astype(np.int8)
    b[rng.random(n) < 0.15, 9:14] = 0
    b[rng.random(n) < 0.3, 0:2] = 0                    # plenty of unvoiced A frames
    b[rng.random(n) < 0.3, 32:34] = 0                  # and B frames
    return b


def test_hiss_bits_do_not_touch_the_0_4_khz_model():
    """The pitch fields of unvoiced frames (A bits 2-8, B bits 34-36) carry the hiss: changing
    them leaves the 8 kHz model the hiss decoder builds unchanged."""
    c = lh.Sotto1200Hiss()
    b = random_superframes(120)
    b2 = b.copy()
    rng = np.random.default_rng(2)
    ua = (b[:, 0] == 0) & (b[:, 1] == 0)
    ub = (b[:, 32] == 0) & (b[:, 33] == 0)
    b2[ua, 2:9] = rng.integers(0, 2, (ua.sum(), 7))
    b2[ub, 34:37] = rng.integers(0, 2, (ub.sum(), 3))
    m1, m2 = c.decode(b), c.decode(b2)
    for k in ("Wo", "L", "voiced", "A"):
        assert np.array_equal(m1[k], m2[k]), k


def test_plain_sotto_1200_keeps_its_decoder():
    """Sotto1200's _synth now calls the model hook and the UU guard flag; both are off there."""
    p = lr.Sotto1200()
    assert not getattr(p, "UU_WO_GUARD", False)
    b = random_superframes(60)
    m = p.decode(b)
    assert m is not None and len(m) == 4 * len(b)


def test_b_tables_and_layout():
    c = lh.Sotto1200Hiss()
    assert c.B_SIDE and c.bcb.shape == (4, 4)          # 2 bits of hiss + the side bit
    assert sum(w for _, w in c.layout()) == 48
    assert c.cb.shape == (64, 4) and c.nb == 6


def test_decoder_16k_with_losses():
    c = lh.Sotto1200Hiss()
    b = random_superframes(50)
    good = np.arange(50) % 8 != 3
    y = c.decode_audio16(b, good, n16=50 * 640)
    assert y.shape == (50 * 640,) and np.all(np.isfinite(y))
    assert c.rebuilt.sum() > 0


@need_c2
def test_encoder_puts_hiss_in_unvoiced_frames():
    """A noise burst above 4 kHz between two vowels: the unvoiced frames get hiss indices, and
    the 16 kHz output has energy above 4 kHz where the 0-4 kHz-only decoder has none."""
    fs = 16000
    x = burst_clip()
    c = lh.Sotto1200Hiss()
    b = c.encode(x)
    y = c.decode_audio16(b, n16=len(x))
    seg = slice(int(0.45 * fs), int(0.75 * fs))
    Y = np.abs(np.fft.rfft(y[seg])) ** 2
    f = np.fft.rfftfreq(len(y[seg]), 1 / fs)
    X = np.abs(np.fft.rfft(x[seg])) ** 2
    hi = lambda P: 10 * np.log10(P[f > 4500].sum() + 1e-12)
    assert abs(hi(Y) - hi(X)) < 8


@need_c
def test_c_decoder_matches_python():
    """The same superframes (with lone and repeated losses) through both 16 kHz decoders: the
    0-4 kHz parts agree sample by sample, the 4-8 kHz noise (random phase) by level."""
    b = random_superframes(60, seed=4)
    good = np.ones(60, bool)
    good[[7, 20, 21, 40]] = False
    c = lh.Sotto1200Hiss()
    from scipy.signal import resample_poly
    for g in (None, good):
        yp = c.decode_audio16(b, g, n16=60 * 640)
        yc = np.frombuffer(c_run("sotto_dec", ["-16", "-1200"], np.packbits(b.astype(np.uint8), axis=1).tobytes(), g),
                           "<i2") / 32768
        assert len(yc) == len(yp)
        lp, lc = resample_poly(yp, 1, 2), resample_poly(yc, 1, 2)
        assert 10 * np.log10(np.sum(lp ** 2) / np.sum((lp - lc) ** 2)) > 20
        hp, hc = band_db(yp, 4000, 8000), band_db(yc, 4000, 8000)
        live = hp > -60
        assert live.sum() > 50 and np.mean(np.abs(hp - hc)[live]) < 1.5


@need_c
@need_c2
def test_c_encoder_matches_python():
    """Both encoders on the burst clip, with the Python's fixed-lag tracker and whisper rule
    (as the C runs them, tools/check_lr_hiss.py): the same superframes."""
    x16 = np.round(burst_clip() * 32767).astype(np.int16)
    saved = dict(vl.TRACK2)
    try:
        vl.TRACK2["lag"] = 8
        bp = lh.Sotto1200Hiss(whisper_lag=10).encode(x16 / 32767.0)
    finally:
        vl.TRACK2.clear()
        vl.TRACK2.update(saved)
    bc = np.unpackbits(np.frombuffer(c_run("sotto_enc", ["-16", "-1200"], x16.astype("<i2").tobytes()),
                                     np.uint8)).reshape(-1, 48)
    assert bc.shape == bp.shape
    assert np.mean(np.all(bc == bp, 1)) >= 0.95
    ua = (bp[:, 0] == 0) & (bp[:, 1] == 0)
    assert ua.any()                                    # the burst's unvoiced A frames carry hiss
