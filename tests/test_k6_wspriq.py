"""The WSPR receive module (c/k6/mod/wspriq.c) and its host decoder (tools/k6_wspr.py): on an
emulated Cortex-M0 the module streams the IF's complex baseband in `q` lines (a steady tone
lands where it should, the line times give the sample rate, everything is put back); the host
rebuilds the baseband from a log, lost lines and all, and decodes WSPR in it. The slow whole-
chain run (a WSPR frame through the emulated 1-bit path, 2 minutes of M0) is opt-in:
K6_SLOW=1. SPDX-License-Identifier: MIT"""
import base64
import os
import sys

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("scipy")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_k6_ft8 import START, ROOT, cal_record, emulate, need_clang  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "tools"))
from sim.k6digi import gp0, wsprref  # noqa: E402
import k6_wspr  # noqa: E402

MOD = os.path.join(ROOT, "c", "k6", "mod")
TEXT = "AG7EW CN87 10"


def short(tmp_path, seconds=3, audio=1500):
    w = tmp_path / "wspriq_short.c"
    w.write_text('#define K6_MODULE_OPT z\n#define WSPR_SECONDS %d\n#define WSPR_AUDIO %d\n#include "%s"\n'
                 % (seconds, audio, os.path.join(MOD, "wspriq.c")))
    return str(w)


def log_of(said):
    p = os.path.join(os.environ.get("TMPDIR", "/tmp"), "wspriq_%d.log" % os.getpid())
    with open(p, "w") as f:
        f.write("".join("K6 MOD say %s\n" % s for s in said))
    return p


@need_clang
def test_wspriq_builds_small():
    sys.path.insert(0, ROOT)
    from modem import k6mod
    code, entry = k6mod.build(os.path.join(MOD, "wspriq.c"))
    assert len(code) + 24 <= 4096, len(code)


@need_clang
def test_wspriq_streams_a_tone(tmp_path):
    """a steady tone 10 Hz above WSPR_AUDIO: `q` lines of 15 samples at about 200 Hz whose
    baseband peaks at +10 Hz; line times a whole number of samples apart; all put back"""
    pytest.importorskip("unicorn")
    tone = gp0.FIF + 1500 + 10.0

    def sig(t, regs):
        return 0.5 + 0.3 * np.cos(2 * np.pi * tone * t)
    ret, said, regs, writes, stack, (rcc, tim, dma, sim) = emulate(short(tmp_path), START, sig)
    assert said[0] == "wspriq start f=1500 inv=0 n=15 cal=0", said[0]
    assert said[1].startswith("wspriq gain rms=") and " r2=" in said[1], said[1]
    q = [s for s in said if s.startswith("q ")]
    assert all(len(s) == 53 for s in q) and len(q) == ret and ret >= 25, (ret, said[:4])
    assert said[-1] == "wspriq done n=%d lost=0" % ret, said[-1]
    info, lines = k6_wspr.parse(log_of(said))
    z, rate, t0 = k6_wspr.baseband(info, lines)
    assert len(z) == 15 * len(lines)                # nothing lost, nothing doubled
    assert 190 < rate < 215, rate                    # 5.71 MS/s / (64 x 16 x r2)
    zz = z[60:]                                      # past the CICs' start
    f = np.fft.fftfreq(4096, 1 / rate)
    peak = f[np.argmax(np.abs(np.fft.fft(zz * np.hanning(len(zz)), 4096)))]
    assert abs(peak - 10.0) < 0.5, peak
    assert stack < 3300, stack                     # the screen's 512 B of rows and powers too
    assert rcc[0x18] == 0 and rcc[0x14] == 0x14 and tim[0x00] == 0 and dma[0x08] == 0
    assert all(regs[r] == v for r, v in START.items()), regs
    assert {r for r, _ in writes} <= {0x33, 0x34, 0x73, 0x7E, 0x13}


@need_clang
def test_wspriq_follows_the_calibration(tmp_path):
    """a K6 whose IF sits 145.5 Hz low: with its calibration record the tone still lands +10 Hz"""
    pytest.importorskip("unicorn")
    off = -14550
    tone = gp0.FIF + 1500 + 10.0 + off / 100

    def sig(t, regs):
        return 0.5 + 0.3 * np.cos(2 * np.pi * tone * t)
    ret, said, *_ = emulate(short(tmp_path, 2), START, sig, level=4, cal=cal_record(off))
    assert abs(int(said[0].split("cal=")[1]) - off) <= 30, said[0]
    z, rate, _ = k6_wspr.baseband(*k6_wspr.parse(log_of(said)))
    zz = z[60:]
    f = np.fft.fftfreq(4096, 1 / rate)
    assert abs(f[np.argmax(np.abs(np.fft.fft(zz, 4096)))] - 10.0) < 1.0


@need_clang
def test_wspriq_screen_on_a_level_6_image(tmp_path):
    """on an image with the screen calls: the title, the window and its calibration, a status
    line, and a waterfall whose steady tone at +10 Hz lights the bin (16 + 10 / 6.2 = 17.6)
    four pixels wide, x 68-75; the stream is still whole"""
    pytest.importorskip("unicorn")
    tone = gp0.FIF + 1500 + 10.0

    def sig(t, regs):
        return 0.5 + 0.3 * np.cos(2 * np.pi * tone * t) + 0.02 * np.cos(2 * np.pi * (tone - 70) * t)
    lcd = {}
    ret, said, regs, writes, stack, _ = emulate(short(tmp_path, 4), START, sig, level=6, lcd=lcd)
    fr = lcd["frames"]
    assert len(fr) >= 2, said
    assert fr[-1]["text"][0] == "WSPR RX  440850 kHz" and fr[-1]["text"][1] == "1500Hz +-99  cal 0"
    assert fr[-1]["text"][5].startswith("t ") and fr[-1]["text"][5].endswith(" of 4 s  lost 0")
    wf = np.array([[(b >> (7 - j)) & 1 for b in row for j in range(8)] for row in fr[-1]["wf"]])
    assert wf.shape == (24, 128)
    lit = wf[:3].sum(axis=0)                       # the newest rows
    assert lit[68:76].sum() >= 3 * 6 and lit[:64].sum() + lit[80:].sum() <= 6, np.nonzero(lit)
    q = [s for s in said if s.startswith("q ")]
    assert said[-1] == "wspriq done n=%d lost=0" % len(q) and ret == len(q)
    assert stack < 3300, stack


def fake_log(z, rate, t0=0.37, every=None, drop=(), shift_bits=12):
    """wspriq's lines for baseband z at `rate`, as the module quantises them (one shift a line)"""
    out = ["wspriq start f=1500 inv=0 n=15 cal=-16580",
           "wspriq gain rms=21.4 r2=27 ks=%d" % round(rate * 64 * 16 * 27 / 1e3 * 1.02)]
    s = z / np.max(np.abs(z)) * 2 ** shift_bits
    for i in range(len(s) // 15):
        if i in drop:
            continue
        blk = s[15 * i:15 * i + 15]
        v = np.round(np.column_stack([blk.real, blk.imag]).ravel()).astype(int)
        e = 0
        while np.max(np.abs(v)) >> e > 127:
            e += 1
        b = np.clip((v + (1 << (e - 1) if e else 0)) >> e, -128, 127).astype(np.int8)
        t = int(round((t0 + (15 * i + 14) / rate) * 96e6)) & 0xFFFFFFFF
        out.append("q %08x %d %s" % (t, e, base64.b64encode(b.tobytes()).decode()))
    out.append("wspriq done n=%d lost=0" % (len(s) // 15 - len(drop)))
    return out


def wspr_baseband(rate, f0=4.0, drift=6.0, snr=-24, start=11.3, secs=150, seed=4):
    """WSPR at baseband (tone 0 at f0 Hz, drifting `drift` Hz over the frame) in noise, SNR in
    2500 Hz as WSJT-X gives it"""
    from sim.k6digi.wsprref import SYM, DF
    rng = np.random.default_rng(seed)
    tones = wsprref.encode(TEXT)
    t = np.arange(int(secs * rate)) / rate
    u = t - start
    k = np.clip((u / SYM).astype(int), 0, 161)
    on = (u >= 0) & (u < 162 * SYM)
    f = f0 + tones[k] * DF + drift * (u / (162 * SYM) - 0.5)
    z = on * np.exp(2j * np.pi * np.cumsum(f) / rate)
    n = (rng.normal(size=len(t)) + 1j * rng.normal(size=len(t))) * np.sqrt(rate / 2500 / 10 ** (snr / 10) / 2)
    return z + n


def test_host_decodes_a_log_with_lost_lines(tmp_path):
    """the host side alone: WSPR 4 Hz above WSPR_AUDIO with 6 Hz of drift at -24 dB, sampled at
    198.6 Hz, quantised as the module does, 20 lines lost: decodes, at the right start time"""
    rate = 198.6
    lines = fake_log(wspr_baseband(rate), rate, drop=set(range(300, 320, 1)))
    log = tmp_path / "k6.log"
    log.write_text("".join("K6 MOD say %s\n" % s for s in lines))
    info, ql = k6_wspr.parse(str(log))
    z, r, t0 = k6_wspr.baseband(info, ql)
    assert abs(r - rate) < 0.01 and abs(t0 - 0.37) < 0.002
    res = k6_wspr.decode(z, r, (-30.0, 0.0, 30.0))
    assert [x[4] for x in res] == [TEXT], res
    off, dt, drift, *_ = res[0]
    assert abs(off - 4.0) < 1.0 and abs(dt - 11.3) < 0.5, res


def test_host_finds_a_signal_off_centre(tmp_path):
    """WSPR 37 Hz above WSPR_AUDIO (an uncalibrated K6 or a crystal's drift): the CLI's
    default 50 Hz span finds it"""
    rate = 201.3
    lines = fake_log(wspr_baseband(rate, f0=37.0, drift=-3.0, snr=-20, seed=8), rate)
    log = tmp_path / "k6.log"
    log.write_text("\n".join(lines) + "\n")
    import subprocess
    out = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "k6_wspr.py"), "decode",
                          str(log)], capture_output=True, text=True, timeout=300)
    assert out.returncode == 0 and TEXT in out.stdout, (out.stdout, out.stderr)
    assert abs(float(out.stdout.split()[0]) - 1537.0) < 1.0, out.stdout


@need_clang
@pytest.mark.skipif(not os.environ.get("K6_SLOW"), reason="2 minutes of emulated M0: K6_SLOW=1")
def test_wspriq_whole_chain_decodes(tmp_path):
    """WSPR in USB at the K6's IF (tone 0 at 1503 Hz) through the 1-bit path, the emulated
    module's DMA ring, mixer and CICs, its `q` lines, and the host decoder"""
    pytest.importorskip("unicorn")
    from sim.k6digi.wsprref import SYM, DF
    tones = wsprref.encode(TEXT)
    rng = np.random.default_rng(9)
    t0 = 1.5

    def sig(t, regs):
        u = t - t0
        k = np.clip((u / SYM).astype(int), 0, 161)
        on = (u >= 0) & (u < 162 * SYM)
        ph = 2 * np.pi * (gp0.FIF + 1503 + tones[k] * DF) * t
        return 0.5 + 0.15 * on * np.cos(ph) + rng.normal(0, 0.05, len(t))
    ret, said, *_ = emulate(short(tmp_path, 116), START, sig, count=4_000_000_000)
    assert said[-1].startswith("wspriq done") and ret > 1000, said[-1]   # not the count cap
    z, rate, _ = k6_wspr.baseband(*k6_wspr.parse(log_of(said)))
    res = k6_wspr.decode(z, rate)
    assert [x[4] for x in res] == [TEXT], res
    assert abs(res[0][0] - 3.0) < 1.0 and abs(res[0][1] - 1.36) < 0.3, res   # stream from 0.14 s


def test_sync_mark_lands_before_an_even_minute():
    """the beacon's sync mark drops 10 s before a slot (1 s past an even UTC minute), the first
    one it can still key whole"""
    base = 1_762_520_400.0                         # an even minute (UTC)
    assert int(base) % 120 == 0
    assert k6_wspr.mark_time(base + 0.0) == (base - 9.0 + 120, base + 121.0)    # :00 too late for :01
    assert k6_wspr.mark_time(base - 30.0) == (base - 9.0, base + 1.0)
    assert k6_wspr.mark_time(base - 12.3) == (base - 9.0, base + 1.0)           # just in time
    assert k6_wspr.mark_time(base - 12.0) == (base - 9.0 + 120, base + 121.0)   # 12.0 < 10+1.2+2
    end, slot = k6_wspr.mark_time(base + 47.5)
    assert slot == base + 121.0 and end == slot - 10.0
    # the mark is the CW ID (about 9 s): it must fit whole too, still inside the 150 s wait
    assert k6_wspr.mark_time(base - 20.5, carrier=9.8) == (base - 9.0 + 120, base + 121.0)
    assert k6_wspr.mark_time(base - 21.0, carrier=9.8) == (base - 9.0, base + 1.0)
    end, _ = k6_wspr.mark_time(base - 20.5, carrier=9.8)       # the longest wait for the drop
    assert end - (base - 20.5) < 150.0 - 10.0
