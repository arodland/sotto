"""DMR detection on the K6's GP0 IF (docs/k6-dmr.md): the sync words, the integer detector
(c/k6/dmrsync.h) against the float one (sim/k6if/dmr.py) on modelled DMR, FM voice and noise,
and the dmrdet module (c/k6/mod/dmrdet.c) on an emulated Cortex-M0 against the modelled DMA
stream (tests/k6_live_emu.py): it must mute a repeater's DMR, open the audio for FM voice, fit
the 4 KB module store, and keep up with the DMA. Needs a C compiler; the module tests also
clang, ld.lld and unicorn.
SPDX-License-Identifier: MIT"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
K6 = os.path.join(ROOT, "c", "k6")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from modem import k6mod  # noqa: E402
from sim.k6if import dmr, frontend as fe, study as st  # noqa: E402

need_clang = pytest.mark.skipif(not (shutil.which("clang") and shutil.which("ld.lld")),
                                reason="needs clang and ld.lld")
MOD = os.path.join(K6, "mod", "dmrdet.c")
CPS8 = round(96e6 / fe.FDMA * 256)


def test_sync_words():
    """TS 102 361-1's sync words: outer symbols only, balanced, data = voice negated; the C
    table's masks are the plus positions"""
    for f in dmr.FAMS:
        assert set(dmr.PAT[f]) == {-3, 3}
    src = open(os.path.join(K6, "dmrsync.h")).read()
    tab = re.search(r"DS_PAT\[DS_NFAM\] = \{.*?\};", src, re.S).group(0)
    masks = [int(m, 16) for m in re.findall(r"0x([0-9A-F]{8})u", tab)]
    assert masks == [sum(1 << i for i, p in enumerate(dmr.PAT[f]) if p > 0) for f in dmr.FAMS]


@pytest.fixture(scope="module")
def host():
    if not shutil.which("cc"):
        pytest.skip("needs a C compiler")
    d = tempfile.mkdtemp()
    exe = os.path.join(d, "dmrsync_host")
    subprocess.run(["cc", "-O2", "-Wall", "-Wextra", "-Werror", "-I", K6, "-o", exe,
                    os.path.join(K6, "dmrsync_host.c")], check=True)
    return exe


def c_hits(exe, x):
    out = subprocess.run([exe, str(CPS8)], input=np.asarray(x, np.int16).tobytes(),
                         capture_output=True, check=True).stdout.decode().split("\n")
    return [tuple(map(int, ln.split())) for ln in out if ln]


@pytest.fixture(scope="module")
def sigs():
    out = {}
    sy, _ = dmr.bs_symbols(2.0, ("voice-mid", "idle"), seed=3)
    out["bs"] = (dmr.dmr_baseband(sy)[0], "bs")
    sy, on = dmr.ms_symbols(2.0, "voice", "ms", seed=5)
    out["ms"] = (dmr.dmr_baseband(sy, on)[0], "ms")
    out["fm"] = (dmr.fm_voice(dmr.speech("male_libri3436_15s__original", 2.0), ctcss=100.0), None)
    out["ffsk"] = (st.fm(st.ic7100(st.ffsk_bits(np.random.default_rng(9).integers(0, 2, 2400), 0.14)[0])), None)
    return out


def test_c_matches_python(host, sigs):
    """the integer detector finds what the float one finds: confirmed syncs of the right
    family on DMR, nothing on FM voice and FFSK"""
    for nm, (s, fam) in sigs.items():
        x = dmr.to_counts(s, 60.0)
        ev, conf, _, _ = dmr.detect(x)
        rows = c_hits(host, x)
        cc = [r for r in rows if r[4]]
        if fam is None:
            assert not ev and not rows, nm
            continue
        assert len(conf) >= 3, nm
        assert abs(len(cc) - len(conf)) <= max(2, len(conf) // 10), (nm, len(cc), len(conf))
        assert all(dmr.FAMS[r[1]] == fam for r in cc), nm
        assert all(r[3] >= 0.64 * 256 for r in rows)


def test_detect_weak_and_off(sigs):
    """52 dB-Hz (about -116 dBm with an 8 dB noise figure) and 600 Hz off still confirm a
    repeater; the DMA's rate 0.3% off its measurement too"""
    sy, _ = dmr.bs_symbols(2.0, ("voice-mid", "idle"), seed=3)
    s, _ = dmr.dmr_baseband(sy, f_off=600.0)
    for cn0, err in ((54.0, 0.0), (60.0, 0.003), (60.0, -0.003)):
        _, conf, _, _ = dmr.detect(dmr.to_counts(s, cn0, seed=2), rate_err=err)
        assert len(conf) >= 5, (cn0, err)


# ---- the module on the emulated M0 ----

def build(defs, src=MOD):
    with tempfile.TemporaryDirectory() as d:
        w = os.path.join(d, "w.c")
        with open(w, "w") as f:
            f.writelines(f"#define {k} {v}\n" for k, v in defs)
            f.write(f'#include "{src}"\n')
        return k6mod.build(w)


def stream(s, cn0=60.0, pre_s=0.5, post_s=0.4, seed=1):
    """the DMA's bytes (bit 1 = GP0): no carrier for pre_s, then s (complex baseband at st.FZ),
    then nothing again; -> (bytes, the sample the carrier comes up at, where it goes)"""
    bits = fe.receive(s, st.FZ, cn0, seed=seed, L=0, fdma=fe.FDMA)
    rng = np.random.default_rng(seed + 1)
    a, b = int(pre_s * fe.FDMA), int(post_s * fe.FDMA)
    ib = np.concatenate([rng.integers(0, 2, a), bits, rng.integers(0, 2, b)]).astype(np.uint8) * 2
    return ib, a, a + len(bits)


def emulate(s, run_s=2, src=MOD, **kw):
    import k6_live_emu as le
    code, entry = build([("RUN_S", f"{run_s}u"), ("S_MS", "1000u")], src)
    ib, on, off = stream(s, **kw)
    e = le.Emu(code, entry, ib, on, fe.FDMA, 1.5, max_s=len(ib) / fe.FDMA + 0.5)
    # the RSSI follows the carrier: up from `on` to `off` only
    e.rssi = lambda: -60 if on <= e.sample_now() < off else -125
    r = e.run()
    return (r[0] if r else None), e.said, e.writes, (r[1] if r else None)


def st_size():
    """sizeof(st_t), dmrdet's state in the store's scratch, on thumbv6m"""
    with tempfile.TemporaryDirectory() as d:
        w, o = os.path.join(d, "w.c"), os.path.join(d, "w.o")
        with open(w, "w") as f:
            f.write(f'#include "{MOD}"\nchar st_size[sizeof(st_t)];\n')
        subprocess.run(["clang", "--target=thumbv6m-none-eabi", "-mcpu=cortex-m0", "-ffreestanding",
                        "-I", K6, "-c", w, "-o", o], check=True)
        out = subprocess.run(["llvm-nm", "-S", o], capture_output=True, text=True, check=True).stdout
    return next(int(ln.split()[1], 16) for ln in out.splitlines() if ln.endswith(" st_size"))


@need_clang
def test_module_fits():
    """code + header + tag + the 512-byte ring + the state inside the 4 KB store (else the
    module refuses to run: -8)"""
    if not shutil.which("llvm-nm"):
        pytest.skip("needs llvm-nm")
    code, _ = k6mod.build(MOD)
    assert len(code) + 24 + 512 + st_size() <= 4096, (len(code), st_size())


@need_clang
def test_module_mutes_repeater_dmr():
    pytest.importorskip("unicorn")
    sy, _ = dmr.bs_symbols(2.6, ("voice", "idle"), seed=7)
    s, _ = dmr.dmr_baseband(sy, pre_s=0.0, post_s=0.0)
    ret, said, writes, stack = emulate(s, run_s=4, post_s=1.5)
    txt = "\n".join(said)
    print(txt)
    assert ret == 1, txt
    assert any(ln.startswith("D on bs") for ln in said), txt
    on = next(ln for ln in said if ln.startswith("D on"))
    assert int(on.split("+")[1]) <= 150, on          # inside HOLD_MS: never heard
    assert 0x6140 not in writes.get(0x47, []), writes.get(0x47)
    s_lines = [ln for ln in said if ln.startswith("s ")]
    assert s_lines and all(" lost=0 " in ln for ln in s_lines), txt
    assert all(0 < int(ln.split("idle=")[1].split()[0]) <= 100 for ln in s_lines), txt
    ks = int(s_lines[-1].split("ks=")[1])
    assert abs(ks - fe.FDMA / 1000) < 30, s_lines[-1]
    assert stack < 700, stack


@need_clang
def test_module_opens_for_fm_voice():
    pytest.importorskip("unicorn")
    a = dmr.speech("female_libri198_14s__original", 2.6)
    ret, said, writes, _ = emulate(dmr.fm_voice(a, ctcss=100.0), run_s=4, post_s=1.5)
    txt = "\n".join(said)
    assert ret == 0, txt
    assert not any(ln.startswith("D on") for ln in said), txt
    assert 0x6140 in writes.get(0x47, []), writes.get(0x47)
    assert any(re.match(r"c -60 \d+ h=0$", ln) for ln in said), txt


@need_clang
def test_capture_module_sees_dmr():
    """dmrcap (c/k6/mod/dmrcap.c) records the IF when the carrier comes up, and the host's
    cap_report finds the repeater's sync in every carrier capture (both slots idle, as the
    real 0.5 s burst at 05:47:15 was: a sync every 30 ms, so every 38 ms capture holds one), through the spread gate,
    at about +-1944 Hz"""
    pytest.importorskip("unicorn")
    sy, _ = dmr.bs_symbols(1.0, ("idle", "idle"), seed=7)
    s, _ = dmr.dmr_baseband(sy, pre_s=0.0, post_s=0.0)
    ret, said, _, stack = emulate(s, run_s=3, src=os.path.join(K6, "mod", "dmrcap.c"), post_s=1.0)
    txt = "\n".join(ln for ln in said if not ln.startswith("y "))
    caps = dmr.parse_caps(said)
    car = [c for c in caps if c["pass_"]]
    assert ret == len(car) >= 2, txt
    assert caps[0]["pass_"] == 0, txt
    for c in car:
        assert len(c["x"]) == 3240 and c["lost"] == 0, txt
        r = dmr.cap_report(c["x"], c["ks"])
        assert r["fam"] == "bs" and abs(r["rho"]) >= 0.8 and r["gate_ok"], r
        assert 1500 < r["plus"] < 2400 and -2400 < r["minus"] < -1500, r
    assert stack < 3504 + 512, stack
