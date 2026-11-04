#!/usr/bin/env python3
"""Writes c/fixed/fx_cs512.inc, fx_cs512()'s packed cos | sin table as C constants (for
-DFX_CS512_CONST, which keeps it in flash instead of 2 KB of RAM), from fx_tables.c's fx_cos512.
  python3 tools/gen_cs512.py [--check]
SPDX-License-Identifier: MIT"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def table():
    s = open(os.path.join(ROOT, "c", "fixed", "fx_tables.c")).read()
    i = s.index("const int16_t fx_cos512[512] = {")
    vals = [int(x) for x in re.findall(r"-?\d+", s[i:s.index("};", i)].split("{", 1)[1])]
    assert len(vals) == 512
    return [(vals[j] & 0xFFFF) | ((vals[(j - 128) & 511] & 0xFFFF) << 16) for j in range(512)]


def text():
    out = ["/* fx_cs512()[j] = (uint16_t)fx_cos512[j] | (uint16_t)fx_cos512[(j - 128) & 511] << 16, the packed",
           "   cos | sin table, as constants (FX_CS512_CONST: flash instead of 2 KB of RAM). Generated from",
           "   fx_tables.c by tools/gen_cs512.py; tests/test_k6_pager.py checks it. SPDX-License-Identifier: MIT */"]
    cs = table()
    for k in range(0, 512, 6):
        out.append("  " + ", ".join("0x%08xu" % v for v in cs[k:k + 6]) + ",")
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    path = os.path.join(ROOT, "c", "fixed", "fx_cs512.inc")
    if "--check" in sys.argv:
        sys.exit(0 if open(path).read() == text() else 1)
    open(path, "w").write(text())
