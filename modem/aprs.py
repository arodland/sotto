"""APRS test traffic: AX.25 UI frames as Bell 202 AFSK 1200 audio (for the K6's APRS clock module,
c/k6/mod/aprsclk.c, docs/k6-aprs-clock.md), and a plain float decoder for checking recordings.

  python -m modem.aprs wav OUT.wav                   a few timestamped packets, as audio
  python -m modem.aprs decode REC.wav                frames and timestamps in a recording

Receive-only work: nothing here keys a radio. SPDX-License-Identifier: MIT
"""
import argparse
import sys

import numpy as np

FS = 48000
BAUD = 1200
MARK, SPACE = 1200.0, 2200.0


def crc16(data):
    """CRC-16/X.25 (the AX.25 FCS), as sent: little-endian after the frame"""
    c = 0xFFFF
    for b in data:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ (0x8408 if c & 1 else 0)
    return c ^ 0xFFFF


def addr(call, last=False, h=False):
    """'VE7ABC-9' -> 7 address bytes; h: the has-been-repeated bit (a digipeater's)"""
    c, _, ssid = call.partition("-")
    b = bytes((ord(ch) << 1) for ch in c.ljust(6)[:6])
    return b + bytes([0x60 | (int(ssid or 0) << 1) | (0x80 if h else 0) | (1 if last else 0)])


def frame(src, dst, info, path=(), repeated=0):
    """a UI frame with its FCS. path: digipeater calls; repeated: how many of them have
    repeated it (their H bits set)"""
    hops = [dst, src, *path]
    a = b"".join(addr(c, last=i == len(hops) - 1, h=(i >= 2 and i - 2 < repeated))
                 for i, c in enumerate(hops))
    body = a + b"\x03\xf0" + (info.encode() if isinstance(info, str) else info)
    f = crc16(body)
    return body + bytes([f & 0xFF, f >> 8])


def hdlc_bits(frames, pre_flags=40, gap_flags=2, tail_flags=3):
    """frames -> the line bits (1 mark) after bit stuffing and NRZI"""
    raw = []
    flag = [0, 1, 1, 1, 1, 1, 1, 0]
    raw += flag * pre_flags
    for k, f in enumerate(frames):
        ones = 0
        for byte in f:
            for i in range(8):
                b = (byte >> i) & 1
                raw.append(b)
                ones = ones + 1 if b else 0
                if ones == 5:
                    raw.append(0)
                    ones = 0
        raw += flag * (gap_flags if k < len(frames) - 1 else tail_flags)
    out, tone = [], 1
    for b in raw:
        if b == 0:
            tone ^= 1
        out.append(tone)
    return np.array(out, np.int8)


def afsk(bits, fs=FS, twist_db=0.0, baud=BAUD):
    """line bits -> phase-continuous Bell 202 audio, peak 1 (mark); twist_db: the space tone's
    level over the mark's (a sender's pre-emphasis makes it positive)"""
    n = len(bits)
    t = np.arange(int(n * fs / baud))
    sym = np.minimum((t * baud // fs).astype(int), n - 1)
    f = np.where(bits[sym] == 1, MARK, SPACE)
    ph = 2 * np.pi * np.cumsum(f) / fs
    a = np.where(bits[sym] == 1, 1.0, 10 ** (twist_db / 20))
    return a * np.sin(ph)


def packet_audio(frames, fs=FS, twist_db=0.0, pre_flags=40, baud=BAUD):
    """(audio, the first frame's first bit in seconds from the audio's start)"""
    bits = hdlc_bits(frames, pre_flags)
    return afsk(bits, fs, twist_db, baud), pre_flags * 8 / baud


def ts_dhm(utc_s):
    """DDHHMMz for seconds since the month's day 0 00:00 (day 1 = 86400 .. )"""
    d, r = divmod(int(utc_s), 86400)
    return f"{d:02d}{r // 3600:02d}{r // 60 % 60:02d}z"


def ts_hms(utc_s):
    r = int(utc_s) % 86400
    return f"{r // 3600:02d}{r // 60 % 60:02d}{r % 60:02d}h"


def rmc(utc_s, date="081126"):
    r = utc_s % 86400
    hh, mm, ss = int(r // 3600), int(r // 60 % 60), int(r % 60 * 100 + 1e-6) / 100  # truncated, as a stamp
    return f"$GPRMC,{hh:02d}{mm:02d}{ss:05.2f},A,4857.00,N,12229.00,W,000.0,000.0,{date},,*00"


def bench_wav(t_utc, call="AG7EW", gap=0.6, fs=FS):
    """the on-air loopback (c/k6/mod/aprsclk_440.c): three stations of one call (-1 HHMMSSh, -2
    $GPRMC, -3 DDHHMMz) each stamped with the true UTC at its own first bit if the audio starts at
    t_utc (Unix seconds), truncated, so every stamp is early by the keying delay only.
    -> (audio, [(call, info, the frame's end in s from the audio's start)])"""
    import time as _t
    tm = _t.gmtime(t_utc)
    month0 = t_utc - (tm.tm_mday * 86400 + tm.tm_hour * 3600 + tm.tm_min * 60 + tm.tm_sec)
    pos, out, sent = 0.0, [], []
    for k, kind in enumerate("hrz"):
        pre = 40
        u = t_utc + pos + pre * 8 / BAUD                 # this frame's first bit
        if kind == "h":
            info = "/" + ts_hms(u) + "4857.00N/12229.00W>K6 clock test"
        elif kind == "r":
            g = _t.gmtime(u)
            info = rmc(u % 86400, f"{g.tm_mday:02d}{g.tm_mon:02d}{g.tm_year % 100:02d}")
        else:
            info = "@" + ts_dhm(u - month0) + "4857.00N/12229.00W_K6 clock test"
        c = f"{call}-{k + 1}"
        x, _ = packet_audio([frame(c, "APRS", info)], fs=fs, pre_flags=pre)
        out += [x, np.zeros(int(gap * fs))]
        sent.append((c, info, pos + len(x) / fs))
        pos += (len(x) + int(gap * fs)) / fs
    return np.concatenate(out), sent


# ---- a float decoder for recordings (correlators + PLL + HDLC, as the K6 module) ----
def demod(audio, fs):
    """audio -> list of frames (bytes, with good FCS) and each one's end sample"""
    n = int(round(fs / BAUD))
    t = np.arange(len(audio))
    out = []
    for f in (MARK, SPACE):
        z = audio * np.exp(-2j * np.pi * f * t / fs)
        c = np.convolve(z, np.ones(n), "same")
        out.append(np.abs(c))
    m, s = out
    k = max(1, int(0.05 * fs))
    pm = np.maximum.accumulate(m)  # coarse: normalise by a running mean of each
    pm = np.convolve(m, np.ones(k) / k, "same") + 1e-9
    ps = np.convolve(s, np.ones(k) / k, "same") + 1e-9
    v = (m / pm - s / ps) > 0
    frames, pll, step, last_v, last_bit = [], 0.0, BAUD / fs, v[0], v[0]
    sr, inf, buf, bb = 0, False, [], 0x80
    for i, x in enumerate(v):
        pll += step
        if pll >= 0.5:
            pll -= 1.0
            bit = int(x == last_bit)
            last_bit = x
            sr = ((sr << 1) | bit) & 0xFF
            if sr == 0x7E:
                if inf and len(buf) >= 18 and crc16(bytes(buf[:-2])) == buf[-2] | buf[-1] << 8:
                    frames.append((bytes(buf), i))
                inf, buf, bb = True, [], 0x80
                continue
            if sr & 0x7F == 0x7F:
                inf = False
                continue
            if not inf or sr & 0x3F == 0x3E:
                continue
            if bit:
                bb |= 0x100
            if bb & 1:
                buf.append(bb >> 1)
                bb = 0x80
            else:
                bb >>= 1
        if x != last_v:
            pll *= 0.75 if inf else 0.5
            last_v = x
    return frames


def describe(f):
    """'SRC>DST,PATH:info' for a frame (with FCS)"""
    calls, i = [], 0
    while True:
        a = f[i:i + 7]
        c = bytes(b >> 1 for b in a[:6]).decode("latin-1").strip()
        ssid = (a[6] >> 1) & 15
        calls.append(c + (f"-{ssid}" if ssid else "") + ("*" if a[6] & 0x80 and i >= 14 else ""))
        i += 7
        if a[6] & 1:
            break
    info = f[i + 2:-2].decode("latin-1")
    return f"{calls[1]}>{calls[0]}{''.join(',' + c for c in calls[2:])}:{info}"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("wav")
    w.add_argument("out")
    w.add_argument("--utc", type=float, default=8 * 86400 + 17 * 3600 + 52 * 60 + 1.0)
    w.add_argument("--now", action="store_true",
                   help="the on-air loopback: three AG7EW stations stamped with the UTC now (play it at once)")
    w.add_argument("--call", default="AG7EW")
    w.add_argument("--delay", type=float, default=0.0,
                   help="with --now: seconds from now until the audio starts (keying time); the stamps stay "
                        "truncated, so too small only makes them early")
    d = sub.add_parser("decode")
    d.add_argument("wav")
    a = ap.parse_args(argv)
    import soundfile as sf
    if a.cmd == "wav" and a.now:
        import time
        t0 = time.time() + a.delay
        x, sent = bench_wav(t0, a.call)
        sf.write(a.out, 0.5 * x, FS)
        print(f"start {t0:.3f}")
        for c, info, end in sent:
            print(f"{c} {info}  (ends +{end:.3f} s)")
        return 0
    if a.cmd == "wav":
        fr = [frame("N0CALL-9", "APRS", "/" + ts_hms(a.utc) + "4857.00N/12229.00W>test"),
              frame("N0CALL-1", "APRS", "@" + ts_dhm(a.utc) + "4857.00N/12229.00W_test")]
        x, _ = packet_audio(fr)
        sf.write(a.out, 0.5 * x, FS)
        return 0
    x, fs = sf.read(a.wav)
    if x.ndim > 1:
        x = x[:, 0]
    for f, i in demod(x, fs):
        print(f"{i / fs:9.3f} {describe(f)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
