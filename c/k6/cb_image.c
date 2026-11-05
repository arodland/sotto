/* cb_image [-c] [OUT.bin]: the codebook image for the K6's SPI flash (sp_cb.h: header, then
 * the table), built from this tree's fx_tables.c. -c prints just its table CRC-32 as 0x...,
 * which tools/k6_build.sh passes to the K6 build as SP_CB_CRC; otherwise it writes OUT.bin
 * and prints the sizes and CRC. SPDX-License-Identifier: MIT */
#include <stdio.h>
#include <string.h>
#include "sp_cb.h"

int main(int argc, char **argv) {
  uint8_t h[SP_CB_HDR], b[256];
  uint32_t len = sp_cb_table_bytes(), off;
  FILE *f;
  sp_cb_header(h);
  if (argc == 2 && !strcmp(argv[1], "-c")) {
    printf("0x%08x\n", (unsigned)sp_cb_expected());
    return 0;
  }
  if (argc != 2) { fprintf(stderr, "usage: %s [-c] OUT.bin\n", argv[0]); return 1; }
  if (!(f = fopen(argv[1], "wb"))) { perror("cb_image"); return 1; }
  fwrite(h, 1, sizeof h, f);
  for (off = 0; off < len; off += sizeof b) {
    unsigned n = len - off < sizeof b ? (unsigned)(len - off) : (unsigned)sizeof b;
    sp_cb_table(off, b, n);
    fwrite(b, 1, n, f);
  }
  fclose(f);
  printf("%s: %u header + %u table bytes, crc 0x%08x\n", argv[1], (unsigned)SP_CB_HDR, (unsigned)len,
         (unsigned)sp_cb_expected());
  return 0;
}
