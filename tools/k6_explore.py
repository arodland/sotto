"""The K6 explorer's candidate list (c/k6/explore.c) on this machine: feed it logs from the scan
module (tools/k6_scan.py) and the CW ID listener and see which frequencies it calls repeaters.

  python3 tools/k6_explore.py replay LOG...           the ranked list from serial logs (H and X
                                                       lines from the scan, CWID ok from cwlisten)
  python3 tools/k6_explore.py replay LOG --all        every candidate, not only the first 20
  python3 tools/k6_explore.py plan                    the band plan rows it grades against

It runs the same C as the radio will (built here with cc, no radio needed), so a replayed log is
a test of the scoring. The scan's telemetry keeps only the newest lines, so give it serial logs
(k6_harness listen --log): the explorer needs every H line. H lines from the first run (n3, no
start time) still work, timed by the log's own clock if it has one.
SPDX-License-Identifier: MIT
"""
import argparse
import ctypes
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
K6 = os.path.join(ROOT, "c", "k6")
sys.path.insert(0, os.path.join(ROOT, "tools"))
import k6_scan  # noqa: E402

F_IGNORED, F_TRIED, F_OFF_HEARD, F_SIMPLEX, F_SPUR = 1, 2, 4, 8, 16


class Cand(ctypes.Structure):
    _fields_ = [("hz", ctypes.c_uint32), ("first_ms", ctypes.c_uint32), ("last_ms", ctypes.c_uint32),
                ("on_ms", ctypes.c_uint32), ("off_hz", ctypes.c_int32), ("overs", ctypes.c_uint16),
                ("tone", ctypes.c_uint16), ("lo", ctypes.c_int16), ("hi", ctypes.c_int16),
                ("prev_start", ctypes.c_uint32), ("ids", ctypes.c_uint8), ("skirts", ctypes.c_uint8),
                ("flags", ctypes.c_uint8),
                ("score", ctypes.c_int8), ("grade", ctypes.c_char), ("call", ctypes.c_char * 10)]


class Ex(ctypes.Structure):
    _fields_ = [("c", ctypes.POINTER(Cand)), ("n", ctypes.c_uint16), ("cap", ctypes.c_uint16),
                ("tol_hz", ctypes.c_uint32)]


class Plan(ctypes.Structure):
    _fields_ = [("lo_hz", ctypes.c_uint32), ("hi_hz", ctypes.c_uint32), ("off_hz", ctypes.c_int32),
                ("simplex", ctypes.c_uint8)]


class Rec(ctypes.Structure):
    """rt_rec_t (c/k6/rt.h), 64 bytes"""
    _fields_ = [("state", ctypes.c_uint16), ("seq", ctypes.c_uint16), ("rx_hz", ctypes.c_uint32),
                ("tx_off", ctypes.c_int32), ("rx_tone", ctypes.c_uint16), ("tx_tone", ctypes.c_uint16),
                ("flags", ctypes.c_uint16), ("best_rssi", ctypes.c_uint8), ("score", ctypes.c_uint8),
                ("last_heard", ctypes.c_uint32), ("name", ctypes.c_char * 16), ("call", ctypes.c_char * 10),
                ("call_flags", ctypes.c_uint8), ("cw", ctypes.c_uint8), ("group", ctypes.c_uint8),
                ("spare0", ctypes.c_uint8), ("id", ctypes.c_uint16), ("wseq", ctypes.c_uint32),
                ("crc", ctypes.c_uint32)]


def load(so=None):
    """explore.c built as a shared library (cc), with its calls typed"""
    so = so or os.path.join(tempfile.mkdtemp(prefix="k6ex"), "libexplore.so")
    subprocess.run(["cc", "-O1", "-g", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC", "-o", so,
                    os.path.join(K6, "explore.c")], check=True)
    L = ctypes.CDLL(so)
    P = ctypes.POINTER(Ex)
    L.ex_init.argtypes = [P, ctypes.POINTER(Cand), ctypes.c_uint]
    L.ex_over.argtypes = [P, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int]
    L.ex_over.restype = ctypes.POINTER(Cand)
    L.ex_spur.argtypes = [P, ctypes.c_uint32]
    L.ex_input.argtypes = [P, ctypes.c_uint32, ctypes.c_int32]
    L.ex_tone.argtypes = [P, ctypes.c_uint32, ctypes.c_uint16]
    L.ex_cw.argtypes = [P, ctypes.c_uint32, ctypes.c_char_p]
    L.ex_find.argtypes = [P, ctypes.c_uint32]
    L.ex_find.restype = ctypes.POINTER(Cand)
    L.ex_rank.argtypes = [P, ctypes.c_uint]
    L.ex_rank.restype = ctypes.POINTER(Cand)
    L.ex_to_rt.argtypes = [ctypes.POINTER(Cand), ctypes.POINTER(Rec)]
    return L


class Explorer:
    """one candidate list (cap entries) over the C library"""

    def __init__(self, L, cap=64):
        self.L, self.store, self.e = L, (Cand * cap)(), Ex()
        L.ex_init(ctypes.byref(self.e), self.store, cap)

    def over(self, hz, t, dur, lvl):
        p = self.L.ex_over(ctypes.byref(self.e), hz, t, dur, lvl)
        return p.contents if p else None

    def find(self, hz):
        p = self.L.ex_find(ctypes.byref(self.e), hz)
        return p.contents if p else None

    def ranked(self):
        out, i = [], 0
        while True:
            p = self.L.ex_rank(ctypes.byref(self.e), i)
            if not p:
                return out
            out.append(p.contents)
            i += 1

    def record(self, c):
        r = Rec()
        self.L.ex_to_rt(ctypes.byref(c), ctypes.byref(r))
        return r

    def __getattr__(self, name):
        f = getattr(self.L, "ex_" + name)
        return lambda *a: f(ctypes.byref(self.e), *a)


def plan_rows(L):
    n = ctypes.c_uint.in_dll(L, "ex_plan_us_n").value
    rows = (Plan * n).in_dll(L, "ex_plan_us")
    return [(r.lo_hz, r.hi_hz, r.off_hz, bool(r.simplex)) for r in rows]


def _clock_ms(t):
    """a log time (seconds, or HH:MM:SS[.f]) in ms, or None"""
    if not t:
        return None
    if ":" in t:
        h, m, s = t.split(":")
        return round((int(h) * 3600 + int(m) * 60 + float(s)) * 1000)
    return round(float(t) * 1000)


CWID = re.compile(r"CWID ok (\S+) fl=\d+ rx=(\d+)")


def replay(ex, text):
    """feed one log's events to ex, in log order; -> (overs, spurs, calls) counted"""
    res = k6_scan.decode(text)
    counts = [0, 0, 0]
    for o in res["overs"]:
        if "at" in o:
            start = o["at"]
        else:
            end = _clock_ms(o["t"])
            start = end - o["ms"] if end is not None else 0
        ex.over(o["f"], start & 0xFFFFFFFF, o["ms"], o["lvl"])
        counts[0] += 1
    for s in res["spurs"]:
        ex.spur(s["f"])
        counts[1] += 1
    for _, ln in k6_scan.lines_of(text):
        if m := CWID.search(ln):
            ex.cw(int(m.group(2)), m.group(1).encode()[:10])
            counts[2] += 1
    return counts


def offset_text(off):
    return "simplex" if off == 0 else f"{off / 1e6:+.1f}".rstrip("0").rstrip(".") + " MHz"


def row_text(c):
    tone = f" T{c.tone / 10:.1f}" if c.tone else ""
    call = " " + c.call.decode(errors="replace") if c.call else ""
    off = "" if c.flags & F_SIMPLEX else " " + offset_text(c.off_hz)
    heard = " (input heard)" if c.flags & F_OFF_HEARD else ""
    head = f"{c.grade.decode()} {c.hz / 1e6:9.4f}{off}{tone}{call}{heard}"
    if c.flags & F_SPUR:
        return head + "  spur"
    if c.overs and c.skirts >= c.overs:
        return head + f"  skirt of a louder neighbour ({c.overs} overs, {c.hi} dBm)"
    return (f"{head}  score {c.score}, {c.overs} overs, {c.on_ms / 1000:.0f} s up, {c.hi} dBm "
            f"(spread {c.hi - c.lo} dB), ids {c.ids}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("replay", help="the ranked candidate list from logs")
    r.add_argument("logs", nargs="+")
    r.add_argument("--cap", type=int, default=64, help="candidates kept (the core plans 64)")
    r.add_argument("--all", action="store_true")
    sub.add_parser("plan", help="the band plan rows")
    a = ap.parse_args(argv)
    L = load()
    if a.cmd == "plan":
        for lo, hi, off, simplex in plan_rows(L):
            print(f"{lo / 1e6:8.3f}-{hi / 1e6:8.3f}  {'simplex' if simplex else offset_text(off)}")
        return 0
    ex = Explorer(L, a.cap)
    for path in a.logs:
        with open(path, errors="replace") as fh:
            n = replay(ex, fh.read())
        print(f"{path}: {n[0]} overs, {n[1]} spurs, {n[2]} CW IDs")
    rows = ex.ranked()
    for c in rows if a.all else rows[:20]:
        print(row_text(c))
    if not rows:
        print("no candidates")
    return 0


if __name__ == "__main__":
    sys.exit(main())
