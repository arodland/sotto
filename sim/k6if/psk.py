"""Linear (SSB) PSK through the K6's GP0 path (docs/k6-if-modem.md): the IC-7100 in USB data
mode sends RRC-shaped BPSK or QPSK on an audio carrier; the K6 keeps its FM set-up (8.46 kHz IF:
the IF chain ahead of the demodulator is linear) and works on the GP0 IF. Compared with the FSK
modes at equal peak power: C/N0 here is relative to a carrier at the transmitter's PEP, as the
FSK study's C/N0 is relative to the FM carrier.

  python3 -m sim.k6if.psk                      C/N0 at 1% information BER per mode
MIT licence."""
import numpy as np
from scipy import signal

from modem import fec
from . import frontend as fe
from . import study as st

FS4 = st.FS4
SSB_BPF = signal.butter(8, [300, 2700], "bandpass", fs=st.FSA, output="sos")


def rrc(sps, alpha=0.5, span=8):
    t = np.arange(-span * sps, span * sps + 1) / sps
    h = np.zeros_like(t)
    for i, x in enumerate(t):
        if abs(x) < 1e-9:
            h[i] = 1 - alpha + 4 * alpha / np.pi
        elif abs(abs(x) - 1 / (4 * alpha)) < 1e-9:
            h[i] = alpha / np.sqrt(2) * ((1 + 2 / np.pi) * np.sin(np.pi / (4 * alpha))
                                        + (1 - 2 / np.pi) * np.cos(np.pi / (4 * alpha)))
        else:
            h[i] = (np.sin(np.pi * x * (1 - alpha)) + 4 * alpha * x * np.cos(np.pi * x * (1 + alpha))) \
                / (np.pi * x * (1 - (4 * alpha * x) ** 2))
    return h / np.sqrt(np.sum(h ** 2))


class PSK:
    def __init__(self, baud=1200, bps=1, fc=1500.0, alpha=0.5, diff=False):
        self.baud, self.bps, self.fc, self.alpha, self.diff = baud, bps, fc, alpha, diff
        self.sps = int(round(FS4 / baud))
        n = (2 if bps == 1 else 4)
        self.name = f"{'D' if diff else ''}{'BPSK' if bps == 1 else 'QPSK'} {baud} baud"
        self.M = n

    def symbols(self, bits):
        b = np.asarray(bits).reshape(-1, self.bps)
        if self.bps == 1:
            s = 1 - 2.0 * b[:, 0]
        else:                                   # Gray QPSK
            s = ((1 - 2.0 * b[:, 0]) + 1j * (1 - 2.0 * b[:, 1])) / np.sqrt(2)
        if self.diff:                           # differential: each symbol rotates the last
            s = np.cumprod(np.concatenate([[1], s]))[1:]
        return s

    def audio(self, syms):
        """the IC-7100's input: RRC pulses on an audio carrier at 48 kHz"""
        spa = st.FSA // self.baud
        up = np.zeros(len(syms) * spa, complex)
        up[::spa] = syms
        bb = np.convolve(up, rrc(spa, self.alpha), "same")
        t = np.arange(len(bb)) / st.FSA
        return np.real(bb * np.exp(2j * np.pi * self.fc * t))


def ssb(audio):
    """the IC-7100 in USB: its audio filter, then the upper sideband, peak (PEP) scaled to 1"""
    a = signal.sosfilt(SSB_BPF, audio)
    z = signal.hilbert(a)
    z = fe.signal.resample_poly(z, st.FZ // st.FSA, 1)
    return z / np.max(np.abs(z))


class Run:
    def __init__(self, mode, nbits, seed=7, floor=0.05, ideal=False, f_off=0.0):
        self.m, self.floor, self.ideal = mode, floor, ideal
        rng = np.random.default_rng(seed)
        self.bits = rng.integers(0, 2, nbits)
        lead = np.ones(32 * mode.bps, int)
        lead[1::2] = 0
        allbits = np.concatenate([lead, self.bits, lead])
        sy = mode.symbols(allbits)
        self.sy = sy
        self.nlead = 32
        a = mode.audio(sy)
        self.s = ssb(np.concatenate([np.zeros(st.FSA // 50), a]))
        if f_off:
            self.s = self.s * np.exp(2j * np.pi * f_off * np.arange(len(self.s)) / st.FZ)
        self.mf = rrc(mode.sps, mode.alpha)
        z0 = self.rx(None)
        self.t0, self.g = self._timing(z0)

    def rx(self, cn0, seed=1):
        x = fe.receive(self.s, st.FZ, cn0, seed=seed, fdma=st.FDMA, floor=self.floor,
                       ideal=self.ideal)
        z = x if self.ideal else st.to_baseband(x.astype(float))
        n = np.arange(len(z))
        return np.convolve(z * np.exp(-2j * np.pi * self.m.fc * n / FS4), self.mf, "same")

    def _timing(self, y):
        best = None
        n = len(self.sy)
        for off in range(0, 6 * self.m.sps + 40 * self.m.sps):
            idx = off + np.arange(n) * self.m.sps
            if idx[-1] >= len(y):
                break
            c = np.vdot(self.sy, y[idx])
            if best is None or abs(c) > best[0]:
                best = (abs(c), off, c / np.vdot(self.sy, self.sy))
        return best[1], best[2]

    def samples(self, y):
        idx = self.t0 + np.arange(len(self.sy)) * self.m.sps
        return y[idx][self.nlead:self.nlead + len(self.bits) // self.m.bps]

    def soft_coherent(self, y, pll=True, bw=0.02):
        """coherent: the channel's gain and phase from the clean capture's fit (the receiver's
        carrier loop converged), then optionally a decision-directed loop on top"""
        r = self.samples(y) / self.g
        if self.m.diff:
            # coherent detection of the differential symbols, then differential decoding
            r = r * 1
        if pll:
            ph, out = 0.0, np.zeros_like(r)
            for k, v in enumerate(r):
                v = v * np.exp(-1j * ph)
                out[k] = v
                d = np.sign(v.real) + (1j * np.sign(v.imag) if self.m.bps == 2 else 0)
                ph += bw * np.angle(v * np.conj(d))
            r = out
        if self.m.diff:
            prev = np.concatenate([[self.sy[self.nlead - 1] / abs(self.sy[self.nlead - 1])], r[:-1]])
            r = r * np.conj(prev)
        return self._llr(r)

    def soft_diff(self, y):
        """differential detection: no carrier phase needed (the symbols must be sent differentially)"""
        r = self.samples(y)
        prev = np.concatenate([[self.sy[self.nlead - 1] * self.g], r[:-1]])
        d = r * np.conj(prev) / (abs(self.g) ** 2)
        return self._llr(d)

    def _llr(self, r):
        if self.m.bps == 1:
            return r.real
        return np.stack([r.real, r.imag], 1).ravel()


def ber_coded(bits, llr, rate=2, block=240):
    """information BER with the rate-1/rate tail-biting K=7 code, by the coset trick of
    modes.coded_test (the bits sent are random; each block's codeword is laid over them)"""
    raw = np.mean((llr < 0).astype(int) != bits[:len(llr)])
    n = len(llr) // (rate * block)
    rng = np.random.default_rng(3)
    errs = 0
    for b in range(n):
        u = rng.integers(0, 2, block)
        c = fec.encode(u)[:, :rate].ravel()
        sl = slice(b * rate * block, (b + 1) * rate * block)
        flip = c != bits[sl]
        L = np.where(flip, -llr[sl], llr[sl]).reshape(block, rate)
        full = np.zeros((block, 3))
        full[:, :rate] = L
        errs += int(np.sum(fec.decode(full) != u))
    return raw, errs / max(1, n * block)


def main():
    from .modes import threshold
    cn0 = [28, 30, 32, 34, 36, 38, 40, 42, 44]
    modes = [PSK(1200, 1, diff=True), PSK(1200, 2, diff=True), PSK(600, 2, diff=True)]
    print("C/N0 dB-Hz (re PEP) at 1% information BER")
    print("mode                 info b/s  coh raw   diff raw  coh+r1/2  diff+r1/2")
    for m in modes:
        run = Run(m, 8000)
        res = {k: [] for k in ("cr", "dr", "cc", "dc")}
        for c in cn0:
            y = run.rx(c, seed=c)
            sc, sd = run.soft_coherent(y), run.soft_diff(y)
            b = run.bits[:len(sc)]
            r1, c1 = ber_coded(b, sc)
            r2, c2 = ber_coded(b, sd)
            for k, v in zip(("cr", "dr", "cc", "dc"), (r1, r2, c1, c2)):
                res[k].append(v)
        th = [threshold(cn0, res[k]) for k in ("cr", "dr", "cc", "dc")]
        rate = m.baud * m.bps
        print(f"{m.name:20s} {rate:5d}/{rate // 2:<5d}" + "".join(f"{t:10.1f}" if t else f"{'-':>10s}" for t in th),
              flush=True)




class SSBMFSK:
    """M orthogonal tones (spacing = baud), phase-continuous, through the IC-7100 in USB: one tone
    at a time is a constant-envelope carrier, so it keeps the full PEP (the FT8 / JS8 idea)"""

    def __init__(self, M=8, baud=150, f0=1000.0):
        self.M, self.baud, self.f0 = M, baud, f0
        self.k = int(np.log2(M))
        self.sps = int(round(FS4 / baud))
        self.tones = f0 + baud * np.arange(M)
        self.name = f"SSB {M}FSK {baud} baud"
        g = np.arange(M) ^ (np.arange(M) >> 1)
        self.bits_of = np.array([[(g[m] >> (self.k - 1 - j)) & 1 for j in range(self.k)] for m in range(M)])


class MRun:
    def __init__(self, mode, nsym, seed=7, floor=0.05, ideal=False):
        self.m, self.floor, self.ideal = mode, floor, ideal
        rng = np.random.default_rng(seed)
        self.syms = rng.integers(0, mode.M, nsym)
        spa = st.FSA // mode.baud
        f = np.repeat(mode.tones[self.syms], spa)
        a = 0.5 * np.sin(2 * np.pi * np.cumsum(f) / st.FSA)
        self.lead = st.FSA // 50
        self.s = ssb(np.concatenate([np.zeros(self.lead), a, np.zeros(st.FSA // 20)]))
        self.t0 = self._timing(self.rx(None))

    def rx(self, cn0, seed=1):
        x = fe.receive(self.s, st.FZ, cn0, seed=seed, fdma=st.FDMA, floor=self.floor,
                       ideal=self.ideal)
        return x if self.ideal else st.to_baseband(x.astype(float))

    def metrics(self, z, start):
        n = len(self.syms)
        seg = np.lib.stride_tricks.sliding_window_view(z[start:start + (n + 1) * self.m.sps],
                                                       self.m.sps)[::self.m.sps][:n]
        k = np.arange(self.m.sps)
        E = np.exp(-2j * np.pi * np.outer(k, self.m.tones) / FS4)
        return np.abs(seg @ E) ** 2

    def _timing(self, z):
        nom = int(round(self.lead / st.FSA * FS4))
        best = None
        for off in range(nom - self.m.sps // 2, nom + self.m.sps, max(1, self.m.sps // 36)):
            met = self.metrics(z, off)
            sc = np.mean(met[np.arange(len(self.syms)), self.syms])
            if best is None or sc > best[0]:
                best = (sc, off)
        return best[1]

    def llr(self, met):
        met = met / (np.median(met) + 1e-12)
        L = np.zeros((len(met), self.m.k))
        for j in range(self.m.k):
            b = self.m.bits_of[:, j]
            L[:, j] = met[:, b == 0].max(1) - met[:, b == 1].max(1)
        return L.ravel()


def main_mfsk():
    from .modes import threshold
    cn0 = [24, 26, 28, 30, 32, 34, 36, 38, 40]
    print("C/N0 dB-Hz (re PEP) at 1% information BER; Eb/N0 re PEP in brackets")
    for m in (SSBMFSK(8, 150), SSBMFSK(16, 100), SSBMFSK(4, 300)):
        run = MRun(m, 12000 if m.M == 4 else 6000)
        raw, cod = [], []
        bits = m.bits_of[run.syms].ravel()
        for c in cn0:
            met = run.metrics(run.rx(c, seed=c), run.t0)
            L = run.llr(met)
            r, cc = ber_coded(bits, L)
            raw.append(r)
            cod.append(cc)
        rate = m.baud * m.k
        tr, tc = threshold(cn0, raw), threshold(cn0, cod)
        fmt = lambda t, r: f"{t:5.1f} [{t - 10 * np.log10(r):4.1f}]" if t else "    -"
        print(f"{m.name:22s} {rate:4d} b/s raw {fmt(tr, rate)}   r1/2 ({rate // 2} b/s) {fmt(tc, rate / 2)}",
              flush=True)


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["mfsk"]:
        main_mfsk()
    else:
        main()
