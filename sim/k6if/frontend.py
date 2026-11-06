"""The K6's GP0 receive path as a model (docs/k6-if-modem.md), for the IF softmodem study.

  baseband (complex, around the IF) -> chip IF filter -> AGC -> real IF at FIF -> 1-bit
  sigma-delta at FCLK -> DMA reads at FDMA -> ones counted L at a time (gp0dma) -> x[n]

The sigma-delta (sdm.c) is built with cc on first use. MIT licence."""
import ctypes
import os
import subprocess

import numpy as np
from scipy import signal

HERE = os.path.dirname(os.path.abspath(__file__))
FIF = 26e6 / 1536          # 16.927 kHz: where GP0 shows a carrier
FCLK = 6.5e6               # GP0's run lengths are multiples of ~156 ns (run 6)
FDMA = 5.49e6              # DMA channel 1 flat out (run 8)
L = 64                     # gp0dma's working set-up: 85.8 kHz out
AMP = 0.5                  # carrier density 25-75% (runs 4-5)
_lib = None


def lib():
    global _lib
    if _lib is None:
        so = os.path.join(HERE, "build", "sdm.so")
        src = os.path.join(HERE, "sdm.c")
        if not os.path.exists(so) or os.path.getmtime(so) < os.path.getmtime(src):
            os.makedirs(os.path.dirname(so), exist_ok=True)
            subprocess.run(["cc", "-O2", "-shared", "-fPIC", "-o", so, src, "-lm"], check=True)
        _lib = ctypes.CDLL(so)
        d, i, p = ctypes.c_double, ctypes.c_int, ctypes.c_void_p
        _lib.k6_frontend.argtypes = [p, p, i, d, d, d, d, d, i, i, p, i, ctypes.c_uint]
        _lib.k6_frontend.restype = i
    return _lib


def sdm(z, fz, fdma=FDMA, L=L, order=2, jit=0.3, seed=1, fif=FIF, fclk=FCLK):
    """z: complex baseband at fz (already at the AGC level). Returns int counts 2*ones-L (or raw
    0/1 samples with L=0) at fdma/L."""
    zr = np.ascontiguousarray(z.real, float)
    zi = np.ascontiguousarray(z.imag, float)
    nmax = int(len(z) / fz * fdma / max(L, 1)) + 4
    out = np.zeros(nmax, np.int32)
    n = lib().k6_frontend(zr.ctypes.data, zi.ctypes.data, len(z), fz, fif, fclk, fdma, jit,
                          order, L, out.ctypes.data, nmax, seed)
    return out[:n]


def chip_filter(z, fz, bw):
    """the FD6818's channel filter, as a 6th-order Butterworth low-pass on the baseband (bw:
    the one-sided -3 dB edge, Hz)"""
    sos = signal.butter(6, bw, fs=fz, output="sos")
    return signal.sosfilt(sos, z)


def receive(s, fz, cn0_db=None, bw=8000.0, seed=1, floor=0.05, ideal=False, **kw):
    """s: the received signal, complex baseband at fz, unit amplitude. cn0_db: C/N0 in dB-Hz
    (None: no noise). Noise and signal go through the chip filter, then the AGC: at most the
    gain that puts the receiver's own noise at `floor` rms on the real IF (gp0tone run 4: with
    no carrier the code 13 density moves "a few %", i.e. 0.03-0.07; see the study), and less
    once the total would pass a strong carrier's AMP. floor None: the total is always held at
    AMP (an AGC that normalises noise too). ideal: skip the sigma-delta (the receivers' bound),
    returning complex baseband at FDMA / L / 4 instead of counts."""
    rng = np.random.default_rng(seed)
    z = np.asarray(s, complex)
    nz = None
    if cn0_db is not None:
        n0 = 10 ** (-cn0_db / 10)                 # C = 1 (unit amplitude carrier)
        nz = chip_filter(rng.normal(0, np.sqrt(n0 * fz / 2), (len(z), 2)) @ [1, 1j], fz, bw)
    z = chip_filter(z, fz, bw)
    if nz is not None:
        z = z + nz
    tot = np.sqrt(np.mean(np.abs(z) ** 2))
    g = AMP / tot
    if floor is not None and nz is not None:
        g = min(g, floor * np.sqrt(2) / np.sqrt(np.mean(np.abs(nz) ** 2)))
    z = z * g
    if ideal:
        fo = kw.get("fdma", FDMA) / L / 4
        from fractions import Fraction
        r = Fraction(fo / fz).limit_denominator(1000)
        return signal.resample_poly(z, r.numerator, r.denominator)
    return sdm(z, fz, seed=seed, **kw)
