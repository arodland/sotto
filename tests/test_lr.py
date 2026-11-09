"""sotto 1200 (48 bits / 40 ms): the Python reference (sim/sotto_lr.py) against the C port
(c/sotto_lr.c, float) and the fixed-point decoder (c/fixed/sotto_fx_lr.c), and its frame
layout. Needs make -C c VARIANT=v3.7 all fixed for the C tests. SPDX-License-Identifier: MIT"""
import os
import re
import subprocess
import sys
import tempfile

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sim"))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import vocoder_lab as vl  # noqa: E402
import sotto_lr as lr  # noqa: E402

C = os.path.join(ROOT, "c")
need_c = pytest.mark.skipif(not os.path.exists(os.path.join(C, "fixed", "sotto_dec_fx_m0")),
                            reason="needs make -C c VARIANT=v3.7 all fixed")


def random_superframes(n, seed=1):
    """n superframes of random fields (any 48 bits decode), with some silent anchors"""
    rng = np.random.default_rng(seed)
    b = rng.integers(0, 2, (n, 48)).astype(np.int8)
    b[rng.random(n) < 0.15, 9:14] = 0
    return b


def c_header():
    s = open(os.path.join(C, "sotto_lr_tables.h")).read()
    tab = {m[0]: [int(v) for v in m[1].split(",")] for m in re.findall(r"lr_(\w+)\[8\] = \{([^}]*)\}", s)}
    num = {m[0]: float(m[1]) for m in re.findall(r"#define LR_(\w+) ([-\d.]+)", s)}
    return tab, num


def test_c_constants_match_python():
    tab, num = c_header()
    c = lr.Sotto1200()
    assert tab["w_tab"] == list(c.W_TAB) and tab["w_abs"] == list(c.W_ABS) and tab["e_tab"] == list(c.E_TAB)
    assert num["NA"] == len(c.A_STAGES) and c.A_STAGES == tuple(range(len(c.A_STAGES)))
    assert (num["BST"], num["BG"]) == c.B_RES and num["JOINT"] == c.JOINT
    assert (num["WB_BITS"], num["EB_BITS"]) == (c.B_WO_BITS, c.B_E_BITS)
    assert num["INTERP"] == c.INTERP
    h = open(os.path.join(C, "sotto.h")).read()
    assert int(re.search(r"#define SOTTO_LR_DEC_DELAY (\d+)", h)[1]) == c.INTERP + 1
    assert num["W_ZERO"] == c.W_ZERO and num["E_ZERO"] == c.E_ZERO and c.E_TAB[c.E_ZERO] == 0


def test_layout_is_48_bits_and_starts_like_v37():
    c = lr.Sotto1200()
    lay = c.layout()
    assert sum(w for _, w in lay) == 48
    assert lay[:3] == [("ca", 2), ("wa", 7), ("ea", 5)]       # energy code at bits 9-13, as v3.7


def test_b_frame_rebuilt_from_anchors():
    c = lr.Sotto1200()
    b = random_superframes(3, seed=5)
    fp = c.frame_params(b)
    f0, f1 = c.unpack(b[0]), c.unpack(b[1])
    assert fp[1]["ei"] == c.e_b(f0["ea"], f1["ea"], f0["de"])
    want = 0.5 * (c.a_shape(f0["si"]) + c.a_shape(f1["si"])) + 1.5 * c.vq.cb[2][f0["sb"]]
    got = (fp[1]["shape"] @ vl.DCT.T)[1:]
    assert np.allclose(got, want, atol=1e-9)


def run(*a):
    subprocess.run([str(x) for x in a], check=True)


def c_decode(bits, good, exe):
    return c_decode_flag(bits, good, exe, "-1200")


def c_decode_flag(bits, good, exe, flag):
    with tempfile.TemporaryDirectory() as d:
        np.packbits(bits.astype(np.uint8), axis=1).tofile(f"{d}/b")
        np.asarray(good, np.uint8).tofile(f"{d}/g")
        run(exe, flag, f"{d}/b", f"{d}/o", f"{d}/g")
        return np.fromfile(f"{d}/o", "<i2").astype(float)


def snr(a, b):
    n = min(len(a), len(b))
    return 10 * np.log10(np.sum(a[:n] ** 2) / (np.sum((a[:n] - b[:n]) ** 2) + 1e-9))


@need_c
def test_c_decoder_matches_python_with_losses():
    b = random_superframes(60)
    good = np.random.default_rng(2).random(60) > 0.2
    good[0] = True
    good[19:45] = True                    # lone losses (rebuilt), runs of 2 and 3 (held)
    good[[20, 25, 30, 31, 40, 41, 42]] = False
    c = lr.Sotto1200()
    yp = vl.synthesise(c.decode(b, good)) * 32768
    assert c.rebuilt[[20, 25]].all() and not c.rebuilt[[30, 31, 40, 41, 42]].any()
    yc = c_decode(b, good, os.path.join(C, "sotto_dec"))
    assert len(yc) == len(yp) == 60 * 320
    assert snr(yp, yc) > 80
    for n in (1, 2, 3, 4):                # short streams: the flush still returns every superframe
        assert len(c_decode(b[:n], good[:n] | True, os.path.join(C, "sotto_dec"))) == n * 320


@need_c
def test_fixed_decoders_agree_and_follow_float():
    b = random_superframes(60, seed=3)
    good = np.random.default_rng(4).random(60) > 0.1
    yf = c_decode(b, good, os.path.join(C, "sotto_dec"))
    ys = [c_decode(b, good, os.path.join(C, "fixed", f"sotto_dec_fx{s}")) for s in ("", "_m0", "_arm")]
    assert np.array_equal(ys[0], ys[1]) and np.array_equal(ys[0], ys[2])     # M0 and M4 paths bit-exact
    for s in ("", "_m0"):                # the 20 ms half calls (the K6's) give the same samples
        assert np.array_equal(ys[0], c_decode_flag(b, good, os.path.join(C, "fixed", f"sotto_dec_fx{s}"), "-1200half"))
    assert np.array_equal(yf, c_decode_flag(b, good, os.path.join(C, "sotto_dec"), "-1200half"))
    for n in (1, 2, 3):
        assert np.array_equal(c_decode(b[:n], good[:n], os.path.join(C, "fixed", "sotto_dec_fx")),
                              c_decode_flag(b[:n], good[:n], os.path.join(C, "fixed", "sotto_dec_fx"), "-1200half"))
    # same 20 ms levels as the float decoder (its random phases differ)
    lv = lambda y: 10 * np.log10(np.mean(y.reshape(-1, 160) ** 2, 1) + 1)
    loud = lv(yf) > 30
    assert np.mean(np.abs(lv(yf) - lv(ys[0]))[loud]) < 1.0


@need_c
def test_c_encoder_matches_python():
    x = np.fromfile(os.path.join(ROOT, "codec", "codec2", "raw", "hts1a.raw"), "<i2")
    x = x[:len(x) // 80 * 80]
    vl.TRACK2["lag"] = 8
    try:
        bp = lr.Sotto1200().encode(x / 32767.0)
    finally:
        vl.TRACK2.pop("lag")
    with tempfile.TemporaryDirectory() as d:
        x.tofile(f"{d}/x")
        run(os.path.join(C, "sotto_enc"), "-1200", f"{d}/x", f"{d}/b")
        bc = np.unpackbits(np.fromfile(f"{d}/b", np.uint8)).reshape(-1, 48)
    assert len(bc) == len(bp)
    assert np.mean(np.all(bc == bp, 1)) >= 0.97


def c_encode(x, exe):
    with tempfile.TemporaryDirectory() as d:
        r, o = os.path.join(d, "x.raw"), os.path.join(d, "x.bit")
        np.asarray(x, "<i2").tofile(r)
        subprocess.run([exe, "-1200", r, o], check=True)
        b = np.frombuffer(open(o, "rb").read(), np.uint8)
    return np.unpackbits(b).reshape(-1, 48)


@need_c
def test_fixed_encoder_bit_exact_across_paths_and_follows_float():
    """c/fixed/sotto_fx_lr_enc.c: x86, M0 and M4 (SMLAD) paths give the same bits, and the
    bits follow the float encoder's about as closely as the v3.7 fixed core follows the float
    core (the joint A search and B's residual are the float's, in integers)"""
    widths = [2, 7, 5, 9, 9, 2, 3, 3, 8]
    edges = np.cumsum([0] + widths)
    agree, n_all = np.zeros(len(widths)), 0
    for name in ("hts1a", "hts2a", "ve9qrp_10s"):
        x = np.fromfile(os.path.join(ROOT, "codec", "codec2", "raw", name + ".raw"), "<i2")
        bx = [c_encode(x, os.path.join(C, "fixed", f"sotto_enc_fx{s}")) for s in ("", "_m0", "_arm")]
        assert np.array_equal(bx[0], bx[1]) and np.array_equal(bx[0], bx[2])
        bf = c_encode(x, os.path.join(C, "sotto_enc"))
        assert len(bf) == len(bx[0]) == -(-len(x) // 320)
        for i in range(len(widths)):
            agree[i] += np.all(bf[:, edges[i]:edges[i + 1]] == bx[0][:, edges[i]:edges[i + 1]], 1).sum()
        n_all += len(bf)
    agree /= n_all
    assert agree[[0, 2, 5, 7]].min() > 0.98            # voicing and energy: the core's
    assert agree[[1, 6]].min() > 0.97                   # pitch: the core's, B's steps exact
    assert agree[[3, 4, 8]].min() > 0.94                # shapes: A's joint search, B's residual
    for n in (1, 2, 3, 5):                              # short inputs: the flush codes every superframe
        x = np.fromfile(os.path.join(ROOT, "codec", "codec2", "raw", "hts1a.raw"), "<i2")[:n * 320]
        assert len(c_encode(x, os.path.join(C, "fixed", "sotto_enc_fx"))) == n
