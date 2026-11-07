"""PSK31 test signal (modem/psk31.py): fldigi's varicode for the characters it has, and a round trip
through noise. SPDX-License-Identifier: MIT"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from modem import psk31  # noqa: E402


def test_varicode_is_well_formed():
    codes = list(psk31.VARICODE.values())
    assert len(set(codes)) == len(codes)
    assert all("00" not in c and c[0] == "1" and c[-1] == "1" for c in codes)
    # spot checks against fldigi's pskvaricode.cxx (all entries matched it on 2026-11-07)
    assert psk31.VARICODE["E"] == "1110111" and psk31.VARICODE[" "] == "1" and psk31.VARICODE["W"] == "101011101"


def test_round_trip_through_noise():
    text = psk31.repeat_to("AG7EW TEST ", 10)
    assert text == "AG7EW TEST " * 2
    x = psk31.modulate(text)
    assert abs(len(x) / 48000 - (1.0 + len(psk31.bits(text)) / 31.25 + 0.3)) < 0.05
    assert np.max(np.abs(x)) <= 0.5 + 1e-6
    y = x + 0.05 * np.random.default_rng(1).standard_normal(len(x))
    assert psk31.demodulate(y) == text
