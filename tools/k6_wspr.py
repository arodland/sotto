#!/usr/bin/env python3
"""Host side of the K6 WSPR modules (c/k6/mod/wspriq.c, wsprtx.c; docs/k6-ft8-wspr.md).

  python3 tools/k6_wspr.py decode LOG [--fc 0] [--span 60]
        wspriq: rebuild the K6's complex baseband from its `q` lines and decode WSPR in it
        (sim/k6digi/wsprref.py's search over time, frequency and drift, then its Fano decoder).
        Prints one line a decode: audio Hz (WSPR_AUDIO + the offset), start (s after the
        stream's first sample), drift (Hz over the frame), sync score, message
  python3 tools/k6_wspr.py iq LOG OUT.npz
        wspriq: just the baseband (z, rate, f) for a look elsewhere
  python3 tools/k6_wspr.py wav OUT.wav [--text "AG7EW CN87 10"] [--f0 1500]
        WSPR audio to send in USB from the IC-7100 at dial = the K6's rx_hz (tone 0 at --f0)
  python3 tools/k6_wspr.py sync [--before 10] [--dry-run]
        the beacon's sync mark (c/k6/mod/wsprbcn.c, WSPR_SYNC_MS 10000): on hamlet, after the
        upload, key the IC-7100 (440.850, through tools/k6_bench.py's PTT lock) with the bench's
        CW ID (no bare carriers: every test transmission IDs) so that it unkeys --before s ahead of a WSPR slot (1 s after an even UTC minute,
        by the PC's NTP clock). The K6 has no clock of its own; its first frame then starts on
        that slot and the rest follow on the 2-minute grid

The K6's own WSPR (wsprtx, audio FSK on FM), recorded from the IC-7100 in FM, decodes with
  python3 -m sim.k6digi.wsprref decode REC.wav

The log is the K6's serial output (`K6 MOD say ...` lines or bare module lines). A line lost
on the serial link comes back as 15 zero samples, placed by the times of the lines either
side, so the frame's timing stays right. MIT licence."""
import argparse
import base64
import os
import re
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

N = 15                  # samples a line (wspriq.c's WQ_N)
CLK = 96e6              # the line times' clock (TIM17)
WSPR_RATE = 375 / 8     # wsprref's rate: 32 samples a symbol


def parse(path):
    """-> (fields from the start/gain lines, [(time in 96 MHz cycles, unwrapped; N complex)])"""
    info, lines, last = None, [], None
    for ln in open(path, errors="replace"):
        m = re.search(r"wspriq (start|gain) (.*)$", ln)
        if m:
            kv = dict(kv.split("=", 1) for kv in m.group(2).split() if "=" in kv)
            if m.group(1) == "start":
                info, lines, last = kv, [], None
            elif info is not None:
                info.update({"gain_" + k if k in info else k: v for k, v in kv.items()})
            continue
        m = re.search(r"(?:^|say )q ([0-9a-f]{8}) ([0-7]) ([A-Za-z0-9+/=]{40})\s*$", ln)
        if not m or info is None:
            continue
        try:
            b = np.frombuffer(base64.b64decode(m.group(3)), np.int8).astype(float)
        except ValueError:
            continue
        if len(b) != 2 * N:
            continue
        t = int(m.group(1), 16)
        tu = t if last is None else last[1] + ((t - last[0]) & 0xFFFFFFFF)
        last = (t, tu)
        lines.append((tu, (b[0::2] + 1j * b[1::2]) * 2 ** int(m.group(2))))
    if info is None:
        raise SystemExit("%s: no wspriq start line" % path)
    return info, lines


def baseband(info, lines):
    """-> (z: evenly spaced complex samples, zeros where lines were lost; rate Hz; t0 s, the
    module-clock time of z[0]). Lines are N samples apart, so the gap between two lines' times
    is a whole number of lines; the rate is then a straight-line fit over every line"""
    if len(lines) < 2:
        raise SystemExit("too few q lines (%d)" % len(lines))
    ks = float(info.get("ks", 5490))
    r2 = float(info.get("r2", 27))
    T = 64 * 16 * r2 / (ks * 1e3) * CLK          # cycles a sample, from the gain line
    t = np.array([tu for tu, _ in lines], float)
    k = [N - 1]
    for i in range(1, len(t)):
        if i >= 8:                               # the local rate from the lines so far
            T = (t[i - 1] - t[max(0, i - 200)]) / (k[-1] - k[max(0, i - 200)])
        k.append(k[-1] + N * max(1, int(round((t[i] - t[i - 1]) / (N * T)))))
    k = np.array(k)
    T, c = np.polyfit(k, t, 1)
    z = np.zeros(k[-1] + 1, complex)
    for ki, (_, s) in zip(k, lines):
        z[ki - N + 1:ki + 1] = s
    return z, CLK / T, c / CLK


def decode(z, rate, fcs=(0.0,), span=20.0):
    """-> [(offset Hz from WSPR_AUDIO, start s, drift Hz, score, text)]"""
    from scipy.signal import resample
    from sim.k6digi import wsprref
    out = []
    t = np.arange(len(z)) / rate
    for fc in fcs:
        y = resample(z * np.exp(-2j * np.pi * fc * t), int(round(len(z) * WSPR_RATE / rate)))
        for text, f0, dt, drift, score in wsprref.decode_baseband(y, WSPR_RATE, span):
            if text not in [o[4] for o in out]:
                out.append((fc + f0, dt, drift, score, text))
    return out


def cmd_decode(a):
    info, lines = parse(a.log)
    z, rate, t0 = baseband(info, lines)
    lost = len(z) // N - len(lines)
    print("%d lines, %d lost, %.1f s at %.3f Hz, f=%s cal=%s" % (
        len(lines), lost, len(z) / rate, rate, info.get("f"), info.get("cal")), file=sys.stderr)
    fcs = [a.fc] if a.span <= 20 else list(a.fc + np.arange(-a.span + 20, a.span - 19, 30.0))
    res = decode(z, rate, fcs, min(a.span, 20.0))
    f = float(info.get("f", 1500))
    for off, dt, drift, score, text in res:
        print("%7.2f %6.2f %5.1f %5.2f  %s" % (f + off, dt, drift, score, text))
    if not res:
        print("no decode", file=sys.stderr)
    return 0 if res else 1


def cmd_iq(a):
    info, lines = parse(a.log)
    z, rate, t0 = baseband(info, lines)
    np.savez(a.out, z=z, rate=rate, t0=t0, f=float(info.get("f", 1500)))
    print("%s: %d samples at %.3f Hz" % (a.out, len(z), rate))


def cmd_wav(a):
    from scipy.io import wavfile
    from sim.k6digi import wsprref
    fs = 12000
    x = np.concatenate([np.zeros(int(a.lead * fs)), wsprref.synth(wsprref.encode(a.text), fs, a.f0)
                        * a.peak, np.zeros(fs)])
    wavfile.write(a.out, fs, np.round(x * 32767).astype(np.int16))
    print("%s: %r, tone 0 at %.1f Hz, %.1f s" % (a.out, a.text, a.f0, len(x) / fs))


def mark_time(now, before=10.0, carrier=1.2, setup=2.0):
    """-> (when the mark's carrier drops, the slot it marks): the first slot (an even UTC minute
    + 1 s) whose mark can still be keyed whole after `setup` s"""
    slot = (int(now) // 120) * 120 + 1.0
    while slot - before - carrier - setup < now:
        slot += 120
    return slot - before, slot


def cmd_sync(a):
    """the sync mark is the bench's CW ID itself (Jeff 2026-11-07: every test transmission carries
    AG7EW's ident, no bare carriers): the IC-7100 keys, plays the ID, and unkeys at the mark's
    time by the clock, not by the end of the audio. A separate ID first would be a mark of its
    own, at the wrong time, so the mark is always the ID."""
    import subprocess
    import time
    import k6_bench as b
    lat = 0.05                                     # rigctl's T 0 to the carrier gone (estimate)
    wav = b.cwid_wav()
    d = b.wav_seconds(wav) + 0.8                   # the ID and a margin for paplay's start
    if a.dry_run:
        end, slot = mark_time(time.time(), a.before, carrier=d)
        print("dry run: mark (CW ID, %.1f s) %s-%s UTC, slot %s UTC"
              % (d, b.utc(end - d), b.utc(end), b.utc(slot)))
        return 0
    rig = b.rigctl(a.rigctld)
    with b.ptt_lock():
        hz = b.rig_hz(rig)
        if hz != a.freq or not b.in_band(hz):
            raise SystemExit("refusing: IC-7100 on %d Hz, not %d" % (hz, a.freq))
        end, slot = mark_time(time.time(), a.before, carrier=d)
        time.sleep(max(0.0, end - d - 0.2 - time.time()))     # keyed() holds 0.2 s before its body
        with b.keyed(rig, kind="id", what="WSPR sync mark (CW ID)"):
            p = subprocess.Popen(["paplay", "--device=%s" % b.SINK, wav])
            time.sleep(max(0.0, end - lat - time.time()))
        off = time.time()
        if p.poll() is None:
            p.kill()
            print("the ID was still playing at the drop: cut", file=sys.stderr)
    print("sync mark (CW ID) dropped at %s UTC (%.3f s late); the K6's first WSPR slot %s UTC"
          % (b.utc(off), off - end, b.utc(slot)))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("decode")
    p.add_argument("log")
    p.add_argument("--fc", type=float, default=0.0, help="Hz from WSPR_AUDIO to search around")
    p.add_argument("--span", type=float, default=50.0,
                   help="Hz either side of --fc (searched in 30 Hz steps past 20)")
    p = sub.add_parser("iq")
    p.add_argument("log")
    p.add_argument("out")
    p = sub.add_parser("wav")
    p.add_argument("out")
    p.add_argument("--text", default="AG7EW CN87 10")
    p.add_argument("--f0", type=float, default=1500.0)
    p.add_argument("--peak", type=float, default=0.5)
    p.add_argument("--lead", type=float, default=1.0)
    p = sub.add_parser("sync")
    p.add_argument("--before", type=float, default=10.0, help="s from the drop to the slot (WSPR_SYNC_MS)")
    p.add_argument("--freq", type=int, default=440_850_000)
    p.add_argument("--rigctld", default=None)
    p.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    return {"decode": cmd_decode, "iq": cmd_iq, "wav": cmd_wav, "sync": cmd_sync}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
