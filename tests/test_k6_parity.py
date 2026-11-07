"""Parity packets for the K6 voice pager (spec section 14): one XOR packet after each interleave
group lets the K6 rebuild a group's one lost packet exactly instead of concealing its frames.
The sender (modem/pager.py), the reference (c/k6/k6fsk_ref.py) and the receiver
(c/k6/sotto_pager.c, through c/k6/pager_host*) must agree. Needs make -C c/k6.
SPDX-License-Identifier: MIT"""
import os
import subprocess
import sys
import tempfile

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
K6 = os.path.join(ROOT, "c", "k6")
HOST = os.path.join(K6, "pager_host")
HOST_LR = os.path.join(K6, "pager_host_lr")
sys.path.insert(0, ROOT)
sys.path.insert(0, K6)
import k6fsk_ref as ref  # noqa: E402
from modem import pager  # noqa: E402

needs_host = pytest.mark.skipif(not (os.path.exists(HOST) and os.path.exists(HOST_LR)),
                                reason="needs make -C c/k6")


def frames_of(n, seed=1):
    rng = np.random.default_rng(seed)
    return b"".join(bytes(rng.integers(0, 256, 6, dtype=np.uint8)) for _ in range(n))


def host(exe, packets, xl=True, store=200000, halves=0):
    """pager_host: store then play (-S) or, with halves, live streaming in the K6's 4 KB"""
    with tempfile.TemporaryDirectory() as d:
        with open(f"{d}/p.bin", "wb") as f:
            f.write(b"".join(packets))
        mode = ["-s", str(halves), "-b", "4096"] if halves else ["-S", "-b", str(store)]
        args = [exe] + mode + (["-x"] if xl else []) + [f"{d}/p.bin", f"{d}/o.raw"]
        out = subprocess.run(args, check=True, capture_output=True, text=True).stdout.split()
        st = dict(kv.split("=", 1) for kv in out if "=" in kv)
        st["results"] = [int(x) for x in st["results"].split(",") if x]
        return st, np.fromfile(f"{d}/o.raw", "<i2")


def split(prof, pk):
    """on-air payloads -> (seq or ('P', group)) per packet, in order"""
    ids = []
    for p in pk:
        q = prof.unpack(p)
        ids.append(("P", q["group"]) if q["parity"] is not None else q["seq"])
    return ids


def test_reference_vector_and_sender_agree():
    """The spec's 70-frame test message with parity: 18 L packets (2 groups, a parity packet after
    each); modem/pager.py packs exactly the reference's bytes, and either rebuilds packet 3."""
    fr = [bytes([f & 0xFF, f >> 8, 0x11, 0x22, 0x33, 0x44]) for f in range(70)]
    pr = ref.pack_message_parity(fr, 0x42)
    assert len(pr) == 18 and pr[:8] == ref.pack_message(fr, 0x42)[:8]
    prof = pager.profile("L", short_last=False, parity=True)    # the spec's message: padded layout
    assert prof.pack(b"".join(fr), 0x42) == pr
    raw = bytes(a ^ b for a, b in zip(pr[17], ref.WHITEN))
    assert raw[:8].hex() == "b742000100460100" and raw[62:].hex() == "60ae"
    f, g = ref.reassemble(pr[:3] + pr[4:])
    assert all(g) and f == fr
    asm = pager.Assembler(pager.L)
    for p in pr[:3] + pr[4:]:
        asm.push(p)
    got, good = asm.frames(0x42)
    assert good.all() and got == b"".join(fr) and asm.rebuilt == 1


@pytest.mark.parametrize("name,depth", [("L", 8), ("XL", 8), ("XL", 4), ("XL", 1), ("L", 2)])
def test_layout_and_assembler(name, depth):
    """A parity packet follows each group's D data packets; the data packets are byte for byte
    the ones a send without parity has, so a K6 without parity support plays them as before."""
    fr = frames_of(377, seed=3)
    prof = pager.profile(name, "1200", depth, parity=True)
    pk = prof.pack(fr, 9)
    plain = pager.profile(name, "1200", depth).pack(fr, 9)
    assert len(pk) == prof.air_packets(377) == len(plain) + -(-len(plain) // depth)
    ids = split(prof, pk)
    assert [i for i in ids if not isinstance(i, tuple)] == list(range(len(plain)))
    assert [p for p, i in zip(pk, ids) if not isinstance(i, tuple)] == plain
    ngroups = -(-len(plain) // depth)
    assert [i for i in ids if isinstance(i, tuple)] == [("P", g) for g in range(ngroups)]
    for g in range(ngroups):     # each parity packet right after its group's last packet
        last = min((g + 1) * depth, len(plain)) - 1
        assert ids[ids.index(last) + 1] == ("P", g)
    # lose one data packet in each group: every frame comes back
    lose = {min(g * depth + (g * 3) % depth, len(plain) - 1) for g in range(ngroups)}
    asm = pager.Assembler(pager.profile(name, "1200"))
    for p, i in zip(pk, ids):
        if i not in lose:
            asm.push(p)
    got, good = asm.frames(9)
    assert good.all() and got == fr and asm.rebuilt == len(lose)


@needs_host
@pytest.mark.parametrize("exe,name,codec", [(HOST, "L", "2400"), (HOST_LR, "XL", "1200"), (HOST_LR, "L", "1200")])
def test_k6_rebuilds_one_lost_packet_per_group(exe, name, codec):
    """Store then play: one lost data packet per group is rebuilt (the samples equal a playback
    with nothing lost); a group that lost two keeps both lost; a lost parity packet costs
    nothing; sp_missing lists only the packets that stay lost."""
    fr = frames_of(700, seed=4)
    prof = pager.profile(name, codec, parity=True)
    pk, ids = prof.pack(fr, 21), None
    ids = split(prof, pk)
    full = [p for p, i in zip(pk, ids) if not isinstance(i, tuple)]
    ngroups = len(full) // 8
    lost = {1, 10, 11, 8 * (ngroups - 1) + 7}     # group 0 one, group 1 two, the last group one
    lost_par = {("P", 2)}                          # group 2's parity lost (it lost nothing)
    rx = [p for p, i in zip(pk, ids) if i not in lost and i not in lost_par]
    st, pcm = host(exe, rx, xl=name == "XL")
    keep = [p for i, p in enumerate(full) if i not in (10, 11)]
    ref_st, ref_pcm = host(exe, keep, xl=name == "XL")
    _, all_pcm = host(exe, full, xl=name == "XL")
    assert st["rebuilt"] == "2" and st["missing"] == "2:10,11"
    assert np.array_equal(pcm, ref_pcm) and st["pcm"] == ref_st["pcm"]
    assert not np.array_equal(pcm, all_pcm)
    res = st["results"]
    par_res = [r for r, i in zip(res, [i for i in ids if i not in lost and i not in lost_par])
               if isinstance(i, tuple)]
    assert par_res.count(1) == 2 and all(r in (0, 1, 3) for r in par_res)
    # and with only group 0's loss, the message completes on its parity packet
    st2, pcm2 = host(exe, [p for p, i in zip(pk, ids) if i != 1], xl=name == "XL")
    assert st2["complete"] == "1" and st2["rebuilt"] == "1" and np.array_equal(pcm2, all_pcm)
    assert int(st2["good"]) == len(pk) - 1 and st2["bad"] == "0"


@needs_host
@pytest.mark.parametrize("depth,lost", [(8, [3, 17, 40, 58]), (4, [2, 9, 30]), (1, [5, 6]), (8, [])])
def test_k6_stream_rebuilds_before_the_group_plays(depth, lost):
    """Streaming in the K6's 4 KB ring at real time (32 halves per XL packet; parity packets take
    their own air time): the parity packet arrives before the group is final, so the rebuilt
    frames play, and the samples match a store-then-play of the same packets. At depth 1 the
    parity packet is a copy of its packet, so it stands in for it."""
    fr = frames_of(1200, seed=5)
    prof = pager.profile("XL", "1200", depth, parity=True)
    pk = prof.pack(fr, 12)
    ids = split(prof, pk)
    rx = [p for p, i in zip(pk, ids) if i not in lost]
    st, pcm = host(HOST_LR, rx, halves=32)
    ref_st, ref = host(HOST_LR, rx)
    _, full = host(HOST_LR, [p for p, i in zip(pk, ids) if not isinstance(i, tuple)])
    assert int(st["ring"]) == 640 and int(st["overruns"]) == 0
    assert st["rebuilt"] == str(len(lost)) and st["missing"] == "0"
    assert np.array_equal(pcm, ref) and np.array_equal(pcm, full) and st["pcm"] == ref_st["pcm"]
    # playback still starts once the first group (and its parity) is in
    assert int(st["start"]) <= depth + 1


@needs_host
def test_parity_never_starts_or_breaks_a_message():
    """A parity packet with no message of its own (first on air, another msg_id, another codec)
    changes nothing: SP_DUP, no message dropped. One with a bad CRC counts as a bad packet."""
    fr = frames_of(160, seed=6)
    prof = pager.profile("XL", "1200", parity=True)
    pk = prof.pack(fr, 30)
    other = pager.profile("XL", "1200", parity=True).pack(frames_of(160, seed=7), 31)
    v37 = pager.profile("XL", "2400", parity=True).pack(fr, 30)
    par = pk[8]
    assert prof.unpack(par)["parity"] is not None
    bad = bytearray(par)
    bad[40] ^= 1
    seq = [par, other[8], v37[8]] + pk[:3] + pk[4:8] + [other[8], v37[8], bytes(bad), par]
    st, pcm = host(HOST_LR, seq)
    assert st["results"][:3] == [0, 0, 0]
    assert st["results"][3] == 2                     # the first data packet starts the message
    assert st["results"][-4:] == [0, 0, -1, 3]       # others ignored, bad CRC, then rebuilt: complete
    assert st["id"] == "30" and st["rebuilt"] == "1" and st["complete"] == "1" and st["bad"] == "1"
    _, full = host(HOST_LR, pk[:8])
    assert np.array_equal(pcm, full)


@needs_host
def test_pcmhash_check_still_matches():
    """tools/k6_pcmhash.py leaves out the packets the K6 lists as missing; a rebuilt packet isn't
    listed, and its frames are the sent ones, so the check needs no parity option."""
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import k6_pcmhash
    fr = frames_of(300, seed=8)
    mid = k6_pcmhash.msg_id_for(fr)
    prof = pager.profile("XL", "1200", parity=True)
    pk = prof.pack(fr, mid)
    ids = split(prof, pk)
    st, _ = host(HOST_LR, [p for p, i in zip(pk, ids) if i not in (2, 12, 13)], halves=32)
    assert st["missing"] == "2:12,13" and st["rebuilt"] == "1"
    assert k6_pcmhash.reference(fr, "1200", "XL", [12, 13])[0] == st["pcm"]


def test_cli_tx_and_rx_round_trip(tmp_path):
    """modem.k6 tx --parity, then rx of the same audio: the parity packets decode and count."""
    from modem import k6
    fr = frames_of(200, seed=9)
    sot = tmp_path / "m.sot"
    sot.write_bytes(fr)
    wav = tmp_path / "tx.wav"
    assert k6.main(["tx", str(sot), str(wav), "--phy", "F", "--payload", "XL", "--codec", "1200", "--parity", "--no-trim", "--no-pause"]) == 0
    back = tmp_path / "back.sot"
    assert k6.main(["rx", str(wav), str(back), "--phy", "F", "--payload", "XL", "--codec", "1200"]) == 0
    assert back.read_bytes() == fr
    with pytest.raises(ValueError):
        pager.profile("S", parity=True)


def test_cli_drop_leaves_out_data_packets(tmp_path):
    """tx --drop 3 sends every packet but data seq 3; with --parity the receiver rebuilds it."""
    from modem import k6
    fr = frames_of(200, seed=10)
    sot = tmp_path / "m.sot"
    sot.write_bytes(fr)
    wav = tmp_path / "tx.wav"
    opts = ["--phy", "F", "--payload", "XL", "--codec", "1200"]
    assert k6.main(["tx", str(sot), str(wav)] + opts + ["--parity", "--no-trim", "--no-pause", "--drop", "3"]) == 0
    back, good = tmp_path / "back.sot", tmp_path / "g.u8"
    assert k6.main(["rx", str(wav), str(back), "--good", str(good)] + opts) == 0
    assert back.read_bytes() == fr and np.fromfile(good, np.uint8).all()
