"""K6 pager test harness (modem/k6_harness.py): parsing the K6's serial report lines, the
bit-error analysis (against the firmware's own sp_test_errors via c/k6/pager_host -e), the
bit-slip estimate, and a sweep against a fake K6 and a fake transmitter.
SPDX-License-Identifier: MIT"""
import os
import subprocess
import sys
import tempfile
import types

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from modem import k6_harness as h  # noqa: E402

HOST = os.path.join(ROOT, "c", "k6", "pager_host")
PK0 = bytes(h.test_packets("L")[0])


def pkt_line(raw, n=1, res=-1, rswap=1, extra=""):
    """A K6 PKT line as the firmware's report_packet writes it."""
    return (f"K6 PKT n={n} got={len(raw)} sync=N res={res} magic=a7 rswap={rswap} soft=0 swap=1 "
            f"{extra}good=0 bad={n} to=0 raw={bytes(raw).hex()}")


def flip(p, bits):
    p = bytearray(p)
    for b in bits:
        p[b // 8] ^= 0x80 >> (b % 8)
    return bytes(p)


def test_parse_line():
    d = h.parse_line(pkt_line(PK0, n=7, extra="be=3 bseq=0 bswap=1 binv=0 blk=1,0,0,0,0,0,0,2 ") + "\r")
    assert d["kind"] == "PKT" and d["n"] == 7 and d["sync"] == "N" and d["res"] == -1
    assert d["raw"] == PK0 and d["blk"] == [1, 0, 0, 0, 0, 0, 0, 2] and d["magic"] == "a7"
    d = h.parse_line("K6 CFG why=enter chip=4819 freq=439500000 phy=F frames=L baud=1200 inv=0 soft=0 swap=1 emph=1 vol=0")
    assert d["phy"] == "F" and d["frames"] == "L" and d["baud"] == 1200 and d["chip"] == "4819"
    assert h.parse_line("K6 CAL sent tx=439500000")["words"] == ["sent"]
    assert h.parse_line("bfk6-fw boot") is None


def test_events():
    assert h.events("2:0:0,1000:27:4,1000:53:4,2000:107:24") == "S@0 F@27+4 F@53+4 D@107+24"
    assert h.events("3002:0:8,") == "SFD@0+8" and h.events("") == ""
    assert h.events("2:0:0,1000:300:22,0:330:10") == "S@0 F@300+22 T@330+10"
    d = h.parse_line(pkt_line(PK0, extra="ev=2:0:0,2000:107:32 "))
    assert "[S@0 D@107+32]" in h.describe(d, h.analyse(d["raw"], 1))


def test_analyse_finds_reading_and_errors():
    a = h.analyse(PK0, rswap=1)
    assert a["errors"] == 0 and a["seq"] == 0 and a["swap"] == 1 and a["inv"] == 0 and a["first"] == -1
    p = h.test_packets("S")
    sw = bytes(np.frombuffer(flip(bytes(p[9]), [5, 100]), np.uint8).reshape(-1, 2)[:, ::-1].reshape(-1) ^ 0xFF)
    a = h.analyse(sw, rswap=1)
    assert (a["errors"], a["seq"], a["swap"], a["inv"], a["first"]) == (2, 9, 0, 1, 5)
    assert a["blocks"] == [1, 0, 0, 0, 0, 0, 1, 0] and sum(a["per_byte"]) == 2
    assert h.error_map(a["per_byte"]).count(".") == 14


@pytest.mark.skipif(not os.path.exists(HOST), reason="needs make -C c/k6")
@pytest.mark.parametrize("prof", ["L", "S"])
def test_analyse_matches_firmware(prof):
    rng = np.random.default_rng(3)
    ref = h.test_packets(prof)
    pk = []
    for i in range(30):
        p = bytes(ref[rng.integers(len(ref))])
        p = flip(p, rng.choice(8 * len(p), rng.integers(0, 12), replace=False))
        if i % 3 == 1:
            p = bytes(np.frombuffer(p, np.uint8).reshape(-1, 2)[:, ::-1].reshape(-1))
        if i % 4 == 2:
            p = bytes(255 - x for x in p)
        pk.append(p)
    pk.append(bytes(rng.integers(0, 256, len(ref[0]), dtype=np.uint8)))    # noise
    with tempfile.TemporaryDirectory() as d:
        open(f"{d}/p.bin", "wb").write(b"".join(pk))
        out = subprocess.run([HOST, "-e"] + (["-s"] if prof == "S" else []) + [f"{d}/p.bin"],
                             check=True, capture_output=True, text=True).stdout.splitlines()
    for p, ln in zip(pk, out):
        c = [int(x) for x in ln.split()]
        a = h.analyse(p, rswap=0)
        assert a["errors"] == c[0]
        assert a["blocks"] == c[4:] or a["errors"] > 20      # ties only in noise


@pytest.mark.parametrize("shift", [-1, 1])
def test_slip_estimate(shift):
    bits = np.unpackbits(np.frombuffer(PK0, np.uint8))
    at = 200
    if shift < 0:      # a lost bit
        b = np.concatenate([bits[:at], bits[at + 1:], [0]])
    else:              # an extra bit
        b = np.concatenate([bits[:at], [1 - bits[at]], bits[at:-1]])
    a = h.analyse(np.packbits(b).tobytes(), rswap=1)
    assert a["errors"] > 60 and a["tail_shift"] == shift and sum(a["per_byte"][:20]) <= 8
    assert h.analyse(flip(PK0, [450, 480]), 1)["tail_shift"] == 0


def test_listen_counts():
    lines = [(0.0, "K6 CFG why=enter chip=4819 freq=439500000 phy=F frames=L baud=1200 inv=0 soft=0 swap=1 emph=1 vol=0"),
             (1.0, pkt_line(PK0, n=1, res=2)),
             (2.0, pkt_line(flip(PK0, [3, 500]), n=2)),
             (3.0, pkt_line(PK0, n=5, res=0)),
             (4.0, "K6 PKT n=6 got=20 sync=n res=short rswap=1 soft=0 swap=1 good=2 bad=1 to=1 raw=" + PK0[:20].hex())]
    out = []
    with tempfile.TemporaryDirectory() as d:
        st = h.listen(iter(lines), f"{d}/log.txt", out.append)
        again = h.listen(h.file_lines(f"{d}/log.txt"), None, lambda s: None)
    assert (st.pk, st.ok, st.short, st.be, st.lost_lines) == (4, 2, 1, [0, 2, 0], 2)
    assert again.be == st.be
    assert "BE   2/512" in out[2] and "pkt  0" in out[2]
    assert "3 report lines lost" not in out[-2] and "2 report lines lost" in out[-2]


class FakeK6:
    """Answers each send with PKT lines, like the K6 would (here: errors grow with the offset)."""

    def __init__(self):
        self.q = [(0.0, "K6 CFG why=key1 chip=4819 freq=439500000 phy=F frames=L baud=1200 inv=0 soft=0 swap=1 emph=1 vol=0")]
        self.n = 0

    def send(self, x, b, out):
        bad = int(round(abs(len(x) / self.len0 - 1) * 1000)) if hasattr(self, "len0") else 0
        self.len0 = getattr(self, "len0", len(x))
        for _ in range(b.repeat):
            self.n += 1
            self.q.append((0.0, pkt_line(flip(PK0, range(0, 8 * bad, 8)), n=self.n, res=-1 if bad else 0)))

    def get(self, timeout=None):
        return self.q.pop(0) if self.q else None

    def drain(self):
        out, self.q = self.q, []
        return out


def test_sweep_with_fake_k6():
    from modem import k6
    import argparse
    ap = argparse.ArgumentParser()
    k6._tx_opts(ap)
    a = ap.parse_args(["--phy", "F"])
    a.speeds, a.levels, a.repeat, a.message, a.settle, a.gap = "0,1", None, 3, False, 0.01, 0.0
    fake = FakeK6()
    out = []
    rows = h.sweep(a, fake, fake.send, out.append)
    assert [r["packets"] for r in rows] == [3, 3] and rows[0]["ok"] == 3 and rows[0]["be_mean"] == 0
    assert rows[1]["ok"] == 0 and rows[1]["be_mean"] == 10       # 1 % shorter -> 10 bytes hit
    assert not any("warning" in s for s in out)
    a.payload = "S"
    out = []
    h.check_cfg({"phy": "F", "frames": "L", "baud": 1200}, a, out.append)
    assert "frames K6 L vs sweep S" in out[0]


def test_speed_shift():
    x = np.zeros(48000)
    assert len(h.speed_shift(x, 48000, 1.0)) == pytest.approx(48000 / 1.01, abs=2)
    assert h.speed_shift(x, 48000, 0) is x


CFG_B = ("K6 CFG why=key1 chip=4819 freq=439500000 phy=F frames=L baud=1200 inv=0 soft=0 swap=1 emph=1 vol=0 "
         "len5d=3f00 keep=0 auto=1 build=1234567-20261104T0610Z")


def test_build_reported_and_logged():
    lines = [(0.0, "K6 PKT n=1 got=20 sync=n res=short rswap=1 soft=0 swap=1 good=0 bad=0 to=0 raw=" + PK0[:20].hex()),
             (0.5, "K6 BOOT build=1234567-20261104T0610Z"),
             (1.0, CFG_B),
             (2.0, pkt_line(PK0, n=2, res=0))]
    out = []
    with tempfile.TemporaryDirectory() as d:
        h.listen(iter(lines), f"{d}/log.txt", out.append)
        logged = open(f"{d}/log.txt").read().splitlines()
        again = list(h.file_lines(f"{d}/log.txt"))
    assert h.parse_line(lines[1][1])["build"] == "1234567-20261104T0610Z"     # digits stay a string
    assert out.count("K6 firmware build 1234567-20261104T0610Z") == 1
    assert logged[0].split("\t")[1] == "?" and all(ln.split("\t")[1].startswith("1234567") for ln in logged[1:])
    assert [ln for _, ln in again] == [ln for _, ln in lines]
    out = []
    h.listen(iter(lines[3:]), None, out.append)
    assert "no firmware build reported" in out[-1]


def test_sweep_expect_build():
    from modem import k6
    import argparse
    ap = argparse.ArgumentParser()
    k6._tx_opts(ap)
    a = ap.parse_args(["--phy", "F"])
    a.speeds, a.levels, a.repeat, a.message, a.settle, a.gap = "0", None, 2, False, 0.01, 0.0
    a.build_wait = 0.2
    for want, cfg, sends in (("1234567", CFG_B, 1), ("72b6e3b", CFG_B, 0), ("1234567", FakeK6().q[0][1], 0)):
        fake = FakeK6()
        fake.q = [(0.0, cfg)]
        n = []
        a.expect_build = want
        send = lambda x, b, out: (n.append(1), fake.send(x, b, out))
        if sends:
            rows = h.sweep(a, fake, send, lambda s: None)
            assert rows[0]["ok"] == 2
        else:
            with pytest.raises(h.WrongBuild):
                h.sweep(a, fake, send, lambda s: None)
        assert len(n) == sends


def test_sweep_refuses_other_settings():
    from modem import k6
    import argparse
    ap = argparse.ArgumentParser()
    k6._tx_opts(ap)
    a = ap.parse_args(["--phy", "F"])
    a.speeds, a.levels, a.repeat, a.message, a.settle, a.gap = "0", None, 1, False, 0.01, 0.0
    a.anyway = False
    fake = FakeK6()
    fake.q = [(0.0, fake.q[0][1].replace("phy=F", "phy=D"))]
    n = []
    with pytest.raises(h.WrongBuild):
        h.sweep(a, fake, lambda x, b, out: n.append(1), lambda s: None)
    assert not n
