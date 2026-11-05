"""K6 voice pager softmodem tests: the packet format against the spec's test vectors, the
modulators and demodulator in loopback (both PHYs, both baud rates, both payload profiles),
the IC-7100 path models, calibration, CI-V frames and the command line.

Run: python -m pytest tests/test_k6.py
"""
import os
import sys

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from modem import cal, civ, fsk, pager                       # noqa: E402
from modem import k6                                          # noqa: E402
from modem.k6 import main as cli                              # noqa: E402

TEST_FRAMES = b"".join(bytes([f & 0xFF, f >> 8, 0x11, 0x22, 0x33, 0x44]) for f in range(70))
RNG = np.random.default_rng(11)
VOICE = RNG.integers(0, 256, 6 * 150, dtype=np.uint8).tobytes()       # 3 s of "speech"

# spec section 10
PKT0_PLAIN = bytes.fromhex(
    "a7 42 00 00 00 46 00 00 11 22 33 44 08 00 11 22 33 44 10 00 11 22 33 44 18 00 11 22 33 44"
    "20 00 11 22 33 44 28 00 11 22 33 44 30 00 11 22 33 44 38 00 11 22 33 44 00 00 00 00 00 00"
    "00 00 a6 39")
PKT0_AIR = bytes.fromhex(
    "55 55 55 55 55 55 55 55 55 55 55 55 55 55 55 55 07 6b 3e 36"
    "58 a3 1d 9a ed c3 33 24 fb 58 e1 7d 78 97 46 28 67 39 3d d8 7c 2f 89 cb 7f 59 d6 80 8c 70"
    "ea 18 21 71 a0 9b ba ec b6 37 b9 98 c4 86 44 6c 2b 65 78 c4 d5 f7 f5 d5 8a cd e7 d1 4e 09"
    "32 17 79 ba")
S_PKT0_AIR = bytes.fromhex("55" * 16 + "07 6b 3e 36 af e1 1d 9a fc a7 00 60 e2 7a c3 1b 43 d3 cb 92")


def test_crc_and_whitening():
    assert pager.crc16_ccitt(b"123456789") == 0x29B1
    assert pager.pn9(8) == bytes.fromhex("ff e1 1d 9a ed 85 33 24")


def test_spec_vectors():
    pk = pager.L.pack(TEST_FRAMES, 0x42)
    assert len(pk) == 16
    assert bytes(a ^ b for a, b in zip(pk[0], pager.WHITEN)) == PKT0_PLAIN
    air = pager.Air(lead_ms=0)
    assert air.burst(pk[:1])[:len(PKT0_AIR)] == PKT0_AIR
    assert pager.L.packet_frames(0) == list(range(0, 64, 8))
    assert pager.L.packet_frames(9) == [65 + 8 * s for s in range(8)]
    ps = pager.S.pack(TEST_FRAMES, 5)
    assert len(ps) == 40
    assert air.burst(ps[:1])[:len(S_PKT0_AIR)] == S_PKT0_AIR


def test_interleave_loss_and_passes():
    pk = pager.L.pack(TEST_FRAMES, 0x42)
    a = pager.Assembler()
    for i, p in enumerate(pk):
        if i != 3:
            a.push(p)
    fr, good = a.frames(0x42)
    assert [i for i, g in enumerate(good) if not g] == [3, 11, 19, 27, 35, 43, 51, 59]
    a.push(pk[3])                                   # a second pass fills the hole
    fr, good = a.frames(0x42)
    assert good.all() and fr == TEST_FRAMES
    bad = bytearray(pk[5])
    bad[20] ^= 1
    assert a.push(bytes(bad)) is None and a.bad == 1


def test_profile_s_trailing_padding():
    ps = pager.S.pack(TEST_FRAMES[:6 * 37], 3)
    a = pager.Assembler(pager.S)
    for p in ps:
        a.push(p)
    fr, good = a.frames(3)
    assert fr == TEST_FRAMES[:6 * 37]


def _loop(phy, baud, profile=pager.L, channel=None, comp=None, fs_out=fsk.FS, invert=False,
          noise=0.03, frames=VOICE, tones=None):
    pk = profile.pack(frames, 77)
    bits = pager.Air().burst_bits(pk, baud)
    x = fsk.modulate(bits, phy, baud, invert=invert, comp=comp, tones=tones)
    y = channel.apply(x) if channel else x
    y = np.concatenate([np.zeros(3000), y, np.zeros(3000)])
    y = y / np.std(y) + RNG.normal(0, noise, len(y))
    y = fsk.resample(y, fsk.FS, fs_out)
    asm, t = fsk.receive(y, fs_out, phy, baud, profile, tones=tones)
    ok = sum(t_[1] for t_ in t)
    return ok, len(pk), asm, t


@pytest.mark.parametrize("phy,baud", [("D", 1200), ("D", 2400), ("F", 1200)])
def test_loopback(phy, baud):
    ok, n, asm, t = _loop(phy, baud, fs_out=44100)
    assert ok == n
    fr, good = asm.frames(77)
    assert fr == VOICE and good.all()


def test_loopback_other_tones():
    """F at 2400 baud with the 1200/1800 pair (the only one the K6 syncs on at 2400)."""
    ok, n, _, _ = _loop("F", 2400, tones=(1200.0, 1800.0))
    assert ok == n


def test_loopback_intermediate_baud():
    """1600 and 1800 baud, F with the 1200/1800 pair and D, for the K6 with REG_72 poked."""
    for baud in (1600, 1800):
        for phy in ("F", "D"):
            ok, n, _, _ = _loop(phy, baud, tones=(1200.0, 1800.0) if phy == "F" else None)
            assert ok == n, (phy, baud, ok, n)


def test_loopback_profile_s():
    ok, n, asm, _ = _loop("D", 1200, pager.S)
    assert ok == n and asm.frames(77 & 15)[0] == VOICE      # profile S msg_id is 4 bits


def test_polarity_is_detected():
    ok, n, _, t = _loop("D", 1200, invert=True)
    assert ok == n and all(t_[2] for t_ in t)


def test_ffsk_survives_a_voice_path():
    """Profile F through pre-emphasis, a 4th-order 300 Hz high-pass and a 3 kHz low-pass."""
    ch = fsk.Channel(pre_emph=300.0, hpf_order=4)
    ok, n, _, _ = _loop("F", 1200, channel=ch)
    assert ok == n


def test_nrz_needs_a_flat_path():
    """Profile D: fine through a 9600-style input (20 Hz coupling), broken by a voice path."""
    ok, n, _, _ = _loop("D", 1200, channel=fsk.Channel(hpf=20.0, hpf_order=1, lpf=6000.0))
    assert ok == n
    ok, n, _, _ = _loop("D", 1200, channel=fsk.Channel(pre_emph=300.0))
    assert ok < n // 4


def test_calibration_fit_and_compensation():
    ch = fsk.Channel(pre_emph=300.0)                # voice path, gentle 2nd-order high-pass
    x = cal.signal(0.5)
    y = -2.0 * ch.apply(x)                          # inverted, different gain
    y = fsk.resample(y, fsk.FS, 44100)
    y = y + RNG.normal(0, 0.002, len(y))
    m = cal.fit(y, 44100 * 1.00005)                 # recorder clock 50 ppm off
    assert m.polarity == -1
    assert abs(m.tone_peak - 1.0) < 0.05
    f = np.array([200.0, 500.0, 2000.0, 3000.0])
    want = np.abs(ch.response(f)) / abs(ch.response(np.array([1000.0]))[0])
    assert np.allclose(20 * np.log10(np.abs(m.response(f))), 20 * np.log10(want), atol=1.0)
    ok, n, _, _ = _loop("D", 1200, channel=ch, comp=m, frames=VOICE[:6 * 64])
    assert ok == n


def test_civ_frames():
    assert civ.ptt(True) == bytes.fromhex("fe fe 88 e0 1c 00 01 fd")
    assert civ.ptt(False) == bytes.fromhex("fe fe 88 e0 1c 00 00 fd")
    assert civ.mode_fm() == bytes.fromhex("fe fe 88 e0 06 05 01 fd")


def test_cli(tmp_path):
    sot, wav, back, good = (str(tmp_path / n) for n in ("m.sot", "tx.wav", "b.sot", "g.u8"))
    open(sot, "wb").write(VOICE)
    assert cli(["tx", sot, wav, "--cw-id", "N0CALL", "--rate", "44100", "--passes", "2"]) == 0
    assert cli(["rx", wav, back, "--good", good]) == 0
    assert open(back, "rb").read() == VOICE
    assert np.fromfile(good, np.uint8).all()
    assert cli(["test", wav, "--repeat", "3", "--phy", "F"]) == 0
    from modem.k6 import _read
    x, fs = _read(wav)
    asm, t = fsk.receive(x, fs, "F", 1800)
    assert sum(t_[1] for t_ in t) == 3 and asm.messages[0x42]["pk"][0] == \
        [TEST_FRAMES[f * 6:f * 6 + 6] for f in range(0, 64, 8)]
    assert cli(["send", sot, "--dry-run", "--ptt", "none"]) == 0
    assert cli(["cal", wav]) == 0


def test_byte_swapped_words_are_accepted():
    """The K6's own test burst arrives with each FIFO word byte-swapped (first on-air test,
    2026-11-04); rx accepts it and says so."""
    pk = pager.L.pack(TEST_FRAMES, 0x42)
    a = pager.Assembler()
    assert a.push(pager.swap_words(pk[0])) is not None
    assert a.swapped == 1 and a.bad == 0
    assert pager.swap_words(b"\x01\x02\x03\x04") == b"\x02\x01\x04\x03"


def test_prime_sends_a_throwaway_first_copy(tmp_path):
    """--prime puts a copy of the first packet in front; the real first packet then gets
    the second packet's extra preamble, and every packet still decodes."""
    from modem.k6 import _read
    wav = str(tmp_path / "p.wav")
    for prime, n in ((["--no-prime"], 3), (["--prime"], 4)):
        assert cli(["test", wav, "--repeat", "3", "--phy", "F"] + prime) == 0
        x, fs = _read(wav)
        asm, t = fsk.receive(x, fs, "F", 1800)
        assert sum(t_[1] for t_ in t) == n
    assert cli(["test", wav, "--repeat", "3", "--phy", "F", "--no-prime", "--first-preamble", "32"]) == 0
    x, fs = _read(wav)
    assert sum(t_[1] for t_ in fsk.receive(x, fs, "F", 1800)[1]) == 3


def test_later_preamble():
    """XL's framing: 16 bytes of 0x55 before the first packet, 24 (+ the second's extra) after."""
    a = pager.Air(lead_ms=0, tail_bytes=0, tail_ms=0, later_preamble_bytes=24)
    raw = a.burst([bytes(128)] * 3)
    extra = a._extra(1200)
    assert len(raw) == 3 * (4 + 128) + 16 + 24 + extra + 24
    assert raw[:16] == b"\x55" * 16 and raw[16:20] == pager.SYNC
    assert a.seconds(3, 128) == len(raw) * 8 / 1200


def test_control_packet_on_air(tmp_path):
    """`ctl` writes control packets that our own demodulator reads back, in D and then F
    (--also), at both lengths; the record carries the settings the K6 applies."""
    import soundfile as sf
    from modem import fsk, k6, pager
    for payload in ("L", "XL"):
        out = tmp_path / f"ctl-{payload}.wav"
        assert cli(["ctl", str(out), "--phy", "D", "--also", "F", "--payload", payload,
                        "--set", "phy=F", "frames=XL", "auto=1", "--action", "play",
                        "--seq", "17", "--copies", "2"]) == 0
        x, fs = sf.read(out)
        want = pager.control_record(17, {"phy": "F", "frames": "XL", "auto": 1}, "play")[:6]
        y = fsk.resample(x, fs, fsk.FS)
        n = pager.PROFILES[payload].payload
        for phy in "DF":
            got = fsk.Demodulator(phy, k6.default_baud(phy), fsk.FS, n).demodulate(y)
            recs = [pager.parse_control(p) for _, p, inv, _ in got if not inv]
            assert recs.count(want) >= 2, (payload, phy, recs)
    assert want == bytes([0xA8, 17, 0x0B, 0x0B, 0x00, 0x01])


def test_control_record_fields():
    from modem import pager
    r = pager.control_record(3, {"phy": "D", "baud": 2400, "var": 5, "vol": 6, "emph": 0})
    assert r[:6] == bytes([0xA8, 3, 1 | 4 | 16 | 32 | 64, 4, 5 | 6 << 4, 0])
    assert pager.crc16_ccitt(r[:6]) == r[6] << 8 | r[7]
    assert pager.control_record(4)[2] == 0          # confirmation only: nothing changes
    assert pager.control_record(5, {"idlekeep": 1})[2:4] == bytes([128, 128])
    import pytest
    with pytest.raises(ValueError):
        pager.control_record(1, {"freq": 440850})
    r = pager.control_reg_record(9, 0x58, 0x73E9)
    assert r[:6] == bytes([0xA8, 9, 0x58, 0x73, 0xE9, 4])
    assert pager.parse_control(pager.control_packet(9, length=64, reg=(0x58, 0x73E9)))[:6] == r[:6]
    with pytest.raises(ValueError):
        pager.control_reg_record(1, 0x30, 0)            # REG_30 (TX/RX enables): refused


def test_boot_dump_parses_log():
    data = bytes(range(256)) * 32
    lines = [f"K6 BOOT off={o:04x} {data[o:o + 32].hex()}\r\n" for o in range(0, 0x2000, 32)]
    log = "K6 CFG why=tick\r\n" + "".join(lines) + "K6 BOOT end seq=7\r\n"
    assert pager.boot_dump(log) == data
    with pytest.raises(ValueError):
        pager.boot_dump("".join(lines[:-1]))
    assert pager.control_record(7, None, "boot")[5] == 6


def test_default_baud_follows_phy(tmp_path):
    """F defaults to 1800 baud (the K6's F boot rate), D to 1200; ctl --also switches with it."""
    assert k6.default_baud("F") == 1800 and k6.default_baud("D") == 1200
    out = tmp_path / "c.wav"
    assert cli(["ctl", str(out), "--phy", "D", "--also", "F", "--set", "phy=F"]) == 0
    assert out.stat().st_size > 0
