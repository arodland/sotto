"""FT8 transmit side as a reference for the K6 receiver (docs/k6-ft8-wspr.md): pack a standard
message, add the CRC-14, LDPC-encode it, make the 79 tones and synthesize GFSK audio.

Follows ft8_lib (kgoba/ft8_lib, MIT): ft8/message.c (pack28, packgrid), ft8/crc.c,
ft8/encode.c, and WSJT-X's GFSK (BT 2). The LDPC tables come from c/k6/ft8/ft8k_tables.h, so
the C receiver and this encoder can't disagree about the code. MIT licence."""
import os
import re

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
TABLES = os.path.join(HERE, "..", "..", "c", "k6", "ft8", "ft8k_tables.h")
COSTAS = [3, 1, 4, 0, 6, 5, 2]
GRAY = [0, 1, 3, 2, 5, 6, 4, 7]
NTOKENS = 2063592
MAX22 = 4194304
MAXGRID4 = 32400
A37 = " 0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
A36 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
A27 = " ABCDEFGHIJKLMNOPQRSTUVWXYZ"
SYM = 0.16
SPACING = 6.25


def _tables():
    src = open(TABLES).read()

    def grab(name):
        body = re.search(name + r"\[[^]]*\]\[[^]]*\]\s*=\s*\{(.*?)\};", src, re.S).group(1)
        return [[int(v, 0) for v in r.split(",") if v.strip()] for r in re.findall(r"\{([^{}]*)\}", body)]
    gen = np.array(grab("FK_GEN"), np.uint8)
    nm = np.array(grab("FK_NM"), np.int32)
    return gen, nm


GEN, NM = _tables()


def pack28(call):
    if call == "DE":
        return 0
    if call == "QRZ":
        return 1
    if call == "CQ":
        return 2
    c6 = None
    n = len(call)
    if n > 2 and call[2].isdigit() and n <= 6:
        c6 = call.ljust(6)
    elif n > 1 and call[1].isdigit() and n <= 5:
        c6 = (" " + call).ljust(6)
    if c6 is None:
        raise ValueError("not a standard callsign: " + call)
    i = [A37.index(c6[0]), A36.index(c6[1]), "0123456789".index(c6[2]),
         A27.index(c6[3]), A27.index(c6[4]), A27.index(c6[5])]
    v = i[0]
    for x, m in zip(i[1:], (36, 10, 27, 27, 27)):
        v = v * m + x
    return NTOKENS + MAX22 + v


def packgrid(extra):
    if not extra:
        return MAXGRID4 + 1
    special = {"RRR": 2, "RR73": 3, "73": 4}
    if extra in special:
        return MAXGRID4 + special[extra]
    if (len(extra) == 4 and "A" <= extra[0] <= "R" and "A" <= extra[1] <= "R"
            and extra[2:].isdigit()):
        return ((ord(extra[0]) - 65) * 18 + ord(extra[1]) - 65) * 100 + int(extra[2:])
    if extra[0] == "R":
        return (MAXGRID4 + 35 + int(extra[1:])) | 0x8000
    return MAXGRID4 + 35 + int(extra)


def pack(text):
    """a standard (i3 = 1) message "CALL1 CALL2 [GRID|REPORT|RRR|RR73|73]" -> 77 bits"""
    f = text.split()
    a, b = pack28(f[0]), pack28(f[1])
    g = packgrid(f[2] if len(f) > 2 else "")
    ir, g = g >> 15, g & 0x7FFF
    bits = []
    for v, n in ((a, 28), (0, 1), (b, 28), (0, 1), (ir, 1), (g, 15), (1, 3)):
        bits += [(v >> (n - 1 - k)) & 1 for k in range(n)]
    return np.array(bits, np.uint8)


def _crc14_ftx(bits82):
    """ft8_lib's ftx_compute_crc over 82 bits, byte-wise (MSB first): the same remainder"""
    msg = np.zeros(12, np.uint8)
    for i, b in enumerate(bits82):
        msg[i >> 3] |= b << (7 - (i & 7))
    rem = 0
    for i in range(82):
        if i % 8 == 0:
            rem ^= int(msg[i >> 3]) << 6
        rem = ((rem << 1) ^ 0x2757) if rem & 0x2000 else (rem << 1)
    return rem & 0x3FFF


def add_crc(b77):
    c = _crc14_ftx(np.concatenate([b77, np.zeros(5, np.uint8)]))
    return np.concatenate([b77, [(c >> (13 - k)) & 1 for k in range(14)]]).astype(np.uint8)


def ldpc_encode(b91):
    m = np.zeros(96, np.uint8)
    m[:91] = b91
    mb = np.packbits(m)
    par = [int(np.unpackbits(mb & GEN[i]).sum() & 1) for i in range(83)]
    return np.concatenate([b91, par]).astype(np.uint8)


def checks_ok(c174):
    for row in NM:
        if sum(int(c174[n - 1]) for n in row if n) & 1:
            return False
    return True


def tones_from_bits(c174):
    data = [GRAY[4 * c174[3 * k] + 2 * c174[3 * k + 1] + c174[3 * k + 2]] for k in range(58)]
    return np.array(COSTAS + data[:29] + COSTAS + data[29:] + COSTAS, np.int32)


def encode(text):
    """text -> (77 message bits, 79 tones)"""
    b77 = pack(text)
    c = ldpc_encode(add_crc(b77))
    assert checks_ok(c)
    return b77, tones_from_bits(c)


def gfsk_phase(tones, fs, f0, bt=2.0, ramp=True):
    """phase (radians) of FT8 GFSK at fs Hz, base tone f0 Hz (WSJT-X gen_ft8wave): returns
    the samples' phase and an amplitude envelope with 1/8-symbol cosine ramps at the ends"""
    from scipy.special import erf
    nsps = int(round(fs * SYM))
    k = np.pi * np.sqrt(2 / np.log(2))
    t = (np.arange(3 * nsps) - 1.5 * nsps) / nsps
    pulse = 0.5 * (erf(k * bt * (t + 0.5)) - erf(k * bt * (t - 0.5)))
    dphi = np.zeros((len(tones) + 2) * nsps)
    hmod = 2 * np.pi / nsps                      # one tone step = 6.25 Hz
    for j, tn in enumerate(tones):
        dphi[j * nsps:(j + 3) * nsps] += hmod * tn * pulse
    dphi[0:2 * nsps] += hmod * tones[0] * pulse[nsps:3 * nsps]
    dphi[len(tones) * nsps:] += hmod * tones[-1] * pulse[0:2 * nsps]
    dphi = dphi[nsps:nsps + len(tones) * nsps] + 2 * np.pi * f0 / fs
    ph = np.concatenate([[0.0], np.cumsum(dphi)[:-1]])
    env = np.ones(len(ph))
    if ramp:
        nr = nsps // 8
        r = 0.5 * (1 - np.cos(np.pi * np.arange(nr) / nr))
        env[:nr] = r
        env[-nr:] = r[::-1]
    return ph, env


def unpack_text(b77):
    """77 bits -> text, for standard (i3 1/2) messages; else hex"""
    v = int("".join(str(int(b)) for b in b77), 2)
    i3 = v & 7
    if i3 not in (1, 2):
        return "%020x" % v

    def call(n):
        if n < NTOKENS:
            return {0: "DE", 1: "QRZ", 2: "CQ"}.get(n, "CQ?%d" % n)
        n -= NTOKENS
        if n < MAX22:
            return "<...>"
        n -= MAX22
        out = []
        for m, a in ((27, A27), (27, A27), (27, A27), (10, "0123456789"), (36, A36), (37, A37)):
            out.append(a[n % m])
            n //= m
        return "".join(reversed(out)).strip()
    na, pa = v >> 49, (v >> 48) & 1
    nb, pb = (v >> 20) & 0xFFFFFFF, (v >> 19) & 1
    ir, g = (v >> 18) & 1, (v >> 3) & 0x7FFF
    a, b = call(na), call(nb)
    if pa and i3 == 1:
        a += "/R"
    if pb and i3 == 1:
        b += "/R"
    if g <= MAXGRID4:
        n = g
        grid = chr(65 + n // 1800) + chr(65 + (n // 100) % 18) + "%02d" % (n % 100)
        extra = ("R " if ir else "") + grid
    else:
        r = g - MAXGRID4
        extra = {1: "", 2: "RRR", 3: "RR73", 4: "73"}.get(r)
        if extra is None:
            extra = ("R" if ir else "") + "%+03d" % (r - 35)
    return (a + " " + b + " " + extra).strip()


if __name__ == "__main__":
    import sys
    for t in sys.argv[1:] or ["CQ AG7EW CN87"]:
        b, tn = encode(t)
        print(t, "->", unpack_text(b), "".join(map(str, tn)))
