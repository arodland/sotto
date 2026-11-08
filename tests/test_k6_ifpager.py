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
    subprocess.run(["cc", "-O2", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC",
                    os.path.join(K6, "ifpager_host.c"), "-o", so], check=True)
    L = ctypes.CDLL(so)
    L.ifp_host_run.argtypes = [P, I, ctypes.c_uint, I, P, I, P, I]
    L.ifp_host_syms.argtypes = [P, P, I, P, I, P, I]
    return L


def message(npk=6, seed=4):
    """npk XL packets of a sotto 1200 message (random frames), as the K6's FIFO would give them"""
    rng = np.random.default_rng(seed)
    return pager.profile("XL", "1200").pack(rng.integers(0, 256, 6 * 20 * npk, dtype=np.uint8).tobytes(), 0x31)


def c_syms(L, sym, met, rate=0):
    sym = np.ascontiguousarray(sym, np.uint8)
    met = np.ascontiguousarray(met, np.uint32)
    out, info = np.zeros(128 * 64, np.uint8), np.zeros(4, np.int32)
    n = L.ifp_host_syms(sym.ctypes.data, met.ctypes.data, len(sym), out.ctypes.data, 64, info.ctypes.data,
                        int(rate))
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


@pytest.mark.parametrize("rate", [0, 1, 2])
def test_reference_receiver_round_trip(rate):
    pk = message()
    s = k6g.burst_symbols(pk, r23=rate)
    assert len(s) == k6g.LEAD_SYMS + len(pk) * (24 + k6g.pkt_syms(rate)) + k6g.TAIL_SYMS
    assert k6g.pkt_syms(rate) == (1032, 774, 688)[rate]
    met = noisy(s, 0.0)
    assert k6g.receive(s, met, r23=rate) == pk


def test_rate_23_punctures_the_same_code():
    """rate 2/3 sends the rate-1/2 code's bits but every odd step's second one"""
    pk = message(1)[0]
    bits = np.unpackbits(np.frombuffer(pk, np.uint8))
    c = k6g.conv_encode(np.concatenate([bits, np.zeros(k6g.TAIL_BITS, np.int8)]))
    full = k6g.GRAY[k6g.packet_symbols(pk)].reshape(-1, 43)
    sent = np.concatenate([full >> 1, full & 1], axis=1).reshape(-1)
    assert np.array_equal(sent, c)
    g = k6g.GRAY[k6g.packet_symbols(pk, r23=True)].reshape(-1, 43)
    sent = np.concatenate([g >> 1, g & 1], axis=1).reshape(-1)
    assert np.array_equal(sent, np.delete(c, np.arange(3, len(c), 4)))


def test_rate_34_punctures_the_same_code():
    """rate 3/4 (133: 1 0 1, 171: 1 1 0): of each three steps both bits of the first, the second
    bit of the second and the first of the third; 1376 coded bits, 16 whole blocks"""
    pk = message(1, seed=2)[0]
    bits = np.unpackbits(np.frombuffer(pk, np.uint8))
    c = k6g.conv_encode(np.concatenate([bits, np.zeros(k6g.TAIL_BITS, np.int8)]))
    g = k6g.GRAY[k6g.packet_symbols(pk, r23=2)].reshape(-1, 43)
    sent = np.concatenate([g >> 1, g & 1], axis=1).reshape(-1)
    drop = np.sort(np.concatenate([np.arange(2, len(c), 6), np.arange(5, len(c), 6)]))
    assert len(sent) == 1376 == 16 * k6g.BLOCK
    assert np.array_equal(sent, np.delete(c, drop))


@pytest.mark.parametrize("rate,sd,lo,hi", [(0, 250.0, 0.04, 0.07), (1, 200.0, 0.015, 0.04),
                                           (2, 170.0, 0.005, 0.025)])
def test_k6_decoder_matches_the_code(lib, rate, sd, lo, hi):
    """the K6's integer decoder (sliding traceback, int16 metrics, per-block LLRs) decodes what
    the sender coded: clean, and through noise that leaves some symbols wrong (rate 1/2: 4-6%,
    the old tones' live floor; rate 2/3: 1.5-4%; rate 3/4: 0.5-2.5%), where the reference
    receiver does too"""
    pk = message(8)
    s = k6g.burst_symbols(pk, r23=rate)
    got, info = c_syms(lib, s, noisy(s, 0.0), rate)
    assert got == pk and info[0] == len(pk)
    met = noisy(s, sd, seed=3)
    ser = np.mean(met.argmax(1) != s)
    assert lo < ser < hi, ser
    got, info = c_syms(lib, met.argmax(1), met, rate)
    assert got == pk, (ser, info)
    assert k6g.receive(met.argmax(1), met, r23=rate) == pk


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


def gp0_counts(pk, cn0, f_off, seed=1, level=k6g.LEVEL, rate=0):
    from sim.k6if import mcu, study as st
    a = k6g.audio(k6g.burst_symbols(pk, r23=rate), st.FSA, level)
    s = st.fm(st.ic7100(np.concatenate([np.zeros(int(0.3 * st.FSA)), a, np.zeros(int(0.3 * st.FSA))])), f_off)
    c = st.fe.receive(s, st.FZ, cn0, seed=seed, fdma=mcu.FDMA, floor=0.05)
    return np.ascontiguousarray(np.clip(c, -127, 127).astype(np.int8)), mcu


@pytest.mark.parametrize("cn0,off,rate", [(60.0, -164.0, 0), (45.0, 164.0, 0),
                                          (60.0, 164.0, 1), (47.0, -164.0, 1),
                                          (60.0, -164.0, 2), (49.0, 164.0, 2)])
def test_whole_receiver_on_the_model(lib, cn0, off, rate):
    """ifrx (the 600-2400 Hz tones on the half-baud grid, as the image builds it) and the
    decoder over the modelled link, 164 Hz either side: every packet. Rates 2/3 and 3/4 with
    about 2 dB more each"""
    pk = message(6)
    c, mcu = gp0_counts(pk, cn0, off, rate=rate)
    out, info = np.zeros(128 * 16, np.uint8), np.zeros(8, np.int32)
    n = lib.ifp_host_run(c.ctypes.data, len(c), round(mcu.FDMA / 1000), int(0.02 * mcu.FC),
                         out.ctypes.data, 16, info.ctypes.data, rate)
    got = [out[i * 128:(i + 1) * 128].tobytes() for i in range(n)]
    assert got == pk, info


def test_carrier_after_the_burst_raises_no_sync_word(lib):
    """the unmodulated carrier around a burst (0.5 s before the lead, and whatever the sender
    leaves after it) decides as tones 0 and 3 with both metrics high, which once made a sync word
    78 symbols after the last packet (a ninth, garbage packet for sp_packet to reject, and a
    longer session): the hunt takes such a decision as bad. 1.5 s of carrier after the burst at
    the modelled floor and above: exactly the packets sent, no more sync words"""
    from sim.k6if import mcu, study as st
    pk = message(5, seed=6)
    for cn0 in (47.0, 55.0):
        a = k6g.audio(k6g.burst_symbols(pk), st.FSA, k6g.LEVEL)
        s = st.fm(st.ic7100(np.concatenate([np.zeros(int(0.5 * st.FSA)), a, np.zeros(int(1.5 * st.FSA))])), -164.0)
        c = st.fe.receive(s, st.FZ, cn0, seed=2, fdma=mcu.FDMA, floor=0.05)
        c = np.ascontiguousarray(np.clip(c, -127, 127).astype(np.int8))
        out, info = np.zeros(128 * 16, np.uint8), np.zeros(8, np.int32)
        n = lib.ifp_host_run(c.ctypes.data, len(c), round(mcu.FDMA / 1000), 0, out.ctypes.data, 16, info.ctypes.data, 0)
        assert [out[i * 128:(i + 1) * 128].tobytes() for i in range(n)] == pk, (cn0, info)
        assert info[1] == len(pk), (cn0, info)


def test_control_record_carries_the_session_length():
    r = pager.control_record(7, None, "ifrx", arg=11)
    assert r[5] == 10 and r[4] == 11 and r[2] == 0 and r[3] == 0
    r = pager.control_record(7, None, "ifrx", arg=11, val=1)    # rate 2/3
    assert r[5] == 10 and r[4] == 11 and r[2] == 0 and r[3] == 1
    assert pager.parse_control(pager.control_packet(7, None, "ifrx", 128, arg=11, val=1)) == r[:6]


@pytest.mark.parametrize("rate", ["1/2", "2/3", "3/4"])
def test_tx_builds_a_g_burst(tmp_path, rate):
    """modem.k6 tx --phy G: the control packet, 0.5 s of carrier, the burst, at its length"""
    from modem import k6
    sot = tmp_path / "m.sot"
    sot.write_bytes(np.random.default_rng(1).integers(0, 256, 6 * 100, dtype=np.uint8).tobytes())
    wav = tmp_path / "g.wav"
    assert k6.main(["tx", str(sot), str(wav), "--phy", "G", "--payload", "XL", "--codec", "1200",
                    "--no-trim", "--no-pause", "--g-rate", rate]) == 0
    import soundfile as sf
    x, fs = sf.read(str(wav))
    burst = k6g.burst_seconds(5, r23=k6g.RATES[rate])
    assert burst + 0.5 < len(x) / fs < burst + 3.0              # the control packet: two XL at F/1800
    assert abs(np.max(np.abs(x[-int(burst * fs):])) - k6g.LEVEL) < 0.01


def test_any_start_offset_at_the_image_rate(lib):
    """where the counts' 64-sample blocks start against the signal mustn't matter. The image's
    DMA runs at 5.9 MS/s (its code polls from flash); there the acquisition's old edge rule
    (back from the best offset while the fit kept half of it) locked a quarter symbol early
    for 12 of 26 starts, and the packets were lost. Starts across one offset step (~410
    samples) of the sweep, -164 Hz"""
    from sim.k6if import study as st
    pk = message(3, seed=9)
    a = k6g.audio(k6g.burst_symbols(pk), st.FSA, k6g.LEVEL)
    s = st.fm(st.ic7100(np.concatenate([np.zeros(int(0.3 * st.FSA)), a, np.zeros(int(0.3 * st.FSA))])), -164.0)
    fdma = 5.9e6
    b = st.fe.receive(s, st.FZ, 52.0, seed=5, L=0, fdma=fdma).astype(np.int64)
    cum = np.concatenate([[0], np.cumsum(b)])
    bad = []
    for s0 in range(0, 410, 41):
        i = s0 + 64 * np.arange((len(b) - s0) // 64 - 1)
        c = np.ascontiguousarray((2 * (cum[i + 64] - cum[i]) - 64).astype(np.int8))
        out, info = np.zeros(128 * 16, np.uint8), np.zeros(8, np.int32)
        n = lib.ifp_host_run(c.ctypes.data, len(c), round(fdma / 1000), 0, out.ctypes.data, 16, info.ctypes.data, 0)
        if [out[k * 128:(k + 1) * 128].tobytes() for k in range(n)] != pk:
            bad.append(s0)
    assert not bad, bad


need_clang = pytest.mark.skipif(not (shutil.which("clang") and shutil.which("ld.lld")),
                                reason="needs clang and ld.lld")


@need_clang
def test_ram_block_is_self_contained():
    """built as tools/k6_build.sh builds it for the image, the receiver's hot loops
    (.text.ifrx_ram: sample, corr2, corr4, count, then ifp_ram_end) carry no relocations (no
    call out of the block, no table reached but through ifrx_t), so ifp_session's copy of them
    runs in RAM as it is; and they fit its buffer (IFP_RAM_MAX, less the 2-byte alignment)"""
    import re
    res = subprocess.run(["clang", "-print-resource-dir"], capture_output=True, text=True).stdout.strip()
    d = tempfile.mkdtemp()
    o = os.path.join(d, "ifp.o")
    subprocess.run(["clang", "--target=thumbv6m-none-eabi", "-mcpu=cortex-m0", "-mthumb", "-mfloat-abi=soft",
                    "-nostdinc", "-isystem", os.path.join(K6, "..", "fixed", "cm4", "inc"), "-isystem",
                    os.path.join(res, "include"), "-ffunction-sections", "-fdata-sections", "-std=c99",
                    "-ffreestanding", "-I" + os.path.join(K6, ".."), "-O2", "-Os", "-c",
                    os.path.join(K6, "ifpager.c"), "-o", o], check=True)
    secs = subprocess.run(["llvm-readelf", "-S", o], capture_output=True, text=True, check=True).stdout
    assert ".rel.text.ifrx_ram" not in secs
    size = int(re.search(r"\.text\.ifrx_ram\s+PROGBITS\s+\S+\s+\S+\s+(\S+)", secs).group(1), 16)
    syms = subprocess.run(["llvm-nm", "-n", o], capture_output=True, text=True, check=True).stdout
    names = [ln.split()[-1] for ln in syms.splitlines() if ln.split()[-1] in
             ("sample", "corr2", "corr4", "count", "ifp_ram_end")]
    assert names[0] == "sample" and names[-1] == "ifp_ram_end" and len(names) == 5, names
    cap = int(re.search(r"#define IFP_RAM_MAX (\d+)u", open(os.path.join(K6, "ifpager.c")).read()).group(1))
    assert size + 2 + 4 <= cap, (size, cap)


@need_clang
@pytest.mark.parametrize("fdma,flash_cpi,rate", [(None, None, 0), (5.9e6, 2.0, 0), (5.9e6, 2.0, 1),
                                                 (5.9e6, 2.0, 2)])
def test_session_on_m0(fdma, flash_cpi, rate):
    """the session (ifpager_emu.c: ifpager.c with a stand-in sp_packet of the same cost) on the
    emulated M0 against a modelled 8-packet burst 164 Hz off: every packet handed over, in
    order, no counts lost (each trellis step and packet check waits for a gap between the
    receiver's bursts), CPU to spare, inside the 3.5 KB of stack. Twice: as a RAM module would
    run (5.49 MS/s), and as the image does (its code in flash, flash_cpi 2.0 matching the K6's
    23% idle while hunting on air, the DMA at the 5.9 MS/s it measured there), where the hot
    loops' copy in RAM is what keeps it inside the ring"""
    pytest.importorskip("unicorn")
    import k6_live_emu as emu
    pk = message(8)
    ret, said, used, got = emu.run_ifpager(pk, cn0=55.0, f_off=-164.0, fdma=fdma, flash_cpi=flash_cpi,
                                          flags=(0, 1, 4)[rate])
    assert ret == len(pk), said
    assert got == pk, said
    f = dict(kv.split("=") for kv in said[-1].split()[2:])
    assert int(f["L"]) == 0 and int(f["syncs"]) == len(pk), said
    assert int(f["ram"]) > 0, said                       # the RAM block was copied and used
    # the decoder within one block (coded bits; a punctured rate's step across two blocks waits
    # for the second, one bit more, and the writer reaches that bit's place 42 symbols later)
    assert int(f["lag"]) <= 86 + (rate > 0), said
    # CPU to spare with sp_packet at the 400 us it took on the K6 (on air fe25ec8 idled 30-33%)
    assert int(f["i"]) >= (20 if flash_cpi else 30), said
    # 3.56 KB: on air (fe25ec8) the session left 596 B to spare at 3.45 KB here; sp_packet's
    # yields to the receiver add ~110 B (hand, sp_packet and the yield under a receiver burst)
    assert used < 3600, used
    assert 0 <= int(f["ld"]) < 50, said                  # the lead's symbols, nearly all right


@need_clang
def test_session_yields_inside_the_store():
    """the stand-in sp_packet costs what the K6's took on air (407 us, 2026-11-07): without its
    yields the ring overran at every good packet; with them (SP_PKT_YIELD, as the image builds
    sotto_pager.c) the counts that come meanwhile go to the receiver, and none are lost. And the
    no-decode bench mode finds every sync word but hands nothing over"""
    pytest.importorskip("unicorn")
    import k6_live_emu as emu
    pk = message(4)
    ret, said, used, got = emu.run_ifpager(pk, cn0=55.0, f_off=-164.0, fdma=5.773e6, flash_cpi=2.0)
    f = dict(kv.split("=") for kv in said[-1].split()[2:])
    assert ret == len(pk) and got == pk and int(f["L"]) == 0, said
    assert int(f["gap"]) < 180, said                     # never away near a lap of the ring
    ret, said, used, got = emu.run_ifpager(pk, cn0=55.0, f_off=-164.0, fdma=5.773e6, flash_cpi=2.0, flags=2)
    f = dict(kv.split("=") for kv in said[-1].split()[2:])
    assert ret == 0 and got == [] and int(f["syncs"]) == len(pk), said


@need_clang
def test_session_blinks_the_led_once_a_packet():
    """API level 7 (the green RX LED, 2026-11-08): the session lights it at each sync word and
    puts it out 144 symbols (120 ms) later, one blink a packet (0.92 s apart at rate 1/2), dark
    at the end; with the CPU still to spare and every packet handed over. It sets the LED on each
    check (no state: the session's stack is at its limit). Level 4 tables never call it"""
    pytest.importorskip("unicorn")
    import k6_live_emu as emu
    pk = message(4)
    leds = []
    ret, said, used, got = emu.run_ifpager(pk, cn0=55.0, f_off=-164.0, fdma=5.9e6, flash_cpi=2.0,
                                          api_level=7, leds=leds)
    f = dict(kv.split("=") for kv in said[-1].split()[2:])
    assert ret == len(pk) and got == pk and int(f["L"]) == 0, said
    assert int(f["i"]) >= 20 and used < 3600, (said, used)
    edges, lit = [], 0
    for t, on in leds:
        if on != lit:
            edges.append((t, on))
            lit = on
    assert lit == 0 and leds[-1][1] == 0, leds[-5:]
    assert [on for _, on in edges] == [1, 0] * len(pk), edges
    ons, offs = edges[0::2], edges[1::2]
    for (a, _), (b, _) in zip(ons, offs):
        assert 110 <= b - a <= 140, edges                # 120 ms, checked every ~3 ms
    assert all(b[0] - a[0] > 800 for a, b in zip(ons, ons[1:])), edges
    leds = []
    emu.run_ifpager(pk[:1], cn0=55.0, f_off=-164.0, fdma=5.9e6, flash_cpi=2.0, leds=leds)
    assert leds == []
