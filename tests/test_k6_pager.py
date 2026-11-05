"""K6 voice pager receive path (c/k6/): packets in the spec's format (c/k6/k6fsk_ref.py) go
through the pager's reassembly and its Cortex-M0 decoder path, and must play exactly what the
fixed-point M0 decoder plays from the same frames and good flags. Needs make -C c/k6 and
make -C c VARIANT=v3.7 fixed. SPDX-License-Identifier: MIT"""
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
    """128-byte packets, 20 frames each: the same header as L, frames g*160 + s*8 + k"""
    sys.path.insert(0, ROOT)
    from modem import pager
    fr = frames_of(300, 6)
    px = pager.XL.pack(b"".join(fr), 7)
    assert len(px) == 16 and all(len(p) == 128 for p in px)
    lost = {2, 13}
    keep = [p for i, p in enumerate(px) if i not in lost]
    st, pcm = run_host(keep, xl=True)
    good = [1] * 300
    for seq in lost:
        for f in pager.XL.packet_frames(seq):
            if f < 300:
                good[f] = 0
    assert st["total"] == "300" and st["of"] == "16" and st["packets"] == "14" and st["complete"] == "0"
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
    assert st["complete"] == "1" and st["of"] == "8"
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
    pk = pager.PROFILES[prof].pack(fr, 0x42)
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
        for f in prof.packet_frames(q):
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
