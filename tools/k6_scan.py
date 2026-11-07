"""The K6 signal explorer's fast two-stage sweep (Phase 1): set up, send and read the sweep module
(c/k6/mod/scan.c, docs/k6-scan.md).

  python3 tools/k6_scan.py plan                       segments, bins, fit in the module's RAM, times
  python3 tools/k6_scan.py run 2m --bin mod-scan.bin --key-file KEY --seconds 120
                                                       the K6 sweeps 2 m for 2 minutes, receive only,
                                                       then reports on 440.850 (k6_upload.py's ARQ)
  python3 tools/k6_scan.py run 2m 125 --seconds 300   several segments, swept in turn
  python3 tools/k6_scan.py run 144.0-148.0/10 ...     any range: LO-HI[/STEP_kHz] in MHz
  python3 tools/k6_scan.py decode LOG... [--svg wf.svg] [--csv hits.csv]
                                                       the waterfall and the hits from a serial log
                                                       (k6_harness listen --log) or the telemetry
  python3 tools/k6_scan.py watch LOG                  follow a growing serial log: the waterfall in
                                                       the terminal, a line a sweep
  python3 tools/k6_scan.py patch IN.bin OUT.bin SEG...    a module set up for those segments

The K6 only listens. The upload (IC-7100 on 440.850, F/1800, magic 0xAE) carries the segments in
the module's parameter block; the K6 sweeps them until --seconds, then answers on 440.850 with
the summary (T and Z lines) as telemetry. The waterfall rows (R lines) only reach the serial
port: run `python -m modem.k6_harness listen --k6 PORT --log FILE` by hand to keep them, and
`watch FILE` to see them live. This tool never opens a serial port itself.

hamlet has no clang: give --bin the CI-built mod-scan.bin (K6 pager firmware artifact).
SPDX-License-Identifier: MIT
"""
import argparse
import csv
import os
import re
import struct
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "c", "k6", "mod", "scan.c")
MAGIC = b"SWEEP v1"
FMT = "<8sBBBBBB2xHHHHH4H4HH4I4I"     # par_t in scan.c, 76 bytes
NSEG = 4
STORE = 4072                         # the module store: code + scratch
SW_T, HOT_T, HOT = 180, 28, 12       # sw_t, hot_t and HOT in scan.c
FLMAX = 1024                         # bins in all: their floors live on the module's stack
TLM_BYTES = 12 * 57
PAGER_HZ = 440_850_000
TUNE_US = 2300                       # a tune and a read, as rxmap's first run found (2026-11-07)

F_REFINE, F_ROWS = 2, 4

# name: (lo Hz, step Hz, bins, snap Hz, what)
SEGMENTS = {
    "2m": (144_000_000, 10_000, 400, 5_000, "2 m, 144-148 MHz at 10 kHz"),
    "2mr": (145_100_000, 10_000, 40, 5_000, "2 m repeater outputs 145.1-145.5"),
    "2mo": (146_600_000, 10_000, 80, 5_000, "2 m repeater outputs 146.6-147.4"),
    "125": (222_000_000, 10_000, 300, 20_000, "1.25 m, 222-225 MHz at 10 kHz"),
    "70a": (440_000_000, 12_500, 400, 12_500, "70 cm repeaters 440-445 MHz at 12.5 kHz"),
    "70b": (445_000_000, 12_500, 400, 12_500, "70 cm 445-450 MHz at 12.5 kHz"),
    "70w": (440_000_000, 25_000, 400, 12_500, "70 cm 440-450 MHz at 25 kHz (coarse)"),
    "70s": (430_000_000, 12_500, 400, 12_500, "70 cm 430-435 MHz"),
    "noaa": (162_400_000, 25_000, 8, 25_000, "NOAA weather 162.400-162.575 (always on: a spur test)"),
}


def code_size():
    """scan.c's built size, or the last build's (3308 B, 2026-11-07) without clang"""
    try:
        from modem import k6mod
        return len(k6mod.build(SRC)[0])
    except Exception:
        return 3308


def row_room(code=None):
    """scratch bytes left for the waterfall row after the module's code, state and hot list"""
    return STORE - (code if code is not None else code_size()) - SW_T - HOT * HOT_T


def fits(segs, cols=128, code=None):
    """the floors (a byte a bin, on the stack) and the row (in the scratch) fit"""
    most = max(n for _, _, n, _ in segs)
    row = min(cols, most) if cols else most
    return sum(n for _, _, n, _ in segs) <= FLMAX and row <= row_room(code)


def pack(segs, seconds=60, quick_us=1500, confirm_us=12000, hot_ms=100, spur_s=60, wide=1, noise_db=6,
         hit_db=10, cols=128, refine=True, rows=True):
    """segs: [(lo Hz, step Hz, bins, snap Hz)] -> the 76-byte parameter block"""
    segs = list(segs)
    if not 1 <= len(segs) <= NSEG:
        raise ValueError(f"1..{NSEG} segments")
    for lo, step, n, snap in segs:
        if not 8 <= n <= 4000 or not 0 < step <= 1_000_000 or not 0 <= snap < 65536:
            raise ValueError(f"segment {lo}: 8..4000 bins, a step up to 1 MHz, snap < 65.536 kHz")
    if not quick_us < confirm_us < 65536:
        raise ValueError("quick_us < confirm_us < 65536")
    pad = [(0, 0, 0, 0)] * (NSEG - len(segs))
    lo, step, n, snap = zip(*(segs + pad))
    flags = (F_REFINE if refine else 0) | (F_ROWS if rows else 0)
    return struct.pack(FMT, MAGIC, len(segs), wide, noise_db, hit_db, cols, flags, quick_us, confirm_us,
                       seconds, hot_ms, spur_s, *n, *snap, 0, *lo, *step)


def unpack(blk):
    v = struct.unpack(FMT, blk[:struct.calcsize(FMT)])
    keys = "magic nseg wide noise_db hit_db cols flags quick_us confirm_us max_s hot_ms spur_s".split()
    d = dict(zip(keys, v))
    k = len(keys)
    n, snap, lo, step = v[k:k + 4], v[k + 4:k + 8], v[k + 9:k + 13], v[k + 13:k + 17]
    d["segs"] = [(lo[i], step[i], n[i], snap[i]) for i in range(d["nseg"])]
    return d


def patch(code, blk):
    at = code.find(MAGIC)
    if at < 0 or code.find(MAGIC, at + 1) >= 0:
        raise ValueError("not the sweep module (no single SWEEP v1 block)")
    return code[:at] + blk + code[at + len(blk):]


def parse_seg(text):
    """a name from SEGMENTS or LO-HI[/STEP_kHz] in MHz -> (lo, step, bins, snap)"""
    if text in SEGMENTS:
        return SEGMENTS[text][:4]
    m = re.fullmatch(r"([\d.]+)-([\d.]+)(?:/([\d.]+))?", text)
    if not m:
        raise SystemExit(f"segment {text!r}: a name from `plan` or LO-HI[/STEP_kHz] in MHz")
    lo, hi = round(float(m.group(1)) * 1e6), round(float(m.group(2)) * 1e6)
    step = round(float(m.group(3) or 10) * 1e3)
    snap = 12_500 if lo >= 400e6 else 5_000
    return lo, step, max(8, round((hi - lo) / step)), snap


def estimate(n, quick_us=1500, tune_us=TUNE_US):
    """seconds a sweep of n bins takes, if few bins need confirming"""
    return n * (quick_us + tune_us) / 1e6


def mhz(hz):
    return f"{hz / 1e6:.3f}"


# ---- reading the results ----

def lines_of(text):
    """(time or None, line) from any log: `K6 MOD say` serial lines (with k6_harness's time prefix
    if any) or telemetry as k6_upload prints it"""
    out = []
    for ln in text.splitlines():
        m = re.search(r"K6 MOD say (.*)$", ln)
        t = re.match(r"\s*(\d+(?:\.\d+)?|\d\d:\d\d:\d\d(?:\.\d+)?)\s", ln)
        out.append((t.group(1) if t and m else None, (m.group(1) if m else ln).rstrip()))
    return out


def _hz(s):
    return round(float(s) * 1e6)


def decode(text):
    """-> {"floors": [dBm a segment], "sweeps": [{"seg", "n", "ms", "c", "h", "row": [dB...]}],
    "new": [...], "overs": [...], "spurs": [...], "summary": [...], "z": {...} or None}"""
    r = {"floors": [], "sweeps": [], "new": [], "overs": [], "spurs": [], "summary": [], "z": None}
    rows = {}
    for t, ln in lines_of(text):
        if m := re.fullmatch(r"F((?: -?\d+)+)", ln):
            r["floors"] = [int(x) for x in m.group(1).split()]
        elif m := re.fullmatch(r"R(\d) (\d+) ([0-o]*)", ln):
            g, at = int(m.group(1)), int(m.group(2))
            row = rows.setdefault(g, [])
            del row[at:]
            row += [ord(c) - 48 for c in m.group(3)]
        elif m := re.fullmatch(r"S(\d) (\d+) (\d+) c(\d+) h(\d+)", ln):
            g = int(m.group(1))
            r["sweeps"].append({"seg": g, "n": int(m.group(2)), "ms": int(m.group(3)), "c": int(m.group(4)),
                                "h": int(m.group(5)), "row": rows.pop(g, []), "t": t})
        elif m := re.fullmatch(r"N (\d+\.\d{3}) (-?\d+) fl(-?\d+) z(\d+) b(\d+)", ln):
            r["new"].append({"f": _hz(m.group(1)), "lvl": int(m.group(2)), "fl": int(m.group(3)),
                             "z": int(m.group(4)), "bin": int(m.group(5)), "t": t})
        elif m := re.fullmatch(r"H (\d+\.\d{3}) (\d+) (-?\d+) n(\d+)", ln):
            r["overs"].append({"f": _hz(m.group(1)), "ms": int(m.group(2)), "lvl": int(m.group(3)),
                               "n": int(m.group(4)), "t": t})
        elif m := re.fullmatch(r"X (\d+\.\d{3}) (-?\d+)", ln):
            r["spurs"].append({"f": _hz(m.group(1)), "lvl": int(m.group(2)), "t": t})
        elif m := re.fullmatch(r"T (\d+\.\d{3}) n(\d+) t(\d+) (-?\d+) d(\d+)( s)?", ln):
            r["summary"].append({"f": _hz(m.group(1)), "overs": int(m.group(2)), "s": int(m.group(3)),
                                 "lvl": int(m.group(4)), "spread": int(m.group(5)), "spur": bool(m.group(6))})
        elif m := re.fullmatch(r"Z sw=(\d+) bins=(\d+) ms=(\d+) bps=(\d+) hot=(\d+)", ln):
            r["z"] = dict(zip(("sweeps", "bins", "ms", "bps", "hot"), map(int, m.groups())))
    return r


def bin_cost_us(res, segs, quick_us=1500):
    """us a coarse bin costs beyond quick_us (tune, read, loop): from sweeps with no confirms"""
    xs = [s["ms"] * 1000 / segs[s["seg"]][2] - quick_us for s in res["sweeps"]
          if s["c"] == 0 and s["n"] > 0 and s["seg"] < len(segs)]
    return sorted(xs)[len(xs) // 2] if xs else None


def report(res, segs=None):
    out = []
    z = res["z"]
    if z:
        out.append(f"{z['sweeps']} sweeps, {z['bins']} bins in {z['ms'] / 1000:.1f} s: {z['bps']} bins/s, "
                   f"{z['hot']} candidates")
    if res["floors"]:
        out.append("settled floors: " + ", ".join(f"{v} dBm" for v in res["floors"]))
    if res["sweeps"]:
        ms = sorted(s["ms"] for s in res["sweeps"])
        out.append(f"sweep time: median {ms[len(ms) // 2]} ms, {min(ms)}-{max(ms)} ms over {len(ms)} sweeps")
        if segs:
            c = bin_cost_us(res, segs)
            if c is not None:
                out.append(f"a coarse bin costs {c:.0f} us beyond its quick read (tune + read + loop)")
    if res["summary"]:
        out.append("")
        out.append(f"{'MHz':>9}  {'overs':>5} {'s up':>5} {'dBm':>5} {'spread':>6}")
        for e in sorted(res["summary"], key=lambda e: (e["spur"], -e["overs"], e["f"])):
            out.append(f"{mhz(e['f']):>9}  {e['overs']:>5} {e['s']:>5} {e['lvl']:>5} {e['spread']:>6}"
                       + ("  spur" if e["spur"] else ""))
    elif res["overs"]:
        out.append("")
        for o in res["overs"]:
            out.append(f"  {mhz(o['f'])} up {o['ms']} ms at {o['lvl']} dBm (over {o['n']})")
    return "\n".join(out)


SHADE = " .:-=+*#%@"


def text_row(row, width=128):
    return "".join(SHADE[min(len(SHADE) - 1, v // 4)] for v in row[:width])


def svg(res, segs, path, seg=0, px=4):
    """the waterfall of one segment: newest sweep on top, one pixel row a sweep"""
    rows = [s["row"] for s in res["sweeps"] if s["seg"] == seg and s["row"]]
    if not rows:
        return False
    rows = rows[::-1]
    w = max(len(r) for r in rows)
    lx, ty = 50, 20
    el = [f'<rect width="{lx + w * px + 20}" height="{ty + len(rows) * px + 40}" fill="#fff"/>']
    for y, row in enumerate(rows):
        for x, v in enumerate(row):
            if v:
                g = max(0, 255 - v * 8)
                el.append(f'<rect x="{lx + x * px}" y="{ty + y * px}" width="{px}" height="{px}" '
                          f'fill="rgb({g},{g},255)"/>')
    if seg < len(segs):
        lo, step, n, _ = segs[seg]
        for k in range(0, 5):
            x = lx + k * w * px / 4
            f = lo + k * n * step / 4
            el.append(f'<text x="{x:.0f}" y="{ty + len(rows) * px + 16}" font-size="11" '
                      f'text-anchor="middle">{f / 1e6:.3f}</text>')
    el.append(f'<text x="{lx}" y="14" font-size="11">K6 sweep: dB over each bin\'s floor, newest on top</text>')
    with open(path, "w") as fh:
        fh.write(f'<svg xmlns="http://www.w3.org/2000/svg" width="{lx + w * px + 20}" '
                 f'height="{ty + len(rows) * px + 40}" font-family="sans-serif">{"".join(el)}</svg>\n')
    return True


def write_csv(res, path):
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["kind", "time", "MHz", "dBm", "ms_up", "overs", "floor", "z"])
        for e in res["new"]:
            w.writerow(["new", e["t"] or "", mhz(e["f"]), e["lvl"], "", "", e["fl"], e["z"]])
        for e in res["overs"]:
            w.writerow(["over", e["t"] or "", mhz(e["f"]), e["lvl"], e["ms"], e["n"], "", ""])
        for e in res["spurs"]:
            w.writerow(["spur", e["t"] or "", mhz(e["f"]), e["lvl"], "", "", "", ""])


# ---- on the bench ----

def code_of(a):
    if a.bin:
        return open(a.bin, "rb").read()
    from modem import k6mod
    return k6mod.build(SRC)[0]


def blk_of(a, code_len):
    segs = [parse_seg(s) for s in a.segments]
    blk = pack(segs, seconds=a.seconds, quick_us=a.quick_us, wide=0 if a.narrow else 1, cols=a.cols,
               refine=not a.no_refine)
    if not fits(segs, a.cols, code_len):
        sys.exit(f"too many bins: {FLMAX} in all, and the row at most {row_room(code_len)} columns")
    return segs, blk


def cmd_run(a):
    code = code_of(a)
    segs, blk = blk_of(a, len(code))
    for lo, step, n, _ in segs:
        print(f"segment {mhz(lo)}-{mhz(lo + n * step)} MHz, {n} bins of {step / 1e3:g} kHz, "
              f"about {estimate(n, a.quick_us):.1f} s a sweep")
    print(f"{time.strftime('%H:%M:%S')} the K6 sweeps for {a.seconds} s, receive only, then reports on 440.850",
          flush=True)
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "mod-scan.bin")
        open(path, "wb").write(patch(code, blk))
        log = os.path.join(a.out, f"scan-{time.strftime('%Y%m%d-%H%M%S')}.log")
        cmd = [sys.executable, os.path.join(ROOT, "tools", "k6_upload.py"), path, "--key-file", a.key_file,
               "--api-level", "2", "--telemetry", "12", "--run-s", f"{a.seconds + 20}", "--rigctld", a.rigctld]
        if a.dry_run:
            cmd.append("--dry-run")
        with open(log, "w") as fh:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=ROOT)
            for ln in proc.stdout:
                sys.stdout.write(ln)
                fh.write(ln)
            proc.wait()
    print(f"log: {log}")
    print(report(decode(open(log).read()), segs) or "no summary decoded (a serial log has every line)")


def watch_line(ln, pending, out=print):
    """one line of a growing log -> what the terminal shows; pending: R lines waiting for their S"""
    if re.match(r"R\d ", ln):
        pending.append(ln)
    elif m := re.match(r"S(\d) ", ln):
        sw = decode("\n".join([x for x in pending if x[1] == m.group(1)] + [ln]))["sweeps"]
        pending[:] = [x for x in pending if x[1] != m.group(1)]
        if sw:
            out(f"{sw[0]['seg']} |{text_row(sw[0]['row'])}| {sw[0]['ms']:>5} ms c{sw[0]['c']}")
    elif ln[:2] in ("N ", "H ", "X ", "F "):
        out({"N ": "  new  ", "H ": "  over ", "X ": "  spur ", "F ": "  floors "}[ln[:2]] + ln[2:])
    elif ln.startswith(("T ", "Z ")):
        out(ln)
        return ln.startswith("Z ")
    return False


def cmd_watch(a):
    """follow a serial log as k6_harness writes it: one terminal row a finished sweep"""
    pos, pending, part = 0, [], ""
    while True:
        try:
            with open(a.log, errors="replace") as fh:
                fh.seek(pos)
                chunk = fh.read()
                pos = fh.tell()
        except FileNotFoundError:
            chunk = ""
        part += chunk
        *done, part = part.split("\n")
        for _, ln in lines_of("\n".join(done)):
            if watch_line(ln, pending):
                return
        time.sleep(0.5)


def cmd_plan(a):
    c = code_size()
    print(f"module {c} B; floors for {FLMAX} bins in all (on the stack), rows up to {row_room(c)} columns")
    print(f"{'segment':<6} {'MHz':<19} {'bins':>4} {'step':>6} {'sweep s':>7} {'fits':>4}  what")
    for name, (lo, step, n, snap, what) in SEGMENTS.items():
        ok = fits([(lo, step, n, snap)], code=c)
        print(f"{name:<6} {mhz(lo):>8}-{mhz(lo + n * step):<10} {n:>4} {step / 1e3:>5g}k "
              f"{estimate(n):>7.2f} {'yes' if ok else 'NO':>4}  {what}")
    print(f"\nsweep s: bins x (1.5 ms quick read + {TUNE_US / 1000:g} ms a tune and read, rxmap's figure; "
          "the S lines measure it)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("segments", nargs="+")
    r.add_argument("--bin", help="CI's mod-scan.bin (else built from c/k6/mod/scan.c with clang)")
    r.add_argument("--key-file", required=True)
    r.add_argument("--seconds", type=int, default=120)
    r.add_argument("--quick-us", type=int, default=1500)
    r.add_argument("--cols", type=int, default=128)
    r.add_argument("--narrow", action="store_true")
    r.add_argument("--no-refine", action="store_true")
    r.add_argument("--rigctld", default="localhost:4532")
    r.add_argument("--out", default=".", help="where the log goes (on hamlet: a run folder in ~/sotto-work/runs)")
    r.add_argument("--dry-run", action="store_true")
    d = sub.add_parser("decode")
    d.add_argument("logs", nargs="+")
    d.add_argument("--segments", nargs="*", default=[], help="the run's segments (for the axis and bin cost)")
    d.add_argument("--svg")
    d.add_argument("--csv")
    w = sub.add_parser("watch")
    w.add_argument("log")
    sub.add_parser("plan")
    p = sub.add_parser("patch")
    p.add_argument("inp")
    p.add_argument("out")
    p.add_argument("segments", nargs="+")
    p.add_argument("--seconds", type=int, default=120)
    p.add_argument("--quick-us", type=int, default=1500)
    p.add_argument("--cols", type=int, default=128)
    p.add_argument("--narrow", action="store_true")
    p.add_argument("--no-refine", action="store_true")
    a = ap.parse_args()
    if a.cmd == "run":
        a.key_file = os.path.abspath(os.path.expanduser(a.key_file))
        a.out = os.path.abspath(os.path.expanduser(a.out))
        cmd_run(a)
    elif a.cmd == "plan":
        cmd_plan(a)
    elif a.cmd == "watch":
        cmd_watch(a)
    elif a.cmd == "patch":
        code = open(a.inp, "rb").read()
        _, blk = blk_of(a, len(code))
        open(a.out, "wb").write(patch(code, blk))
    else:
        text = "".join(open(p, errors="replace").read() for p in a.logs)
        res = decode(text)
        segs = [parse_seg(s) for s in a.segments]
        print(report(res, segs))
        if a.svg and svg(res, segs, a.svg):
            print(f"waterfall: {a.svg}")
        if a.csv:
            write_csv(res, a.csv)
            print(f"hits: {a.csv}")


if __name__ == "__main__":
    main()
