"""The K6 pager's hiss band build (c/k6, -DSP_LR -DSP_VLR -DSP_HISS: pager_host_vlrh): sotto 1200,
600 and 400 messages play at 16 kHz through the K6 hiss layer (c/fixed/sotto_fx_hiss.c with
FX_HISS_FB on the v3.8 core, docs/k6-hiss-port.md), 320 samples a frame, bit-exactly as
c/fixed/sotto_dec_fx_k6 decodes the same superframes and good flags; the frame size is what the
firmware checks its PWM ring against. Needs make -C c/k6 and make -C c fixed/sotto_dec_fx_k6.
SPDX-License-Identifier: MIT"""
import os
import subprocess
import sys
import tempfile

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
K6 = os.path.join(ROOT, "c", "k6")
DEC = os.path.join(ROOT, "c", "fixed", "sotto_dec_fx_k6")
HOST = os.path.join(K6, "pager_host_vlrh")
sys.path.insert(0, ROOT)
from modem import pager  # noqa: E402

pytestmark = pytest.mark.skipif(not (os.path.exists(HOST) and os.path.exists(DEC)),
                                reason="needs make -C c/k6 and make -C c fixed/sotto_dec_fx_k6")

N = {"1200": 2, "600": 4, "400": 6}         # 20 ms frames per slot
SEG = 320                                    # samples per frame at 16 kHz


def frames_of(n, seed=1):
    rng = np.random.default_rng(seed)
    return [bytes(rng.integers(0, 256, 6, dtype=np.uint8)) for _ in range(n)]


def run(packets, xl=False, store=200000, halves=0):
    """the host's status (k=v) and the samples it played"""
    with tempfile.TemporaryDirectory() as d:
        with open(f"{d}/p.bin", "wb") as f:
            f.write(b"".join(packets))
        args = [HOST] + (["-x"] if xl else []) + (["-s", str(halves)] if halves else [])
        args += ["-b", str(store), f"{d}/p.bin", f"{d}/o.raw"]
        out = subprocess.run(args, check=True, capture_output=True, text=True).stdout.split()
        return dict(kv.split("=", 1) for kv in out if "=" in kv), np.fromfile(f"{d}/o.raw", "<i2")


def dec(codec, frames, good):
    """c/fixed/sotto_dec_fx_k6 -16: the decoder's delay dropped; it pads the layer's last blocks
    to whole superframes with silence, which the pager doesn't play"""
    with tempfile.TemporaryDirectory() as d:
        with open(f"{d}/in.bit", "wb") as f:
            f.write(b"".join(frames))
        np.asarray(good, np.uint8).tofile(f"{d}/good.u8")
        subprocess.run([DEC, "-16", f"-{codec}", f"{d}/in.bit", f"{d}/out.raw", f"{d}/good.u8"], check=True)
        return np.fromfile(f"{d}/out.raw", "<i2")


@pytest.mark.parametrize("codec", ["1200", "600", "400"])
@pytest.mark.parametrize("name,lost", [("L", []), ("XL", [3]), ("L", [1, 2])])
def test_hiss_pager_plays_what_the_k6_decoder_plays(codec, name, lost):
    prof = pager.profile(name, codec)
    fr = frames_of(70, seed=7)
    pk = prof.pack(b"".join(fr), msg_id=9)
    st, pcm = run([p for i, p in enumerate(pk) if i not in lost], xl=name == "XL")
    good = [1] * len(fr)
    for q in lost:
        for f in prof.packet_frames(q, len(fr)):
            if f < len(fr):
                good[f] = 0
    assert int(st["total"]) == 70 and int(st["got"]) == sum(good)
    # a 20 ms frame of 320 samples per slot frame: the delay's silence dropped at the start,
    # the core's last frame and the layer's tail played at the end
    assert len(pcm) == 70 * N[codec] * SEG
    ref = dec(codec, fr, good)
    assert len(ref) >= len(pcm) and np.array_equal(pcm, ref[:len(pcm)])
    assert not np.any(ref[len(pcm):])


@pytest.mark.parametrize("codec,n,lost", [("600", 900, [3, 40]), ("1200", 1200, [7])])
def test_hiss_streams_bit_exact(codec, n, lost):
    """streaming (the default) with the radio's 2.5 KB store plays as a store-then-play does"""
    prof = pager.profile("XL", codec)
    pk = [p for i, p in enumerate(prof.pack(b"".join(frames_of(n, seed=5)), msg_id=12)) if i not in lost]
    st, pcm = run(pk, xl=True, store=2560, halves=20 * N[codec])
    ref_st, ref = run(pk, xl=True, store=200000)
    assert int(st["overruns"]) == 0 and int(st["total"]) == n
    assert np.array_equal(pcm, ref) and st["pcm"] == ref_st["pcm"]


def test_pcmhash_tool_knows_the_hiss_build():
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import k6_pcmhash
    fr = b"".join(frames_of(40, seed=3))
    h, n, st = k6_pcmhash.reference(fr, "600", "XL", hiss=True, trim_keep_s=None, pause=False)
    h8, n8, _ = k6_pcmhash.reference(fr, "600", "XL", trim_keep_s=None, pause=False)
    assert n == 40 * N["600"] * SEG and n8 == (40 * N["600"] + 1) * 160 and h != h8
    _, pcm = run(pager.profile("XL", "600").pack(fr, msg_id=k6_pcmhash.msg_id_for(fr)), xl=True)
    assert n == len(pcm) and st["pcm"] == h
