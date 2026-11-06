"""Upload ARQ end to end on this machine: the K6 loader's reports (k6_mod.c via km_host) as the
air audio the K6 sends (F/1800, the chip's framing), decoded back by modem/k6mod.py's reports()
through noise, plus the decisions tools/k6_upload.py makes from them and the CLI's --arq.
SPDX-License-Identifier: MIT"""
import os
import subprocess
import sys
import types

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_k6_mod import KEY, fake_module, need_host, run_host_reports  # noqa: E402

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from modem import fsk, k6, k6mod, pager  # noqa: E402
import k6_upload  # noqa: E402


def air(packets, fs=48000, snr_db=None, seed=1):
    """packets as the K6's chip sends them (F/1800, L framing) with silence around"""
    a = types.SimpleNamespace(prime=False, baud=1800, payload="L", later_preamble=None, first_preamble=None,
                              lead_ms=150.0, stream=False, phy="F", level=0.5, invert=False, comp=None,
                              max_gain=30.0, tones=None, cw_id=None, cw_wpm=20)
    x = k6.build_audio(list(packets), a, fs)
    x = np.concatenate([np.zeros(fs // 2), x, np.zeros(fs // 2)])
    if snr_db is not None:
        rms = np.sqrt(np.mean(x[x != 0] ** 2))
        x = x + np.random.default_rng(seed).normal(0, rms * 10 ** (-snr_db / 20), len(x))
    return x


@need_host
@pytest.mark.parametrize("snr_db", [None, 12])
def test_partway_report_through_the_air(snr_db):
    img = fake_module(500)
    pk = k6mod.packets(img, 9, 64, arq=True)
    lost = [2, 5]
    _, status, text = run_host_reports([p for i, p in enumerate(pk) if i not in lost])
    reps = k6mod.reports(air([status, status, text], snr_db=snr_db), 48000)
    kinds = [r["kind"] for _, r in reps]
    assert kinds.count("status") >= 1 and "text" in kinds, reps
    st, joined = k6mod.collect(reps)
    assert st["state"] == "partway" and st["missing"] == lost and st["xfer"] == 9
    assert joined == "hello\n"
    assert k6_upload.next_step(st, len(pk), 9) == ("send", lost)


def test_next_step():
    base = dict(xfer=4, state="partway", missing=[], r=1, ret=0, ms=0, stack_free=0)
    assert k6_upload.next_step(None, 7, 4) == ("send", list(range(7)))
    assert k6_upload.next_step(dict(base, missing=[0, 6]), 7, 4) == ("send", [0, 6])
    assert k6_upload.next_step(base, 7, 4) == ("send", list(range(7)))
    assert k6_upload.next_step(dict(base, state="ran", ret=-9, ms=1500, stack_free=900), 7, 4) == \
        ("done", "ran: returned -9 in 1500 ms, 900 B stack free")
    assert k6_upload.next_step(dict(base, state="refused", r=-4), 7, 4) == ("stop", "refused: wrong key")
    assert k6_upload.next_step(dict(base, xfer=5), 7, 4)[0] == "stop"


@need_host
def test_cli_arq_upload_and_reports(tmp_path):
    key = tmp_path / "k"
    key.write_text(KEY.hex() + "\n")
    code = tmp_path / "m.bin"
    code.write_bytes(bytes(150))
    w = tmp_path / "up.wav"
    r = subprocess.run([sys.executable, "-m", "modem.k6", "mod", str(w), str(code), "--key-file", str(key),
                        "--serial", "321", "--xfer", "7", "--phy", "F", "--baud", "1800", "--arq",
                        "--telemetry", "3", "--copies", "1"], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "reply asked" in r.stderr
    x, fs = k6._read(str(w))
    d = fsk.Demodulator("F", 1800, fsk.FS, 64)
    got = [k6mod.parse(p) for _, p, _, _ in d.demodulate(fsk.resample(x, fs, fsk.FS))]
    got = [g for g in got if g]
    assert len(got) == 3 and all(g[0] == 7 for g in got)
    img = b"".join(g[3] for g in sorted(got, key=lambda g: g[1]))
    assert img[14] == 3          # the telemetry parts asked for
    assert all(pager._whiten(p)[0] == k6mod.MAGIC_ARQ
               for p in k6mod.packets(img[:16 + 150 + 8], 7, 64, arq=True))
    r = subprocess.run([sys.executable, "-m", "modem.k6", "mod", str(w), str(code), "--key-file", str(key),
                        "--telemetry", "3"], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode != 0 and "--arq" in r.stderr
    # the reports subcommand on a recording of the K6
    pk = k6mod.packets(k6mod.image(bytes(150), 0, 321, KEY, tlm=3), 7, 64, arq=True)
    _, status, text = run_host_reports(pk)
    import soundfile as sf
    rec = tmp_path / "rec.wav"
    sf.write(str(rec), air([status, text], snr_db=15), 48000, subtype="PCM_16")
    r = subprocess.run([sys.executable, "-m", "modem.k6", "reports", str(rec)], cwd=ROOT, capture_output=True,
                       text=True)
    assert r.returncode == 0, r.stderr
    assert "xfer 7 ran, 3/3 chunks, r=3 (ready)" in r.stdout and "hello" in r.stdout, r.stdout


def test_reports_ignore_other_packets():
    """voice and module packets on the channel are not reports"""
    pk = k6mod.packets(fake_module(120), 2, 64)
    assert k6mod.reports(air(pk), 48000) == []
