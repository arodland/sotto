/* Checks the assembly kernels (cb_dots.asm, hsum.asm, nlp_dft.asm) against their C
 * versions on pseudo-random inputs, on the simulator: C55X_MAIN=kernels sh tools/c55x_build.sh,
 * then run kernels.out with bench.js. Prints one line per kernel, "ok" or the first mismatch.
 * SPDX-License-Identifier: MIT */
#include <stdio.h>
#include "../fx.h"
#include "../fx_tables.h"

void fx_cb_dots(const int16_t *r, const int16_t *cb, int n, const int32_t *e2, int32_t *h);
fx_acc fx_hsum(const uint32_t *p, uint32_t acc, uint32_t w, int m);
void fx_nlp_dft(const int16_t *x, const int16_t *cos512, int32_t *rr, int32_t *qq, int k0, int k1);

static uint32_t seed = 12345;
static uint32_t rnd32(void) { seed = seed * 1103515245ul + 12345ul; return seed; }

static int16_t cb[512 * 20], r[20];
static int32_t e2[512], h[512];
static uint32_t p[257];

int main(void) {
  int i, k, bad;
  /* cb_dots */
  for (i = 0; i < 512 * 20; i++) cb[i] = (i % 20) == 19 ? 0 : (int16_t)((int)(rnd32() >> 24) - 128);
  for (i = 0; i < 19; i++) r[i] = (int16_t)((int)(rnd32() >> 16) - 32768);
  r[19] = 0;
  for (i = 0; i < 512; i++) e2[i] = (int32_t)(rnd32() >> 13);
  fx_cb_dots(r, cb, 512, e2, h);
  for (bad = -1, i = 0; i < 512 && bad < 0; i++) {
    int32_t ry = 0;
    for (k = 0; k < 20; k++) ry += (int32_t)r[k] * cb[20 * i + k];
    if (h[i] != e2[i] * 32 - ry) bad = i;
  }
  printf("cb_dots %s %d\n", bad < 0 ? "ok" : "MISMATCH at", bad);
  /* hsum */
  for (i = 0; i < 257; i++) p[i] = rnd32() >> 1;
  for (bad = -1, k = 0; k < 200 && bad < 0; k++) {
    uint32_t w = (rnd32() >> 7) | (1ul << 23), acc = w + (1ul << 22), a = acc;
    int m = (int)(((256ul << 23) - (1ul << 22)) / w);
    fx_acc E = 0, F;
    if (k % 3 == 0) m -= 1;
    for (i = 0; i < m; i++, a += w) E += p[a >> 23];
    F = fx_hsum(p, acc, w, m);
    if (E != F) bad = k;
  }
  printf("hsum %s %d\n", bad < 0 ? "ok" : "MISMATCH at", bad);
  /* nlp_dft */
  {
    int16_t x[64];
    int32_t rr[130], qq[130];
    for (i = 0; i < 64; i++) x[i] = (int16_t)((int)(rnd32() >> 18) - 8192);
    for (i = 0; i < 130; i++) rr[i] = qq[i] = 0x5A5A5A5Al;
    fx_nlp_dft(x, fx_cos512, rr, qq, 15, 130);
    for (bad = -1, k = 15; k < 130 && bad < 0; k++) {
      fx_acc a = 0, b = 0;
      for (i = 0; i < 64; i++) {
        int j = (k * i) & 511;
        a += (fx_acc)x[i] * fx_cos512[j];
        b += (fx_acc)x[i] * fx_cos512[(j - 128) & 511];
      }
      if (rr[k] != fx_rshr(a, 15) || qq[k] != fx_rshr(b, 15)) {
        bad = k;
        printf("  k %d: rr %ld want %ld, qq %ld want %ld\n", k, (long)rr[k], (long)fx_rshr(a, 15), (long)qq[k], (long)fx_rshr(b, 15));
      }
    }
    printf("nlp_dft %s %d (rr[14] %s)\n", bad < 0 ? "ok" : "MISMATCH at", bad,
           rr[14] == 0x5A5A5A5Al ? "untouched" : "WRITTEN");
  }
  printf("done\n");
  return 0;
}
