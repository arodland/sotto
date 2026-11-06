"""The K6 pager with sotto's codebook in SPI flash (c/k6/sp_cb.h): the decoder reads each
20-byte vector from the (emulated) flash instead of the table, and must play exactly what the
table build plays, in v3.7 and sotto 1200, stored or streamed, with lost packets. Also the
image format, the install path, and that a missing or wrong image plays nothing.
Needs make -C c/k6. SPDX-License-Identifier: MIT"""
import os
import struct
import subprocess
import sys
import tempfile
import zlib

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
K6 = os.path.join(ROOT, "c", "k6")
sys.path.insert(0, ROOT)
from modem import pager  # noqa: E402


def exe(name):
    return os.path.join(K6, name)


pytestmark = pytest.mark.skipif(
    not all(os.path.exists(exe(n)) for n in ("cb_image", "pager_host", "pager_host_lr", "pager_host_cbs",
                                             "pager_host_lr_cbs", "pager_host_cbi")),
    reason="needs make -C c/k6")

TABLE_BYTES = 20 * (512 + 512 + 256 + 256)


def frames_of(n, seed=1):
    rng = np.random.default_rng(seed)
    return [bytes(rng.integers(0, 256, 6, dtype=np.uint8)) for _ in range(n)]


@pytest.fixture(scope="module")
def image():
    with tempfile.TemporaryDirectory() as d:
        subprocess.run([exe("cb_image"), f"{d}/cb.bin"], check=True, capture_output=True)
        with open(f"{d}/cb.bin", "rb") as f:
            return f.read()


def run(host, packets, xl=False, store=6144, halves=0, flash=None, save=False):
    with tempfile.TemporaryDirectory() as d:
        with open(f"{d}/p.bin", "wb") as f:
            f.write(b"".join(packets))
        args = [exe(host)]
        if flash is not None:
            with open(f"{d}/cb.bin", "wb") as f:
                f.write(flash)
            args += ["-F", f"{d}/cb.bin"]
        if save:
            args += ["-W", f"{d}/out-cb.bin"]
        if halves:
            args += ["-s", str(halves)]
        args += (["-x"] if xl else []) + ["-b", str(store), f"{d}/p.bin", f"{d}/o.raw"]
        out = subprocess.run(args, check=True, capture_output=True, text=True).stdout.split()
        st = dict(kv.split("=", 1) for kv in out)
        if save:
            with open(f"{d}/out-cb.bin", "rb") as f:
                st["saved"] = f.read()
        return st, np.fromfile(f"{d}/o.raw", "<i2")


def test_image_format(image):
    """header: K6CB, version 1, 20 bytes a vector, 30,720 table bytes, 4 stages of 9 9 8 8 bits,
    the table's CRC-32 and the header's (zlib's CRC-32); then the table"""
    h, t = image[:32], image[32:]
    assert len(t) == TABLE_BYTES
    assert h[:4] == b"K6CB"
    ver, vec, n = struct.unpack_from("<HHI", h, 4)
    assert (ver, vec, n) == (1, 20, TABLE_BYTES)
    assert list(h[12:17]) == [4, 9, 9, 8, 8] and h[17:24] == bytes(7)
    crc, hcrc = struct.unpack_from("<II", h, 24)
    assert crc == zlib.crc32(t) and hcrc == zlib.crc32(h[:28])
    out = subprocess.run([exe("cb_image"), "-c"], check=True, capture_output=True, text=True).stdout
    assert int(out, 16) == crc
    # every vector's 20th value is the zero pad (low byte of its 10th word)
    assert all(t[20 * v + 18] == 0 for v in range(TABLE_BYTES // 20))


@pytest.mark.parametrize("codec,host,name,lost", [
    ("2400", "pager_host_cbs", "L", []), ("2400", "pager_host_cbs", "XL", [3]),
    ("1200", "pager_host_lr_cbs", "L", []), ("1200", "pager_host_lr_cbs", "XL", [3, 9])])
def test_spi_codebook_plays_what_the_table_plays(image, codec, host, name, lost):
    """the same packets through the table build and the SPI build: identical PCM and pcm= hash,
    with the decoder reading a vector per stage per frame (v3.7: 4 per 20 ms frame; sotto 1200:
    stages 1-2 for A and one stage-3 vector for B per 40 ms, fewer for a lost slot)"""
    prof = pager.profile(name, codec)
    n = 150
    pk = [p for i, p in enumerate(prof.pack(b"".join(frames_of(n, seed=11)), msg_id=5)) if i not in lost]
    table = "pager_host" if codec == "2400" else "pager_host_lr"
    st_t, pcm_t = run(table, pk, xl=name == "XL")
    st_s, pcm_s = run(host, pk, xl=name == "XL", flash=image)
    assert st_s["cb"] == "1" and st_s["mode"] == "1"
    assert int(st_s["total"]) == n and len(pcm_s) > 0
    assert np.array_equal(pcm_t, pcm_s) and st_t["pcm"] == st_s["pcm"]
    reads = int(st_s["cb_reads"])
    if codec == "2400":           # every frame's fields are unpacked, lost ones too
        assert reads == 4 * n
    elif not lost:
        assert reads == 3 * n
    else:                         # a lost slot is rebuilt from its neighbours' shapes
        assert 3 * int(st_s["got"]) <= reads < 3 * n


def test_spi_codebook_streams_bit_exact(image):
    """sotto 1200 live streaming (the radio's default) through the SPI build: what the table
    build streams, no overruns"""
    prof = pager.profile("XL", "1200")
    pk = [p for i, p in enumerate(prof.pack(b"".join(frames_of(1200, seed=5)), msg_id=12)) if i not in (3, 17)]
    st_t, pcm_t = run("pager_host_lr", pk, xl=True, store=4096, halves=32)
    st_s, pcm_s = run("pager_host_lr_cbs", pk, xl=True, store=4096, halves=32, flash=image)
    assert int(st_s["overruns"]) == 0 and np.array_equal(pcm_t, pcm_s) and st_t["pcm"] == st_s["pcm"]


@pytest.mark.parametrize("damage,code", [("none", -1), ("header", -2), ("other", -3), ("table", -4)])
def test_bad_image_plays_nothing(image, damage, code):
    """no image, a damaged header, another codebook's good image, or a damaged table: sp_cb_setup
    says which, and the message is stored but not played (no garbage from a wrong codebook)"""
    img = bytearray(image)
    if damage == "none":
        img = None
    elif damage == "header":
        img[5] ^= 1
    elif damage == "other":       # a self-consistent image of a different table
        img[32 + 100] ^= 0x10
        struct.pack_into("<I", img, 24, zlib.crc32(bytes(img[32:])))
        struct.pack_into("<I", img, 28, zlib.crc32(bytes(img[:28])))
    else:
        img[32 + 5000] ^= 0x01
    prof = pager.profile("L", "2400")
    pk = prof.pack(b"".join(frames_of(40, seed=3)), msg_id=2)
    st, pcm = run("pager_host_cbs", pk, flash=None if img is None else bytes(img))
    assert st["cb"] == str(code)
    assert int(st["got"]) == 40 and len(pcm) == 0 and st["samples"] == "0" and st["cb_reads"] == "0"


def test_install_writes_the_image_once(image):
    """the install build (table linked, SP_CB_INSTALL) finds erased flash, writes the image and
    plays from it (cb=2); given the image already there it only checks it (cb=1); either way it
    plays what the table build plays, and what it wrote is cb_image's image byte for byte"""
    prof = pager.profile("L", "2400")
    pk = prof.pack(b"".join(frames_of(60, seed=4)), msg_id=7)
    st_t, pcm_t = run("pager_host", pk)
    st_i, pcm_i = run("pager_host_cbi", pk, save=True)
    assert st_i["cb"] == "2" and st_i["mode"] == "2" and st_i["saved"] == image
    assert np.array_equal(pcm_t, pcm_i)
    st_2, pcm_2 = run("pager_host_cbi", pk, flash=image)
    assert st_2["cb"] == "1" and np.array_equal(pcm_t, pcm_2)
    bad = bytearray(image)
    bad[32 + 77] ^= 4
    st_3, pcm_3 = run("pager_host_cbi", pk, flash=bytes(bad), save=True)
    assert st_3["cb"] == "2" and st_3["saved"] == image and np.array_equal(pcm_t, pcm_3)


def test_bench_sum(image):
    """the radio's `K6 CB bench n=1000 ... sum=` line: the same reads on this machine give the
    same sum in both decoders' builds, and 0 with no image"""
    with tempfile.TemporaryDirectory() as d:
        with open(f"{d}/cb.bin", "wb") as f:
            f.write(image)
        sums = {subprocess.run([exe(h), "-B", "1000", "-F", f"{d}/cb.bin"], check=True, capture_output=True,
                               text=True).stdout.split()[-1] for h in ("pager_host_cbs", "pager_host_lr_cbs")}
    assert len(sums) == 1 and sums != {"sum=00000000"}
    out = subprocess.run([exe("pager_host_cbs"), "-B", "1000"], check=True, capture_output=True, text=True).stdout
    assert out.split() == ["cb=-1", "n=1000", "sum=00000000"]
