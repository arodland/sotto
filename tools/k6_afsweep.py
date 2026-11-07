#!/usr/bin/env python3
"""The IC-7100 side of the K6 AF sweep (c/k6/mod/afsweep.c, docs/k6-afsweep.md).

  python3 tools/k6_afsweep.py wav OUTDIR            the test audio, one WAV per pass
  python3 tools/k6_afsweep.py run [--dry-run]       key the IC-7100 (rigctld on :4532) for the
                                                    three passes, FM, AM, USB, then a CW ID
  python3 tools/k6_afsweep.py log LOG               the module's step lines as a table

Each pass is 1 s of a 1 kHz tone (the module starts its 16 steps when it sees the carrier), then
16 blocks of 4 s, one per AF value, each: 0.6 s 1 kHz, a 400-2400 Hz sweep for 1.4 s, 1 s of
700 + 1900 Hz, and 1 s of a 300 Hz / 3 kHz pair. The sender unkeys for 4 s between passes so the
module sees the carrier drop, and changes the IC-7100's mode (data modes, so the USB audio is
the modulation source: PKTFM, PKTAM, PKTUSB in hamlib). The frequency must already be the K6's.
MIT licence."""
import argparse
import os
import re
import subprocess
import sys
import tempfile
import time
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import k6_bench as bench  # noqa: E402  (the PTT lock and the CW ID keeper's state)

TOOL = "k6_afsweep"

FS = 48000
PASSES = (("fm", "PKTFM"), ("am", "PKTAM"), ("usb", "PKTUSB"))


def tone(f, s, lv):
    t = np.arange(int(s * FS)) / FS
    return lv * np.sin(2 * np.pi * np.outer(np.atleast_1d(f), t)).sum(0) / len(np.atleast_1d(f))


def ramp(x, ms=5):
    r = int(FS * ms / 1000)
    w = np.minimum(1, np.minimum(np.arange(len(x)), np.arange(len(x))[::-1]) / r)
    return x * w


def block(lv):
    t = np.arange(int(1.4 * FS)) / FS
    f = 400 * (6 ** (t / 1.4))
    sweep = lv * np.sin(2 * np.pi * np.cumsum(f) / FS)
    return np.concatenate([ramp(tone(1000, 0.6, lv)), ramp(sweep), ramp(tone([700, 1900], 1.0, lv)),
                           ramp(tone([300, 3000], 1.0, lv))])


def pass_audio(lv):
    return np.concatenate([ramp(tone(1000, 1.0, lv))] + [block(lv) for _ in range(16)])


def write(path, x):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(FS)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())


def cmd_wav(a):
    os.makedirs(a.outdir, exist_ok=True)
    for name, _ in PASSES:
        p = os.path.join(a.outdir, f"afsweep-{name}.wav")
        x = pass_audio(a.level)
        write(p, x)
        print(f"wrote {p}: {len(x) / FS:.1f} s at peak {a.level}")


def rig(a, *args):
    cmd = ["rigctl", "-m", "2", "-r", a.rigctld] + [str(v) for v in args]
    if a.dry_run:
        print("dry run:", " ".join(cmd))
        return "0"
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.strip()


def key_and_play(a, path, kind="data", text=bench.ID_TEXT):
    """the bench's keying sequence under its PTT lock (tools/k6_bench.py); kind "id" for a CW ID"""
    bench.key_and_play(lambda *v: rig(a, *v), path, a.device, a.dry_run, kind,
                       f"{TOOL} {os.path.basename(path)}", text)


def cmd_run(a):
    f = rig(a, "f")
    if not a.dry_run and int(f) != a.freq:
        sys.exit(f"IC-7100 on {f} Hz, not {a.freq}: set both radios to the same frequency first")
    with tempfile.TemporaryDirectory() as d:
        for i, (name, mode) in enumerate(PASSES):
            if a.only and name not in a.only.split(","):
                continue
            p = os.path.join(d, f"{name}.wav")
            write(p, pass_audio(a.level))
            rig(a, "M", mode, 12000 if mode == "PKTFM" else 0)  # FM-D wide, as the bench runs
            time.sleep(1.0)
            print(f"{time.strftime('%H:%M:%S')} pass {name} ({mode}): 65 s", flush=True)
            key_and_play(a, p)
            time.sleep(4.0)
        rig(a, "M", "PKTFM", 12000)  # 0 picks FM-D narrow (6 kHz): half the deviation
        time.sleep(1.0)
        # the station ID (FCC 97.119), in FM like the ID keeper
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
        from modem import k6
        p = os.path.join(d, "id.wav")
        write(p, k6.cw(a.call + " SOTTO TEST", FS))
        print(f"{time.strftime('%H:%M:%S')} CW ID", flush=True)
        key_and_play(a, p, "id", a.call + " SOTTO TEST")


def cmd_log(a):
    rows = [m.groups() for m in (re.search(r"s (fm|am|usb) (\d+) (.*)$", ln.strip())
                                 for ln in open(a.log, errors="replace")) if m]
    if not rows:
        sys.exit("no afsweep step lines")
    print("pass AF  rssi dBm   noise   glitch   AF level")
    for mode, v, rest in rows:
        n = [int(x) for x in re.findall(r"-?\d+", rest)]
        print(f"{mode:4s} {int(v):2d}  {n[0]:4d}..{n[1]:<4d} {n[2]:3d}..{n[3]:<3d} "
              f"{n[4]:3d}..{n[5]:<3d} {n[6]:3d}..{n[7]:<3d}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("wav")
    w.add_argument("outdir")
    w.add_argument("--level", type=float, default=0.15)
    w.set_defaults(fn=cmd_wav)
    r = sub.add_parser("run")
    r.add_argument("--level", type=float, default=0.15, help="peak audio (default 0.15: under the "
                   "IC-7100's FM limiter, which starts near 0.25 at 1 kHz)")
    r.add_argument("--freq", type=int, default=440850000)
    r.add_argument("--rigctld", default="localhost:4532")
    r.add_argument("--device", default="alsa_output.usb-Burr-Brown_from_TI_USB_Audio_CODEC-00.analog-stereo")
    r.add_argument("--call", default="AG7EW")
    r.add_argument("--only", help="passes to send, e.g. am,usb")
    r.add_argument("--dry-run", action="store_true")
    r.set_defaults(fn=cmd_run)
    lg = sub.add_parser("log")
    lg.add_argument("log")
    lg.set_defaults(fn=cmd_log)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
