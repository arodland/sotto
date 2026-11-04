"""K6 voice pager test harness: listen to the Baofeng K6's serial report on its programming
cable, score each received packet against the spec's test message, and sweep transmit settings
through the IC-7100 with the K6's answers collected automatically.

  python -m modem.k6_harness listen --k6 /dev/ttyUSB2               live, one line per packet
  python -m modem.k6_harness listen --k6 /dev/ttyUSB2 --log run.txt ... and keep every line
  python -m modem.k6_harness listen --file run.txt                  analyse a saved log again
  python -m modem.k6_harness sweep --k6 /dev/ttyUSB2 --port /dev/ttyUSB0 --phy F \\
      --player paplay --speeds=-1,-0.5,0,0.5,1 --repeat 10           test packets per setting

The K6 must run the pager image with the serial report (firmware/k6-aura, VOICE MSG). It sends
one text line per event at 115200 8N1 and never reads the cable, because the cable's other
wire is the K6's PTT line. This harness only reads that port; it never writes to it. Set the
K6's PHY, frames and baud with its keys to match the sweep; `sweep` checks the K6's last
`K6 CFG` line and transmits nothing if they differ (--anyway sends regardless).

Every image reports its build, the sotto commit it was built from plus the UTC build time, at
power-on and at the end of each CFG line; `listen` and `sweep` print it, every --log line
carries it, and `sweep --expect-build 72b6e3b` transmits nothing until the K6 has reported a
build starting with that commit (and stops if it reboots into another).

The firmware's lines (one per event):

  K6 BOOT build=72b6e3b-20261104T0555Z
  K6 CFG why=enter chip=4819 freq=439500000 phy=F frames=L baud=1200 inv=0 soft=0 swap=1 ...
         ... build=72b6e3b-20261104T0555Z
  K6 STEP play id=42 stored=70        (playback progress: decoder reset, first frame, PWM ...)
  K6 FAULT pc=... lr=...               a CPU fault, then a reboot
  K6 PANIC src/app/pager.rs:812 ...    a Rust panic, then a reboot
  K6 PKT n=12 got=64 sync=N res=-1 magic=a7 rswap=1 soft=0 swap=1 be=3 bseq=0 bswap=1 binv=0
         blk=0,0,0,1,0,0,0,2 ev=2:0:0,1000:27:4,... good=0 bad=12 to=0 raw=58a31d...
         (one line on the wire; ev: each interrupt's flags in hex, ms, FIFO words read)
  K6 PLAY id=42 total=70 stored=70 got=70 packets=16/16 played=71 dec_mean_us=11800 ...
  K6 CAL sent tx=439500000

`raw` is the packet as the K6 read it from its FIFO, with the byte swap it had then (`rswap`),
before any inversion. The harness redoes the K6's own bit-error count (`be`) and adds a
per-byte error map and a bit-slip estimate.
SPDX-License-Identifier: MIT
"""
import argparse
import copy
import queue
import sys
import threading
import time

import numpy as np

from . import fsk, pager

TEST_FRAMES = b"".join(bytes([f & 0xFF, f >> 8, 0x11, 0x22, 0x33, 0x44]) for f in range(70))
_TEST = {}


def test_packets(profile):
    """The spec's 70-frame test message, whitened, as the K6 compares against (16 in L, 40 in S)."""
    if profile not in _TEST:
        pk = pager.PROFILES[profile].pack(TEST_FRAMES, 0x42 if profile == "L" else 5)
        _TEST[profile] = np.array([np.frombuffer(p, np.uint8) for p in pk])
    return _TEST[profile]


def parse_line(line):
    """'K6 KIND k=v ...' -> dict(kind=..., k=v ...), raw as bytes, numbers as int; else None."""
    line = line.strip()
    if not line.startswith("K6 "):
        return None
    parts = line.split()
    if len(parts) < 2:
        return None
    d = {"kind": parts[1]}
    words = []
    for p in parts[2:]:
        if "=" not in p:
            words.append(p)
            continue
        k, v = p.split("=", 1)
        if k == "raw":
            try:
                d[k] = bytes.fromhex(v)
            except ValueError:
                d[k] = b""
        elif k == "blk":
            d[k] = [int(x) for x in v.split(",") if x]
        elif k in ("magic", "chip", "build"):
            d[k] = v
        else:
            try:
                d[k] = int(v)
            except ValueError:
                d[k] = v
    if words:
        d["words"] = words
    return d


def _bits(a):
    return np.unpackbits(np.asarray(a, np.uint8), axis=-1)


def analyse(raw, rswap=1):
    """A packet as the K6 read it -> bit errors against the closest test-message packet and
    reading (as read, byte-swapped, inverted, both), like the firmware's sp_test_errors, plus:
    errors per byte, the first wrong bit, and the bit shift that best fits the packet's last
    quarter (a lost or extra bit shows as shift +-1 there with few errors)."""
    raw = np.frombuffer(bytes(raw), np.uint8)
    n = len(raw)
    if n not in (pager.L.payload, pager.S.payload):
        return None
    ref = test_packets("L" if n == pager.L.payload else "S")
    best = None
    for sw in (0, 1):
        r = raw.reshape(-1, 2)[:, ::-1].reshape(-1) if sw else raw
        for inv in (0, 1):
            rr = r ^ 0xFF if inv else r
            e = _bits(ref ^ rr).sum(axis=1)                  # per test packet
            k = int(np.argmin(e))
            if best is None or e[k] < best[0]:
                best = (int(e[k]), k, sw, inv, rr)
    errors, seq, sw, inv, rr = best
    want = ref[seq]
    per_byte = _bits((want ^ rr)[:, None]).sum(axis=1)
    wb, gb = _bits(want), _bits(rr)
    wrong = np.flatnonzero(wb != gb)
    q = len(gb) * 3 // 4                                     # the last quarter, by shift
    shifts = {}
    for s in range(-3, 4):
        lo, hi = max(q, q + s), min(len(gb), len(gb) + s)
        shifts[s] = int((gb[lo:hi] != wb[lo - s:hi - s]).sum()) if hi > lo else len(gb)
    tail_shift = min(shifts, key=shifts.get)
    return {"errors": errors, "bits": 8 * n, "seq": seq, "swap": int(rswap) ^ sw, "inv": inv,
            "per_byte": per_byte.tolist(), "first": int(wrong[0]) if len(wrong) else -1,
            "blocks": [int(per_byte[i * n // 8:(i + 1) * n // 8].sum()) for i in range(8)],
            "tail_shift": tail_shift if shifts[tail_shift] < shifts[0] // 2 else 0,
            "tail_errors": shifts}


IRQ_NAMES = ((0x0002, "S"), (0x1000, "F"), (0x2000, "D"))     # sync, FIFO almost full, RX done


def events(ev):
    """The firmware's ev field ('flags:ms:words,...') -> 'S@0 F@27+4 D@108+28': which
    interrupts came when in the packet (ms after the first), and how many FIFO words each read.
    With timed FIFO reads (key 8), the words read on the timer count toward the next event, and
    a last pseudo event T marks the packet completed on the timer."""
    out = []
    for item in str(ev).split(","):
        try:
            f, ms, n = item.split(":")
            f, ms, n = int(f, 16), int(ms), int(n)
        except ValueError:
            continue
        name = "".join(c for bit, c in IRQ_NAMES if f & bit) or ("T" if f == 0 else f"{f:x}")  # T: timed reads
        out.append(f"{name}@{ms}" + (f"+{n}" if n else ""))
    return " ".join(out)


def error_map(per_byte):
    """'.' for a right byte, else its count of wrong bits (1-8)."""
    return "".join("." if c == 0 else str(c) for c in per_byte)


def describe(d, a):
    """One console line for a K6 PKT report and its analysis."""
    res = d.get("res")
    crc = "short" if res == "short" else ("ok " if isinstance(res, int) and res >= 0 else "BAD")
    s = f"#{d.get('n', '?'):<4} sync {d.get('sync', '?')}  {crc}  got {d.get('got', '?')}"
    if a is None:
        return s + (f"  [{events(d['ev'])}]" if d.get("ev") else "") + timing(d)
    s += (f"  BE {a['errors']:3d}/{a['bits']}  s{'+' if a['swap'] else '-'}x{'+' if a['inv'] else '-'}"
          f"  pkt {a['seq']:2d}  {error_map(a['per_byte'])}")
    if a["tail_shift"]:
        s += f"  slip {a['tail_shift']:+d}"
    fw = d.get("be")
    if fw is not None and fw != a["errors"]:
        s += f"  (radio said {fw})"
    if d.get("ev"):
        s += f"  [{events(d['ev'])}]"
    return s + timing(d)


def timing(d):
    """gap= (ms from the radio's last re-arm to this packet: about 90 back to back on the
    default 40 ms re-arm, about 560 more per packet missed), stray= (when the last timed
    packet's own RX-done came, ms after its last word; -1 none) and rearm= (the re-arm delay
    before this packet, ms after the last one's end; key 8)."""
    t = ""
    if isinstance(d.get("gap"), int):
        t += f"  gap {d['gap']}"
    if isinstance(d.get("stray"), int) and d["stray"] >= 0:
        t += f"  stray {d['stray']}"
    if isinstance(d.get("rearm"), int) and d["rearm"] >= 0:
        t += f"  rearm {d['rearm']}"
    return t


class Stats:
    """Packets seen, CRC passes and bit errors over a run."""

    def __init__(self):
        self.pk, self.ok, self.short, self.be, self.blocks, self.slips = 0, 0, 0, [], [], {}
        self.sync = {}
        self.lost_lines = 0
        self._last_n = None

    def add(self, d, a):
        self.pk += 1
        n = d.get("n")
        if isinstance(n, int) and self._last_n is not None and n > self._last_n + 1:
            self.lost_lines += n - self._last_n - 1
        if isinstance(n, int):
            self._last_n = n
        self.sync[d.get("sync", "?")] = self.sync.get(d.get("sync", "?"), 0) + 1
        res = d.get("res")
        if res == "short":
            self.short += 1
        elif isinstance(res, int) and res >= 0:
            self.ok += 1
        if a is not None:
            self.be.append(a["errors"])
            self.blocks.append(a["blocks"])
            self.slips[a["tail_shift"]] = self.slips.get(a["tail_shift"], 0) + 1
        elif isinstance(d.get("be"), int) and d["be"] >= 0 and len(d.get("blk") or []) == 8:
            self.be.append(d["be"])
            self.blocks.append(d["blk"])

    def summary(self):
        if not self.pk:
            return "no packets"
        s = f"{self.pk} packets, {self.ok} CRC ok, {self.short} short"
        if self.be:
            be = np.array(self.be)
            blk = np.mean(self.blocks, axis=0)
            s += (f"; bit errors mean {be.mean():.1f}, median {np.median(be):.0f}, "
                  f"{int((be == 0).sum())} clean; per eighth " + " ".join(f"{x:.1f}" for x in blk))
            slips = {k: v for k, v in self.slips.items() if k}
            if slips:
                s += "; slips " + ", ".join(f"{k:+d}: {v}" for k, v in sorted(slips.items()))
        s += "; sync " + " ".join(f"{k}:{v}" for k, v in sorted(self.sync.items()))
        if self.lost_lines:
            s += f"; {self.lost_lines} report lines lost"
        return s

    def row(self):
        be = np.array(self.be) if self.be else np.array([np.nan])
        return {"packets": self.pk, "ok": self.ok, "short": self.short,
                "be_mean": float(np.mean(be)), "be_median": float(np.median(be)),
                "clean": int((be == 0).sum()) if self.be else 0,
                "blocks": np.mean(self.blocks, axis=0).round(1).tolist() if self.blocks else []}


# ---- line sources ----

class SerialLines:
    """Lines from the K6's port in a background thread, each with its arrival time. Read only."""

    def __init__(self, port, baud=115200):
        import serial                       # pyserial
        self.ser = serial.Serial(port, baud, timeout=0.2)
        self.q = queue.Queue()
        self._stop = False
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def _run(self):
        buf = b""
        while not self._stop:
            b = self.ser.read(256)
            if not b:
                continue
            buf += b
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                self.q.put((time.time(), line.decode("ascii", "replace").strip()))

    def get(self, timeout=None):
        """(time, line), or None after timeout."""
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain(self):
        out = []
        while True:
            x = self.get(0)
            if x is None:
                return out
            out.append(x)

    def close(self):
        self._stop = True
        self.t.join(1)
        self.ser.close()


def file_lines(path):
    """Lines of a saved log ('time<TAB>line' as --log writes, or bare lines)."""
    for ln in open(path, encoding="ascii", errors="replace"):
        ln = ln.rstrip("\r\n")
        parts = ln.split("\t", 2)          # time, line; or time, build, line
        if len(parts) > 1:
            try:
                yield float(parts[0]), parts[-1]
                continue
            except ValueError:
                pass
        yield 0.0, ln


# ---- listen ----

def build_of(d):
    """The firmware build a BOOT or CFG line names (sotto commit + build time), else None."""
    if d and d["kind"] in ("BOOT", "CFG") and "build" in d:
        return d["build"]
    return None


def handle(t, line, stats, out, log=None, cfg=None, seen=None):
    """One line: log it, print it, count it. Returns the parsed dict (or None). `seen` (a dict)
    keeps the last firmware build reported; every log line carries it ('?' before the first)."""
    d = parse_line(line)
    b = build_of(d)
    if seen is not None and b and seen.get("build") != b:
        seen["build"] = b
        out(f"K6 firmware build {b}")
    if log:
        log.write(f"{t:.3f}\t{(seen or {}).get('build', '?')}\t{line}\n")
        log.flush()
    if d is None:
        if line:
            out(f"   {line}")
        return None
    if d["kind"] == "PKT":
        # packets that pass CRC come without raw= since 2026-11-04 (a shorter line between
        # back-to-back packets); their bit errors are the firmware's be=/blk=
        a = analyse(d.get("raw", b""), d.get("rswap", 1)) if d.get("res") != "short" else None
        stats.add(d, a)
        out(describe(d, a))
    else:
        if d["kind"] == "CFG" and cfg is not None:
            cfg.clear()
            cfg.update(d)
        out(line)
    return d


def listen(src, log_path=None, out=print):
    stats = Stats()
    seen = {}
    log = open(log_path, "a") if log_path else None
    try:
        if isinstance(src, SerialLines):
            out("listening (Ctrl-C to stop) ...")
            while True:
                x = src.get(0.5)
                if x is not None:
                    handle(*x, stats, out, log, seen=seen)
        else:
            for t, line in src:
                handle(t, line, stats, out, log, seen=seen)
    except KeyboardInterrupt:
        pass
    finally:
        if log:
            log.close()
    out(stats.summary())
    if not seen:
        out("no firmware build reported (a K6 BOOT or CFG line with build=; older images have none)")
    return stats


# ---- sweep ----

def speed_shift(x, rate, speed_pct):
    """Play x faster by speed_pct percent (baud and tones both), as a sound-card clock error would."""
    if not speed_pct:
        return x
    return fsk.resample(x, int(round(rate * (1 + speed_pct / 100.0))), rate)


def check_cfg(cfg, a, out, strict=False):
    if not cfg:
        out("note: no K6 CFG line seen yet; press a key on the K6 (e.g. 4 twice) to make it report")
        return
    want = {"phy": a.phy, "frames": a.payload, "baud": a.baud}
    bad = {k: (cfg.get(k), v) for k, v in want.items() if str(cfg.get(k)) != str(v)}
    if bad:
        msg = "the K6 is set differently: " + ", ".join(f"{k} K6 {have} vs sweep {v}" for k, (have, v) in bad.items())
        if strict:
            raise WrongBuild(msg + ": not transmitting (--anyway sends regardless)")
        out("warning: " + msg)


class WrongBuild(Exception):
    pass


def wait_build(src, seen, want, cfg, out, log=None, timeout=60.0):
    """Hold off transmitting until the K6 reports a build starting with `want`."""
    def ok():
        b = seen.get("build")
        if b is not None and not b.startswith(want):
            raise WrongBuild(f"the K6 runs build {b}, not {want}: not transmitting")
        return b is not None
    if ok():
        return
    out(f"waiting for the K6 to report build {want} (press a key on it, e.g. 4 twice) ...")
    t_end = time.time() + timeout
    while time.time() < t_end:
        y = src.get(max(0.0, min(0.5, t_end - time.time())))
        if y is not None:
            handle(*y, Stats(), out, log, cfg, seen)
            if ok():
                return
    raise WrongBuild(f"no build report from the K6 in {timeout:.0f} s (old image?): not transmitting")


def sweep(a, src, send, out=print, log=None):
    """For each (speed, level): send the test packets, collect the K6's reports, score them."""
    from . import k6
    speeds = [float(s) for s in a.speeds.split(",")]
    levels = [float(s) for s in a.levels.split(",")] if a.levels else [a.level]
    cfg = {}
    seen = {}
    want = getattr(a, "expect_build", None)
    rows = []
    for t, line in src.drain():
        handle(t, line, Stats(), out, log, cfg, seen)
    if want:
        wait_build(src, seen, want, cfg, out, log, getattr(a, "build_wait", 60.0))
    check_cfg(cfg, a, out, strict=bool(cfg) and not getattr(a, "anyway", True))
    for lev in levels:
        for sp in speeds:
            b = copy.copy(a)
            b.level, b.message = lev, a.message
            x = k6.build_audio(k6._test_payloads(b), b, b.rate)
            x = speed_shift(x, b.rate, sp)
            out(f"--- speed {sp:+.2f} %  level {lev:.2f}: {len(x) / b.rate:.1f} s on air")
            stats = Stats()
            for y in src.drain():
                handle(*y, Stats(), out, log, cfg, seen)
            if want:
                wait_build(src, seen, want, cfg, out, log, 0.0)   # a reboot to another image stops it
            send(x, b, out)
            t_end = time.time() + a.settle
            while time.time() < t_end:
                y = src.get(max(0.0, t_end - time.time()))
                if y is not None:
                    handle(*y, stats, out, log, cfg, seen)
            for y in src.drain():
                handle(*y, stats, out, log, cfg, seen)
            sent = a.repeat if not a.message else len(test_packets(a.payload))
            out(f"    sent {sent}: {stats.summary()}")
            rows.append({"speed_pct": sp, "level": lev, "sent": sent, **stats.row()})
            time.sleep(a.gap)
    out("")
    out(f"{'speed %':>8} {'level':>6} {'sent':>5} {'seen':>5} {'CRC ok':>7} {'BE mean':>8} {'clean':>6}  per eighth")
    for r in rows:
        out(f"{r['speed_pct']:+8.2f} {r['level']:6.2f} {r['sent']:5d} {r['packets']:5d} {r['ok']:7d} "
            f"{r['be_mean']:8.1f} {r['clean']:6d}  {' '.join(str(v) for v in r['blocks'])}")
    return rows


def main(argv=None):
    from . import civ, k6
    ap = argparse.ArgumentParser(prog="python -m modem.k6_harness", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    li = sub.add_parser("listen", help="print and score the K6's reports")
    src = li.add_mutually_exclusive_group(required=True)
    src.add_argument("--k6", metavar="PORT", help="the K6's programming cable, e.g. /dev/ttyUSB2")
    src.add_argument("--file", help="a saved log instead")
    li.add_argument("--log", help="append every line, with its time, to this file")

    sw = sub.add_parser("sweep", help="send test packets per setting via the IC-7100; collect the K6's reports")
    sw.add_argument("--k6", metavar="PORT", required=True, help="the K6's programming cable, e.g. /dev/ttyUSB2")
    sw.add_argument("--log", help="append every K6 line, with its time, to this file")
    k6._tx_opts(sw)
    sw.add_argument("--speeds", default="0", help="comma list of speed offsets in percent, e.g. -1,-0.5,0,0.5,1")
    sw.add_argument("--levels", help="comma list of audio levels (default: --level)")
    sw.add_argument("--repeat", type=int, default=10, help="test packets per setting (default 10)")
    sw.add_argument("--message", action="store_true", help="send the whole 70-frame test message instead")
    sw.add_argument("--settle", type=float, default=2.0, help="wait after each send for reports, s")
    sw.add_argument("--gap", type=float, default=2.0, help="pause between sends, s")
    sw.add_argument("--port", help="the IC-7100's CI-V / PTT port, e.g. /dev/ttyUSB0")
    sw.add_argument("--ptt", choices=("civ", "rts", "dtr", "none"), default="civ")
    sw.add_argument("--civ-baud", type=int, default=19200)
    sw.add_argument("--civ-addr", type=lambda v: int(v, 0), default=civ.IC7100)
    sw.add_argument("--set-mode", action="store_true")
    sw.add_argument("--device")
    sw.add_argument("--player", help="e.g. paplay, or 'aplay -D plughw:CARD=CODEC'")
    sw.add_argument("--max-tx", type=float, default=60.0)
    sw.add_argument("--dry-run", action="store_true", help="don't key or play; still listens")
    sw.add_argument("--expect-build", metavar="SHA",
                    help="transmit only once the K6 reports a build starting with this (its sotto commit)")
    sw.add_argument("--build-wait", type=float, default=60.0, help="how long to wait for that report, s")
    sw.add_argument("--anyway", action="store_true",
                    help="transmit even when the K6's CFG line shows other PHY/frames/baud settings")
    a = ap.parse_args(argv)
    if a.cmd == "sweep" and a.k6 == a.port:
        ap.error("--k6 (the K6 cable) and --port (the IC-7100) must be different ports")

    if a.cmd == "listen":
        s = SerialLines(a.k6) if a.k6 else file_lines(a.file)
        try:
            listen(s, a.log)
        finally:
            if a.k6:
                s.close()
        return 0

    s = SerialLines(a.k6)
    log = open(a.log, "a") if a.log else None
    try:
        sweep(a, s, lambda x, b, out: k6.send(x, b, out), log=log)
    except KeyboardInterrupt:
        pass
    except WrongBuild as e:
        print(f"stopped: {e}")
        return 1
    finally:
        s.close()
        if log:
            log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
