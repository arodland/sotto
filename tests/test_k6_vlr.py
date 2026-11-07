"""The K6 pager's sotto 600 / 400 build (c/k6, -DSP_LR -DSP_VLR): packets with magic 0xAC / 0xAD
are stored as any other and play through the fixed-point very-low-rate decoder 20 ms at a time,
bit-exactly as c/fixed/sotto_vlr_dec_fx decodes the same superframes and good flags; the same
build still plays sotto 1200; the shape codebooks can live in (emulated) SPI flash at 0x108000
(c/k6/sp_vlr.h) and install themselves once. Needs make -C c/k6 and make -C c VARIANT=v3.7 fixed.
SPDX-License-Identifier: MIT"""
import os
import subprocess
import sys
import tempfile

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
K6 = os.path.join(ROOT, "c", "k6")
DEC = os.path.join(ROOT, "c", "fixed", "sotto_vlr_dec_fx")
sys.path.insert(0, ROOT)
from modem import pager  # noqa: E402


def exe(name):
    return os.path.join(K6, name)


pytestmark = pytest.mark.skipif(
    not (all(os.path.exists(exe(n)) for n in ("pager_host", "pager_host_lr", "pager_host_vlr", "pager_host_vlr_cbs",
                                              "pager_host_vlr_cbi", "vlr_image", "cb_image")) and os.path.exists(DEC)),
    reason="needs make -C c/k6 and make -C c VARIANT=v3.7 fixed")

N = {"600": 4, "400": 6}


def frames_of(n, seed=1):
    rng = np.random.default_rng(seed)
    return [bytes(rng.integers(0, 256, 6, dtype=np.uint8)) for _ in range(n)]


def run(host, packets, xl=False, store=6144, halves=0, flash=(), save=()):
    """host's status (k=v) and the samples it played; flash: (option, file) pairs to load"""
    with tempfile.TemporaryDirectory() as d:
        with open(f"{d}/p.bin", "wb") as f:
            f.write(b"".join(packets))
        args = [exe(host)] + (["-x"] if xl else []) + (["-s", str(halves)] if halves else [])
        for opt, path in flash:
            args += [opt, path]
        for opt, path in save:
            args += [opt, path]
        args += ["-b", str(store), f"{d}/p.bin", f"{d}/o.raw"]
        out = subprocess.run(args, check=True, capture_output=True, text=True).stdout.split()
        return dict(kv.split("=", 1) for kv in out if "=" in kv), np.fromfile(f"{d}/o.raw", "<i2")


def dec(codec, frames, good):
    """c/fixed/sotto_vlr_dec_fx, the decoder's delay dropped, without the flush's padding"""
    with tempfile.TemporaryDirectory() as d:
        with open(f"{d}/in.bit", "wb") as f:
            f.write(b"".join(frames))
        np.asarray(good, np.uint8).tofile(f"{d}/good.u8")
        subprocess.run([DEC, codec, f"{d}/in.bit", f"{d}/out.raw", f"{d}/good.u8"], check=True)
        y = np.fromfile(f"{d}/out.raw", "<i2")
    return y[:len(frames) * N[codec] * 160 + 160]          # + the core's last 20 ms


@pytest.fixture(scope="module")
def images():
    d = tempfile.mkdtemp()
    subprocess.run([exe("vlr_image"), f"{d}/vlr.bin"], check=True, capture_output=True)
    subprocess.run([exe("cb_image"), f"{d}/cb.bin"], check=True, capture_output=True)
    return f"{d}/cb.bin", f"{d}/vlr.bin"


@pytest.mark.parametrize("codec", ["600", "400"])
@pytest.mark.parametrize("name,lost", [("L", []), ("XL", []), ("XL", [3]), ("L", [1, 2])])
def test_vlr_pager_plays_what_the_fixed_decoder_plays(codec, name, lost):
    prof = pager.profile(name, codec)
    fr = frames_of(70, seed=7)
    pk = prof.pack(b"".join(fr), msg_id=9)
    st, pcm = run("pager_host_vlr", [p for i, p in enumerate(pk) if i not in lost], xl=name == "XL")
    good = [1] * len(fr)
    for q in lost:
        for f in prof.packet_frames(q, len(fr)):
            if f < len(fr):
                good[f] = 0
    assert st["vlr"] == "1" and int(st["total"]) == 70 and int(st["got"]) == sum(good)
    assert len(pcm) == 70 * N[codec] * 160 + 160
    assert np.array_equal(pcm, dec(codec, fr, good))


def test_each_build_plays_only_its_codecs():
    """Every build stores every magic; the VLR build plays sotto 1200, 600 and 400 but not v3.7,
    and the older builds don't play sotto 600 / 400."""
    fr = b"".join(frames_of(24, seed=3))
    plays = {"pager_host": {"2400"}, "pager_host_lr": {"1200"}, "pager_host_vlr": {"1200", "600", "400"}}
    for host, codecs in plays.items():
        for codec in ("2400", "1200", "600", "400"):
            st, pcm = run(host, pager.profile("L", codec).pack(fr, msg_id=4))
            assert int(st["total"]) == 24 and int(st["got"]) == 24 and st["bad"] == "0", (host, codec)
            assert (len(pcm) > 0) == (codec in codecs), (host, codec, len(pcm))


def test_a_new_codec_starts_a_new_message():
    """Same msg_id, profile and total, other magic: the store starts over (the slots mean
    something else)."""
    fr = b"".join(frames_of(16, seed=4))
    pk = (pager.profile("L", "600", short_last=False).pack(fr, msg_id=5)
          + pager.profile("L", "400", short_last=False).pack(fr, msg_id=5)[:1])
    st, _ = run("pager_host_vlr", pk)
    r = [x for x in st["results"].split(",") if x]
    assert r[:2] == ["2", "1"] and r[-1] == "2" and int(st["got"]) == 2


def test_auto_gain_reads_the_energy_mean():
    """sp_auto_gain_q8 takes sotto 600 / 400's energy mean (bits 13..17): a loud message gets less
    gain than a quiet one."""
    def sf(em):
        v = (0 << 45) | (64 << 38) | (4 << 35) | (em << 30)
        return v.to_bytes(6, "big")
    g = {}
    for em in (22, 31):
        st, _ = run("pager_host_vlr", pager.profile("L", "600").pack(sf(em) * 8, msg_id=em))
        g[em] = int(st["auto_gain"])
    assert g[22] == 1378 and g[31] == 133


@pytest.mark.parametrize("codec,n,lost", [("600", 1500, []), ("600", 1500, [3, 40]), ("400", 900, [7])])
def test_vlr_streams_bit_exact(codec, n, lost):
    """Streaming (the default): a message longer than the 4 KB ring (640 slots) plays while it
    arrives, as a store-then-play of the same packets plays it. At 1800 baud an XL packet takes
    640 ms on air and holds 1.6 s of sotto 600, so the sender runs ahead; here playback keeps
    pace (N x 20 frames per packet), as a paced sender would let it."""
    prof = pager.profile("XL", codec)
    pk = [p for i, p in enumerate(prof.pack(b"".join(frames_of(n, seed=5)), msg_id=12)) if i not in lost]
    st, pcm = run("pager_host_vlr", pk, xl=True, store=4096, halves=20 * N[codec])
    ref_st, ref = run("pager_host_vlr", pk, xl=True, store=200000)
    assert int(st["ring"]) == 640 and int(st["overruns"]) == 0 and int(st["total"]) == n
    assert np.array_equal(pcm, ref) and st["pcm"] == ref_st["pcm"]


def test_image_format(images):
    _, vlr = images
    b = open(vlr, "rb").read()
    import zlib
    assert b[:4] == b"K6VL" and int.from_bytes(b[4:6], "little") == 1 and int.from_bytes(b[6:8], "little") == 19
    n = int.from_bytes(b[8:12], "little")
    assert n == 21280 == len(b) - 32
    assert int.from_bytes(b[12:14], "little") == 600 and int.from_bytes(b[14:16], "little") == 400
    assert int.from_bytes(b[24:28], "little") == zlib.crc32(b[32:])
    assert int.from_bytes(b[28:32], "little") == zlib.crc32(b[:28])
    crc = subprocess.run([exe("vlr_image"), "-c"], check=True, capture_output=True, text=True).stdout.strip()
    assert int(crc, 16) == zlib.crc32(b[32:])


@pytest.mark.parametrize("codec", ["600", "400"])
def test_spi_tables_play_what_the_tables_play(images, codec):
    cb, vlr = images
    pk = pager.profile("XL", codec).pack(b"".join(frames_of(90, seed=2)), msg_id=3)
    st_t, pcm_t = run("pager_host_vlr", pk, xl=True)
    st_s, pcm_s = run("pager_host_vlr_cbs", pk, xl=True, flash=[("-F", cb), ("-V", vlr)])
    assert st_s["cb"] == "1" and st_s["vlr"] == "1" and int(st_s["vlr_reads"]) > 0 and st_s["cb_reads"] == "0"
    assert len(pcm_s) > 0 and np.array_equal(pcm_s, pcm_t) and st_s["pcm"] == st_t["pcm"]


@pytest.mark.parametrize("damage,code", [(None, "-1"), (0, "-1"), (5, "-2"), (100, "-4")])
def test_missing_or_bad_tables_play_nothing_but_1200_still_plays(images, damage, code):
    cb, vlr = images
    with tempfile.TemporaryDirectory() as d:
        flash = [("-F", cb)]
        if damage is not None:
            b = bytearray(open(vlr, "rb").read())
            b[damage] ^= 0x40
            open(f"{d}/bad.bin", "wb").write(b)
            flash.append(("-V", f"{d}/bad.bin"))
        fr = b"".join(frames_of(40, seed=6))
        st, pcm = run("pager_host_vlr_cbs", pager.profile("XL", "600").pack(fr, msg_id=1), xl=True, flash=flash)
        assert st["vlr"] == code and len(pcm) == 0 and int(st["got"]) == 40
        st, pcm = run("pager_host_vlr_cbs", pager.profile("XL", "1200").pack(fr, msg_id=2), xl=True, flash=flash)
        assert st["vlr"] == code and len(pcm) == 40 * 320


def test_install_writes_the_image_once(images):
    """pager_host_vlr_cbi (SP_VLR_INSTALL): on blank flash it writes the image (vlr=2), which is
    byte for byte vlr_image's, and plays from it; with the image there it only checks (vlr=1)."""
    cb, vlr = images
    pk = pager.profile("L", "400").pack(b"".join(frames_of(30, seed=8)), msg_id=7)
    _, pcm_t = run("pager_host_vlr", pk)
    with tempfile.TemporaryDirectory() as d:
        st, pcm = run("pager_host_vlr_cbi", pk, flash=[("-F", cb)], save=[("-X", f"{d}/w.bin")])
        assert st["vlr"] == "2" and np.array_equal(pcm, pcm_t)
        assert open(f"{d}/w.bin", "rb").read() == open(vlr, "rb").read()
        st, pcm = run("pager_host_vlr_cbi", pk, flash=[("-F", cb), ("-V", vlr)])
        assert st["vlr"] == "1" and np.array_equal(pcm, pcm_t)


def test_pcmhash_tool_knows_the_new_codecs():
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import k6_pcmhash
    fr = b"".join(frames_of(50, seed=11))
    h, n, st = k6_pcmhash.reference(fr, "600", "XL", [2])
    assert n == 50 * 4 * 160 + 160 - 0 and st["vlr"] == "1"
