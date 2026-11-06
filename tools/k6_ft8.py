#!/usr/bin/env python3
"""Host side of the K6 FT8 modules (c/k6/mod/ft8wf.c, ft8rx.c; docs/k6-ft8-wspr.md).

  python3 tools/k6_ft8.py decode LOG [--thr 20]    ft8wf: rebuild the waterfall from its `w`
                                                   lines and decode it with the K6's own
                                                   receiver (c/k6/ft8/ft8k.c, built for the host)
  python3 tools/k6_ft8.py rows LOG OUT.bin         ft8wf: just the rows (FT8_NB / 2 bytes each)
  python3 tools/k6_ft8.py wav OUT.wav [--text "CQ AG7EW CN87"] [--f0 1480] [--repeat 4]
                                                   FT8 audio to send in USB from the IC-7100
                                                   (12 kHz mono, a transmission every 15 s)

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
    a = ap.parse_args()
    {"decode": cmd_decode, "rows": cmd_rows, "wav": cmd_wav}[a.cmd](a)


if __name__ == "__main__":
    main()
