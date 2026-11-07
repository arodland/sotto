"""sotto voice pager packets for the Baofeng UV-K6's RF chip (FD6818 / BK4819) FSK modem.

This implements the K6 voice-pager FSK packet format, version 1 (the contract shared with
the K6 receiver firmware; docs/k6-pager.md has the summary). A message is a .sot file: raw
sotto v3.7 frames, 6 bytes per 20 ms, or sotto 1200 superframes (sotto_enc -1200), 6 bytes
per 40 ms, or sotto 600 / 400 superframes (sotto_enc -600 / -400; docs/sotto-vlr.md), 6 bytes per
80 / 120 ms. Either way a "frame" here is one 6-byte slot; the magic byte says which codec.

Profile L (primary), 64-byte payload, before whitening:

    0       magic: 0xA7 (format 1, sotto v3.7 8 kHz frames, 20 ms each), 0xA9 (sotto 1200
            superframes, 40 ms each; docs/sotto-1200.md), 0xAC (sotto 600, 80 ms each) or
            0xAD (sotto 400, 120 ms each; docs/sotto-vlr.md)
    1       msg_id
    2-3     big-endian: bits 15-14 the interleave depth (0: 8 packets per group, 1: 4, 2: 2,
            3: 1; receivers before 2026-11-05 know only 0), bit 13 set on the packets of a
            short last group (2026-11-07, below), bits 12-0 seq, the packet number
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
restarts at every packet. Interleave: groups of D packets (D = 8 unless the header says 4, 2
or 1; profile S is always 8). Packet k of group g carries frames g*F*D + s*D + k (s = 0..F-1,
F = 8 frames per packet in L, 20 in XL, 2 in S), so a lost packet only costs isolated frames D
slots apart. A smaller D lets a streaming receiver start sooner (a group is in after D packets)
but spaces a lost packet's frames closer together (D = 1: a lost packet is one 20-frame gap).

Short last group (2026-11-07): when the last group has only rem < F*D frames, it takes
m = ceil(rem / F) packets instead of D (the rest were padding), seqs G*D .. G*D+m-1, packet k
of it carrying frames G*F*D + s*m + k; those packets have header bit 13 set, and the receiver
gets m from total. Full groups are as before, so a receiver from before this change plays every
full group and loses only the short one (its seqs read as 8192 and up). Profile(short_last=False)
sends the old padded layout. On XL this is about 9% fewer packets on real clips.

Parity packets (optional, `Profile(parity=True)`, `modem.k6 tx --parity`; spec section 14): after
the D packets of each interleave group comes one more L or XL packet whose byte 0 is the codec's
magic | 0x10 (0xB7, 0xB9, 0xBC, 0xBD), bytes 1-5 the header with the group number g in place of
seq (bits 15-14 the depth as in a data packet), and bytes 6 up to the CRC the XOR of the group's
data packets' bytes in the same places (their frames, zero padding and L's reserved bytes). A
receiver missing exactly one of the group's packets rebuilds it: the parity XOR the others.
Receivers that don't know it drop it (an unknown magic). It costs 1/D more air time (12.5 % at
the usual depth 8) and turns a group's single lost packet from concealed frames into exact ones.

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
MAGIC_600 = 0xAC                # sotto 600, 80 ms per frame (0xAA: k6_update, 0xAB: RAM modules)
MAGIC_400 = 0xAD                # sotto 400, 120 ms per frame
PARITY = 0x10                   # magic | PARITY: the parity packet of an interleave group
CODECS = {"2400": (MAGIC, 0.020), "1200": (MAGIC_1200, 0.040),     # magic, seconds per frame
          "600": (MAGIC_600, 0.080), "400": (MAGIC_400, 0.120)}
FRAME_BYTES = 6
HDR = 6                         # magic, msg_id, seq (2), total (2)
GROUP = 8                       # packets per interleave group, unless the header says otherwise
DEPTHS = (8, 4, 2, 1)           # by the depth code in seq's top 2 bits
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
    depth: int = GROUP    # packets per interleave group: 8, 4, 2 or 1 (L and XL)
    short_last: bool = True  # a partial last group takes only the packets it fills (seq bit 13)
    parity: bool = False  # pack: one XOR parity packet after each group (L and XL)

    @property
    def frame_s(self):
        """Seconds of speech per frame."""
        return next(t for m, t in CODECS.values() if m == self.magic)

    @property
    def group_frames(self):
        return self.slots * self.depth

    def last_group(self, total):
        """(first seq of the last group, its packet count m) when that group is short: with
        short_last, a last group of rem < F*D frames takes m = ceil(rem / F) packets instead of D,
        packet k of it carrying frames G*F*D + s*m + k. (None, 0) when the layout is the plain one
        (no partial group, m = D anyway, short_last off, or profile S)."""
        if not self.short_last or self.name == "S":
            return None, 0
        g, rem = divmod(total, self.group_frames)
        m = -(-rem // self.slots)
        return (g * self.depth, m) if 0 < m < self.depth else (None, 0)

    def packet_frames(self, seq, total=None):
        """The frames packet seq carries (given total, the short last group's layout too)."""
        g, k = divmod(seq, self.depth)
        if total is not None:
            first, m = self.last_group(total)
            if first is not None and seq >= first:
                return [g * self.group_frames + s * m + seq - first for s in range(self.slots)]
        return [g * self.group_frames + s * self.depth + k for s in range(self.slots)]

    def n_packets(self, total):
        """Data packets (seqs) in a message of `total` frames; parity packets come on top."""
        first, m = self.last_group(total)
        return first + m if first is not None else -(-total // self.group_frames) * self.depth

    def group_seqs(self, g, total):
        """The seqs of interleave group g's data packets: what its parity packet covers."""
        return range(g * self.depth, min((g + 1) * self.depth, self.n_packets(total)))

    def air_packets(self, total):
        """Packets on air for one pass: the data packets and, with parity, one per group."""
        n = self.n_packets(total)
        return n + (-(-n // self.depth) if self.parity else 0)

    def _header(self, magic, msg_id, field, total):
        hs = DEPTHS.index(self.depth) << 14 | field
        return bytes([magic, msg_id & 0xFF]) + hs.to_bytes(2, "big") + total.to_bytes(2, "big")

    def _seal(self, p):
        p += bytes(self.payload - 2 - len(p))                # L: 8 reserved bytes
        return _whiten(p + crc16_ccitt(p).to_bytes(2, "big"))

    def pack(self, frames, msg_id=0):
        """frames: bytes (6 per frame) -> list of whitened payloads in transmit order."""
        fr = _split(frames)
        total = len(fr)
        if self.name != "S" and total > 0xFFFF:
            raise ValueError(f"message too long for profile {self.name} (65535 frames)")
        npk = self.n_packets(total)
        if self.depth not in DEPTHS or (self.name == "S" and self.depth != GROUP):
            raise ValueError(f"interleave depth {self.depth}: L and XL take 8, 4, 2 or 1, S only 8")
        if self.name != "S" and npk > 0x2000:
            raise ValueError("message too long: seq has 13 bits (bit 13 marks the short last group)")
        if self.name == "S" and npk > 2048:
            raise ValueError("message too long for profile S (2048 packets)")
        if self.parity and self.name == "S":
            raise ValueError("profile S has no magic byte, so no parity packets")
        out = []
        first, _ = self.last_group(total)
        acc = None
        for seq in range(npk):
            body = b"".join(fr[f] if f < total else bytes(6) for f in self.packet_frames(seq, total))
            if self.name != "S":
                short = first is not None and seq >= first
                p = self._header(self.magic, msg_id, short << 13 | seq, total) + body
                out.append(self._seal(p))
                if self.parity:      # XOR of bytes 6 .. payload-3 of the group's packets
                    d = _whiten(out[-1])[HDR:-2]
                    acc = d if acc is None else bytes(a ^ b for a, b in zip(acc, d))
                    if (seq + 1) % self.depth == 0 or seq == npk - 1:
                        g = seq // self.depth
                        out.append(self._seal(self._header(self.magic | PARITY, msg_id, short << 13 | g, total) + acc))
                        acc = None
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
            if p[0] not in (self.magic, self.magic | PARITY):
                return None
            q = dict(msg_id=p[1], seq=int.from_bytes(p[2:4], "big") & 0x1FFF,
                     depth=DEPTHS[p[2] >> 6], short=bool(p[2] & 0x20), total=int.from_bytes(p[4:6], "big"),
                     last=None, frames=[p[6 + 6 * s:12 + 6 * s] for s in range(self.slots)], parity=None)
            if p[0] & PARITY:        # a parity packet: seq is the group, the rest the XOR
                q.update(group=q.pop("seq"), parity=p[HDR:-2], frames=None)
            return q
        return dict(msg_id=p[0] >> 4, seq=(p[0] & 7) << 8 | p[1], depth=GROUP, short=False, total=None,
                    last=(p[0] >> 3) & 1, frames=[p[2:8], p[8:14]], parity=None)


L = Profile("L", 64, 8)
XL = Profile("XL", 128, 20)
S = Profile("S", 16, 2)
PROFILES = {"L": L, "XL": XL, "S": S}


def energy_codes(frames, codec="2400"):
    """Each 6-byte slot's 5-bit energy code (0 = silence; code c is 10 + 70c/31 - 87.3 dBFS): bits
    9-13 for sotto v3.7 and sotto 1200 (its A frame), the superframe's energy mean (bits 13-17)
    for sotto 600 / 400. The same fields the K6's automatic gain reads."""
    b = np.frombuffer(bytes(frames), np.uint8).reshape(-1, FRAME_BYTES).astype(np.uint32)
    if codec in ("600", "400"):
        return ((b[:, 1] << 16 | b[:, 2] << 8 | b[:, 3]) >> 14) & 31
    return ((b[:, 1] << 8 | b[:, 2]) >> 10) & 31


TRIM_BELOW = 15       # codes under the message's 98th-percentile level: 15 x 2.26 = 34 dB


def trim_silence(frames, codec="2400", keep_s=0.06, below=TRIM_BELOW):
    """Dead air cut from both ends of a message: slots whose energy code is `below` codes or more
    under the message's 98th-percentile code (or 0) count as silent, and at most keep_s of them
    stay before the first and after the last loud slot (at least one slot). The K6's auto gain
    lifts the peak to -13 dBFS, so what is cut would play at -47 dBFS or lower, under the speaker's
    own noise (silent-frame-skipping memo, 2026-11-04). -> (frames, slots cut at the start,
    slots cut at the end). A message with nothing loud in it is left as it is."""
    frames = bytes(frames)
    n = len(frames) // FRAME_BYTES
    if n == 0:
        return frames, 0, 0
    e = energy_codes(frames, codec).astype(int)
    peak = int(np.percentile(e, 98, method="higher"))
    loud = np.flatnonzero((e > peak - below) & (e > 0))
    if peak == 0 or len(loud) == 0:
        return frames, 0, 0
    keep = max(1, int(np.ceil(keep_s / CODECS[codec][1] - 1e-9)))
    a, b = max(0, int(loud[0]) - keep), min(n, int(loud[-1]) + 1 + keep)
    return frames[a * FRAME_BYTES:b * FRAME_BYTES], a, n - b


# ---- pause frames (2026-11-07): one slot stands for a run of silent slots ------------------
#
# A pause frame is a 6-byte slot: voicing 0 (unvoiced), Wo 127, energy 0, then a 10-bit count N
# (bits 14-23) and the 24-bit marker PAUSE_MARKER (bits 24-47). A K6 from 2026-11-07 plays N slots
# of fill in its place: the last real slot before it again, with that slot's good flag (so a lost
# one is concealed, never a held speech frame). Older receivers decode it as one silent slot
# (energy 0; in sotto 1200 the B half's energy step is -6 and its voicing unvoiced too). sotto
# 2400 and sotto 1200 only (sotto 600 / 400 lay their bits out differently).

PAUSE_MARKER = 0xA5304B      # bits 32-33 (1200 voicing B) = 00, bits 37-39 (1200 energy B step) = 000
PAUSE_MAX = 1023
PAUSE_CODECS = ("2400", "1200")
_PAUSE_HEAD = 0x3F80         # bits 0-13: voicing 00, Wo 1111111, energy 00000 (as the top 14 of 16)


def pause_frame(n):
    """The pause slot standing for n (1..1023) slots of fill."""
    if not 1 <= n <= PAUSE_MAX:
        raise ValueError(f"a pause frame holds 1..{PAUSE_MAX} slots")
    v = (_PAUSE_HEAD >> 2) << 34 | n << 24 | PAUSE_MARKER
    return v.to_bytes(6, "big")


def pause_count(slot):
    """N if this 6-byte slot is a pause frame, else 0."""
    v = int.from_bytes(bytes(slot), "big")
    if v >> 34 != _PAUSE_HEAD >> 2 or v & 0xFFFFFF != PAUSE_MARKER:
        return 0
    return v >> 24 & 0x3FF


def _voicing_codes(slot, codec):
    """The 2-bit voicing codes a slot feeds the decoder, in order (sotto 1200: A, then B)."""
    v = int.from_bytes(bytes(slot), "big")
    return (v >> 46,) if codec == "2400" else (v >> 46, v >> 14 & 3)


def _prev_v(state, codes):
    """The decoder's prev_v (second-half voicing) after these codes: 0..2 as they are, code 3 the
    edge whose voiced half the previous frame decides (sotto_fx_dec.c)."""
    for c in codes:
        state = c if c < 3 else (2 if state == 0 else 0)
    return state


def compact_pauses(frames, codec="2400", below=TRIM_BELOW, min_run=4):
    """Runs of min_run or more silent slots (as trim_silence: `below` codes under the 98th
    percentile; sotto 1200 needs its next slot's A energy quiet too, since B interpolates to it)
    become their first slot plus a pause frame: the receiver replays the first slot in the gap.
    When replaying it would leave the decoder's voicing state (prev_v) other than the real run
    leaves it, the run's last slot is sent too. Real slots that look like a pause frame's head
    get Wo 126. -> (frames, slots saved). Other codecs: unchanged."""
    frames = bytes(frames)
    sl = _split(frames)
    if codec not in PAUSE_CODECS or not sl:
        return frames, 0
    e = energy_codes(frames, codec).astype(int)
    peak = int(np.percentile(e, 98, method="higher"))
    quiet = (e <= peak - below) | (e == 0)
    silent = quiet.copy() if codec == "2400" else quiet & np.append(quiet[1:], True)
    fix = []
    for f in sl:          # Wo 127 + energy 0 is the pause head: move real slots off it
        v = int.from_bytes(f, "big")
        if (v >> 34) & 0xFFF == (_PAUSE_HEAD >> 2) & 0xFFF:
            v ^= 1 << 39  # Wo 127 -> 126 (still silent: energy 0)
        fix.append(v.to_bytes(6, "big"))
    sl = fix
    out, state, i, n = [], 0, 0, len(sl)
    while i < n:
        if not silent[i]:
            out.append(sl[i])
            state = _prev_v(state, _voicing_codes(sl[i], codec))
            i += 1
            continue
        j = i
        while j < n and silent[j]:
            j += 1
        run = sl[i:j]
        real = state
        for f in run:
            real = _prev_v(real, _voicing_codes(f, codec))
        hold = _voicing_codes(run[0], codec)
        plan = None
        if len(run) >= min_run:
            if _prev_v(state, hold * len(run)) == real:
                plan = (len(run) - 1, False)
            elif len(run) >= min_run + 1 and \
                    _prev_v(_prev_v(state, hold * (len(run) - 1)), _voicing_codes(run[-1], codec)) == real:
                plan = (len(run) - 2, True)
        if plan is None:
            out.extend(run)
        else:
            gap, last = plan
            out.append(run[0])
            while gap > 0:
                k = min(gap, PAUSE_MAX)
                out.append(pause_frame(k))
                gap -= k
            if last:
                out.append(run[-1])
        state = real
        i = j
    return b"".join(out), n - len(out)


def expand_pauses(frames, good=None, codec="2400"):
    """Undo compact_pauses as the K6 plays it: each good pause frame becomes N copies of the last
    real slot before it, with that slot's good flag. -> (frames, good as uint8)."""
    sl = _split(frames)
    good = np.ones(len(sl), np.uint8) if good is None else np.asarray(good, np.uint8)
    if codec not in PAUSE_CODECS:
        return bytes(frames), good
    out, og, last = [], [], None
    for f, g in zip(sl, good):
        k = pause_count(f) if g else 0
        if k and last is not None:
            out += [last[0]] * k
            og += [last[1]] * k
        else:
            out.append(f)
            og.append(int(g))
            last = (f, int(g))
    return b"".join(out), np.array(og, np.uint8)


def profile(name, codec="2400", depth=GROUP, short_last=True, parity=False):
    """The packet profile for a codec: "2400" (sotto v3.7), "1200" (sotto 1200, magic 0xA9), "600"
    or "400" (0xAC, 0xAD; the last three L and XL only, since profile S has no magic byte), with
    `depth` packets per interleave group
    (8, 4, 2 or 1; S only 8), and with `parity` an XOR parity packet after each group (L and XL)."""
    magic = CODECS[codec][0]
    if name == "S" and magic != MAGIC:
        raise ValueError("profile S carries sotto v3.7 frames only (it has no magic byte)")
    if depth not in DEPTHS or (name == "S" and depth != GROUP):
        raise ValueError(f"interleave depth {depth}: L and XL take 8, 4, 2 or 1, S only 8")
    if parity and name == "S":
        raise ValueError("profile S has no magic byte, so no parity packets")
    return replace(PROFILES[name], magic=magic, depth=depth, short_last=short_last and name != "S",
                   parity=parity)


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
               "reset": 7, "stream-on": 8, "stream-off": 9, "ifrx": 10,
               "sweep": 11}
# "ifrx" (images from 2026-11-07 that aren't lean): a coded 4FSK receive session on the IF for up
# to byte 4 x 2 s (modem/k6g.py, PHY G): the burst follows the control packet on the same carrier
# "sweep" (images built after 2026-11-07 07:30): the K6 plays its speaker sweep (c/k6/sotto_pager.h
# sp_sweep_pwm: 1 s silence, then 14 tones of 1 s, 250 Hz to 7.5 kHz, 0.25 s apart, at 16 kHz PWM
# updates), printing `K6 SWEEP start` and `K6 SWEEP done`; record the speaker, run tools/k6_sweep.py
# "stream-on" / "stream-off" (images with streaming): sotto 1200 messages play while their later
# interleave groups arrive (the sotto 1200 image's default), or store then play as before; from
# the next new message, RAM only
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


def control_record(seq, settings=None, action="none", arg=None):
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
    if arg is not None:   # an action's argument in byte 4 (ifrx: the session's length / 2 s)
        vv = int(arg) & 0xFF
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


def control_packet(seq, settings=None, action="none", length=64, reg=None, arg=None):
    """A whitened control payload: the record repeated to fill `length` (64 = L, 128 = XL).
    reg=(register, value) makes a register write instead of a settings change; arg: byte 4."""
    rec = control_reg_record(seq, *reg) if reg else control_record(seq, settings, action, arg)
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
    message, as on the radio; `messages` keeps them all here for testing. Parity packets are
    kept per group; `frames` rebuilds a group's one missing packet from its parity (counted in
    `rebuilt`), as the K6 does."""

    def __init__(self, profile=L):
        self.profile, self.bad, self.swapped, self.messages = profile, 0, 0, {}
        self.rebuilt = 0

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
        m = self.messages.setdefault(q["msg_id"], {"total": None, "last_seq": None, "pk": {},
                                                   "depth": q["depth"], "short": False, "parity": {}})
        m["short"] |= q["short"]
        if q["total"] is not None:
            m["total"] = q["total"]
        if q["parity"] is not None:
            m["parity"][q["group"]] = q["parity"]
            return q
        if q["last"]:
            m["last_seq"] = max(m["last_seq"] or 0, q["seq"])
        m["pk"][q["seq"]] = q["frames"]
        return q

    def _rebuild(self, m, prof):
        """Each group with exactly one data packet missing and its parity packet in: the
        missing packet = the parity XOR the group's other packets (frames and padding as sent;
        L's reserved bytes are zero on both sides)."""
        nb = prof.slots * FRAME_BYTES
        for g, par in m["parity"].items():
            seqs = prof.group_seqs(g, m["total"])
            lost = [q for q in seqs if q not in m["pk"]]
            if len(lost) != 1:
                continue
            body = bytearray(par[:nb])
            for q in seqs:
                if q != lost[0]:
                    for i, b in enumerate(b"".join(m["pk"][q])):
                        body[i] ^= b
            m["pk"][lost[0]] = [bytes(body[6 * s:6 * s + 6]) for s in range(prof.slots)]
            self.rebuilt += 1

    def frames(self, msg_id):
        """-> (frames bytes, good uint8 per frame) for one message."""
        m = self.messages[msg_id]
        total = m["total"]
        if total is None:      # profile S: up to the end of the last group seen, minus zero padding
            top = max(m["pk"]) if m["last_seq"] is None else m["last_seq"] | (GROUP - 1)
            total = (top // GROUP + 1) * self.profile.group_frames
        fr, good = [bytes(6)] * total, np.zeros(total, np.uint8)
        prof = replace(self.profile, depth=m.get("depth", GROUP), short_last=m.get("short", False))
        if m.get("parity") and m["total"] is not None:
            self._rebuild(m, prof)
        for seq, body in m["pk"].items():
            for s, f in enumerate(prof.packet_frames(seq, m["total"])):
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
