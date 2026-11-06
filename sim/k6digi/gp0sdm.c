/* The K6's GP0 receive path for sim/k6digi (the same model as sim/k6if/sdm.c in the IF
   softmodem study, docs/k6-if-modem.md): complex baseband z around the FD6818's low IF ->
   real IF -> 2nd-order 1-bit sigma-delta at the GP0 clock -> read by DMA at its mean rate
   with jitter around a fixed grid -> ones counted L at a time, out = 2 * ones - L.
   The IF oscillator is a rotating phasor, renormalised, so 100 M steps cost seconds.
   SPDX-License-Identifier: MIT */
#include <math.h>
#include <stdint.h>

static uint64_t rs;
static double urand(void) {
  rs ^= rs >> 12, rs ^= rs << 25, rs ^= rs >> 27;
  return (double)((rs * 2685821657736338717ull) >> 11) / 9007199254740992.0;
}

int gp0_counts(const double *zr, const double *zi, int n, double fz, double fif, double fclk,
               double fdma, double jit, int L, int8_t *out, int maxout, unsigned seed) {
  double i1 = 0, i2 = 0, y = 1, cr = 1, ci = 0, w = 2 * M_PI * fif / fclk;
  double wr = cos(w), wi = sin(w), tz = 0, dz = fz / fclk, ddma = fclk / fdma, t0, tdma;
  long kd = 0, clk = 0;
  int no = 0, acc = 0, k = 0;
  rs = 0x9E3779B97F4A7C15ull ^ ((uint64_t)seed * 0x2545F4914F6CDD1Dull + 1);
  t0 = ddma * urand();
  tdma = t0;
  for (;;) {
    int j = (int)tz;
    double f = tz - j, re, im, x, t;
    if (j + 1 >= n || no >= maxout) break;
    re = zr[j] + f * (zr[j + 1] - zr[j]);
    im = zi[j] + f * (zi[j + 1] - zi[j]);
    x = re * cr - im * ci;
    t = cr * wr - ci * wi, ci = cr * wi + ci * wr, cr = t;
    if ((clk & 1023) == 0) {
      double m = 1.5 - 0.5 * (cr * cr + ci * ci);
      cr *= m, ci *= m;
    }
    i1 += x - y;
    i2 += i1 - y;
    y = i2 >= 0 ? 1 : -1;
    clk++;
    tz += dz;
    while (tdma < clk && no < maxout) {
      tdma = t0 + ddma * (++kd + jit * (urand() - 0.5));
      acc += y > 0;
      if (++k == L) {
        out[no++] = (int8_t)(2 * acc - L);
        acc = k = 0;
      }
    }
  }
  return no;
}
