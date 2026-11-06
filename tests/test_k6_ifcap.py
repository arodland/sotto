"""K6 IF softmodem bench pipeline (tools/k6_ifcap.py, c/k6/mod/gp0cap.c's log format, sim/k6if):
a simulated gp0cap log decodes, and the IF matched filter beats the discriminator on the same
captures. SPDX-License-Identifier: MIT"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import k6_ifcap as K  # noqa: E402


def test_pn9_period():
    b = K.pn9(1022)
    assert np.array_equal(b[:511], b[511:])
    assert 250 <= b[:511].sum() <= 260
    for _, mode in K.PASSES.values():
        s = K.pn_symbols(mode)
        assert len(s) == 511 and set(np.unique(s)) <= set(range(mode.M))


def test_simulated_capture_log():
    lines = K.simlog(steps=((0, 70.0), (1, 44.0)), ncaps=6, secs=2.0)
    caps = K.parse(lines)
    assert len(caps) == 1 + 2 * 2 * 6
    assert all(len(c["x"]) == 2160 for c in caps)
    res = K.analyze(caps, out=lambda *a: None)
    for pas in (1, 2):
        assert res[pas, 0]["ber_if"] < 0.03
    # FFSK at 44 dB-Hz: the IF detector well ahead of the discriminator
    assert res[1, 1]["ber_if"] < res[1, 1]["ber_disc"]
    assert res[1, 1]["ber_disc"] > 0.02
    assert 0.02 < res["floor"] < 0.12
