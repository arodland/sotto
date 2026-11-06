"""Write c/k6/ft8/ft8k_tables.h from ft8_lib's ft8/constants.c (kgoba/ft8_lib, MIT): the
(174,91) LDPC code's generator and parity-check rows, the only protocol tables the K6 FT8
receiver (docs/k6-ft8-wspr.md) needs that it can't make itself.

  python3 tools/gen_ft8_tables.py PATH/TO/ft8_lib/ft8/constants.c > c/k6/ft8/ft8k_tables.h

It only parses the numbers. MIT licence."""
import re
import sys


def table(src, name):
    m = re.search(name + r"\[[^]]*\]\[[^]]*\]\s*=\s*\{(.*?)\};", src, re.S)
    rows = re.findall(r"\{([^{}]*)\}", m.group(1))
    return [[int(v, 0) for v in r.replace(" ", "").split(",") if v] for r in rows]


def main():
    src = open(sys.argv[1]).read()
    gen = table(src, "kFTX_LDPC_generator")
    nm = table(src, "kFTX_LDPC_Nm")
    assert len(gen) == 83 and all(len(r) == 12 for r in gen)
    assert len(nm) == 83 and all(len(r) in (6, 7) for r in nm)
    print("/* FT8's (174,91) LDPC code, from ft8_lib's ft8/constants.c (Copyright (c) 2018 Karlis")
    print("   Goba, MIT licence; the code itself is the WSJT-X FT8 protocol's). Written by")
    print("   tools/gen_ft8_tables.py; don't edit.")
    print("   FK_GEN: the 83 parity rows of the generator over the 91 message+CRC bits, MSB first.")
    print("   FK_NM: the bits (1-based, 0 = none) in each of the 83 parity checks.")
    print("   SPDX-License-Identifier: MIT */")
    print("#ifndef FT8K_TABLES_H\n#define FT8K_TABLES_H\n#include <stdint.h>")
    print("static const uint8_t FK_GEN[83][12] = {")
    for r in gen:
        print("  {" + ", ".join("0x%02x" % v for v in r) + "},")
    print("};")
    print("static const uint8_t FK_NM[83][7] = {")
    for r in nm:
        r = r + [0] * (7 - len(r))
        print("  {" + ", ".join("%d" % v for v in r) + "},")
    print("};\n#endif")


if __name__ == "__main__":
    main()
