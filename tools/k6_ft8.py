#!/usr/bin/env python3
"""Host side of the K6 FT8 modules (c/k6/mod/ft8wf.c, ft8rx.c; docs/k6-ft8-wspr.md).

  python3 tools/k6_ft8.py decode LOG [--thr 20]    ft8wf: rebuild the waterfall from its `w`
                                                   lines and decode it with the K6's own
                                                   receiver (c/k6/ft8/ft8k.c, built for the host)
  python3 tools/k6_ft8.py rows LOG OUT.bin         ft8wf: just the rows (FT8_NB / 2 bytes each)
  python3 tools/k6_ft8.py wav OUT.wav [--text "CQ AG7EW CN87"] [--f0 1480] [--repeat 4]
                                                   FT8 audio to send in USB from the IC-7100
                                                   (12 kHz mono, a transmission every 15 s)
  python3 tools/k6_ft8.py track LOG [--every 1]     ft8wf: the strongest bin each second (a steady
                                                   tone should hold still in one bin)
  python3 tools/k6_ft8.py tonewav OUT.wav --hz 1000 [--secs 60]
                                                   a steady tone to send in USB
  python3 tools/k6_ft8.py tone LOG --hz 1000 2000  where USB audio lands on the K6's IF: a
                                                   gp0cap log (c/k6/mod/gp0cap.c) with a steady
                                                   tone of each --hz in its passes 1 and 2. Says
                                                   which side of the IF the upper sideband falls
                                                   on, so which module (ft8wf or ft8wf_inv) and
                                                   which --f0 put FT8 in the window

The log is the K6's serial output (`K6 MOD say ...` lines or bare module lines). Rows lost on
the serial link (a gap in the `w` numbers) come back as noise-floor rows, so the times stay
right. MIT licence."""
import argparse
import base64
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FLOOR = 0x44          # two bins at the floor (4 = the quantiser's noise level)


def parse(path):
    """-> (start fields {nb, f, inv, ...}, rows as bytes, lost rows)"""
    start, rows, lost, seq = None, bytearray(), 0, 0
    for ln in open(path, errors="replace"):
        m = re.search(r"ft8wf (start|gain) (.*)$", ln)
        if m:
            kv = dict(kv.split("=", 1) for kv in m.group(2).split() if "=" in kv)
            if m.group(1) == "start":
                start, rows, lost, seq = kv, bytearray(), 0, 0
            elif start is not None:
                start.update({"gain_" + k if k in start else k: v for k, v in kv.items()})
            continue
        m = re.search(r"(?:^|say )w (\d+) ([A-Za-z0-9+/=]+)\s*$", ln)
        if not m or start is None:
            continue
        rb = int(start["nb"]) // 2
        n = int(m.group(1))
        try:
            b = base64.b64decode(m.group(2), validate=True)
        except ValueError:
            continue
        if len(b) != 2 * rb or n < seq:
            continue
        if n > seq:                                 # lines lost on the link
            rows += bytes([FLOOR]) * (rb * (n - seq))
            lost += n - seq
        rows += b
        seq = n + 2
    if start is None:
        sys.exit("no 'ft8wf start' line in the log")
    return start, bytes(rows), lost


def decode(start, rows, thr=None):
    """-> [dict(text, f, snr, t, score, iters)] from ft8k_host wf"""
    from sim.k6digi import ft8_wav
    nb, f = int(start["nb"]), int(start.get("f", 1500))
    with tempfile.NamedTemporaryFile(suffix=".wf", delete=False) as fh:
        fh.write(rows)
        name = fh.name
    try:
        cmd = [ft8_wav.host(), "wf", name, str(nb)] + (["thr=%d" % thr] if thr is not None else [])
        out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    finally:
        os.unlink(name)
    res = []
    for line in out.splitlines():
        p = line.split(None, 5)
        row, b = int(p[0]), int(p[1])
        res.append(dict(t=row * 0.08, f=f + (b - nb // 2) * 6.25, snr=int(p[2]),
                        score=int(p[3]), iters=int(p[4]), text=p[5] if len(p) > 5 else ""))
    return res


def cmd_decode(a):
    start, rows, lost = parse(a.log)
    nb = int(start["nb"])
    n = len(rows) // (nb // 2)
    print("ft8wf: %d rows (%.1f s), %d lost on the link; %s" % (
        n, n * 0.08, lost, " ".join("%s=%s" % kv for kv in start.items())))
    res = decode(start, rows, a.thr)
    for d in res:
        print("t=%6.2f f=%7.1f s=%3d score=%3d it=%2d  %s" % (d["t"], d["f"], d["snr"],
                                                             d["score"], d["iters"], d["text"]))
    print("%d decodes" % len(res))


def unpack(rows, nb):
    """rows -> list of per-row bin levels (low nibble = even bin)"""
    rb = nb // 2
    out = []
    for i in range(0, len(rows) - rb + 1, rb):
        r = rows[i:i + rb]
        out.append([v for b in r for v in (b & 15, b >> 4)])
    return out


def cmd_track(a):
    start, rows, lost = parse(a.log)
    nb, f = int(start["nb"]), int(start.get("f", 1500))
    lv = unpack(rows, nb)
    per = max(1, int(round(a.every / 0.08)))
    print("   t   bin    Hz   level  mean")
    for i in range(0, len(lv) - per + 1, per):
        blk = lv[i:i + per]
        tot = [sum(r[b] for r in blk) / per for b in range(nb)]
        b = max(range(nb), key=lambda j: tot[j])
        print("%5.1f  %3d  %6.1f  %5.1f  %5.2f" % (i * 0.08, b, f + (b - nb // 2) * 6.25, tot[b],
                                                  sum(tot) / nb))


def cmd_rows(a):
    start, rows, lost = parse(a.log)
    open(a.out, "wb").write(rows)
    print("%d rows of %d bytes, %d lost" % (len(rows) // (int(start["nb"]) // 2),
                                            int(start["nb"]) // 2, lost))


def cmd_wav(a):
    import numpy as np
    from scipy.io import wavfile
    from sim.k6digi import ft8ref
    fs = 12000
    b, tones = ft8ref.encode(a.text)
    ph, env = ft8ref.gfsk_phase(tones, fs, a.f0)
    one = np.zeros(15 * fs)
    s = int(0.5 * fs)
    one[s:s + len(ph)] = np.cos(ph) * env * a.peak
    x = np.tile(one, a.repeat)
    wavfile.write(a.out, fs, np.round(x * 32767).astype(np.int16))
    print("%s: %r at %.1f Hz, %d x 15 s (%s)" % (a.out, a.text, a.f0, a.repeat,
                                                "".join(map(str, tones))))


FIF = 26e6 / 1536                                   # the FD6818's nominal low IF


def tone_peak(x, fs, lo=11000.0, hi=23000.0):
    """-> (Hz, dB over the band's median) of the strongest line in lo..hi"""
    import numpy as np
    x = np.asarray(x, float)
    n = 1 << int(np.ceil(np.log2(len(x) * 8)))
    X = np.abs(np.fft.rfft((x - x.mean()) * np.hanning(len(x)), n))
    f = np.arange(len(X)) * fs / n
    band = (f >= lo) & (f <= hi)
    i = np.flatnonzero(band)[np.argmax(X[band])]
    return f[i], 20 * np.log10(X[i] / np.median(X[band]))


def tone_fit(caps, hz):
    """gp0cap captures (tools/k6_ifcap.parse) with a tone of hz[0] in pass 1 and hz[1] in pass 2
    -> dict: per pass the median peak, slope (+1: the upper sideband is above the IF), the
    carrier's IF, and the module and --f0 that centre FT8's tones on its window"""
    import numpy as np
    per = {}
    for c in caps:
        if c["pass_"] in (1, 2) and len(c["x"]):
            f, snr = tone_peak(c["x"], c["ks"] * 1000.0 / 64)
            if snr > 15:
                per.setdefault(c["pass_"], []).append(f)
    if 1 not in per or 2 not in per:
        return dict(per={k: len(v) for k, v in per.items()}, error="no clear tone in a pass")
    ia, ib = float(np.median(per[1])), float(np.median(per[2]))
    s = (ib - ia) / (hz[1] - hz[0])
    sign = 1 if s > 0 else -1
    carrier = ia - sign * hz[0]
    inv = sign < 0
    centre = FIF - 1500 if inv else FIF + 1500          # the module's mixer (FT8_AUDIO 1500)
    fc = hz[0] + (centre - ia) / sign                   # the audio that lands on it
    return dict(if1=ia, if2=ib, n1=len(per[1]), n2=len(per[2]), slope=s, carrier=carrier,
                inv=int(inv), module="ft8wf_inv" if inv else "ft8wf",
                f0=round(fc - 3.5 * 6.25), spread=float(np.std(per[1]) + np.std(per[2])))


def cmd_tone(a):
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import k6_ifcap
    caps = k6_ifcap.parse(open(a.log, errors="replace").read().splitlines())
    if a.each:
        for c in caps:
            if c["pass_"] in (1, 2) and len(c["x"]):
                f, snr = tone_peak(c["x"], c["ks"] * 1000.0 / 64)
                print("pass %d step %d cap %3d  r=%4d  %8.1f Hz  %4.0f dB" % (
                    c["pass_"], c["gain"], c["k"], c["rssi"], f, snr))
    r = tone_fit(caps, a.hz)
    if "error" in r:
        sys.exit("%s (clear captures per pass: %s)" % (r["error"], r["per"]))
    print("pass 1 (%g Hz): IF %.1f Hz over %d captures; pass 2 (%g Hz): IF %.1f Hz over %d"
          % (a.hz[0], r["if1"], r["n1"], a.hz[1], r["if2"], r["n2"]))
    print("slope %+.3f: the upper sideband is %s the IF; carrier at IF %.1f Hz (nominal %.1f)"
          % (r["slope"], "below (mirrored)" if r["inv"] else "above", r["carrier"], FIF))
    print("FT8: send mod-%s.bin and play tools/k6_ft8.py wav --f0 %d" % (r["module"], r["f0"]))


def cmd_tonewav(a):
    import numpy as np
    from scipy.io import wavfile
    fs = 12000
    t = np.arange(int(a.secs * fs)) / fs
    env = np.minimum(1, np.minimum(t, t[-1] - t) / 0.01)
    wavfile.write(a.out, fs, np.round(np.cos(2 * np.pi * a.hz * t) * env * a.peak * 32767)
                  .astype(np.int16))
    print("%s: %g Hz, %g s" % (a.out, a.hz, a.secs))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("decode")
    p.add_argument("log")
    p.add_argument("--thr", type=int)
    p = sub.add_parser("rows")
    p.add_argument("log")
    p.add_argument("out")
    p = sub.add_parser("wav")
    p.add_argument("out")
    p.add_argument("--text", default="CQ AG7EW CN87")
    p.add_argument("--f0", type=float, default=1480.0)  # tones 1480-1524: the middle of the window
    p.add_argument("--repeat", type=int, default=4)
    p.add_argument("--peak", type=float, default=0.5)
    p = sub.add_parser("track")
    p.add_argument("log")
    p.add_argument("--every", type=float, default=1.0)
    p = sub.add_parser("tone")
    p.add_argument("log")
    p.add_argument("--hz", type=float, nargs=2, default=[1000.0, 2000.0])
    p.add_argument("--each", action="store_true", help="every capture's peak, in order")
    p = sub.add_parser("tonewav")
    p.add_argument("out")
    p.add_argument("--hz", type=float, required=True)
    p.add_argument("--secs", type=float, default=60.0)
    p.add_argument("--peak", type=float, default=0.5)
    a = ap.parse_args()
    {"decode": cmd_decode, "rows": cmd_rows, "wav": cmd_wav, "tone": cmd_tone, "track": cmd_track,
     "tonewav": cmd_tonewav}[a.cmd](a)


if __name__ == "__main__":
    main()
