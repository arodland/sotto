"""Score the K6's DMR decode (dmrrx's serial lines) against the IC-7100's answer key
(tools/k6_dmrwatch.py's JSONL) from the same stretch of time.

  python tools/k6_dmrscore.py --key ~/sotto-work/runs/dmr-key.jsonl --log k6-serial.log
  python tools/k6_dmrscore.py --key key.jsonl --log k6.log --start 2026-11-09T03:12:40Z

dmrrx ends every line in " @" and its time since it started, in tenths of a second. The log
may carry anything before the K6's own text on each line (a host timestamp, a prompt): only
"L ...", "E ...", "A ...", "K ...", "H ..." lines with an @ time count. The module's start in
UTC comes from --start, else from the offset at which the most K6 talkgroup/source lines land
on answer-key calls with the same talkgroup and source (1 s bins; it needs 2 to agree).

Scored, with the window a call's span plus --slack seconds each side:
  calls     the key's voice calls with a known talkgroup and source: how many the K6 named
            (an L or E line in the window), how many it named right (talkgroup and source,
            and the slot where both give one)
  wrong     K6 L/E lines inside a call's window that disagree with it
  stray     K6 L/E lines outside every call's window
  data      the key's data headers: how many have a K6 H line in the window with the same
            destination and source
  alias     the key's talker aliases the K6 printed in full
Prints counts and the misses, never a data packet's payload (it may hold someone's position).
Receive-only bookkeeping: nothing here touches a radio. SPDX-License-Identifier: MIT"""
import argparse
import collections
import datetime as dt
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sim.k6if import dmrdec as dd  # noqa: E402

LINE = re.compile(r"(?:^|\s)([LEAKH]) (.*?) @(\d+)\s*$")
LC = re.compile(r"(?:TS(\d) )?(?:CC\d+ )?TG (\d+) src (\d+)")


def ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def iso(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-5] + "Z"


def read_log(path):
    """-> [(seconds since the module started, kind, text)]"""
    out = []
    with open(path, errors="replace") as f:
        for ln in f:
            m = LINE.search(ln.rstrip("\r\n"))
            if m:
                out.append((int(m.group(3)) / 10.0, m.group(1), m.group(2)))
    return out


def read_key(path):
    calls, data = [], []
    with open(path) as f:
        for ln in f:
            if not ln.strip():
                continue
            e = json.loads(ln)
            if e.get("kind") == "call":
                lc = e.get("lc") or {}
                cb = e.get("combined") or {}
                tg, src = lc.get("dst"), lc.get("src")
                if tg is None and cb.get("ok"):
                    tg, src = cb.get("dst"), cb.get("src")
                calls.append({"t": ts(e["at"]), "dur": e.get("dur", 0.0), "slot": e.get("slot"),
                              "tg": tg, "src": src, "alias": e.get("alias"), "file": e["file"]})
            elif e.get("kind") == "data":
                h = e.get("hdr") or {}
                data.append({"t": ts(e["at"]), "slot": e.get("slot"), "dst": h.get("dst"),
                             "src": h.get("src"), "file": e["file"]})
    return calls, data


def k6_lcs(lines):
    """L and E lines -> [(t, kind, slot or None, tg, src)]"""
    out = []
    for t, k, txt in lines:
        if k in "LE":
            m = LC.search(txt)
            if m:
                out.append((t, k, int(m.group(1)) if m.group(1) else None, int(m.group(2)), int(m.group(3))))
    return out


def find_offset(lcs, calls):
    """the UTC of the module's start at which the most K6 LCs land on key calls that say the
    same: the mode of (call start - line time) over matching pairs, 1 s bins, the mean of the
    best bin's neighbourhood. None if fewer than 2 agree"""
    d = [c["t"] - t for t, _, _, tg, src in lcs for c in calls if c["tg"] == tg and c["src"] == src]
    if not d:
        return None
    bins = collections.Counter(round(x) for x in d)
    b, n = max(bins.items(), key=lambda kv: (kv[1] + bins.get(kv[0] - 1, 0) + bins.get(kv[0] + 1, 0), kv[1]))
    near = [x for x in d if abs(x - b) <= 1.5]
    if len(near) < 2:
        return None
    # a line comes after its call's start (a header's ~0.1-0.5 s, an embedded LC's up to the
    # call's length), so call start - line time runs under the true start: take the upper
    # quarter of the near ones
    return sorted(near)[-(len(near) // 4) - 1]


def score(lines, calls, data, start, slack):
    lcs = k6_lcs(lines)
    rep = {"calls": 0, "named": 0, "right": 0, "wrong": 0, "stray": 0, "data": len(data), "data_hit": 0,
           "alias": 0, "alias_hit": 0, "miss": [], "bad": []}
    used = set()
    for c in calls:
        if c["tg"] is None:
            continue
        rep["calls"] += 1
        lo, hi = c["t"] - slack, c["t"] + c["dur"] + slack
        mine = [(i, x) for i, x in enumerate(lcs) if lo <= start + x[0] <= hi]
        used.update(i for i, _ in mine)
        if not mine:
            rep["miss"].append(f"{iso(c['t'])} TS{c['slot']} TG {c['tg']} src {c['src']} ({c['file']})")
            continue
        rep["named"] += 1
        ok = [x for _, x in mine if x[3] == c["tg"] and x[4] == c["src"] and (x[2] is None or c["slot"] is None or x[2] == c["slot"])]
        rep["right"] += bool(ok)
        for _, x in mine:
            if x[3] != c["tg"] or x[4] != c["src"]:
                rep["wrong"] += 1
                rep["bad"].append(f"{iso(start + x[0])} K6 {x[1]} TG {x[3]} src {x[4]}: key TG {c['tg']} src {c['src']}")
        if c["alias"]:
            rep["alias"] += 1
            al = [txt for t, k, txt in lines if k == "A" and lo <= start + t <= hi]
            rep["alias_hit"] += any(a.strip() == c["alias"].strip() for a in al)
    for i, x in enumerate(lcs):
        if i not in used:
            rep["stray"] += 1
            rep["bad"].append(f"{iso(start + x[0])} K6 {x[1]} TG {x[3]} src {x[4]}: no call in the key")
    hs = []
    for t, k, txt in lines:
        if k == "H":
            m = re.search(r"([0-9a-f]{20})", txt)
            if m:
                f = dd.block_fields(list(bytes.fromhex(m.group(1))) + [0, 0], 6)
                hs.append((t, f["dst"], f["src"]))
    for p in data:
        lo, hi = p["t"] - slack, p["t"] + slack
        rep["data_hit"] += any(lo <= start + t <= hi and dst == p["dst"] and src == p["src"] for t, dst, src in hs)
    return rep


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--key", required=True, help="tools/k6_dmrwatch.py's JSONL")
    ap.add_argument("--log", required=True, help="the K6's serial lines (dmrrx)")
    ap.add_argument("--start", help="the module's start, UTC (2026-11-09T03:12:40Z); default: found")
    ap.add_argument("--slack", type=float, default=1.5, help="seconds either side of a call (1.5)")
    ap.add_argument("--json", action="store_true", help="the report as JSON")
    a = ap.parse_args()
    lines = read_log(a.log)
    calls, data = read_key(a.key)
    if a.start:
        start = ts(a.start)
    else:
        start = find_offset(k6_lcs(lines), calls)
        if start is None:
            sys.exit("no offset: fewer than 2 K6 talkgroup lines match the key's calls; give --start")
    if lines:
        t0, t1 = start + lines[0][0], start + lines[-1][0]
        calls = [c for c in calls if t0 - 60 <= c["t"] <= t1 + 60]
        data = [p for p in data if t0 - 60 <= p["t"] <= t1 + 60]
    rep = score(lines, calls, data, start, a.slack)
    rep["start"] = iso(start)
    if a.json:
        print(json.dumps(rep, indent=1))
        return
    print(f"module start {rep['start']}; {len(lines)} K6 lines; key: {rep['calls']} calls, {rep['data']} data headers")
    print(f"calls named {rep['named']}/{rep['calls']}, right {rep['right']}; wrong lines {rep['wrong']}, "
          f"stray {rep['stray']}; data headers {rep['data_hit']}/{rep['data']}; alias {rep['alias_hit']}/{rep['alias']}")
    for m in rep["miss"]:
        print("  missed", m)
    for b in rep["bad"]:
        print("  ", b)


if __name__ == "__main__":
    main()
