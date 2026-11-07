"""Can the K6 hear below 18 MHz? Run the hfprobe module (c/k6/mod/hfprobe.c) and read what it
found (docs/k6-hf.md). Receive only: the K6 transmits nothing but its usual report on 440.850.

  python3 tools/k6_hf.py plan                              the probes and configurations
  python3 tools/k6_hf.py probe --bin mod-hfprobe.bin --key-file KEY [--probes MHz,...] [--cfg ...]
  python3 tools/k6_hf.py decode LOG...                     the table from a log (this tool's or serial)
  python3 tools/k6_hf.py patch IN.bin OUT.bin [--probes ...] [--cfg ...]
  python3 tools/k6_hf.py bandreg --bin mod-bandreg.bin --key-file KEY [--mhz ...]
                                                           which registers follow the tuning; PLL lock

Each probe is a frequency whose signal is known exactly: the K6's own clock harmonics and the time
stations. A probe the synthesizer really reaches sees its signal over the floor 30 kHz above it.
A configuration (--cfg, space separated) is PATH[:REG=VALUE]: PATH 0 Aura's front-end choice (VHF
below 300 MHz), 1 UHF path (PA13), 2 VHF path (PA14), 3 neither, 4 both; REG=VALUE one FD6818
register (hex) for that configuration, put back after it, e.g. 0:3e=b000.

hamlet has no clang: give --bin the CI-built mod-hfprobe.bin. Never uses ttyUSB2.
SPDX-License-Identifier: MIT
"""
import argparse
import os
import re
import struct
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "c", "k6", "mod", "hfprobe.c")
BAND_SRC = os.path.join(ROOT, "c", "k6", "mod", "bandreg.c")
MAGIC = b"HFPROBE1"
NPROBE, NCFG = 16, 8
FMT = "<8sIHBBB3x16I" + "BBH" * NCFG    # par_t in hfprobe.c, 116 bytes

# exact-frequency signals from 26 MHz down: clock harmonics (the FD6818's 26 MHz crystal and its
# 13 MHz half, the 24 MHz family's 12 and 6 MHz) and WWV (Fort Collins) / CHU (Ottawa) carriers
PROBES = [26.0, 25.0, 24.0, 20.0, 18.0, 16.0, 15.0, 14.67, 13.0, 12.0, 10.0, 7.85, 6.5, 6.0, 5.0, 3.33]
CFGS = ["0", "3", "1", "4", "0:3e=8000", "0:3e=c000"]
WHAT = {26.0: "26 MHz xtal", 25.0: "WWV 25", 24.0: "24 MHz clock", 20.0: "WWV 20", 18.0: "6 MHz grid",
        16.0: "?", 15.0: "WWV 15", 14.67: "CHU", 13.0: "13 MHz (xtal/2)", 12.0: "6 MHz grid",
        10.0: "WWV 10", 7.85: "CHU", 6.5: "xtal/4", 6.0: "6 MHz grid", 5.0: "WWV 5", 3.33: "CHU"}
PATHS = ["Aura", "UHF path", "VHF path", "no path", "both paths"]


def parse_cfg(text):
    m = re.fullmatch(r"([0-4])(?::([0-9a-fA-F]{1,2})=([0-9a-fA-F]{1,4}))?", text)
    if not m:
        raise SystemExit(f"--cfg {text!r}: PATH[:REG=VALUE], PATH 0-4, REG and VALUE hex")
    reg = int(m.group(2), 16) if m.group(2) else 0
    if reg in (0x30, 0x33, 0x36, 0x37):
        raise SystemExit(f"REG_{reg:02X} is refused by the core (state, GPIO, PA, power)")
    return int(m.group(1)), reg, int(m.group(3), 16) if m.group(3) else 0


def pack(probes_hz, cfgs, ref_hz=30_000, full_us=30_000, wide=1):
    if not 1 <= len(probes_hz) <= NPROBE or not 1 <= len(cfgs) <= NCFG:
        raise ValueError(f"1..{NPROBE} probes and 1..{NCFG} configurations")
    pr = list(probes_hz) + [0] * (NPROBE - len(probes_hz))
    cf = [v for c in list(cfgs) + [(0, 0, 0)] * (NCFG - len(cfgs)) for v in c]
    return struct.pack(FMT, MAGIC, ref_hz, full_us, len(probes_hz), len(cfgs), wide, *pr, *cf)


def unpack(blk):
    v = struct.unpack(FMT, blk[:struct.calcsize(FMT)])
    n, c = v[3], v[4]
    return {"ref": v[1], "full_us": v[2], "probes": list(v[6:6 + n]),
            "cfgs": [tuple(v[22 + 3 * i:25 + 3 * i]) for i in range(c)], "wide": v[5]}


def patch(code, blk):
    at = code.find(MAGIC)
    if at < 0 or code.find(MAGIC, at + 1) >= 0:
        raise ValueError("not the hfprobe module (no single HFPROBE1 block)")
    return code[:at] + blk + code[at + len(blk):]


def block_of(a):
    probes = [round(float(x) * 1e6) for x in a.probes.split(",")] if a.probes else [round(f * 1e6) for f in PROBES]
    return pack(probes, [parse_cfg(c) for c in (a.cfg or CFGS)], ref_hz=a.ref_khz * 1000)


def estimate(n, c, full_us=30_000, tune_us=2300):
    return n * c * 2 * (full_us + 3500 + tune_us + 5000) / 1e6


def cfg_name(c):
    path, reg, val = c
    return PATHS[path] + (f", REG_{reg:02X}={val:04X}" if reg else "")


# ---- bandreg (c/k6/mod/bandreg.c): registers that follow the tuning, and REG_0C bit 9 (PLL lock) ----

BAND_MAGIC = b"BANDREG1"
BAND_FMT = "<8sIBBH24I"                  # 112 bytes
# the first is the baseline; the rest walk down through the guide's bands to below divide-by-96's floor
BAND_MHZ = [440.85, 145.0, 60.0, 40.0, 28.0, 23.5, 20.0, 18.0, 17.0, 16.5, 16.0, 15.5, 15.0, 14.0, 12.0, 10.0]


def band_pack(hz, settle_us=30_000, wide=1):
    if not 1 <= len(hz) <= 24:
        raise ValueError("1..24 frequencies")
    return struct.pack(BAND_FMT, BAND_MAGIC, settle_us, len(hz), wide, 0, *(list(hz) + [0] * (24 - len(hz))))


def band_patch(code, blk):
    at = code.find(BAND_MAGIC)
    if at < 0 or code.find(BAND_MAGIC, at + 1) >= 0:
        raise ValueError("not the bandreg module (no single BANDREG1 block)")
    return code[:at] + blk + code[at + len(blk):]


def band_decode(text):
    """-> [(hz, reg0c, {reg: value})] in order; '+' lines carry on the line before"""
    out = []
    for ln in text.splitlines():
        m = re.search(r"K6 MOD say (.*)$", ln)
        ln = (m.group(1) if m else ln).strip()
        if m := re.fullmatch(r"([fd]) (\d+\.\d{3}) 0c=([0-9a-f]{4})((?: [0-9a-f]{2}=[0-9a-f]{4})*)", ln):
            regs = {int(k, 16): int(v, 16) for k, v in (t.split("=") for t in m.group(4).split())}
            out.append((round(float(m.group(2)) * 1e6), int(m.group(3), 16), regs))
        elif out and (m := re.fullmatch(r"\+((?: [0-9a-f]{2}=[0-9a-f]{4})*)", ln)):
            out[-1][2].update({int(k, 16): int(v, 16) for k, v in (t.split("=") for t in m.group(1).split())})
    return out


def band_table(rows):
    regs = sorted({r for _, _, d in rows for r in d})
    lines = [f"{'MHz':>8} {'lock':>4} {'0c':>5}" + "".join(f" {r:02x}".rjust(6) for r in regs)]
    for hz, c, d in rows:
        lines.append(f"{hz / 1e6:>8.3f} {('yes' if c & 0x200 else 'NO'):>4} {c:04x} "
                     + "".join(f" {d[r]:04x}" if r in d else "     ." for r in regs))
    return "\n".join(lines)


def cmd_bandreg(a):
    hz = [round(float(x) * 1e6) for x in a.mhz.split(",")] if a.mhz else [round(f * 1e6) for f in BAND_MHZ]
    code = open(a.bin, "rb").read() if a.bin else __import__("modem.k6mod", fromlist=["build"]).build(BAND_SRC)[0]
    print(f"{time.strftime('%H:%M:%S')} bandreg: {len(hz)} frequencies, the K6 receives only (no register "
          "writes), then reports on 440.850", flush=True)
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "mod-bandreg.bin")
        open(path, "wb").write(band_patch(code, band_pack(hz)))
        log = f"bandreg-{time.strftime('%Y%m%d-%H%M%S')}.log"
        cmd = [sys.executable, os.path.join(ROOT, "tools", "k6_upload.py"), path, "--key-file", a.key_file,
               "--api-level", "2", "--telemetry", "12", "--run-s", f"{len(hz) * 0.2 + 15:.0f}", "--rigctld", a.rigctld]
        cmd += ["--dry-run"] if a.dry_run else []
        with open(log, "w") as fh:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=ROOT)
            for ln in proc.stdout:
                sys.stdout.write(ln)
                fh.write(ln)
            proc.wait()
    print(f"log: {log}")
    print(band_table(band_decode(open(log).read())))


# ---- reading the results ----

def decode(text):
    """-> {"p": {(cfg, hz): (sig, ref)}, "r": {cfg: str}, "S": {cfg: [(sig, ref)]}, "ms": int}"""
    out = {"p": {}, "r": {}, "S": {}, "ms": None}
    for ln in text.splitlines():
        m = re.search(r"K6 MOD say (.*)$", ln)
        ln = (m.group(1) if m else ln).strip()
        if m := re.fullmatch(r"p (\d+) (\d+\.\d{3}) (-?\d+) (-?\d+)", ln):
            out["p"][(int(m.group(1)), round(float(m.group(2)) * 1e6))] = (int(m.group(3)), int(m.group(4)))
        elif m := re.fullmatch(r"r (\d+) ([0-9a-f]{2}) ([0-9a-f]{4})>([0-9a-f]{4}) (ok|refused)", ln):
            out["r"][int(m.group(1))] = f"REG_{m.group(2).upper()} {m.group(3)}>{m.group(4)} {m.group(5)}"
        elif m := re.fullmatch(r"S(\d+) ([0-o]*)", ln):
            ch = [(ord(x) - 48) * 2 - 160 for x in m.group(2)]
            out["S"][int(m.group(1))] = list(zip(ch[0::2], ch[1::2]))
        elif m := re.fullmatch(r"H n=(\d+) c=(\d+) ms=(\d+)", ln):
            out["ms"] = int(m.group(3))
    return out


def table(res, probes, cfgs):
    """sig over its ref per probe and configuration; serial detail (1 dB) where the log has it"""
    head = f"{'MHz':>8} {'what':<16}" + "".join(f"{'c' + str(i):>10}" for i in range(len(cfgs)))
    lines = ["Probe level over the floor 30 kHz above it, dB (sig dBm in brackets); 10 dB or more = heard",
             head]
    for k, f in enumerate(probes):
        row = f"{f / 1e6:>8.3f} {WHAT.get(round(f / 1e6, 2), ''):<16}"
        for c in range(len(cfgs)):
            v = res["p"].get((c, f)) or (res["S"].get(c, [])[k:k + 1] or [None])[0]
            row += f"{'-':>10}" if v is None else f"{v[0] - v[1]:>+4} ({v[0]:>4})"
        lines.append(row)
    lines.append("")
    for i, c in enumerate(cfgs):
        lines.append(f"c{i}: {cfg_name(c)}" + (f"  [{res['r'][i]}]" if i in res["r"] else ""))
    return "\n".join(lines)


# ---- on the bench ----

def cmd_probe(a):
    blk = block_of(a)
    p = unpack(blk)
    run_s = estimate(len(p["probes"]), len(p["cfgs"])) + 15
    code = open(a.bin, "rb").read() if a.bin else __import__("modem.k6mod", fromlist=["build"]).build(SRC)[0]
    print(f"{time.strftime('%H:%M:%S')} hfprobe: {len(p['probes'])} probes x {len(p['cfgs'])} configurations; "
          f"the K6 receives only, about {run_s - 15:.0f} s, then reports on 440.850", flush=True)
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "mod-hfprobe.bin")
        open(path, "wb").write(patch(code, blk))
        log = f"hfprobe-{time.strftime('%Y%m%d-%H%M%S')}.log"
        cmd = [sys.executable, os.path.join(ROOT, "tools", "k6_upload.py"), path, "--key-file", a.key_file,
               "--api-level", "2", "--telemetry", "12", "--run-s", f"{run_s:.0f}", "--rigctld", a.rigctld]
        cmd += ["--dry-run"] if a.dry_run else []
        with open(log, "w") as fh:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=ROOT)
            for ln in proc.stdout:
                sys.stdout.write(ln)
                fh.write(ln)
            proc.wait()
    print(f"log: {log}")
    print(table(decode(open(log).read()), p["probes"], p["cfgs"]))


def cmd_plan(a):
    p = unpack(block_of(a))
    print("probes (MHz): " + ", ".join(f"{f / 1e6:g} ({WHAT.get(round(f / 1e6, 2), '?')})" for f in p["probes"]))
    for i, c in enumerate(p["cfgs"]):
        print(f"c{i}: {cfg_name(c)}")
    print(f"about {estimate(len(p['probes']), len(p['cfgs'])):.0f} s on the K6, plus the upload (~30 s)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("probe", "plan", "patch"):
        s = sub.add_parser(name)
        s.add_argument("--probes", help="MHz, comma separated (default: plan's list)")
        s.add_argument("--cfg", nargs="+", help="PATH[:REG=VALUE] ... (default: " + " ".join(CFGS) + ")")
        s.add_argument("--ref-khz", type=int, default=30)
    pr = sub.choices["probe"]
    pr.add_argument("--bin", help="CI's mod-hfprobe.bin (else built from c/k6/mod/hfprobe.c with clang)")
    pr.add_argument("--key-file", required=True)
    pr.add_argument("--rigctld", default="localhost:4532")
    pr.add_argument("--dry-run", action="store_true")
    pa = sub.choices["patch"]
    pa.add_argument("inp")
    pa.add_argument("out")
    b = sub.add_parser("bandreg")
    b.add_argument("--bin", help="CI's mod-bandreg.bin (else built from c/k6/mod/bandreg.c with clang)")
    b.add_argument("--key-file", required=True)
    b.add_argument("--rigctld", default="localhost:4532")
    b.add_argument("--mhz", help="MHz, comma separated, the first is the baseline (default: 440.85 down to 10)")
    b.add_argument("--dry-run", action="store_true")
    bd = sub.add_parser("banddecode")
    bd.add_argument("logs", nargs="+")
    d = sub.add_parser("decode")
    d.add_argument("logs", nargs="+")
    d.add_argument("--probes")
    d.add_argument("--cfg", nargs="+")
    d.add_argument("--ref-khz", type=int, default=30)
    a = ap.parse_args()
    if a.cmd == "bandreg":
        a.key_file = os.path.abspath(os.path.expanduser(a.key_file))
        cmd_bandreg(a)
    elif a.cmd == "banddecode":
        print(band_table(band_decode("".join(open(x, errors="replace").read() for x in a.logs))))
    elif a.cmd == "probe":
        a.key_file = os.path.abspath(os.path.expanduser(a.key_file))
        cmd_probe(a)
    elif a.cmd == "plan":
        cmd_plan(a)
    elif a.cmd == "patch":
        open(a.out, "wb").write(patch(open(a.inp, "rb").read(), block_of(a)))
    else:
        p = unpack(block_of(a))
        text = "".join(open(x, errors="replace").read() for x in a.logs)
        print(table(decode(text), p["probes"], p["cfgs"]))


if __name__ == "__main__":
    main()
