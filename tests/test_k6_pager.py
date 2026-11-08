"""K6 voice pager receive path (c/k6/): packets in the spec's format (c/k6/k6fsk_ref.py) go
through the pager's reassembly and its Cortex-M0 decoder path, and must play exactly what the
fixed-point M0 decoder plays from the same frames and good flags. Needs make -C c/k6 and
make -C c VARIANT=v3.7 fixed. SPDX-License-Identifier: MIT"""
from dataclasses import replace
import os
import subprocess
import sys
import tempfile

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
K6 = os.path.join(ROOT, "c", "k6")
HOST = os.path.join(K6, "pager_host")
FX = os.path.join(ROOT, "c", "fixed")
sys.path.insert(0, K6)
import k6fsk_ref as ref  # noqa: E402

pytestmark = pytest.mark.skipif(
    not (os.path.exists(HOST) and os.path.exists(os.path.join(FX, "sotto_dec_fx_m0"))),
    reason="needs make -C c/k6 and make -C c VARIANT=v3.7 fixed")

SPEC_PACKET0 = bytes.fromhex(
    "58a31d9aedc33324fb58e17d78974628 67393dd87c2f89cb7f59d6808c70ea18"
    "2171a09bbaecb637b998c486446c2b65 78c4d5f7f5d58acde7d14e09321779ba".replace(" ", ""))


def frames_of(n, seed=1):
    """n random-looking 6-byte frames (any 48 bits decode)"""
    rng = np.random.default_rng(seed)
    return [bytes(rng.integers(0, 256, 6, dtype=np.uint8)) for _ in range(n)]


def run_host(packets, xl=False, store=6144, pwm=False):
    with tempfile.TemporaryDirectory() as d:
        with open(f"{d}/p.bin", "wb") as f:
            f.write(b"".join(packets))
        args = [HOST] + (["-x"] if xl else []) + (["-p"] if pwm else []) + ["-b", str(store), f"{d}/p.bin", f"{d}/o.raw"]
        out = subprocess.run(args, check=True, capture_output=True, text=True).stdout.split()
        st = dict(kv.split("=", 1) for kv in out)
        st["results"] = [int(x) for x in st["results"].split(",") if x]
        return st, np.fromfile(f"{d}/o.raw", "<u2" if pwm else "<i2")


def run_dec(frames, good):
    with tempfile.TemporaryDirectory() as d:
        with open(f"{d}/in.bit", "wb") as f:
            f.write(b"".join(frames))
        np.asarray(good, np.uint8).tofile(f"{d}/good.u8")
        subprocess.run([os.path.join(FX, "sotto_dec_fx_m0"), f"{d}/in.bit", f"{d}/out.raw", f"{d}/good.u8"], check=True)
        return np.fromfile(f"{d}/out.raw", "<i2")


def test_calibration_packet_matches_spec():
    out = subprocess.run([HOST, "-t"], check=True, capture_output=True, text=True).stdout
    assert bytes.fromhex(out) == SPEC_PACKET0
    fr = [bytes([f & 0xFF, f >> 8, 0x11, 0x22, 0x33, 0x44]) for f in range(70)]
    assert ref.pack_message(fr, 0x42)[0] == SPEC_PACKET0


def test_spec_vector_lost_packet_3():
    fr = [bytes([f & 0xFF, f >> 8, 0x11, 0x22, 0x33, 0x44]) for f in range(70)]
    pl = ref.pack_message(fr, 0x42)
    st, _ = run_host(pl[:3] + pl[4:])
    assert st["total"] == "70" and st["packets"] == "15" and st["of"] == "16"
    assert st["got"] == str(70 - 8) and st["complete"] == "0"
    assert st["missing"] == "1:3"


@pytest.mark.parametrize("lost", [[], [3], [0, 9, 10, 17], list(range(8, 16))])
def test_playback_equals_m0_decoder(lost):
    fr = frames_of(150)
    pl = ref.pack_message(fr, 7)
    keep = [p for i, p in enumerate(pl) if i not in lost]
    st, pcm = run_host(keep)
    frames, good = ref.reassemble(keep)
    assert st["got"] == str(sum(good))
    want = run_dec(frames, good)
    # the radio plays the decoder's first 20 ms (silence); sotto_dec drops it
    assert np.all(pcm[:160] == 0)
    assert np.array_equal(pcm[160:], want)
    assert st["results"][0] == 2 and (st["complete"] == "1") == (not lost)
    assert st["missing"] == (f"{len(lost)}:" + ",".join(map(str, lost[:8])) if lost else "0")


def test_second_pass_fills_holes():
    fr = frames_of(130, seed=2)
    pl = ref.pack_message(fr, 9)
    pass1 = [p for i, p in enumerate(pl) if i % 5 != 1]
    pass2 = [p for i, p in enumerate(pl) if i % 5 != 3]
    st, pcm = run_host(pass1 + pass2)
    assert st["complete"] == "1" and st["got"] == "130"
    assert 3 in st["results"]                                   # SP_COMPLETE once
    assert st["results"].count(3) == 1
    assert np.array_equal(pcm[160:], run_dec(fr, [1] * 130))


def test_new_message_drops_old_and_bad_packets_count():
    a, b = ref.pack_message(frames_of(64, 3), 1), ref.pack_message(frames_of(70, 4), 2)
    bad = bytearray(b[0])
    bad[20] ^= 0x10
    st, pcm = run_host(a[:5] + [bytes(bad)] + b)
    assert st["id"] == "2" and st["total"] == "70" and st["complete"] == "1"
    assert st["results"][5] == -1 and st["results"][6] == 2 and st["bad"] == "1"
    assert np.array_equal(pcm[160:], run_dec(frames_of(70, 4), [1] * 70))


def test_store_truncates_long_message():
    fr = frames_of(200, 5)
    st, pcm = run_host(ref.pack_message(fr, 3), store=99 * 6)     # 96 frames
    assert st["max_frames"] == "96" and st["stored"] == "96" and st["complete"] == "1"
    assert np.array_equal(pcm[160:], run_dec(fr[:96], [1] * 96))


def test_profile_xl():
    """128-byte packets, 20 frames each: the same header as L, frames g*160 + s*8 + k; the
    short last group's 140 frames take 7 packets, frames 160 + s*7 + k"""
    sys.path.insert(0, ROOT)
    from modem import pager
    fr = frames_of(300, 6)
    px = pager.XL.pack(b"".join(fr), 7)
    assert len(px) == 15 and all(len(p) == 128 for p in px)
    assert pager.XL.packet_frames(13, 300)[:3] == [165, 172, 179]
    lost = {2, 13}
    keep = [p for i, p in enumerate(px) if i not in lost]
    st, pcm = run_host(keep, xl=True)
    good = [1] * 300
    for seq in lost:
        for f in pager.XL.packet_frames(seq, 300):
            if f < 300:
                good[f] = 0
    assert st["total"] == "300" and st["of"] == "15" and st["packets"] == "13" and st["complete"] == "0"
    assert st["got"] == str(sum(good))
    assert np.array_equal(pcm[160:], run_dec(fr, good))


def test_xl_and_l_play_the_same():
    """one message sent as XL or as L plays the same"""
    sys.path.insert(0, ROOT)
    from modem import pager
    fr = frames_of(70, 8)
    x = pager.XL.pack(b"".join(fr), 4)
    l = ref.pack_message(fr, 4)
    st, pcm_x = run_host(x, xl=True)
    assert st["complete"] == "1" and st["of"] == "4" == str(len(x))     # one short group of 4
    st, pcm_l = run_host(l)
    assert st["complete"] == "1" and st["of"] == "16"
    assert np.array_equal(pcm_x, pcm_l)          # the same frames play the same


def test_auto_gain_and_pwm_conversion():
    """sp_auto_gain_q8 follows the loudest energy code; sp_play_pwm = pre-emphasis
    (33 x[n] - 21 x[n-1] + 4 x[n-2]) / 16, gain, then 1000 + 1000 x / 32768 within 0..2000"""
    fr = frames_of(90, 7)
    pl = ref.pack_message(fr, 4)
    st, pcm = run_host(pl)
    _, pwm = run_host(pl, pwm=True)
    top = max((int.from_bytes(f, "big") >> 34) & 31 for f in fr)
    level = 10 + top * 70 / 31 - 87.3
    want_gain = round(256 * 10 ** (max(-6.0, min(18.0, -13.0 - level)) / 20))
    assert int(st["auto_gain"]) == want_gain
    x = pcm.astype(np.int64)
    xp = np.concatenate([[0, 0], x])
    y = (33 * xp[2:] - 21 * xp[1:-1] + 4 * xp[:-2]) >> 4
    t = np.clip((y * want_gain) >> 8, -32767, 32767)
    assert np.array_equal(pwm, np.clip(1000 + ((t * 1000) >> 15), 0, 2000))


def run_errors(packets, xl=False):
    with tempfile.TemporaryDirectory() as d:
        with open(f"{d}/p.bin", "wb") as f:
            f.write(b"".join(packets))
        out = subprocess.run([HOST, "-e"] + (["-x"] if xl else []) + [f"{d}/p.bin"],
                             check=True, capture_output=True, text=True).stdout
        return [[int(x) for x in ln.split()] for ln in out.splitlines()]


@pytest.mark.parametrize("prof", ["L", "XL"])
def test_bit_errors_against_test_message(prof):
    """sp_test_errors finds the test packet, the byte order and the polarity, and puts each
    flipped bit in its eighth of the packet"""
    sys.path.insert(0, ROOT)
    from modem import pager
    fr = b"".join(bytes([f & 0xFF, f >> 8, 0x11, 0x22, 0x33, 0x44]) for f in range(70))
    pk = replace(pager.PROFILES[prof], short_last=False).pack(fr, 0x42)   # the spec's layout
    assert len(pk) == (16 if prof == "L" else 8)
    n = len(pk[0])

    def flip(p, bits):
        p = bytearray(p)
        for b in bits:
            p[b // 8] ^= 0x80 >> (b % 8)
        return bytes(p)

    def swap(p):
        return bytes(p[i ^ 1] for i in range(len(p)))

    def inv(p):
        return bytes(255 - x for x in p)

    seq = len(pk) - 3
    bits = [3, 8 * n - 1, 8 * n - 9]          # one in the first eighth, two in the last
    got = run_errors([pk[0], flip(pk[seq], bits), swap(flip(pk[1], bits)), inv(pk[2]), inv(swap(pk[5]))],
                     prof == "XL")
    blocks = [1, 0, 0, 0, 0, 0, 0, 2]
    assert got[0] == [0, 0, 0, 0] + [0] * 8
    assert got[1] == [3, seq, 0, 0] + blocks
    assert got[2] == [3, 1, 1, 0] + blocks        # counted where the bits sit once swapped back
    assert got[3] == [0, 2, 0, 1] + [0] * 8
    assert got[4] == [0, 5, 1, 1] + [0] * 8


def test_cs512_const_table_matches():
    """c/fixed/fx_cs512.inc (FX_CS512_CONST, the K6 build) is fx_cos512 packed as fx_cs512() does it."""
    import subprocess
    import sys as _sys
    r = subprocess.run([_sys.executable, os.path.join(ROOT, "tools", "gen_cs512.py"), "--check"])
    assert r.returncode == 0, "regenerate with python3 tools/gen_cs512.py"


def run_control(packets, xl=False):
    with tempfile.TemporaryDirectory() as d:
        with open(f"{d}/p.bin", "wb") as f:
            f.write(b"".join(packets))
        args = [HOST] + (["-x"] if xl else []) + ["-c", f"{d}/p.bin"]
        out = subprocess.run(args, check=True, capture_output=True, text=True).stdout
        return [ln.split() for ln in out.splitlines()]


def test_control_packet_records():
    """sp_control finds the record pager.control_packet wrote, in L and XL, through the
    radio's byte swap and inversion, and after early copies are damaged; voice packets and
    noise give none."""
    sys.path.insert(0, ROOT)
    from modem import pager
    want = pager.control_record(9, {"phy": "F", "frames": "XL", "auto": 1, "var": 2}, "play")[:6].hex()
    pk = pager.control_packet(9, {"phy": "F", "frames": "XL", "auto": 1, "var": 2}, "play")
    bad = bytearray(pk)
    for i in range(0, 40):                     # first five copies hit
        bad[i] ^= 0x10
    inv = bytes(b ^ 0xFF for b in pk)
    rows = run_control([pk, pager.swap_words(pk), inv, pager.swap_words(inv), bytes(bad)])
    assert [r[0] for r in rows] == ["1", "2", "3", "4", "1"]
    assert all("".join(r[1:]) == want for r in rows)
    # an L control packet read as XL (the K6 in XL takes 128 bytes) still has its records first
    xl_rows = run_control([pk + bytes(64), pager.control_packet(9, {"phy": "D"}, length=128)], xl=True)
    assert xl_rows[0][0] == "1" and "".join(xl_rows[0][1:]) == want
    assert xl_rows[1][0] == "1" and xl_rows[1][3] == "01"
    # a voice packet and noise are not control packets
    noise = bytes(np.random.default_rng(3).integers(0, 256, 64, dtype=np.uint8))
    assert [r[0] for r in run_control([SPEC_PACKET0, noise])] == ["0", "0"]
    assert pager.parse_control(pk).hex() == want and pager.parse_control(SPEC_PACKET0) is None


HOST_LR = os.path.join(K6, "pager_host_lr")


def _run(host, packets, xl=False, store=6144):
    with tempfile.TemporaryDirectory() as d:
        with open(f"{d}/p.bin", "wb") as f:
            f.write(b"".join(packets))
        args = [host] + (["-x"] if xl else []) + ["-b", str(store), f"{d}/p.bin", f"{d}/o.raw"]
        out = subprocess.run(args, check=True, capture_output=True, text=True).stdout.split()
        st = dict(kv.split("=", 1) for kv in out)
        return st, np.fromfile(f"{d}/o.raw", "<i2")


def _dec_lr(frames, good):
    with tempfile.TemporaryDirectory() as d:
        with open(f"{d}/in.bit", "wb") as f:
            f.write(b"".join(frames))
        np.asarray(good, np.uint8).tofile(f"{d}/good.u8")
        subprocess.run([os.path.join(FX, "sotto_dec_fx_m0"), "-1200", f"{d}/in.bit", f"{d}/out.raw",
                        f"{d}/good.u8"], check=True)
        return np.fromfile(f"{d}/out.raw", "<i2")


@pytest.mark.skipif(not os.path.exists(HOST_LR), reason="needs make -C c/k6")
@pytest.mark.parametrize("name,lost", [("L", []), ("XL", []), ("XL", [3])])
def test_lr_pager_plays_what_the_m0_decoder_plays(name, lost):
    """The SP_LR build (pager_host_lr) stores 0xA9 packets and plays them through the 20 ms half
    calls bit-exactly as sotto_dec_fx_m0 -1200 decodes the same slots and good flags (the
    decoder's 80 ms of delay dropped), with a lost packet concealed alike."""
    sys.path.insert(0, ROOT)
    from modem import pager
    prof = pager.profile(name, "1200")
    fr = frames_of(70, seed=7)
    pk = prof.pack(b"".join(fr), msg_id=9)
    sent = [p for i, p in enumerate(pk) if i not in lost]
    st, pcm = _run(HOST_LR, sent, xl=name == "XL")
    good = [1] * len(fr)
    for q in lost:
        for f in prof.packet_frames(q, len(fr)):
            if f < len(fr):
                good[f] = 0
    ref_pcm = _dec_lr(fr, good)
    assert int(st["total"]) == 70 and int(st["got"]) == sum(good)
    assert len(pcm) == len(ref_pcm) == 70 * 320
    assert np.array_equal(pcm, ref_pcm)


@pytest.mark.skipif(not os.path.exists(HOST_LR), reason="needs make -C c/k6")
def test_each_build_plays_only_its_codec():
    """Both builds take both magics (good packets, stored), but the v3.7 build plays nothing of a
    sotto 1200 message and the 1200 build nothing of a v3.7 one."""
    sys.path.insert(0, ROOT)
    from modem import pager
    fr = b"".join(frames_of(16, seed=3))
    for host, codec, plays in ((HOST, "1200", False), (HOST_LR, "2400", False),
                               (HOST, "2400", True), (HOST_LR, "1200", True)):
        st, pcm = _run(host, pager.profile("L", codec).pack(fr, msg_id=4))
        assert int(st["total"]) == 16 and int(st["got"]) == 16, (host, codec)
        assert (len(pcm) > 0) == plays, (host, codec, len(pcm))


def _fnv(pcm):
    h = 2166136261
    for b in np.asarray(pcm, "<i2").tobytes():
        h = ((h ^ b) * 16777619) & 0xFFFFFFFF
    return f"{h:08x}"


@pytest.mark.skipif(not os.path.exists(HOST_LR), reason="needs make -C c/k6")
@pytest.mark.parametrize("host,codec,name,lost", [(HOST, "2400", "XL", []), (HOST_LR, "1200", "XL", []),
                                                  (HOST_LR, "1200", "XL", [3]), (HOST_LR, "1200", "L", [1])])
def test_pcm_hash_is_fnv_of_the_samples_played(host, codec, name, lost):
    """pcm= (what the radio's PLAY line prints) is FNV-1a over exactly the samples played, and
    tools/k6_pcmhash.py rebuilds it from the .sot and the missing list, MATCH or MISMATCH."""
    sys.path.insert(0, ROOT)
    from modem import pager
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import k6_pcmhash
    fr = b"".join(frames_of(70, seed=11))
    pk = pager.profile(name, codec).pack(fr, msg_id=k6_pcmhash.msg_id_for(fr))
    st, pcm = _run(host, [p for i, p in enumerate(pk) if i not in lost], xl=name == "XL")
    assert st["pcm"] == _fnv(pcm) and int(st["samples"]) == len(pcm) > 0
    h, n, _ = k6_pcmhash.reference(fr, codec, name, lost)
    assert (h, n) == (st["pcm"], len(pcm))
    with tempfile.TemporaryDirectory() as d:
        with open(f"{d}/m.sot", "wb") as f:
            f.write(fr)
        miss = f"{len(lost)}" + (":" + ",".join(map(str, lost)) if lost else "")
        line = f"K6 PLAY id=00 total=70 aborted=0 pcm={h} samples={n} missing={miss}"
        args = [f"{d}/m.sot", "--codec", codec, "--payload", name, "--no-trim", "--no-pause", "--check"]
        assert k6_pcmhash.main(args + [line]) == 0
        assert k6_pcmhash.main(args + [line.replace(f"pcm={h}", "pcm=00000000")]) == 1


def _stream(host, packets, halves, xl=True, store=4096):
    """pager_host -s: playback interleaved with arrival, HALVES x 20 ms per packet"""
    with tempfile.TemporaryDirectory() as d:
        with open(f"{d}/p.bin", "wb") as f:
            f.write(b"".join(packets))
        args = [host, "-s", str(halves)] + (["-x"] if xl else []) + ["-b", str(store), f"{d}/p.bin", f"{d}/o.raw"]
        out = subprocess.run(args, check=True, capture_output=True, text=True).stdout.split()
        return dict(kv.split("=", 1) for kv in out), np.fromfile(f"{d}/o.raw", "<i2")


@pytest.mark.skipif(not os.path.exists(HOST_LR), reason="needs make -C c/k6")
@pytest.mark.parametrize("name,n,lost,halves", [("XL", 1200, [], 32), ("XL", 1200, [3, 17, 40], 32),
                                                ("XL", 1200, [], 60), ("L", 700, [5, 70], 18),
                                                ("XL", 70, [3], 32)])
def test_lr_stream_plays_what_store_then_play_plays(name, n, lost, halves):
    """sotto 1200 streams by default: a message much longer than the K6's 4 KB store (1200
    slots = 48 s, 7.5 XL groups) plays through the ring while it arrives, and every decoded
    sample matches a store-then-play (-S) of the same packets in a store that holds it all,
    lost packets included. Played faster than it arrives (60 halves per packet) it waits for
    the next group instead of decoding frames that haven't come."""
    sys.path.insert(0, ROOT)
    from modem import pager
    prof = pager.profile(name, "1200")
    fr = b"".join(frames_of(n, seed=5))
    pk = [p for i, p in enumerate(prof.pack(fr, msg_id=12)) if i not in lost]
    st, pcm = _stream(HOST_LR, pk, halves, xl=name == "XL")
    ref_st, ref = _run(HOST_LR, pk, xl=name == "XL", store=200000)   # -S isn't needed: it fits
    assert int(st["ring"]) == (640 if name == "XL" else 640) and int(st["overruns"]) == 0
    assert int(st["total"]) == n and len(pcm) == n * 320
    assert np.array_equal(pcm, ref) and st["pcm"] == ref_st["pcm"]
    if halves > 40:
        assert int(st["waits"]) > 0
    elif halves == 32 and name == "XL" and n > 160:
        assert int(st["waits"]) == 0


@pytest.mark.skipif(not os.path.exists(HOST_LR), reason="needs make -C c/k6")
def test_lr_stream_overrun_when_the_sender_runs_far_ahead():
    """Played 4x slower than it arrives, the ring's later groups take the place of unplayed
    ones: counted as overruns (the audio differs then; the radio plays the newest)."""
    sys.path.insert(0, ROOT)
    from modem import pager
    fr = b"".join(frames_of(1200, seed=5))
    st, _ = _stream(HOST_LR, pager.profile("XL", "1200").pack(fr, msg_id=12), 8)
    assert int(st["overruns"]) > 0


@pytest.mark.skipif(not os.path.exists(HOST_LR), reason="needs make -C c/k6")
def test_store_mode_still_available():
    """-S: store then play, as before stream mode (a message longer than the store is cut)."""
    sys.path.insert(0, ROOT)
    from modem import pager
    fr = b"".join(frames_of(1200, seed=5))
    pk = pager.profile("XL", "1200").pack(fr, msg_id=12)
    with tempfile.TemporaryDirectory() as d:
        with open(f"{d}/p.bin", "wb") as f:
            f.write(b"".join(pk))
        out = subprocess.run([HOST_LR, "-S", "-x", "-b", "4096", f"{d}/p.bin", f"{d}/o.raw"], check=True,
                             capture_output=True, text=True).stdout.split()
    st = dict(kv.split("=", 1) for kv in out)
    assert int(st["stored"]) == 656 and int(st["total"]) == 1200


@pytest.mark.parametrize("name,depth", [("L", 4), ("XL", 4), ("XL", 2), ("XL", 1), ("L", 1)])
def test_interleave_depth_in_the_header(name, depth):
    """seq's top 2 bits carry the interleave depth (0 = 8, as before); packet k of group g holds
    frames g*F*D + s*D + k, and the Assembler takes the depth from each message's packets."""
    sys.path.insert(0, ROOT)
    from modem import pager
    prof = pager.profile(name, "1200", depth)
    fr = b"".join(frames_of(203, seed=2))
    pk = prof.pack(fr, msg_id=5)
    g_full, rem = divmod(203, prof.slots * depth)
    m = -(-rem // prof.slots)
    assert len(pk) == prof.n_packets(203) == g_full * depth + m
    assert prof.unpack(pk[-1])["short"] == (m < depth) and not prof.unpack(pk[0])["short"]
    q = prof.unpack(pk[depth + 1])
    assert q["seq"] == depth + 1 and q["depth"] == depth
    g, k = divmod(depth + 1, depth)
    assert prof.packet_frames(depth + 1)[:2] == [g * prof.slots * depth + k, g * prof.slots * depth + k + depth]
    assert pager.profile(name, "1200").unpack(pk[0])["depth"] == depth   # any receiver profile reads it
    asm = pager.Assembler(pager.profile(name, "1200"))
    for i, p in enumerate(pk):
        if i != 2:
            asm.push(p)
    got, good = asm.frames(5)
    lost = [f for f in prof.packet_frames(2) if f < 203]
    assert [i for i in range(203) if not good[i]] == lost
    assert all(got[6 * f:6 * f + 6] == fr[6 * f:6 * f + 6] for f in range(203) if good[f])
    assert pager.profile(name, "1200", 8).pack(fr, 5) == pager.profile(name, "1200").pack(fr, 5)
    with pytest.raises(ValueError):
        pager.profile(name, "1200", 3)


@pytest.mark.skipif(not os.path.exists(HOST_LR), reason="needs make -C c/k6")
@pytest.mark.parametrize("name,depth,lost", [("XL", 4, [3]), ("XL", 2, [1]), ("XL", 1, [2]), ("L", 4, [5]),
                                             ("L", 1, [])])
def test_depth_plays_what_the_m0_decoder_plays(name, depth, lost):
    """At depth 4, 2 or 1 the pager stores each packet's frames where the header says, and plays
    them (and a lost packet's concealment) bit-exactly as sotto_dec_fx_m0 -1200."""
    sys.path.insert(0, ROOT)
    from modem import pager
    prof = pager.profile(name, "1200", depth)
    fr = frames_of(150, seed=8)
    pk = [p for i, p in enumerate(prof.pack(b"".join(fr), msg_id=9)) if i not in lost]
    st, pcm = _run(HOST_LR, pk, xl=name == "XL", store=200000)
    good = [1] * len(fr)
    for q in lost:
        for f in prof.packet_frames(q, len(fr)):
            if f < len(fr):
                good[f] = 0
    assert int(st["got"]) == sum(good) and int(st["of"]) == len(prof.pack(b"".join(fr)))
    assert st["missing"] == (f"{len(lost)}:" + ",".join(map(str, lost)) if lost else "0")
    assert np.array_equal(pcm, _dec_lr(fr, good))


@pytest.mark.skipif(not os.path.exists(HOST_LR), reason="needs make -C c/k6")
@pytest.mark.parametrize("depth,lost", [(4, []), (4, [3, 17, 40]), (2, [5]), (1, [9])])
def test_lr_stream_at_smaller_depth(depth, lost):
    """Streaming at depth D: playback starts after D packets instead of 8, the ring still holds
    640 frames, and the samples match a store-then-play of the same packets."""
    sys.path.insert(0, ROOT)
    from modem import pager
    prof = pager.profile("XL", "1200", depth)
    fr = b"".join(frames_of(1200, seed=5))
    pk = [p for i, p in enumerate(prof.pack(fr, msg_id=12)) if i not in lost]
    st, pcm = _stream(HOST_LR, pk, 32)
    ref_st, ref = _run(HOST_LR, pk, xl=True, store=200000)
    assert int(st["ring"]) == 640 and int(st["overruns"]) == 0
    assert np.array_equal(pcm, ref) and st["pcm"] == ref_st["pcm"]
    # the first group is final once its D packets are in, or (one of them lost) once the next
    # group's first arrives: D packets read either way
    assert int(st["start"]) == depth


@pytest.mark.skipif(not os.path.exists(HOST_LR), reason="needs make -C c/k6")
def test_pcmhash_takes_the_depth():
    sys.path.insert(0, ROOT)
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import k6_pcmhash
    from modem import pager
    fr = b"".join(frames_of(300, seed=4))
    pk = pager.profile("XL", "1200", 4).pack(fr, msg_id=k6_pcmhash.msg_id_for(fr))
    st, _ = _stream(HOST_LR, [p for i, p in enumerate(pk) if i != 6], 32)
    assert k6_pcmhash.reference(fr, "1200", "XL", [6], depth=4)[0] == st["pcm"]
    assert k6_pcmhash.reference(fr, "1200", "XL", [6])[0] != st["pcm"]


@pytest.mark.skipif(not os.path.exists(HOST_LR), reason="needs make -C c/k6")
@pytest.mark.parametrize("name,depth,n,lost", [("XL", 8, 300, []), ("XL", 8, 300, [13]), ("XL", 8, 210, [8, 9]),
                                               ("XL", 4, 203, [10]), ("L", 8, 70, [8]), ("L", 4, 203, [24]),
                                               ("XL", 2, 203, [10])])
def test_short_last_group_plays_what_the_m0_decoder_plays(name, depth, n, lost):
    """The short last group (seq bit 13, m = ceil(rem / F) packets, frames g*F*D + s*m + k) lands
    where the sender put it, store-then-play and streaming alike, and a packet of it lost is
    concealed frame by frame as sotto_dec_fx_m0 -1200 does."""
    sys.path.insert(0, ROOT)
    from modem import pager
    prof = pager.profile(name, "1200", depth)
    fr = frames_of(n, seed=11)
    pk = prof.pack(b"".join(fr), msg_id=21)
    first, m = prof.last_group(n)
    assert first is not None and m < depth and len(pk) == first + m == prof.n_packets(n)
    good = [1] * n
    for q in lost:
        for f in prof.packet_frames(q, n):
            if f < n:
                good[f] = 0
    sent = [p for i, p in enumerate(pk) if i not in lost]
    st, pcm = _run(HOST_LR, sent, xl=name == "XL", store=200000)
    assert int(st["of"]) == len(pk) and int(st["got"]) == sum(good)
    assert np.array_equal(pcm, _dec_lr(fr, good))
    st, spcm = _stream(HOST_LR, sent, 32, xl=name == "XL")
    assert np.array_equal(spcm, pcm) and int(st["overruns"]) == 0


@pytest.mark.skipif(not os.path.exists(HOST_LR), reason="needs make -C c/k6")
def test_old_layout_and_bad_short_packets():
    """A sender's old padded layout (short_last=False) still plays the same; a packet that claims
    to be in the short group but isn't (k >= m, or not the last group) is dropped."""
    sys.path.insert(0, ROOT)
    from modem import pager
    fr = frames_of(300, seed=12)
    new = pager.profile("XL", "1200").pack(b"".join(fr), msg_id=22)
    old = pager.profile("XL", "1200", short_last=False).pack(b"".join(fr), msg_id=22)
    assert len(new) == 15 and len(old) == 16
    st_new, pcm_new = _run(HOST_LR, new, xl=True, store=200000)
    st_old, pcm_old = _run(HOST_LR, old, xl=True, store=200000)
    assert np.array_equal(pcm_new, pcm_old) and st_old["of"] == "16" and st_new["of"] == "15"
    st, _ = _run(HOST_LR, old[:3], xl=True, store=200000)      # before the last group: presumed short
    assert st["of"] == "15" and st["missing"] == "12:" + ",".join(map(str, range(3, 11)))

    def forge(p, seq):
        q = bytearray(pager._whiten(p))
        q[2], q[3] = (q[2] & 0xC0) | 0x20 | seq >> 8, seq & 0xFF
        c = pager.crc16_ccitt(q[:-2])
        q[-2:] = c.to_bytes(2, "big")
        return pager._whiten(bytes(q))
    bad = [forge(new[14], 15), forge(new[3], 3)]   # k = 7 of m = 7; group 0 isn't the last
    st, _ = _run(HOST_LR, new[:1] + bad, xl=True, store=200000)
    assert [x for x in st["results"].split(",") if x][1:] == ["0", "0"]   # SP_DUP: nothing stored
    assert int(st["got"]) == 20


def _speechy(n, codec, seed):
    """n slots with talk-spurt energies: loud stretches and silent runs of 1-40 slots, random
    voicing (code 3 included) and other bits"""
    rng = np.random.default_rng(seed)
    out, i = bytearray(), 0
    while len(out) < 6 * n:
        loud = i % 2 == 0
        for _ in range(int(rng.integers(3, 30) if loud else rng.integers(1, 40))):
            v = int.from_bytes(rng.integers(0, 256, 6, dtype=np.uint8).tobytes(), "big")
            e = int(rng.integers(20, 31)) if loud else int(rng.integers(0, 4))
            v = v & ~(31 << 34) | e << 34
            out += v.to_bytes(6, "big")
        i += 1
    return bytes(out[:6 * n])


@pytest.mark.parametrize("codec", ["2400", "1200"])
def test_pause_frames_compact_and_expand(codec):
    sys.path.insert(0, ROOT)
    from modem import pager
    fr = _speechy(900, codec, seed=3)
    c, saved = pager.compact_pauses(fr, codec)
    assert saved > 200 and len(c) == len(fr) - 6 * saved
    ex, good = pager.expand_pauses(c, None, codec)
    assert len(ex) == len(fr) and good.all()
    pc = [pager.pause_count(c[i:i + 6]) for i in range(0, len(c), 6)]
    assert sum(pc) == saved + sum(1 for k in pc if k)
    # every slot is the original or (inside a pause) the run's first slot again with its energy
    # code stepping down to 0, and the decoder's voicing state after each run is the original's
    e = pager.energy_codes(fr, codec)
    ee = pager.energy_codes(ex, codec)
    mask = ~(0x1F << 34)
    held = None
    for i in range(0, len(fr), 6):
        if ex[i:i + 6] != fr[i:i + 6]:
            assert e[i // 6] <= 30 - pager.TRIM_BELOW or e[i // 6] == 0
            v, h = int.from_bytes(ex[i:i + 6], "big"), int.from_bytes(ex[held:held + 6], "big")
            assert v & mask == h & mask                 # the run's first slot, its energy aside
            prev = ee[i // 6 - 1]
            assert ee[i // 6] == (prev - pager.PAUSE_FADE[codec] if prev > pager.PAUSE_FADE[codec] else 0)
        else:
            held = i
    assert (ee[1:] < ee[:-1]).sum() > 20                 # some fades happened
    assert pager.fade_slot(pager.fade_slot(fr[:6], 3), 40) == pager.fade_slot(fr[:6], 31)
    st_a = st_b = 0
    for i in range(0, len(fr), 6):
        st_a = pager._prev_v(st_a, pager._voicing_codes(fr[i:i + 6], codec))
        st_b = pager._prev_v(st_b, pager._voicing_codes(ex[i:i + 6], codec))
        nxt = i + 6
        if nxt < len(fr) and e[nxt // 6] >= 20:        # a loud slot follows: the run is over
            assert st_a == st_b
    assert pager.compact_pauses(fr, "600") == (fr, 0)
    # a real slot shaped like a pause head moves to Wo 126; nothing real reads as a pause
    head = pager.pause_frame(7)
    c2, _ = pager.compact_pauses(head + fr, codec)
    assert pager.pause_count(c2[:6]) == 0


@pytest.mark.skipif(not os.path.exists(HOST_LR), reason="needs make -C c/k6")
@pytest.mark.parametrize("codec,name,lost,stream", [("1200", "XL", [], False), ("1200", "XL", [2, 9], False),
                                                    ("1200", "XL", [4], True), ("1200", "L", [7, 30], False),
                                                    ("2400", "XL", [], False), ("2400", "XL", [3, 11], False)])
def test_pause_frames_play_as_the_full_message(codec, name, lost, stream):
    """The K6 plays a compacted message exactly as sotto_dec_fx_m0 plays the full one with the
    gaps filled by the slot before (a lost pause frame: one concealed slot; a lost slot before a
    pause: the fill is concealed too), store-then-play and streaming."""
    sys.path.insert(0, ROOT)
    from modem import pager
    fr = _speechy(1100 if codec == "1200" else 700, codec, seed=5)
    c, saved = pager.compact_pauses(fr, codec)
    assert saved > 100
    prof = pager.profile(name, codec)
    n = len(c) // 6
    pk = [p for i, p in enumerate(prof.pack(c, msg_id=33)) if i not in lost]
    good = np.ones(n, np.uint8)
    for q in lost:
        for f in prof.packet_frames(q, n):
            if f < n:
                good[f] = 0
    ex, eg = pager.expand_pauses(c, good, codec)
    xl = name == "XL"
    if codec == "1200":
        st, pcm = (_stream(HOST_LR, pk, 32, xl=xl) if stream else _run(HOST_LR, pk, xl=xl, store=200000))
        ref_pcm = _dec_lr(_split6(ex), eg)
    else:
        st, pcm = run_host(pk, xl=xl, store=200000)
        ref_pcm = run_dec(_split6(ex), eg)
        pcm = pcm[160:] if len(pcm) == len(ref_pcm) + 160 else pcm
    assert int(st["total"]) == n
    assert len(pcm) == len(ref_pcm) and np.array_equal(pcm, ref_pcm)


def _split6(b):
    return [b[i:i + 6] for i in range(0, len(b), 6)]
