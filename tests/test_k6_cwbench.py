"""CW workbench (tools/k6_cwbench.py): both models decode a repeater-like ID (open-squelch hiss
louder than the ID around its carrier, as WA7DMR on 440.925 was recorded on 2026-11-08), and the
K6 tone-detector model misses a tone above its 1350 Hz top. SPDX-License-Identifier: MIT"""
import os
import shutil
import sys

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import k6_cwbench as cb  # noqa: E402
from modem import k6  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("cc"), reason="needs a C compiler")
FS = 48000


def clip(text, tone, wpm=20, seed=1):
    rng = np.random.default_rng(seed)
    cw = k6.cw(text, fs=FS, wpm=wpm, tone=tone, level=0.07)
    hiss = rng.normal(0, 0.22, 2 * FS)
    quiet = rng.normal(0, 0.01, FS // 2)
    car = np.concatenate([quiet, cw + rng.normal(0, 0.01, len(cw)), quiet])
    return np.concatenate([hiss, car, hiss])


def calls(keys):
    return [d["call"] for d in cb.decode(keys) if d["call"]]


def test_both_models_read_a_repeater_id_in_squelch_hiss():
    x = clip("WA7DMR", 1000)
    assert calls(cb.key_ideal(x, FS)[0]) == ["WA7DMR"]
    keys, info = cb.key_k6(x, FS)
    assert calls(keys) == ["WA7DMR"] and info["tone"] == 1000


def test_the_k6_model_misses_a_tone_above_its_range():
    x = clip("K7XYZ", 1500, wpm=25)
    assert calls(cb.key_ideal(x, FS)[0]) == ["K7XYZ"]
    assert calls(cb.key_k6(x, FS)[0]) == []
