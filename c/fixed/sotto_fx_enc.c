/* sotto encoder in fixed point (integer only), for the C5517 DSP.
 *
 * Same API and, frame for frame, nearly the same bits as the float encoder in ../sotto.c
 * (sotto v3.6). New code, written from the float model's behaviour:
 *  - analysis every 10 ms: Codec 2's 279-point Hann window on a 320-sample buffer and a
 *    512-point FFT, block-normalised and stored as 16-bit complex bins with one exponent;
 *  - Codec 2's NLP pitch estimator (squared signal, DC notch, 600 Hz low-pass, decimate by
 *    5, 64-point Hann, DFT magnitudes, sub-multiple search), its two-stage harmonic-sum
 *    refinement and its MBE voicing decision, which give the tracker its starting pitch;
 *  - sotto's pitch and voicing tracker: candidates at x1, x2 and x1/2 of the neighbouring
 *    frames' pitch, harmonic fit costs, a sub-octave check and a fixed-lag (80 ms) Viterbi,
 *    with costs in natural-log units Q16;
 *  - tail unvoicing and mixed voicing from the harmonic SNR below 1 kHz;
 *  - on even frames, the rate-K envelope and the M-best search of the 8-bit codebook,
 *    and the energy measured on the envelope (v3.6).
 * Where float compares near-equal values (pitch near-ties, bins on a rounding edge) the
 * two can decide differently; tools/check_fx.py counts the frames that differ.
 * Each frame also carries the per-half features the v3.8 layer's rules read
 * (fx_enc_frame_t, sotto_fx_hiss.c): envelope mean and tilt, harmonic SNR, pitch, loudness.
 * SPDX-License-Identifier: MIT */
#define SOTTO_BUILD
#include <stdlib.h>
#include <string.h>
#include "../sotto.h"
#include "fx.h"
#include "fx_internal.h"
#include "fx_tables.h"

#define NSAMP 80
#define MPITCH 320
#define NB 257                 /* stored bins 0..256 of the 512-point spectrum */
#define K FX_K
#define NC FX_NC
#ifndef FX_LAG
#define FX_LAG 8               /* pitch tracker decision lag, 10 ms frames */
#endif
#define LAG FX_LAG
#ifdef SOTTO_PITCH_THIRD
#define N_MU 4                /* v3.7: also a third of frame t's own estimate */
#else
#define N_MU 3
#endif
#define NCAND 10
#define NST (NCAND + 1)
#define RING 16
#define TAIL_HOLD 30
#define MIXED 2
#define MBEST 8
#define MAXH 80
#define WO_MIN 26843546u      /* 2 pi / 160, Q32 turns */
#define WO_HI 268435456u      /* 2 pi / 16 */
#define WO_C2MAX 214748365u   /* 2 pi / 20, Codec 2's highest pitch */
#define DB(x) ((int32_t)((x) * 256L))
#define NAT(x) ((int32_t)((x) * 65536))   /* costs: natural log units, Q16 */
#define LN2_Q16 45426
#define LN_RAD (-1333188)     /* (ln(2 pi) - 32 ln 2), Q16: ln(Wo in radians) from Q32 turns */

#define HIST MPITCH           /* the analysis buffer */
#define SRING (LAG + 2)       /* spectra kept: frame t is decided after frame t + LAG + 1 is analysed */

typedef struct {
  int16_t sw[2 * NB];         /* spectrum, re/im interleaved, 16 bits */
  int16_t e;                  /* exponent: float spectrum = sw x 2^(e + fx_sw_l2 / 2^16) */
  uint32_t p[NB];             /* power per bin, normalised to 31 bits, for the pitch searches */
} spec_t;

typedef struct {
  uint32_t wo0;               /* Codec 2's Wo (NLP + two-stage refinement) */
  int16_t v0;                 /* Codec 2's voicing */
  int16_t nc;
  uint32_t cand[NCAND];
  int32_t lnc[NCAND];         /* ln(candidate Wo), Q16 */
  int32_t cost[NST];
  int32_t D[NST];             /* best path cost ending in each state, less the frame's minimum */
  unsigned char back[NST];
} frame_t;

struct sotto_enc {
  int16_t hist[HIST];         /* input; the last MPITCH samples are the analysis buffer */
  spec_t spec[2];             /* spectra of the last two frames, for the tracker */
  int32_t re[512], im[512];   /* FFT scratch */
  int32_t sre[SRING][NB], sim[SRING][NB];   /* bins 0..256 of the last SRING frames, for decide() */
  int16_t sexp[SRING];        /* their exponents (spectrum_fft's return value) */
  /* NLP */
  int32_t sq[MPITCH], mem_x, mem_y, fir[48];
  int16_t sh, blk[4];         /* NLP block floating point: values are true x 2^-sh */
  int16_t prev_bin;
  frame_t ring[RING];
  int32_t n_an, n_dec;
  int32_t ehist[TAIL_HOLD + 1];
  /* even half of the frame being built */
  uint32_t wo_e;
  int32_t en_e;
  int16_t idx_e[5], cls_e;
  int32_t c_e[NC];            /* even half's shape target, dB Q16 (fx_enc_frame_t.p.c) */
  int16_t shape_only;         /* skip the shape search (sotto_enc_set_shape_only) */
  fx_enc_frame_t cur;         /* the frame being built: per-half features for v3.8 */
  int32_t peak;               /* causal loudness reference, dB Q16 (hiss_band._onsets) */
  fx_enc_frame_t q[8];
  int16_t q_head, q_n, flushed;
};

#define R(e, t) (&(e)->ring[(t) % RING])

static int32_t ln_q16(fx_uacc x) { return (int32_t)((fx_mul32(fx_log2_acc(x), LN2_Q16) + 32768) >> 16); }
static int32_t ln_wo(uint32_t wo) { return ln_q16(wo) + LN_RAD; }
/* 2^48 / x, rounded: pitch period Q16 <-> Wo Q32 turns */
static uint32_t recip48_(uint32_t x);
static uint32_t recip48(uint32_t x) {
  uint32_t r;
  FX_PROF(38, 1);
  r = recip48_(x);
  FX_PROF(38, 0);
  return r;
}
static int bitlen32(uint32_t x);
/* floor((2^32 - 1) / (xn + 1)) for xn in [2^15, 2^16) without a division: a table seed
   y0 ~ 2^30 / (xn + 1), one Newton step to about 17 bits, then the exact remainder, which
   puts y right with at most one step (checked for every xn). */
static uint32_t rcp17(uint32_t xn) {
  int16_t y0 = fx_rcp_tab[(xn >> 7) & 255];                 /* [2^14, 2^15) */
  int16_t xl = (int16_t)(xn - 32768);                       /* xn + 1 = xl + 2^15 + 1, xl in [0, 2^15) */
  int32_t e = ((int32_t)1 << 30) - ((int32_t)xl * y0 + ((int32_t)y0 << 15) + y0);   /* 2^30 - (xn + 1) y0, |e| < 2^22 */
  int16_t eh = (int16_t)(e >> 7);
  uint32_t y = ((uint32_t)y0 << 2) + (uint32_t)(((int32_t)y0 * eh) >> 21);          /* y0 (4 + 4 e / 2^30) */
  fx_acc r = 0xFFFFFFFFll - (fx_mul32x16((int32_t)y, xl) + ((fx_acc)y << 15) + y);   /* 2^32 - 1 - y (xn + 1) */
  uint32_t d = xn + 1;
  while (r < 0) y--, r += d;
  while (r >= d) y++, r -= d;
  return y;
}
/* round(2^48 / x) for x in [2^20, 2^28): a 17-bit reciprocal of the top 16 bits, a
   multiply for the next 14 bits, then the exact remainder (mod 2^32, |r| < 2^31): at most
   one correction step for every such x (checked exhaustively against the 64-bit division).
   On the C5517 the same steps run in c55x/recip48.asm with 16 x 16-bit products (here the
   32 x 32-bit products are library calls); this C version is its check in c55x/kernels.c. */
#if defined(__TMS320C55X__) && !defined(FX_RECIP_C)
uint32_t fx_recip48(uint32_t x);
#else
#define fx_recip48 fx_recip48_c
#endif
uint32_t fx_recip48_c(uint32_t x) {
  int n = bitlen32(x);                                      /* 21..28 */
  uint32_t xn, y, r, q;
  xn = x >> (n - 16);
  y = rcp17(xn);
  r = 0u - y * x;                                           /* 2^(16+n) - y x, < 2^(n+2) */
  q = (y << (32 - n)) + (((r >> (n - 13)) * y) >> (n - 3));
  r = 0u - q * x;                                           /* 2^48 - q x */
  while ((int32_t)r < 0) q--, r += x;
  while (r >= x) q++, r -= x;
  return q + (r + (x >> 1) >= x);
}
static uint32_t recip48_(uint32_t x) {
  if (x < ((uint32_t)1 << 20) || x >= ((uint32_t)1 << 28)) {
    fx_uacc r40, q40 = fx_udiv_shl(1, 48, x, &r40);
    return (uint32_t)q40 + (r40 + (x >> 1) >= x);
  }
  return fx_recip48(x);
}

/* power of bin b (0..511) of frame f's 16-bit spectrum */
static inline fx_acc pw(const spec_t *f, int b) {
  if (b > 256) b = 512 - b;
  return FX_P16(f->sw[2 * b], f->sw[2 * b]) + FX_P16(f->sw[2 * b + 1], f->sw[2 * b + 1]);
}

/* Codec 2's harmonic count: floor(pi / Wo), less one when the last harmonic is at 0.95 pi or above */
static int n_harm(uint32_t wo) {
  int L = (int)(0x80000000u / wo);
  if ((fx_uacc)L * wo >= 2040109466u) L--;
  return L;
}

/* ceil((2l - 1) Wo 256 / 2^32) style bin bounds: ceil(x / 2^sh) */
static int ceil_sh(fx_uacc x, int sh) { return (int)((x + ((fx_uacc)1 << sh) - 1) >> sh); }

/* ---------------------------------------------------------------- analysis */

/* power of a bin rounded right by t bits */
static fx_acc sq2(int32_t re, int32_t im, int t) {
  int32_t a = t ? fx_rshr32(re, t) : re, b = t ? fx_rshr32(im, t) : im;
  return fx_sq32(a) + fx_sq32(b);
}

/* Codec 2's windowed 512-point spectrum of the 320 samples at buf: bins 0..256 in e->re,
   e->im (int32, float spectrum = re x 2^(return value + fx_sw_l2 / 2^16)) */
static int spectrum_fft(struct sotto_enc *e, const int16_t *buf) {
  int i;
  int32_t *re = e->re, *im = e->im;
  int32_t x[278];
  uint32_t mx = 0;
  for (i = 0; i < 278; i++) {
    x[i] = (int32_t)buf[21 + i] * fx_hann[i];                /* Q15 */
    uint32_t a = (uint32_t)(x[i] < 0 ? -x[i] : x[i]);
    if (a > mx) mx = a;
  }
  memset(re, 0, sizeof e->re);
  int s = 0;                                                  /* x * 2^-s, largest in [2^22, 2^23) */
  if (mx) {
    while ((mx >> s) >= ((uint32_t)1 << 23)) s++;
    if (s == 0)
      while ((mx << (1 - s)) < ((uint32_t)1 << 23)) s--;
  }
  /* Codec 2 centres the window on the time origin: samples 160..298 at 0.., 21..159 at the end */
  for (i = 0; i < 278; i++) {
    int n = 21 + i;                                           /* buffer index */
    re[n >= 160 ? n - 160 : 512 - 160 + n] = s > 0 ? fx_rshr32(x[i], s) : (int32_t)((uint32_t)x[i] << -s);
  }
  FX_PROF(15, 1);
  fx_rfft_pack(re, im, 256);                                  /* real input: a 256-point FFT */
  fx_fft(re, im, 256);
  fx_rfft_split(re, im, 256, fx_cos512, 512);                 /* bins 0..256, DFT / 512 */
  FX_PROF(15, 0);
  return s;
}

/* the 16-bit copy of a spectrum (bins re, im with exponent s, as spectrum_fft leaves them) in *f */
static void spectrum_bins(const int32_t *re, const int32_t *im, int s, spec_t *f) {
  uint32_t m2 = fx_bins_max(re, im, NB);
  int s2 = 0, t = 0, sp = 0;
  if (m2) {
    while ((m2 >> s2) > 32767) s2++;
    if (s2 == 0)
      while (s2 > -15 && (m2 << (1 - s2)) <= 32767) s2--;
  }
  fx_bins_to16(re, im, f->sw, NB, s2);
  f->e = (int16_t)(s + s2);
  /* power per bin at 31 bits, from the bins rounded to 19 bits (so a power is < 2^39) */
  while ((m2 >> t) >= ((uint32_t)1 << 19) - 1) t++;        /* rounding may add one */
  fx_acc pm = fx_bins_pow_max(re, im, NB, t);
  while ((pm >> sp) >= ((fx_acc)1 << 31)) sp++;
  fx_bins_pow(re, im, f->p, NB, t, sp, sp ? (uint32_t)1 << (sp - 1) : 0);
}

/* Codec 2's NLP: the pitch bin (3.125 Hz steps) of the 10 ms just added to the input */
static int msb(uint32_t x) { int n = -1; while (x) { x >>= 1; n++; } return n; }
static uint32_t uabs(int32_t x) { return (uint32_t)(x < 0 ? -x : x); }
static inline int32_t shift(int32_t x, int s) { return s > 0 ? fx_rshr32(x, s) : (int32_t)((uint32_t)x << -s); }

/* rr[k], qq[k] = the 512-point DFT's cos and sin sums of the 64 samples x at bins k0 <= k < k1,
   rounded right by 15 (c55x/nlp_dft.asm on the C5517: circular addressing, dual MAC) */
#if defined(__TMS320C55X__) && !defined(FX_NLP_DFT_C)
void fx_nlp_dft(const int16_t *x, const int16_t *cos512, int32_t *rr, int32_t *qq, int k0, int k1);
#elif defined(FX_ARM_DSP) && !defined(FX_NLP_DFT_C)
/* x[n] in both halves against cos | sin (fx_cs512): SMLALD sums r + q, SMLSLD r - q */
static void fx_nlp_dft(const int16_t *x, const int16_t *cos512, int32_t *rr, int32_t *qq, int k0, int k1) {
  const uint32_t *cs = fx_cs512();
  uint32_t xx[64];
  int k, n;
  (void)cos512;
  for (n = 0; n < 64; n++) xx[n] = (uint32_t)(uint16_t)x[n] * 0x10001u;
  for (k = k0; k < k1; k++) {
    fx_acc sum = 0, dif = 0;
    unsigned i = 0;
    for (n = 0; n < 64; n++, i = (i + (unsigned)k) & 511u) {
      sum = fx_smlald(xx[n], cs[i], sum);
      dif = fx_smlsld(xx[n], cs[i], dif);
    }
    rr[k] = fx_rshr((sum + dif) >> 1, 15), qq[k] = fx_rshr((sum - dif) >> 1, 15);
  }
}
#else
static void fx_nlp_dft(const int16_t *x, const int16_t *cos512, int32_t *rr, int32_t *qq, int k0, int k1) {
  int k, n;
  for (k = k0; k < k1; k++) {
    fx_acc r = 0, q = 0;
    for (n = 0; n < 64; n++) {
      int i = (k * n) & 511;
      r += FX_P16(x[n], cos512[i]);
      q += FX_P16(x[n], cos512[(i - 128) & 511]);
    }
    FX_ACC_CHECK(r); FX_ACC_CHECK(q);
    rr[k] = fx_rshr(r, 15), qq[k] = fx_rshr(q, 15);
  }
}
#endif

static int nlp_bin(struct sotto_enc *e) {
  int i, j, b, k, mult;
  /* squared input; choose this block's exponent so it and the filter memories stay below
     2^22, and rescale the memories to it */
  uint32_t sq2[NSAMP], mx = 0, mm = uabs(e->mem_x) | uabs(e->mem_y);
  for (i = 0; i < NSAMP; i++) {
    int32_t v = e->hist[HIST - NSAMP + i];
    sq2[i] = (uint32_t)(v * v);
    mx |= sq2[i];
  }
  for (j = 0; j < 48; j++) mm |= uabs(e->fir[j]);
  int sh = msb(mx) - 21, shm = mm ? e->sh + msb(mm) - 21 : -99;
  if (shm > sh) sh = shm;
  if (sh < -8) sh = -8;
  int d = sh - e->sh;
  e->mem_x = shift(e->mem_x, d);
  e->mem_y = shift(e->mem_y, d);
  for (j = 0; j < 48; j++) e->fir[j] = shift(e->fir[j], d);
  e->sh = (int16_t)sh;
  e->blk[3] = (int16_t)sh;
  int32_t one = shift(1, sh);                                 /* Codec 2 adds 1.0 after the notch */
  FX_PROF(29, 1);
  {
    /* the filter input as one run: z[0..46] = the last 47 inputs, z[47 + i] = this block's;
       sample i's 48 taps are z[i..i+47]. Split once into 16-bit halves (z = hi 2^15 + lo),
       so each tap is two 16 x 16 multiply-accumulates over memory in order. */
    int16_t zh[47 + NSAMP], zl[47 + NSAMP];
    for (j = 0; j < 47; j++) zh[j] = FX_HI(e->fir[j + 1]), zl[j] = FX_LO(e->fir[j + 1]);
    for (i = 0; i < NSAMP; i++) {
      int32_t x = shift(sq2[i], sh);
      int32_t y = x - e->mem_x + fx_rshr(fx_mul32x16(e->mem_y, 31130), 15);   /* DC notch, pole 0.95 */
      e->mem_x = x;
      e->mem_y = y;
      if (i >= NSAMP - 48) e->fir[i - (NSAMP - 48)] = y + one;   /* the last 48 inputs */
      zh[47 + i] = FX_HI(y + one), zl[47 + i] = FX_LO(y + one);
    }
    for (i = 0; i < NSAMP; i++) {
      const int16_t *h = zh + i, *l = zl + i;
      fx_acc a = 0, al = 0;
      for (j = 0; j < 48; j++) a += FX_P16(h[j], fx_nlp_fir[j]);
      for (j = 0; j < 48; j++) al += FX_P16(l[j], fx_nlp_fir[j]);
      a = FX_JOIN(a, al);
      FX_ACC_CHECK(a);
      e->sq[MPITCH - NSAMP + i] = fx_rshr(a, 15);
    }
  }
  FX_PROF(29, 0);
  /* decimate by 5 (aligning the four blocks' exponents), window, normalise to 15 bits */
  int shx = e->blk[0];
  for (b = 1; b < 4; b++)
    if (e->blk[b] > shx) shx = e->blk[b];
  int32_t xd[64];
  mx = 0;
  for (i = 0; i < 64; i++) {
    int32_t v = shift(e->sq[5 * i], shx - e->blk[(5 * i) / NSAMP]);
    xd[i] = fx_rshr(fx_mul32x16(v, fx_nlp_win[i]), 15);
    mx |= uabs(xd[i]);
  }
  int s = 0;
  while ((mx >> s) >= ((uint32_t)1 << 14)) s++;
  int16_t x16[64];
  for (i = 0; i < 64; i++) x16[i] = (int16_t)(s ? fx_rshr32(xd[i], s) : xd[i]);
  /* DFT magnitudes at bins 15..129 of 512 */
  FX_PROF(30, 1);
  fx_acc P[130];                                          /* entries 15..129 used */
  int32_t rr[130], qq[130];
  uint32_t pmx = 0;
  fx_nlp_dft(x16, fx_cos512, rr, qq, 15, 130);
  for (k = 15; k < 130; k++) pmx |= uabs(rr[k]) | uabs(qq[k]);
  FX_PROF(30, 0);
  /* magnitudes below 2^16, so powers are < 2^33 and 20 x a power fits in 40 bits */
  int tp = 0;
  while ((pmx >> tp) >= 65535) tp++;
  for (k = 15; k < 130; k++) {
    int32_t a = tp ? fx_rshr32(rr[k], tp) : rr[k], c = tp ? fx_rshr32(qq[k], tp) : qq[k];
    P[k] = fx_sq32(a) + fx_sq32(c);
  }
  fx_acc gmax = 0;
  int gbin = 16;
  for (k = 16; k <= 128; k++)
    if (P[k] > gmax) gmax = P[k], gbin = k;
  /* sub-multiples: prefer a lower pitch peak that is a local maximum above a threshold,
     a lower threshold near the previous frame's estimate */
  int cbin = gbin;
  for (mult = 2; gbin / mult >= 16; mult++) {
    int b = gbin / mult, bmin = fx_nlp_bmin[b], bmax = fx_nlp_bmax[b];
    if (bmin < 16) bmin = 16;
    int near = e->prev_bin > bmin && e->prev_bin < bmax;
    fx_acc lmax = 0;
    int lbin = bmin;
    for (b = bmin; b <= bmax; b++)
      if (P[b] > lmax) lmax = P[b], lbin = b;
    /* lmax > 0.3 gmax, or 0.15 gmax near the previous estimate */
    if ((near ? 20 * lmax > 3 * gmax : 10 * lmax > 3 * gmax) && lmax > P[lbin - 1] && lmax > P[lbin + 1])
      cbin = lbin;
  }
  e->prev_bin = (int16_t)cbin;
  memmove(e->sq, e->sq + NSAMP, (MPITCH - NSAMP) * sizeof(int32_t));
  memmove(e->blk, e->blk + 1, 3 * sizeof(int16_t));
  return cbin;
}

/* Codec 2's harmonic-sum refinement over pitch periods p0 - span .. p0 + span (Q16) */
static uint32_t hs_refine(const spec_t *f, uint32_t p0, uint32_t span, uint32_t step, uint32_t wo) {
  uint32_t p;
  int m;
  int L = (int)(0x80000000u / wo);
  fx_acc em = 0;
  uint32_t wom = wo;
  for (p = p0 - span; p <= p0 + span; p += step) {
    uint32_t w = recip48(p);
    fx_acc E = 0;
    uint32_t acc = w + ((uint32_t)1 << 22);                /* m w + 1/2 bin, < 2^32 for m <= L */
    for (m = 1; m <= L; m++, acc += w) {
      int b = (int)(acc >> 23);
      E += f->p[b > 256 ? 512 - b : b];
    }
    FX_ACC_CHECK(E);
    if (E > em) em = E, wom = w;
  }
  return wom;
}

/* bit length of x (0 for 0) */
static int bitlen32(uint32_t x) {
  int n = 0;
  if (x >> 16) n += 16, x >>= 16;
  if (x >> 8) n += 8, x >>= 8;
  if (x >> 4) n += 4, x >>= 4;
  if (x >> 2) n += 2, x >>= 2;
  if (x >> 1) n += 1, x >>= 1;
  return n + (int)x;
}
/* bit length of a 40-bit x >= 0 */
static int bitlen40(fx_uacc x) {
  uint32_t h = (uint32_t)(x >> 8);
  return h ? 8 + bitlen32(h) : bitlen32((uint32_t)x);
}
/* floor(x / 2^m) of a 40-bit x for 0 <= m, saturated to 32 bits, with 32-bit shifts only (a
   variable shift of a 40-bit value is a library call on the C55x) */
static uint32_t ushr40_sat(fx_uacc x, int m) {
  uint32_t h = (uint32_t)(x >> 8), lo = (uint32_t)x & 0xFF;
  if (m >= 8) return m >= 40 ? 0 : h >> (m - 8);
  if (h >> (24 + m)) return 0xFFFFFFFFu;                   /* >= 2^32 after the shift */
  return (h << (8 - m)) | (lo >> m);
}

/* n 2^15 / d for d > 0, truncated toward zero, with d rounded down to 16 bits: about 15
   significant bits, plenty for a least-squares amplitude (the residual is flat around its
   minimum), and one 32-bit division instead of several 40-bit ones. Two numerators over the
   same d (the real and imaginary parts), so d is normalised once. */
#if defined(__TMS320C55X__) && !defined(FX_DIVQ_C)
#define div_q15_2 fx_div_q15_2                              /* c55x/divq15.asm */
#else
static void div_q15_2(fx_acc n1, fx_acc n2, fx_acc d, int32_t *q1, int32_t *q2) {
  int sd = bitlen40((fx_uacc)d) - 16, sh, i;
  if (sd < 0) sd = 0;                                      /* d >> sd in [2^15, 2^16), or d < 2^16 */
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
#endif

/* least-squares fit of the window response to bins [al, bl) around harmonic l:
   returns the residual power, and the band power in *sig */
static fx_acc harm_fit_(const spec_t *f, uint32_t wo, int l, fx_acc *sig);
static fx_acc harm_fit(const spec_t *f, uint32_t wo, int l, fx_acc *sig) {
  fx_acc r;
  FX_PROF(35, 1);
  r = harm_fit_(f, wo, l, sig);
  FX_PROF(35, 0);
  return r;
}
static fx_acc harm_fit_(const spec_t *f, uint32_t wo, int l, fx_acc *sig) {
  uint32_t lw = (uint32_t)fx_mul32x16((int32_t)wo, (int16_t)l);   /* < 2^31 for the harmonics fitted */
  int al = ceil_sh((fx_uacc)lw * 2 - wo, 24), bl = ceil_sh((fx_uacc)lw * 2 + wo, 24), nb = bl - al;
  int off = (int)((((fx_uacc)256 << 23) + ((fx_uacc)1 << 22) - lw) >> 23);
  fx_acc s = 0, err = 0;
  if (nb > 0) {
    const int16_t *sw = f->sw + 2 * al, *w = fx_W + off + al;
    fx_acc sums[3];                                         /* nr, ni, den */
    fx_hfit_sums(sw, w, nb, sums);
    FX_ACC_CHECK(sums[0]); FX_ACC_CHECK(sums[1]); FX_ACC_CHECK(sums[2]);
    if (sums[2] > 0) {
      int32_t ar, ai;
      div_q15_2(sums[0], sums[1], sums[2], &ar, &ai);       /* Q15 */
      err = fx_hfit_err(sw, w, nb, ar, ai);
      if (sig) {
        int b2 = bl < NB ? bl : NB, i;                      /* bins past 256 mirror (pw) */
        if (b2 > al) s = fx_hfit_pow(sw, 2 * (b2 - al));
        for (i = b2 > al ? b2 : al; i < bl; i++) s += pw(f, i);
      }
      FX_ACC_CHECK(err); FX_ACC_CHECK(s);
    }
  }
  if (sig) *sig = s;
  return err;
}

/* a ka > b kb for a, b >= 0 and 0 < ka, kb < 2^18: both sides shifted to 21 bits first
   when they are larger, so the products stay inside 40 bits */
static int gt_mul(fx_acc a, int32_t ka, fx_acc b, int32_t kb) {
  fx_acc m = a > b ? a : b;
  int s = 0;
  while ((m >> s) >= ((fx_acc)1 << 21)) s++;
  return (a >> s) * ka > (b >> s) * kb;
}

/* Codec 2's MBE voicing decision at its refined Wo */
static int c2_voicing(const spec_t *f, uint32_t wo) {
  int m, i, l;
  int L = n_harm(wo);
  fx_acc A2[MAXH + 1];
  fx_uacc edge = (fx_uacc)wo + ((fx_uacc)1 << 23);          /* (2m - 1) wo + 1/2 bin */
  for (m = 1; m <= L; m++) {
    int am = (int)(edge >> 24), bm = (int)((edge + 2 * (fx_uacc)wo) >> 24);
    fx_acc p = 0;
    for (i = am; i < bm; i++) p += pw(f, i);
    A2[m] = p;
    edge += 2 * (fx_uacc)wo;
  }
  int l1 = L / 4;
  fx_acc sig = 0, err = 0;
  for (l = 1; l <= l1; l++) {
    sig += A2[l];
    err += harm_fit(f, wo, l, NULL);
  }
  int v = gt_mul(sig, 65536, err, 260904);                  /* SNR > 6 dB */
  fx_acc lo = 0, hi = 0;
  for (l = 1; l <= L / 2; l++) lo += A2[l];
  for (l = L / 2; l <= L; l++) hi += A2[l];
  if (!v && gt_mul(lo, 1, hi, 10)) v = 1;                   /* low/high ratio > 10 dB */
  if (v) {
    if (gt_mul(hi, 1, lo, 10)) v = 0;                       /* < -10 dB */
    if (gt_mul(hi, 65536, lo, 164619) && wo <= 32212254u) v = 0;   /* < -4 dB at a pitch of 60 Hz or less */
  }
  return v;
}

/* ---------------------------------------------------------------- tracker */

/* sum of p[(acc + k w) >> 23] for k < m, every index below 256 (c55x/hsum.asm on the C5517) */
#if defined(__TMS320C55X__) && !defined(FX_HSUM_C)
fx_acc fx_hsum(const uint32_t *p, uint32_t acc, uint32_t w, int m);
#else
static fx_acc fx_hsum(const uint32_t *p, uint32_t acc, uint32_t w, int m) {
  fx_acc E = 0;
  for (; m > 0; m--, acc += w) E += p[(int)(acc >> 23)];
  return E;
}
#endif

/* sotto's two-stage refinement (sinusoid.refine), clamped to [WO_MIN, wo_max] */
static uint32_t refine(const spec_t *f, uint32_t wo, uint32_t wo_max) {
  int s, i;
  static const uint32_t span[2] = {(uint32_t)5 << 16, (uint32_t)1 << 16}, step[2] = {(uint32_t)1 << 16, (uint32_t)1 << 14};
  static const int np[2] = {11, 9};
  uint32_t p0 = recip48(wo);
  for (s = 0; s < 2; s++) {
    int L = (int)(p0 >> 17);                                /* floor(pi / Wo) */
    fx_acc best = -1;
    uint32_t pbest = p0;
    for (i = 0; i < np[s]; i++) {
      uint32_t p = p0 - span[s] + (uint32_t)i * step[s], w;
      FX_PROF(14, 1);
      w = recip48(p);
      FX_PROF(14, 0);
      int mx = L;                                           /* bins < 256 for m <= mx */
      while (mx > 0 && (uint32_t)mx * w >= ((uint32_t)256 << 23) - ((uint32_t)1 << 22)) mx--;
      fx_acc E = fx_hsum(f->p, w + ((uint32_t)1 << 22), w, mx);   /* m w + 1/2 bin: round(m w / 2^23) */
      FX_ACC_CHECK(E);
      if (E > best) best = E, pbest = p;
    }
    p0 = pbest;
  }
  uint32_t w = recip48(p0);
  return w < WO_MIN ? WO_MIN : w > wo_max ? wo_max : w;
}

/* sinusoid.fit_error, as a natural log, Q16 */
static int32_t ln_fit_error(const spec_t *f, uint32_t wo) {
  int l, i;
  int L = (int)(1342177280u / wo);                          /* 2500 Hz / f0 */
  if (L < 1) L = 1;
  int hi = ceil_sh((fx_uacc)(2 * L + 1) * wo, 24);
  fx_acc err = 0, en = 0;
  for (l = 1; l <= L; l++) err += harm_fit(f, wo, l, NULL);
  for (i = 1; i < hi; i++) en += pw(f, i);
  if (en == 0) return 0;
  return ln_q16((fx_uacc)(err > 0 ? err : 1)) - ln_q16((fx_uacc)en);
}

/* sinusoid.odd_even_db, dB Q8 */
static int32_t odd_even_db(const spec_t *f, uint32_t wo) {
  int j, i;
  int h = (int)(wo >> 25);
  if (h < 1) h = 1;
  int L = (int)(805306368u / wo);                           /* 1500 Hz / f0 */
  fx_acc odd = 0, even = 0;
  uint32_t acc = 3 * wo;                                    /* j wo < 2^31 for j <= L */
  for (j = 3; j <= L; j++, acc += wo) {
    int c = (int)(acc >> 23);
    fx_acc p = 0;
    if (c < 256)
      for (i = c - h > 1 ? c - h : 1; i <= c + h; i++) {
        uint32_t q = f->p[i];                               /* c <= 1500 Hz, i < 105 */
        if (q > p) p = q;
      }
    if (j % 2) odd += p; else even += p;
  }
  return fx_log2_to_db_pow(fx_log2_acc((fx_uacc)(odd > 0 ? odd : 1)) - fx_log2_acc((fx_uacc)(even > 0 ? even : 1)));
}

/* candidates and costs of frame t */
static void frame_costs(struct sotto_enc *e, int32_t t, int has_next) {
  int i, j, c;
  frame_t *f = R(e, t);
  const spec_t *sp = &e->spec[t & 1];
  static const int dd[3] = {0, -1, 1};
  f->nc = 0;
  for (i = 0; i < 3; i++) {
    int32_t u = t + dd[i];
    if (u < 0 || (dd[i] == 1 && !has_next)) continue;
    uint32_t base = R(e, u)->wo0;
    for (j = 0; j < (dd[i] == 0 ? N_MU : 3); j++) {
      if (j == 3 && base < 112742892u) continue;              /* base / 3 below 70 Hz */
      uint32_t w0 = j == 0 ? base : j == 1 ? base * 2 : j == 2 ? (base + 1) >> 1 : (base + 1) / 3;   /* base < 2^28 */
      uint32_t w0c = w0 < WO_MIN ? WO_MIN : w0 > WO_HI ? WO_HI : (uint32_t)w0;
      FX_PROF(8, 1);
      uint32_t w = refine(sp, w0c, WO_HI);
      FX_PROF(8, 0);
      if (w >= 37580964u || (dd[i] == 0 && j == 0)) {        /* 70 Hz */
        int dup = 0;
        for (c = 0; c < f->nc; c++) {                    /* |ln(w / cand)| <= 0.01: */
          uint32_t a = f->cand[c], lo = a < w ? a : w, dw = a < w ? w - a : a - w;
          /* w 10^6 <= a 1010050 and a 10^6 <= w 1010050, exactly, inside 40 bits */
          if (dw <= lo >> 6 && (fx_uacc)dw * 20000 <= (fx_uacc)lo * 201) dup = 1;
        }
        if (!dup) f->cand[f->nc++] = w;
      }
    }
  }
  f->cost[0] = NAT(-0.5) + NAT(1.0) * (f->v0 > 0);
  for (c = 0; c < f->nc; c++) {
    uint32_t w = f->cand[c];
    f->lnc[c] = ln_wo(w);
    FX_PROF(9, 1);
    int32_t cst = ln_fit_error(sp, w) - f->lnc[c] / 2 + NAT(1.0) * (f->v0 == 0);
    FX_PROF(9, 0);
    FX_PROF(13, 1);
    int32_t oe = odd_even_db(sp, w);
    FX_PROF(13, 0);
    if (-oe - DB(10) > 0) cst += (int32_t)(((fx_acc)(-oe - DB(10)) * 512) / 10);   /* 2 (-oe - 10) / 10 */
    f->cost[1 + c] = cst;
  }
}

static void viterbi_step(struct sotto_enc *e, int32_t t) {
  int j, i;
  frame_t *f = R(e, t);
  int n = f->nc + 1;
  if (t == 0) {
    for (j = 0; j < n; j++) f->D[j] = f->cost[j], f->back[j] = 0;
  } else {
    frame_t *p = R(e, t - 1);
    int np = p->nc + 1;
    for (j = 0; j < n; j++) {
      int32_t best = 0;
      int bi = 0;
      for (i = 0; i < np; i++) {
        int32_t tr;
        if (i == 0 && j == 0) tr = 0;
        else if (i == 0 || j == 0) tr = NAT(1.5);
        else {
          int32_t lj = p->lnc[i - 1] - f->lnc[j - 1], x;
          if (lj < 0) lj = -lj;
          x = lj - 15142;                                    /* ln 2 / 3 */
          tr = 3 * lj + 4 * (x > 0 ? x : 0);
        }
        int32_t tot = p->D[i] + tr;
        if (i == 0 || tot < best) best = tot, bi = i;
      }
      f->D[j] = best + f->cost[j];
      f->back[j] = (unsigned char)bi;
    }
  }
  int32_t mn = f->D[0];                                      /* keep D small: argmins don't change */
  for (j = 1; j < n; j++)
    if (f->D[j] < mn) mn = f->D[j];
  for (j = 0; j < n; j++) f->D[j] -= mn;
}

/* ---------------------------------------------------------------- frame decision */

static void put_bits(unsigned char *out, int *pos, int v, int nb) {
  int i;
  for (i = nb - 1; i >= 0; i--, (*pos)++)
    if ((v >> i) & 1) out[*pos >> 3] |= 0x80 >> (*pos & 7);
}

#ifdef FX_CB_UNPACKED
/* h[v] = 32 e2[v] - r.y_v for the n vectors of an unpacked codebook (FX_CBW values each,
   r padded to FX_CBW with 0); c55x/cb_dots.asm on the C5517 */
#if defined(__TMS320C55X__) && !defined(FX_CB_DOTS_C)
void fx_cb_dots(const int16_t *r, const int16_t *cb, int n, const int32_t *e2, int32_t *h);
#elif defined(FX_ARM_DSP) && !defined(FX_CB_DOTS_C)
/* Cortex-M4: SMLAD, two products a cycle, on value pairs (FX_CBW is even), two vectors at a
   time so each pair of r is loaded once (n is even); the same 32-bit sums */
static void fx_cb_dots(const int16_t *r, const int16_t *cb, int n, const int32_t *e2, int32_t *h) {
  uint32_t rp[FX_CBW / 2], y0, y1, x;
  int v, k;
  memcpy(rp, r, sizeof rp);
  for (v = 0; v < n; v += 2, cb += 2 * FX_CBW) {
    int32_t a0 = 0, a1 = 0;
    for (k = 0; k < FX_CBW / 2; k++) {
      x = rp[k];
      memcpy(&y0, cb + 2 * k, 4);
      memcpy(&y1, cb + FX_CBW + 2 * k, 4);
      a0 = fx_smlad(x, y0, a0);
      a1 = fx_smlad(x, y1, a1);
    }
    h[v] = e2[v] * 32 - a0;
    h[v + 1] = e2[v + 1] * 32 - a1;
  }
}
#else
static void fx_cb_dots(const int16_t *r, const int16_t *cb, int n, const int32_t *e2, int32_t *h) {
  int v, k;
  for (v = 0; v < n; v++, cb += FX_CBW) {
    int32_t ry = 0;
    for (k = 0; k < FX_CBW; k++) ry += (int32_t)r[k] * cb[k];
    h[v] = e2[v] * 32 - ry;
  }
}
#endif
#endif

/* the largest h with rr + 128 h < worst, clamped to 32 bits */
static int32_t h_limit(fx_acc worst, fx_acc rr) {
  fx_acc t = (worst - rr - 1) >> 7;                          /* floor */
  return t > INT32_MAX ? INT32_MAX : t < INT32_MIN ? INT32_MIN : (int32_t)t;
}

/* M-best multistage search over the first nst stages; c in dB Q16; hv: scratch for 512 values */
typedef char fx_mbest_matches[FX_MBEST == MBEST ? 1 : -1];
int fx_msvq_paths(const int32_t *c, int nst, int16_t path[MBEST][5], int32_t recon[MBEST][NC], int32_t *hv) {
  int s, p, k, v, i;
  int np = 1;
  memset(recon, 0, sizeof(int32_t) * MBEST * NC);
  memset(path, 0, sizeof(int16_t) * MBEST * 5);
  for (s = 0; s < nst; s++) {
    int n = 1 << fx_cb_bits[s], nk = 0;
    const int16_t *cb = fx_cb[s];
    const int32_t *e2 = fx_cb_e2[s];
    fx_acc kd[MBEST];
    int kp[MBEST], kc[MBEST];
    for (p = 0; p < np; p++) {
      int16_t r[NC + 1];                                     /* residual in codebook steps, Q6 */
      fx_acc rr = fx_cb_resid(c, recon[p], r, NC, fx_cb_istep[s]);
      int32_t hthr;
      r[NC] = 0;                                             /* the padding value */
      FX_ACC_CHECK(rr);
      /* d = sum (r - 64 y)^2 = rr - 128 r.y + 4096 |y|^2 = rr + 128 h, h = 32 |y|^2 - r.y,
         exactly. The inner loop is one 16 x 16 multiply-accumulate per value, and h fits in
         32 bits (r.y < 2^27, 32 |y|^2 < 2^24), so most vectors are rejected with a 32-bit
         compare against hthr, the largest h that could still enter the list */
      hthr = nk < MBEST ? INT32_MAX : h_limit(kd[nk - 1], rr);
      /* list insertion for vector v with h = 32 |y|^2 - r.y, in increasing v */
#define CB_CONSIDER(vv, hh) do { \
        int32_t h_ = (hh); \
        if (h_ <= hthr) { \
          fx_acc d = rr + (fx_acc)h_ * 128;                   /* < 2^36 */ \
          FX_ACC_CHECK(d); \
          if (nk < MBEST || d < kd[nk - 1]) { \
            int i = nk < MBEST ? nk++ : MBEST - 1; \
            while (i > 0 && kd[i - 1] > d) kd[i] = kd[i - 1], kp[i] = kp[i - 1], kc[i] = kc[i - 1], i--; \
            kd[i] = d, kp[i] = p, kc[i] = (vv); \
            if (nk == MBEST) hthr = h_limit(kd[nk - 1], rr); \
          } \
        } \
      } while (0)
#ifdef FX_CB_UNPACKED
      /* every vector's h first (on the C55x an assembly loop with one dual multiply-
         accumulate per value pair), then the list in increasing v, as before */
      FX_PROF(28, 1);
      fx_cb_dots(r, cb, n, e2, hv);
      FX_PROF(28, 0);
      {   /* the smallest h of each run of 8: a run whose smallest h is above hthr has nothing
             to add, and hthr only falls, so whole runs are skipped (most of them, once the
             list is full) and the rest go through in order, as before */
        int32_t bm[512 / 8];
        int b;
        FX_PROF(36, 1);
        for (b = 0; b < n / 8; b++) {
          const int32_t *hb = hv + 8 * b;
          int32_t m = hb[0];
          for (k = 1; k < 8; k++) m = hb[k] < m ? hb[k] : m;
          bm[b] = m;
        }
        FX_PROF(36, 0);
        FX_PROF(37, 1);
        for (b = 0; b < n / 8; b++)
          if (bm[b] <= hthr)
            for (v = 8 * b; v < 8 * b + 8; v++) CB_CONSIDER(v, hv[v]);
        FX_PROF(37, 0);
      }
#else
      for (v = 0; v < n; v++) {
        const int16_t *y = cb + v * FX_CBW;
        /* 256 x each value, as a 16-bit word, without unpacking: the high byte with the
           low one cleared, the low byte shifted up (two operations a word, not five) */
        fx_acc a = 0;
        for (k = 0; k < FX_CBW; k++)
          a += FX_P16(r[2 * k], (int16_t)(y[k] & -256)) + FX_P16(r[2 * k + 1], (int16_t)((uint16_t)y[k] << 8));
        CB_CONSIDER(v, e2[v] * 32 - (int32_t)(a >> 8));      /* exact: a is a multiple of 256 */
      }
#endif
#undef CB_CONSIDER
    }
    int16_t npath[MBEST][5];
    int32_t nrec[MBEST][NC];
    /* step = sh 2^15 + sl, so a value times the step is two 16 x 16 products (the C55x
       compiler's 16 x 32 multiply is a library call) */
    int16_t sh = (int16_t)(fx_cb_step[s] >> 15), sl = (int16_t)(fx_cb_step[s] & 0x7FFF);
    for (i = 0; i < nk; i++) {
      memcpy(npath[i], path[kp[i]], sizeof path[0]);
      npath[i][s] = (int16_t)kc[i];
      for (k = 0; k < NC; k++) {
        int16_t v = (int16_t)FX_CB_VAL(cb + kc[i] * FX_CBW, k);
        nrec[i][k] = recon[kp[i]][k] + (int32_t)(((fx_acc)v * sh) * 32768 + (fx_acc)v * sl);
      }
    }
    memcpy(path, npath, sizeof npath);
    memcpy(recon, nrec, sizeof nrec);
    np = nk;
  }
  return np;
}

fx_acc fx_shape_dist(const int32_t *a, const int32_t *b) {
  fx_acc d = 0;
  int k;
  for (k = 0; k < NC; k++) {
    int32_t q = fx_rshr(a[k] - b[k], 8);
    d += (fx_acc)q * q;
  }
  return d;
}

static void msvq_search(const int32_t *c, int16_t *idx, int32_t *hv) {
  int16_t path[MBEST][5];
  int32_t recon[MBEST][NC];                                  /* dB Q16 */
  int p, best = 0, np = fx_msvq_paths(c, fx_cb_stages, path, recon, hv);
  fx_acc bd = 0;
  for (p = 0; p < np; p++) {
    fx_acc d = fx_shape_dist(c, recon[p]);
    if (p == 0 || d < bd) bd = d, best = p;
  }
  memcpy(idx, path[best], fx_cb_stages * sizeof(int16_t));
}

/* envelope (dB Q8) at f (Hz Q4), as the decoder's */
static int32_t env_at(const int32_t *rk, int32_t f) {
  if (f <= fx_kfreq[0]) return rk[0];
  if (f >= fx_kfreq[K - 1]) return rk[K - 1];
  int j = 0;
  while (fx_kfreq[j + 1] < f) j++;
  int32_t fr = (int32_t)(fx_mul32(f - fx_kfreq[j], fx_kinvw[j]) >> 16);
  return rk[j] + (int32_t)((fx_mul32(rk[j + 1] - rk[j], fr) + 32768) >> 16);
}

/* 10 log10 of a power p 2^xb in units of a spectrum with exponent ex, as absolute dB Q8 */
static int32_t power_db(int ex, fx_acc p, int xb) {
  if (p <= 0) return DB(-90);
  return fx_log2_to_db_pow(fx_log2_acc((fx_uacc)p) + 2 * ((int32_t)ex * 65536 + fx_sw_l2) + (int32_t)xb * 65536);
}

static void emit(struct sotto_enc *e, int cls, uint32_t wo_o);

static void decide(struct sotto_enc *e, int32_t t, int k) {
  int h, i, l, j;
  int32_t u;
  frame_t *f = R(e, t);
  uint32_t wo = k ? f->cand[k - 1] : f->wo0;
  int L = n_harm(wo);
  /* frame t's spectrum, kept from its analysis: the 32-bit bins for the harmonic levels,
     and the 16-bit copy for the harmonic fits */
  spec_t spx, *sp = &spx;
  const int32_t *re = e->sre[t % SRING], *im = e->sim[t % SRING];
  int ex = e->sexp[t % SRING];
  FX_PROF(7, 1);
  spectrum_bins(re, im, ex, sp);
  FX_PROF(7, 0);
  /* band powers from the bins rounded to 16 bits: a bin's power is < 2^33, a band (at most
     33 bins) < 2^39, and the total is summed at 1/8 */
  int t16 = 0;
  {
    uint32_t m = fx_bins_max(re, im, NB);
    while ((m >> t16) >= ((uint32_t)1 << 16) - 1) t16++;  /* rounding may add one */
  }
  fx_acc A2[MAXH + 1], tot = 0;
  fx_uacc edge = (fx_uacc)wo + ((fx_uacc)1 << 23);          /* (2h - 1) wo + 1/2 bin */
  for (h = 1; h <= L; h++, edge += 2 * (fx_uacc)wo) {
    int am = (int)(edge >> 24), bm = (int)((edge + 2 * (fx_uacc)wo) >> 24);
    fx_acc p = 0;
    for (i = am; i < bm; i++) {
      int b = i > 256 ? 512 - i : i;
      p += sq2(re[b], im[b], t16);
    }
    FX_ACC_CHECK(p);
    A2[h] = p;
    tot += p >> 3;
  }
  ex += t16;
  int32_t en = power_db(ex, tot, 3);

  /* unvoice_tails, then voicing_classes */
  e->ehist[t % (TAIL_HOLD + 1)] = en;
  int cls = k > 0;
  int32_t snr = INT32_MAX;
  if (cls) {
    int32_t emax = INT32_MIN;
    for (u = t - TAIL_HOLD < 0 ? 0 : t - TAIL_HOLD; u <= t; u++)
      if (e->ehist[u % (TAIL_HOLD + 1)] > emax) emax = e->ehist[u % (TAIL_HOLD + 1)];
    /* mean harmonic SNR below 1 kHz, dB Q8 */
    int32_t sum = 0, n = 0;
    for (l = 1; l <= L; l++) {
      if ((fx_uacc)l * wo >= ((fx_uacc)1 << 29)) break;          /* 1 kHz */
      fx_acc sig, err = harm_fit(sp, wo, l, &sig);
      sum += sig > 0 ? fx_log2_to_db_pow(fx_log2_acc((fx_uacc)sig) - fx_log2_acc((fx_uacc)(err > 0 ? err : 1))) : 0;
      n++;
    }
    if (n) snr = sum / n;
    if (en < emax - DB(20) && snr < DB(6)) cls = 0;
    else if (snr < DB(8)) cls = MIXED;
  }

  /* the rate-K envelope of every half: the shape of even halves, the v3.8 features of both */
  int32_t a[MAXH], amax = INT32_MIN, rk[K], sum = 0;
  for (h = 1; h <= L; h++) {
    a[h - 1] = A2[h] > 0 ? power_db(ex, A2[h], 0) : DB(-320);
    if (a[h - 1] > amax) amax = a[h - 1];
  }
  for (h = 0; h < L; h++)
    if (a[h] < amax - DB(50)) a[h] = amax - DB(50);
  /* np.interp of the harmonic levels at the rate-K frequencies (harmonic h at h f0) */
  for (j = 0; j < K; j++) {
    fx_uacc u = fx_udiv_shl((fx_uacc)fx_kfreq[j], 38, (fx_uacc)wo * 125, NULL);   /* kfreq 2^41 / (1000 wo): harmonics, Q16 */
    int32_t ui = (int32_t)(u >> 16), fr = (int32_t)(u & 0xFFFF);
    if (ui < 1) rk[j] = a[0];
    else if (ui >= L) rk[j] = a[L - 1];
    else rk[j] = a[ui - 1] + (int32_t)(((fx_acc)(a[ui] - a[ui - 1]) * fr + 32768) >> 16);
  }
  for (i = 0; i < K; i++) sum += rk[i];
  {   /* per-half features of the v3.8 encoder rules (hiss_band._onsets, _whisper) */
    int hf = (int)(t % 2), nlo = 0, nhi = 0;
    int32_t mean = (sum + (sum >= 0 ? K / 2 : -(K / 2))) / K, lo = 0, hi = 0;
    int32_t m16 = mean * 256, dec = e->peak - 3277;      /* peak falls 0.05 dB a half */
    e->peak = m16 > dec ? m16 : dec;
    e->cur.en[hf] = mean;
    e->cur.loud[hf] = (int16_t)(m16 > e->peak - DB(16) * 256);
    for (i = 0; i < K; i++) {
      if (fx_kfreq[i] < 1000 * 16) lo += rk[i], nlo++;
      if (fx_kfreq[i] > 2000 * 16) hi += rk[i], nhi++;
    }
    e->cur.tilt[hf] = lo / nlo - hi / nhi;
    e->cur.snr[hf] = cls ? snr : FX_SNR_NONE;
    e->cur.lwo[hf] = ln_wo(wo);
  }

  if (t % 2 == 0) {
    int32_t c[NC];
    for (j = 1; j < K; j++) {                            /* DCT of rk - mean, dB Q16 */
      fx_acc v = 0;
      for (i = 0; i < K; i++) v += fx_mul32x16(rk[i] * 20 - sum, fx_dct[j * K + i]);
      FX_ACC_CHECK(v);
      c[j - 1] = fx_rshr(v / 20, 7);                         /* Q8 x Q15 x 20 -> dB Q16 */
    }
    FX_PROF(6, 1);
    memcpy(e->c_e, c, sizeof e->c_e);
    if (e->shape_only) memset(e->idx_e, 0, sizeof e->idx_e);
    else msvq_search(c, e->idx_e, e->re);                /* e->re is free here */
    FX_PROF(6, 0);
    e->wo_e = wo;
    /* v3.6: energy of the envelope at the harmonics */
    int32_t lv[MAXH];
    for (h = 1; h <= L; h++) lv[h - 1] = env_at(rk, (int32_t)(((fx_uacc)h * wo * 125) >> 22));   /* h f0, Hz Q4 */
    e->en_e = fx_db_power_sum(lv, L);
    e->cls_e = (int16_t)cls;
  } else {
    emit(e, cls, wo);
  }
}

static int wo_index(uint32_t wo) {
  int wi = 0;
  while (wi < 127 && fx_wo_thr[wi] <= wo) wi++;
  return wi;
}

/* pack the frame: the even half's parameters and both halves' voicing classes. The low-rate
   modes' pitch (p.wi) at an onset edge (unvoiced, then voiced half) is the voiced half's
   (wo_o), as the float core's (sotto.c emit); the 2400 bits keep the even half's. */
static void emit(struct sotto_enc *e, int cls, uint32_t wo_o) {
  int s;
  int a = e->cls_e, b = cls, code = a == b ? a : (a == 0 || b == 0) ? 3 : 1;
  fx_enc_frame_t *q = &e->q[(e->q_head + e->q_n++) % 8];
  unsigned char *out = q->bits;
  *q = e->cur;
  int pos = 0, wi = wo_index(e->wo_e), ei;
  int32_t num = (e->en_e - DB(10)) * 31;                     /* rint((en - 10) / 70 x 31) */
  ei = num <= 0 ? 0 : (int)((num + 8960) / 17920);
  if (ei > 31) ei = 31;
  q->p.code = (int16_t)code, q->p.ei = (int16_t)ei;
  q->p.wi = (int16_t)(a == 0 && b != 0 ? wo_index(wo_o) : wi);
  memcpy(q->p.c, e->c_e, sizeof q->p.c);
  memset(out, 0, SOTTO_BYTES_PER_FRAME);
  put_bits(out, &pos, code, 2);
  put_bits(out, &pos, wi, 7);
  put_bits(out, &pos, ei, 5);
  for (s = 0; s < fx_cb_stages; s++) put_bits(out, &pos, e->idx_e[s], fx_cb_bits[s]);
}

static void decide_from(struct sotto_enc *e, int32_t t, int32_t last) {
  int j;
  int32_t u;
  frame_t *f = R(e, last);
  int k = 0;
  for (j = 1; j <= f->nc; j++)
    if (f->D[j] < f->D[k]) k = j;
  for (u = last; u > t; u--) k = R(e, u)->back[k];
  decide(e, t, k);
}

/* ---------------------------------------------------------------- API */

struct sotto_enc *sotto_enc_create(void) {
  int i;
  struct sotto_enc *e = calloc(1, sizeof *e);
  if (!e) return NULL;
  for (i = 0; i < HIST; i++) e->hist[i] = 1;
  e->prev_bin = 16;                                          /* 50 Hz */
  e->peak = INT32_MIN / 2;
  return e;
}

void sotto_enc_destroy(struct sotto_enc *e) { free(e); }
void sotto_enc_set_shape_only(struct sotto_enc *e, int shape_only) { e->shape_only = (int16_t)(shape_only != 0); }
unsigned sotto_fx_enc_bytes(void) { return (unsigned)sizeof(struct sotto_enc); }

static void analyse_10ms(struct sotto_enc *e, const short *x) {
  int i;
  memmove(e->hist, e->hist + NSAMP, (HIST - NSAMP) * sizeof(int16_t));
  for (i = 0; i < NSAMP; i++) e->hist[HIST - NSAMP + i] = x[i];
  int32_t a = e->n_an++;
  frame_t *f = R(e, a);
  spec_t *sp = &e->spec[a & 1];
  FX_PROF(0, 1);
  int bin = nlp_bin(e);
  FX_PROF(0, 0);
  FX_PROF(1, 1);
  {
    int s = spectrum_fft(e, e->hist + HIST - MPITCH), r = (int)(a % SRING);
    memcpy(e->sre[r], e->re, NB * sizeof(int32_t));          /* kept for decide() */
    memcpy(e->sim[r], e->im, NB * sizeof(int32_t));
    e->sexp[r] = (int16_t)s;
    spectrum_bins(e->re, e->im, s, sp);
  }
  FX_PROF(1, 0);
  /* Codec 2: Wo = 2 pi / pitch, pitch = 2560 / bin samples; two-stage refinement */
  uint32_t p0 = (((uint32_t)2560 << 16) + (uint32_t)(bin / 2)) / (uint32_t)bin;
  uint32_t wo = recip48(p0);
  FX_PROF(2, 1);
  wo = hs_refine(sp, p0, (uint32_t)5 << 16, (uint32_t)1 << 16, wo);
  wo = hs_refine(sp, recip48(wo), (uint32_t)1 << 16, (uint32_t)1 << 14, wo);
  FX_PROF(2, 0);
  if (wo < WO_MIN) wo = WO_MIN;
  if (wo > WO_C2MAX) wo = WO_C2MAX;
  f->wo0 = wo;
  FX_PROF(3, 1);
  f->v0 = (int16_t)c2_voicing(sp, wo);
  FX_PROF(3, 0);
  if (a >= 1) {
    FX_PROF(4, 1);
    frame_costs(e, a - 1, 1);
    FX_PROF(4, 0);
    viterbi_step(e, a - 1);
    FX_PROF(5, 1);
    if (a - 1 - LAG >= 0) decide_from(e, e->n_dec++, a - 1);
    FX_PROF(5, 0);
  }
}

int sotto_enc_pop_frame(struct sotto_enc *e, fx_enc_frame_t *f) {
  if (!e->q_n) return 0;
  *f = e->q[e->q_head];
  e->q_head = (int16_t)((e->q_head + 1) % 8);
  e->q_n--;
  return 1;
}

static int pop(struct sotto_enc *e, unsigned char *out) {
  fx_enc_frame_t f;
  if (!sotto_enc_pop_frame(e, &f)) return 0;
  memcpy(out, f.bits, SOTTO_BYTES_PER_FRAME);
  return 1;
}

void sotto_enc_analyse_block(struct sotto_enc *e, const short x8[NSAMP]) { analyse_10ms(e, x8); }

int sotto_encode(struct sotto_enc *e, const short speech[SOTTO_SAMPLES_PER_FRAME],
                 unsigned char out[SOTTO_BYTES_PER_FRAME]) {
  analyse_10ms(e, speech);
  analyse_10ms(e, speech + NSAMP);
  return pop(e, out);
}

void sotto_encode_half(struct sotto_enc *e, const short speech[SOTTO_SAMPLES_PER_FRAME / 2]) {
  analyse_10ms(e, speech);
}

void sotto_enc_flush_core(struct sotto_enc *e) {
  if (!e->flushed && e->n_an > 0) {
    int32_t n = e->n_an;
    frame_costs(e, n - 1, 0);
    viterbi_step(e, n - 1);
    while (e->n_dec < n) decide_from(e, e->n_dec++, n - 1);
    if (n % 2) emit(e, e->cls_e, e->wo_e);                   /* odd frame count: the last half repeated */
    e->flushed = 1;
  }
}

int sotto_enc_flush(struct sotto_enc *e, unsigned char out[SOTTO_BYTES_PER_FRAME]) {
  sotto_enc_flush_core(e);
  return pop(e, out);
}
