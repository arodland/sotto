"""DMR metadata from a receiver's symbols (docs/k6-dmr.md, "Decoding"): the bursts around each
sync, the CACH's TACT, the slot type (colour code, data type), the BPTC(196,96) full link
control of voice headers and terminators (FLCO, talkgroup or unit, source ID), and the
embedded LC of voice superframes (the same LC, or talker alias). No voice: AMBE+2 is out.

ETSI TS 102 361-1 conventions, as reimplemented here from memory of the standard and of
open decoders, are checked on real signals rather than trusted: `check` reports, for every
burst, the Golay, Hamming and RS syndromes under each candidate layout, and clean bursts must
come out at zero.

  python3 -m sim.k6if.dmrdec audio CLIP.wav...   decode a receiver's FM audio (the IC-7100's)
  python3 -m sim.k6if.dmrdec calls CLIP.wav...   one line a call: talkgroup, source, talker alias
  python3 -m sim.k6if.dmrdec summary CLIP.wav... compact: counts, distinct CSBKs, calls, data packets
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


def counts_symbols(x, fc=dmr.FC, sps=SPS):
    """the K6's GP0 counts -> matched-filtered frequency at sps a symbol: the MCU's band-pass,
    the IF's phase at sps points a symbol, its steps, an RRC"""
    ph, _ = dmr.phase_grid(dmr.bandpass(x), sps, fc=fc)
    y = np.nan_to_num(dmr.freq(ph, sps))
    return np.convolve(y, dmr.rrc(sps, 0.2, 6), "same")


def find_syncs(y, sps=SPS, thr=0.85):
    """[(sample of the sync's first symbol, family, sign, rho)]: the normalised correlation at
    every sample offset, symbols sps apart, local peaks over thr within half a symbol"""
    n = len(y) - 24 * sps
    if n <= 0:
        return []
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
    return sorted(out)


def burst_symbols(y, s0, sps=SPS, pol=1, ref=None):
    """the 144 symbols (12 CACH + 132 burst) around a sync starting at sample s0, scaled so the
    sync's outer symbols sit at +-3, as integers -3..3, and the soft values (None, None if the
    burst runs off the end). ref = (centre, unit) of another burst's sync, for bursts with an
    EMB where the sync would be."""
    first = s0 - (12 + 54) * sps
    last = s0 + (24 + 54) * sps
    if first < 0 or last > len(y):
        return None, None
    v = y[first:first + 144 * sps:sps] * pol
    if ref is None:
        sync = v[66:90]
        ref = (np.mean(sync), np.mean(np.abs(sync - np.mean(sync))) / 3.0)
    u = (v - ref[0]) / ref[1]
    d = np.where(u > 2, 3, np.where(u > 0, 1, np.where(u > -2, -1, -3)))
    return d, u


def burst_ref(y, s0, sps=SPS, pol=1):
    """(centre, unit) of the sync starting at sample s0, for burst_symbols(ref=)"""
    v = y[s0:s0 + 24 * sps:sps] * pol
    c = np.mean(v)
    return c, np.mean(np.abs(v - c)) / 3.0


def bits(sym):
    return np.array([b for s in sym for b in DIBIT[int(s)]], np.uint8)


# ---- the fields ----

def split(sym144):
    """-> dict of bit arrays: cach (24), info (196), slot (20), mid (48)"""
    b = bits(sym144)
    cach, bb = b[:24], b[24:]
    return {"cach": cach, "info": np.concatenate([bb[:98], bb[166:]]),
            "slot": np.concatenate([bb[98:108], bb[156:166]]), "mid": bb[108:156]}


def split_soft(u144):
    """soft values -> the same fields as split, per bit positive = 1"""
    b = soft_bits(u144)
    bb = b[24:]
    return {"cach": b[:24], "info": np.concatenate([bb[:98], bb[166:]]),
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
    """-> the LC fields, rs_ok and the syndromes (no RS correction attempted)"""
    by = lc_bytes(data96)
    cw = by[:9] + [b ^ MASK[kind] for b in by[9:]]
    syn = rs_syndromes(cw)
    return dict(lc_fields(by), rs_ok=not any(syn), syn=syn, bytes=by)


DATA_TYPES = {0: "PI hdr", 1: "voice LC hdr", 2: "terminator", 3: "CSBK", 4: "MBC hdr",
              5: "MBC cont", 6: "data hdr", 7: "rate 1/2", 8: "rate 3/4", 9: "idle",
              10: "rate 1"}


def decode_burst(sym144, layout="dpp"):
    f = split(sym144)
    out = {"tact": tact(f["cach"])}
    d, err = golay_decode(f["slot"], layout)
    out["cc"], out["dtype"], out["golay_err"] = d >> 4, d & 15, err
    if err <= 3 and out["dtype"] in (1, 2, 3, 4, 6, 7):
        data, rb, cb = bptc196(f["info"])
        out["bptc_bad"] = (rb, cb)
        if out["dtype"] in (1, 2):
            out["lc"] = full_lc(data, "voice_hdr" if out["dtype"] == 1 else "term")
        else:
            by = lc_bytes(data)
            out["bytes"] = by
            kind = {3: "csbk", 4: "mbc_hdr", 6: "data_hdr"}.get(out["dtype"])
            if kind:
                out["crc"] = crc_match(by, kind)
                out["fields"] = block_fields(by, out["dtype"]) if out["dtype"] != 4 else {}
    return out

# ---- EMB, embedded LC, CSBK and data headers ----

# QR (16,7,6) on the EMB (colour code 4, PI 1, LCSS 2): the (17,9) quadratic residue code's
# generator x^8 + x^5 + x^4 + x^3 + 1, an overall parity bit, shortened by 2 data bits.
def qr1676_word(d7):
    r = d7 << 8
    for i in range(14, 7, -1):
        if r & (1 << i):
            r ^= 0x139 << (i - 8)
    w17 = (d7 << 8) | r
    return (d7 << 9) | (r << 1) | (bin(w17).count("1") & 1)


QR1676 = np.array([qr1676_word(d) for d in range(128)])


def emb_decode(mid48):
    """-> (cc, pi, lcss, bit errors) from a burst's 48 middle bits (EMB 8 | data 32 | EMB 8)"""
    w = to_int(np.concatenate([mid48[:8], mid48[40:]]))
    dist = np.array([bin(int(c) ^ w).count("1") for c in QR1676])
    d = int(np.argmin(dist))
    return d >> 3, (d >> 2) & 1, d & 3, int(dist[d])


def emb_encode(cc, lcss, pi=0):
    w = int(QR1676[(cc << 3) | (pi << 2) | lcss])
    return np.array([(w >> (15 - i)) & 1 for i in range(16)], np.uint8)


def ham16_syn(d):
    return ham15_syn(d) + (d[0] ^ d[2] ^ d[5] ^ d[6] ^ d[8] ^ d[9] ^ d[10] ^ d[15],)


# Embedded LC: 72 LC bits and a 5-bit checksum (the 9 bytes' sum mod 31) in an 8 x 16 matrix:
# rows 0-1 carry 11 data bits, rows 2-6 10 data bits and one checksum bit (column 10), each
# row Hamming (16,11,4); row 7 is column parity. Sent column by column, 32 bits in each of
# voice bursts B-E (LCSS 1, 3, 3, 2).
EMB_DATA = [r * 16 + c for r in range(7) for c in range(11 if r < 2 else 10)]
EMB_CRC = [r * 16 + 10 for r in range(2, 7)]
EMB_ORDER = [16 * (a % 8) + a // 8 for a in range(128)]     # sent bit a = matrix bit EMB_ORDER[a]


def emb_lc_decode(frag128):
    """the 4 fragments joined (128 bits, as sent) -> (9 LC bytes, ok)"""
    m = np.zeros(128, np.uint8)
    m[EMB_ORDER] = frag128
    m = m.reshape(8, 16)
    for r in range(7):
        m[r], _ = _fix(m[r].copy(), ham16_syn, 16)
    flat = m.reshape(-1)
    by = [to_int(flat[EMB_DATA][i * 8:(i + 1) * 8]) for i in range(9)]
    ok = ((m.sum(axis=0) & 1).sum() <= 1                    # one error in row 7 is harmless
          and all(not any(ham16_syn(m[r])) for r in range(7))
          and to_int(flat[EMB_CRC]) == sum(by) % 31)
    return by, ok


def emb_lc_encode(by9):
    m = np.zeros(128, np.uint8)
    m[EMB_DATA] = [(b >> (7 - i)) & 1 for b in by9 for i in range(8)]
    c = sum(by9) % 31
    m[EMB_CRC] = [(c >> (4 - i)) & 1 for i in range(5)]
    m = m.reshape(8, 16)
    for r in range(7):
        m[r] = _parity_fill(m[r], ham16_syn, 16, 11)
    m[7] = m[:7].sum(axis=0) & 1
    return m.reshape(-1)[EMB_ORDER]


def lc_fields(by):
    return {"flco": by[0] & 0x3F, "pf": by[0] >> 7, "fid": by[1], "opts": by[2],
            "dst": (by[3] << 16) | (by[4] << 8) | by[5], "src": (by[6] << 16) | (by[7] << 8) | by[8]}


def talker_alias(parts):
    """{0: header LC bytes (FLCO 4), 1..3: blocks (FLCO 5-7)} -> (format, text so far)"""
    if 0 not in parts:
        return None, ""
    h = parts[0]
    fmt, n = h[2] >> 6, (h[2] >> 1) & 31
    b = [h[2] & 1] + [(x >> (7 - i)) & 1 for x in h[3:9] for i in range(8)]
    for k in (1, 2, 3):
        if k not in parts:
            break
        b += [(x >> (7 - i)) & 1 for x in parts[k][2:9] for i in range(8)]
    if fmt == 0:
        ch = [to_int(b[i:i + 7]) for i in range(0, len(b) - 6, 7)]
    else:
        ch = [to_int(b[i:i + 8]) for i in range(1, len(b) - 7, 8)]
    if fmt == 3:
        txt = bytes(ch[:2 * n]).decode("utf-16-be", "replace")
    else:
        txt = bytes(ch[:n]).decode("utf-8" if fmt == 2 else "latin-1", "replace")
    return fmt, txt


# CRC-CCITT on 10-byte blocks (CSBK, data headers): candidates (init, final XOR) are checked
# on real bursts; the per-type masks are ETSI's.
CRC_MASK = {"csbk": 0xA5A5, "data_hdr": 0xCCCC, "mbc_hdr": 0xAAAA}
CRC_VARIANTS = {"i0x0": (0, 0), "i0xF": (0, 0xFFFF), "iFx0": (0xFFFF, 0), "iFxF": (0xFFFF, 0xFFFF)}


def crc_ccitt(by, init=0, xorout=0):
    c = init
    for b in by:
        c ^= b << 8
        for _ in range(8):
            c = ((c << 1) ^ 0x1021) if c & 0x8000 else (c << 1)
            c &= 0xFFFF
    return c ^ xorout


def crc_match(by12, kind):
    """-> [variant names whose CRC matches]"""
    got = (by12[10] << 8) | by12[11]
    return [k for k, (i, x) in CRC_VARIANTS.items()
            if crc_ccitt(by12[:10], i, x) ^ CRC_MASK[kind] == got]


CRC_USE = "i0xF"          # MMDVM-style: init 0, inverted (to confirm on 440.925)

CSBKO = {0x04: "UU voice req", 0x05: "UU ans rsp", 0x07: "ch timing", 0x19: "aloha",
         0x1C: "ahoy", 0x20: "ack", 0x28: "C_BCAST", 0x38: "BS act", 0x3D: "preamble"}


def block_fields(by, dtype):
    """CSBK / data header fields from the 12 BPTC bytes"""
    if dtype == 3:
        out = {"lb": by[0] >> 7, "pf": (by[0] >> 6) & 1, "csbko": by[0] & 0x3F, "fid": by[1],
               "data": bytes(by[2:10]).hex()}
        out["name"] = CSBKO.get(out["csbko"], "") if out["fid"] == 0 else ""
        if out["csbko"] == 0x3D and out["fid"] == 0:
            out.update(dst=(by[4] << 16) | (by[5] << 8) | by[6], src=(by[7] << 16) | (by[8] << 8) | by[9],
                       blocks=by[3], data_follows=(by[2] >> 7) & 1, group=(by[2] >> 6) & 1)
        return out
    out = {"gi": by[0] >> 7, "a": (by[0] >> 6) & 1, "dpf": by[0] & 15, "sap": by[1] >> 4,
           "dst": (by[2] << 16) | (by[3] << 8) | by[4], "src": (by[5] << 16) | (by[6] << 8) | by[7],
           "blocks": by[8] & 0x7F, "f": by[8] >> 7, "b9": by[9]}
    out["dpf_name"] = {0: "UDT", 1: "response", 2: "unconfirmed", 3: "confirmed", 13: "defined short",
                       14: "raw short", 15: "proprietary"}.get(out["dpf"], "")
    return out


def soft_bits(u):
    """soft symbol values (about -3..3) -> per bit, positive means 1: +3 = 01, +1 = 00, -1 = 10, -3 = 11"""
    u = np.asarray(u, float)
    out = np.empty(2 * len(u))
    out[0::2] = -u
    out[1::2] = np.abs(u) - 2
    return out


# ---- the other way: valid bursts for tests (the same layouts as the decoder) ----

def _parity_fill(word, syn_fn, n, k):
    """set the n - k parity bits so the syndrome is zero (in these checks each parity bit
    enters its own syndrome bit alone)"""
    w = np.array(word, np.uint8)
    w[k:] = 0
    w[k:] = syn_fn(w)
    assert not any(syn_fn(w))
    return w


def rs_encode(data9, kind):
    """9 data bytes -> 12 (the parity masked for kind)"""
    gen = [1]
    for j in (1, 2, 3):                                  # (x - alpha^j)
        nxt = [0] * (len(gen) + 1)
        for i, g in enumerate(gen):
            nxt[i] ^= g
            nxt[i + 1] ^= gmul(g, int(EXP[j]))
        gen = nxt
    msg = list(data9) + [0, 0, 0]
    for i in range(9):
        c = msg[i]
        if c:
            for k in range(1, 4):
                msg[i + k] ^= gmul(gen[k], c)
    par = msg[9:]
    return list(data9) + [b ^ MASK[kind] for b in par]


def bptc196_encode(data96):
    m = np.zeros((13, 15), np.uint8)
    flat = np.concatenate([np.zeros(3, np.uint8), np.asarray(data96, np.uint8)])
    for r in range(9):
        m[r, :11] = flat[r * 11:(r + 1) * 11]
        m[r] = _parity_fill(m[r], ham15_syn, 15, 11)
    for c in range(15):
        m[:, c] = _parity_fill(m[:, c], ham13_syn, 13, 9)
    mm = np.concatenate([[0], m.reshape(-1)]).astype(np.uint8)
    out = np.zeros(196, np.uint8)
    for i in range(196):
        out[(i * 181) % 196] = mm[i]
    return out


def lc_encode(flco, dst, src, fid=0, opts=0, kind="voice_hdr"):
    by = [flco & 0x3F, fid, opts, dst >> 16 & 255, dst >> 8 & 255, dst & 255,
          src >> 16 & 255, src >> 8 & 255, src & 255]
    cw = rs_encode(by, kind)
    return np.array([(b >> (7 - i)) & 1 for b in cw for i in range(8)], np.uint8)


def burst_bits(cc, dtype, info196, mid48, at=1, tc=0, lcss=0, layout="dpp", rng=None):
    """-> 288 bits: CACH (TACT with its Hamming bits, the rest random) + the burst"""
    rng = rng or np.random.default_rng(0)
    cach = rng.integers(0, 2, 24).astype(np.uint8)
    t = np.array([at, tc, lcss >> 1, lcss & 1, 0, 0, 0], np.uint8)
    d0, d1, d2, d3 = t[:4]
    t[4:] = [d0 ^ d1 ^ d2, d1 ^ d2 ^ d3, d0 ^ d1 ^ d3]
    cach[TACT_POS] = t
    w = int(GOLAY[layout][(cc << 4) | dtype])
    slot = np.array([(w >> (19 - i)) & 1 for i in range(20)], np.uint8)
    info = np.asarray(info196, np.uint8)
    return np.concatenate([cach, info[:98], slot[:10], mid48, slot[10:], info[98:]])


def bits_to_symbols(b):
    inv = {v: k for k, v in DIBIT.items()}
    return np.array([inv[(int(b[i]), int(b[i + 1]))] for i in range(0, len(b), 2)])


def voice_header_symbols(cc=1, tg=91, src=3112345, n=3, seed=0):
    """n voice LC header bursts (BS data sync), alternating slots' worth of idle between, as
    4800/s symbols: what a repeater sends at a call's start"""
    rng = np.random.default_rng(seed)
    data_sync = bits(-np.array(dmr.PAT["bs"]))
    hdr = bptc196_encode(lc_encode(0, tg, src))
    out = []
    for i in range(2 * n):
        if i % 2 == 0:
            b = burst_bits(cc, 1, hdr, data_sync, tc=0, rng=rng)
        else:
            b = burst_bits(cc, 9, rng.integers(0, 2, 196), data_sync, tc=1, rng=rng)
        out.append(bits_to_symbols(b))
    return np.concatenate(out)


def voice_burst_bits(mid48, tc=0, rng=None):
    rng = rng or np.random.default_rng(0)
    cach = rng.integers(0, 2, 24).astype(np.uint8)
    t = np.array([1, tc, 0, 0, 0, 0, 0], np.uint8)
    d0, d1, d2, d3 = t[:4]
    t[4:] = [d0 ^ d1 ^ d2, d1 ^ d2 ^ d3, d0 ^ d1 ^ d3]
    cach[TACT_POS] = t
    v = rng.integers(0, 2, 216).astype(np.uint8)            # stands in for AMBE+2 frames
    return np.concatenate([cach, v[:108], np.asarray(mid48, np.uint8), v[108:]])


def lc9(flco, dst, src, fid=0, opts=0):
    return [flco & 0x3F, fid, opts, dst >> 16 & 255, dst >> 8 & 255, dst & 255,
            src >> 16 & 255, src >> 8 & 255, src & 255]


def alias_lcs(text, fid=0):
    """talker alias text (7-bit format) -> the 4 LCs (FLCO 4, 5, 6, 7) as 9-byte lists"""
    n = len(text)
    b = [(ord(c) >> (6 - i)) & 1 for c in text for i in range(7)]
    b += [0] * (49 + 3 * 56 - len(b))
    hb = [4, fid, (0 << 6) | (n << 1) | b[0]] + [to_int(b[1 + 8 * i:9 + 8 * i]) for i in range(6)]
    out = [hb]
    for k in range(3):
        o = 49 + 56 * k
        out.append([4 + k + 1, fid] + [to_int(b[o + 8 * i:o + 8 * i + 8]) for i in range(7)])
    return out


def voice_call_symbols(cc=1, tg=91, src=3112345, superframes=4, alias=None, seed=0):
    """a repeater's call on slot 1 (slot 2 idle): 3 voice LC headers, superframes whose bursts
    B-E carry the embedded LC (talker alias LCs in turn after the first, if given), a
    terminator; as 4800/s symbols"""
    rng = np.random.default_rng(seed)
    data_sync = bits(-np.array(dmr.PAT["bs"]))
    voice_sync = bits(np.array(dmr.PAT["bs"]))
    s1 = [burst_bits(cc, 1, bptc196_encode(lc_encode(0, tg, src)), data_sync, tc=0, rng=rng)
          for _ in range(3)]
    lcs = [lc9(0, tg, src)]
    if alias:
        lcs += alias_lcs(alias)
    for k in range(superframes):
        lc = lcs[0] if k % 2 == 0 or len(lcs) == 1 else lcs[1 + (k // 2) % (len(lcs) - 1)]
        fr = emb_lc_encode(lc)
        s1.append(voice_burst_bits(voice_sync, 0, rng))
        for j, lcss in enumerate((1, 3, 3, 2, 0)):
            e = emb_encode(cc, lcss)
            body = fr[32 * j:32 * j + 32] if j < 4 else np.zeros(32, np.uint8)
            s1.append(voice_burst_bits(np.concatenate([e[:8], body, e[8:]]), 0, rng))
    s1.append(burst_bits(cc, 2, bptc196_encode(lc_encode(0, tg, src, kind="term")), data_sync,
                         tc=0, rng=rng))
    out = []
    for b in s1:
        out.append(bits_to_symbols(b))
        out.append(bits_to_symbols(burst_bits(cc, 9, rng.integers(0, 2, 196), data_sync, tc=1,
                                              rng=rng)))
    return np.concatenate(out)


# ---- a stream of bursts: polarity, voice superframes, calls ----

def stream_polarity(y, syncs, layout="dpp"):
    """the input's polarity: each sync read as a data sync must give a clean slot type"""
    votes = 0
    for s0, fam, sign, r in syncs:
        sym, _ = burst_symbols(y, s0, pol=-sign)
        if sym is not None and golay_decode(split(sym)["slot"], layout)[1] == 0:
            votes -= sign
    return 1 if votes >= 0 else -1


def decode_stream(y, layout="dpp", thr=0.85):
    """matched-filtered samples -> (polarity, [event dicts]): data bursts as decode_burst plus
    t, slot and soft values; voice bursts A with the EMBs of B-F and their embedded LC"""
    syncs = find_syncs(y, thr=thr)
    pol = stream_polarity(y, syncs, layout)
    ev = []
    for s0, fam, sign, r in syncs:
        sym, u = burst_symbols(y, s0, pol=pol)
        if sym is None:
            continue
        t = s0 / (SPS * dmr.BAUD)
        if sign * pol < 0:
            d = decode_burst(sym, layout)
            if d["golay_err"] > 3:
                continue
            d.update(t=t, fam=fam, kind="data", u=u, rho=r)
        else:
            d = {"t": t, "fam": fam, "kind": "voice", "tact": tact(split(sym)["cach"]), "rho": r}
            ref = burst_ref(y, s0, pol=pol)
            frags = []
            for k in range(1, 6):
                sk, _ = burst_symbols(y, s0 + 288 * SPS * k, pol=pol, ref=ref)
                if sk is None:
                    break
                mid = split(sk)["mid"]
                frags.append(emb_decode(mid) + (mid[8:40],))
            d["emb"] = [f[:4] for f in frags]
            if len(frags) >= 4 and [f[2] for f in frags[:4]] == [1, 3, 3, 2] \
                    and all(f[3] <= 2 for f in frags[:4]):
                by, ok = emb_lc_decode(np.concatenate([f[4] for f in frags[:4]]))
                d["emb_lc"] = dict(lc_fields(by), ok=ok, bytes=by)
        d["slot"] = d["tact"]["tc"] + 1
        ev.append(d)
    return pol, ev


TERM_DELTA = None


def combine_lc(events):
    """voice LC headers and terminators of one call, soft-combined: the terminators' parity
    masks turned into the header's (the codes are linear), the bits' soft values summed ->
    (full_lc of the sum, copies used)"""
    global TERM_DELTA
    if TERM_DELTA is None:
        TERM_DELTA = bptc196_encode(np.array(
            [(b >> (7 - i)) & 1 for b in [0] * 9 + [0x96 ^ 0x99] * 3 for i in range(8)], np.uint8))
    acc, n = np.zeros(196), 0
    for d in events:
        if d.get("kind") == "data" and d["dtype"] in (1, 2) and d.get("u") is not None:
            sb = split_soft(d["u"])["info"]
            if d["dtype"] == 2:
                sb = np.where(TERM_DELTA == 1, -sb, sb)
            acc += sb
            n += 1
    if not n:
        return None, 0
    data, rb, cb = bptc196((acc > 0).astype(np.uint8))
    return full_lc(data, "voice_hdr"), n


def calls(ev, gap_s=1.0):
    """events -> per slot, calls: [{slot, t0, t1, lc (by vote), copies, emb, alias, combined}]"""
    out = []
    for slot in (1, 2):
        cur = None
        for d in ev:
            if d["slot"] != slot:
                continue
            is_call = d["kind"] == "voice" or d.get("dtype") in (1, 2)
            if not is_call:
                continue
            new_call = d.get("dtype") == 1 and cur is not None and cur["ev"][-1].get("dtype") == 2
            if cur is None or d["t"] - cur["t1"] > gap_s or new_call:
                cur = {"slot": slot, "t0": d["t"], "t1": d["t"], "ev": []}
                out.append(cur)
            cur["t1"] = d["t"]
            cur["ev"].append(d)
    for c in out:
        votes, ta = {}, {}
        for d in c["ev"]:
            lc = d.get("lc") if d.get("lc", {}).get("rs_ok") else None
            e = d.get("emb_lc") if d.get("emb_lc", {}).get("ok") else None
            for x in (lc, e):
                if x is None:
                    continue
                if x["flco"] in (4, 5, 6, 7):
                    ta[x["flco"] - 4] = x["bytes"]
                else:
                    k = (x["flco"], x["fid"], x["dst"], x["src"])
                    votes[k] = votes.get(k, 0) + 1
        c["lc"] = max(votes, key=votes.get) if votes else None
        c["copies"] = votes
        c["alias"] = talker_alias(ta)[1] if ta else ""
        c["combined"], c["n_comb"] = combine_lc(c["ev"])
        del c["ev"]
    return sorted(out, key=lambda c: c["t0"])


def packets(ev):
    """data headers with the rate 1/2 (or 3/4, 1) blocks that follow on their slot ->
    [{t, slot, hdr fields, blocks: [12-byte lists], payload bytes}]"""
    out, cur = [], {}
    for d in ev:
        if d["kind"] != "data":
            continue
        sl = d["slot"]
        if d["dtype"] == 6 and "fields" in d:
            cur[sl] = {"t": d["t"], "slot": sl, "hdr": d["fields"], "crc": d["crc"], "blocks": []}
            out.append(cur[sl])
        elif d["dtype"] == 7 and sl in cur and "bytes" in d:
            p = cur[sl]
            if len(p["blocks"]) < max(p["hdr"]["blocks"], 1):
                p["blocks"].append(d["bytes"])
        elif d["dtype"] not in (7, 9):
            cur.pop(sl, None)
    for p in out:
        p["payload"] = bytes(b for blk in p["blocks"] for b in blk)
    return out


def _printable(by):
    return "".join(chr(c) if 32 <= c < 127 else "." for c in by)


# ---- commands ----

def _load(p):
    import soundfile as sf
    x, fs = sf.read(p)
    return (x if x.ndim == 1 else x[:, 0]), fs


def bursts_from_audio(x, fs, thr=0.85):
    return bursts_from_audio_y(audio_symbols(x, fs), thr)


def bursts_from_audio_y(y, thr=0.85):
    """matched-filtered samples (audio_symbols or counts_symbols) -> [(s, family, sign, rho,
    polarity, symbols, soft values)] for each sync, both polarities"""
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


def _lc_str(lc):
    return f"FLCO {lc['flco']} FID {lc['fid']} opts {lc['opts']:02x} dst {lc['dst']} src {lc['src']}"


def cmd_audio(a):
    for p in a.files:
        x, fs = _load(p)
        pol, ev = decode_stream(audio_symbols(x, fs), a.layout)
        print(f"{os.path.basename(p)}: polarity {pol:+d}, {len(ev)} bursts")
        for d in ev:
            s = f"  {d['t']:7.3f}s TS{d['slot']} "
            if d["kind"] == "voice":
                s += "voice  EMB " + " ".join(f"cc{c}/{l}{'' if e == 0 else '!' + str(e)}"
                                             for c, _, l, e in d["emb"])
                if "emb_lc" in d:
                    s += f"  embLC {'ok' if d['emb_lc']['ok'] else 'BAD'} {_lc_str(d['emb_lc'])}"
                    s += f" [{bytes(d['emb_lc']['bytes']).hex()}]"
            else:
                s += (f"CC{d['cc']} {DATA_TYPES.get(d['dtype'], d['dtype'])}"
                      f"{'' if d['golay_err'] == 0 else ' (golay %d)' % d['golay_err']}")
                if "lc" in d:
                    s += f"  {_lc_str(d['lc'])} rs {'ok' if d['lc']['rs_ok'] else d['lc']['syn']}"
                if "crc" in d:
                    s += f"  crc {','.join(d['crc']) or 'none'} [{bytes(d['bytes']).hex()}]"
                    f = d.get("fields", {})
                    if d["dtype"] == 3:
                        s += f" CSBKO {f['csbko']:02x} FID {f['fid']:02x} {f['name']}"
                        if "dst" in f:
                            s += f" dst {f['dst']} src {f['src']} blocks {f['blocks']}"
                    elif d["dtype"] == 6:
                        s += (f" DPF {f['dpf']} {f['dpf_name']} SAP {f['sap']} dst {f['dst']}"
                              f" src {f['src']} blocks {f['blocks']}")
                elif "bytes" in d:
                    by = bytes(d["bytes"])
                    s += f"  [{by.hex()}] {''.join(chr(c) if 32 <= c < 127 else '.' for c in by)}"
            print(s)


def cmd_calls(a):
    for p in a.files:
        x, fs = _load(p)
        _, ev = decode_stream(audio_symbols(x, fs), a.layout)
        print(f"{os.path.basename(p)}:")
        for c in calls(ev):
            lc = c["lc"]
            cb = c["combined"]
            s = f"  TS{c['slot']} {c['t0']:6.2f}-{c['t1']:6.2f}s "
            s += f"FLCO {lc[0]} FID {lc[1]} dst {lc[2]} src {lc[3]}" if lc else "no clean LC"
            s += f"  copies {sum(c['copies'].values())}"
            if cb:
                s += f"  combined({c['n_comb']}) {'ok' if cb['rs_ok'] else 'bad'} dst {cb['dst']} src {cb['src']}"
            if c["alias"]:
                s += f"  alias {c['alias']!r}"
            print(s)


def cmd_summary(a):
    """compact: per file, burst counts, every distinct CSBK / embedded LC, the calls and the data
    packets (for relaying hamlet's results as text)"""
    for p in a.files:
        x, fs = _load(p)
        pol, ev = decode_stream(audio_symbols(x, fs), a.layout)
        cnt, dist = {}, {}
        for d in ev:
            k = "voice" if d["kind"] == "voice" else DATA_TYPES.get(d["dtype"], str(d["dtype"]))
            cnt[k] = cnt.get(k, 0) + 1
            if d["kind"] == "voice":
                if "emb_lc" in d:
                    e = d["emb_lc"]
                    key = f"embLC {'ok ' if e['ok'] else 'BAD'} {bytes(e['bytes']).hex()} {_lc_str(e)}"
                else:
                    key = "voice EMB " + " ".join(f"{c}/{l}/{er}" for c, _, l, er in d["emb"])
            elif d["dtype"] == 3 and "bytes" in d:
                f = d["fields"]
                key = (f"CSBK crc {','.join(d['crc']) or 'none'} {bytes(d['bytes']).hex()} "
                       f"CSBKO {f['csbko']:02x} FID {f['fid']:02x} {f['name']}")
            else:
                continue
            dist[key] = dist.get(key, 0) + 1
        print(f"{os.path.basename(p)}: polarity {pol:+d} " + " ".join(f"{k}={v}" for k, v in cnt.items()))
        for k, v in dist.items():
            print(f"  {v:3d}x {k}")
        for c in calls(ev):
            lc, cb = c["lc"], c["combined"]
            print(f"  call TS{c['slot']} {c['t0']:.2f}-{c['t1']:.2f}s "
                  + (f"FLCO {lc[0]} FID {lc[1]} dst {lc[2]} src {lc[3]}" if lc else "no clean LC")
                  + f" copies {sum(c['copies'].values())}"
                  + (f" combined {'ok' if cb['rs_ok'] else 'bad'} {cb['dst']}/{cb['src']}" if cb else "")
                  + (f" alias {c['alias']!r}" if c["alias"] else ""))
        for pk in packets(ev):
            h = pk["hdr"]
            print(f"  data TS{pk['slot']} {pk['t']:.2f}s hdr crc {','.join(pk['crc']) or 'none'} DPF {h['dpf']}"
                  f" {h['dpf_name']} SAP {h['sap']} G{h['gi']} A{h['a']} dst {h['dst']} src {h['src']}"
                  f" blocks {h['blocks']}/{len(pk['blocks'])} b9 {h['b9']:02x}")
            print(f"    {pk['payload'].hex()}")
            print(f"    {_printable(pk['payload'])}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("cmd", choices=["audio", "calls", "check", "summary"])
    ap.add_argument("files", nargs="+")
    ap.add_argument("--layout", default="dpp", choices=list(GOLAY))
    a = ap.parse_args()
    {"audio": cmd_audio, "calls": cmd_calls, "check": cmd_check, "summary": cmd_summary}[a.cmd](a)


if __name__ == "__main__":
    main()
