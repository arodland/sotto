"""Upload a K6 RAM module with ARQ: send it, listen for the K6's report, resend only what it lacks.

  python3 tools/k6_upload.py c/k6/mod/hello.c --key-file ~/.config/sotto/k6-mod.key
  python3 tools/k6_upload.py mod-ifcal.bin --api-level 4 --telemetry 4 --key-file ... --payload XL

On hamlet: the IC-7100 (rigctld on :4532) sends the module as F/1800 audio with magic 0xAE, so
the K6 answers. While the tool waits, it records the IC-7100's receive audio (parec) and decodes
the K6's 0xAF reports (modem/k6mod.py):

  partway   the K6 lists the chunks it lacks: they go again, same serial and transfer id
  ran       the module ran; its return value, run time, free stack and say lines (--telemetry)
  refused   bad header, wrong key, no key, or an old serial: no point resending
  nothing   no report heard: everything goes again (a K6 that ran it already answers a repeat)

The K6 waits 2 s after the last packet and for a clear channel, keys up on 440.850 only (its
tx_on takes about 1.4 s), sends the report twice, then its own CW ID. The tool sends the PC's CW
ID at the end. Before the first key-up the K6 needs an image from 2026-11-06 on (API level 4).
Never use ttyUSB2 (the K6's PTT line); this tool opens no serial port.
SPDX-License-Identifier: MIT
"""
import argparse
import os
import subprocess
import sys
import tempfile
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from modem import k6mod  # noqa: E402

FS = 48000
PLAY = "alsa_output.usb-Burr-Brown_from_TI_USB_Audio_CODEC-00.analog-stereo"
REC = "alsa_input.usb-Burr-Brown_from_TI_USB_Audio_CODEC-00.analog-stereo"


def next_step(st, count, xfer):
    """The K6's newest status report (or None) -> ("send", chunk list) | ("done", why) |
    ("stop", why). count: the image's chunks; xfer: this upload's transfer id."""
    if st is None:
        return "send", list(range(count))
    if st["xfer"] != xfer:
        return "stop", f"the K6 reported transfer {st['xfer']}, not {xfer} (another upload?)"
    if st["state"] == "ran":
        return "done", f"ran: returned {st['ret']} in {st['ms']} ms, {st['stack_free']} B stack free"
    if st["state"] == "refused":
        return "stop", f"refused: {k6mod.RESULTS.get(st['r'], st['r'])}"
    if not st["missing"]:
        return "send", list(range(count))
    return "send", st["missing"]


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


def mod_wav(a, path, serial, xfer, only=None):
    cmd = [sys.executable, "-m", "modem.k6", "mod", path, a.source, "--key-file", a.key_file,
           "--serial", str(serial), "--xfer", str(xfer), "--phy", a.phy, "--baud", str(a.baud),
           "--payload", a.payload, "--level", str(a.level), "--arq", "--telemetry", str(a.telemetry)]
    if a.api_level:
        cmd += ["--api-level", str(a.api_level)]
    if only is not None:
        cmd += ["--only", ",".join(map(str, only))]
    r = subprocess.run(cmd, check=True, capture_output=True, text=True,
                       cwd=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    return r.stderr.strip()


def chunks_of(a):
    """the image's chunk count, from the same build `modem.k6 mod` makes"""
    if a.source.endswith(".bin"):
        code = open(a.source, "rb").read()
    else:
        code, _ = k6mod.build(a.source)
    n = k6mod.HDR + len(code) + k6mod.TAG
    dl = k6mod.data_bytes(128 if a.payload == "XL" else 64)
    return -(-n // dl)


class Recorder:
    """parec of the IC-7100's receive audio into a raw file, read back while it grows"""

    def __init__(self, a, d):
        self.path = os.path.join(d, "rx.raw")
        self.p = None
        if not a.dry_run:
            self.p = subprocess.Popen(["parec", f"--device={a.rec_device}", "--format=s16le",
                                       f"--rate={FS}", "--channels=1", "--raw", self.path])

    def samples(self):
        """samples in the file so far: parec writes behind real time, so mark positions by this"""
        return os.path.getsize(self.path) // 2 if os.path.exists(self.path) else 0

    def audio(self, since=0):
        if not os.path.exists(self.path):
            return np.zeros(0)
        x = np.fromfile(self.path, np.int16).astype(float) / 32768
        return x[since:]

    def stop(self):
        if self.p:
            self.p.terminate()
            self.p.wait()


def listen(a, rec, since, xfer, seconds):
    """decode the recording from sample `since` on until a report for this transfer is complete
    (status and every text part) or seconds pass -> (status, text, every report seen)"""
    t_end = time.time() + seconds
    st, text, reps = None, "", []
    while time.time() < t_end:
        time.sleep(1.0)
        if a.dry_run:
            break
        x = rec.audio(since)
        if len(x) < FS:              # under a second of new audio yet: nothing to decode
            continue
        reps = [(t + since / FS, r) for t, r in k6mod.reports(x, FS, a.phy, a.baud)]
        mine = [(t, r) for t, r in reps if r["kind"] == "text" or r.get("xfer") == xfer]
        st, text = k6mod.collect(mine)
        if st is not None and "lost]" not in text:
            if st["state"] != "ran" or st["parts"] <= 1 or text:
                time.sleep(4.0)          # its CW ID; then the channel is ours again
                break
    return st, text, reps


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("source", help="module C source or its built .bin (CI's mod-*.bin)")
    ap.add_argument("--key-file", required=True)
    ap.add_argument("--api-level", type=int)
    ap.add_argument("--telemetry", type=int, default=4, help="say-line parts to send back (0-12, default 4)")
    ap.add_argument("--payload", choices=("L", "XL"), default="XL")
    ap.add_argument("--phy", choices="DF", default="F")
    ap.add_argument("--baud", type=float, default=1800)
    ap.add_argument("--level", type=float, default=0.3)
    ap.add_argument("--rounds", type=int, default=4, help="sends at most (default 4)")
    ap.add_argument("--run-s", type=float, default=10.0,
                    help="how long the module may run before its report (default 10 s)")
    ap.add_argument("--freq", type=int, default=440_850_000)
    ap.add_argument("--rigctld", default="localhost:4532")
    ap.add_argument("--device", default=PLAY)
    ap.add_argument("--rec-device", default=REC)
    ap.add_argument("--call", default="AG7EW")
    ap.add_argument("--keep", metavar="RAW", help="copy the recording here at the end (raw s16le mono 48 kHz)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.freq != 440_850_000:
        sys.exit("the K6 answers on 440.850 only")
    f = rig(a, "f")
    if not a.dry_run and int(f) != a.freq:
        sys.exit(f"IC-7100 on {f} Hz, not {a.freq}: set both radios to 440.850 first")
    rig(a, "M", "PKTFM", 12000)      # the wide FM-D filter: passband 0 picks the 6 kHz one, half the deviation
    serial = int(time.time())
    xfer = serial & 0xFF
    count = chunks_of(a)
    todo = list(range(count))
    with tempfile.TemporaryDirectory() as d:
        rec = Recorder(a, d)
        try:
            for rnd in range(1, a.rounds + 1):
                w = os.path.join(d, f"up{rnd}.wav")
                print(f"{time.strftime('%H:%M:%S')} send {rnd}: {len(todo)} of {count} chunks "
                      f"(serial {serial}, xfer {xfer}); {mod_wav(a, w, serial, xfer, todo)}", flush=True)
                key_and_play(a, w)
                since = rec.samples()
                print(f"{time.strftime('%H:%M:%S')} listening for the K6's report", flush=True)
                st, text, _ = listen(a, rec, since, xfer, 25.0 + a.run_s)
                step, arg = next_step(st, count, xfer)
                if st is not None:
                    print(f"{time.strftime('%H:%M:%S')} K6: {st['state']}, {st['have']}/{st['count']} "
                          f"chunks, r={st['r']} ({k6mod.RESULTS.get(st['r'], '?')})", flush=True)
                else:
                    print(f"{time.strftime('%H:%M:%S')} no report heard", flush=True)
                if step == "send":
                    todo = arg
                    continue
                print(arg)
                if text:
                    print("K6 telemetry:\n" + text, end="" if text.endswith("\n") else "\n")
                break
            else:
                print(f"gave up after {a.rounds} sends")
        finally:
            rec.stop()
            if a.keep:
                import shutil
                shutil.copy(rec.path, a.keep)
                print(f"recording (s16le mono {FS} Hz) kept in {a.keep}")
            from modem import k6
            p = os.path.join(d, "id.wav")
            import soundfile as sf
            sf.write(p, k6.cw(a.call + " SOTTO TEST", FS), FS, subtype="PCM_16")
            print(f"{time.strftime('%H:%M:%S')} CW ID", flush=True)
            key_and_play(a, p)


if __name__ == "__main__":
    main()
