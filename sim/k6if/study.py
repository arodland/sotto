"""K6 IF softmodem study (docs/k6-if-modem.md): FM-discriminator vs IF matched-filter
detection of the IC-7100's FFSK, through a model of the K6's GP0 path (frontend.py).

  python3 -m sim.k6if.study ber [--bits N]      BER vs C/N0 for each receiver
MIT licence."""
import argparse
import sys

import numpy as np
from scipy import signal

from modem import fsk
from . import frontend as fe

FSA = 48000                 # the sender's audio rate
FZ = 192000                 # the channel's complex baseband rate
BAUD = 1200
SPS = 18                    # receiver samples a symbol: DMA 5.5296 MS/s / 64 / 4 = 21.6 kHz
FDMA = 64 * 4 * SPS * BAUD  # 5.5296 MS/s (the real one is ~5.49: the K6 will resample)
FS4 = FDMA / 64 / 4
TX_CH = fsk.Channel(pre_emph=600, emph_top=2600, hpf=300, hpf_order=2, lpf=None)
TX_LPF = signal.butter(14, 3100, fs=FSA, output="sos")
HZ_PER_UNIT = 18000.0       # run 9: peak 0.1 at 935 Hz -> 1.8 kHz
LIMIT = 4300.0              # run 9: the limiter


def ic7100(audio):
    """sender audio (peak <= 1) -> the IC-7100's deviation in Hz (run 9's measured shape)"""
    d = np.clip(TX_CH.apply(audio, FSA) * HZ_PER_UNIT, -LIMIT, LIMIT)
    return signal.sosfilt(TX_LPF, d)


def fm(dev, f_off=0.0):
    d = fsk.resample(dev, FSA, FZ)
    return np.exp(2j * np.pi * np.cumsum(d + f_off) / FZ)


def ffsk_bits(bits, level):
    """bits -> sender audio (profile F, phase-continuous 1200/1800 Hz), and each symbol's audio
    phase at its start (0 or pi: 1800 Hz is 1.5 cycles)"""
    x = fsk.ffsk(bits, BAUD, FSA) * level
    th = np.pi * (np.concatenate([[0], np.cumsum(1 - np.asarray(bits))[:-1]]) % 2)
    return x, th


def to_baseband(x):
    """GP0 counts at FDMA/64 -> complex baseband at FS4 (mixed down from the IF, 4:1)"""
    n = np.arange(len(x))
    z = x * np.exp(-2j * np.pi * fe.FIF * n / (FDMA / 64))
    return signal.resample_poly(z, 1, 4, window=("kaiser", 6.0))


class Link:
    def __init__(self, nbits, level=0.14, seed=7, lead=24):
        rng = np.random.default_rng(seed)
        self.bits = rng.integers(0, 2, nbits)
        self.lead = lead
        allb = np.concatenate([np.tile([1, 0], lead // 2), self.bits, np.tile([1, 0], 8)])
        a, th = ffsk_bits(allb, level)
        self.th = th[lead:lead + nbits]
        self.dev = ic7100(np.concatenate([np.zeros(FSA // 50), a]))
        self.t0 = FSA // 50 / FSA            # the first symbol's start, s
        self.s = fm(self.dev)

    def rx(self, cn0=None, seed=1, bw=8000.0, floor=0.05, ideal=False):
        x = fe.receive(self.s, FZ, cn0, bw=bw, seed=seed, fdma=FDMA, floor=floor, ideal=ideal)
        return x if ideal else to_baseband(x.astype(float))


def symbols(z, start, n):
    """(n, SPS) segments of z from sample start"""
    return np.lib.stride_tricks.sliding_window_view(z[start:start + (n + 1) * SPS], SPS)[::SPS][:n]


def find_start(link, z):
    """the receiver's symbol timing: the offset whose discriminator output best matches the
    known lead (done on a clean capture, then kept: a timing loop comes later)"""
    best = None
    d = np.angle(z[1:] * np.conj(z[:-1]))
    nom = int(round(link.t0 * FS4))
    for off in range(nom - 2 * SPS, nom + 6 * SPS):
        seg = symbols(np.concatenate([[0], d]), off + link.lead * SPS, 200)
        k = np.arange(SPS)
        e12 = np.abs(seg @ np.exp(-2j * np.pi * 1200 * k / FS4))
        e18 = np.abs(seg @ np.exp(-2j * np.pi * 1800 * k / FS4))
        sc = np.mean(np.where(link.bits[:200] == 1, e12 - e18, e18 - e12))
        if best is None or sc > best[0]:
            best = (sc, off)
    return best[1] + link.lead * SPS


def train(link, z, start):
    """IF templates (tone, start phase) averaged over the clean capture's symbols, each segment
    turned so its first sample has phase 0; the per-symbol carrier phase step of each too"""
    n = len(link.bits)
    seg = symbols(z, start, n)
    rot = seg[:, :1] / np.abs(seg[:, :1])
    seg = seg / rot
    T = {}
    for b in (0, 1):
        for p in (0, 1):
            m = (link.bits == b) & ((link.th > 1) == p)
            T[b, p] = seg[m].mean(0)
    return T


def det_disc(z, start, n, bw):
    sos = signal.butter(6, bw, fs=FS4, output="sos")
    zf = signal.sosfiltfilt(sos, z)
    d = np.concatenate([[0], np.angle(zf[1:] * np.conj(zf[:-1]))])
    seg = symbols(d, start, n)
    k = np.arange(SPS)
    e12 = np.abs(seg @ np.exp(-2j * np.pi * 1200 * k / FS4))
    e18 = np.abs(seg @ np.exp(-2j * np.pi * 1800 * k / FS4))
    return (e12 > e18).astype(int)


def det_if(z, start, n, T):
    seg = symbols(z, start, n)
    m = {h: np.abs(seg @ np.conj(t)) for h, t in T.items()}
    e1 = np.maximum(m[1, 0], m[1, 1])
    e0 = np.maximum(m[0, 0], m[0, 1])
    return (e1 > e0).astype(int)


def det_coh(z, start, n, T, alpha=0.85):
    """decision-aided coherent: the carrier phasor at each symbol's start is predicted from the
    last ones (each decided template's end phase carries it on), and the metric is Re{c conj P}"""
    seg = symbols(z, start, n)
    hs = list(T)
    tt = np.array([T[h] for h in hs])
    end = np.array([t[-1] / abs(t[-1]) for t in tt])       # phase across one symbol
    # the step from this symbol's start to the next one's: the template's end, plus one sample
    step = np.array([(t[-1] / t[-2]) / abs(t[-1] / t[-2]) for t in tt]) * end
    c = seg @ np.conj(tt).T                                  # (n, 4)
    out = np.zeros(n, int)
    P = c[0, 0] / abs(c[0, 0]) if abs(c[0, 0]) else 1
    acc = P
    for k in range(n):
        met = (c[k] * np.conj(acc)).real
        j = int(np.argmax(met))
        out[k] = hs[j][0]
        meas = c[k, j]
        acc = alpha * acc + (1 - alpha) * meas / (abs(meas) + 1e-12) * abs(acc)
        acc = acc / (abs(acc) + 1e-12) * step[j]
    return out


def cmd_ber(a):
    link = Link(a.bits, level=a.level)
    z0 = link.rx(None)
    start = find_start(link, z0)
    T = train(link, z0, start)
    n = len(link.bits)
    print(f"{n} bits, level {a.level}, peak dev {np.max(np.abs(link.dev)):.0f} Hz; start {start}")
    print("C/N0 dB-Hz  Eb/N0 dB   disc 5k   disc 7k   IF noncoh   IF coherent")
    print(f"front end: {'no sigma-delta' if a.ideal else f'noise floor {a.floor} rms'}")
    for cn0 in a.cn0:
        z = link.rx(cn0, seed=int(cn0 * 10), floor=a.floor, ideal=a.ideal)
        r = [np.mean(det_disc(z, start, n, bw) != link.bits) for bw in (5000, 7000)]
        r.append(np.mean(det_if(z, start, n, T) != link.bits))
        r.append(np.mean(det_coh(z, start, n, T) != link.bits))
        print(f"{cn0:8.1f}  {cn0 - 10 * np.log10(BAUD):8.1f}   " + "  ".join(f"{v:8.4f}" for v in r),
              flush=True)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("ber")
    b.add_argument("--bits", type=int, default=4000)
    b.add_argument("--level", type=float, default=0.14)
    b.add_argument("--floor", type=float, default=0.05)
    b.add_argument("--ideal", action="store_true")
    b.add_argument("--cn0", type=float, nargs="*", default=[60, 50, 46, 43, 40, 38, 36, 34])
    b.set_defaults(fn=cmd_ber)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
