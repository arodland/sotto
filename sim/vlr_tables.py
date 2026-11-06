"""Very-low-rate sotto (sim/sotto_vlr.py): the modes' bit layouts and the tables as the C
decoder stores them. numpy only, so tools/export_vlr_c.py (make check-tables) needs nothing else.
SPDX-License-Identifier: MIT"""
import os
import numpy as np

TABLES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "vlr_tables.npz")

MODES = {
    # name: N, voicing bits, pitch centre bits, slope bits, energy mean bits, energy residual
    # bits, shape anchors as (frame offset, stage bits)
    "600": dict(N=4, bv=3, bw=7, bs=3, be=5, br=5, anchors=((0, (7, 6)), (2, (7, 5)))),
    "400": dict(N=6, bv=3, bw=7, bs=3, be=5, br=5, anchors=((0, (9,)), (2, (8,)), (4, (8,)))),
}


def mode_bits(cfg):
    return cfg["bv"] + cfg["bw"] + cfg["bs"] + cfg["be"] + cfg["br"] + \
        sum(sum(b) for _, b in cfg["anchors"])


def quantize_tables(t):
    """The tables as the C decoder stores them: slopes and energy residuals in 1/16 steps,
    shape codebooks as int8 times one step per stage (max |value| / 127, rounded to Q16, as
    c/sotto_vlr_tables.c stores it, so the C encoder searches the same values). Idempotent."""
    t = dict(t)
    for k in list(t):
        if not k.startswith("m"):
            continue
        name = k.split("_", 1)[1]
        if name in ("slope", "eres"):
            t[k] = np.round(np.asarray(t[k], float) * 16) / 16
        elif name.startswith("a"):
            c = np.asarray(t[k], float)
            step = np.abs(c).max() / 127
            t[k] = np.round(c / step) * (np.round(step * 65536) / 65536)
    return t
