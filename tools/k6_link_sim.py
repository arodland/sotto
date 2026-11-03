"""Packet success of the K6 pager softmodem over a simulated FM link (no fading).

FM-modulates the softmodem's output at 2.5 kHz peak deviation, adds white noise at a given
C/N0, FM-demodulates with a 12.5 kHz channel filter, and runs our demodulator. Our
demodulator stands in for the K6's chip, so treat the numbers as a guide to the waveform,
not a prediction of the radio.

  python tools/k6_link_sim.py [--packets 40]
"""
import argparse
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from modem import fsk, pager, sdr       # noqa: E402

FS_IQ = 96000


def run(phy, baud, cn0, packets, seed=1, dev=2500.0):
    rng = np.random.default_rng(seed)
    frames = rng.integers(0, 256, 6 * 64 * packets // 8, dtype=np.uint8).tobytes()
    pk = pager.L.pack(frames, 1)
    x = fsk.modulate(pager.Air().burst_bits(pk, baud), phy, baud, level=1.0)
    iq = sdr.fm_modulate(x * dev, fsk.FS, FS_IQ)
    n0 = 10 ** (-cn0 / 10) * FS_IQ          # carrier power 1
    iq = iq + np.sqrt(n0 / 2) * (rng.standard_normal(len(iq)) + 1j * rng.standard_normal(len(iq)))
    y = sdr.fm_demodulate(iq.astype(np.complex64), FS_IQ, fsk.FS, bw_hz=6250.0)
    _, t = fsk.receive(y, fsk.FS, phy, baud)
    return sum(r[1] for r in t) / len(pk)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--packets", type=int, default=40)
    a = ap.parse_args()
    cases = [("D", 1200), ("D", 2400), ("F", 1200)]
    levels = (40, 43, 46, 49, 52, 55)
    print("C/N0 dB-Hz  " + "  ".join(f"{p} {b:4d}" for p, b in cases))
    for cn0 in levels:
        print(f"{cn0:10d}  " + "  ".join(f"{100 * run(p, b, cn0, a.packets):5.0f}%" for p, b in cases))


if __name__ == "__main__":
    main()
