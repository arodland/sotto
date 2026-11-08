/* dmrrx_host: the K6's DMR decoder (c/k6/dmrsync.h + c/k6/mod/dmrdec.inc) on the host, for
   tests/test_k6_dmrdec.py. Reads int16 counts (2 * ones - 64, little-endian) on stdin and
   writes dmrrx's decode lines (L, E, A, K, H) as they come, then one summary line:
     n rd= g= L= E= mids=/ok=
   argv[1]: cps8 (96 MHz cycles a DMA transfer, Q8; default 4476 = 5.49 MS/s). No DMA, no
   pauses: housekeeping's share runs every 4096 counts as on the radio (~48 ms).
   SPDX-License-Identifier: MIT */
#include <stdio.h>
#include <stdlib.h>
#include "k6_mod.h"
#define DS_FRING 512u
#define NOINLINE __attribute__((noinline))
#include "dmrburst.h"
#include "dmrsync.h"

static char *put(char *p, const char *s) {
  while (*s) *p++ = *s++;
  return p;
}

static char *kv(char *p, const char *k, int v) {
  p = put(p, k);
  return p + sprintf(p, "%d", v);
}

#include "mod/dmrdec.inc"

int main(int argc, char **argv) {
  static ds_t d;
  static dx_t x;
  int16_t buf[4096];
  size_t n, i, hk = 0;
  char line[96], *p;
  ds_init(&d, argc > 1 ? (uint32_t)atoi(argv[1]) : 4476u);
  while ((n = fread(buf, 2, 4096, stdin)) > 0)
    for (i = 0; i < n; i++) {
      if (ds_count(&d, buf[i])) dx_hit(&x, &d);
      if (x.nq) dx_poll(&x, &d);
      if (++hk == 4096) {
        hk = 0;
        while ((p = dx_house(&x, line)) != 0) *p = 0, puts(line);
      }
    }
  while ((p = dx_house(&x, line)) != 0) *p = 0, puts(line);
  printf("n rd=%u g=%u L=%u E=%u mids=%u/%u\n", x.nrd, x.ngol, x.nlc, x.nemb, x.nmid, x.nmok);
  return 0;
}
