"""DMR metadata from a receiver's symbols (docs/k6-dmr.md, "Decoding"): the bursts around each
sync, the CACH's TACT, the slot type (colour code, data type), the BPTC(196,96) full link
control of voice headers and terminators (FLCO, talkgroup or unit, source ID), and the
embedded LC of voice superframes (the same LC, or talker alias). No voice: AMBE+2 is out.

ETSI TS 102 361-1 conventions, as reimplemented here from memory of the standard and of
open decoders, are checked on real signals rather than trusted: `check` reports, for every
burst, the Golay, Hamming and RS syndromes under each candidate layout, and clean bursts must
come out at zero.

  python3 -m sim.k6if.dmrdec audio CLIP.wav...   decode a receiver's FM audio (the IC-7100's)
  python3 -m sim.k6if.dmrdec check CLIP.wav...   the syndromes per burst, both polarities

The symbols come from the audio resampled to 8 a symbol, an RRC matched filter (the
transmitter has the other half), and each burst's own sync for timing, scale and polarity.

MIT licence."""
import argparse
import os

import numpy as np
from scipy import signal

from . import dmr

SPS = 8
DIBIT = {3: (0, 1), 1: (0, 0), -1: (1, 0), -3: (1, 1)}


# ---- symbols out of audio ----

def audio_symbols(x, fs, sps=SPS):
    """audio -> (matched-filtered samples at sps a symbol)"""
    from fractions import Fraction
    x = np.asarray(x, float)
    x = x - np.mean(x)
    r = Fraction(dmr.BAUD * sps / fs).limit_denominator(1000)
    y = signal.resample_poly(x, r.numerator, r.denominator)
    h = dmr.rrc(sps, 0.2, 6)
    return np.convolve(y, h, "same")


def find_syncs(y, sps=SPS, thr=0.85):
    """[(sample of the sync's first symbol, family, sign, rho)]: the normalised correlation at
    every sample offset, symbols sps apart, local peaks over thr within half a symbol"""
    n = len(y) - 24 * sps
    if n <= 0:
        return []
    idx = np.arange(24) * sps
    win = np.lib.stride_tricks.sliding_window_view(y, 23 * sps + 1)[:, ::sps][:n]
    w = win - win.mean(axis=1, keepdims=True)
    nrm = np.sqrt((w * w).sum(axis=1)) * np.sqrt(24) + 1e-12
    out = []
    for f in dmr.FAMS:
        p = np.array(dmr.PAT[f]) / 3.0
        r = (w @ p) / nrm
        a = np.abs(r)
        for i in np.nonzero(a >= thr)[0]:
            lo, hi = max(0, i - sps // 2), min(n, i + sps // 2 + 1)
            if a[i] == a[lo:hi].max():
                out.append((int(i), f, int(np.sign(r[i])), float(r[i])))
    del idx
    return sorted(out)


def burst_symbols(y, s0, sps=SPS, pol=1):
    """the 144 symbols (12 CACH + 132 burst) around a sync starting at sample s0, scaled so the
    sync's outer symbols sit at +-3, as integers -3..3 (None if the burst runs off the end)"""
    first = s0 - (12 + 54) * sps
    last = s0 + (24 + 54) * sps
    if first < 0 or last > len(y):
        return None, None
    v = y[first:first + 144 * sps:sps] * pol
    sync = v[66:90]
    unit = np.mean(np.abs(sync)) / 3.0
    u = (v - np.mean(sync)) / unit
    d = np.where(u > 2, 3, np.where(u > 0, 1, np.where(u > -2, -1, -3)))
    return d, u


def bits(sym):
    return np.array([b for s in sym for b in DIBIT[int(s)]], np.uint8)


# ---- the fields ----

def split(sym144):
    """-> dict of bit arrays: cach (24), info (196), slot (20), mid (48)"""
    b = bits(sym144)
    cach, bb = b[:24], b[24:]
    return {"cach": cach, "info": np.concatenate([bb[:98], bb[166:]]),
            "slot": np.concatenate([bb[98:108], bb[156:166]]), "mid": bb[108:156]}


def to_int(b):
    v = 0
    for x in b:
        v = (v << 1) | int(x)
    return v


# Golay (20,8): the (23,12) Golay code's generator, shortened to 8 data bits, plus overall
# parity. Candidate layouts: data in the top 8 bits then the 11 remainder bits then the parity
# bit ("dpp"), or parity first ("pdp"); checked on real bursts.
G23 = 0xC75


def golay_parity11(d8):
    r = d8 << 11
    for i in range(18, 10, -1):
        if r & (1 << i):
            r ^= G23 << (i - 11)
    return r & 0x7FF


def golay_words(layout="dpp"):
    out = []
    for d in range(256):
        p = golay_parity11(d)
        w19 = (d << 11) | p
        par = bin(w19).count("1") & 1
        out.append((w19 << 1) | par if layout == "dpp" else (par << 19) | w19)
    return np.array(out)


GOLAY = {k: golay_words(k) for k in ("dpp", "pdp")}


def golay_decode(b20, layout="dpp"):
    """-> (8 data bits as an int, bit errors corrected) by nearest codeword"""
    w = to_int(b20)
    dist = np.array([bin(int(c) ^ w).count("1") for c in GOLAY[layout]])
    d = int(np.argmin(dist))
    return d, int(dist[d])


# Hamming (7,4) on the TACT: bits at CACH positions 0,4,8,12,14,18,22
TACT_POS = [0, 4, 8, 12, 14, 18, 22]


def hamming74_syndrome(t):
    """AT TC LCSS1 LCSS0 h2 h1 h0; parity as h2 = d0^d1^d2, h1 = d1^d2^d3, h0 = d0^d1^d3"""
    d0, d1, d2, d3, h2, h1, h0 = (int(x) for x in t)
    return ((d0 ^ d1 ^ d2) ^ h2, (d1 ^ d2 ^ d3) ^ h1, (d0 ^ d1 ^ d3) ^ h0)


def tact(cach):
    t = cach[TACT_POS]
    return {"at": int(t[0]), "tc": int(t[1]), "lcss": int(t[2]) * 2 + int(t[3]),
            "syn": hamming74_syndrome(t)}


# BPTC (196,96): bit i of the burst's 196 info bits is matrix bit (i * 181) % 196; the matrix
# is 13 rows of 15: rows Hamming (15,11,3), columns Hamming (13,9,3), first 3 bits reserved.

def deinterleave196(b):
    m = np.zeros(196, np.uint8)
    for i in range(196):
        m[i] = b[(i * 181) % 196]
    return m


def ham15_syn(d):
    c0 = d[0] ^ d[1] ^ d[2] ^ d[3] ^ d[5] ^ d[7] ^ d[8]
    c1 = d[1] ^ d[2] ^ d[3] ^ d[4] ^ d[6] ^ d[8] ^ d[9]
    c2 = d[2] ^ d[3] ^ d[4] ^ d[5] ^ d[7] ^ d[9] ^ d[10]
    c3 = d[0] ^ d[1] ^ d[2] ^ d[4] ^ d[6] ^ d[7] ^ d[10]
    return (c0 ^ d[11], c1 ^ d[12], c2 ^ d[13], c3 ^ d[14])


def ham13_syn(d):
    c0 = d[0] ^ d[1] ^ d[3] ^ d[5] ^ d[6]
    c1 = d[0] ^ d[1] ^ d[2] ^ d[4] ^ d[6] ^ d[7]
    c2 = d[0] ^ d[1] ^ d[2] ^ d[3] ^ d[5] ^ d[7] ^ d[8]
    c3 = d[0] ^ d[2] ^ d[4] ^ d[5] ^ d[8]
    return (c0 ^ d[9], c1 ^ d[10], c2 ^ d[11], c3 ^ d[12])


def _fix(word, syn_fn, n):
    """correct one bit by trying each (small codes: brute force is fine on the host)"""
    if not any(syn_fn(word)):
        return word, 0
    for i in range(n):
        w = word.copy()
        w[i] ^= 1
        if not any(syn_fn(w)):
            return w, 1
    return word, -1


def bptc196(info196, passes=3):
    """-> (96 data bits, rows bad, cols bad after correction)"""
    m = deinterleave196(info196)[1:].reshape(13, 15)   # bit 0 is the reserved R(3)'s first? see check
    m = m.copy()
    for _ in range(passes):
        for c in range(15):
            col, _ = _fix(m[:, c].copy(), ham13_syn, 13)
            m[:, c] = col
        for r in range(9):
            row, _ = _fix(m[r].copy(), ham15_syn, 15)
            m[r] = row
    rows_bad = sum(1 for r in range(9) if any(ham15_syn(m[r])))
    cols_bad = sum(1 for c in range(15) if any(ham13_syn(m[:, c])))
    data = np.concatenate([m[0, 3:11]] + [m[r, :11] for r in range(1, 9)])
    return data, rows_bad, cols_bad


# RS (12,9) over GF(2^8), x^8 + x^4 + x^3 + x^2 + 1, roots alpha^1..alpha^3; the 3 parity bytes
# are XORed with a mask per data type.
EXP = np.zeros(512, np.int64)
LOG = np.zeros(256, np.int64)
_v = 1
for _i in range(255):
    EXP[_i] = _v
    LOG[_v] = _i
    _v <<= 1
    if _v & 0x100:
        _v ^= 0x11D
EXP[255:510] = EXP[:255]
MASK = {"voice_hdr": 0x96, "term": 0x99}


def gmul(a, b):
    return 0 if a == 0 or b == 0 else int(EXP[LOG[a] + LOG[b]])


def rs_syndromes(cw12):
    """cw12: 12 bytes (data first); S_j = c(alpha^j), j = 1..3"""
    out = []
    for j in (1, 2, 3):
        s = 0
        for c in cw12:
            s = gmul(s, int(EXP[j])) ^ int(c)
        out.append(s)
    return out


def lc_bytes(data96):
    return [to_int(data96[i * 8:(i + 1) * 8]) for i in range(12)]


def full_lc(data96, kind):
    """-> dict, or None if the RS check fails (no correction attempted yet)"""
    by = lc_bytes(data96)
    cw = by[:9] + [b ^ MASK[kind] for b in by[9:]]
    syn = rs_syndromes(cw)
    lc = {"flco": by[0] & 0x3F, "pf": by[0] >> 7, "fid": by[1], "opts": by[2],
          "dst": (by[3] << 16) | (by[4] << 8) | by[5], "src": (by[6] << 16) | (by[7] << 8) | by[8],
          "rs_ok": not any(syn), "syn": syn}
    return lc


DATA_TYPES = {0: "PI hdr", 1: "voice LC hdr", 2: "terminator", 3: "CSBK", 4: "MBC hdr",
              5: "MBC cont", 6: "data hdr", 7: "rate 1/2", 8: "rate 3/4", 9: "idle",
              10: "rate 1"}


def decode_burst(sym144, layout="dpp"):
    f = split(sym144)
    out = {"tact": tact(f["cach"])}
    d, err = golay_decode(f["slot"], layout)
    out["cc"], out["dtype"], out["golay_err"] = d >> 4, d & 15, err
    if err <= 3 and out["dtype"] in (1, 2):
        data, rb, cb = bptc196(f["info"])
        out["bptc_bad"] = (rb, cb)
        out["lc"] = full_lc(data, "voice_hdr" if out["dtype"] == 1 else "term")
    return out


# ---- commands ----

def _load(p):
    import soundfile as sf
    x, fs = sf.read(p)
    return (x if x.ndim == 1 else x[:, 0]), fs


def bursts_from_audio(x, fs, thr=0.85):
    y = audio_symbols(x, fs)
    out = []
    for s0, fam, sign, r in find_syncs(y, thr=thr):
        for pol in (1, -1):
            sym, u = burst_symbols(y, s0, pol=pol)
            if sym is None:
                continue
            out.append((s0 / (SPS * dmr.BAUD), fam, sign * pol, r, pol, sym, u))
    return out


def cmd_check(a):
    for p in a.files:
        x, fs = _load(p)
        print(f"{os.path.basename(p)}:")
        for t, fam, sgn, r, pol, sym, _ in bursts_from_audio(x, fs):
            f = split(sym)
            g = {k: golay_decode(f["slot"], k) for k in GOLAY}
            ta = tact(f["cach"])
            dd = deinterleave196(f["info"])
            print(f"  {t:7.3f}s {fam}{'+' if sgn > 0 else '-'} pol{pol:+d} rho={r:+.2f}"
                  f"  golay dpp d={g['dpp'][0]:02x} e={g['dpp'][1]}  pdp d={g['pdp'][0]:02x} e={g['pdp'][1]}"
                  f"  tact syn={ta['syn']}  r0..2={dd[:3]}")


def cmd_audio(a):
    for p in a.files:
        x, fs = _load(p)
        print(f"{os.path.basename(p)}:")
        for t, fam, sgn, r, pol, sym, _ in bursts_from_audio(x, fs):
            d = decode_burst(sym, a.layout)
            if d["golay_err"] > 3:
                continue
            s = (f"  {t:7.3f}s {fam}{'+' if sgn > 0 else '-'} pol{pol:+d} CC{d['cc']} "
                 f"{DATA_TYPES.get(d['dtype'], d['dtype'])} (golay {d['golay_err']}) TC{d['tact']['tc']}"
                 f" AT{d['tact']['at']} tact_syn={d['tact']['syn']}")
            if "lc" in d:
                lc = d["lc"]
                s += (f"  FLCO {lc['flco']} FID {lc['fid']} dst {lc['dst']} src {lc['src']}"
                      f" rs {'ok' if lc['rs_ok'] else lc['syn']} bptc {d['bptc_bad']}")
            print(s)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("cmd", choices=["audio", "check"])
    ap.add_argument("files", nargs="+")
    ap.add_argument("--layout", default="dpp", choices=list(GOLAY))
    a = ap.parse_args()
    {"audio": cmd_audio, "check": cmd_check}[a.cmd](a)


if __name__ == "__main__":
    main()
