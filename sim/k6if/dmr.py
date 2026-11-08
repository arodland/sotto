"""DMR on the K6's GP0 receive path: a model of what the radio sees and a sync-word detector
(docs/k6-dmr.md).

DMR (ETSI TS 102 361-1) is 4FSK at 4800 symbols/s, dibits 01 00 10 11 -> +3 +1 -1 -3, one
unit 648 Hz of deviation, root-raised-cosine pulses (alpha 0.2), two 30 ms TDMA slots. Every
burst is 54 payload symbols, 24 symbols of sync or embedded signalling, 54 payload symbols. A
repeater (BS) sends both slots continuously, each with a 12-symbol CACH in front; a mobile (MS)
or a direct-mode radio sends 27.5 ms bursts in one slot, every 60 ms, with the carrier off in
between. The 48-bit sync words use only the outer symbols (+-3), are balanced, and a data
sync is the voice sync negated, so one correlator per source family (BS, MS, direct TS1, direct
TS2) finds both. A voice call carries its sync in one burst of six (every 360 ms per slot); data,
idle and header bursts carry it in every burst (a repeater's idle slot: every 60 ms).

The detector, as the MCU would run it (c/k6/mod/dmrdet.c):
  counts (2 * ones - 64, ~85.8 kHz) -> rising zero crossings, interpolated -> the IF's phase
  (cycles) at a uniform grid of `sps` points a symbol -> frequency over the last `win` grid
  steps -> for each family, the normalised correlation of the 24 sync symbols (spaced `sps`
  apart) with the pattern: rho = sum(p * y) / sqrt(24 * sum((y - mean)^2)), signed (+ voice,
  - data)
  -> a hit is a local peak of |rho| over the threshold; DMR is two hits of one family a whole
  number of 30 ms slots apart (+- one symbol), within 720 ms.

  python3 -m sim.k6if.dmr pd        detection vs C/N0 and false hits on FM voice, noise, pager
  python3 -m sim.k6if.dmr hist      |rho| percentiles for each signal

MIT licence."""
import argparse
import os

import numpy as np
from scipy import signal

from modem import fsk
from . import frontend as fe
from . import study as st

BAUD = 4800
UNIT = 648.0                # Hz per symbol unit: +-3 = +-1944 Hz
SLOT_S = 0.030
FC = fe.FDMA / fe.L         # counts a second (~85.8 kHz)
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# TS 102 361-1 table 9.2: the 48-bit sync words (voice; data = voice ^ 0xAAAAAAAAAAAA)
SYNC = {"bs": 0x755FD7DF75F7, "ms": 0x7F7D5DD57DFD, "ts1": 0x5D577F7757FF, "ts2": 0x7DFFD5F55D5F}
FAMS = list(SYNC)
DIBIT = {0b01: 3, 0b00: 1, 0b10: -1, 0b11: -3}


def sync_symbols(word):
    return np.array([DIBIT[(word >> (46 - 2 * i)) & 3] for i in range(24)])


PAT = {f: sync_symbols(w) for f, w in SYNC.items()}
assert all(np.array_equal(sync_symbols(w ^ 0xAAAAAAAAAAAA), -PAT[f]) for f, w in SYNC.items())
assert all(PAT[f].sum() == 0 for f in FAMS)


def rnd(rng, n):
    return rng.choice([-3, -1, 1, 3], n)


def burst(rng, mid):
    """132 symbols: 54 payload, the 24-symbol mid field (a pattern, or None: embedded
    signalling, modelled as random), 54 payload"""
    return np.concatenate([rnd(rng, 54), rnd(rng, 24) if mid is None else mid, rnd(rng, 54)])


def slot_mids(kind, n, fam, rng):
    """the mid field of n consecutive bursts of one slot: 'voice' (LC headers with data sync x3,
    then superframes: voice sync in burst A, embedded in B-F), 'voice-mid' (a call already
    running, joined at a random burst), 'idle' (data sync every burst)"""
    v, d = PAT[fam], -PAT[fam]
    if kind == "idle":
        return [d] * n
    sf = [v] + [None] * 5
    if kind == "voice":
        seq = [d, d, d] + sf * (n // 6 + 1)
    else:
        k = int(rng.integers(6))
        seq = (sf * (n // 6 + 2))[k:]
    return seq[:n]


def bs_symbols(dur_s, slots=("voice-mid", "idle"), seed=1):
    """a repeater's output: CACH + burst every 30 ms, slots alternating TS1, TS2"""
    rng = np.random.default_rng(seed)
    n = int(np.ceil(dur_s / SLOT_S))
    mids = [slot_mids(k, n // 2 + 1, "bs", rng) for k in slots]
    out = [np.concatenate([rnd(rng, 12), burst(rng, mids[i % 2][i // 2])]) for i in range(n)]
    return np.concatenate(out), None


def ms_symbols(dur_s, kind="voice-mid", fam="ms", seed=1):
    """a mobile (fam 'ms') or a direct-mode radio ('ts1'/'ts2'): one 27.5 ms burst every 60 ms
    -> (symbols over the whole time at 4800/s, the carrier's on/off per symbol)"""
    rng = np.random.default_rng(seed)
    n = int(np.ceil(dur_s / (2 * SLOT_S)))
    mids = slot_mids(kind, n, fam, rng)
    sy, on = [], []
    for i in range(n):
        sy += [burst(rng, mids[i]), np.zeros(156)]
        on += [np.ones(132), np.zeros(156)]
    return np.concatenate(sy), np.concatenate(on)


def rrc(sps, alpha=0.2, span=8):
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
    return h / h.sum()


def dmr_baseband(sym, on=None, f_off=0.0, pre_s=0.05, post_s=0.05):
    """symbols (4800/s) -> complex baseband at st.FZ, unit amplitude while keyed. The DMR
    transmitter's own filter: RRC alpha 0.2 (sum-normalised, so a run of +3 reaches 1944 Hz)."""
    fsa = st.FSA
    sps = fsa // BAUD                                   # 10
    up = np.zeros(len(sym) * sps)
    up[::sps] = sym * sps
    dev = np.convolve(up, rrc(sps), "same") * UNIT
    npre, npost = int(pre_s * fsa), int(post_s * fsa)
    dev = np.concatenate([np.zeros(npre), dev, np.zeros(npost)])
    s = st.fm(dev, f_off)
    env = np.concatenate([np.zeros(npre), np.ones(len(sym) * sps) if on is None else
                          np.repeat(on, sps), np.zeros(npost)])
    env = np.convolve(env, np.ones(48) / 48, "same")    # 1 ms ramps
    env = fsk.resample(env, fsa, st.FZ)[:len(s)]
    return s * np.clip(env, 0, 1), npre / fsa


def speech(name="ears_p047_f", dur_s=None):
    import soundfile as sf
    for d in (os.path.join(ROOT, "docs", "samples"), "/mnt/project-files/vocoder/samples-clean"):
        p = os.path.join(d, name + ".flac")
        if os.path.exists(p):
            x, fs = sf.read(p)
            x = x if x.ndim == 1 else x[:, 0]
            x = fsk.resample(x, fs, st.FSA)
            if dur_s:
                x = np.tile(x, int(np.ceil(dur_s * st.FSA / len(x))))[:int(dur_s * st.FSA)]
            return x / np.max(np.abs(x))
    raise FileNotFoundError(name)


def fm_voice(audio, dev_pk=2500.0, ctcss=None, f_off=0.0):
    """an analog FM repeater: pre-emphasis 300-3000 Hz, 300 Hz high-pass, clipped at dev_pk,
    3 kHz low-pass; a CTCSS tone (Hz, 15% of dev_pk) if given"""
    ch = fsk.Channel(pre_emph=300, emph_top=3000, hpf=300, lpf=None)
    d = ch.apply(audio, st.FSA)
    d = np.clip(d / np.percentile(np.abs(d), 99.5) * dev_pk, -dev_pk, dev_pk)
    d = signal.sosfilt(signal.butter(8, 3000, fs=st.FSA, output="sos"), d)
    if ctcss:
        d = d + 0.15 * dev_pk * np.sin(2 * np.pi * ctcss * np.arange(len(d)) / st.FSA)
    return st.fm(d, f_off)


def to_counts(s, cn0=None, seed=1, bw=8000.0):
    return fe.receive(s, st.FZ, cn0, bw=bw, seed=seed, fdma=fe.FDMA).astype(float)


# ---- the detector ----

def phase_grid(x, sps=2, fc=FC, rate_err=0.0):
    """counts -> the IF's phase in cycles at a grid of sps points a symbol (the MCU's clock
    off by rate_err: its grid spacing in counts assumes FC, the counts come at FC*(1+rate_err))"""
    x = np.asarray(x, float)
    i = np.nonzero((x[:-1] < 0) & (x[1:] >= 0))[0]
    t = i + (-x[i]) / (x[i + 1] - x[i])
    g = np.arange(np.ceil(t[0]), t[-1], fc / (BAUD * sps) * (1 + rate_err))
    return np.interp(g, t, np.arange(len(t))), g


def freq(ph, sps=2, win=1):
    """Hz over the last win grid steps (minus the IF), at every grid point"""
    gr = BAUD * sps
    y = np.full(len(ph), np.nan)
    y[win:] = (ph[win:] - ph[:-win]) * gr / win - fe.FIF
    return y


SD_LO, SD_HI = 1300.0, 2600.0   # the sync window's spread (Hz rms) a hit needs: +-3 symbols are 1944


def rho(y, sps=2, gate=True):
    """{family: signed normalised correlation at each grid point (the sync's last symbol)}, 0
    where the 24 taps' spread is outside SD_LO..SD_HI (a bare carrier, squelch noise)"""
    y = np.nan_to_num(y)
    n = len(y)
    span = 23 * sps
    out = {}
    s1 = np.zeros(n)
    s2 = np.zeros(n)
    for i in range(24):
        sh = span - i * sps
        yy = np.concatenate([np.zeros(sh), y[:n - sh]])
        s1 += yy
        s2 += yy * yy
    var = np.maximum(s2 - s1 * s1 / 24, 1e-9)
    for f in FAMS:
        c = np.zeros(n)
        for i, p in enumerate(PAT[f]):
            sh = span - i * sps
            c[sh:] += p / 3 * y[:n - sh]
        out[f] = c / np.sqrt(24 * var)
        if gate:
            sd = np.sqrt(var / 24)
            out[f][(sd < SD_LO) | (sd > SD_HI)] = 0.0
    return out


def hits(r, thr, sps=2):
    """[(grid index, family, rho)] local peaks of |rho| over thr (one per symbol's neighbourhood)"""
    ev = []
    for f, c in r.items():
        a = np.abs(c)
        idx = np.nonzero(a >= thr)[0]
        for j in idx:
            lo, hi = max(0, j - sps), min(len(a), j + sps + 1)
            if a[j] == a[lo:hi].max():
                ev.append((int(j), f, float(c[j])))
    return sorted(ev)


def confirm(ev, sps=2, tol_sym=1, max_slots=24):
    """the grid indexes where DMR is confirmed: a hit whose family had another hit a whole number
    of 30 ms slots (1..max_slots) earlier, +- tol_sym symbols and 1/256 of the gap (the MCU's
    grid follows the DMA's measured rate, which moves ~0.1-0.3% between measurements)"""
    slot = SLOT_S * BAUD * sps                          # 288 grid steps at 2 sps
    tol = tol_sym * sps
    out = []
    last = {f: [] for f in FAMS}
    for j, f, c in ev:
        for j0 in last[f]:
            d = j - j0
            m = round(d / slot)
            if 1 <= m <= max_slots and abs(d - m * slot) <= tol + d / 256:
                out.append((j, f, c))
                break
        last[f] = [j0 for j0 in last[f] if j - j0 <= max_slots * slot + tol] + [j]
    return out


def detect(x, sps=2, win=1, thr=0.8, rate_err=0.0):
    ph, g = phase_grid(x, sps, rate_err=rate_err)
    r = rho(freq(ph, sps, win), sps)
    ev = hits(r, thr, sps)
    return ev, confirm(ev, sps), g, r


# ---- the study ----

def signals(dur, f_off=0.0):
    """name -> (complex baseband at st.FZ, the expected answer: True for DMR)"""
    sy, _ = bs_symbols(dur, ("voice-mid", "idle"), seed=3)
    bs_idle, _ = dmr_baseband(sy, f_off=f_off)
    sy, _ = bs_symbols(dur, ("voice-mid", "voice-mid"), seed=4)
    bs_vv, _ = dmr_baseband(sy, f_off=f_off)
    sy, on = ms_symbols(dur, "voice-mid", "ms", seed=5)
    ms_v, _ = dmr_baseband(sy, on, f_off=f_off)
    sy, on = ms_symbols(dur, "voice-mid", "ts1", seed=6)
    dm_v, _ = dmr_baseband(sy, on, f_off=f_off)
    out = {"dmr-bs-voice+idle": (bs_idle, True), "dmr-bs-voice+voice": (bs_vv, True),
           "dmr-ms-voice": (ms_v, True), "dmr-direct-voice": (dm_v, True)}
    for nm in ("ears_p047_f", "ears_p020_m", "librispeech_251_m"):
        try:
            out[f"fm-{nm}"] = (fm_voice(speech(nm, dur), ctcss=100.0, f_off=f_off), False)
        except FileNotFoundError:
            pass
    n = int(dur * st.FSA)
    out["fm-carrier"] = (st.fm(np.zeros(n), f_off), False)
    rng = np.random.default_rng(9)
    out["fm-ffsk1200"] = (st.fm(st.ic7100(st.ffsk_bits(rng.integers(0, 2, int(dur * 1200)), 0.14)[0]), f_off), False)
    out["fm-cw-1khz"] = (fm_voice(np.sin(2 * np.pi * 1000 * np.arange(n) / st.FSA)
                                  * (np.arange(n) % 9600 < 4800)), False)
    out["noise"] = (np.zeros(int(dur * st.FZ), complex), False)
    return out


def cmd_hist(a):
    sig = signals(a.dur)
    for nm, (s, truth) in sig.items():
        cn0 = a.cn0 if nm != "noise" else 30.0
        x = to_counts(s if nm != "noise" else s + 1e-9, cn0)
        ph, _ = phase_grid(x, a.sps)
        r = rho(freq(ph, a.sps, a.win), a.sps)
        m = np.max(np.abs(np.array(list(r.values()))), axis=0)
        print(f"{nm:22s} |rho| p50 {np.percentile(m, 50):.2f} p99 {np.percentile(m, 99):.2f} "
              f"p99.99 {np.percentile(m, 99.99):.2f} max {m.max():.2f}")


def cmd_pd(a):
    sig = signals(a.dur, a.off)
    print(f"sps={a.sps} win={a.win} thr={a.thr} rate_err={a.rate_err} dur={a.dur}s off={a.off} Hz")
    for cn0 in a.cn0s:
        line = [f"C/N0 {cn0:4.0f}"]
        for nm, (s, truth) in sig.items():
            x = to_counts(s if nm != "noise" else s + 1e-9, cn0, seed=int(cn0))
            ev, conf, g, _ = detect(x, a.sps, a.win, a.thr, a.rate_err)
            t_first = g[conf[0][0]] / FC if conf else None
            line.append(f"{nm}: hits={len(ev)} conf={len(conf)}"
                        + (f" first={t_first * 1000:.0f}ms" if t_first is not None else ""))
        print("\n  ".join(line))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("cmd", choices=["pd", "hist"])
    ap.add_argument("--sps", type=int, default=2)
    ap.add_argument("--win", type=int, default=1)
    ap.add_argument("--thr", type=float, default=0.8)
    ap.add_argument("--dur", type=float, default=3.0)
    ap.add_argument("--cn0", type=float, default=50.0)
    ap.add_argument("--cn0s", type=float, nargs="+", default=[60, 50, 45, 42, 40])
    ap.add_argument("--off", type=float, default=0.0)
    ap.add_argument("--rate-err", type=float, default=0.0)
    a = ap.parse_args()
    {"pd": cmd_pd, "hist": cmd_hist}[a.cmd](a)


if __name__ == "__main__":
    main()
