"""sotto 1200 with the 4-8 kHz hiss band (sim/sotto_lr_hiss.py): the hiss rides bits the 0-4 kHz
decoder doesn't use, plain sotto 1200 is unchanged, and the 16 kHz decoder runs with losses.
Decoder tests need no Codec 2; the encoder test needs tools/build_codec2.sh.
SPDX-License-Identifier: MIT"""
import os
import sys

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sim"))
import vocoder_lab as vl  # noqa: E402
import sotto_lr as lr  # noqa: E402
import sotto_lr_hiss as lh  # noqa: E402


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


@pytest.mark.skipif(not os.path.exists(os.path.join(vl.ar.C2BIN, "c2sim")),
                    reason="needs the Codec 2 tools (tools/build_codec2.sh)")
def test_encoder_puts_hiss_in_unvoiced_frames():
    """A noise burst above 4 kHz between two vowels: the unvoiced frames get hiss indices, and
    the 16 kHz output has energy above 4 kHz where the 0-4 kHz-only decoder has none."""
    fs, rng = 16000, np.random.default_rng(3)
    t = np.arange(int(0.4 * fs)) / fs
    vowel = sum(np.sin(2 * np.pi * 120 * k * t) / k for k in range(1, 30)) * 0.05
    from scipy.signal import butter, sosfilt
    s = sosfilt(butter(6, 4500, "high", fs=fs, output="sos"), rng.standard_normal(len(t))) * 0.1
    x = np.concatenate([vowel, s, vowel])
    x = x[:len(x) // 640 * 640]
    c = lh.Sotto1200Hiss()
    b = c.encode(x)
    y = c.decode_audio16(b, n16=len(x))
    seg = slice(int(0.45 * fs), int(0.75 * fs))
    Y = np.abs(np.fft.rfft(y[seg])) ** 2
    f = np.fft.rfftfreq(len(y[seg]), 1 / fs)
    X = np.abs(np.fft.rfft(x[seg])) ** 2
    hi = lambda P: 10 * np.log10(P[f > 4500].sum() + 1e-12)
    assert abs(hi(Y) - hi(X)) < 8
