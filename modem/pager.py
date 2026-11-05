"""sotto voice pager packets for the Baofeng UV-K6's RF chip (FD6818 / BK4819) FSK modem.

This implements the K6 voice-pager FSK packet format, version 1 (the contract shared with
the K6 receiver firmware; docs/k6-pager.md has the summary). A message is a .sot file: raw
sotto v3.7 frames, 6 bytes per 20 ms, or sotto 1200 superframes (sotto_enc -1200), 6 bytes
per 40 ms. Either way a "frame" here is one 6-byte slot; the magic byte says which codec.

Profile L (primary), 64-byte payload, before whitening:

    0       magic: 0xA7 (format 1, sotto v3.7 8 kHz frames, 20 ms each) or 0xA9 (sotto 1200
            superframes, 40 ms each; docs/sotto-1200.md)
    1       msg_id
    2-3     seq, packet number, big-endian
    4-5     total frames in the message, big-endian
    6-53    8 sotto frames, slot 0 first
    54-61   reserved, zero
    62-63   CRC-16/CCITT-FALSE of bytes 0-61, big-endian

Profile XL (128-byte payload, 2026-11-04): the same 6-byte header, 20 sotto frames in bytes
6-125, no reserved bytes, CRC of bytes 0-125 in 126-127. 120 of 128 bytes are speech, against
48 of 64 in L.

Profile S (the first fallback, 16 bytes; the K6 firmware no longer takes it): msg_id (4 bits) |
last-group flag | seq bits 10-8, seq bits 7-0, 2 sotto frames, CRC of bytes 0-13.

All are then XORed with PN9 whitening (x^9 + x^5 + 1, seed 0x1FF: FF E1 1D 9A ...), which
restarts at every packet. Interleave: groups of 8 packets. In profile L packet k of group g
carries frames g*64 + s*8 + k (s = 0..7), in XL g*160 + s*8 + k (s = 0..19), in profile S frames g*16 + s*8 + k (s = 0, 1), so a
lost packet only costs isolated 20 ms frames 160 ms apart.

On air (Air): 0x55 x 16, sync 07 6B 3E 36, payload, MSB first. The chip CRC and scrambler
are off; all checking is ours. Stream framing (Air(preamble_bytes=0)): the lead-in, then each
packet as just the sync word and the payload, back to back, for a receiver that finds the sync
in software and never re-arms (the K6's key 7 stream mode).
"""
from dataclasses import dataclass, replace

import re

import numpy as np

MAGIC = 0xA7                    # sotto v3.7, 20 ms per frame
MAGIC_1200 = 0xA9               # sotto 1200, 40 ms per frame (0xA8 is the control packet)
CODECS = {"2400": (MAGIC, 0.020), "1200": (MAGIC_1200, 0.040)}     # magic, seconds per frame
FRAME_BYTES = 6
GROUP = 8                       # packets per interleave group
SYNC = bytes([0x07, 0x6B, 0x3E, 0x36])
PREAMBLE_BYTE = 0x55


def crc16_ccitt(data, crc=0xFFFF):
    """CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflection ('123456789' -> 0x29B1)."""
    for b in bytes(data):
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def pn9(n):
    """PN9 whitening bytes (TI DN509 / CC1101): low byte of the register before each 8 shifts."""
    out, s = bytearray(), 0x1FF
    for _ in range(n):
        out.append(s & 0xFF)
        for _ in range(8):
            s = (s >> 1) | (((s ^ (s >> 5)) & 1) << 8)
    return bytes(out)


WHITEN = pn9(128)


def _whiten(p):
    return bytes(a ^ b for a, b in zip(p, WHITEN))


def _split(frames):
    frames = bytes(frames)
    n = len(frames) // FRAME_BYTES
    if n * FRAME_BYTES != len(frames):
        raise ValueError("a .sot file is a whole number of 6-byte frames")
    return [frames[i * 6:i * 6 + 6] for i in range(n)]


@dataclass(frozen=True)
class Profile:
    name: str
    payload: int          # bytes per packet
    slots: int            # sotto frames per packet
    magic: int = MAGIC    # byte 0 (L and XL): which codec the frames are

    @property
    def frame_s(self):
        """Seconds of speech per frame."""
        return next(t for m, t in CODECS.values() if m == self.magic)

    @property
    def group_frames(self):
        return self.slots * GROUP

    def packet_frames(self, seq):
        g, k = divmod(seq, GROUP)
        return [g * self.group_frames + s * GROUP + k for s in range(self.slots)]

    def n_packets(self, total):
        return -(-total // self.group_frames) * GROUP

    def pack(self, frames, msg_id=0):
        """frames: bytes (6 per frame) -> list of whitened payloads in transmit order."""
        fr = _split(frames)
        total = len(fr)
        if self.name != "S" and total > 0xFFFF:
            raise ValueError(f"message too long for profile {self.name} (65535 frames)")
        npk = self.n_packets(total)
        if self.name == "S" and npk > 2048:
            raise ValueError("message too long for profile S (2048 packets)")
        out = []
        for seq in range(npk):
            body = b"".join(fr[f] if f < total else bytes(6) for f in self.packet_frames(seq))
            if self.name != "S":
                p = bytes([self.magic, msg_id & 0xFF]) + seq.to_bytes(2, "big") + total.to_bytes(2, "big")
                p += body
                p += bytes(self.payload - 2 - len(p))         # L: 8 reserved bytes
            else:
                last = int(seq // GROUP == npk // GROUP - 1)
                p = bytes([(msg_id & 15) << 4 | last << 3 | (seq >> 8) & 7, seq & 0xFF]) + body
            out.append(_whiten(p + crc16_ccitt(p).to_bytes(2, "big")))
        return out

    def unpack(self, payload):
        """Whitened payload -> dict (msg_id, seq, total or None, last, frames), or None."""
        p = _whiten(payload)
        if len(p) != self.payload or crc16_ccitt(p[:-2]) != int.from_bytes(p[-2:], "big"):
            return None
        if self.name != "S":
            if p[0] != self.magic:
                return None
            return dict(msg_id=p[1], seq=int.from_bytes(p[2:4], "big"),
                        total=int.from_bytes(p[4:6], "big"), last=None,
                        frames=[p[6 + 6 * s:12 + 6 * s] for s in range(self.slots)])
        return dict(msg_id=p[0] >> 4, seq=(p[0] & 7) << 8 | p[1], total=None,
                    last=(p[0] >> 3) & 1, frames=[p[2:8], p[8:14]])


L = Profile("L", 64, 8)
XL = Profile("XL", 128, 20)
S = Profile("S", 16, 2)
PROFILES = {"L": L, "XL": XL, "S": S}


def profile(name, codec="2400"):
    """The packet profile for a codec: "2400" (sotto v3.7) or "1200" (sotto 1200, magic 0xA9;
    L and XL only, since profile S has no magic byte)."""
    magic = CODECS[codec][0]
    if name == "S" and magic != MAGIC:
        raise ValueError("profile S carries sotto v3.7 frames only (it has no magic byte)")
    return replace(PROFILES[name], magic=magic)


# ---- control packets (spec section 13) ----------------------------------------------------
#
# A control packet changes the K6's receive settings over the air, so the PC doesn't need the
# cable's PC-to-radio wire (the PTT line). It is an L or XL payload, whitened like a voice
# packet, filled with copies of one 8-byte record: 0xA8, seq, mask, values, var | vol << 4,
# action, CRC-16 of those 6 bytes (big-endian). The K6 takes the first copy whose CRC checks,
# so copies early in the packet get through a link that slips later in it (D/1200). A record
# with a seq the K6 just applied is only a confirmation. After a change of PHY, profile, baud
# or variant, the K6 goes back to its previous settings unless a good packet (voice or
# control) arrives within 60 s, so a command can't leave it deaf.

CTL_MAGIC = 0xA8
CTL_BITS = {"phy": 1, "frames": 2, "baud": 4, "auto": 8, "emph": 16, "var": 32, "vol": 64,
            "idlekeep": 128}
CTL_ACTIONS = {"none": 0, "play": 1, "clear": 2, "report": 3, "reg": 4, "regs": 5, "boot": 6,
               "reset": 7}
# "reset" (images from db796ee): the K6 prints `K6 RESET seq=N` and reboots; tools/k6_update.py
# catches the bootloader's ~200 ms UPDATE window after it, to flash without the side keys
# "boot" (images after b1f0492): the K6 prints its stock bootloader (8 KB) as `K6 BOOT off=` lines,
# read only; boot_dump() turns a serial log of them back into bytes. That image is Baofeng's code:
# keep it on your own machine, never commit or share it
# registers action "reg" may write (the K6 refuses others): AF filters, FSK mode, FSK control
# (low byte only), CRC/scrambler, baud, (images after 647a60e) the RX bandwidth REG_43, and
# (newer images) demod gain REG_44, AFC REG_73 and the RX DC filter REG_7E, then the
# discriminator gain REG_96, RX filter bypasses REG_9B and FIFO thresholds REG_5E (a 5E write
# also switches the K6 to interrupt-driven FIFO reads)
CTL_REGS = (0x2B, 0x43, 0x44, 0x58, 0x59, 0x5C, 0x5E, 0x72, 0x73, 0x7E, 0x96, 0x9B)


def control_record(seq, settings=None, action="none"):
    """settings: {"phy": "F"|"D", "frames": "L"|"XL", "baud": 1200|2400, "auto": 0|1,
    "emph": 0|1, "var": 0..15 (key 0's receive variant), "vol": 0..7 (key Up/Down steps,
    3 = 0 dB), "idlekeep": 0|1 (no re-arm on an idle no-sync RX-done)}; only the ones given
    change."""
    mask = val = vv = 0
    for k, v in (settings or {}).items():
        if k not in CTL_BITS:
            raise ValueError(f"unknown setting {k!r} (have {', '.join(CTL_BITS)})")
        bit = CTL_BITS[k]
        mask |= bit
        if k == "phy":
            on = {"F": 1, "D": 0}[str(v).upper()]
        elif k == "frames":
            on = {"XL": 1, "L": 0}[str(v).upper()]
        elif k == "baud":
            on = {2400: 1, 1200: 0}[int(v)]
        elif k in ("auto", "emph", "idlekeep"):
            on = int(bool(int(v)))
        elif k == "var":
            vv |= int(v) & 0x0F
            on = 0
        else:  # vol
            vv |= (min(int(v), 7) & 0x0F) << 4
            on = 0
        val |= bit if on else 0
    r = bytes([CTL_MAGIC, seq & 0xFF, mask, val, vv, CTL_ACTIONS[action]])
    c = crc16_ccitt(r)
    return r + bytes([c >> 8, c & 0xFF])


def control_reg_record(seq, reg, value):
    """Action reg: write `value` to FD6818 register `reg` (0: drop every write), then a REGS line.
    The K6 undoes the writes 60 s later unless a good packet arrives, like a PHY change."""
    if reg and reg not in CTL_REGS:
        raise ValueError(f"register {reg:#x} is not one the K6 lets a control packet write "
                         f"({', '.join(f'{r:#x}' for r in CTL_REGS)})")
    r = bytes([CTL_MAGIC, seq & 0xFF, reg, value >> 8 & 0xFF, value & 0xFF, CTL_ACTIONS["reg"]])
    c = crc16_ccitt(r)
    return r + bytes([c >> 8, c & 0xFF])


def control_packet(seq, settings=None, action="none", length=64, reg=None):
    """A whitened control payload: the record repeated to fill `length` (64 = L, 128 = XL).
    reg=(register, value) makes a register write instead of a settings change."""
    rec = control_reg_record(seq, *reg) if reg else control_record(seq, settings, action)
    return _whiten(rec * (length // len(rec)))


def parse_control(payload):
    """The first record whose CRC checks in a (whitened) payload, as the K6 reads it, or None."""
    d = _whiten(payload)
    for o in range(0, len(d) - 7, 8):
        r = d[o:o + 8]
        if r[0] == CTL_MAGIC and crc16_ccitt(r[:6]) == (r[6] << 8 | r[7]):
            return r[:6]
    return None


def swap_words(p):
    """Swap the two bytes of every 16-bit word."""
    p = bytes(p)
    return bytes(p[i ^ 1] for i in range(len(p) & ~1)) + p[len(p) & ~1:]


class Assembler:
    """Collects payloads (any pass, any order) into messages: frames + per-frame good flags.

    A packet with a bad CRC or magic is counted and dropped. A new msg_id starts a new
    message, as on the radio; `messages` keeps them all here for testing."""

    def __init__(self, profile=L):
        self.profile, self.bad, self.swapped, self.messages = profile, 0, 0, {}

    def push(self, payload):
        """-> the unpacked packet, or None. A payload whose 16-bit words arrive byte-swapped
        (an FD6818 transmitting its FIFO low byte first, as the K6's own test burst does) is
        accepted too and counted in `swapped`."""
        q = self.profile.unpack(payload)
        if q is None:
            q = self.profile.unpack(swap_words(payload))
            if q is None:
                self.bad += 1
                return None
            self.swapped += 1
        m = self.messages.setdefault(q["msg_id"], {"total": None, "last_seq": None, "pk": {}})
        if q["total"] is not None:
            m["total"] = q["total"]
        if q["last"]:
            m["last_seq"] = max(m["last_seq"] or 0, q["seq"])
        m["pk"][q["seq"]] = q["frames"]
        return q

    def frames(self, msg_id):
        """-> (frames bytes, good uint8 per frame) for one message."""
        m = self.messages[msg_id]
        total = m["total"]
        if total is None:      # profile S: up to the end of the last group seen, minus zero padding
            top = max(m["pk"]) if m["last_seq"] is None else m["last_seq"] | (GROUP - 1)
            total = (top // GROUP + 1) * self.profile.group_frames
        fr, good = [bytes(6)] * total, np.zeros(total, np.uint8)
        for seq, body in m["pk"].items():
            for s, f in enumerate(self.profile.packet_frames(seq)):
                if f < total:
                    fr[f], good[f] = body[s], 1
        if m["total"] is None and m["last_seq"] is not None:
            while total and good[total - 1] and fr[total - 1] == bytes(6):
                total -= 1
            fr, good = fr[:total], good[:total]
        return b"".join(fr), good


@dataclass
class Air:
    """What goes on air around each payload (spec sections 3 and 7).

    lead_ms of 0x55 after keying, then per packet 0x55 x preamble_bytes, the sync word and
    the payload; the carrier stays up with 0x55 between packets (the next preamble); 2 bytes
    of 0x55 before unkeying. preamble_bytes=0 is the stream framing: packets back to back
    with only the sync word between them.

    second_extra_ms more 0x55 go before the second packet only: the K6 usually notices the
    first packet's sync late (its main loop redraws the screen for 41 ms), so it re-arms late
    after it and missed the second packet every time (f190a46: 15/16, always seq 1). From the
    second packet on it waits in a tight loop and is on time.

    later_preamble_bytes (None: preamble_bytes) is the preamble of every packet after the first.
    XL wants 24 (modem.k6 sets it): after a 128-byte packet the FD6818's own RX-done comes about
    144 ms after the K6's timed read ends, and with 16 bytes the K6's re-arm then false-synced
    in the next preamble (b0ff399: 5-6/8); 24 or 32 everywhere false-synced on the first packet
    instead (a long 0x55 run before the first sync, as with a 400 ms lead-in), 7/8; 16 first and
    24 after: 8/8, 0 bit errors."""
    sync: bytes = SYNC
    preamble_bytes: int = 16
    later_preamble_bytes: int | None = None
    lead_ms: float = 150.0
    tail_bytes: int = 2
    tail_ms: float = 300.0
    second_extra_ms: float = 50.0

    preamble_byte = PREAMBLE_BYTE

    def _tail(self, baud):
        """0x55 bytes after the last packet: tail_ms of them, at least tail_bytes. With 2 bytes
        the K6 heard the last packet of a burst far less often than the others (2026-11-05, 3-packet
        bursts at 2400: first 50 of 76, middle 43, last 29): the PC's audio chain was still
        playing when PTT dropped. With 300 ms of tail and PTT held 0.5 s after the player exits
        it heard the last packet in 22 of 24 bursts."""
        return max(self.tail_bytes, int(np.ceil(self.tail_ms / 1000 * baud / 8)))

    def _extra(self, baud):
        return int(np.ceil(self.second_extra_ms / 1000 * baud / 8))

    def _pre(self, i, baud):
        """0x55 bytes before packet i."""
        if i == 0 or self.later_preamble_bytes is None:
            n = self.preamble_bytes
        else:
            n = self.later_preamble_bytes
        return n + (self._extra(baud) if i == 1 else 0)

    def burst(self, payloads, baud=1200):
        lead = int(np.ceil(self.lead_ms / 1000 * baud / 8))
        p = bytes([PREAMBLE_BYTE])
        raw = p * lead + b"".join(p * self._pre(i, baud) + self.sync + bytes(x)
                                  for i, x in enumerate(payloads))
        return raw + p * self._tail(baud)

    def burst_bits(self, payloads, baud=1200):
        return np.unpackbits(np.frombuffer(self.burst(payloads, baud), np.uint8))

    def seconds(self, n_packets, payload=64, baud=1200):
        lead = int(np.ceil(self.lead_ms / 1000 * baud / 8))
        nbytes = lead + n_packets * (len(self.sync) + payload) + self._tail(baud)
        nbytes += sum(self._pre(i, baud) for i in range(n_packets))
        return nbytes * 8 / baud


def boot_dump(text, size=0x2000):
    """The bytes in `K6 BOOT off=XXXX <hex>` lines of a serial log (the last copy of each line
    wins); raises if any of the `size` bytes is missing."""
    out = bytearray(size)
    seen = bytearray(size)
    for m in re.finditer(r"K6 BOOT off=([0-9a-f]{4}) ([0-9a-f]+)", text):
        off, data = int(m.group(1), 16), bytes.fromhex(m.group(2))
        if off + len(data) <= size:
            out[off:off + len(data)] = data
            seen[off:off + len(data)] = b"\x01" * len(data)
    gaps = [i for i in range(0, size, 32) if not all(seen[i:i + 32])]
    if gaps:
        raise ValueError(f"missing {len(gaps)} lines, first at off={gaps[0]:04x}")
    return bytes(out)
