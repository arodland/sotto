"""Measure a transmitter's modulation path, so the softmodem can pre-compensate it.

`signal()` makes a test transmission: 0.5 s of a 1 kHz reference tone, then a periodic
multitone (5 Hz spacing, 5 Hz to 6 kHz, Schroeder phases). Send it through the radio exactly
as the modem will (same input, same levels) and record a *flat* receiver: an FM receiver's
9600 bps data output (for example the FTM-300D's DATA jack) or an SDR's IQ. `fit()` then
returns the end-to-end response H(f) = received / sent, with the delay removed and |H| = 1
at 1 kHz, as a Measured object for `fsk.modulate(..., comp=...)`.

What the recording also tells you:
  - polarity: whether positive audio in gave positive frequency (only meaningful with an SDR
    or a receiver whose own polarity you know),
  - deviation: with IQ input (Hz units), how many Hz of deviation the reference tone made.

The receiver's own response is in the measurement too, so use the flattest one you have.
"""
import json
from dataclasses import dataclass

import numpy as np
from scipy import signal as sps

from .fsk import FS, resample

PERIOD = 0.2           # s; 5 Hz tone spacing
F_LO, F_HI = 5.0, 6000.0
TONE_S, GAP_S, PERIODS = 0.5, 0.25, 30


def _multitone(fs=FS):
    n = int(round(PERIOD * fs))
    k = np.arange(int(F_LO * PERIOD), int(F_HI * PERIOD) + 1)
    ph = np.pi * k * (k - 1) / len(k)          # Schroeder phases: low crest factor
    X = np.zeros(n // 2 + 1, complex)
    X[k] = np.exp(1j * ph)
    x = np.fft.irfft(X, n)
    return x / np.max(np.abs(x)), X, k


def signal(level=0.5, fs=FS):
    """-> audio: reference tone, gap, PERIODS periods of the multitone (peak = level)."""
    t = np.arange(int(TONE_S * fs)) / fs
    tone = np.sin(2 * np.pi * 1000 * t)
    mt, _, _ = _multitone(fs)
    x = np.concatenate([np.zeros(int(0.3 * fs)), tone, np.zeros(int(GAP_S * fs)),
                        np.tile(mt, PERIODS), np.zeros(int(0.3 * fs))])
    return x * level


@dataclass
class Measured:
    freqs: np.ndarray          # Hz
    h: np.ndarray              # complex response, |h(1 kHz)| = 1, delay removed
    polarity: int = 1
    tone_peak: float = None    # amplitude of the received 1 kHz reference (Hz for IQ input)
    sent_level: float = None

    def response(self, f):
        f = np.asarray(f, float)
        mag = np.interp(f, self.freqs, np.abs(self.h), left=abs(self.h[0]), right=abs(self.h[-1]))
        ph = np.interp(f, self.freqs, np.unwrap(np.angle(self.h)))
        # below the lowest measured tone, keep falling like a first-order high-pass
        lo = f < self.freqs[0]
        mag = np.where(lo, mag * f / self.freqs[0], mag)
        return mag * np.exp(1j * ph)

    def save(self, path):
        json.dump({"freqs": self.freqs.tolist(), "re": self.h.real.tolist(),
                   "im": self.h.imag.tolist(), "polarity": self.polarity,
                   "tone_peak": self.tone_peak, "sent_level": self.sent_level},
                  open(path, "w"), indent=1)

    @classmethod
    def load(cls, path):
        d = json.load(open(path))
        return cls(np.array(d["freqs"]), np.array(d["re"]) + 1j * np.array(d["im"]),
                   d.get("polarity", 1), d.get("tone_peak"), d.get("sent_level"))

    def summary(self):
        out = []
        for f in (20, 50, 100, 200, 300, 500, 1000, 2000, 3000, 4000, 5000):
            if self.freqs[0] <= f <= self.freqs[-1]:
                out.append(f"{f} Hz {20 * np.log10(abs(self.response(np.array([f]))[0]) + 1e-12):+.1f} dB")
        return ", ".join(out)


def _min_phase(mag):
    """Minimum-phase angle for magnitudes on a uniform grid from DC to Nyquist."""
    n = 2 * (len(mag) - 1)
    c = np.fft.irfft(np.log(np.maximum(mag, 1e-6)), n)
    w = np.zeros(n)
    w[0], w[1:n // 2], w[n // 2] = 1, 2, 1
    return np.angle(np.exp(np.fft.rfft(c * w, n)))


def _polarity(f, h, band):
    """+1 or -1: the radio's filters are minimum phase, so the measured phase minus the
    minimum phase for the measured magnitude is a delay (removed already) plus 0 or pi."""
    df = f[1] - f[0]
    grid = np.arange(0, 4 * f[-1] + df, df)        # pad well past the band, then Nyquist
    mag = np.interp(grid, f, np.abs(h), left=abs(h[0]), right=abs(h[-1]) * 1e-3)
    mp = np.interp(f, grid, _min_phase(mag))
    d = np.angle(h) - mp
    d = d[band] - np.polyfit(f[band], np.unwrap(d[band]), 1)[0] * f[band]
    return 1 if np.cos(np.angle(np.mean(np.exp(1j * d)))) >= 0 else -1


def fit(x, fs, sent_level=0.5):
    """Recording of signal() through the radio -> Measured."""
    y = resample(np.asarray(x, float), fs, FS)
    mt, X, k = _multitone(FS)
    n = len(mt)
    # find the multitone: correlate against one period, take the strongest peak
    c = sps.correlate(y, mt, "valid")
    first = int(np.argmax(np.abs(c[:len(c) - (PERIODS - 1) * n])))
    # back up to the first period that correlates nearly as well
    while first - n >= 0 and abs(c[first - n]) > 0.7 * abs(c[first]):
        first -= n
    hs = []
    f = k / PERIOD
    band = (f > 300) & (f < 2500)
    for p in range(2, PERIODS - 1):            # skip the first periods (filter transients)
        s = first + p * n
        if s + n > len(y):
            break
        Y = np.fft.rfft(y[s:s + n])[k]
        h = Y / X[k]
        ph = np.unwrap(np.angle(h))
        slope = np.polyfit(f[band], ph[band], 1)[0]   # residual delay (also clock drift)
        hs.append(h * np.exp(-1j * slope * f))
    if not hs:
        raise ValueError("calibration signal not found in the recording")
    h = np.mean(hs, axis=0)
    i1k = np.argmin(abs(f - 1000))
    pol = _polarity(f, h, band)
    h = pol * h / abs(h[i1k])
    # the reference tone's amplitude
    t0 = first - int(GAP_S * FS) - int(TONE_S * FS)
    seg = y[max(0, t0 + int(0.1 * FS)):max(0, t0 + int(0.4 * FS))]
    peak = float(np.sqrt(2) * np.std(seg)) if len(seg) else None
    return Measured(f, h, pol, peak, sent_level)
