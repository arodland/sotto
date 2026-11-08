/* dmrsync_host: c/k6/dmrsync.h on the host, for tests/test_k6_dmr.py. Reads int16 counts
   (2 * ones - 64, little-endian) on stdin, writes one line per hit:
     j fam sign r2q8 conf
   argv[1]: cps8 (96 MHz cycles a DMA transfer, Q8; default 4476 = 5.49 MS/s).
   SPDX-License-Identifier: MIT */
#include <stdio.h>
#include <stdlib.h>
#include "dmrsync.h"

int main(int argc, char **argv) {
  ds_t d;
  int16_t buf[4096];
  size_t n, i;
  ds_init(&d, argc > 1 ? (uint32_t)atoi(argv[1]) : 4476u);
  while ((n = fread(buf, 2, 4096, stdin)) > 0)
    for (i = 0; i < n; i++) {
      int r = ds_count(&d, buf[i]);
      if (r) printf("%u %d %d %u %d\n", d.hj, d.hfam, d.hsign, d.hr2, r == DS_CONF);
    }
  return 0;
}
