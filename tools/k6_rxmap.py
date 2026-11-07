"""The K6's real receive coverage and sensitivity: run the rxmap module (c/k6/mod/rxmap.c) and
read what it found (docs/k6-rxmap.md).

  python3 tools/k6_rxmap.py plan                          segments, run times, telemetry fit
  python3 tools/k6_rxmap.py map A B C D --bin mod-rxmap.bin --key-file KEY
                                                          the K6 scans 18-1298 MHz, receive only
  python3 tools/k6_rxmap.py map fm air vhf uhf ...        named finer segments (plan lists them)
  python3 tools/k6_rxmap.py cal --bin mod-rxmap.bin --key-file KEY
                                                          the IC-7100 keys carriers on 2 m / 70 cm
                                                          points, the K6 measures them
  python3 tools/k6_rxmap.py decode LOG... [--svg out.svg] the coverage table (and a plot) from the
                                                          logs: this tool's own, or a serial log
  python3 tools/k6_rxmap.py patch IN.bin OUT.bin SEG      a module set up for one segment

map: the K6 receives only. The upload (IC-7100 on 440.850, F/1800, magic 0xAE) carries the
segment in the module's parameter block; the K6 scans it (about a minute for a 320 MHz segment)
and answers on 440.850 with its summary as telemetry (k6_upload.py's ARQ). Each segment's log goes
to rxmap-<seg>-<time>.log.

cal: the IC-7100 transmits, in the US amateur 2 m and 70 cm bands only (430-450 MHz: Lynden is
north of Line A, where 420-430 is closed to amateurs), on a dummy load if you have one. Each
point gets a bare FM carrier for --hold s and the station's CW ID, then the power steps at
440.850; the K6 measures each carrier and sends the lines back when it is done.

hamlet has no clang: give --bin the CI-built mod-rxmap.bin (K6 pager firmware artifact). The
tool patches its parameter block (the bytes after "RXMAP v1") before signing. Never uses ttyUSB2.
SPDX-License-Identifier: MIT
"""
import argparse
import math
import os
import re
import statistics
import struct
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "c", "k6", "mod", "rxmap.c")
MAGIC = b"RXMAP v1"
FMT = "<8sIIIHHHHBBBBBBH16I"          # par_t in rxmap.c, 100 bytes
NCAL = 16
TLM_BYTES = 12 * 57                   # the telemetry the K6 sends back at most
PAGER_HZ = 440_850_000

# name: (lo Hz, bin Hz, bins, step Hz, what it is for)
SEGMENTS = {
    "A": (18_000_000, 2_000_000, 160, 25_000, "18-338 MHz: HF top, FM broadcast, air, 2 m, VHF TV"),
    "B": (338_000_000, 2_000_000, 160, 25_000, "338-658 MHz: 70 cm, land mobile, UHF TV, 600 MHz LTE"),
    "C": (658_000_000, 2_000_000, 160, 25_000, "658-978 MHz: the synth gap, cellular, 33 cm, pagers"),
    "D": (978_000_000, 2_000_000, 160, 25_000, "978-1298 MHz: ADS-B, DME, 23 cm"),
    "fm": (87_800_000, 200_000, 101, 25_000, "FM broadcast 87.8-108.0, a bin per channel"),
    "air": (118_000_000, 250_000, 76, 25_000, "airband 118-137 and VOR below it"),
    "vhf": (130_000_000, 500_000, 100, 25_000, "130-180 MHz: the VHF filter's edges, NOAA"),
    "uhf": (380_000_000, 2_000_000, 80, 25_000, "380-540 MHz: the UHF filter's edges, TV"),
    "2m": (144_000_000, 50_000, 80, 12_500, "2 m in detail"),
    "70cm": (420_000_000, 375_000, 80, 12_500, "70 cm in detail"),
    "gap": (600_000_000, 2_000_000, 125, 25_000, "600-850 MHz: where the synthesizer stops"),
    # below the FD6818's 18 MHz spec (docs/k6-hf.md): does the synthesizer follow? Its own clock
    # harmonics (12, 13 MHz ...) and WWV / CHU carriers say, by turning up at their own frequency
    "hf": (8_000_000, 250_000, 88, 5_000, "8-30 MHz in 250 kHz bins: 18 MHz spec edge, 20 m, WWV 10/15/20/25"),
    "hflo": (1_000_000, 250_000, 28, 5_000, "1-8 MHz: far under the spec (a hung PLL shows as a flat floor)"),
}

# what may be on the air there (US / BC, Pacific Northwest); a label for the table, not a claim
SERVICES = [
    (2.495, 2.505, "WWV 2.5"), (3.33, 3.331, "CHU 3.330"), (3.5, 4.0, "80 m"), (4.995, 5.005, "WWV 5"),
    (5.9, 6.2, "49 m broadcast"), (7.0, 7.3, "40 m"), (7.3, 7.6, "41 m broadcast"), (7.85, 7.851, "CHU 7.850"),
    (9.4, 9.9, "31 m broadcast"), (9.995, 10.005, "WWV 10"), (10.1, 10.15, "30 m"),
    (11.6, 12.1, "25 m broadcast"), (13.57, 13.87, "22 m broadcast"), (14.0, 14.35, "20 m"),
    (14.67, 14.671, "CHU 14.670"), (14.995, 15.005, "WWV 15"), (15.1, 15.8, "19 m broadcast"),
    (17.48, 17.9, "16 m broadcast"), (18.068, 18.168, "17 m"), (19.995, 20.005, "WWV 20"),
    (21.0, 21.45, "15 m"), (21.45, 21.85, "13 m broadcast"), (24.89, 24.99, "12 m"), (24.995, 25.005, "WWV 25"),
    (25.67, 26.1, "11 m broadcast"), (26.96, 27.41, "CB"), (28.0, 29.7, "10 m"), (50.0, 54.0, "6 m"), (54.0, 88.0, "VHF-lo TV / land mobile"),
    (88.0, 108.0, "FM broadcast"), (108.0, 118.0, "VOR / ILS"), (118.0, 137.0, "airband (AM)"),
    (137.0, 138.0, "weather satellites"), (144.0, 148.0, "2 m"), (148.0, 162.4, "land mobile"),
    (162.4, 162.56, "NOAA weather"), (162.56, 174.0, "land mobile"), (174.0, 216.0, "VHF TV"),
    (216.0, 222.0, "land mobile"), (222.0, 225.0, "1.25 m"), (225.0, 400.0, "military air"),
    (400.0, 406.0, "radiosondes"), (406.0, 420.0, "federal"), (420.0, 450.0, "70 cm"),
    (450.0, 470.0, "land mobile / GMRS"), (470.0, 608.0, "UHF TV"), (608.0, 617.0, "radio astronomy / TV"),
    (617.0, 652.0, "600 MHz LTE down"), (652.0, 698.0, "600 MHz LTE up"), (698.0, 806.0, "700 MHz LTE"),
    (806.0, 824.0, "SMR / cellular up"), (851.0, 869.0, "SMR / public safety"), (869.0, 894.0, "cellular down"),
    (896.0, 902.0, "SMR"), (902.0, 928.0, "33 cm / ISM"), (929.0, 932.0, "paging"),
    (935.0, 941.0, "SMR"), (960.0, 1215.0, "aero nav (DME, ADS-B 1090)"), (1240.0, 1300.0, "23 cm"),
]
# where the K6 should hear, as far as we know (unverified on the FD6818: the run settles it)
REGIONS = [
    (1, 18, "below the 18 MHz spec"), (18, 30, "HF top (above spec)"), (30, 136, "below the VHF filter"), (136, 174, "VHF filter"), (174, 300, "above the VHF filter"),
    (300, 400, "below the UHF filter"), (400, 520, "UHF filter"), (520, 630, "above the UHF filter"),
    (630, 840, "synthesizer gap?"), (840, 1300, "synth top band"),
]
XTAL_HZ = 26_000_000                  # the FD6818's crystal
CLOCK_HZ = 6_000_000                  # a 24 MHz clock's grid (A: 144, 168, 240 ...; D: a 6 MHz comb)
BENCH_HZ = 25_000_000                 # 125 and 175 MHz on the bench: a 25 MHz clock (hamlet's Ethernet?)


# ---- the parameter block ----

def pack(lo, bin_hz, nbins, step, mode=0, quick_us=3000, full_us=30000, wide=1, tops=12, det_db=15,
         cal=(PAGER_HZ,), quiet_s=10, max_s=240):
    if mode == 0:
        nsteps = bin_hz // step
        if nsteps * step != bin_hz or not 1 <= nsteps <= 127 or not 1 <= nbins <= 340:
            raise ValueError("bin_hz must be 1..127 steps, and 1..340 bins")
    else:
        nsteps = 1
    cal = list(cal)
    if not 1 <= len(cal) <= NCAL:
        raise ValueError(f"1..{NCAL} cal points")
    return struct.pack(FMT, MAGIC, lo, bin_hz, step, nbins, nsteps, quick_us, full_us, mode, wide, tops, det_db,
                       len(cal), quiet_s, max_s, *(cal + [0] * (NCAL - len(cal))))


def unpack(blk):
    v = struct.unpack(FMT, blk[:struct.calcsize(FMT)])
    keys = ("magic lo bin step nbins nsteps quick_us full_us mode wide tops det_db ncal quiet_s max_s").split()
    d = dict(zip(keys, v))
    d["cal"] = list(v[len(keys):len(keys) + d["ncal"]])
    return d


def patch(code, blk):
    """the module's code with its parameter block replaced"""
    at = code.find(MAGIC)
    if at < 0 or code.find(MAGIC, at + 1) >= 0:
        raise ValueError("not the rxmap module (no single RXMAP v1 block)")
    return code[:at] + blk + code[at + len(blk):]


def seg_block(name, **kw):
    lo, b, n, s, _ = SEGMENTS[name] if name in SEGMENTS else parse_range(name)
    return pack(lo, b, n, s, **kw)


def parse_range(text):
    """'150-160/500/25' (MHz, bin kHz, step kHz) -> a segment"""
    m = re.fullmatch(r"([\d.]+)-([\d.]+)(?:/([\d.]+))?(?:/([\d.]+))?", text)
    if not m:
        raise SystemExit(f"segment {text!r}: a name from `plan` or LO-HI[/BIN_kHz[/STEP_kHz]] in MHz")
    lo, hi = round(float(m.group(1)) * 1e6), round(float(m.group(2)) * 1e6)
    b = round(float(m.group(3) or 2000) * 1e3)
    s = round(float(m.group(4) or 25) * 1e3)
    return lo, b, max(1, round((hi - lo) / b)), s, text


def estimate(nbins, nsteps, quick_us=3000, full_us=30000, tune_us=2300):
    """seconds a map run takes on the K6: a step = tune + quick_us, a full read = full_us + 3 ms, a line
    5 ms. tune_us from the first bench run (2026-11-07: segment A took 79.7 s, so a tune costs ~2.3 ms)"""
    return nbins * (nsteps * (quick_us + tune_us) + 2 * (full_us + 3500 + tune_us) + 5000) / 1e6


def tlm_size(nbins, tops=12):
    rows = 2 * (nbins + 6 * -(-nbins // 50))
    return rows + 13 * tops + 4 * -(-tops // 4) + 61


def mhz(hz):
    return f"{hz / 1e6:.3f}"


# ---- reading the results ----

def lines_of(text):
    """the module's lines from any log: telemetry as k6_upload prints it, or `K6 MOD say` serial lines"""
    out = []
    for ln in text.splitlines():
        m = re.search(r"K6 MOD say (.*)$", ln)
        out.append((m.group(1) if m else ln).rstrip())
    return out


def map_run(meta, rows, detail, tops):
    """one map run from its M line and the lines before it"""
    r = dict(meta, kind="map", bins=[], tops=tops)
    for i in range(r["n"]):
        f = r["lo"] + i * r["bin"]
        if f in detail:                 # serial: exact dBm and the peak's frequency
            fl, pk, at = detail[f]
        else:                           # telemetry: 2 dB steps, the peak's frequency if listed
            fl, pk, at = rows["F"].get(i), rows["P"].get(i), None
        r["bins"].append({"f": f, "fl": fl, "pk": pk, "at": at})
    for f, pk in tops:
        i = (f - r["lo"]) // r["bin"]
        if 0 <= i < r["n"] and r["bins"][i]["at"] is None:
            r["bins"][i].update(at=f, pk=pk)
    return r


def decode(text):
    """-> list of runs: {"kind": "map", "lo", "bin", "n", "bins": [{f, fl, pk, at}], "tops", "ms"} or
    {"kind": "cal", "floors": {hz: dBm}, "events": [...]}"""
    runs = []
    rows, detail, tops = {"F": {}, "P": {}}, {}, []

    def cal_run():
        if not runs or runs[-1]["kind"] != "cal" or runs[-1].get("done"):
            runs.append({"kind": "cal", "floors": {}, "events": []})
        return runs[-1]
    for ln in lines_of(text):
        if m := re.fullmatch(r"b (\d+\.\d{3}) (-?\d+) (-?\d+) @(\d+\.\d{3}) z([0-9a-f]{4})", ln):
            detail[round(float(m.group(1)) * 1e6)] = (int(m.group(2)), int(m.group(3)),
                                                     round(float(m.group(4)) * 1e6))
        elif m := re.fullmatch(r"T((?: \d+\.\d{3}:-?\d+)+)", ln):
            tops += [(round(float(f) * 1e6), int(v)) for f, v in (t.split(":") for t in m.group(1).split())]
        elif m := re.fullmatch(r"([FP])(\d+) ([0-o]+)", ln):
            for k, c in enumerate(m.group(3)):
                rows[m.group(1)][int(m.group(2)) + k] = (ord(c) - 48) * 2 - 160
        elif m := re.fullmatch(r"M lo=(\d+\.\d{3}) b=(\d+) n=(\d+) s=(\d+) q=(\d+) sig=(\d+) ms=(\d+)", ln):
            meta = {"lo": round(float(m.group(1)) * 1e6), "bin": int(m.group(2)) * 1000, "n": int(m.group(3)),
                    "step": int(m.group(4)), "q": int(m.group(5)), "sig": int(m.group(6)), "ms": int(m.group(7))}
            runs.append(map_run(meta, rows, detail, tops))
            rows, detail, tops = {"F": {}, "P": {}}, {}, []
        elif m := re.fullmatch(r"C((?: \d+\.\d{3}:-?\d+)+)", ln):
            r = cal_run()
            for t in m.group(1).split():
                f, v = t.split(":")
                r["floors"][round(float(f) * 1e6)] = int(v)
        elif m := re.fullmatch(r"c (\d+\.\d{3}) (-?\d+) (-?\d+) fl(-?\d+) n(\d+) z(\d+)", ln):
            cal_run()["events"].append({"f": round(float(m.group(1)) * 1e6), "mean": int(m.group(2)),
                                        "max": int(m.group(3)), "fl": int(m.group(4)), "n": int(m.group(5)),
                                        "z": int(m.group(6))})
        elif m := re.fullmatch(r"K ev=(\d+) ms=(\d+)", ln):
            cal_run().update(done=True, ms=int(m.group(2)))
    return runs


def service(hz):
    f = hz / 1e6
    return next((s for lo, hi, s in SERVICES if lo <= f < hi), "")


def birdie(hz, tol=30_000):
    """a harmonic of the K6's own clocks, on the bench 2026-11-07: 13 MHz (half the FD6818's 26 MHz
    crystal: every peak in 658-978 MHz) and 6 MHz (24 MHz multiples at 144-336 MHz in segment A, a 6 MHz
    comb at -128 to -134 dBm in 978-1298), and 25 MHz (125, 175 MHz: probably the bench PC): likely
    not off the air. Flags ~1.7% of real signals by chance."""
    if hz is None:
        return False
    for c in (XTAL_HZ // 2, CLOCK_HZ, BENCH_HZ):    # 13 MHz covers 26's harmonics too
        n = round(hz / c)
        if n > 0 and abs(hz - n * c) <= tol:
            return True
    return False


def coverage(runs):
    """bins from every map run, merged and sorted by frequency (a later run wins a duplicate)"""
    by = {}
    for r in runs:
        if r["kind"] == "map":
            for b in r["bins"]:
                if b["fl"] is not None and b["pk"] is not None:
                    by[(b["f"], r["bin"])] = b
    return [b for _, b in sorted(by.items())]


def report(runs):
    out = []
    bins = coverage(runs)
    if bins:
        out.append("Receive coverage (map runs): median floor, signals 10 dB or more over the floor")
        out.append(f"{'MHz':>11}  {'region':<22} {'bins':>4} {'floor':>6} {'sig':>4}  strongest")
        for lo, hi, name in REGIONS:
            sel = [b for b in bins if lo * 1e6 <= b["f"] < hi * 1e6]
            if not sel:
                continue
            fl = statistics.median(b["fl"] for b in sel)
            sig = [b for b in sel if b["pk"] - b["fl"] >= 10 and not birdie(b["at"])]
            best = sorted(sig, key=lambda b: b["fl"] - b["pk"])[:3]
            s = ", ".join(f"{mhz(b['at'] or b['f'])} {b['pk']} ({service(b['at'] or b['f'])})" for b in best)
            out.append(f"{lo:>5}-{hi:<5}  {name:<22} {len(sel):>4} {fl:>6.0f} {len(sig):>4}  {s}")
        sig = sorted((b for b in bins if b["pk"] - b["fl"] >= 10), key=lambda b: b["fl"] - b["pk"])
        out.append("")
        out.append("Signals, strongest over floor first (* = on a 13, 6 or 25 MHz harmonic: a clock, not off the air)")
        for b in sig[:40]:
            f = b["at"] or b["f"]
            out.append(f"  {mhz(f):>9} MHz  {b['pk']:>4} dBm  +{b['pk'] - b['fl']:<3} {'*' if birdie(f) else ' '} "
                       f"{service(f)}")
    for r in runs:
        if r["kind"] != "cal":
            continue
        out.append("")
        out.append("Calibrated points (IC-7100 carriers): mean / max dBm, floor, over floor, noise indicator")
        for e in r["events"]:
            out.append(f"  {mhz(e['f'])} MHz  {e['mean']:>4} / {e['max']:>4}  fl {e['fl']:>4}  "
                       f"+{e['mean'] - e['fl']:<3}  n {e['n']:<3} z {e['z']}")
        if not r["events"]:
            out.append("  no carrier seen")
    return "\n".join(out)


def svg(runs, path, width=1100, height=420):
    """the coverage as a plot: floor (grey) and peak (blue) per bin, the filters' nominal bands shaded"""
    bins = coverage(runs)
    if not bins:
        return False
    f0 = min(b["f"] for b in bins) / 1e6
    f1 = max(b["f"] for b in bins) / 1e6 + 1
    lx, rx, ty, by = 60, width - 20, 20, height - 40
    y0, y1 = -160, -20

    def X(f):
        return lx + (f - f0) / (f1 - f0) * (rx - lx)

    def Y(v):
        return by - (max(y0, min(y1, v)) - y0) / (y1 - y0) * (by - ty)
    el = [f'<rect width="{width}" height="{height}" fill="#fff"/>']
    for lo, hi, col in ((136, 174, "#e8f1e4"), (400, 520, "#e8f1e4"), (630, 840, "#f6e7e7")):
        if hi > f0 and lo < f1:
            el.append(f'<rect x="{X(max(lo, f0)):.1f}" y="{ty}" width="{X(min(hi, f1)) - X(max(lo, f0)):.1f}" '
                      f'height="{by - ty}" fill="{col}"/>')
    for v in range(y0, y1 + 1, 20):
        el.append(f'<line x1="{lx}" x2="{rx}" y1="{Y(v):.1f}" y2="{Y(v):.1f}" stroke="#ddd"/>'
                  f'<text x="{lx - 6}" y="{Y(v) + 4:.1f}" font-size="11" text-anchor="end">{v}</text>')
    step = 10 ** math.floor(math.log10(max(1e-3, f1 - f0) / 2))
    step *= 5 if (f1 - f0) / step > 25 else 2 if (f1 - f0) / step > 10 else 1
    t = math.ceil(f0 / step) * step
    while t <= f1:
        el.append(f'<line x1="{X(t):.1f}" x2="{X(t):.1f}" y1="{by}" y2="{by + 4}" stroke="#444"/>'
                  f'<text x="{X(t):.1f}" y="{by + 17}" font-size="11" text-anchor="middle">{t:g}</text>')
        t += step
    for key, col in (("fl", "#888"), ("pk", "#1f5fbf")):
        pts = " ".join(f"{X(b['f'] / 1e6):.1f},{Y(b[key]):.1f}" for b in bins)
        el.append(f'<polyline points="{pts}" fill="none" stroke="{col}" stroke-width="1.2"/>')
    for b in sorted(bins, key=lambda b: b["fl"] - b["pk"])[:12]:
        f = (b["at"] or b["f"]) / 1e6
        if b["pk"] - b["fl"] >= 10:
            el.append(f'<text x="{X(f):.1f}" y="{Y(b["pk"]) - 4:.1f}" font-size="10" text-anchor="middle" '
                      f'fill="#1f5fbf">{f:.1f}</text>')
    el.append(f'<text x="{lx}" y="{height - 6}" font-size="11">MHz · dBm (RSSI) · grey floor, blue peak · '
              f'green: K6 filters (nominal), red: synth gap (nominal)</text>')
    with open(path, "w") as fh:
        fh.write(f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
                 f'font-family="sans-serif">{"".join(el)}</svg>\n')
    return True


# ---- on the bench ----

def code_of(a):
    if a.bin:
        return open(a.bin, "rb").read()
    from modem import k6mod
    return k6mod.build(SRC)[0]


def upload_args(a, path, run_s):
    cmd = [sys.executable, os.path.join(ROOT, "tools", "k6_upload.py"), path, "--key-file", a.key_file,
           "--api-level", "2", "--telemetry", "12", "--run-s", f"{run_s:.0f}", "--rigctld", a.rigctld]
    return cmd + (["--dry-run"] if a.dry_run else [])


def cmd_map(a):
    code = code_of(a)
    for name in a.segments:
        blk = seg_block(name, quick_us=a.quick_us, wide=0 if a.narrow else 1)
        p = unpack(blk)
        run_s = estimate(p["nbins"], p["nsteps"], a.quick_us) + 15
        print(f"{time.strftime('%H:%M:%S')} segment {name}: {mhz(p['lo'])}-"
              f"{mhz(p['lo'] + p['nbins'] * p['bin'])} MHz, {p['nbins']} bins; the K6 receives only, "
              f"about {run_s - 15:.0f} s, then reports on 440.850", flush=True)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "mod-rxmap.bin")
            open(path, "wb").write(patch(code, blk))
            log = f"rxmap-{name.replace('/', '_')}-{time.strftime('%Y%m%d-%H%M%S')}.log"
            with open(log, "w") as fh:
                proc = subprocess.Popen(upload_args(a, path, run_s), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True, cwd=ROOT)
                for ln in proc.stdout:
                    sys.stdout.write(ln)
                    fh.write(ln)
                proc.wait()
            runs = decode(open(log).read())
            print(f"log: {log}")
            print(report(runs) if runs else "no summary decoded (see the log; a serial log has it all)")


CAL_POINTS = [144_050_000, 145_550_000, 146_550_000, 147_450_000, 147_950_000,
              430_100_000, 433_000_000, 440_850_000, 445_950_000, 449_900_000]
POWERS = [1.0, 0.5, 0.25, 0.1, 0.0]


# US amateur HF segments for --hf, the PSK31 test into a dummy load (Jeff OK'd 17 m and 20 m at RF
# power 0.0, 2026-11-07, docs/k6-hf.md). Points are USB dial frequencies (PSK31 watering holes); the
# signal is TONE_HZ above them, and that is where the K6 listens
HF_BANDS = [(14_000_000, 14_350_000), (18_068_000, 18_168_000)]
HF_POINTS = [18_100_000, 14_070_000]
TONE_HZ = 1000
HF_TEXT = "AG7EW TEST "


def allowed(hz, line_a_north=True, hf=False):
    """US amateur 2 m / 70 cm, 10 kHz inside the edges; 430-450 north of Line A. hf: also 20 and 17 m
    (10 kHz inside their edges), for the HF PSK31 test into a dummy load"""
    lo70 = 430_000_000 if line_a_north else 420_000_000
    if hf and any(lo + 10_000 <= hz <= hi - 10_000 for lo, hi in HF_BANDS):
        return True
    return 144_010_000 <= hz <= 147_990_000 or lo70 + 10_000 <= hz <= 449_990_000


def cal_schedule(points, powers, power_at=PAGER_HZ):
    """[(hz, rf power or None)]: every point at the radio's own power, then power_at stepped down"""
    return [(f, None) for f in points] + [(power_at, p) for p in powers]


def cmd_cal(a):
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import k6_upload as up
    import soundfile as sf
    from modem import k6
    points = ([round(float(x) * 1e6) for x in a.points.split(",")] if a.points
              else HF_POINTS if a.hf else CAL_POINTS)
    powers = ([] if a.powers == "none" else [float(x) for x in a.powers.split(",")] if a.powers
              else [] if a.hf else POWERS)
    bad = [f for f in points if not allowed(f, not a.south_of_line_a, a.hf)]
    if bad:
        sys.exit("not on 2 m / 70 cm (430-450 north of Line A)" + (" or 20/17 m" if a.hf else "")
                 + ": " + ", ".join(map(mhz, bad)))
    hold = a.hold if a.hold is not None else 10.0 if a.hf else 2.5
    if a.hf:
        from modem import psk31
        psk = psk31.modulate(psk31.repeat_to(HF_TEXT, hold), up.FS, TONE_HZ, level=0.3)
        hold = len(psk) / up.FS
    k6hz = {f: f + TONE_HZ if a.hf else f for f in points}     # where the K6 listens for each point
    # --hf leaves 440.850 out: the closing CW ID there must not count as "seen" (run 2, 2026-11-07)
    uniq = list(dict.fromkeys([k6hz[f] for f in points] + ([] if a.hf else [PAGER_HZ])))
    if len(uniq) > NCAL:
        sys.exit(f"at most {NCAL - 1} points besides 440.850")
    sched = cal_schedule(points, powers)
    per = hold + (2.6 if a.id_each else 0) + a.gap + 0.6
    total = len(sched) * per
    max_s = int(total + (40 if a.hf else 120))   # the K6 gives up this long after it starts
    blk = pack(PAGER_HZ, 0, 0, 0, mode=1, cal=uniq, quiet_s=12, max_s=max_s, det_db=a.det_db,
               wide=0 if a.narrow else 1)
    ns = SimpleNamespace(rigctld=a.rigctld, dry_run=a.dry_run, device=up.PLAY, rec_device=up.REC, phy="F",
                         baud=1800.0, payload="XL", level=0.3, telemetry=12, api_level=2, key_file=a.key_file,
                         source=None)
    f = up.rig(ns, "f")
    if not a.dry_run and int(f) != PAGER_HZ:
        sys.exit(f"IC-7100 on {f} Hz, not 440850000: set both radios to 440.850 first")
    up.rig(ns, "M", "PKTFM", 12000)       # 0 picks the 6 kHz filter at half deviation (PR #81)
    p0 = up.rig(ns, "l", "RFPOWER")
    p0 = float(p0) if not a.dry_run else 0.5
    serial = int(time.time())
    xfer = serial & 0xFF
    log = f"rxmap-cal-{time.strftime('%Y%m%d-%H%M%S')}.log"
    out = open(log, "w")

    def note(s):
        s = f"{time.strftime('%H:%M:%S')} {s}"
        print(s, flush=True)
        out.write(s + "\n")
    with tempfile.TemporaryDirectory() as d:
        ns.source = os.path.join(d, "mod-rxmap.bin")
        open(ns.source, "wb").write(patch(code_of(a), blk))
        idw = os.path.join(d, "id.wav")
        sf.write(idw, k6.cw(a.call, up.FS, wpm=25), up.FS, subtype="PCM_16")
        pskw = os.path.join(d, "psk31.wav")
        if a.hf:
            sf.write(pskw, psk, up.FS, subtype="PCM_16")
        count = up.chunks_of(ns)
        rec = up.Recorder(ns, d)
        try:
            todo = list(range(count))
            for rnd in range(1, 5):
                w = os.path.join(d, f"up{rnd}.wav")
                note(f"upload {rnd}: {len(todo)} of {count} chunks; {up.mod_wav(ns, w, serial, xfer, todo)}")
                up.key_and_play(ns, w)
                t_run = time.time()                       # the K6 starts once it holds every chunk
                since = rec.samples()
                st, _, _ = up.listen(ns, rec, since, xfer, 12.0)
                if st is None:
                    note("no partway report: the K6 has the module and is measuring its floors")
                    break
                step, arg = up.next_step(st, count, xfer)
                if step != "send":
                    sys.exit(f"the K6 answered: {arg}")
                todo = arg
            else:
                sys.exit("upload incomplete after 4 sends")
            time.sleep(2.0)
            if a.hf:
                up.rig(ns, "L", "RFPOWER", f"{a.hf_power:.2f}")
            note(f"the K6 listens; the IC-7100 sends {len(sched)} " + ("PSK31 bursts" if a.hf else "carriers")
                 + f", {total:.0f} s in all (RF power {a.hf_power if a.hf else p0:.2f} unless stepped)")
            for i, (hz, pw) in enumerate(sched):
                up.rig(ns, "F", hz)
                if a.hf:
                    up.rig(ns, "M", "PKTUSB", 0)
                    if not a.dry_run and int(up.rig(ns, "f")) != hz:
                        sys.exit(f"IC-7100 didn't take {hz} Hz")
                if pw is not None:
                    up.rig(ns, "L", "RFPOWER", f"{pw * p0:.2f}")
                if up.rig(ns, "t") != "0":
                    time.sleep(5)
                note(("PSK31 " + repr(HF_TEXT.strip()) + f" USB dial {mhz(hz)}, K6 on {mhz(k6hz.get(hz, hz))}"
                      if a.hf and hz in k6hz else f"carrier {i + 1}/{len(sched)}: {mhz(hz)} MHz")
                     + (f", RF power {pw * p0:.2f} ({pw:g} of {p0:.2f})" if pw is not None else ""))
                # under the bench's PTT lock, so the CW ID keeper waits; lead 0: the K6's
                # schedule counts the hold from T 1
                with up.bench.keyed(lambda *v: up.rig(ns, *v), "data", f"rxmap carrier {mhz(hz)}",
                                    dry_run=ns.dry_run, lead=0.0):
                    if a.hf:
                        up.bench.play(pskw, ns.device, ns.dry_run)
                    else:
                        time.sleep(hold)
                    if a.id_each:
                        up.bench.play(idw, ns.device, ns.dry_run)
                time.sleep(a.gap)
        finally:
            up.rig(ns, "F", PAGER_HZ)
            if a.hf:
                up.rig(ns, "M", "PKTFM", 12000)
            up.rig(ns, "L", "RFPOWER", f"{p0:.2f}")
        try:
            note("back on 440.850; waiting for the K6's report")
            since = rec.samples()
            # it reports 12 s after the last carrier it saw, or at max_s if it saw none: wait for both
            st, text, _ = up.listen(ns, rec, since, xfer, max(60.0, t_run + max_s + 30 - time.time()))
            if st is not None:
                note(f"K6: {st['state']}, returned {st['ret']} in {st['ms']} ms")
            out.write("K6 telemetry:\n" + (text or "") + "\n")
            print(report(decode(text or "")) or "no lines decoded")
        finally:
            rec.stop()
            note("CW ID")
            up.key_and_play(ns, idw, "id", a.call)
            out.close()
            print(f"log: {log}")


def cmd_plan(a):
    print(f"{'segment':<8} {'MHz':<19} {'bins':>4} {'bin kHz':>8} {'run s':>6} {'tlm B':>6}  what")
    for name, (lo, b, n, s, what) in SEGMENTS.items():
        t = tlm_size(n)
        print(f"{name:<8} {mhz(lo):>8}-{mhz(lo + n * b):<10} {n:>4} {b / 1e3:>8g} "
              f"{estimate(n, b // s):>6.0f} {t:>6}{'!' if t > TLM_BYTES else ' '} {what}")
    print(f"\ncal: {len(CAL_POINTS)} points + {len(POWERS)} power steps at 440.850; points (MHz): "
          + ", ".join(mhz(f) for f in CAL_POINTS))
    print(f"telemetry holds {TLM_BYTES} B; '!' = the summary won't all come back over the air (serial has it)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("map", "cal"):
        s = sub.add_parser(name)
        s.add_argument("--bin", help="CI's mod-rxmap.bin (else built from c/k6/mod/rxmap.c with clang)")
        s.add_argument("--key-file", required=True)
        s.add_argument("--rigctld", default="localhost:4532")
        s.add_argument("--narrow", action="store_true", help="narrow filter (default wide)")
        s.add_argument("--dry-run", action="store_true")
    m = sub.choices["map"]
    m.add_argument("segments", nargs="+", help="names from `plan`, or LO-HI[/BIN_kHz[/STEP_kHz]] in MHz")
    m.add_argument("--quick-us", type=int, default=3000)
    c = sub.choices["cal"]
    c.add_argument("--points", help="MHz, comma separated (default: plan's list)")
    c.add_argument("--powers", help="RF power steps at 440.850, 0..1 of the radio's own (default 1,.5,.25,.1,0; "
                                    "'none' for no steps)")
    c.add_argument("--hold", type=float, help="s of bare carrier a point (default 2.5; --hf: about 10 s of PSK31)")
    c.add_argument("--gap", type=float, default=1.5, help="s unkeyed between points (default 1.5)")
    c.add_argument("--no-id-each", dest="id_each", action="store_false", help="no CW ID after each carrier")
    c.add_argument("--det-db", type=int, default=15)
    c.add_argument("--call", default="AG7EW")
    c.add_argument("--south-of-line-a", action="store_true", help="allow 420-430 MHz")
    c.add_argument("--hf", action="store_true",
                   help="HF test (Jeff's OK each run, dummy load on the HF port): the IC-7100 sends PSK31 "
                        f"'{HF_TEXT.strip()}' over and over in PKTUSB, dial 18.100 and 14.070 (any point in "
                        "20/17 m), the K6 listens 1 kHz up; no power steps")
    c.add_argument("--hf-power", type=float, default=0.0, help="--hf: RF POWER 0..1 (default 0.0, the minimum)")
    d = sub.add_parser("decode")
    d.add_argument("logs", nargs="+")
    d.add_argument("--svg")
    sub.add_parser("plan")
    p = sub.add_parser("patch")
    p.add_argument("inp")
    p.add_argument("out")
    p.add_argument("segment")
    a = ap.parse_args()
    if a.cmd in ("map", "cal"):
        a.key_file = os.path.abspath(os.path.expanduser(a.key_file))   # the uploads run from the repo root
    if a.cmd == "map":
        cmd_map(a)
    elif a.cmd == "cal":
        cmd_cal(a)
    elif a.cmd == "plan":
        cmd_plan(a)
    elif a.cmd == "patch":
        open(a.out, "wb").write(patch(open(a.inp, "rb").read(), seg_block(a.segment)))
    else:
        runs = []
        for path in a.logs:
            runs += decode(open(path, errors="replace").read())
        print(report(runs))
        if a.svg and svg(runs, a.svg):
            print(f"plot: {a.svg}")


if __name__ == "__main__":
    main()
