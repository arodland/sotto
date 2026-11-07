"""Regression tests on real on-air data from Jeff's K6 (tests/data/k6/air/).

cwlisten-20261106T0529.log.gz is the K6's serial log of the three cwlisten runs on 440.850 MHz
(2026-11-06 05:30-05:35 UTC, image 6473ca7, docs/k6-cwid.md "On air"): for every transmission
the K6 heard, the marks and spaces the tone detector (REG_0B bit 12) gave, in 4 ms units, with
what the module made of them. The replay turns those marks and spaces back into a REG_0B
timeline and runs today's cwlisten on an emulated M0 against it: every ID must decode to the
same text, speed, lag, tone and marks as on the air, the callsign must confirm on the second
copy as it did, and the upload-tail bursts on tone 15 must only be counted (short=).

cwprobe-20261106T0506.log.gz is the probe run before it; its event log has REG_0B's raw edges
for the start of the 13 wpm ID (A, then the first dashes of G), which pin down the detector's
lag model that tests/test_k6_cwid.py uses (marks 10 ms long, spaces 10 ms short).

k6cal-2400-20261104T0928.flac is the IC-7100's recording (FM-D, USB audio, 48 kHz) of the K6's
own CAL burst at 2400 baud, 2026-11-04 09:28 UTC (image f190a46): two D/2400 then two F/2400
test packets, sent by the FD6818 with its FIFO low byte first. modem.k6 rx must decode all four
as spec test packet 0.

None of these files hold firmware: serial text and audio only.
Needs a C compiler, clang, ld.lld and unicorn for the module replay; numpy and soundfile for
the recording.
SPDX-License-Identifier: MIT"""
import gzip
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_k6_cwid import LISTEN, ROOT, need_clang, run_m0, tone_reg  # noqa: E402

sys.path.insert(0, ROOT)
from modem import k6mod  # noqa: E402

AIR = os.path.join(ROOT, "tests", "data", "k6", "air")
CWLISTEN_LOG = os.path.join(AIR, "cwlisten-20261106T0529.log.gz")
CWPROBE_LOG = os.path.join(AIR, "cwprobe-20261106T0506.log.gz")
CAL2400 = os.path.join(AIR, "k6cal-2400-20261104T0928.flac")
CW_END_MS = 1200           # cwid.h: key up this long ends a transmission
TONE_LO, TONE_STEP = 600, 50


def log_lines(path):
    """[(host time s, text after the build column)] of a hamlet K6 serial log."""
    out = []
    with gzip.open(path, "rt") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 3:
                out.append((float(parts[0]), parts[2]))
    return out


def cwlisten_runs(path=CWLISTEN_LOG):
    """The runs in a cwlisten log: [{"t0", "said": [...], "tx": [{"t", "head", "lines", "d"}],
    "ret"}], where said is every CWID line as the K6 said it (without "K6 MOD say ") and d a
    transmission's marks (+) and spaces (-) in ms."""
    runs = []
    for t, s in log_lines(path):
        if s.startswith("K6 MOD say CWID listen "):
            runs.append({"t0": t, "said": [], "tx": [], "ret": None})
        if not runs:
            continue
        r = runs[-1]
        if s.startswith("K6 MOD ret="):
            r["ret"] = int(s.split("=")[1].split()[0])
        if not s.startswith("K6 MOD say CWID "):
            continue
        line = s[len("K6 MOD say "):]
        r["said"].append(line)
        if re.match(r"CWID \d+wpm ", line):
            r["tx"].append({"t": t - r["t0"], "head": line, "lines": [line], "d": []})
        elif r["tx"] and not line.startswith(("CWID listen", "CWID end")):
            tx = r["tx"][-1]
            tx["lines"].append(line)
            if line.startswith("CWID d "):
                tx["d"] += [int(v) for v in line.split()[2:]]
    return runs


def tone_of(head):
    return (int(head.split(" tone=")[1].split()[0]) - TONE_LO) // TONE_STEP


def timeline(txs, lead=1000.0):
    """[(ms, key, tone)] edges that give each transmission's marks and spaces back, with every
    transmission ending where the log says the K6 reported it (CW_END_MS before), pushed later
    when that would overlap the one before."""
    edges, free = [], lead
    for tx in txs:
        dur = sum(abs(v) for v in tx["d"])
        start = max(free, tx["t"] * 1000.0 - CW_END_MS - 150.0 - dur)
        t, idx = start, tone_of(tx["head"])
        for v in tx["d"]:
            edges.append((t, 1 if v > 0 else 0, idx))
            t += abs(v)
        edges.append((t, 0, idx))
        free = t + CW_END_MS + 400.0
    return edges, free


def reg0b(edges):
    """REG_0B from edges: 0xD006 | tone << 8 while keyed, 0xC006 | tone << 8 while not."""
    import bisect
    ts = [e[0] for e in edges]

    def f(reg, ms):
        if reg != 0x0B:
            return 0x0200 if reg == 0x0C else 0
        i = bisect.bisect_right(ts, ms) - 1
        if i < 0:
            return 0xC006
        _, k, idx = edges[i]
        return (0xD006 if k else 0xC006) | (idx << 8)
    return f


def is_id(tx):
    return any(s.startswith("CWID call=") for s in tx["lines"])


def test_logs_hold_no_firmware():
    """Only serial text: no K6 BOOT dump lines, no image bytes beyond packet payloads."""
    for path in (CWLISTEN_LOG, CWPROBE_LOG):
        for _, s in log_lines(path):
            assert not s.startswith("K6 BOOT"), s
            assert s.startswith("K6 "), s


def test_cwlisten_log_as_documented():
    """The log is what docs/k6-cwid.md reports: three runs, each confirming AG7EW on the second
    copy at 20/800, 13/600, 25/1200, after 7, 3 and 6 upload-tail bursts on tone 15."""
    runs = cwlisten_runs()
    assert [r["ret"] for r in runs] == [1, 1, 1]
    want = [(20, 800, 7), (13, 600, 3), (25, 1200, 6)]
    for r, (wpm, tone, junk) in zip(runs, want):
        ids = [tx for tx in r["tx"] if is_id(tx)]
        assert len(ids) == 2 and len(r["tx"]) == 2 + junk
        for tx in ids:
            assert tx["head"].startswith(f"CWID {wpm}wpm ") and f" tone={tone} q=100 n=61" in tx["head"]
            assert "CWID text AG7EW SOTTO TEST" in tx["lines"] and len(tx["d"]) == 61
        assert f"CWID ok AG7EW fl=0 rx=440850000 tone={tone} wpm={wpm}" in ids[1]["lines"]
        assert all(tone_of(tx["head"]) == 15 and len(tx["d"]) < 21 for tx in r["tx"] if not is_id(tx))


@need_clang
@pytest.mark.parametrize("run", [0, 1, 2])
def test_cwlisten_replays_air(run):
    """Today's cwlisten on the K6's own REG_0B timeline from the run: the IDs come out as on
    the air (head line, text, call, ok, every mark and space to the 4 ms unit), the bursts
    are only counted, and it stops on the confirm with ret 1."""
    r = cwlisten_runs()[run]
    edges, end = timeline(r["tx"])
    code, entry = k6mod.build(LISTEN)
    ret, said, writes, stack, _ = run_m0(code, entry, reg0b(edges), end + 30_000, 0)
    assert said[0] == "CWID listen rx=440850000 th=24 s=1500"
    ids = [tx for tx in r["tx"] if is_id(tx)]
    junk = len(r["tx"]) - len(ids)
    assert ret == 1 and said[-1] == f"CWID end ok msgs=2 short={junk}", said[-3:]
    heads = [s for s in said if re.match(r"CWID \d+wpm ", s)]
    assert heads == [tx["head"] for tx in ids]
    for want in ids:
        for s in want["lines"]:
            if not s.startswith("CWID d "):
                assert s in said, s
    got_d = []
    for s in said:
        if re.match(r"CWID \d+wpm ", s):
            got_d.append([])
        elif s.startswith("CWID d "):
            got_d[-1] += [int(v) for v in s.split()[2:]]
    for want, got in zip(ids, got_d):
        assert got == want["d"], want["head"]       # exactly the 4 ms units the K6 reported
    assert all(len(s) <= 60 for s in said)
    print(f"cwlisten run {run + 1}: {len(said)} lines, stack {stack} B")


def probe_events(path=CWPROBE_LOG):
    """REG_0B changes from the probe's event log ("T ms b=XXXX"), per listening window."""
    wins, last = [], None
    for t, s in log_lines(path):
        m = re.match(r"K6 MOD say T (\d+) b=([0-9a-f]{4})$", s)
        if not m:
            continue
        if last is None or t - last > 60:
            wins.append([])
        last = t
        wins[-1].append((int(m.group(1)), int(m.group(2), 16)))
    return wins


def test_probe_edges_match_lag_model():
    """The probe's raw REG_0B edges for the 13 wpm ID at 600 Hz (dot 92 ms, dash 277): A is
    `.-`, then the two dashes of G (`--.`; the log lost the rest). Marks come out 6-12 ms long and spaces
    as much short, which tests/test_k6_cwid.py's tone_reg model must reproduce."""
    wins = probe_events()
    ev = wins[-1]
    assert all(b & 0x0F00 == 0 for _, b in ev)                    # tone 0, 600 Hz
    keyed = [(t, (b >> 12) & 1) for t, b in ev]
    assert [k for _, k in keyed] == [1, 0, 1, 0, 1, 0, 1, 0]
    d = [b[0] - a[0] for a, b in zip(keyed, keyed[1:])]
    sent = [92, 92, 277, 277, 277, 92, 277]                        # A, letter gap, G's - . -
    marks = [x - s for x, s, (_, k) in zip(d, sent, keyed) if k]
    spaces = [x - s for x, s, (_, k) in zip(d, sent, keyed) if not k]
    assert all(6 <= m <= 12 for m in marks), marks
    assert all(-12 <= s <= -6 for s in spaces), spaces
    # tone_reg's default (on 10 ms after the key goes down, off 20 ms after it goes up) gives
    # the same: marks 10 ms long, spaces 10 ms short
    edges, t = [], 0.0
    for i, s in enumerate(sent + [92]):
        edges.append((t, 1 - i % 2))
        t += s
    f = tone_reg(edges, 0)
    prev, flips = None, []
    for ms in range(0, int(t) + 40):
        k = (f(0x0B, ms) >> 12) & 1
        if k != prev:
            flips.append(ms)
            prev = k
    md = [b - a for a, b in zip(flips[1:], flips[2:])]
    assert [x - s for x, s in zip(md, sent)] == [10, -10] * 3 + [10]
    assert all(abs(x - m) <= 4 for x, m in zip(md, d)), (md, d)


# ---- the K6's own CAL burst, as the IC-7100 heard it ----

def cal_audio():
    sf = pytest.importorskip("soundfile")
    if not os.path.exists(CAL2400):
        pytest.skip("no CAL recording in tests/data/k6/air")
    x, fs = sf.read(CAL2400, dtype="float64")
    return (x if x.ndim == 1 else x[:, 0]), fs


@pytest.mark.parametrize("phy", ["D", "F"])
def test_k6_cal_burst_2400_decodes(phy):
    """modem.k6 rx on the K6's own CAL burst: both packets of each PHY at 2400 baud decode as
    spec test packet 0 (msg_id 0x42, frames 0-7), with their 16-bit words byte-swapped (the
    FD6818 sends its FIFO low byte first); D arrives inverted, as on the bench (REG_59 bit 10)."""
    from modem import fsk, pager
    import types
    from modem import k6
    x, fs = cal_audio()
    asm, times = fsk.receive(x, fs, phy, 2400.0, pager.L)
    good = [t for t in times if t[1]]
    assert len(good) == 2, times
    assert all(t[2] == (phy == "D") for t in good)
    assert asm.bad == 0 and asm.swapped == 0
    assert list(asm.messages) == [0x42]
    ref = pager.L.unpack(k6._test_payloads(types.SimpleNamespace(payload="L", message=False, repeat=1,
                                                                  codec="2400"))[0])
    assert asm.messages[0x42]["pk"] == {ref["seq"]: ref["frames"]}
    # the two packets 410 ms apart, D before F
    starts = [t[0] for t in good]
    assert 0.39 < starts[1] - starts[0] < 0.43
