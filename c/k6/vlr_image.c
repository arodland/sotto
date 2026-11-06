/* vlr_image [-c] [OUT.bin]: sotto 600 / 400's codebook image for the K6's SPI flash
 * (sp_vlr.h: header, then vlr_cb_all), from this tree's c/sotto_vlr_tables.c. -c prints just
 * its table CRC-32 as 0x..., which tools/k6_build.sh passes to the K6 build as SP_VLR_CRC.
 * SPDX-License-Identifier: MIT */
#include <stdio.h>
#include <string.h>
#include "sp_vlr.h"

int main(int argc, char **argv) {
  uint8_t h[SP_VLR_HDR], b[256];
  uint32_t len = sp_vlr_table_bytes(), off;
  FILE *f;
  sp_vlr_header(h);
  if (argc == 2 && !strcmp(argv[1], "-c")) {
    printf("0x%08x\n", (unsigned)sp_vlr_expected());
    return 0;
  }
  if (argc != 2) { fprintf(stderr, "usage: %s [-c] OUT.bin\n", argv[0]); return 1; }
  if (!(f = fopen(argv[1], "wb"))) { perror("vlr_image"); return 1; }
  fwrite(h, 1, sizeof h, f);
  for (off = 0; off < len; off += sizeof b) {
    unsigned n = sp_vlr_table(off, b, sizeof b);
    fwrite(b, 1, n, f);
  }
  fclose(f);
  printf("%s: %u header + %u table bytes, crc 0x%08x\n", argv[1], (unsigned)SP_VLR_HDR, (unsigned)len,
         (unsigned)sp_vlr_expected());
  return 0;
}
