#!/usr/bin/env python3
"""Bench test for the K6 IF softmodem (docs/k6-if-modem.md): the IC-7100 sends known PN9 data in
FFSK and 4FSK, the K6's gp0cap module (c/k6/mod/gp0cap.c) records 25 ms stretches of the GP0 IF at
stepped front-end gains, and this decodes every capture twice, with an FM discriminator and with
the IF matched filter, so the two meet the same samples.

  python3 tools/k6_ifcap.py wav OUTDIR          the two passes' audio (65 s each) and a CW ID
  python3 tools/k6_ifcap.py run [--dry-run]     key the IC-7100 (rigctld :4532, PKTFM) for both
  python3 tools/k6_ifcap.py analyze LOG         per pass and gain step: C/N0, BER disc vs IF
  python3 tools/k6_ifcap.py simlog OUT.log      a log as gp0cap would print it, from the study's
                                                model (sim/k6if) at stepped C/N0: checks analyze

Each pass is 1 s of alternating symbols (the module waits 2 s after the carrier), then the PN9
sequence (x^9 + x^5 + 1, all-ones seed, period 511) over and over: one bit a symbol in FFSK
(profile F's 1200/1800 Hz at 1200 baud, peak 0.14: under the IC-7100's limiter), two bits a
symbol (Gray) in 4FSK (900/1500/2100/2700 Hz at 1200 baud, peak 0.12). MIT licence."""
import argparse
import base64
import os
import re
import subprocess
import sys
import tempfile
import time
import wave
from fractions import Fraction

import numpy as np
from scipy import signal

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
from sim.k6if import study as st  # noqa: E402
from sim.k6if.modes import Mode  # noqa: E402

FSA = 48000
PASSES = {1: ("ffsk", Mode([1200, 1800], 1200, 0.14)),
          2: ("4fsk", Mode([900, 1500, 2100, 2700], 1200, 0.12))}
PASS_S, LEAD_S = 65.0, 1.0
FS_GRID = st.FDMA / 64          # the study's 86.4 kHz grid (18 samples a symbol at 21.6 kHz)


def pn9(n):
    r, out = 0x1FF, np.zeros(n, int)
    for i in range(n):
        b = ((r >> 8) ^ (r >> 4)) & 1
        out[i] = r & 1
        r = ((r << 1) | b) & 0x1FF
    return out


def pn_symbols(mode, n=511):
    """the PN9 sequence as the mode's symbols (period 511 symbols in both modes)"""
    b = pn9(n * mode.k)
    if mode.k == 1:
        return b
    v = b.reshape(n, mode.k) @ (1 << np.arange(mode.k - 1, -1, -1))
    inv = np.argsort(mode.gray)                  # Gray value -> symbol index
    return inv[v]


def pass_audio(mode, secs=PASS_S):
    nsym = int(secs * mode.baud)
    lead = np.tile([0, mode.M - 1], int(LEAD_S * mode.baud) // 2)
    syms = np.concatenate([lead, np.resize(pn_symbols(mode), nsym - len(lead))])
    x, _ = mode.audio(syms)
    return x


def write(path, x):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(FSA)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())


def cmd_wav(a):
    os.makedirs(a.outdir, exist_ok=True)
    for _, (name, mode) in PASSES.items():
        p = os.path.join(a.outdir, f"ifcap-{name}.wav")
        write(p, pass_audio(mode))
        print("wrote", p)


def rig(a, *args):
    cmd = ["rigctl", "-m", "2", "-r", a.rigctld] + [str(v) for v in args]
    if a.dry_run:
        print("dry run:", " ".join(cmd))
        return "0"
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.strip()


def key_and_play(a, path):
    if rig(a, "t") != "0":
        sys.exit("the IC-7100 is already keyed (ID keeper?): try again in a minute")
    try:
        rig(a, "T", 1)
        time.sleep(0.2)
        if a.dry_run:
            print("dry run: paplay", path)
        else:
            subprocess.run(["paplay", f"--device={a.device}", path], check=True)
        time.sleep(0.05)
    finally:
        rig(a, "T", 0)


def cmd_run(a):
    f = rig(a, "f")
    if not a.dry_run and int(f) != a.freq:
        sys.exit(f"IC-7100 on {f} Hz, not {a.freq}: set both radios to the same frequency first")
    rig(a, "M", "PKTFM", 0)
    with tempfile.TemporaryDirectory() as d:
        for _, (name, mode) in PASSES.items():
            p = os.path.join(d, f"{name}.wav")
            write(p, pass_audio(mode))
            print(f"{time.strftime('%H:%M:%S')} pass {name}: {PASS_S:.0f} s", flush=True)
            key_and_play(a, p)
            time.sleep(4.0)
        from modem import k6
        p = os.path.join(d, "id.wav")
        write(p, k6.cw(a.call + " SOTTO TEST", FSA))
        print(f"{time.strftime('%H:%M:%S')} CW ID", flush=True)
        key_and_play(a, p)


# ---- the log ----

def parse(lines):
    """-> list of dicts: pass, gain, k, ks, rssi, lost, x (int8 counts)"""
    caps, cur = {}, {}
    for ln in lines:
        m = re.search(r"(?:^|say )c (\d+) (\d+) (\d+) ks=(\d+) r=(-?\d+) t=(\d+) lost=(\d+)", ln)
        if m:
            p, g, k, ks, r, t, lost = map(int, m.groups())
            caps[k] = dict(pass_=p, gain=g, k=k, ks=ks, rssi=r, lost=lost, data=bytearray())
            continue
        m = re.search(r"(?:^|say )y (\d+) ([0-9a-f]{4}) ([A-Za-z0-9+/]+)\s*$", ln)
        if m:
            k, off, b = int(m.group(1)), int(m.group(2), 16), m.group(3)
            c = caps.get(k)
            if c is not None and off == len(c["data"]):
                c["data"] += base64.b64decode(b)
    out = []
    for k in sorted(caps):
        c = caps[k]
        c["x"] = np.frombuffer(bytes(c.pop("data")), np.int8).astype(float)
        out.append(c)
    return out


def baseband(c, ncap):
    """a capture's counts -> complex baseband at 21.6 kHz (the study's grid)"""
    x = c["x"][:ncap]
    fs = c["ks"] * 1000.0 / 64
    r = Fraction(FS_GRID / fs).limit_denominator(4000)
    x = signal.resample_poly(x - x.mean(), r.numerator, r.denominator)
    return st.to_baseband(x)


class Det:
    """the two detectors on one mode, with templates trained on strong captures"""

    def __init__(self, mode):
        self.m = mode
        self.sps = mode.sps
        self.ref = pn_symbols(mode)
        # audio phase classes a symbol can start on (multiples of 1/ncls of a cycle)
        frac = (mode.tones / mode.baud) % 1
        q = [Fraction(float(f)).limit_denominator(24).denominator for f in frac]
        self.ncls = int(np.lcm.reduce(q))
        self.T = None
        k = np.arange(self.sps)
        self.E = np.exp(-2j * np.pi * np.outer(k, mode.tones) / st.FS4)

    EDGE = 2   # symbols dropped at each end of a capture (the resampling filters' edges)

    def segs(self, z, off):
        n = (len(z) - off) // self.sps
        return z[off:off + n * self.sps].reshape(n, self.sps)[self.EDGE:n - self.EDGE]

    def disc(self, z, off, bw=7000.0):
        sos = signal.butter(6, bw, fs=st.FS4, output="sos")
        zf = signal.sosfiltfilt(sos, z)
        d = np.concatenate([[0], np.angle(zf[1:] * np.conj(zf[:-1]))])
        return np.abs(self.segs(d, off) @ self.E)

    def if_(self, z, off):
        s = self.segs(z, off)
        out = np.zeros((len(s), self.m.M))
        for (m, c), t in self.T.items():
            out[:, m] = np.maximum(out[:, m], np.abs(s @ np.conj(t)))
        return out

    def sync(self, z, metric):
        """data-aided: the sample offset and PN phase whose known symbols score best"""
        best = None
        n511 = len(self.ref)
        for off in range(self.sps):
            met = metric(z, off)
            n = len(met)
            met = (met - met.mean(1, keepdims=True)) / (met.std() + 1e-12)
            # score(p) = sum_k met[k, ref[p + k]]
            idx = (np.arange(n511)[:, None] + np.arange(n)[None, :]) % n511
            sc = met[np.arange(n)[None, :], self.ref[idx]].sum(1)
            p = int(np.argmax(sc))
            if best is None or sc[p] > best[0]:
                best = (sc[p], off, p)
        return best[1], best[2]

    def known(self, n, p):
        """the n known symbols from PN phase p (p counts from the first symbol segs keeps)"""
        return self.ref[(p + np.arange(n)) % len(self.ref)]

    def train(self, zs, iters=4):
        """templates per (tone, audio start phase) from strong captures, synced by the
        discriminator: each symbol's start phase first from its discriminator audio, then
        refined k-means style (each segment to the template it correlates with best)"""
        segs, tone, cls = [], [], []
        sos = signal.butter(6, 7000.0, fs=st.FS4, output="sos")
        for z in zs:
            off, p = self.sync(z, self.disc)
            s = self.segs(z, off)
            sy = self.known(len(s), p)
            zf = signal.sosfiltfilt(sos, z)
            d = self.segs(np.concatenate([[0], np.angle(zf[1:] * np.conj(zf[:-1]))]), off)
            ph = np.angle(np.sum(d * self.E[:, sy].T, 1))     # each symbol's tone phase
            for m in range(self.m.M):
                sel = np.where(sy == m)[0]
                if not len(sel):
                    continue
                o = np.angle(np.exp(1j * self.ncls * ph[sel]).mean()) / self.ncls
                c = np.round((ph[sel] - o) * self.ncls / (2 * np.pi)).astype(int) % self.ncls
                segs += [s[j] / (s[j, 0] / (abs(s[j, 0]) + 1e-12)) for j in sel]
                tone += [m] * len(sel)
                cls += list(c)
        segs, tone, cls = np.array(segs), np.array(tone), np.array(cls)
        for _ in range(iters):
            T = {}
            for m in range(self.m.M):
                for c in range(self.ncls):
                    sel = (tone == m) & (cls == c)
                    if sel.sum() >= 2:
                        T[m, c] = segs[sel].mean(0)
            for m in range(self.m.M):
                hs = [h for h in T if h[0] == m]
                sel = np.where(tone == m)[0]
                if not hs or not len(sel):
                    continue
                cor = np.stack([np.abs(segs[sel] @ np.conj(T[h])) / np.linalg.norm(T[h]) for h in hs], 1)
                cls[sel] = [hs[i][1] for i in np.argmax(cor, 1)]
        self.T = T

    def score(self, z):
        """-> (BER disc, BER IF, Es/N0 dB estimate, bits) at the IF detector's sync"""
        off, p = self.sync(z, self.if_)
        mi, md = self.if_(z, off), self.disc(z, off)
        sy = self.known(len(mi), p)
        b = self.m.bits_of
        e_if = np.sum(b[np.argmax(mi, 1)] != b[sy])
        e_d = np.sum(b[np.argmax(md, 1)] != b[sy])
        # Es/N0: per symbol, the best template of the known tone fitted with its own complex gain
        s = self.segs(z, off)
        best = np.zeros(len(s))
        for (m, c), t in self.T.items():
            sel = sy == m
            pr = np.abs(s[sel] @ np.conj(t)) ** 2 / np.vdot(t, t).real
            best[sel] = np.maximum(best[sel], pr)
        tot = np.sum(np.abs(s) ** 2, 1)
        n0 = np.mean(tot - best) / (self.sps - 1)
        es = max(np.mean(best) - n0, 1e-12)
        return e_d, e_if, 10 * np.log10(es / max(n0, 1e-12)), len(sy) * self.m.k


def analyze(caps, ncap=2160, out=print):
    good = [c for c in caps if c["lost"] == 0 and len(c["x"]) >= ncap]
    out(f"{len(caps)} captures, {len(good)} whole and without overruns")
    noise = [c for c in good if c["pass_"] == 0]
    res = {}
    for pas, (name, mode) in PASSES.items():
        cs = [c for c in good if c["pass_"] == pas]
        if not cs:
            continue
        zs = {c["k"]: baseband(c, ncap) for c in cs}
        det = Det(mode)
        # train on the step with the most power (the first fixed-gain step, usually)
        steps = sorted({c["gain"] for c in cs})
        pw = {g: np.median([np.mean(np.abs(zs[c["k"]]) ** 2) for c in cs if c["gain"] == g]) for g in steps}
        gt = max(steps, key=lambda g: pw[g])
        det.train([zs[c["k"]] for c in cs if c["gain"] == gt][:10])
        out(f"pass {pas} ({name}): {len(cs)} captures, templates from step {gt}, "
            f"{len(det.T)} templates")
        out("  step  caps  rssi  power dB  Es/N0 dB  C/N0 dB-Hz   bits   BER disc   BER IF")
        for g in steps:
            rows = [det.score(zs[c["k"]]) for c in cs if c["gain"] == g]
            ed = sum(r[0] for r in rows)
            ei = sum(r[1] for r in rows)
            nb = sum(r[3] for r in rows)
            esn0 = float(np.median([r[2] for r in rows]))
            cn0 = esn0 + 10 * np.log10(mode.baud)
            rs = int(np.median([c["rssi"] for c in cs if c["gain"] == g]))
            res[pas, g] = dict(esn0=esn0, cn0=cn0, ber_disc=ed / nb, ber_if=ei / nb, bits=nb)
            out(f"  {g:4d}  {len(rows):4d}  {rs:4d}  {10 * np.log10(pw[g]):8.1f}  {esn0:8.1f}  "
                f"{cn0:10.1f}  {nb:5d}  {ed / nb:9.4f}  {ei / nb:7.4f}")
    if noise and (1, 0) in res:
        # the GP0 noise floor in the study's units: noise-only power against the pager-gain
        # step's signal power (a carrier at the AGC's level, AMP = 0.5)
        pn = np.median([np.mean(np.abs(baseband(c, ncap)) ** 2) for c in noise if c["gain"] == 0])
        ps = np.median([np.mean(np.abs(baseband(c, ncap)) ** 2) for c in good
                        if c["pass_"] == 1 and c["gain"] == 0])
        fl = 0.5 * np.sqrt(pn / ps / 2)
        out(f"noise floor: no-carrier power {10 * np.log10(pn / ps):.1f} dB re the signal at "
            f"the pager's gain, i.e. floor ~{fl:.3f} rms in the study's units (it assumed 0.03-0.06)")
        res["floor"] = fl
    return res


def simlog(steps=((0, 70.0), (1, 48.0), (2, 44.0), (3, 41.0)), ncaps=10, secs=3.0, ncap=2160,
           fdma=5.49e6, floor=0.05, seed=1):
    """gp0cap's serial lines for the study's model: each (gain step, C/N0) gets ncaps captures
    from random points after the lead-in, both passes; one noise-only capture as pass 0"""
    from sim.k6if import frontend as fe
    lines, k = ["K6 MOD say cap start floor=-120 ncap=%d r7e=0000 r13=03be" % ncap], 0

    def emit(pas, g, cap, r):
        nonlocal k
        lines.append(f"K6 MOD say c {pas} {g} {k} ks={int(fdma / 1000)} r={r} t=0 lost=0")
        b = np.clip(cap, -64, 63).astype(np.int8).tobytes()
        for off in range(0, ncap, 36):
            lines.append(f"K6 MOD say y {k} {off:04x} " + base64.b64encode(b[off:off + 36]).decode())
        k += 1
    x = fe.receive(np.full(int(0.1 * st.FZ), 1e-9, complex), st.FZ, 40.0, seed=seed, fdma=fdma,
                   floor=floor)
    emit(0, 0, x[:ncap], -120)
    for pas, (name, mode) in PASSES.items():
        s = st.fm(st.ic7100(pass_audio(mode, secs)))
        for g, cn0 in steps:
            x = fe.receive(s, st.FZ, cn0, seed=seed + g + 10 * pas, fdma=fdma, floor=floor)
            rng = np.random.default_rng(g + 100 * pas)
            for _ in range(ncaps):
                o = int(rng.integers(int((LEAD_S + 0.1) * fdma / 64), len(x) - ncap - 10))
                emit(pas, g, x[o:o + ncap], -80)
    return lines


def cmd_simlog(a):
    lines = simlog(ncaps=a.ncaps)
    open(a.out, "w").write("\n".join(lines) + "\n")
    print(f"wrote {a.out}: {len(lines)} lines")


def cmd_analyze(a):
    analyze(parse(open(a.log, errors="replace")), a.ncap)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("wav")
    w.add_argument("outdir")
    w.set_defaults(fn=cmd_wav)
    r = sub.add_parser("run")
    r.add_argument("--freq", type=int, default=440850000)
    r.add_argument("--rigctld", default="localhost:4532")
    r.add_argument("--device", default="alsa_output.usb-Burr-Brown_from_TI_USB_Audio_CODEC-00.analog-stereo")
    r.add_argument("--call", default="AG7EW")
    r.add_argument("--dry-run", action="store_true")
    r.set_defaults(fn=cmd_run)
    z = sub.add_parser("analyze")
    z.add_argument("log")
    z.add_argument("--ncap", type=int, default=2160)
    z.set_defaults(fn=cmd_analyze)
    sl = sub.add_parser("simlog")
    sl.add_argument("out")
    sl.add_argument("--ncaps", type=int, default=10)
    sl.set_defaults(fn=cmd_simlog)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
