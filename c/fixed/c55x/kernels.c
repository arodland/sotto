/* Checks the assembly kernels (cb_dots, hsum, nlp_dft, recip48, bins, hfit, fft, divq15,
 * cb_resid, oa_pair, mp_sums, fft5) against their C versions on pseudo-random inputs, on
 * the simulator: C55X_MAIN=kernels sh tools/c55x_build.sh,
 * then run kernels.out with bench.js. Prints one line per kernel, "ok" or the first mismatch.
 * SPDX-License-Identifier: MIT */
#include <stdio.h>
#include "../fx.h"
#include "../fx_tables.h"

void fx_cb_dots(const int16_t *r, const int16_t *cb, int n, const int32_t *e2, int32_t *h);
fx_acc fx_hsum(const uint32_t *p, uint32_t acc, uint32_t w, int m);
void fx_nlp_dft(const int16_t *x, const int16_t *cos512, int32_t *rr, int32_t *qq, int k0, int k1);
uint32_t fx_recip48(uint32_t x);
/* the C versions of bins.asm, as in fx_math.c */
static uint32_t c_bins_max(const int32_t *re, const int32_t *im, int n) {
  int b;
  uint32_t m = 0;
  for (b = 0; b < n; b++) {
    uint32_t a = (uint32_t)(re[b] < 0 ? -re[b] : re[b]), c = (uint32_t)(im[b] < 0 ? -im[b] : im[b]);
    if (a > m) m = a;
    if (c > m) m = c;
  }
  return m;
}
static void c_bins_to16(const int32_t *re, const int32_t *im, int16_t *sw, int n, int s) {
  int b;
  for (b = 0; b < n; b++) {
    sw[2 * b] = (int16_t)(s > 0 ? fx_rshr32(re[b], s) : (int32_t)((uint32_t)re[b] << -s));
    sw[2 * b + 1] = (int16_t)(s > 0 ? fx_rshr32(im[b], s) : (int32_t)((uint32_t)im[b] << -s));
  }
}
static fx_acc sq2(int32_t re, int32_t im, int t) {
  int32_t a = t ? fx_rshr32(re, t) : re, b = t ? fx_rshr32(im, t) : im;
  return fx_sq32(a) + fx_sq32(b);
}
static fx_acc c_bins_pow_max(const int32_t *re, const int32_t *im, int n, int t) {
  int b;
  fx_acc pm = 0;
  for (b = 0; b < n; b++) {
    fx_acc q = sq2(re[b], im[b], t);
    if (q > pm) pm = q;
  }
  return pm;
}
static void c_bins_pow(const int32_t *re, const int32_t *im, uint32_t *p, int n, int t, int32_t sp, uint32_t rnd) {
  int b;
  for (b = 0; b < n; b++) p[b] = (uint32_t)((sq2(re[b], im[b], t) + rnd) >> sp);
}
/* the C versions of hfit.asm, as in fx_math.c */
static void c_hfit_sums(const int16_t *sw, const int16_t *w, int nb, fx_acc out[3]) {
  int i;
  fx_acc nr = 0, ni = 0, den = 0;
  for (i = 0; i < nb; i++) {
    nr += (fx_acc)sw[2 * i] * w[i];
    ni += (fx_acc)sw[2 * i + 1] * w[i];
    den += (fx_acc)w[i] * w[i];
  }
  out[0] = nr, out[1] = ni, out[2] = den;
}
static fx_acc c_hfit_err(const int16_t *sw, const int16_t *w, int nb, int32_t ar, int32_t ai) {
  int i;
  fx_acc err = 0;
  for (i = 0; i < nb; i++) {
    int32_t er = sw[2 * i] - fx_rshr(fx_mul32x16(ar, w[i]), 15), ei = sw[2 * i + 1] - fx_rshr(fx_mul32x16(ai, w[i]), 15);
    err += fx_sq32(er) + fx_sq32(ei);
  }
  return err;
}
static fx_acc c_hfit_pow(const int16_t *sw, int n) {
  int k;
  fx_acc s = 0;
  for (k = 0; k < n; k++) s += (fx_acc)sw[k] * sw[k];
  return s;
}

/* the C versions of fft.asm, as in fx_math.c */
static void bfly(int32_t *ar, int32_t *ai, int32_t *br, int32_t *bi, int16_t c, int16_t s) {
  int16_t rh = FX_HI(*br), rl = FX_LO(*br), ih = FX_HI(*bi), il = FX_LO(*bi);
  int32_t xr = (int32_t)rh * c + (int32_t)ih * s + (((int32_t)rl * c + (int32_t)il * s + 16384) >> 15);
  int32_t xi = (int32_t)ih * c - (int32_t)rh * s + (((int32_t)il * c - (int32_t)rl * s + 16384) >> 15);
  int32_t a0 = *ar, a1 = *ai;
  *ar = (a0 + xr + 1) >> 1; *ai = (a1 + xi + 1) >> 1;
  *br = (a0 - xr + 1) >> 1; *bi = (a1 - xi + 1) >> 1;
}
static void c_fft_bfly1(int32_t *ar, int32_t *ai, int32_t *br, int32_t *bi, int cnt, int stride, int32_t cs) {
  int16_t c = (int16_t)(cs >> 16), s = (int16_t)(uint16_t)(cs & 0xFFFF);
  for (; cnt > 0; cnt--, ar += stride, ai += stride, br += stride, bi += stride) bfly(ar, ai, br, bi, c, s);
}
static void c_fft_bfly_grp(int32_t *ar, int32_t *ai, int32_t *br, int32_t *bi, int q, int step) {
  const int16_t *cp = fx_cos512, *sp = fx_cos512 + 384;
  int k;
  for (k = 0; k < q; k++, cp += step, sp += step) bfly(ar++, ai++, br++, bi++, *cp, *sp);
  sp = fx_cos512;
  for (k = 0; k < q; k++, cp += step, sp += step) bfly(ar++, ai++, br++, bi++, *cp, *sp);
}

/* the C version of fft5.asm: the radix-5 loop of sotto_fx_hiss.c fft5x for m = 32 */
#define C5_1 10126
#define C5_2 (-26510)
#define S5_1 31164
#define S5_2 19261
static void c_fft5_stage32(int32_t *re, int32_t *im, const int32_t *sr, const int32_t *si, int t) {
  int n1, k2, m = 32;
  for (k2 = 0; k2 < m; k2++) {
    int32_t zr[5], zi[5], t1r, t1i, t2r, t2i, t3r, t3i, t4r, t4i, a1r, a1i, a2r, a2i, b1r, b1i, b2r, b2i;
    zr[0] = sr[k2], zi[0] = si[k2];
    for (n1 = 1; n1 < 5; n1++) {
      int i = n1 * k2 * t;
      int16_t c = fx_cos320[i], s = fx_cos320[i + 240 < 320 ? i + 240 : i - 80];
      int32_t yr = sr[m * n1 + k2], yi = si[m * n1 + k2];
      zr[n1] = fx_q15_mac2(yr, c, yi, s), zi[n1] = fx_q15_mac2(yi, c, -yr, s);
    }
    t1r = zr[1] + zr[4], t1i = zi[1] + zi[4];
    t2r = zr[2] + zr[3], t2i = zi[2] + zi[3];
    t3r = zr[1] - zr[4], t3i = zi[1] - zi[4];
    t4r = zr[2] - zr[3], t4i = zi[2] - zi[3];
    re[k2] = zr[0] + t1r + t2r;
    im[k2] = zi[0] + t1i + t2i;
    a1r = zr[0] + fx_q15_mac2(t1r, C5_1, t2r, C5_2);
    a1i = zi[0] + fx_q15_mac2(t1i, C5_1, t2i, C5_2);
    a2r = zr[0] + fx_q15_mac2(t1r, C5_2, t2r, C5_1);
    a2i = zi[0] + fx_q15_mac2(t1i, C5_2, t2i, C5_1);
    b1r = fx_q15_mac2(t3r, S5_1, t4r, S5_2);
    b1i = fx_q15_mac2(t3i, S5_1, t4i, S5_2);
    b2r = fx_q15_mac2(t3r, S5_2, t4r, -S5_1);
    b2i = fx_q15_mac2(t3i, S5_2, t4i, -S5_1);
    re[m + k2] = a1r + b1i, im[m + k2] = a1i - b1r;
    re[4 * m + k2] = a1r - b1i, im[4 * m + k2] = a1i + b1r;
    re[2 * m + k2] = a2r + b2i, im[2 * m + k2] = a2i - b2r;
    re[3 * m + k2] = a2r - b2i, im[3 * m + k2] = a2i + b2r;
  }
}

/* the C version of mp_sums.asm, as in fx_math.c */
static void c_mp_sums(const int16_t *h, const int16_t *l, const int16_t *row, int n1, int n2, fx_acc out[4]) {
  int j;
  fx_acc a = 0, b = 0, c = 0, d = 0;
  for (j = 0; j < n1; j++) a += (fx_acc)h[j] * row[j], b += (fx_acc)l[j] * row[j];
  for (; j < n1 + n2; j++) c += (fx_acc)h[j] * row[j], d += (fx_acc)l[j] * row[j];
  out[0] = a, out[1] = b, out[2] = c, out[3] = d;
}

/* the C version of oa_pair.asm, as in fx_math.c */
static fx_acc c_oa_pair(const int16_t *bin, const int16_t *re, const int16_t *im, int nb, int n, fx_acc *a1) {
  int j;
  fx_acc a0 = 0, b1 = 0;
  for (j = 0; j < nb; j++) {
    int x = (int)(((unsigned)bin[j] * (unsigned)n) & 511u);
    int y = (int)(((unsigned)bin[j] * (unsigned)(n + 1)) & 511u);
    a0 += (fx_acc)re[j] * fx_cos512[x] - (fx_acc)im[j] * fx_cos512[(x - 128) & 511];
    b1 += (fx_acc)re[j] * fx_cos512[y] - (fx_acc)im[j] * fx_cos512[(y - 128) & 511];
  }
  *a1 = b1;
  return a0;
}

/* the C version of cb_resid.asm, as in fx_math.c */
static fx_acc c_cb_resid(const int32_t *c, const int32_t *recon, int16_t *r, int n, int32_t istep) {
  int k;
  fx_acc rr = 0;
  for (k = 0; k < n; k++) {
    fx_acc t = ((fx_acc)(c[k] - recon[k]) * istep) >> 24;
    r[k] = (int16_t)(t > 32767 ? 32767 : t < -32767 ? -32767 : t);
    rr += (fx_acc)r[k] * r[k];
  }
  return rr;
}

/* the C version of divq15.asm, as div_q15_2 and its helpers in sotto_fx_enc.c */
static int bitlen32(uint32_t x) {
  int n = 0;
  if (x >> 16) n += 16, x >>= 16;
  if (x >> 8) n += 8, x >>= 8;
  if (x >> 4) n += 4, x >>= 4;
  if (x >> 2) n += 2, x >>= 2;
  if (x >> 1) n += 1, x >>= 1;
  return n + (int)x;
}
static int bitlen40(fx_uacc x) {
  uint32_t h = (uint32_t)(x >> 8);
  return h ? 8 + bitlen32(h) : bitlen32((uint32_t)x);
}
static uint32_t ushr40_sat(fx_uacc x, int m) {
  uint32_t h = (uint32_t)(x >> 8), lo = (uint32_t)x & 0xFF;
  if (m >= 8) return m >= 40 ? 0 : h >> (m - 8);
  if (h >> (24 + m)) return 0xFFFFFFFFu;
  return (h << (8 - m)) | (lo >> m);
}
static void c_div_q15_2(fx_acc n1, fx_acc n2, fx_acc d, int32_t *q1, int32_t *q2) {
  int sd = bitlen40((fx_uacc)d) - 16, sh, i;
  if (sd < 0) sd = 0;
  uint32_t dd = ushr40_sat((fx_uacc)d, sd);
  sh = 15 - sd;
  for (i = 0; i < 2; i++) {
    fx_acc n = i ? n2 : n1;
    fx_uacc un = (fx_uacc)(n < 0 ? -n : n);
    uint32_t num;
    if (sh >= 0) num = bitlen40(un) + sh > 32 ? 0xFFFFFFFFu : (uint32_t)un << sh;
    else num = ushr40_sat(un, -sh);
    uint32_t q = num / dd;
    if (q > 0x7FFFFFFFu) q = 0x7FFFFFFFu;
    *(i ? q2 : q1) = n < 0 ? -(int32_t)q : (int32_t)q;
  }
}

static int32_t re[257], im[257], re2[257], im2[257];
static uint32_t pa[257], pb[257];
static int16_t swa[514], swb[514];

static uint32_t seed = 12345;
static uint32_t rnd32(void) { seed = seed * 1103515245ul + 12345ul; return seed; }
/* a random value of bit length b (0 for b = 0), b <= 39 */
static fx_acc rand40(int b) {
  fx_acc x = ((fx_acc)(rnd32() & 0x7F) << 32) | rnd32();
  if (b == 0) return 0;
  return (x & (((fx_acc)1 << (b - 1)) - 1)) | ((fx_acc)1 << (b - 1));
}

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
  /* recip48: the range edges, every bit length, and random values */
  {
    int n;
    uint32_t x;
    for (bad = -1, n = 20; n < 28 && bad < 0; n++) {
      uint32_t lo = (uint32_t)1 << n, hi = ((uint32_t)1 << (n + 1)) - 1;
      if (fx_recip48(lo) != fx_recip48_c(lo)) bad = n;
      if (fx_recip48(hi) != fx_recip48_c(hi)) bad = n;
      if (fx_recip48(lo + 1) != fx_recip48_c(lo + 1)) bad = n;
      if (fx_recip48(hi - 1) != fx_recip48_c(hi - 1)) bad = n;
    }
    printf("recip48 edges %s %d\n", bad < 0 ? "ok" : "MISMATCH at bit length", bad + 1);
    for (bad = -1, i = 0; i < 20000 && bad < 0; i++) {
      x = (rnd32() >> 4) | ((uint32_t)1 << 20);             /* [2^20, 2^28) */
      if (fx_recip48(x) != fx_recip48_c(x)) {
        bad = i;
        printf("  x %lu: %lu want %lu\n", (unsigned long)x, (unsigned long)fx_recip48(x), (unsigned long)fx_recip48_c(x));
      }
    }
    printf("recip48 random %s %d\n", bad < 0 ? "ok" : "MISMATCH at", bad);
  }
  /* bins: 20 spectra of random size and level, every shift the callers use */
  {
    int r, n, t, s, sp;
    for (bad = -1, r = 0; r < 20 && bad < 0; r++) {
      int bits = 8 + (int)(rnd32() % 17);                   /* bins below 2^bits, 2^8 .. 2^24 */
      uint32_t ma, mb;
      fx_acc qa, qb;
      n = r < 10 ? 257 : 161;
      for (i = 0; i < n; i++) {
        re[i] = (int32_t)(rnd32() >> (32 - bits)) - ((int32_t)1 << (bits - 1));
        im[i] = (int32_t)(rnd32() >> (32 - bits)) - ((int32_t)1 << (bits - 1));
      }
      if (r == 0) re[3] = -((int32_t)1 << (bits - 1)), im[7] = ((int32_t)1 << (bits - 1)) - 1;
      ma = c_bins_max(re, im, n), mb = fx_bins_max(re, im, n);
      if (ma != mb) { bad = r; printf("  max %lu want %lu\n", (unsigned long)mb, (unsigned long)ma); }
      for (s = 0; ma; s++) if ((ma >> s) <= 32767) break;
      if (s == 0) while (s > -15 && (ma << (1 - s)) <= 32767) s--;
      c_bins_to16(re, im, swa, n, s), fx_bins_to16(re, im, swb, n, s);
      for (i = 0; i < 2 * n && bad < 0; i++)
        if (swa[i] != swb[i]) { bad = r; printf("  to16 %d: %d want %d (s %d)\n", i, swb[i], swa[i], s); }
      for (t = 0; (ma >> t) >= ((uint32_t)1 << 19) - 1; t++) ;
      qa = c_bins_pow_max(re, im, n, t), qb = fx_bins_pow_max(re, im, n, t);
      if (qa != qb) { bad = r; printf("  pow_max %ld want %ld (t %d)\n", (long)(qb >> 8), (long)(qa >> 8), t); }
      for (sp = 0; (qa >> sp) >= ((fx_acc)1 << 31); sp++) ;
      if (r & 1) sp = 8;
      c_bins_pow(re, im, pa, n, t, sp, (r & 1) ? 0 : sp ? (uint32_t)1 << (sp - 1) : 0);
      fx_bins_pow(re, im, pb, n, t, sp, (r & 1) ? 0 : sp ? (uint32_t)1 << (sp - 1) : 0);
      for (i = 0; i < n && bad < 0; i++)
        if (pa[i] != pb[i]) { bad = r; printf("  pow %d: %lu want %lu (t %d sp %d)\n", i, (unsigned long)pb[i], (unsigned long)pa[i], t, sp); }
    }
    printf("bins %s %d\n", bad < 0 ? "ok" : "MISMATCH at spectrum", bad);
  }
  /* hfit: fits of 1..16 bins, random spectra and window values, amplitudes of either sign */
  {
    int r, nb, k;
    int16_t *w = swb;
    for (bad = -1, r = 0; r < 40 && bad < 0; r++) {
      fx_acc oa[3], ob[3], ea, eb, pa2, pb2;
      int32_t ar, ai;
      nb = r < 2 ? 1 + r : 1 + (int)(rnd32() % 16);
      for (i = 0; i < 2 * nb; i++) swa[i] = (int16_t)((int)(rnd32() >> 16) - 32768);
      for (i = 0; i < nb; i++) w[i] = (int16_t)((int)(rnd32() >> 16) - (r & 2 ? 32768 : 0));
      if (r == 0) swa[0] = -32768, swa[1] = 32767, w[0] = 32767;
      ar = (int32_t)(rnd32() >> 12) - ((int32_t)1 << 19);  /* |a w| >> 15 < 2^19, so e^2 fits */
      ai = (int32_t)(rnd32() >> 12) - ((int32_t)1 << 19);
      c_hfit_sums(swa, w, nb, oa), fx_hfit_sums(swa, w, nb, ob);
      for (k = 0; k < 3; k++)
        if (oa[k] != ob[k]) { bad = r; printf("  sums %d: %ld want %ld (nb %d)\n", k, (long)ob[k], (long)oa[k], nb); }
      ea = c_hfit_err(swa, w, nb, ar, ai), eb = fx_hfit_err(swa, w, nb, ar, ai);
      if (ea != eb) { bad = r; printf("  err %ld want %ld (nb %d)\n", (long)(eb >> 4), (long)(ea >> 4), nb); }
      pa2 = c_hfit_pow(swa, 2 * nb), pb2 = fx_hfit_pow(swa, 2 * nb);
      if (pa2 != pb2) { bad = r; printf("  pow %ld want %ld (nb %d)\n", (long)pb2, (long)pa2, nb); }
    }
    printf("hfit %s %d\n", bad < 0 ? "ok" : "MISMATCH at fit", bad);
  }
  /* fft butterflies: every stage of a 256-point FFT on random data, both loop shapes */
  {
    int r, k, len = 0;
    for (bad = -1, r = 0; r < 24 && bad < 0; r++) {
      for (i = 0; i < 256; i++) {
        re[i] = re2[i] = (int32_t)(rnd32() >> 8) - ((int32_t)1 << 23);
        im[i] = im2[i] = (int32_t)(rnd32() >> 8) - ((int32_t)1 << 23);
      }
      if (r < 12) {                                          /* len 2, 4, 8: one twiddle, strided */
        int half, step;
        len = 2 << (r % 3), half = len >> 1, step = 512 / len;
        for (k = 0; k < half; k++) {
          uint16_t c = (uint16_t)fx_cos512[k * step], s = (uint16_t)fx_cos512[(k * step + 384) & 511];
          int32_t cs = (int32_t)(((uint32_t)c << 16) | s);
          c_fft_bfly1(re + k, im + k, re + k + half, im + k + half, 256 / len, len, cs);
          fx_fft_bfly1(re2 + k, im2 + k, re2 + k + half, im2 + k + half, 256 / len, len, cs);
        }
      } else {                                               /* len 16 .. 256: groups */
        int half, step, g;
        len = 16 << (r % 5), half = len >> 1, step = 512 / len;
        for (g = 0; g < 256; g += len) {
          c_fft_bfly_grp(re + g, im + g, re + g + half, im + g + half, half >> 1, step);
          fx_fft_bfly_grp(re2 + g, im2 + g, re2 + g + half, im2 + g + half, half >> 1, step);
        }
      }
      for (i = 0; i < 256 && bad < 0; i++)
        if (re[i] != re2[i] || im[i] != im2[i]) {
          bad = r;
          printf("  bfly %d: %ld %ld want %ld %ld (len %d)\n", i, (long)re2[i], (long)im2[i], (long)re[i], (long)im[i], len);
        }
    }
    printf("fft bfly %s %d\n", bad < 0 ? "ok" : "MISMATCH at round", bad);
  }
  /* div_q15_2: numerators and denominators of every bit length, both signs, the caps */
  {
    int r;
    for (bad = -1, r = 0; r < 400 && bad < 0; r++) {
      fx_acc d = rand40(1 + (int)(rnd32() % 39)), n1 = rand40((int)(rnd32() % 40)), n2 = rand40((int)(rnd32() % 40));
      int32_t qa1, qa2, qb1, qb2;
      if (r & 1) n1 = -n1;
      if (r & 2) n2 = -n2;
      if (r == 0) d = 1, n1 = ((fx_acc)1 << 39) - 1, n2 = -n1;
      if (r == 1) d = 1, n1 = 0, n2 = (fx_acc)1 << 31;
      if (r == 2) d = 65535, n1 = 65535, n2 = -65536;
      if (r == 3) d = 65536, n1 = ((fx_acc)1 << 39) - 1, n2 = 1;
      c_div_q15_2(n1, n2, d, &qa1, &qa2), fx_div_q15_2(n1, n2, d, &qb1, &qb2);
      if (qa1 != qb1 || qa2 != qb2) {
        bad = r;
        printf("  div d %ld: %ld %ld want %ld %ld (n >> 8: %ld %ld)\n", (long)d, (long)qb1, (long)qb2, (long)qa1, (long)qa2, (long)(n1 >> 8), (long)(n2 >> 8));
      }
    }
    printf("div_q15_2 %s %d\n", bad < 0 ? "ok" : "MISMATCH at", bad);
  }
  /* cb_resid: residuals of either sign up to 2^20 (and a few at the clamps), every step */
  {
    static const int32_t isteps[4] = {24899, 105341, 248066, 339202};
    int r;
    for (bad = -1, r = 0; r < 40 && bad < 0; r++) {
      int bits = r < 4 ? 24 : 8 + (int)(rnd32() % 13);
      fx_acc ra, rb;
      for (i = 0; i < 19; i++) {
        re[i] = (int32_t)(rnd32() >> (32 - bits)) - ((int32_t)1 << (bits - 1));
        im[i] = (int32_t)(rnd32() >> (32 - bits)) - ((int32_t)1 << (bits - 1));
      }
      ra = c_cb_resid(re, im, swa, 19, isteps[r & 3]), rb = fx_cb_resid(re, im, swb, 19, isteps[r & 3]);
      if (ra != rb) { bad = r; printf("  rr %ld want %ld\n", (long)rb, (long)ra); }
      for (i = 0; i < 19 && bad < 0; i++)
        if (swa[i] != swb[i]) { bad = r; printf("  r %d: %d want %d\n", i, swb[i], swa[i]); }
    }
    printf("cb_resid %s %d\n", bad < 0 ? "ok" : "MISMATCH at", bad);
  }
  /* oa_pair: 1..80 harmonics on bins 0..255, every n of the synthesis (0..511) */
  {
    int r, nb, n;
    int16_t *bin = swa, *ore = swa + 80, *oim = swb;
    for (bad = -1, r = 0; r < 60 && bad < 0; r++) {
      fx_acc a0, a1, b0, b1;
      nb = r < 2 ? 1 + 79 * r : 1 + (int)(rnd32() % 80);
      n = r < 4 ? 511 - r : (int)(rnd32() % 512);
      for (i = 0; i < nb; i++) {
        bin[i] = (int16_t)(rnd32() % 256);
        ore[i] = (int16_t)((int)(rnd32() % 32769) - 16384);
        oim[i] = (int16_t)((int)(rnd32() % 32769) - 16384);
      }
      a0 = c_oa_pair(bin, ore, oim, nb, n, &a1), b0 = fx_oa_pair(bin, ore, oim, nb, n, &b1);
      if (a0 != b0 || a1 != b1) {
        bad = r;
        printf("  oa n %d nb %d: %ld %ld want %ld %ld\n", n, nb, (long)(b0 >> 4), (long)(b1 >> 4), (long)(a0 >> 4), (long)(a1 >> 4));
      }
    }
    printf("oa_pair %s %d\n", bad < 0 ? "ok" : "MISMATCH at", bad);
  }
  /* mp_sums: rows of 63 random values against random high/low halves */
  {
    int r, k;
    int16_t *h = swa, *l = swa + 63, *row = swb;
    for (bad = -1, r = 0; r < 20 && bad < 0; r++) {
      fx_acc oa[4], ob[4];
      for (i = 0; i < 63; i++) {
        h[i] = (int16_t)((int)(rnd32() >> 16) - 32768);
        l[i] = (int16_t)(rnd32() & 0x7FFF);
        row[i] = (int16_t)((int)(rnd32() >> 16) - 32768);
      }
      c_mp_sums(h, l, row, 31, 32, oa), fx_mp_sums(h, l, row, 31, 32, ob);
      for (k = 0; k < 4; k++)
        if (oa[k] != ob[k]) { bad = r; printf("  mp %d: %ld want %ld\n", k, (long)ob[k], (long)oa[k]); }
    }
    printf("mp_sums %s %d\n", bad < 0 ? "ok" : "MISMATCH at", bad);
  }
  /* fft5 stage: five random 32-point spectra below 2^23 (the 32-point FFTs' output bound) */
  {
    int r;
    int32_t *sr = e2, *si = e2 + 256;
    for (bad = -1, r = 0; r < 12 && bad < 0; r++) {
      int bits = r < 6 ? 24 : 12;
      for (i = 0; i < 160; i++) {
        sr[i] = (int32_t)(rnd32() >> (32 - bits)) - ((int32_t)1 << (bits - 1));
        si[i] = (int32_t)(rnd32() >> (32 - bits)) - ((int32_t)1 << (bits - 1));
      }
      c_fft5_stage32(re, im, sr, si, 2), fx_fft5_stage32(re2, im2, sr, si, 2);
      for (i = 0; i < 160 && bad < 0; i++)
        if (re[i] != re2[i] || im[i] != im2[i]) {
          bad = r;
          printf("  fft5 %d: %ld %ld want %ld %ld\n", i, (long)re2[i], (long)im2[i], (long)re[i], (long)im[i]);
        }
    }
    printf("fft5 stage %s %d\n", bad < 0 ? "ok" : "MISMATCH at", bad);
  }
  printf("done\n");
  return 0;
}
