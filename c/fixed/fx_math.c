/* Fixed-point math: table cos, log2 and exp2, dB conversions. See fx.h.
 * SPDX-License-Identifier: MIT */
#include "fx.h"
#include "fx_tables.h"

#ifdef FX_CHECK
#include <stdio.h>
#include <stdlib.h>
int16_t fx_hi_check(int32_t v) {
  if (v >= ((int32_t)1 << 30) || v < -((int32_t)1 << 30)) {
    fprintf(stderr, "fx: %ld is too large to split into 16-bit halves\n", (long)v);
    abort();
  }
  return (int16_t)(v >> 15);
}
void fx_acc_check(fx_acc a) {
  if (a >= ((fx_acc)1 << 39) || a <= -((fx_acc)1 << 39)) {
    fprintf(stderr, "fx: accumulator beyond 40 bits: %lld\n", (long long)a);
    abort();
  }
}
#endif

int16_t fx_cos(uint32_t ph) {
  int i = (int)(ph >> 23);
  int32_t r = (int32_t)((ph >> 7) & 0xFFFF);
  int32_t c0 = fx_cos512[i], c1 = fx_cos512[(i + 1) & 511];
  return (int16_t)(c0 + (((int32_t)fx_mul32x16(r, (int16_t)(c1 - c0)) + 32768) >> 16));   /* |c1 - c0| < 2^9 */
}

/* position of the highest set bit of x > 0 (C55x: 31 - _lnorm) */
static int msb32(uint32_t x) {
  int n = 0;
  if (x >> 16) { n += 16; x >>= 16; }
  if (x >> 8) { n += 8; x >>= 8; }
  if (x >> 4) { n += 4; x >>= 4; }
  if (x >> 2) { n += 2; x >>= 2; }
  if (x >> 1) n += 1;
  return n;
}

int32_t fx_log2(uint32_t x) {
  int e = msb32(x);
  uint32_t m = x << (31 - e);                 /* [2^31, 2^32) */
  int i = (int)((m >> 23) & 255);
  int32_t r = (int32_t)((m >> 7) & 0xFFFF);
  int32_t a = fx_log2_tab[i], b = fx_log2_tab[i + 1];
  int32_t l = a + (int32_t)((fx_mul32(b - a, r) + 32768) >> 16);   /* Q30 */
  return (int32_t)e * 65536 + ((l + 8192) >> 14);
}

int32_t fx_log2_acc(fx_uacc x) {
  int sh = 0;
  while (x >> 32) { x >>= 1; sh++; }
  return fx_log2((uint32_t)x) + (int32_t)sh * 65536;
}

fx_uacc fx_udiv_shl(fx_uacc a, int sh, fx_uacc d, fx_uacc *rem) {
  int c, room = 40;                            /* room: 40 - bit length of d */
  fx_uacc q, r;
  if (d < ((fx_uacc)1 << 28) && a < d) {       /* 32-bit steps: cheaper than 40-bit ones */
    uint32_t d32 = (uint32_t)d, r32 = (uint32_t)a, qi;
    room = 32;
    while ((d32 >> (32 - room)) != 0) room--;
    q = 0;
    while (sh > 0) {
      c = sh < room ? sh : room;
      r32 <<= c;
      qi = r32 / d32;
      r32 -= qi * d32;
      q = (q << c) + qi;
      sh -= c;
    }
    if (rem) *rem = r32;
    return q;
  }
  q = a / d, r = a % d;
  while (room > 0 && (d >> (40 - room)) != 0) room--;
  while (sh > 0) {                             /* r < d, so r << c stays below 2^40 */
    c = sh < room ? sh : room;
    r <<= c;
    q = (q << c) + r / d;
    r %= d;
    sh -= c;
  }
  if (rem) *rem = r;
  return q;
}

/* one radix-2 butterfly: b *= W (W = c - j s), then a, b = (a + b) / 2, (a - b) / 2, rounded.
   round((br c + bi s) / 2^15) and round((bi c - br s) / 2^15), each 32 x 16 product as two
   16 x 16 ones: x = hi 2^15 + lo, so round(x c / 2^15) = hi c + round(lo c / 2^15), exactly,
   in 32 bits (|lo|, |c|, |s| <= 32767), so no 40-bit arithmetic, which the C55x compiler
   handles by switching modes */
#if !(defined(__TMS320C55X__) && !defined(FX_FFT_C))
static inline void bfly(int32_t *ar, int32_t *ai, int32_t *br, int32_t *bi, int16_t c, int16_t s) {
#ifdef FX_WIDE_MUL
  int32_t xr = fx_q15_mac2(*br, c, *bi, s);
  int32_t xi = (int32_t)(((fx_acc)*bi * c - (fx_acc)*br * s + 16384) >> 15);
#else
  int16_t rh = FX_HI(*br), rl = FX_LO(*br), ih = FX_HI(*bi), il = FX_LO(*bi);
  int32_t xr = (int32_t)rh * c + (int32_t)ih * s + (((int32_t)rl * c + (int32_t)il * s + 16384) >> 15);
  int32_t xi = (int32_t)ih * c - (int32_t)rh * s + (((int32_t)il * c - (int32_t)rl * s + 16384) >> 15);
#endif
  int32_t a0 = *ar, a1 = *ai;
  *ar = (a0 + xr + 1) >> 1; *ai = (a1 + xi + 1) >> 1;
  *br = (a0 - xr + 1) >> 1; *bi = (a1 - xi + 1) >> 1;
}
/* the two butterfly loops (c55x/fft.asm on the C55x): cnt butterflies with one twiddle,
   the pointers stepping by stride values; and the 2q consecutive butterflies of one group
   with the twiddles W^(k step), whose sine index k step + 384 passes 512 at k = q */
void fx_fft_bfly1(int32_t *ar, int32_t *ai, int32_t *br, int32_t *bi, int cnt, int stride, int32_t cs) {
  int16_t c = (int16_t)(cs >> 16), s = (int16_t)(uint16_t)(cs & 0xFFFF);
  for (; cnt > 0; cnt--, ar += stride, ai += stride, br += stride, bi += stride) bfly(ar, ai, br, bi, c, s);
}
void fx_fft_bfly_grp(int32_t *ar, int32_t *ai, int32_t *br, int32_t *bi, int q, int step) {
  const int16_t *cp = fx_cos512, *sp = fx_cos512 + 384;
  int k;
  for (k = 0; k < q; k++, cp += step, sp += step) bfly(ar++, ai++, br++, bi++, *cp, *sp);
  sp = fx_cos512;
  for (k = 0; k < q; k++, cp += step, sp += step) bfly(ar++, ai++, br++, bi++, *cp, *sp);
}
#endif

/* in-place radix-2 FFT, decimation in time, forward (e^-j), every stage halves, so the
   output is the DFT / n. |input| <= 2^23 keeps every sum inside 32 bits. The early stages
   (few twiddles) loop over the groups inside each twiddle, the later ones over the twiddles
   inside each group, so the inner loop is long and walks memory in steps of one either way;
   the arithmetic is the same butterfly in the same order within each group. */
void fx_fft(int32_t *re, int32_t *im, int n) {
  int i, j, len, k, step;
  for (i = 0, j = 0; i < n; i++) {        /* bit reversal */
    if (i < j) {
      int32_t t = re[i]; re[i] = re[j]; re[j] = t;
      t = im[i]; im[i] = im[j]; im[j] = t;
    }
    int m = n >> 1;
    while (m >= 1 && (j & m)) { j ^= m; m >>= 1; }
    j |= m;
  }
  FX_PROF(31, 1);
  for (len = 2, step = 256; len <= n; len <<= 1, step >>= 1) {
    int half = len >> 1;
    if (half < 8) {
      for (k = 0; k < half; k++) {              /* one twiddle factor, every group */
        uint16_t c = (uint16_t)fx_cos512[k * step], s = (uint16_t)fx_cos512[(k * step + 384) & 511];
        fx_fft_bfly1(&re[k], &im[k], &re[k + half], &im[k + half], n / len, len, (int32_t)(((uint32_t)c << 16) | s));
      }
    } else {
      int g;
      for (g = 0; g < n; g += len) fx_fft_bfly_grp(&re[g], &im[g], &re[g + half], &im[g + half], half >> 1, step);
    }
  }
  FX_PROF(31, 0);
}

/* the FFT of n real samples in re[0..n) (n = 2 n2) through an n2-point complex FFT:
   deinterleave z[m] = x[2m] + j x[2m+1] into re/im[0..n2) (im on input is ignored). */
void fx_rfft_pack(int32_t *re, int32_t *im, int n2) {
  int m;
  for (m = 0; m < n2; m++) {
    im[m] = re[2 * m + 1];
    re[m] = re[2 * m];
  }
}

/* the split after fx_rfft_pack and an n2-point FFT Z of the packed samples: writes bins
   X[0..n2] of the real input into re/im[0..n2], as (Z[k] + Z*[n2-k] + W^k (Z[k] - Z*[n2-k]) / j) / 4,
   W = e^(-2 pi j / n): for Z = DFT / n2 that is the n-point DFT / n. ct: cos(2 pi i / n),
   i < n, Q15 (n a multiple of 4). |Z| < 2^28. */
void fx_rfft_split(int32_t *re, int32_t *im, int n2, const int16_t *ct, int n) {
  int k, q3 = 3 * (n >> 2);
  int32_t z0r = re[0], z0i = im[0];
  for (k = 1; k < n2 - k; k++) {
    int j = n2 - k;
    int16_t c = ct[k], s = ct[k + q3 < n ? k + q3 : k + q3 - n];
    int32_t ar = re[k] + re[j], ai = im[k] - im[j];         /* A = Z[k] + Z*[j] */
    int32_t br = re[k] - re[j], bi = im[k] + im[j];         /* B = Z[k] - Z*[j], D = B / j = (bi, -br) */
    int32_t wr = fx_q15_mac2(bi, c, -br, s), wi = fx_q15_mac2(-br, c, -bi, s);   /* W^k D */
    re[k] = (ar + wr + 2) >> 2, im[k] = (ai + wi + 2) >> 2;
    re[j] = (ar - wr + 2) >> 2, im[j] = (wi - ai + 2) >> 2; /* W^j D' = (-wr, wi) for X[j] */
  }
  if (k == n2 - k) re[k] = (re[k] + 1) >> 1, im[k] = (1 - im[k]) >> 1;   /* X[n2/2] = Z*[n2/2] / 2 */
  re[0] = (z0r + z0i + 1) >> 1, im[0] = 0;
  re[n2] = (z0r - z0i + 1) >> 1, im[n2] = 0;
}

/* the inverse direction: from bins Y[0..n2] of a Hermitian spectrum (the rest implied),
   Z[k] = (G[k] + j H[k]) / 2 for k < n2, G = Y[k] + Y*[n2-k], H = (Y[k] - Y*[n2-k]) W^k.
   The n2-point FFT of Z, as DFT / n2, then holds v[2m] in re[m] and v[2m+1] in im[m], where
   v = the n-point DFT of Y / n (real); fx_rfft_unpack interleaves it into re[0..n). */
void fx_irfft_pre(int32_t *re, int32_t *im, int n2, const int16_t *ct, int n) {
  int k, q3 = 3 * (n >> 2);
  {
    int32_t gr = re[0] + re[n2], gi = im[0] - im[n2], er = re[0] - re[n2], ei = im[0] + im[n2];
    re[0] = (gr - ei + 1) >> 1, im[0] = (gi + er + 1) >> 1;
  }
  for (k = 1; k < n2 - k; k++) {
    int j = n2 - k;
    int16_t c = ct[k], s = ct[k + q3 < n ? k + q3 : k + q3 - n];
    int32_t gr = re[k] + re[j], gi = im[k] - im[j];         /* G = Y[k] + Y*[j] */
    int32_t er = re[k] - re[j], ei = im[k] + im[j];         /* E = Y[k] - Y*[j], H = E W^k */
    int32_t hr = fx_q15_mac2(er, c, ei, s), hi = fx_q15_mac2(ei, c, -er, s);
    re[k] = (gr - hi + 1) >> 1, im[k] = (gi + hr + 1) >> 1;
    re[j] = (gr + hi + 1) >> 1, im[j] = (hr - gi + 1) >> 1; /* G' = G*, H' = (hr, -hi) */
  }
  /* k = n2/2: Z = Y */
}

void fx_rfft_unpack(int32_t *re, const int32_t *im, int n2) {
  int m;
  for (m = n2 - 1; m >= 0; m--) {
    re[2 * m + 1] = im[m];
    re[2 * m] = re[m];
  }
}

#if !(defined(__TMS320C55X__) && !defined(FX_BINS_C))
uint32_t fx_bins_max(const int32_t *re, const int32_t *im, int n) {
  int b;
  uint32_t m = 0;
  for (b = 0; b < n; b++) {
    uint32_t a = (uint32_t)(re[b] < 0 ? -re[b] : re[b]), c = (uint32_t)(im[b] < 0 ? -im[b] : im[b]);
    if (a > m) m = a;
    if (c > m) m = c;
  }
  return m;
}
void fx_bins_to16(const int32_t *re, const int32_t *im, int16_t *sw, int n, int s) {
  int b;
  for (b = 0; b < n; b++) {
    sw[2 * b] = (int16_t)(s > 0 ? fx_rshr32(re[b], s) : (int32_t)((uint32_t)re[b] << -s));
    sw[2 * b + 1] = (int16_t)(s > 0 ? fx_rshr32(im[b], s) : (int32_t)((uint32_t)im[b] << -s));
  }
}
/* power of a bin rounded right by t bits */
static fx_acc sq2(int32_t re, int32_t im, int t) {
  int32_t a = t ? fx_rshr32(re, t) : re, b = t ? fx_rshr32(im, t) : im;
  return fx_sq32(a) + fx_sq32(b);
}
fx_acc fx_bins_pow_max(const int32_t *re, const int32_t *im, int n, int t) {
  int b;
  fx_acc pm = 0;
  for (b = 0; b < n; b++) {
    fx_acc q = sq2(re[b], im[b], t);
    if (q > pm) pm = q;
  }
  return pm;
}
void fx_bins_pow(const int32_t *re, const int32_t *im, uint32_t *p, int n, int t, int32_t sp, uint32_t rnd) {
  int b;
  for (b = 0; b < n; b++) p[b] = (uint32_t)((sq2(re[b], im[b], t) + rnd) >> sp);
}
#endif

#if !(defined(__TMS320C55X__) && !defined(FX_MP_C))
void fx_mp_sums(const int16_t *h, const int16_t *l, const int16_t *row, int n1, int n2, fx_acc out[4]) {
  int j;
  fx_acc a = 0, b = 0, c = 0, d = 0;
  for (j = 0; j < n1; j++) a += (fx_acc)h[j] * row[j], b += (fx_acc)l[j] * row[j];
  for (; j < n1 + n2; j++) c += (fx_acc)h[j] * row[j], d += (fx_acc)l[j] * row[j];
  out[0] = a, out[1] = b, out[2] = c, out[3] = d;
}
#endif

#if !(defined(__TMS320C55X__) && !defined(FX_OA_C))
fx_acc fx_oa_pair(const int16_t *bin, const int16_t *re, const int16_t *im, int nb, int n, fx_acc *a1) {
  int j;
  fx_acc a0 = 0, b1 = 0;
  for (j = 0; j < nb; j++) {
    int x = (int)(((unsigned)bin[j] * (unsigned)n) & 511u);   /* wraps at 16 bits on the C55x */
    int y = (int)(((unsigned)bin[j] * (unsigned)(n + 1)) & 511u);
    a0 += (fx_acc)re[j] * fx_cos512[x] - (fx_acc)im[j] * fx_cos512[(x - 128) & 511];
    b1 += (fx_acc)re[j] * fx_cos512[y] - (fx_acc)im[j] * fx_cos512[(y - 128) & 511];
  }
  *a1 = b1;
  return a0;
}
#endif

#if !(defined(__TMS320C55X__) && !defined(FX_CB_RESID_C))
fx_acc fx_cb_resid(const int32_t *c, const int32_t *recon, int16_t *r, int n, int32_t istep) {
  int k;
  fx_acc rr = 0;
  for (k = 0; k < n; k++) {
    fx_acc t = ((fx_acc)(c[k] - recon[k]) * istep) >> 24;
    FX_ACC_CHECK((fx_acc)(c[k] - recon[k]) * istep);
    r[k] = (int16_t)(t > 32767 ? 32767 : t < -32767 ? -32767 : t);
    rr += (fx_acc)r[k] * r[k];
  }
  return rr;
}
#endif

#if !(defined(__TMS320C55X__) && !defined(FX_HFIT_C))
void fx_hfit_sums(const int16_t *sw, const int16_t *w, int nb, fx_acc out[3]) {
  int i;
  fx_acc nr = 0, ni = 0, den = 0;
  for (i = 0; i < nb; i++) {
    nr += (fx_acc)sw[2 * i] * w[i];
    ni += (fx_acc)sw[2 * i + 1] * w[i];
    den += (fx_acc)w[i] * w[i];
  }
  out[0] = nr, out[1] = ni, out[2] = den;
}
fx_acc fx_hfit_err(const int16_t *sw, const int16_t *w, int nb, int32_t ar, int32_t ai) {
  int i;
  fx_acc err = 0;
  for (i = 0; i < nb; i++) {
    int32_t er = sw[2 * i] - fx_rshr(fx_mul32x16(ar, w[i]), 15), ei = sw[2 * i + 1] - fx_rshr(fx_mul32x16(ai, w[i]), 15);
    err += fx_sq32(er) + fx_sq32(ei);
  }
  return err;
}
fx_acc fx_hfit_pow(const int16_t *sw, int n) {
  int k;
  fx_acc s = 0;
  for (k = 0; k < n; k++) s += (fx_acc)sw[k] * sw[k];
  return s;
}
#endif

int32_t fx_exp2(int32_t y, int q) {
  int32_t t = y + (int32_t)q * 65536;
  int32_t i = t >> 16;                        /* floor (arithmetic shift) */
  int32_t f = t & 0xFFFF;
  int j = (int)(f >> 8);
  int32_t r = f & 255;
  uint32_t a = fx_exp2_tab[j], b = fx_exp2_tab[j + 1];
  uint32_t m = a + (((uint32_t)fx_mul32x16((int32_t)(b - a), (int16_t)r) + 128) >> 8);   /* Q30, [2^30, 2^31] */
  if (i >= 31) return INT32_MAX;
  int s = (int)(30 - i);
  if (s <= 0) return m > INT32_MAX ? INT32_MAX : (int32_t)m;   /* i == 30 */
  if (s >= 32) return 0;
  return (int32_t)((m + ((uint32_t)1 << (s - 1))) >> s);   /* m + 2^(s-1) <= 2^31 + 2^30: 32-bit shifts */
}

/* log2(10) / 20 x 2^16 and friends, rounded:
 *   dB Q8 -> log2 Q16:  x 256 log2(10)/20 = 42.5207 (amplitude), 85.0414 (power)
 *   log2 Q16 -> dB Q8:  x 20/log2(10)/256 = 0.0235180 (amplitude), 0.0117590 (power) */
int32_t fx_db_to_log2_amp(int32_t db) { return (int32_t)((fx_mul32(db, 2786641) + 32768) >> 16); }
int32_t fx_db_to_log2_pow(int32_t db) { return (int32_t)((fx_mul32(db, 5573281) + 32768) >> 16); }
int32_t fx_log2_to_db_amp(int32_t l2) { return (int32_t)((fx_mul32(l2, 394566) + ((int32_t)1 << 23)) >> 24); }
int32_t fx_log2_to_db_pow(int32_t l2) { return (int32_t)((fx_mul32(l2, 197283) + ((int32_t)1 << 23)) >> 24); }

int32_t fx_db_power_sum(const int32_t *v, int n) {
  int i;
  int32_t mx = v[0];
  for (i = 1; i < n; i++)
    if (v[i] > mx) mx = v[i];
  uint32_t s = 0;                              /* Q22, n <= 160 terms of at most 1 */
  for (i = 0; i < n; i++) s += (uint32_t)fx_exp2(fx_db_to_log2_pow(v[i] - mx), 22);
  return mx + fx_log2_to_db_pow(fx_log2(s) - 22 * 65536);
}
