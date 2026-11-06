"""The K6's GP0 IF path for the FT8/WSPR study (docs/k6-ft8-wspr.md): an SSB signal from the
IC-7100 (USB), the K6's receiver noise, the FD6818's channel filter and AGC, the sigma-delta on
GP0, the DMA and the 64-sample counts, as in the IF softmodem study (docs/k6-if-modem.md).

Its knobs and their defaults are that study's: IF 26 MHz / 1536, GP0 clock 6.5 MHz, DMA
5.49 MS/s with 0.3 of jitter, L = 64, a carrier at density 25-75%, the AGC holding the
receiver noise at `floor` rms on the IF (0.05; 0.03 is the pessimistic case). MIT licence."""
import ctypes
import os
import subprocess
import tempfile

import numpy as np
from scipy import signal

from . import ft8_wav

HERE = os.path.dirname(os.path.abspath(__file__))
FIF = 26e6 / 1536
FCLK = 6.5e6
FDMA = 5.49e6
L = 64
AMP = 0.5
_lib = None


def lib():
    global _lib
    if _lib is None:
        so = os.path.join(HERE, "build", "gp0sdm.so")
        src = os.path.join(HERE, "gp0sdm.c")
        if not os.path.exists(so) or os.path.getmtime(so) < os.path.getmtime(src):
            os.makedirs(os.path.dirname(so), exist_ok=True)
            tmp = so + ".%d" % os.getpid()
            subprocess.run(["cc", "-O2", "-shared", "-fPIC", "-o", tmp, src, "-lm"], check=True)
            os.replace(tmp, so)
        _lib = ctypes.CDLL(so)
        d, i, p = ctypes.c_double, ctypes.c_int, ctypes.c_void_p
        _lib.gp0_counts.argtypes = [p, p, i, d, d, d, d, d, i, p, i, ctypes.c_uint]
        _lib.gp0_counts.restype = i
    return _lib


def counts(z, fz, fdma=FDMA, jit=0.3, seed=1, fif=FIF, L=L):
    zr = np.ascontiguousarray(z.real, float)
    zi = np.ascontiguousarray(z.imag, float)
    nmax = int(len(z) / fz * fdma / L) + 4
    out = np.zeros(nmax, np.int8)
    n = lib().gp0_counts(zr.ctypes.data, zi.ctypes.data, len(z), fz, fif, FCLK, fdma, jit, L,
                         out.ctypes.data, nmax, seed)
    return out[:n]


def k6_if(sig, fz, snr_db, floor=0.05, seed=1, bw=8000.0, ideal=False):
    """sig: the transmitted SSB signal as complex baseband at fz (unit power while on); noise
    for snr_db in 2500 Hz; the chip filter and AGC. Returns the complex IF baseband at the
    AGC's level (ideal: the receiver noise only, no sigma-delta) """
    rng = np.random.default_rng(seed + 1000)
    n0 = 1.0 / (2500 * 10 ** (snr_db / 10))
    nz = rng.normal(0, np.sqrt(n0 * fz / 2), (len(sig), 2)) @ [1, 1j]
    sos = signal.butter(6, bw, fs=fz, output="sos")
    z = signal.sosfilt(sos, sig + nz)
    nzf = signal.sosfilt(sos, nz)
    g = AMP / np.sqrt(np.mean(np.abs(z) ** 2))
    if floor is not None:
        g = min(g, floor * np.sqrt(2) / np.sqrt(np.mean(np.abs(nzf) ** 2)))
    return z * g


def ssb(x, fs, fz=48000):
    """real audio at fs -> the USB signal as complex baseband at fz (positive tones only)"""
    from fractions import Fraction
    a = signal.hilbert(x) / np.sqrt(2)          # the SSB signal has the audio's power
    r = Fraction(fz / fs).limit_denominator(100)
    return signal.resample_poly(a, r.numerator, r.denominator)


def run_counts(c, nb, fmix, opts=(), fdma=FDMA, r2=None, L=L):
    """L = 128: counts of 128 samples into a first CIC of 8 (as gp0rx.h), else 64 into 16"""
    fs = fdma / L
    r1 = 1024 // L
    if L != 64:
        opts = list(opts) + ["r1=%d" % r1, "sh1=%d" % (16 - 2 * (L // 64 - 1))]
    if r2 is None:
        r2 = int(round(fs / r1 / 200))
    with tempfile.NamedTemporaryFile(suffix=".s8", delete=False) as f:
        f.write(c.astype(np.int8).tobytes())
        name = f.name
    try:
        out = subprocess.run([ft8_wav.host(), "counts", name, str(nb), str(r2), repr(fs),
                              repr(fmix)] + list(opts), capture_output=True, text=True,
                             check=True).stdout
    finally:
        os.unlink(name)
    res = []
    for line in out.splitlines():
        p = line.split(None, 5)
        res.append(dict(row=int(p[0]), bin=int(p[1]), snr=int(p[2]), score=int(p[3]),
                        iters=int(p[4]), text=p[5] if len(p) > 5 else ""))
    return res


def ideal_counts(z, fz, fdma=FDMA):
    """the counts a perfect L-sample average of the real IF would give (no sigma-delta)"""
    fs = fdma / L
    t = np.arange(int(len(z) / fz * fs)) / fs
    tz = np.arange(len(z)) / fz
    zi = np.interp(t, tz, z.real) + 1j * np.interp(t, tz, z.imag)
    return np.clip(np.round(L * np.real(zi * np.exp(2j * np.pi * FIF * t))), -64, 64)


def run_ssb(clean, fs, fc, nb, opts, snr, seed=1, floor=0.05, L=L):
    """clean audio (unit power while on) -> USB -> K6 at snr -> counts -> the receiver centred
    on audio fc. floor < 0: no sigma-delta (the front end's own loss). Returns the decodes."""
    fz = 48000
    sig = ssb(clean, fs, fz)
    z = k6_if(sig, fz, snr, floor=abs(floor), seed=seed)
    c = counts(z, fz, seed=seed, L=L) if floor > 0 else ideal_counts(z, fz)
    return run_counts(c, nb, FIF + fc, opts, L=L)
