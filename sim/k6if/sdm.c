/* GP0 front-end model for sim/k6if (docs/k6-if-modem.md): complex baseband z (around the
   FD6818's low IF) -> real IF -> 1-bit sigma-delta at the GP0 clock -> sampled at the DMA rate
   -> ones counted L samples at a time, as gp0dma does. Built and loaded by frontend.py with
   ctypes. SPDX-License-Identifier: MIT */
#include <math.h>
#include <stdint.h>

static uint64_t rs = 0x9E3779B97F4A7C15ull;
static double urand(void) { /* xorshift64*, uniform 0..1 */
  rs ^= rs >> 12, rs ^= rs << 25, rs ^= rs >> 27;
  return (double)((rs * 2685821657736338717ull) >> 11) / 9007199254740992.0;
}

/* zr, zi: n complex samples at fz Hz (baseband, linearly interpolated). fif: the IF (Hz).
   fclk: the sigma-delta clock. fdma: the DMA's mean sample rate; jit: each read's timing
   error around its grid, uniform +-jit/2 of a period (bus contention). order: 1 or 2. L: samples an output (0: raw 0/1 samples).
   out: room for the outputs. Returns the outputs written. */
int k6_frontend(const double *zr, const double *zi, int n, double fz, double fif, double fclk,
                double fdma, double jit, int order, int L, int *out, int maxout, unsigned seed) {
  double i1 = 0, i2 = 0, y = 1, w = 2 * M_PI * fif / fclk, ph = 0;
  double tz = 0, dz = fz / fclk, tdma, ddma = fclk / fdma, t0;
  long kd = 0;
  long clk = 0;
  int no = 0, acc = 0, k = 0;
  rs = 0x9E3779B97F4A7C15ull ^ ((uint64_t)seed * 0x2545F4914F6CDD1Dull + 1);
  t0 = ddma * urand();
  tdma = t0;
  for (;;) {
    int j = (int)tz;
    double f = tz - j, re, im, x;
    if (j + 1 >= n || no >= maxout) break;
    re = zr[j] + f * (zr[j + 1] - zr[j]);
    im = zi[j] + f * (zi[j + 1] - zi[j]);
    x = re * cos(ph) - im * sin(ph);
    ph += w;
    if (ph > 2 * M_PI) ph -= 2 * M_PI;
    if (order == 1) {
      i1 += x - y;
      y = i1 >= 0 ? 1 : -1;
    } else {
      i1 += x - y;
      i2 += i1 - y;
      y = i2 >= 0 ? 1 : -1;
    }
    clk++;
    tz += dz;
    while (tdma < clk && no < maxout) {
      /* the DMA's clock is the crystal's: jitter around a fixed grid, not a random walk */
      tdma = t0 + ddma * (++kd + jit * (urand() - 0.5));
      if (!L) {
        out[no++] = y > 0;
        continue;
      }
      acc += y > 0;
      if (++k == L) {
        out[no++] = 2 * acc - L;
        acc = k = 0;
      }
    }
  }
  return no;
}
