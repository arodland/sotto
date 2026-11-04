"""sotto voice pager packets for the Baofeng UV-K6's RF chip (FD6818 / BK4819) FSK modem.

This implements the K6 voice-pager FSK packet format, version 1 (the contract shared with
the K6 receiver firmware; docs/k6-pager.md has the summary). A message is a .sot file: raw
sotto v3.7 frames, 6 bytes per 20 ms.

Profile L (primary), 64-byte payload, before whitening:

    0       magic 0xA7 (format 1, sotto v3.7 8 kHz frames)
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
from dataclasses import dataclass

import numpy as np

MAGIC = 0xA7
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
                p = bytes([MAGIC, msg_id & 0xFF]) + seq.to_bytes(2, "big") + total.to_bytes(2, "big")
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
            if p[0] != MAGIC:
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
    second_extra_ms: float = 50.0

    preamble_byte = PREAMBLE_BYTE

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
        return raw + p * self.tail_bytes

    def burst_bits(self, payloads, baud=1200):
        return np.unpackbits(np.frombuffer(self.burst(payloads, baud), np.uint8))

    def seconds(self, n_packets, payload=64, baud=1200):
        lead = int(np.ceil(self.lead_ms / 1000 * baud / 8))
        nbytes = lead + n_packets * (len(self.sync) + payload) + self.tail_bytes
        nbytes += sum(self._pre(i, baud) for i in range(n_packets))
        return nbytes * 8 / baud
