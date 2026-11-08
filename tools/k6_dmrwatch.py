"""DMR answer key: decode receive recordings (the IC-7100's FM audio of 440.925) into one JSON
line a call or data packet, so the K6's own DMR decoder can be scored against them later.

  python tools/k6_dmrwatch.py ~/sotto-work/runs/*/*.wav -o ~/sotto-work/runs/dmr-key.jsonl
  python tools/k6_dmrwatch.py --watch ~/sotto-work/runs/cwbench-20261108 -o key.jsonl

Files already in the output are skipped, so it can be rerun (or left in --watch) as the
recorder adds cuts. Each line: file, its start time (UTC, from the name's 20261108T042456Z or
_060718Z, else the file's mtime less its length), the event's
offset and absolute time, the slot, and either the call's LC (vote of clean copies, the
soft-combined headers, the talker alias) or the data packet's header and payload in hex.
Receive-only: nothing here keys a radio. SPDX-License-Identifier: MIT"""
import argparse
import datetime as dt
import glob
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from sim.k6if import dmrdec as dd  # noqa: E402

EXT = (".wav", ".flac")


def utc(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z"


def start_time(p, dur):
    """the recording's start (Unix time): from a name with 20261108T042456Z, or with _060718Z (the
    date from the file's mtime), else the mtime less the length"""
    import re
    name, mt = os.path.basename(p), os.path.getmtime(p)
    m = re.search(r"(\d{8})T(\d{6})Z", name)
    if m:
        return dt.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(
            tzinfo=dt.timezone.utc).timestamp()
    m = re.search(r"_(\d{6})Z", name)
    if m:
        day = dt.datetime.fromtimestamp(mt - dur, dt.timezone.utc).strftime("%Y%m%d")
        return dt.datetime.strptime(day + m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=dt.timezone.utc).timestamp()
    return mt - dur


def decode_file(p):
    x, fs = dd._load(p)
    t0 = start_time(p, len(x) / fs)
    pol, ev = dd.decode_stream(dd.audio_symbols(x, fs))
    base = {"file": os.path.basename(p), "start": utc(t0)}
    out = []
    for c in dd.calls(ev):
        cb = c["combined"]
        if c["lc"] is None and not (cb and cb["rs_ok"]):
            continue                            # a lone voice sync at a clip's edge
        out.append(dict(base, kind="call", t=round(c["t0"], 3), at=utc(t0 + c["t0"]),
                        dur=round(c["t1"] - c["t0"], 3), slot=c["slot"],
                        lc=dict(zip(("flco", "fid", "dst", "src"), c["lc"])) if c["lc"] else None,
                        copies=sum(c["copies"].values()), alias=c["alias"] or None,
                        combined={"dst": cb["dst"], "src": cb["src"], "ok": cb["rs_ok"]} if cb else None))
    for pk in dd.packets(ev):
        out.append(dict(base, kind="data", t=round(pk["t"], 3), at=utc(t0 + pk["t"]), slot=pk["slot"],
                        hdr={k: v for k, v in pk["hdr"].items()}, crc=pk["crc"],
                        payload=pk["payload"].hex()))
    if not out:                                 # nothing decoded: one line to say so (t 0, the start)
        return [dict(base, kind="none", t=0.0, at=base["start"], bursts=len(ev), polarity=pol)]
    return sorted(out, key=lambda e: e["t"])


def files_of(args):
    fs = []
    for a in args:
        if os.path.isdir(a):
            fs += [os.path.join(a, f) for f in sorted(os.listdir(a)) if f.lower().endswith(EXT)]
        else:
            fs += sorted(glob.glob(os.path.expanduser(a)))
    return fs


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("paths", nargs="+", help="recordings, globs or directories")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--watch", action="store_true", help="keep checking the paths every 30 s")
    a = ap.parse_args()
    done = set()
    if os.path.exists(a.out):
        with open(a.out) as f:
            done = {json.loads(line)["file"] for line in f if line.strip()}
    while True:
        for p in files_of(a.paths):
            name = os.path.basename(p)
            if name in done or time.time() - os.path.getmtime(p) < 5:
                continue
            try:
                lines = decode_file(p)
            except Exception as e:              # a half-written or odd file: note it and go on
                lines = [{"file": name, "kind": "error", "error": repr(e)}]
            with open(a.out, "a") as f:
                for e in lines:
                    f.write(json.dumps(e) + "\n")
            done.add(name)
            for e in lines:
                if e["kind"] == "call":
                    lc = e["lc"] or {}
                    print(f"{e['at']} {name} TS{e['slot']} TG {lc.get('dst')} src {lc.get('src')}"
                          f"{' alias ' + e['alias'] if e['alias'] else ''}", flush=True)
        if not a.watch:
            break
        time.sleep(30)


if __name__ == "__main__":
    main()
