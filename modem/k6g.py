"""PHY G: coded 4FSK for the K6's IF receiver (docs/k6-if-fec.md). The K6's pager packets
(profile XL, 128 bytes, whitened, CRC as in modem/pager.py) go as 4FSK at 1200 baud with a
rate-1/2 K=7 convolutional code, decoded on the K6 with soft decisions from the IF matched filter
(c/k6/ifrx.c) by c/k6/ifpager.c. This module is the sender and a reference receiver.

A burst:
  a lead of LEAD_SYMS symbols, tones 0 and 3 in turn (from 0): ifrx acquires on it
  then, for each packet, SYNC (24 symbols of tones 0 and 3) and the packet's 1032 coded symbols
  then TAIL_SYMS more of the lead's pattern (the last symbols' decisions, the sender's hang)
A packet's coding: its 128 bytes, MSB first, and 8 zeros (the code's 6 tail bits and 2 more) are
1032 steps of the code (polynomials 133 and 171 octal; the register shifts each bit in at the
bottom, so bit 0 of it is the newest) -> 2064 coded bits c, c[2n] from 133 and c[2n+1] from 171.
They go in 24 blocks of 86 bits: symbol i (0..42) of block b carries c[86b + i] as its Gray
value's high bit and c[86b + 43 + i] as its low bit, so a symbol's two bits are 21 steps apart.
Punctured rates (the rate code: 0 for 1/2, 1 for 2/3, 2 for 3/4; True and False stand for 1 and
0): the same code with some coded bits not sent, the receiver taking 0 for them. Rate 2/3: c[2n+1]
of each odd step n not sent, 1548 coded bits in 18 blocks, 774 symbols a packet. Rate 3/4 (the
usual K=7 pattern, 133: 1 0 1, 171: 1 1 0): of each three steps, both bits of the first, c[2n+1]
of the second, c[2n] of the third, 1376 coded bits in 16 blocks, 688 symbols. The control
packet's byte 3 says which (bit 0: 2/3, bit 2: 3/4).
Tones (PLANS): "4h", the default, 600, 1200, 1800, 2400 Hz (four tones on a half-baud grid: every
symbol starts on audio phase 0 or a half turn; 0 bit errors in 80,000 on air where the old set
made 5-6%, docs/k6-if-fec.md), or "4fsk", 900, 1500, 2100, 2700 Hz (the images before the
half-baud grid). Tone t's Gray value is t ^ (t >> 1).
MIT licence."""
import numpy as np

PLANS = {"4h": (600.0, 1200.0, 1800.0, 2400.0), "4fsk": (900.0, 1500.0, 2100.0, 2700.0)}
TONES = PLANS["4h"]
BAUD = 1200
LEVEL = 0.12                 # peak, as iflive's live runs (the IC-7100 limits its deviation
                             # past this; the model distorts at 0.15)
LEAD_SYMS = 2400             # 2.0 s: ifrx's lead steering (256 symbols) ends before the data
TAIL_SYMS = 48
SYNC = "100100000111111001101010"   # 1 = tone 3, 0 = tone 0; >= 10 symbols from any window
                                    # of the lead running into it
SYNC_MAX_MISS = 6            # the receiver takes a window with at most this many symbols wrong
PAYLOAD = 128
TAIL_BITS = 8
STEPS = PAYLOAD * 8 + TAIL_BITS       # 1032
BLOCK = 86
NBLK = 2 * STEPS // BLOCK             # 24
PKT_SYMS = STEPS                      # one symbol per step
PKT_SYMS23 = STEPS * 3 // 4           # 774: rate 2/3
PKT_SYMS34 = STEPS * 2 // 3           # 688: rate 3/4
# the coded bits each rate sends, c[2n] then c[2n+1] for each step n
_KEEP = [np.ones(2 * STEPS, bool) for _ in range(3)]
_KEEP[1][3::4] = False                # c[2n+1], n odd
_KEEP[2][2::6] = False                # c[2n], n = 1 mod 3
_KEEP[2][5::6] = False                # c[2n+1], n = 2 mod 3
_KEEP23 = _KEEP[1]
RATES = {"1/2": 0, "2/3": 1, "3/4": 2}
G0, G1 = 0o133, 0o171
SYNC_SYMS = np.array([3 if c == "1" else 0 for c in SYNC])
GRAY = np.array([0, 1, 3, 2])         # tone -> Gray value
TONE_OF = np.argsort(GRAY)            # Gray value -> tone
LLR_ONE = 32                          # the LLR a symbol as strong as the running mean gets
LLR_MAX = 127


def _parity(x):
    return bin(x).count("1") & 1


def conv_encode(bits):
    reg, out = 0, []
    for b in bits:
        reg = ((reg << 1) | int(b)) & 127
        out += [_parity(reg & G0), _parity(reg & G1)]
    return np.array(out, np.int8)


def pkt_syms(r23=0):
    """data symbols a packet at the rate code r23 (0 1/2, 1 2/3, 2 3/4)"""
    return (PKT_SYMS, PKT_SYMS23, PKT_SYMS34)[int(r23)]


def packet_symbols(payload, r23=0):
    """one packet (128 bytes, as the K6's FIFO would give it: whitened) -> 1032 tones (774 at
    rate 2/3, 688 at 3/4)"""
    assert len(payload) == PAYLOAD
    bits = np.unpackbits(np.frombuffer(bytes(payload), np.uint8))
    c = conv_encode(np.concatenate([bits, np.zeros(TAIL_BITS, np.int8)]))
    if r23:
        c = c[_KEEP[int(r23)]]
    c = c.reshape(-1, BLOCK)
    g = (c[:, :BLOCK // 2] << 1) | c[:, BLOCK // 2:]
    return TONE_OF[g.reshape(-1)]


def lead(n):
    return np.tile([0, 3], (n + 1) // 2)[:n]


def burst_symbols(payloads, lead_syms=LEAD_SYMS, tail_syms=TAIL_SYMS, r23=0):
    parts = [lead(lead_syms)]
    for p in payloads:
        parts += [SYNC_SYMS, packet_symbols(p, r23)]
    parts.append(lead(tail_syms))
    return np.concatenate(parts)


def audio(syms, fs=48000, level=LEVEL, plan="4h", baud=BAUD):
    """phase-continuous 4FSK, `level` peak: the tone changes at t = k / baud exactly and the
    phase runs on in continuous time (the same samples as a per-sample sum when a symbol is a
    whole number of them, as at 1200 baud)"""
    syms = np.asarray(syms)
    n = int(round(len(syms) * fs / baud))
    t = np.arange(n) / fs
    k = np.minimum((t * baud).astype(int), len(syms) - 1)
    f = np.asarray(PLANS[plan])[syms]
    starts = np.concatenate([[0.0], np.cumsum(f / baud)[:-1]])
    return np.sin(2 * np.pi * (starts[k] + f[k] * (t - k / baud))) * level


def burst_seconds(npackets, lead_syms=LEAD_SYMS, r23=0):
    return (lead_syms + npackets * (len(SYNC) + pkt_syms(r23)) + TAIL_SYMS) / BAUD


# ---- the receiver's side (c/k6/ifpager.c does the same in integers) ----

def llrs(met, mean):
    """one symbol's tone metrics (4) and the running mean of the winner's -> the LLRs of its
    Gray value's two bits (> 0: bit 0), as ifpager.c: max-log on |corr|, scaled so a symbol as
    strong as the mean gives LLR_ONE"""
    m = np.asarray(met, float)
    out = []
    for k in (1, 0):
        z = ((GRAY >> k) & 1) == 0
        out.append(np.clip((m[z].max() - m[~z].max()) * LLR_ONE / max(mean, 1.0), -LLR_MAX, LLR_MAX))
    return out


_n = np.arange(64)
_pred = np.stack([_n >> 1, (_n >> 1) | 32])
_reg = ((_pred << 1) | (_n & 1)) & 127
_s0 = np.array([[1 - 2 * _parity(int(r) & G0) for r in row] for row in _reg], float)
_s1 = np.array([[1 - 2 * _parity(int(r) & G1) for r in row] for row in _reg], float)


def viterbi(llr2):
    """(N, 2) LLRs -> N bits, the trellis started and ended in state 0"""
    pm = np.full(64, -1e9)
    pm[0] = 0.0
    dec = np.zeros((len(llr2), 64), np.uint8)
    for i, (a, b) in enumerate(llr2):
        cand = pm[_pred] + _s0 * a + _s1 * b
        ch = cand[1] > cand[0]
        dec[i] = ch
        pm = np.where(ch, cand[1], cand[0])
        pm -= pm.max()
    s, out = 0, np.zeros(len(llr2), np.int8)
    for i in range(len(llr2) - 1, -1, -1):
        out[i] = s & 1
        s = int(_pred[dec[i, s], s])
    return out


def decode_packet(llr, r23=0):
    """(pkt_syms(r23), 2) LLRs (high bit, low bit) in air order -> the 128 bytes"""
    l = np.asarray(llr, float).reshape(-1, BLOCK // 2, 2)
    c = np.concatenate([l[:, :, 0], l[:, :, 1]], axis=1).reshape(-1)
    if r23:
        full = np.zeros(2 * STEPS)
        full[_KEEP[int(r23)]] = c
        c = full
    bits = viterbi(c.reshape(-1, 2))[:PAYLOAD * 8]
    return np.packbits(bits).tobytes()


def receive(syms, mets, mean_shift=6, r23=0):
    """the K6's decisions (tone, 4 metrics per symbol, e.g. from ifrx) -> the packets found, in
    order, as ifpager.c finds them: sync hunt on the hard tones, a running mean of the
    winning metric, the LLRs, Viterbi"""
    out, hist, i, mean = [], [], 0, 0.0
    sync = list(SYNC_SYMS)
    while i < len(syms):
        m = mets[i]
        mean += (max(m) - mean) / (1 << mean_shift) if mean else max(m)
        hist = (hist + [int(syms[i])])[-len(sync):]
        i += 1
        if len(hist) == len(sync) and sum(a != b for a, b in zip(hist, sync)) <= SYNC_MAX_MISS:
            n = pkt_syms(r23)
            if i + n > len(syms):
                break
            ll = []
            for j in range(i, i + n):
                m = mets[j]
                mean += (max(m) - mean) / (1 << mean_shift)
                ll.append(llrs(m, mean))
            out.append(decode_packet(ll, r23))
            i += n
            hist = []
    return out
