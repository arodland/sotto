"""GROUNDWAVE (c/k6/mod/gw.c) on the bench: decode a recording of a K6's overs.

  python3 tools/k6_gw.py decode RUN.raw            raw s16le mono 48 kHz (k6_upload.py --keep)
  python3 tools/k6_gw.py decode RUN.wav

Prints every GROUNDWAVE packet (magic 0xA6) whose CRC checks, with its time, type, seq, game id
and body, and any K6 upload report (0xAF) in the same audio. Each over carries its packet twice,
so a good over shows two lines a few hundred ms apart. Opens no serial port and keys nothing.
SPDX-License-Identifier: MIT
"""
import argparse
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from modem import fsk, k6gw, k6mod, pager  # noqa: E402


def load(path):
    if path.endswith(".wav"):
        import soundfile
        x, fs = soundfile.read(path, dtype="float64")
        return (x if x.ndim == 1 else x[:, 0]), fs
    return np.fromfile(path, dtype="<i2").astype(float) / 32768, 48000


def decode(x, fs, baud=1800):
    """audio -> [(time s, packet dict)], GROUNDWAVE packets only, either word order"""
    d = fsk.Demodulator("F", baud, fsk.FS, 64)
    y = fsk.resample(np.asarray(x, dtype=float), fs, fsk.FS)
    out = []
    if len(y) < fsk.FS // 2:
        return out
    for start, p, _, _ in d.demodulate(y):
        for q in (p, pager.swap_words(p)):
            pk = k6gw.parse(bytes(q))
            if pk is not None:
                pk["swapped"] = q is not p
                out.append((start / fsk.FS, pk))
                break
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    dp = sub.add_parser("decode")
    dp.add_argument("file")
    dp.add_argument("--baud", type=float, default=1800)
    a = ap.parse_args()
    x, fs = load(a.file)
    pkts = decode(x, fs, a.baud)
    for t, p in pkts:
        print(f"{t:8.2f} s  {k6gw.NAMES.get(p['type'], p['type'])} seq={p['seq']} gid={p['gid']:04x} "
              f"from={p['from']} round={p['round']} turn={p['turn']} call={p['call']} "
              f"body={p['body'][:8].hex()}{' (words swapped)' if p['swapped'] else ''}")
    for t, r in k6mod.reports(x, fs):
        print(f"{t:8.2f} s  K6 report {r}")
    print(f"{len(pkts)} GROUNDWAVE packets")
    return 0 if pkts else 1


if __name__ == "__main__":
    sys.exit(main())
