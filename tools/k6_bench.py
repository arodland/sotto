#!/usr/bin/env python3
"""The hamlet bench's transmit rules in one place: the PTT lock, the CW ID keeper, and the state
they share. Every tool that keys the IC-7100 goes through here (docs/hamlet-permissions.md).

  python3 tools/k6_bench.py paths              where everything lives (SOTTO_WORK, default ~/sotto-work)
  python3 tools/k6_bench.py status             last CW ID, last data TX, the keeper, the lock
  python3 tools/k6_bench.py send WAV           key and play one WAV on 70 cm (sh tools/k6_send.sh)
         [--k6log LOG | --freq HZ]             ... and only if the rig matches the K6's last CFG / HZ
  python3 tools/k6_bench.py id [WHY]           a CW ID now
  python3 tools/k6_bench.py idkeeper           the CW ID keeper (the k6-idkeeper user unit runs it)
  python3 tools/k6_bench.py wavs               (re)make the ID and K6 reset WAVs (needs the checkout)
  python3 tools/k6_bench.py install [--unit]   copy this file and the reset trigger into
                                               $SOTTO_WORK/bin, make the WAVs, write the unit

What they share, under $SOTTO_WORK/state/ (it outlives reboots, unlike a session's /tmp):
  ptt.lock          flock: one keyer at a time, data or CW ID
  ptt.lock.last_tx  when the last data transmission ended (epoch s)
  ptt.lock.last_id  when the last CW ID ended
  txlog.txt         one line per data transmission: start, end, what
  cwid.wav          'AG7EW SOTTO TEST', MCW 800 Hz, 20 wpm, level 0.4
  reset.wav         the K6 reset control packet (modem.k6 ctl --phy F --seq 201 --action reset)
and $SOTTO_WORK/logs/k6-cwid.txt, one line per CW ID (~/k6-cwid.txt links there).

The keeper (FCC 97.119): a session is active while a data TX ended less than 10 min ago. While
active, it IDs when 9 min have passed since the last ID; 2 min after the last data TX it sends a
final ID unless one came after it (but not for a TX over 30 min old, such as one from before a
reboot). It only keys on 70 cm or 2 m, never anywhere else.

Standard library only, so the copy in $SOTTO_WORK/bin keeps working whatever commit ~/sotto has
checked out. Opens no serial port: PTT goes through rigctld (/dev/ttyUSB0), never /dev/ttyUSB2.
SPDX-License-Identifier: MIT
"""
import argparse
import contextlib
import fcntl
import os
import shutil
import subprocess
import sys
import time
import wave

SINK = "alsa_output.usb-Burr-Brown_from_TI_USB_Audio_CODEC-00.analog-stereo"
PAGER_HZ = 440_850_000
BAND_70CM = (430_010_000, 449_990_000)        # north of Line A: nothing at 420-430
BANDS_ID = (BAND_70CM, (144_010_000, 147_990_000))
ID_TEXT = os.environ.get("CWID", "AG7EW SOTTO TEST")
SESSION_S, ID_EVERY_S, END_AFTER_S = 600, 540, 120
END_STALE_S = 1800                            # no "end of session" ID for a TX older than this
                                              # (state left from before a reboot or a keeper outage)
ID_FIRST_S = 510                              # send: ID first when the last one is older (8.5 min)
LOCK_WAIT_S = 90.0
HERE = os.path.dirname(os.path.abspath(__file__))


# ---- where things live ----

def work():
    """$SOTTO_WORK, default ~/sotto-work: every bench file outside the checkout goes under it"""
    return os.path.abspath(os.path.expanduser(os.environ.get("SOTTO_WORK") or "~/sotto-work"))


def path(name):
    """a bench file by name: lock, last_tx, last_id, txlog, cwid_wav, reset_wav, cwid_log,
    keeper (the keeper's pid file), bin"""
    w = work()
    st = os.path.join(w, "state")
    return {"lock": os.path.join(st, "ptt.lock"), "last_tx": os.path.join(st, "ptt.lock.last_tx"),
            "last_id": os.path.join(st, "ptt.lock.last_id"), "txlog": os.path.join(st, "txlog.txt"),
            "cwid_wav": os.path.join(st, "cwid.wav"), "reset_wav": os.path.join(st, "reset.wav"),
            "keeper": os.path.join(st, "idkeeper.pid"), "cwid_log": os.path.join(w, "logs", "k6-cwid.txt"),
            "bin": os.path.join(w, "bin")}[name]


def repo():
    """the sotto checkout (for modem.k6): this file's parent when it is in one, else $SOTTO_REPO
    or ~/sotto"""
    up = os.path.dirname(HERE)
    if os.path.isdir(os.path.join(up, "modem")):
        return up
    return os.path.abspath(os.path.expanduser(os.environ.get("SOTTO_REPO") or "~/sotto"))


def _read(name):
    try:
        return float(open(path(name)).read())
    except (OSError, ValueError):
        return 0.0


def _write(name, value):
    p = path(name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        f.write(f"{value:.3f}")
    os.replace(tmp, p)


def _append(name, line):
    p = path(name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a") as f:
        f.write(line + "\n")


def utc(t):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t))


def note_tx(t0, t1, what):
    _write("last_tx", t1)
    _append("txlog", f"{t0:.3f} {t1:.3f} {what}")


def note_id(t0, t1, why, text=ID_TEXT):
    _write("last_id", t1)
    _append("cwid_log", f"{utc(t0)} UTC  CW ID '{text}' ({why}), {t1 - t0:.1f} s")


# ---- the PTT lock ----

_held = 0


@contextlib.contextmanager
def ptt_lock(wait=LOCK_WAIT_S):
    """one keyer at a time across every process on the bench. Re-entrant within a process."""
    global _held
    if _held:
        _held += 1
        try:
            yield
        finally:
            _held -= 1
        return
    p = path("lock")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a") as lk:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise SystemExit(f"the PTT lock ({p}) was held for {wait:.0f} s: another keyer is running")
                time.sleep(0.1)
        _held = 1
        try:
            yield
        finally:
            _held = 0
            fcntl.flock(lk, fcntl.LOCK_UN)


def lock_free():
    """True when nobody holds the PTT lock (a probe; it takes and drops it at once)"""
    p = path("lock")
    if not os.path.exists(p):
        return True
    with open(p, "a") as lk:
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(lk, fcntl.LOCK_UN)
        return True


# ---- the rig ----

def rigctl(rigctld=None):
    """rig(*args) -> rigctl's first output line, through rigctld (rigctl -m 2)"""
    base = ["rigctl", "-m", "2"] + (["-r", rigctld] if rigctld else [])

    def rig(*args):
        out = subprocess.run(base + [str(v) for v in args], check=True, capture_output=True, text=True)
        return out.stdout.strip().split("\n")[0].strip()
    return rig


def rig_hz(rig):
    f = rig("f")
    if not f.isdigit():
        raise SystemExit(f"refusing: rigctl f gave '{f}'")
    return int(f)


def in_band(hz, bands=(BAND_70CM,)):
    return any(lo <= hz <= hi for lo, hi in bands)


def simplex(rig):
    """transmit where the rig listens: VFO A, split off, no repeater shift, set and read back right
    before PTT. The IC-7100's auto repeater turns DUP on by itself in the 70 cm repeater sub-bands
    (the CW recorder parks there), and on 2026-11-08 a CW ID went out shifted after that."""
    rig("V", "VFOA")
    rig("S", 0, "VFOA")
    rig("R", "0")
    shift = (rig("r").splitlines() or [""])[0]
    split = (rig("s").splitlines() or [""])[0]
    if shift != "None" or split != "0":
        raise SystemExit(f"refusing to key: repeater shift {shift!r}, split {split!r} after clearing them")


@contextlib.contextmanager
def keyed(rig, kind="data", what="", text=ID_TEXT, dry_run=False, lead=0.2):
    """PTT on for the with-body, under the lock, then off (always). Afterwards the keeper's state:
    kind "data" sets last_tx and a txlog line, kind "id" sets last_id and a k6-cwid.txt line.
    dry_run: no lock and no state (rig is the caller's, which prints instead)"""
    with (contextlib.nullcontext() if dry_run else ptt_lock()):
        t = rig("t")
        if t != "0":
            raise SystemExit(f"the IC-7100 is already keyed (t={t}) by something outside the PTT lock")
        if not dry_run:
            simplex(rig)
        t0 = time.time()
        try:
            rig("T", 1)
            time.sleep(lead)
            yield
        finally:
            rig("T", 0)
            if not dry_run:
                (note_id(t0, time.time(), what or "CW ID", text) if kind == "id"
                 else note_tx(t0, time.time(), what or "data"))


def play(wav, device=SINK, dry_run=False, max_s=None):
    if dry_run:
        print("dry run: paplay", wav)
        return
    cmd = ["paplay", f"--device={device}", wav]
    if max_s:
        r = subprocess.run(["timeout", str(int(max_s))] + cmd).returncode
        if r:
            print(f"paplay ended with {r} (124: hit {max_s:.0f} s)", file=sys.stderr)
    else:
        subprocess.run(cmd, check=True)


def key_and_play(rig, wav, device=SINK, dry_run=False, kind="data", what="", text=ID_TEXT, tail=0.05,
                 max_s=None):
    """the bench's keying sequence: lock, t must read 0, T 1, 0.2 s, play, tail, T 0, record"""
    with keyed(rig, kind, what or os.path.basename(wav), text, dry_run):
        play(wav, device, dry_run, max_s)
        time.sleep(tail)


def wav_seconds(p):
    with wave.open(p) as w:
        return w.getnframes() / w.getframerate()


def cwid_wav():
    p = path("cwid_wav")
    if not os.path.exists(p):
        make_wavs(only_id=True)
    return p


def send_id(rig, why, device=SINK, dry_run=False):
    key_and_play(rig, cwid_wav(), device, dry_run, kind="id", what=why)


def id_due_first(now=None):
    """send's rule: the last CW ID is older than 8.5 min"""
    return (now or time.time()) - _read("last_id") > ID_FIRST_S


def keeper_due(now, last_tx, last_id):
    """why the keeper should ID now, or None"""
    if now - last_tx < SESSION_S and now - last_id >= ID_EVERY_S:
        return "9-min periodic"
    if last_tx > last_id and END_AFTER_S <= now - last_tx < END_STALE_S:
        return "end of session"
    return None


def k6_cfg_hz(log):
    """the freq= of the last 'K6 CFG' line in a harness log, or None"""
    import re
    hz = None
    for ln in open(log, errors="replace"):
        if "K6 CFG" in ln:
            m = re.search(r"freq=(\d+)", ln)
            hz = int(m.group(1)) if m else hz
    return hz


# ---- the WAVs ----

def write_wav(p, x, fs=48000):
    import numpy as np
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with wave.open(p, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(fs)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())


def make_wavs(only_id=False):
    r = repo()
    if not os.path.isdir(os.path.join(r, "modem")):
        raise SystemExit(f"no sotto checkout at {r} to make the WAVs from: set SOTTO_REPO")
    sys.path.insert(0, r)
    from modem import k6
    write_wav(path("cwid_wav"), k6.cw(ID_TEXT, 48000, 20, 800.0, 0.4))
    print("wrote", path("cwid_wav"), f"'{ID_TEXT}'")
    if only_id:
        return
    subprocess.run([sys.executable, "-m", "modem.k6", "ctl", path("reset_wav"), "--phy", "F", "--seq", "201",
                    "--action", "reset"], cwd=r, check=True)
    print("wrote", path("reset_wav"))


# ---- the commands ----

def cmd_paths(a):
    print("SOTTO_WORK", work())
    for n in ("bin", "lock", "last_tx", "last_id", "txlog", "cwid_wav", "reset_wav", "keeper", "cwid_log"):
        print(f"{n:9s} {path(n)}{'' if os.path.exists(path(n)) else '  (missing)'}")


def keeper_pid():
    """the running keeper's pid, or None"""
    p = path("keeper")
    if not os.path.exists(p):
        return None
    with open(p, "a+") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            f.seek(0)
            return f.read().strip() or "?"
        fcntl.flock(f, fcntl.LOCK_UN)
    return None


def cmd_status(a):
    now = time.time()
    tx, idt = _read("last_tx"), _read("last_id")
    age = lambda t: f"{utc(t)} UTC, {(now - t) / 60:.1f} min ago" if t else "never"
    print(f"last data TX  {age(tx)}")
    print(f"last CW ID    {age(idt)}")
    print(f"ID due now    {keeper_due(now, tx, idt) or 'no'}")
    pid = keeper_pid()
    print(f"keeper        {'running, pid ' + pid if pid else 'NOT running'}")
    print(f"PTT lock      {'free' if lock_free() else 'held (someone is keying)'}")


def cmd_send(a):
    if not os.path.isfile(a.wav):
        raise SystemExit(f"no such file: {a.wav}")
    dur = wav_seconds(a.wav)
    if dur > a.max_s:
        raise SystemExit(f"refusing: {a.wav} is {dur:.1f} s, over --max-s {a.max_s:.0f}")
    rig = rigctl(a.rigctld)
    with ptt_lock():
        hz = rig_hz(rig)
        if not in_band(hz):
            raise SystemExit(f"refusing: IC-7100 on {hz} Hz, outside 430.010-449.990 MHz")
        if a.k6log:
            k6hz = k6_cfg_hz(a.k6log)
            if k6hz != hz:
                raise SystemExit(f"refusing: K6 on {k6hz} (last CFG in {a.k6log}) vs IC-7100 {hz}")
        elif a.freq and hz != a.freq:
            raise SystemExit(f"refusing: IC-7100 on {hz} Hz, not {a.freq}")
        if not a.no_id and (a.force_id or id_due_first()):
            send_id(rig, "before a data send", a.device)
            time.sleep(1.0)
        t0 = time.time()
        key_and_play(rig, a.wav, a.device, tail=a.tail, max_s=a.max_s)
    print(f"sent {a.wav} ({dur:.1f} s) on {hz} Hz from {utc(t0)} UTC; t0={t0:.3f}; PTT now {rig('t')}")


def cmd_id(a):
    rig = rigctl(a.rigctld)
    with ptt_lock():
        hz = rig_hz(rig)
        if not in_band(hz, BANDS_ID):
            raise SystemExit(f"refusing: IC-7100 on {hz} Hz, not 70 cm or 2 m")
        send_id(rig, a.why, a.device)
    print(f"CW ID '{ID_TEXT}' on {hz} Hz ({a.why}); PTT now {rig('t')}")


def cmd_idkeeper(a):
    p = path("keeper")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    me = open(p, "a+")
    try:
        fcntl.flock(me, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        me.seek(0)
        raise SystemExit(f"a keeper is already running (pid {me.read().strip()})")
    me.seek(0)
    me.truncate()
    me.write(str(os.getpid()))
    me.flush()
    rig = rigctl(a.rigctld)
    cwid_wav()
    say = lambda s: print(f"{time.strftime('%H:%M:%S', time.gmtime())} UTC {s}", flush=True)
    say(f"idkeeper: '{ID_TEXT}', state in {os.path.dirname(path('lock'))}")
    while True:
        why = keeper_due(time.time(), _read("last_tx"), _read("last_id"))
        if why:
            try:
                with ptt_lock(300.0):
                    why = keeper_due(time.time(), _read("last_tx"), _read("last_id"))   # again, now we hold it
                    hz = rig_hz(rig) if why else 0
                    if why and not in_band(hz, BANDS_ID):
                        say(f"ID due ({why}) but the IC-7100 is on {hz} Hz, not 70 cm or 2 m: not keying")
                        time.sleep(50)
                    elif why:
                        send_id(rig, why, a.device)
                        say(f"CW ID ({why}) on {hz} Hz, PTT now {rig('t')}")
            except (SystemExit, subprocess.CalledProcessError, OSError) as e:
                say(f"no ID ({why}): {e}")
                time.sleep(20)
        time.sleep(a.every)


def cmd_wavs(a):
    make_wavs()


def unit_text():
    w = work()
    return f"""[Unit]
Description=sotto K6 bench CW ID keeper (AG7EW, FCC 97.119; tools/k6_bench.py)
After=rigctld.service
Wants=rigctld.service

[Service]
Environment=SOTTO_WORK={w}
ExecStart=/usr/bin/python3 {os.path.join(w, 'bin', 'k6_bench.py')} idkeeper
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
"""


def cmd_install(a):
    b = path("bin")
    for d in (b, os.path.dirname(path("lock")), os.path.dirname(path("cwid_log"))):
        os.makedirs(d, exist_ok=True)
    for f in ("k6_bench.py", "k6-reset-trigger.sh"):
        shutil.copy2(os.path.join(HERE, f), os.path.join(b, f))
        os.chmod(os.path.join(b, f), 0o755)
        print("installed", os.path.join(b, f))
    make_wavs()
    if a.unit:
        d = os.path.expanduser("~/.config/systemd/user")
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, "k6-idkeeper.service"), "w").write(unit_text())
        print("wrote", os.path.join(d, "k6-idkeeper.service"),
              "\nthen: systemctl --user daemon-reload && systemctl --user enable --now k6-idkeeper")
    else:
        print("(the keeper's unit: install --unit)")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rigctld", help="host:port (default rigctl's own, localhost:4532)")
    ap.add_argument("--device", default=SINK, help="the IC-7100's USB audio sink")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("paths").set_defaults(fn=cmd_paths)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    s = sub.add_parser("send", help="key and play one WAV on 70 cm")
    s.add_argument("wav")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--k6log", help="the harness log: the rig must match the K6's last CFG freq=")
    g.add_argument("--freq", type=int, help="the rig must read exactly this (Hz)")
    s.add_argument("--max-s", type=float, default=180.0, help="refuse a longer WAV; cut play here (180)")
    s.add_argument("--tail", type=float, default=0.5, help="PTT hold after the audio ends, s (0.5)")
    s.add_argument("--no-id", action="store_true", help="no CW ID first even when one is due")
    s.add_argument("--force-id", action="store_true", help="a CW ID first in any case")
    s.set_defaults(fn=cmd_send)
    i = sub.add_parser("id", help="a CW ID now")
    i.add_argument("why", nargs="?", default="by hand")
    i.set_defaults(fn=cmd_id)
    k = sub.add_parser("idkeeper", help="the CW ID keeper (runs until stopped)")
    k.add_argument("--every", type=float, default=10.0, help="check every so many s (10)")
    k.set_defaults(fn=cmd_idkeeper)
    sub.add_parser("wavs").set_defaults(fn=cmd_wavs)
    n = sub.add_parser("install")
    n.add_argument("--unit", action="store_true", help="also write ~/.config/systemd/user/k6-idkeeper.service")
    n.set_defaults(fn=cmd_install)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
