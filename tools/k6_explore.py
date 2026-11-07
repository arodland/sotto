"""The K6 explorer's candidate list (c/k6/explore.c) on this machine: feed it logs from the scan
module (tools/k6_scan.py) and the CW ID listener and see which frequencies it calls repeaters.

  python3 tools/k6_explore.py replay LOG...           the ranked list from serial logs (H and X
                                                       lines from the scan, CWID ok from cwlisten)
  python3 tools/k6_explore.py replay LOG --all        every candidate, not only the first 20
  python3 tools/k6_explore.py plan                    the band plan rows it grades against
  python3 tools/k6_explore.py score LOG --truth CSV   Phase 4: the EXPLORE app's list (its K6 CAND
                                                       lines, key 7) against a repeater list: how
                                                       many listed outputs it found, how many of
                                                       its R entries are real

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

F_IGNORED, F_TRIED, F_OFF_HEARD, F_SIMPLEX, F_SPUR, F_KNOWN = 1, 2, 4, 8, 16, 32


class Cand(ctypes.Structure):
    _fields_ = [("hz", ctypes.c_uint32), ("first_ms", ctypes.c_uint32), ("last_ms", ctypes.c_uint32),
                ("on_ms", ctypes.c_uint32), ("off_hz", ctypes.c_int32), ("overs", ctypes.c_uint16),
                ("tone", ctypes.c_uint16), ("lo", ctypes.c_int16), ("hi", ctypes.c_int16),
                ("prev_start", ctypes.c_uint32), ("ids", ctypes.c_uint8), ("skirts", ctypes.c_uint8),
                ("flags", ctypes.c_uint8),
                ("score", ctypes.c_int8), ("grade", ctypes.c_char), ("call", ctypes.c_char * 10),
                ("listens", ctypes.c_uint8)]


class Ex(ctypes.Structure):
    _fields_ = [("c", ctypes.POINTER(Cand)), ("n", ctypes.c_uint16), ("cap", ctypes.c_uint16),
                ("tol_hz", ctypes.c_uint32), ("known", ctypes.POINTER(ctypes.c_uint32)),
                ("known_n", ctypes.c_uint16), ("known_cap", ctypes.c_uint16)]


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
    L.ex_spur.argtypes = [P, ctypes.c_uint32, ctypes.c_int]
    L.ex_input.argtypes = [P, ctypes.c_uint32, ctypes.c_int32]
    L.ex_tone.argtypes = [P, ctypes.c_uint32, ctypes.c_uint16]
    L.ex_cw.argtypes = [P, ctypes.c_uint32, ctypes.c_char_p]
    L.ex_known_store.argtypes = [P, ctypes.POINTER(ctypes.c_uint32), ctypes.c_uint]
    L.ex_know.argtypes = [P, ctypes.c_uint32]
    L.ex_find.argtypes = [P, ctypes.c_uint32]
    L.ex_find.restype = ctypes.POINTER(Cand)
    L.ex_rank.argtypes = [P, ctypes.c_uint]
    L.ex_rank.restype = ctypes.POINTER(Cand)
    L.ex_to_rt.argtypes = [ctypes.POINTER(Cand), ctypes.POINTER(Rec)]
    L.ex_pick_id.argtypes = [P, ctypes.POINTER(ctypes.c_uint32), ctypes.c_uint]
    L.ex_pick_id.restype = ctypes.POINTER(Cand)
    return L


class Explorer:
    """one candidate list (cap entries) over the C library"""

    def __init__(self, L, cap=64):
        self.L, self.store, self.e = L, (Cand * cap)(), Ex()
        L.ex_init(ctypes.byref(self.e), self.store, cap)
        self.known = (ctypes.c_uint32 * 16)()
        L.ex_known_store(ctypes.byref(self.e), self.known, 16)

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
        ex.spur(s["f"], s["lvl"])
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


CAND = re.compile(r"K6 CAND \d+ (\S) (\d+) off=(-?\d+) tone=(\d+) call=(\S*) score=(-?\d+) overs=(\d+)")


def cands_of(text):
    """the EXPLORE app's K6 CAND lines (the last dump in the log wins): [dict]"""
    out = []
    for ln in text.splitlines():
        if re.search(r"K6 CAND band=", ln):
            out = []
        elif m := CAND.search(ln):
            g, hz, off, tone, call, score, overs = m.groups()
            out.append({"grade": g, "hz": int(hz), "off": int(off), "tone": int(tone), "call": call,
                        "score": int(score), "overs": int(overs)})
    return out


def truth_of(text):
    """a repeater list: CSV or plain lines whose first number in 18-1000 is the output in MHz (a
    RepeaterBook export works: its Frequency / Output column comes first in a row)"""
    out = []
    for ln in text.splitlines():
        for tok in re.split(r"[,;\t ]+", ln):
            try:
                f = float(tok.strip('"'))
            except ValueError:
                continue
            if 18 <= f <= 1000:
                out.append(round(f * 1e6))
                break
    return sorted(set(out))


def score(cands, truth, tol_hz=5000, lo=0, hi=10**10):
    """recall over the truth outputs inside [lo, hi] (found as R or r), precision of the R entries"""
    truth = [f for f in truth if lo <= f <= hi]
    rep = [c for c in cands if c["grade"] in "Rr"]
    near = lambda f, cs: [c for c in cs if abs(c["hz"] - f) <= tol_hz]  # noqa: E731
    found = [f for f in truth if near(f, rep)]
    big_r = [c for c in cands if c["grade"] == "R"]
    real = [c for c in big_r if any(abs(c["hz"] - f) <= tol_hz for f in truth)]
    return {"truth": len(truth), "found": len(found), "missed": [f for f in truth if f not in found],
            "heard_any": [f for f in truth if near(f, cands)], "R": len(big_r), "R_real": len(real),
            "R_false": [c["hz"] for c in big_r if c not in real]}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("replay", help="the ranked candidate list from logs")
    r.add_argument("logs", nargs="+")
    r.add_argument("--cap", type=int, default=64, help="candidates kept (the core plans 64)")
    r.add_argument("--all", action="store_true")
    sub.add_parser("plan", help="the band plan rows")
    sc = sub.add_parser("score", help="Phase 4: the app's list against a repeater list")
    sc.add_argument("log")
    sc.add_argument("--truth", required=True, help="repeater list (CSV, output MHz first)")
    sc.add_argument("--tol-khz", type=float, default=5)
    sc.add_argument("--band", nargs=2, type=float, metavar=("LO", "HI"), help="only outputs in LO-HI MHz")
    a = ap.parse_args(argv)
    if a.cmd == "score":
        with open(a.log, errors="replace") as fh:
            cands = cands_of(fh.read())
        with open(a.truth, errors="replace") as fh:
            truth = truth_of(fh.read())
        lo, hi = (round(a.band[0] * 1e6), round(a.band[1] * 1e6)) if a.band else (0, 10**10)
        r = score(cands, truth, round(a.tol_khz * 1000), lo, hi)
        print(f"{len(cands)} candidates; {r['truth']} listed outputs")
        print(f"recall: {r['found']} of {r['truth']} listed outputs graded R or r "
              f"({len(r['heard_any'])} heard at all)")
        print(f"precision: {r['R_real']} of {r['R']} R entries are listed repeaters")
        for f in r["missed"]:
            print(f"  missed {f / 1e6:.4f}" + ("  (heard, graded lower)" if f in r["heard_any"] else ""))
        for f in r["R_false"]:
            print(f"  R not on the list {f / 1e6:.4f}")
        return 0
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
