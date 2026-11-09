"""AIS on the K6's GP0 receive path: a model of the bursts, a receiver the MCU can run, and the
time the messages carry (docs/k6-ais.md).

AIS (ITU-R M.1371-5) sends 9600 bit/s GMSK (BT 0.4, modulation index 0.5: +-2400 Hz) on
161.975 MHz (channel 87B, "AIS 1") and 162.025 MHz (88B, "AIS 2"), in self-organised TDMA:
a UTC minute is 2250 slots of 26.67 ms, and slot 0 starts on the minute. A burst is

  8 bits ramp-up, 24 bits training (0101...), flag 0x7E, data (168 bits for most messages,
  bytes sent least significant bit first, so each byte of the message goes out reversed),
  FCS (CRC-16/X.25), flag, ramp-down; data and FCS bit-stuffed (a 0 after five 1s), the whole
  burst NRZI (a 0 changes the level).

What carries time:
  - message 4 (base station report) and 11 (UTC/date response): year, month, day, hour, minute,
    second. Shore base stations send 4 every 10 s.
  - messages 1, 2, 3 (class A position) and 18 (class B): the UTC second the report was made.
  - the SOTDMA communication state of 1, 2, 4, 11 and 18 (sync state, slot time-out, a sub-message
    chosen by the time-out): time-out 1 carries the UTC hour and minute, 2, 4 and 6 the slot
    number this burst was sent in. With the station on UTC (sync state 0), the slot number is
    the burst's time in the minute to the bit: its flag starts 32 bits (3.33 ms) after the slot.

The receiver, as the MCU would run it:
  counts (2 * ones - 64, ~85.8 kHz) -> mixed down by the nominal IF (16.927 kHz) -> a 2nd-order
  CIC decimating by DEC -> the phase step between samples (CORDIC on the MCU) is the FM
  discriminator, its mean over the burst taken off (the carrier's offset) -> the phase advance
  over each bit gives the bit, at 8 clock phases (and the DMA's rate +-0.15%) -> NRZI -> HDLC
  (flags, unstuffing) -> CRC. A burst is 27 ms, short enough to record whole and decode after.

  python3 -m sim.k6if.ais pd        decodes vs C/N0 through the GP0 model
  python3 -m sim.k6if.ais cap LOG...  the receiver on the K6's own IF captures (aiscap's lines)
  python3 -m sim.k6if.ais nmea 'LINE'...   decode !AIVDM sentences (to check the parser)

MIT licence."""
import argparse
import os

import numpy as np

from . import frontend as fe
from . import study as st

BAUD = 9600
DEV = 2400.0
BT = 0.4
SLOT_S = 60.0 / 2250
FLAG_OFF_BITS = 32                  # ramp-up + training ahead of the start flag
CH = {"A": 161975000, "B": 162025000}
FC = fe.FDMA / fe.L                 # counts a second (~85.8 kHz)
DEC = 3                             # counts a baseband sample (~28.6 kHz, ~3 a bit)


# ---- bits ----

def crc16_x25(bits):
    """CRC-16/X.25 over bits in the order sent: (the register, ~register as the FCS bits LSB first)"""
    r = 0xFFFF
    for b in bits:
        x = (r ^ b) & 1
        r >>= 1
        if x:
            r ^= 0x8408
    return r


def fcs_bits(bits):
    c = crc16_x25(bits) ^ 0xFFFF
    return [(c >> i) & 1 for i in range(16)]


def crc_ok(bits):
    return crc16_x25(bits) == 0xF0B8


def to_air(msg):
    """message bits (fields MSB first) -> the order sent (each byte reversed)"""
    assert len(msg) % 8 == 0
    return [msg[8 * k + 7 - i] for k in range(len(msg) // 8) for i in range(8)]


def from_air(bits):
    return to_air(bits)


def stuff(bits):
    out, run = [], 0
    for b in bits:
        out.append(b)
        run = run + 1 if b else 0
        if run == 5:
            out.append(0)
            run = 0
    return out


def nrzi(bits, level=1):
    out = []
    for b in bits:
        if not b:
            level ^= 1
        out.append(level)
    return out


FLAG = [0, 1, 1, 1, 1, 1, 1, 0]


def burst_bits(msg):
    """a message's bits -> the levels sent, ramp to ramp (NRZI applied)"""
    air = to_air(msg)
    body = stuff(air + fcs_bits(air))
    raw = [1] * 8 + [0, 1] * 12 + FLAG + body + FLAG + [1] * 8
    return nrzi(raw)


def field(bits, v, n):
    bits.extend((v >> (n - 1 - i)) & 1 for i in range(n))


def sotdma(sync=0, timeout=1, sub=0):
    return (sync << 17) | (timeout << 14) | sub


def msg4(mmsi, y, mo, d, h, mi, s, lat=48.9, lon=-122.75, comm=None):
    b = []
    field(b, 4, 6), field(b, 0, 2), field(b, mmsi, 30)
    field(b, y, 14), field(b, mo, 4), field(b, d, 5), field(b, h, 5), field(b, mi, 6), field(b, s, 6)
    field(b, 1, 1), field(b, round(lon * 600000) & (2 ** 28 - 1), 28)
    field(b, round(lat * 600000) & (2 ** 27 - 1), 27)
    field(b, 1, 4), field(b, 0, 10), field(b, 0, 1)
    field(b, sotdma(0, 3, 12) if comm is None else comm, 19)
    assert len(b) == 168
    return b


def msg1(mmsi, sec, comm, typ=1, lat=48.95, lon=-122.8):
    b = []
    field(b, typ, 6), field(b, 0, 2), field(b, mmsi, 30)
    field(b, 0, 4), field(b, 0, 8), field(b, 52, 10), field(b, 1, 1)
    field(b, round(lon * 600000) & (2 ** 28 - 1), 28), field(b, round(lat * 600000) & (2 ** 27 - 1), 27)
    field(b, 1800, 12), field(b, 511, 9), field(b, sec, 6), field(b, 0, 2), field(b, 0, 3), field(b, 0, 1)
    field(b, comm, 19)
    assert len(b) == 168
    return b


# ---- the burst on the air ----

def gaussian(sps, bt=BT, span=3):
    t = np.arange(-span * sps / 2, span * sps / 2 + 1) / sps
    a = np.sqrt(np.log(2) / 2) / bt
    h = np.sqrt(np.pi) / a * np.exp(-(np.pi * t / a) ** 2)
    return h / h.sum()


def gmsk(levels, fz=st.FZ, f_off=0.0, pre_s=0.004, post_s=0.004, t_frac=0.0):
    """levels (0/1, NRZI already applied) -> complex baseband at fz, unit amplitude, with the
    carrier off pre_s before and post_s after (the ramps are the 8-bit ramp fields)"""
    sps = fz / BAUD
    n = int(np.ceil(len(levels) * sps))
    t = (np.arange(n) - t_frac) / sps
    idx = np.clip(np.floor(t).astype(int), 0, len(levels) - 1)
    nrz = 2.0 * np.asarray(levels, float)[idx] - 1
    f = np.convolve(nrz, gaussian(int(round(sps))), mode="same") * DEV + f_off
    ph = 2 * np.pi * np.cumsum(f) / fz
    amp = np.clip(np.minimum(t, len(levels) - t) / 8.0, 0, 1)        # the ramps
    z = amp * np.exp(1j * ph)
    return np.concatenate([np.zeros(int(pre_s * fz)), z, np.zeros(int(post_s * fz))])


def to_counts(s, cn0=None, seed=1, bw=8000.0, fdma=fe.FDMA):
    return fe.receive(s, st.FZ, cn0, bw=bw, seed=seed, fdma=fdma).astype(float)


# ---- the receiver ----

def baseband(x, fc=FC, dec=DEC, fif=fe.FIF):
    """counts -> complex baseband at fc / dec: mixed down by fif, a 2nd-order CIC by dec"""
    x = np.asarray(x, float)
    n = np.arange(len(x))
    z = x * np.exp(-2j * np.pi * fif / fc * n)
    for _ in range(2):
        z = np.cumsum(z)
    z = z[dec - 1::dec]
    for _ in range(2):
        z = np.diff(np.concatenate([[0], z]))
    return z


def disc(z):
    """the phase step a sample, in turns (the FM discriminator)"""
    return np.concatenate([[0.0], np.angle(z[1:] * np.conj(z[:-1])) / (2 * np.pi)])


def bits_at(th, t0, sps, n):
    """the unwrapped phase th (turns, a sample) -> n hard bits, bit k centred at t0 + k * sps:
    the sign of the phase advance over the bit"""
    tk = t0 + np.arange(n) * sps
    idx = np.arange(len(th))
    a = np.interp(tk + sps / 2, idx, th) - np.interp(tk - sps / 2, idx, th)
    return (a > 0).astype(int).tolist()


def hdlc(levels, at=None, minbits=72):
    """NRZI levels -> [(payload bits in the order sent, index of the start flag's first bit)] of
    every frame between flags whose CRC is good"""
    raw = [1 if levels[i] == levels[i - 1] else 0 for i in range(1, len(levels))]
    frames = []
    i = 0
    n = len(raw)
    while i + 8 <= n:
        if raw[i:i + 8] != FLAG:
            i += 1
            continue
        j = i + 8
        data, run = [], 0
        ok = False
        while j < n:
            b = raw[j]
            if run == 5:
                if b == 0:       # a stuffed zero
                    run = 0
                    j += 1
                    continue
                if j + 1 < n and raw[j + 1] == 0 and len(data) >= minbits:
                    ok = True    # 0111111 then 0: the closing flag's last bits
                break
            data.append(b)
            run = run + 1 if b else 0
            j += 1
        if ok:
            frame = data[:-6] if data[-6:] == [0, 1, 1, 1, 1, 1] else None
            if frame and len(frame) % 8 == 0 and crc_ok(frame):
                frames.append((frame[:-16], (at[i + 1] if at is not None else i + 1)))
                i = j
                continue
        i += 1
    return frames


def receive(x, fc=FC, dec=DEC, fif=fe.FIF, nph=8, rates=(0.0, -0.0015, 0.0015)):
    """counts -> [(message bits, the start flag's time in counts)]. The receiver works on the
    whole capture: the discriminator's mean (the carrier's offset from the IF, over a burst of
    nearly balanced bits) comes off, then the bits are taken at nph clock phases and each rate
    error, and every frame with a good CRC is kept"""
    z = baseband(x, fc, dec, fif)
    f = disc(z)
    on = np.abs(z) > 0.25 * np.percentile(np.abs(z), 90)
    if on.sum() > 10:
        f = f - np.mean(f[on])
    th = np.cumsum(f)
    out, seen = [], set()
    for re_ in rates:
        sps = fc / dec / BAUD * (1 + re_)
        n = int((len(th) - 1) / sps) - 1
        for k in range(nph):
            t0 = sps / 2 + k * sps / nph
            for fr, i in hdlc(bits_at(th, t0, sps, n)):
                m = from_air(fr)
                if tuple(m) not in seen:
                    seen.add(tuple(m))
                    out.append((m, (t0 + i * sps - sps / 2) * dec))
    return out


# ---- the messages ----

def uint(m, a, n):
    v = 0
    for b in m[a:a + n]:
        v = (v << 1) | b
    return v


def comm_state(c):
    sync, to, sub = c >> 17, (c >> 14) & 7, c & 0x3FFF
    d = {"sync": sync, "timeout": to}
    if to == 1:
        d["hour"], d["minute"] = sub >> 9, (sub >> 2) & 0x7F
    elif to in (2, 4, 6):
        d["slot"] = sub
    elif to in (3, 5, 7):
        d["stations"] = sub
    else:
        d["offset"] = sub
    return d


def parse(m):
    """message bits -> a dict of what matters for the clock"""
    t = uint(m, 0, 6)
    d = {"type": t, "mmsi": uint(m, 8, 30)}
    if t in (1, 2, 3) and len(m) >= 168:
        d["second"] = uint(m, 137, 6)
        if t in (1, 2):
            d["comm"] = comm_state(uint(m, 149, 19))
    elif t in (4, 11) and len(m) >= 168:
        d.update(year=uint(m, 38, 14), month=uint(m, 52, 4), day=uint(m, 56, 5), hour=uint(m, 61, 5),
                 minute=uint(m, 66, 6), second=uint(m, 72, 6))
        d["comm"] = comm_state(uint(m, 149, 19))
    elif t == 18 and len(m) >= 168:
        d["second"] = uint(m, 133, 6)
        if not uint(m, 148, 1):
            d["comm"] = comm_state(uint(m, 149, 19))
    return d


def nmea_bits(payload, fill=0):
    m = []
    for ch in payload:
        v = ord(ch) - 48
        if v > 40:
            v -= 8
        field(m, v, 6)
    return m[:len(m) - fill]


# ---- the study ----

def cmd_pd(a):
    rng = np.random.default_rng(a.seed)
    print(f"DEC {a.dec}: {FC / a.dec / 1000:.1f} kHz baseband, {FC / a.dec / BAUD:.2f} samples a bit;"
          f" {a.n} bursts of message 4 at each C/N0, IF offset +-{a.off:.0f} Hz")
    for cn0 in a.cn0s:
        ok = 0
        for k in range(a.n):
            m = msg4(3669000 + k, 2026, 11, 9, 3, 15, int(rng.integers(0, 60)))
            off = float(rng.uniform(-a.off, a.off))
            s = gmsk(burst_bits(m), f_off=off, t_frac=float(rng.uniform(0, 20)))
            x = to_counts(s, cn0, seed=int(rng.integers(1, 1 << 30)))
            got = receive(x, dec=a.dec)
            ok += any(g[0] == m for g in got)
        print(f"  {cn0:5.1f} dB-Hz: {ok}/{a.n}")


FCPU = 96e6


def parse_caps(lines):
    """aiscap's serial lines -> dmr.parse_caps's captures, each with its e line's pre, c0, c1
    (CPU cycles of the 1024 counts before the trigger and the counts after it) when there is one"""
    import re
    from .dmr import parse_caps as pc
    lines = list(lines)
    caps = pc(lines)
    ex = {}
    for ln in lines:
        m = re.search(r"(?:^|say )e (\d+) pre=(\d+) c0=(\d+) c1=(\d+)", ln)
        if m:
            ex[int(m.group(1))] = tuple(map(int, m.groups()[1:]))
    for c in caps:
        if c["k"] in ex:
            c["pre"], c["c0"], c["c1"] = ex[c["k"]]
    return caps


def uniform(c):
    """a capture's counts on one time grid -> (counts, fc). The polled aiscap's counts come at
    one rate before the trigger and a slightly different one after (the loop's two paths): put
    the ones before onto the after's grid. The DMA's captures (no c1) are uniform already at ks."""
    x = np.asarray(c["x"], float)
    if not c.get("c1"):
        return x, c["ks"] * 1000.0 / fe.L
    pre = c["pre"]
    t1 = c["c1"] / (len(x) - pre) / FCPU
    t0 = c["c0"] / 1024 / FCPU if c.get("c0") else t1
    i = np.arange(len(x))
    t = np.where(i < pre, (i - pre) * t0, (i - pre) * t1)
    g = np.arange(t[0], t[-1], t1)
    return np.interp(g, t, x), 1.0 / t1


BACKS = (32, 64, 128, 256)   # polled captures: decode from this many counts before the trigger


def receive_cap(c, **kw):
    """a capture -> [(message bits, the start flag's time in counts from the capture's start)].
    A polled capture is tried from a few starts just before the trigger, at the rate after it
    (the burst starts within ~0.5 ms of the trigger, so the counts before it hardly matter):
    near the threshold each start's carrier-offset estimate and CIC phase decode different bursts
    (run 2: 10 frames from 60 captures with all four, 6-8 with any one). Then the whole capture on
    one grid (uniform)."""
    out, seen = [], set()
    tries = []
    if c.get("c1"):
        fc1 = FCPU * (len(c["x"]) - c["pre"]) / c["c1"]
        tries = [(np.asarray(c["x"][c["pre"] - b:], float), fc1, c["pre"] - b) for b in BACKS]
    x, fc = uniform(c)
    tries.append((x, fc, 0))
    for xx, f, s0 in tries:
        for m, i in receive(xx, fc=f, **kw):
            if tuple(m) not in seen:
                seen.add(tuple(m))
                out.append((m, i + s0))
    return out


def cmd_cap(a):
    for p in a.files:
        with open(p, errors="replace") as fh:
            caps = parse_caps(fh)
        print(f"{os.path.basename(p)}: {len(caps)} captures")
        for c in caps:
            fc = FCPU * (len(c["x"]) - c["pre"]) / c["c1"] if c.get("c1") else c["ks"] * 1000.0 / fe.L
            got = receive_cap(c)
            print(f"  {c['k']:3d} r={c['rssi']} fc={fc:.0f} lost={c['lost']}: {len(got)} frames")
            for m, i in got:
                print(f"      @{i / fc * 1e3:6.2f} ms {parse(m)}")


def cmd_nmea(a):
    for ln in a.files:
        parts = ln.split(",")
        m = nmea_bits(parts[5], int(parts[6].split("*")[0]))
        print(parse(m))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("cmd", choices=["pd", "cap", "nmea"])
    ap.add_argument("files", nargs="*")
    ap.add_argument("--cn0s", type=float, nargs="+", default=[60, 56, 53, 50, 48, 46])
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--dec", type=int, default=DEC)
    ap.add_argument("--off", type=float, default=500.0)
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    {"pd": cmd_pd, "cap": cmd_cap, "nmea": cmd_nmea}[a.cmd](a)


if __name__ == "__main__":
    main()
