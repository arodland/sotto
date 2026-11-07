"""PHY G, the K6 pager's coded 4FSK (modem/k6g.py, c/k6/ifpager.c, docs/k6-if-fec.md): the
sender's framing and code against the reference receiver and the K6's integer decoder; the whole
receiver (ifrx's IF matched filter with the K6-trained templates, then the decoder) on the
study's GP0 model; and the session as the pager image runs it, on an emulated Cortex-M0 against
the modelled DMA stream: every packet, no counts lost, CPU and stack to spare.
Needs a C compiler; the emulator test also clang, ld.lld and unicorn. SPDX-License-Identifier: MIT"""
import ctypes
import os
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
from modem import k6g, pager  # noqa: E402

P, I = ctypes.c_void_p, ctypes.c_int


@pytest.fixture(scope="module")
def lib():
    if not shutil.which("cc"):
        pytest.skip("needs a C compiler")
    d = tempfile.mkdtemp()
    so = os.path.join(d, "libifp.so")
    subprocess.run(["cc", "-O2", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC", "-DIFRX_T4_K6",
                    os.path.join(K6, "ifpager_host.c"), "-o", so], check=True)
    L = ctypes.CDLL(so)
    L.ifp_host_run.argtypes = [P, I, ctypes.c_uint, I, P, I, P]
    L.ifp_host_syms.argtypes = [P, P, I, P, I, P]
    return L


def message(npk=6, seed=4):
    """npk XL packets of a sotto 1200 message (random frames), as the K6's FIFO would give them"""
    rng = np.random.default_rng(seed)
    return pager.profile("XL", "1200").pack(rng.integers(0, 256, 6 * 20 * npk, dtype=np.uint8).tobytes(), 0x31)


def c_syms(L, sym, met):
    sym = np.ascontiguousarray(sym, np.uint8)
    met = np.ascontiguousarray(met, np.uint32)
    out, info = np.zeros(128 * 64, np.uint8), np.zeros(4, np.int32)
    n = L.ifp_host_syms(sym.ctypes.data, met.ctypes.data, len(sym), out.ctypes.data, 64, info.ctypes.data)
    return [out[i * 128:(i + 1) * 128].tobytes() for i in range(n)], info


def noisy(syms, sd, seed=1):
    """|corr|-like metrics: 1000 on the sent tone, 300 elsewhere, Gaussian noise on each"""
    rng = np.random.default_rng(seed)
    met = np.full((len(syms), 4), 300.0)
    met[np.arange(len(syms)), syms] = 1000.0
    return np.clip(met + rng.normal(0, sd, met.shape), 0, None)


def test_sync_word_is_far_from_the_lead():
    """no 24-symbol window of the lead running into the sync word is within 10 symbols of it"""
    s = k6g.SYNC_SYMS
    for ph in (0, 1):
        x = np.concatenate([k6g.lead(len(s) + ph)[ph:], s])
        d = [int(np.sum(x[k:k + len(s)] != s)) for k in range(len(s))]
        assert min(d) >= 10
    assert int(k6g.SYNC, 2) == 0x907E6A                  # ifpager.h's IFP_SYNC


def test_reference_receiver_round_trip():
    pk = message()
    s = k6g.burst_symbols(pk)
    assert len(s) == k6g.LEAD_SYMS + len(pk) * (24 + k6g.PKT_SYMS) + k6g.TAIL_SYMS
    met = noisy(s, 0.0)
    assert k6g.receive(s, met) == pk


def test_k6_decoder_matches_the_code(lib):
    """the K6's integer decoder (sliding traceback, int16 metrics, per-block LLRs) decodes what
    the sender coded: clean, and through noise that leaves 4-6% of symbols wrong (the live
    floor), where the reference receiver does too"""
    pk = message(8)
    s = k6g.burst_symbols(pk)
    got, info = c_syms(lib, s, noisy(s, 0.0))
    assert got == pk and info[0] == len(pk)
    met = noisy(s, 250.0, seed=3)
    ser = np.mean(met.argmax(1) != s)
    assert 0.04 < ser < 0.07, ser
    got, info = c_syms(lib, met.argmax(1), met)
    assert got == pk, (ser, info)
    assert k6g.receive(met.argmax(1), met) == pk


def test_lost_symbol_still_finds_the_next_sync(lib):
    """a symbol lost in packet 1 (a slip) costs that packet only: the next sync word is still
    found where it really is"""
    pk = message(4)
    s = k6g.burst_symbols(pk)
    k = k6g.LEAD_SYMS + 24 + k6g.PKT_SYMS + 24 + 500       # inside packet 1
    s2 = np.delete(s, k)
    got, info = c_syms(lib, s2, noisy(s2, 0.0))
    assert info[0] == len(pk)
    assert got[0] == pk[0] and got[2:] == pk[2:] and got[1] != pk[1]


def gp0_counts(pk, cn0, f_off, seed=1, level=k6g.LEVEL):
    from sim.k6if import mcu, study as st
    a = k6g.audio(k6g.burst_symbols(pk), st.FSA, level)
    s = st.fm(st.ic7100(np.concatenate([np.zeros(int(0.3 * st.FSA)), a, np.zeros(int(0.3 * st.FSA))])), f_off)
    c = st.fe.receive(s, st.FZ, cn0, seed=seed, fdma=mcu.FDMA, floor=0.05)
    return np.ascontiguousarray(np.clip(c, -127, 127).astype(np.int8)), mcu


@pytest.mark.parametrize("cn0,off", [(60.0, -164.0), (47.0, 164.0)])
def test_whole_receiver_on_the_model(lib, cn0, off):
    """ifrx (the K6-trained 4FSK templates, as the image builds it) and the decoder over the
    modelled link, 164 Hz either side: every packet (on the model the trained templates lose
    1-2 dB against its own; on the K6 they win)"""
    pk = message(6)
    c, mcu = gp0_counts(pk, cn0, off)
    out, info = np.zeros(128 * 16, np.uint8), np.zeros(8, np.int32)
    n = lib.ifp_host_run(c.ctypes.data, len(c), round(mcu.FDMA / 1000), int(0.02 * mcu.FC),
                         out.ctypes.data, 16, info.ctypes.data)
    got = [out[i * 128:(i + 1) * 128].tobytes() for i in range(n)]
    assert got == pk, info


def test_control_record_carries_the_session_length():
    r = pager.control_record(7, None, "ifrx", arg=11)
    assert r[5] == 10 and r[4] == 11 and r[2] == 0


def test_tx_builds_a_g_burst(tmp_path):
    """modem.k6 tx --phy G: the control packet, 0.5 s of carrier, the burst, at its length"""
    from modem import k6
    sot = tmp_path / "m.sot"
    sot.write_bytes(np.random.default_rng(1).integers(0, 256, 6 * 100, dtype=np.uint8).tobytes())
    wav = tmp_path / "g.wav"
    assert k6.main(["tx", str(sot), str(wav), "--phy", "G", "--payload", "XL", "--codec", "1200",
                    "--no-trim", "--no-pause"]) == 0
    import soundfile as sf
    x, fs = sf.read(str(wav))
    burst = k6g.burst_seconds(5)
    assert burst + 0.5 < len(x) / fs < burst + 3.0              # the control packet: two XL at F/1800
    assert abs(np.max(np.abs(x[-int(burst * fs):])) - k6g.LEVEL) < 0.01


need_clang = pytest.mark.skipif(not (shutil.which("clang") and shutil.which("ld.lld")),
                                reason="needs clang and ld.lld")


@need_clang
def test_session_on_m0():
    """the session (ifpager_emu.c: ifpager.c with a stand-in sp_packet of the same cost) on the
    emulated M0 against a modelled 8-packet burst 164 Hz off: every packet handed over, in
    order, no counts lost (each trellis step and packet check waits for a gap between the
    receiver's bursts), CPU to spare, inside the 3.5 KB of stack"""
    pytest.importorskip("unicorn")
    import k6_live_emu as emu
    pk = message(8)
    ret, said, used, got = emu.run_ifpager(pk, cn0=55.0, f_off=-164.0)
    assert ret == len(pk), said
    assert got == pk, said
    f = dict(kv.split("=") for kv in said[-1].split()[2:])
    assert int(f["L"]) == 0 and int(f["syncs"]) == len(pk), said
    assert int(f["i"]) >= 30, said
    assert used < 3504 - 256, used
