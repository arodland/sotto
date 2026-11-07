"""The K6 speaker sweep: the C schedule (c/k6/sotto_pager.c sp_sweep_pwm, via pager_host -w) and
tools/k6_sweep.py, which must find it in a recording and read back a known response: the PWM
held at 16 kHz, through the schematic's RC filter, into a 44.1 kHz recording with noise and a
delay. Needs make -C c/k6."""
import os
import subprocess
import sys

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOST = os.path.join(ROOT, "c", "k6", "pager_host")
sys.path.insert(0, os.path.join(ROOT, "tools"))
import k6_sweep  # noqa: E402

pytestmark = pytest.mark.skipif(not os.path.exists(HOST), reason="needs make -C c/k6")


def sweep_pwm(tmp_path):
    out = tmp_path / "sw.u16"
    subprocess.run([HOST, "-w", str(out)], check=True)
    return np.fromfile(out, "<u2").astype(float)


def test_schedule(tmp_path):
    x = sweep_pwm(tmp_path)
    fs = k6_sweep.RATE
    assert abs(len(x) / fs - (1 + 14 * 1.25)) < 0.02
    assert np.ptp(x[:int(0.99 * fs)]) == 0 and x[0] == 1000
    for j, f in enumerate(k6_sweep.HZ):
        a = int((1 + 1.25 * j + 0.1) * fs)
        seg = x[a:a + int(0.8 * fs)] - 1000
        assert 230 <= seg.max() <= 250 and -250 <= seg.min() <= -230   # peak, as sampled
        sp = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
        assert abs(np.argmax(sp) * fs / len(seg) - f) < 2
        g = x[int((2.05 + 1.25 * j) * fs):int((2.2 + 1.25 * j) * fs)]
        assert np.ptp(g) == 0                       # the gap is silent


def test_reads_back_a_known_response(tmp_path):
    from scipy.signal import bilinear, lfilter, resample_poly
    x = (sweep_pwm(tmp_path) - 1000) / 1000
    y = np.repeat(x, 3)                          # 16 kHz values held at 48 kHz
    # the ladder as an analog filter, matched at 48 kHz (its poles sit at 1.5 and 8.7 kHz)
    r1, c1, r2, c2 = 4.7e3, 3.9e-9, 4.7e3, 22e-9
    b, a = bilinear([1.0], [r1 * c1 * r2 * c2, r1 * c1 + r2 * c2 + r1 * c2, 1.0], 48000)
    y = lfilter(b, a, y)
    y = resample_poly(y, 147, 160)               # 44.1 kHz
    rng = np.random.default_rng(1)
    y = np.concatenate([np.zeros(int(1.7 * 44100)), y, np.zeros(44100)])
    y = y + rng.normal(0, 1e-3, len(y))
    import soundfile as sf
    sf.write(tmp_path / "rec.wav", y, 44100)
    r = k6_sweep.analyse(str(tmp_path / "rec.wav"))
    assert abs(r["start_s"] - 2.7) < 0.03
    from scipy.signal import freqz
    hz = np.array(k6_sweep.HZ, float)
    _, h = freqz(b, a, worN=np.append(hz, 1000.0), fs=48000)
    want = 20 * np.log10(np.abs(h[:-1]) / np.abs(h[-1]))
    for t, w in zip(r["tones"], want):
        assert abs(t["db"] - w) < 0.7, (t, w)
        assert t["snr_db"] > 30
