"""Reference implementation of the K6 voice-pager packet format, version 1 (spec:
k6-fsk-packet-spec.md next to this file). Pure Python, no dependencies.

  pack_message(frames, msg_id)  -> list of 64-byte on-air payloads (whitened)
  unpack_payload(payload)       -> dict or None (bad CRC / magic)
  reassemble(payloads)          -> (frames, good) for the sotto decoder
  parity_payload(group_payloads, g) -> the group's XOR parity packet (section 14)
  reassemble(payloads) also rebuilds a group's one missing packet from its parity packet
  python3 k6fsk_ref.py          -> prints the test vectors in the spec

SPDX-License-Identifier: MIT"""

MAGIC = 0xA7               # format version 1, sotto v3.7 8 kHz frames
FRAME_BYTES = 6            # one sotto frame (48 bits, 20 ms), as c/sotto_enc writes them
FRAMES_PER_PACKET = 8
PACKETS_PER_GROUP = 8      # interleave depth: 64 frames (1.28 s) per group
PAYLOAD_BYTES = 64
SYNC = bytes([0x07, 0x6B, 0x3E, 0x36])
PREAMBLE = bytes([0x55]) * 16


def crc16_ccitt_false(data):
    """poly 0x1021, init 0xFFFF, MSB first, no reflection, no final xor (check '123456789' = 0x29B1)"""
    c = 0xFFFF
    for b in data:
        c ^= b << 8
        for _ in range(8):
            c = ((c << 1) ^ 0x1021) & 0xFFFF if c & 0x8000 else (c << 1) & 0xFFFF
    return c


def pn9(n):
    """PN9 whitening bytes, x^9 + x^5 + 1, seed 0x1FF (TI DN509 / CC1101 sequence: FF E1 1D 9A ...)"""
    out, s = [], 0x1FF
    for _ in range(n):
        out.append(s & 0xFF)
        for _ in range(8):
            fb = ((s >> 0) ^ (s >> 5)) & 1
            s = (s >> 1) | (fb << 8)
    return bytes(out)


WHITEN = pn9(PAYLOAD_BYTES)


def frame_slot(f):
    """frame number -> (packet seq, slot 0..7). Packet k of group g carries frames
    g*64 + s*8 + k for s = 0..7, so a lost packet loses frames 8 apart (160 ms)."""
    g, r = divmod(f, FRAMES_PER_PACKET * PACKETS_PER_GROUP)
    s, k = divmod(r, PACKETS_PER_GROUP)
    return g * PACKETS_PER_GROUP + k, s


def packet_frames(seq):
    g, k = divmod(seq, PACKETS_PER_GROUP)
    return [g * 64 + s * 8 + k for s in range(FRAMES_PER_PACKET)]


def n_packets(total_frames):
    groups = (total_frames + 63) // 64
    return groups * PACKETS_PER_GROUP


def build_payload(msg_id, seq, total, frames6):
    """frames6: 8 entries of 6 bytes (absent frames past the end: 6 zero bytes)"""
    p = bytearray(PAYLOAD_BYTES)
    p[0] = MAGIC
    p[1] = msg_id & 0xFF
    p[2:4] = seq.to_bytes(2, 'big')
    p[4:6] = total.to_bytes(2, 'big')
    for s, fr in enumerate(frames6):
        p[6 + 6 * s: 12 + 6 * s] = fr
    # bytes 54..61 reserved (zero) for future FEC
    p[62:64] = crc16_ccitt_false(p[:62]).to_bytes(2, 'big')
    return bytes(a ^ b for a, b in zip(p, WHITEN))


def pack_message(frames, msg_id):
    """frames: list of 6-byte sotto frames -> on-air payloads in transmit order"""
    total = len(frames)
    out = []
    for seq in range(n_packets(total)):
        f6 = [frames[f] if f < total else bytes(6) for f in packet_frames(seq)]
        out.append(build_payload(msg_id, seq, total, f6))
    return out


def unpack_payload(payload):
    p = bytes(a ^ b for a, b in zip(payload, WHITEN))
    if len(p) != PAYLOAD_BYTES or p[0] != MAGIC:
        return None
    if crc16_ccitt_false(p[:62]) != int.from_bytes(p[62:64], 'big'):
        return None
    return {'msg_id': p[1], 'seq': int.from_bytes(p[2:4], 'big'), 'total': int.from_bytes(p[4:6], 'big'),
            'frames': [p[6 + 6 * s: 12 + 6 * s] for s in range(FRAMES_PER_PACKET)]}


def reassemble(payloads):
    """-> (frames, good): every frame of the message, and whether it arrived (good = 0 frames
    go to sotto_decode with good = 0, which conceals them)"""
    pk = [q for q in (unpack_payload(p) for p in payloads) if q]
    if not pk:
        return [], []
    total = pk[0]['total']
    have = {q['seq'] for q in pk}
    for par in (q for q in (unpack_parity(p) for p in payloads) if q):
        seqs = range(par['group'] * PACKETS_PER_GROUP, (par['group'] + 1) * PACKETS_PER_GROUP)
        lost = [q for q in seqs if q not in have]
        if len(lost) == 1:          # the parity XOR the other 7 packets' frames
            x = bytearray(par['xor'][:FRAMES_PER_PACKET * FRAME_BYTES])
            for q in pk:
                if q['seq'] in seqs:
                    for i, b in enumerate(b''.join(q['frames'])):
                        x[i] ^= b
            pk.append({'seq': lost[0], 'frames': [bytes(x[6 * s:6 * s + 6]) for s in range(FRAMES_PER_PACKET)]})
            have.add(lost[0])
    frames, good = [bytes(6)] * total, [False] * total
    for q in pk:
        for s, f in enumerate(packet_frames(q['seq'])):
            if f < total:
                frames[f], good[f] = q['frames'][s], True
    return frames, good


# ---- Parity packets (section 14, 2026-11-07): one optional packet after each group of 8 ----
# byte 0 = the codec's magic | 0x10 (0xB7 here), byte 1 msg_id, bytes 2-3 the group number g
# (where a data packet has seq), bytes 4-5 total, bytes 6..61 the XOR of the group's 8 data
# packets' bytes 6..61 (before whitening), CRC and whitening as a data packet. A receiver that
# has 7 of the group's 8 packets rebuilds the eighth: the parity XOR the other seven.
PARITY = 0x10


def parity_payload(group_payloads, g):
    """the whitened parity packet of group g, from its 8 whitened data payloads"""
    acc = bytearray(PAYLOAD_BYTES - 8)
    msg_id = total = None
    for pl in group_payloads:
        p = bytes(a ^ b for a, b in zip(pl, WHITEN))
        msg_id, total = p[1], p[4:6]
        for i in range(6, 62):
            acc[i - 6] ^= p[i]
    p = bytearray(PAYLOAD_BYTES)
    p[0] = MAGIC | PARITY
    p[1] = msg_id
    p[2:4] = g.to_bytes(2, 'big')
    p[4:6] = total
    p[6:62] = acc
    p[62:64] = crc16_ccitt_false(p[:62]).to_bytes(2, 'big')
    return bytes(a ^ b for a, b in zip(p, WHITEN))


def pack_message_parity(frames, msg_id):
    """pack_message with a parity packet after every group of 8"""
    pl, out = pack_message(frames, msg_id), []
    for g in range(len(pl) // PACKETS_PER_GROUP):
        grp = pl[g * PACKETS_PER_GROUP:(g + 1) * PACKETS_PER_GROUP]
        out += grp + [parity_payload(grp, g)]
    return out


def unpack_parity(payload):
    p = bytes(a ^ b for a, b in zip(payload, WHITEN))
    if len(p) != PAYLOAD_BYTES or p[0] != MAGIC | PARITY:
        return None
    if crc16_ccitt_false(p[:62]) != int.from_bytes(p[62:64], 'big'):
        return None
    return {'msg_id': p[1], 'group': int.from_bytes(p[2:4], 'big'), 'total': int.from_bytes(p[4:6], 'big'),
            'xor': p[6:62]}


# ---- Profile S (fallback, only if the FD6818 won't take 64-byte frames): 16-byte payload ----
# byte 0: msg_id (4 bits) << 4 | last-group flag << 3 | seq bits 10..8; byte 1: seq bits 7..0;
# bytes 2..13: 2 sotto frames; bytes 14..15: CRC-16/CCITT-FALSE of bytes 0..13 (big-endian);
# then PN9-whiten all 16 bytes (the first 16 bytes of the same sequence).
S_BYTES, S_FRAMES = 16, 2
S_GROUP = S_FRAMES * PACKETS_PER_GROUP          # 16 frames (320 ms) per group


def s_packet_frames(seq):
    g, k = divmod(seq, PACKETS_PER_GROUP)
    return [g * S_GROUP + s * PACKETS_PER_GROUP + k for s in range(S_FRAMES)]


def s_pack_message(frames, msg_id):
    total = len(frames)
    ngroups = (total + S_GROUP - 1) // S_GROUP
    out = []
    for seq in range(ngroups * PACKETS_PER_GROUP):
        last = 1 if seq // PACKETS_PER_GROUP == ngroups - 1 else 0
        p = bytearray(S_BYTES)
        p[0] = (msg_id & 0xF) << 4 | last << 3 | (seq >> 8) & 7
        p[1] = seq & 0xFF
        for s, f in enumerate(s_packet_frames(seq)):
            p[2 + 6 * s: 8 + 6 * s] = frames[f] if f < total else bytes(6)
        p[14:16] = crc16_ccitt_false(p[:14]).to_bytes(2, 'big')
        out.append(bytes(a ^ b for a, b in zip(p, WHITEN)))
    return out


def s_unpack_payload(payload):
    p = bytes(a ^ b for a, b in zip(payload, WHITEN))
    if len(p) != S_BYTES or crc16_ccitt_false(p[:14]) != int.from_bytes(p[14:16], 'big'):
        return None
    return {'msg_id': p[0] >> 4, 'last': (p[0] >> 3) & 1, 'seq': (p[0] & 7) << 8 | p[1],
            'frames': [p[2:8], p[8:14]]}


if __name__ == '__main__':
    assert crc16_ccitt_false(b'123456789') == 0x29B1
    print('PN9 first 8 bytes:', pn9(8).hex(' '))
    frames = [bytes([f & 0xFF, (f >> 8) & 0xFF, 0x11, 0x22, 0x33, 0x44]) for f in range(70)]
    pl = pack_message(frames, msg_id=0x42)
    print('70 frames ->', len(pl), 'packets; packet 0 frames', packet_frames(0), 'packet 9 frames', packet_frames(9))
    print('packet 0 on air (preamble + sync + whitened payload):')
    print((PREAMBLE + SYNC + pl[0]).hex(' '))
    print('packet 0 before whitening:')
    print(bytes(a ^ b for a, b in zip(pl[0], WHITEN)).hex(' '))
    f, g = reassemble(pl[:3] + pl[4:])          # lose packet 3
    print('lost packet 3 -> missing frames', [i for i, x in enumerate(g) if not x])
    assert f[5] == frames[5]
    pp = pack_message_parity(frames, msg_id=0x42)
    print('with parity:', len(pp), 'packets; the parity packets are packets 8 and 17 on air')
    print('group 0 parity before whitening (its frames XOR to zero):', bytes(a ^ b for a, b in zip(pp[8], WHITEN)).hex(' '))
    print('group 1 parity before whitening:', bytes(a ^ b for a, b in zip(pp[17], WHITEN)).hex(' '))
    print('group 1 parity on air (whitened payload):', pp[17].hex(' '))
    f, g = reassemble(pp[:12] + pp[13:])        # lose packet 11 (frame 67): rebuilt from group 1's parity
    assert all(g) and f == frames
    f, g = reassemble(pp[:3] + pp[4:])          # lose packet 3 again: rebuilt from parity
    assert all(g) and f == frames
    print('lost packet 3 with parity -> missing frames', [i for i, x in enumerate(g) if not x])
    ps = s_pack_message(frames, msg_id=0x5)
    print('profile S: 70 frames ->', len(ps), 'packets; packet 0 frames', s_packet_frames(0))
    print('profile S packet 0 on air:', (PREAMBLE + SYNC + ps[0]).hex(' '))
    q = s_unpack_payload(ps[-1])
    assert q and q['last'] == 1 and q['seq'] == len(ps) - 1
