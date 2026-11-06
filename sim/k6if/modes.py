"""General audio-tone CPFSK modes for the K6 IF study (docs/k6-if-modem.md): M tones at a baud
rate, phase-continuous, sent through the IC-7100 model, received through the GP0 model, and
detected three ways: FM discriminator + audio tone filters, IF matched filter (noncoherent), and
the same with a rate-1/2 or 1/3 K=7 convolutional code (modem/fec.py) on soft values.
MIT licence."""
import numpy as np
from scipy import signal

from modem import fec
from . import study as st

FS4 = st.FS4


class Mode:
    def __init__(self, tones, baud, level=0.14, name=None):
        self.tones = np.asarray(tones, float)
        self.M = len(tones)
        self.k = int(np.log2(self.M))
        self.baud = baud
        self.sps = int(round(FS4 / baud))
        assert abs(self.sps * baud - FS4) < 1e-6
        self.level = level
        self.name = name or f"{self.M}FSK {baud:.0f} baud ({','.join(f'{t:.0f}' for t in tones)})"
        # Gray map: symbol index -> bits
        g = np.arange(self.M) ^ (np.arange(self.M) >> 1)
        self.gray = g
        self.bits_of = np.array([[(g[m] >> (self.k - 1 - j)) & 1 for j in range(self.k)]
                                 for m in range(self.M)])

    def audio(self, syms):
        """symbols -> sender audio and each symbol's audio phase class at its start"""
        spa = st.FSA / self.baud
        n = int(round(len(syms) * spa))
        idx = np.minimum((np.arange(n) / spa).astype(int), len(syms) - 1)
        f = self.tones[syms][idx]
        ph = 2 * np.pi * np.cumsum(f) / st.FSA
        x = np.sin(np.concatenate([[0], ph[:-1]])) * self.level
        # the phase at each symbol start, in cycles mod 1, quantised to 1/24
        starts = np.concatenate([[0], np.cumsum(self.tones[syms] / self.baud)[:-1]]) % 1
        cls = np.round(starts * 24).astype(int) % 24
        return x, cls


class Run:
    """one mode over the link: random symbols, the clean capture for training and timing"""

    def __init__(self, mode, nsym, seed=7, lead=32, floor=0.05, ideal=False):
        self.m, self.floor, self.ideal = mode, floor, ideal
        rng = np.random.default_rng(seed)
        self.syms = rng.integers(0, mode.M, nsym)
        lead_syms = np.tile([0, mode.M - 1], lead // 2)
        allsym = np.concatenate([lead_syms, self.syms, lead_syms[:16]])
        a, cls = mode.audio(allsym)
        self.cls = cls[lead:lead + nsym]
        self.lead = lead
        self.dev = st.ic7100(np.concatenate([np.zeros(st.FSA // 50), a]))
        self.s = st.fm(self.dev)
        self.t0 = st.FSA // 50 / st.FSA
        z0 = self.rx(None)
        self.start = self._timing(z0)
        self.T = self._train(z0)

    def rx(self, cn0, seed=1):
        x = st.fe.receive(self.s, st.FZ, cn0, seed=seed, fdma=st.FDMA, floor=self.floor,
                          ideal=self.ideal)
        return x if self.ideal else st.to_baseband(x.astype(float))

    def seg(self, z, start=None, n=None):
        sps = self.m.sps
        start = self.start if start is None else start
        n = len(self.syms) if n is None else n
        return np.lib.stride_tricks.sliding_window_view(z[start:start + (n + 1) * sps], sps)[::sps][:n]

    def _timing(self, z):
        d = np.concatenate([[0], np.angle(z[1:] * np.conj(z[:-1]))])
        nom = int(round(self.t0 * FS4)) + self.lead * self.m.sps
        best = None
        for off in range(nom - 3 * self.m.sps, nom + 6 * self.m.sps):
            e = self.tone_energy(self.seg(d, off, 300))
            sc = np.mean(e[np.arange(300), self.syms[:300]]) - np.mean(e)
            if best is None or sc > best[0]:
                best = (sc, off)
        return best[1]

    def tone_energy(self, seg):
        k = np.arange(seg.shape[1])
        E = np.exp(-2j * np.pi * np.outer(k, self.m.tones) / FS4)
        return np.abs(seg @ E)

    def _train(self, z):
        seg = self.seg(z)
        seg = seg / (seg[:, :1] / np.abs(seg[:, :1]))
        T = {}
        for m in range(self.m.M):
            for c in np.unique(self.cls[self.syms == m]):
                sel = (self.syms == m) & (self.cls == c)
                if sel.sum() >= 3:
                    T[m, c] = seg[sel].mean(0)
        return T

    # ---- detectors: (n, M) symbol metrics ----
    def met_disc(self, z, bw=None):
        bw = bw or min(8000.0, 0.5 * (np.max(np.abs(self.dev)) * 2 + 2 * self.m.tones.max()) * 1.0)
        sos = signal.butter(6, bw, fs=FS4, output="sos")
        zf = signal.sosfiltfilt(sos, z)
        d = np.concatenate([[0], np.angle(zf[1:] * np.conj(zf[:-1]))])
        return self.tone_energy(self.seg(d))

    def met_if(self, z):
        seg = self.seg(z)
        out = np.zeros((len(seg), self.m.M))
        for (m, c), t in self.T.items():
            out[:, m] = np.maximum(out[:, m], np.abs(seg @ np.conj(t)))
        return out

    # ---- metrics -> decisions ----
    def ser_ber(self, met):
        sym = np.argmax(met, 1)
        b_tx = self.m.bits_of[self.syms].ravel()
        b_rx = self.m.bits_of[sym].ravel()
        return np.mean(sym != self.syms), np.mean(b_tx != b_rx)

    def llr(self, met):
        """max-log soft value per bit, positive = 0 (fec.decode's convention), scaled by the
        metrics' spread"""
        met = met / (np.median(met) + 1e-12)
        L = np.zeros((len(met), self.m.k))
        for j in range(self.m.k):
            b = self.m.bits_of[:, j]
            L[:, j] = met[:, b == 0].max(1) - met[:, b == 1].max(1)
        return L.ravel()


def coded_test(run, met, rate=3, block=240):
    """use the run's bits as coded bits of a rate-1/rate tail-biting code: the transmitted
    symbols are random, so we re-label them as the encoding of random info bits by XOR (the
    channel is symmetric enough for that: a coset code). Returns the info BER and block error rate."""
    tx_bits = run.m.bits_of[run.syms].ravel()
    L = run.llr(met)
    nblk = len(tx_bits) // (rate * block)
    rng = np.random.default_rng(3)
    errs, fer = 0, 0
    for b in range(nblk):
        u = rng.integers(0, 2, block)
        c = fec.encode(u)[:, :rate].ravel() if rate < 3 else fec.encode(u).ravel()
        sl = slice(b * rate * block, (b + 1) * rate * block)
        # the channel saw tx_bits; flip the soft values where the codeword differs from them
        flip = (c != tx_bits[sl])
        llr = np.where(flip, -L[sl], L[sl]).reshape(block, rate)
        full = np.zeros((block, 3))
        full[:, :rate] = llr
        d = fec.decode(full)
        e = int(np.sum(d != u))
        errs += e
        fer += e > 0
    return errs / max(1, nblk * block), fer / max(1, nblk)


def threshold(cn0, ber, target=0.01):
    """the C/N0 where ber (falling with cn0 ascending) last crosses target, log-interpolated"""
    for i in range(len(cn0) - 2, -1, -1):
        if ber[i] >= target >= ber[i + 1]:
            a, b = np.log10(max(ber[i], 1e-5)), np.log10(max(ber[i + 1], 1e-5))
            return cn0[i] + (np.log10(target) - a) / (b - a - 1e-12) * (cn0[i + 1] - cn0[i])
    return None


MODES = {"ffsk1200": ([1200, 1800], 1200, 0.14), "4fsk1200": ([900, 1500, 2100, 2700], 1200, 0.12),
         "msk2400": ([1200, 2400], 2400, 0.14)}


def table(names, floor=0.05, ideal=False, nsym=6000, cn0=(36, 38, 40, 42, 44, 46, 48, 50, 52, 54)):
    """the doc's results table: C/N0 at 1% information BER per mode and receiver"""
    cols = ("disc", "if", "disc+r1/2", "if+r1/2", "if+r1/3")
    print("mode       " + "".join(f"{c:>11s}" for c in cols))
    for name in names:
        tones, baud, lv = MODES[name]
        run = Run(Mode(tones, baud, lv), nsym, floor=floor, ideal=ideal)
        res = {c: [] for c in cols}
        for c in cn0:
            z = run.rx(c, seed=c)
            md, mi = run.met_disc(z), run.met_if(z)
            res["disc"].append(run.ser_ber(md)[1])
            res["if"].append(run.ser_ber(mi)[1])
            res["disc+r1/2"].append(coded_test(run, md, 2)[0])
            res["if+r1/2"].append(coded_test(run, mi, 2)[0])
            res["if+r1/3"].append(coded_test(run, mi, 3)[0])
        th = [threshold(list(cn0), res[c]) for c in cols]
        print(f"{name:10s} " + "".join(f"{t:11.1f}" if t else f"{'-':>11s}" for t in th), flush=True)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="C/N0 (dB-Hz) at 1% information BER")
    ap.add_argument("modes", nargs="*", default=["ffsk1200", "4fsk1200"])
    ap.add_argument("--floor", type=float, default=0.05)
    ap.add_argument("--ideal", action="store_true")
    a = ap.parse_args()
    table(a.modes, a.floor, a.ideal)
