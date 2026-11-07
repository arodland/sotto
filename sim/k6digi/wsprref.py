"""WSPR reference for the K6 modules (docs/k6-ft8-wspr.md): the encoder, an audio synth, and a
decoder with a drift search and a Fano sequential decoder.

The protocol (Joe Taylor K1JT's WSPR, as G4JNT documented it): a 50-bit message (a 28-bit
callsign, a 15-bit grid and 7 bits of power), 31 zero tail bits, a K = 32 rate-1/2
convolutional code (polynomials 0xF2D05351 and 0xE4613C47) to 162 bits, bit-reversal
interleaving, and 162 4-FSK symbols: sync bit + 2 x data bit. A symbol lasts 8192 / 12000 s
(0.683 s) and the tones are 12000 / 8192 Hz (1.46 Hz) apart, 110.6 s in all. Constants checked
against Etherkit's JTEncode (wspr_bit_packing, convolve, wspr_interleave, the sync vector).

  python3 -m sim.k6digi.wsprref encode "AG7EW CN87 10"        the 162 symbols
  python3 -m sim.k6digi.wsprref wav OUT.wav [--f0 1500]       WSPR audio (12 kHz)
  python3 -m sim.k6digi.wsprref decode REC.wav [--lo --hi]    decode a recording

Only the plain message type (a callsign with its digit in position 2 or 3, a 4-character grid
and a power) is handled. MIT licence."""
import argparse

import numpy as np

SYNC = np.array([
    1, 1, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 1, 1, 1, 0, 0, 0, 1, 0, 0,
    1, 0, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, 1, 0, 1, 0, 0,
    0, 0, 0, 0, 1, 0, 1, 1, 0, 0, 1, 1, 0, 1, 0, 0, 0, 1, 1, 0, 1,
    0, 0, 0, 0, 1, 1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 0, 1, 0, 0, 1, 0,
    1, 1, 0, 0, 0, 1, 1, 0, 1, 0, 1, 0, 0, 0, 1, 0, 0, 0, 0, 0, 1,
    0, 0, 1, 0, 0, 1, 1, 1, 0, 1, 1, 0, 0, 1, 1, 0, 1, 0, 0, 0, 1,
    1, 1, 0, 0, 0, 0, 0, 1, 0, 1, 0, 0, 1, 1, 0, 0, 0, 0, 0, 0, 0,
    1, 1, 0, 1, 0, 1, 1, 0, 0, 0, 1, 1, 0, 0, 0], np.int32)
POLY = (0xF2D05351, 0xE4613C47)
NSYM, NBITS = 162, 81                  # symbols; message + tail bits into the coder
SYM = 8192 / 12000                     # s a symbol
DF = 12000 / 8192                      # Hz between tones
POWERS = (0, 3, 7, 10, 13, 17, 20, 23, 27, 30, 33, 37, 40, 43, 47, 50, 53, 57, 60)


def _code(c):
    if c.isdigit():
        return ord(c) - 48
    if c == " ":
        return 36
    return ord(c) - 55


def _parity(x):
    return bin(x).count("1") & 1


def pack(call, grid, dbm):
    """the 50 message bits, MSB first"""
    call = call.upper()
    if len(call) > 1 and call[1].isdigit() and len(call) < 6 and not call[2:3].isdigit():
        call = " " + call                  # the digit must sit in position 3
    call = (call + "      ")[:6]
    assert call[2].isdigit(), call
    n = _code(call[0])
    n = n * 36 + _code(call[1])
    n = n * 10 + _code(call[2])
    for c in call[3:]:
        n = n * 27 + _code(c) - 10
    grid = grid.upper()
    m = (179 - 10 * (ord(grid[0]) - 65) - (ord(grid[2]) - 48)) * 180 + 10 * (ord(grid[1]) - 65) + ord(grid[3]) - 48
    assert dbm in POWERS, dbm
    m = m * 128 + dbm + 64
    return [(n >> (27 - i)) & 1 for i in range(28)] + [(m >> (21 - i)) & 1 for i in range(22)]


def unpack(bits):
    """50 bits -> "CALL GRID DBM", or None if it isn't a plain message"""
    n = int("".join(map(str, bits[:28])), 2)
    m = int("".join(map(str, bits[28:50])), 2)
    al = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ "
    c = [0] * 6
    for i in (5, 4, 3):
        c[i] = al[n % 27 + 10]
        n //= 27
    c[2] = al[n % 10]
    n //= 10
    c[1] = al[n % 36]
    n //= 36
    if n >= 37:
        return None
    c[0] = al[n]
    p = m & 127
    m >>= 7
    if p - 64 not in POWERS or m >= 32400:
        return None
    a, b = 179 - m // 180, m % 180
    grid = chr(65 + a // 10) + chr(65 + b // 10) + str(a % 10) + str(b % 10)
    return "%s %s %d" % ("".join(c).strip(), grid, p - 64)


def conv(bits81):
    s, out = 0, []
    for b in bits81:
        s = ((s << 1) | b) & 0xFFFFFFFF
        out += [_parity(s & POLY[0]), _parity(s & POLY[1])]
    return out


def _order():
    """where each coded bit goes: coded bit i -> symbol _order()[i] (bit-reversed index)"""
    o = []
    for j in range(256):
        r = int("{:08b}".format(j)[::-1], 2)
        if r < NSYM:
            o.append(r)
    return np.array(o)


ORDER = _order()


def encode(text):
    """ "AG7EW CN87 10" -> 162 tones (0-3)"""
    call, grid, dbm = text.split()
    coded = conv(pack(call, grid, int(dbm)) + [0] * 31)
    d = np.zeros(NSYM, np.int32)
    d[ORDER] = coded
    return SYNC + 2 * d


def synth(tones, fs=12000, f0=1500.0):
    """continuous-phase 4-FSK audio, tone k at f0 + k DF Hz"""
    n = int(round(fs * SYM))
    f = np.repeat(f0 + DF * np.asarray(tones, float), n)
    return np.cos(2 * np.pi * np.cumsum(f) / fs)


# ---- decoding ----

def fano(met, delta=17, maxcycles=20000 * NBITS):
    """Fano sequential decoding (after Karn's fano.c). met(k, s): the metric of coded bit pair k
    (bits 2k, 2k+1) coming out as s (2 bits, first bit high). Returns 81 bits or None."""
    tail = NBITS - 31
    gam = [0] * (NBITS + 1)
    st = [0] * (NBITS + 1)
    tm = [[0, 0] for _ in range(NBITS)]
    ii = [0] * NBITS

    def metrics(k):
        s0 = st[k]
        a = met(k, (_parity(s0 & POLY[0]) << 1) | _parity(s0 & POLY[1]))
        s1 = s0 | 1
        b = met(k, (_parity(s1 & POLY[0]) << 1) | _parity(s1 & POLY[1]))
        if k >= tail:
            tm[k][0], tm[k][1] = a, -10**9
        elif a >= b:
            tm[k][0], tm[k][1] = a, b
        else:
            tm[k][0], tm[k][1] = b, a
            st[k] |= 1
        ii[k] = 0

    t, k = 0, 0
    metrics(0)
    for _ in range(maxcycles):
        ng = gam[k] + tm[k][ii[k]]
        if ng >= t:
            if gam[k] < t + delta:
                while ng >= t + delta:
                    t += delta
            gam[k + 1] = ng
            st[k + 1] = (st[k] << 1) & 0xFFFFFFFF
            k += 1
            if k == NBITS:
                return [(st[j] & 1) for j in range(NBITS)]
            metrics(k)
            continue
        while True:
            if k == 0 or gam[k - 1] < t:
                t -= delta
                if ii[k] != 0:
                    ii[k] = 0
                    st[k] ^= 1
                break
            k -= 1
            if k < tail and ii[k] != 1:
                ii[k] = 1
                st[k] ^= 1
                break
    return None


_LIB = None


def _lib():
    """c/k6/wspr/wspr_fano.c built for the host (as ft8_wav builds ft8k), by ctypes"""
    global _LIB
    if _LIB is None:
        import ctypes
        import os
        import subprocess
        here = os.path.dirname(os.path.abspath(__file__))
        src = os.path.join(here, "..", "..", "c", "k6", "wspr", "wspr_fano.c")
        so = os.path.join(here, "build", "wspr_fano.so")
        if not os.path.exists(so) or os.path.getmtime(so) < os.path.getmtime(src):
            os.makedirs(os.path.dirname(so), exist_ok=True)
            tmp = so + ".%d" % os.getpid()
            subprocess.run(["cc", "-O2", "-shared", "-fPIC", "-o", tmp, src], check=True)
            os.replace(tmp, so)
        _LIB = ctypes.CDLL(so)
        _LIB.wspr_fano.restype = ctypes.c_long
    return _LIB


def fano_c(q0, q1, delta=17, maxcycles=10000 * NBITS):
    import ctypes
    a = (ctypes.c_int16 * NSYM)(*[int(v) for v in q0])
    b = (ctypes.c_int16 * NSYM)(*[int(v) for v in q1])
    out = (ctypes.c_uint8 * NBITS)()
    r = _lib().wspr_fano(a, b, out, ctypes.c_int32(delta), ctypes.c_long(maxcycles))
    return list(out) if r >= 0 else None


def metrics_q(llr, scale=4):
    """llr[162] in symbol order -> the integer Fano metrics (q0, q1) per coded bit, coder order:
    log2(2 / (1 + e^-(+-L))) - 1/2, x scale"""
    c = np.clip(np.asarray(llr, float)[ORDER], -12, 12)
    q1 = np.round(scale * (np.log2(2 / (1 + np.exp(-c))) - 0.5)).astype(int)
    q0 = np.round(scale * (np.log2(2 / (1 + np.exp(c))) - 0.5)).astype(int)
    return q0, q1


def decode_soft(llr, use_c=True):
    """llr[162]: per symbol, the log-likelihood of data bit 1 over 0. Deinterleaves, Fano-decodes,
    checks the tail and returns (text, bits) or None"""
    q0, q1 = metrics_q(llr)
    if use_c:
        bits = fano_c(q0, q1)
    else:
        def met(k, s):
            a, b = 2 * k, 2 * k + 1
            return (q1[a] if s & 2 else q0[a]) + (q1[b] if s & 1 else q0[b])
        bits = fano(met)
    if bits is None or any(bits[50:]):
        return None
    text = unpack(bits[:50])
    return (text, bits[:50]) if text else None


def baseband(x, fs, fc, rate=375 / 8):
    """audio -> complex baseband around fc at `rate` Hz (32 samples a symbol)"""
    from scipy import signal
    t = np.arange(len(x)) / fs
    z = x * np.exp(-2j * np.pi * fc * t)
    from fractions import Fraction
    fr = Fraction(rate / fs).limit_denominator(4096)
    z = signal.resample_poly(z, fr.numerator, fr.denominator)
    return z


def demod(z, rate=375 / 8, fspan=20.0, drifts=np.arange(-24, 24.1, 1.0)):
    """z: complex baseband with WSPR's tone 0 within +-fspan Hz of 0 Hz, at `rate` (32 samples a
    symbol, so a 32-point frame's bins are the tones). Searches start time (quarter symbols),
    frequency (half tones) and linear drift over the frame (Hz); returns the best few
    candidates as dicts (f0 Hz, dt s, drift Hz, sync score, llr[162])."""
    n = int(round(rate * SYM))                         # 32
    hop = n // 4
    t = np.arange(len(z)) / rate
    nfft = 2 * n
    cands = []
    for d in drifts:
        # remove a linear drift of d Hz over the frame, referred to its middle (searched below)
        zz = z * np.exp(-1j * np.pi * (d / (NSYM * SYM)) * (t - t[len(t) // 2]) ** 2)
        nf = (len(zz) - n) // hop + 1
        idx = np.arange(n)[None, :] + hop * np.arange(nf)[:, None]
        sp = np.abs(np.fft.fft(zz[idx], nfft, axis=1)) ** 2   # half-tone bins
        sp = np.fft.fftshift(sp, axes=1)
        bins = np.arange(nfft) - nfft // 2                       # bin b = b / 2 tones
        sp /= np.median(sp) + 1e-12
        # per start frame s and base bin b: the 162 symbols' four tone powers
        nstart = nf - 4 * (NSYM - 1)
        if nstart <= 0:
            continue
        rows = np.arange(NSYM) * 4
        lo = np.searchsorted(bins, -fspan / DF * 2)
        hi = np.searchsorted(bins, fspan / DF * 2)
        sgn = (2 * SYNC - 1)[None, :, None]
        Q = sp[np.arange(nstart)[:, None] + rows[None, :]]          # start x 162 x nfft
        p0, p1, p2, p3 = Q[:, :, 0:-6], Q[:, :, 2:-4], Q[:, :, 4:-2], Q[:, :, 6:]
        sc = np.sum(sgn * (p1 + p3 - p0 - p2), axis=1) / np.sum(p0 + p1 + p2 + p3, axis=1)
        sc[:, :lo] = -1
        sc[:, hi:] = -1
        for s in range(nstart):                                     # the best bins of each start
            for b in np.argsort(sc[s])[-3:]:
                cands.append((sc[s, b], s, int(b), d))
    cands.sort(reverse=True)
    out = []
    for sc, s, b, d in cands[:8]:
        out.append({"score": sc, "dt": s * hop / rate, "f0": bins[b] / 2 * DF, "drift": d,
                    "start": s, "bin": b})
    return out


def soft_from(z, c, rate=375 / 8):
    """the llrs for candidate c, by coherent-free tone powers at its exact time, frequency, drift"""
    n = int(round(rate * SYM))
    i0 = int(round(c["dt"] * rate))
    llr = np.zeros(NSYM)
    pw = np.zeros((NSYM, 4))
    tt = np.arange(n) / rate
    for k in range(NSYM):
        seg = z[i0 + k * n:i0 + (k + 1) * n]
        if len(seg) < n:
            break
        fk = c["f0"] + c["drift"] * ((k + 0.5) / NSYM - 0.5)
        for j in range(4):
            pw[k, j] = abs(np.sum(seg * np.exp(-2j * np.pi * (fk + j * DF) * tt[:len(seg)]))) ** 2
    pw /= np.median(pw) + 1e-12
    for k in range(NSYM):
        s = SYNC[k]
        a, b = pw[k, s], pw[k, s + 2]                   # data 0, data 1
        llr[k] = np.log((b + 0.3) / (a + 0.3))
    return llr


def decode_baseband(z, rate=375 / 8, fspan=20.0, drifts=np.arange(-24, 24.1, 1.0)):
    """[(text, f0, dt, drift, score)] decoded from complex baseband"""
    found = []
    for c in demod(z, rate, fspan, drifts):
        best = None
        for df in (0.0, -0.18, 0.18):
            for dd in (0.0, -0.5, 0.5):
                cc = dict(c, f0=c["f0"] + df, drift=c["drift"] + dd)
                r = decode_soft(soft_from(z, cc, rate))
                if r:
                    best = (r[0], cc["f0"], cc["dt"], cc["drift"], c["score"])
                    break
            if best:
                break
        if best and best[0] not in [f[0] for f in found]:
            found.append(best)
    return found


def decode_audio(x, fs, fc=1500.0, fspan=20.0, drifts=np.arange(-24, 24.1, 1.0)):
    z = baseband(np.asarray(x, float), fs, fc)
    return [(t, fc + f, dt, d, s) for t, f, dt, d, s in decode_baseband(z, fspan=fspan, drifts=drifts)]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("encode")
    p.add_argument("text")
    p = sub.add_parser("wav")
    p.add_argument("out")
    p.add_argument("--text", default="AG7EW CN87 10")
    p.add_argument("--f0", type=float, default=1500.0)
    p.add_argument("--peak", type=float, default=0.5)
    p.add_argument("--lead", type=float, default=1.0)
    p = sub.add_parser("decode")
    p.add_argument("wav")
    p.add_argument("--fc", type=float, default=1500.0)
    p.add_argument("--span", type=float, default=20.0)
    a = ap.parse_args()
    if a.cmd == "encode":
        print("".join(map(str, encode(a.text))))
    elif a.cmd == "wav":
        from scipy.io import wavfile
        x = np.concatenate([np.zeros(int(a.lead * 12000)), synth(encode(a.text), 12000, a.f0) * a.peak,
                            np.zeros(12000)])
        wavfile.write(a.out, 12000, np.round(x * 32767).astype(np.int16))
        print("%s: %r, tone 0 at %.1f Hz, %.1f s" % (a.out, a.text, a.f0, len(x) / 12000))
    else:
        from scipy.io import wavfile
        fs, x = wavfile.read(a.wav)
        if x.ndim > 1:
            x = x[:, 0]
        for t, f, dt, d, s in decode_audio(x.astype(float), fs, a.fc, a.span):
            print("%7.2f %6.2f %5.1f %5.2f  %s" % (f, dt, d, s, t))


if __name__ == "__main__":
    main()
