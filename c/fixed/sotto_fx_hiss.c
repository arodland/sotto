/* sotto v3.8 in fixed point: the 16 kHz layer over the v3.7 core (../sotto_hiss.c in integers).
 *
 * Same structure, call for call, as the float layer: the encoder decimates 16 kHz input for
 * the core, runs the v3.8 rules over a ring of core frames with SOTTO_RULE_LAG frames of
 * look-ahead, and measures the hiss index and the speaker side channel against its own core
 * decoder; the decoder upsamples the core's output and adds a 4-8 kHz noise band shaped per
 * 10 ms STFT frame. What changes in integers:
 *  - the 320-point STFT is a 5 x 64 FFT (five fx_fft of 64 points and radix-5 butterflies),
 *    block floating point: each frame is scaled to 22 bits before the FFT and its bin powers
 *    carry one exponent; band levels are dB Q8 from fx_log2;
 *  - the resamplers use the same 41 half-band taps in Q19;
 *  - levels, weights and thresholds are dB Q8 (tables Q14), pitch logs Q16, the smoothing
 *    one-poles Q15, the running percentile exact in integers;
 *  - the noise band is synthesised by the same FFT on the conjugate spectrum, with the phase
 *    sequence of the float layer (xorshift32 as Q32 turns), so the two outputs' hiss bands
 *    agree sample for sample within rounding.
 * The hiss index and the side channel can differ from the float layer's for near-ties
 * (tools/check_fx.py --v38 counts them); the output levels agree within a fraction of a dB.
 * SPDX-License-Identifier: MIT */
#define SOTTO_BUILD
#include <stdlib.h>
#include <string.h>
#include "../sotto.h"
#include "fx.h"
#include "fx_internal.h"
#include "fx_tables.h"

#define N16 SOTTO_SAMPLES_PER_FRAME_16K     /* 320 */
#define N8 SOTTO_SAMPLES_PER_FRAME          /* 160 */
#define HN 320                              /* STFT window, 20 ms at 16 kHz */
#define HH 160                              /* hop */
#define NBIN 161
#define NTAPS 41
#define NFB FX_NFB
#define HB_LAG 2
#define WIN_FRAMES 100
#define MSG_BITS 16
#define DB(x) ((int32_t)((x) * 256L))
#define UV_GAIN DB(3)
#define MIXED_DB DB(-3)
#define BURST_DB DB(12)
#define SIB_DB DB(-10)                      /* sib_db = -10, sib_voiced = True (the "z fix") */
#define ONSET_LOWDOM DB(3)
#define WHISPER_MIN 10
#define WHISPER_SNR2 (2 * DB(6))            /* twice the median threshold (medians are kept doubled) */
#define WHISPER_JIT2 3277                   /* 2 x 0.025 in Q16, rounded up: 2 x median >= this */
#define VOICED_TRIM DB(-5)
#define SMOOTH_A 23479                      /* exp(-10 / 30) Q15, the 30 ms one-poles */
#define SMOOTH_B (32768 - SMOOTH_A)
#define BRIDGE 4                            /* blend: flickers of <= 4 frames bridged */
#define PC_STEP_Q8 (4 * 256)
#define PCT_WARM 50
#define SIDE_MIN_FRAMES 20
#define DB_FLOOR DB(-120)                   /* 10 log10(1e-12), the float layer's power floor */
#define MASK3_DB 2443                       /* 20 log10(3), Q8: mixed frames' consonant layer */
#ifndef SOTTO_RULE_LAG
#define SOTTO_RULE_LAG 10                   /* frames of look-ahead for the encoder rules */
#endif
#ifndef FX_PCT_MAX
#define FX_PCT_MAX 12000                    /* frames the running percentile remembers (120 s, 4 bytes each) */
#endif
#define FRING 128                           /* frames of history the rules see */
#define SRING 64                            /* STFT frames of features kept */
#define MRING 64                            /* model frames (10 ms) */
#define GRING 32                            /* codec frames */
#define TRING 32                            /* STFT frames between the decoder's stages A and B */
#define ALIGN_CALLS 2                       /* core decoder calls before its output lines up with the input */

static const int32_t pc_min_q8[3] = {DB(-52), DB(-32), DB(-32)};
/* 20 log10 of the band edge ramp clip((f - 3900) / 200, 0, 1) at bins 79, 80, 81 (dB Q8); 0 above, off below */
static const int32_t ramp_db[3] = {-3083, -1541, -640};
#define RAMP0 79
/* interpolation of the 4 and 16 band levels onto the bins (np.interp, clamped): segment and Q16 fraction */
static int16_t seg4[NBIN], seg16[NBIN];
static int32_t frac4[NBIN], frac16[NBIN];
/* the resampler taps (Q19) split into 16-bit halves (tap = hi 2^15 + lo) and ordered by the
   input sample they multiply: down2 tdh/tdl[i] for x[i]; up2 teh/tel[i] (even outputs) and
   toh/tol[i] (odd outputs, entry 0 unused) for x[i]. The sums are fx_mul32x16's, regrouped. */
static int16_t tdh[NTAPS], tdl[NTAPS], teh[21], tel[21], toh[21], tol[21];
static int tables_ready;

static void init_tables(void) {
  int k;
  if (tables_ready) return;
  for (k = 0; k < NTAPS; k++) tdh[k] = FX_HI(fx_hs_taps[NTAPS - 1 - k]), tdl[k] = FX_LO(fx_hs_taps[NTAPS - 1 - k]);
  for (k = 0; k <= 20; k++) teh[k] = FX_HI(fx_hs_taps[40 - 2 * k]), tel[k] = FX_LO(fx_hs_taps[40 - 2 * k]);
  toh[0] = tol[0] = 0;
  for (k = 1; k <= 20; k++) toh[k] = FX_HI(fx_hs_taps[41 - 2 * k]), tol[k] = FX_LO(fx_hs_taps[41 - 2 * k]);
  for (k = 0; k < NBIN; k++) {
    int32_t f = 50 * k;
    if (f <= 4500) seg4[k] = 0, frac4[k] = 0;
    else if (f >= 7500) seg4[k] = 3, frac4[k] = 0;
    else seg4[k] = (int16_t)((f - 4500) / 1000), frac4[k] = ((f - 4500 - 1000 * seg4[k]) * 65536 + 500) / 1000;
    if (f <= 4125) seg16[k] = 0, frac16[k] = 0;
    else if (f >= 7875) seg16[k] = 15, frac16[k] = 0;
    else seg16[k] = (int16_t)((f - 4125) / 250), frac16[k] = ((f - 4125 - 250 * seg16[k]) * 65536 + 125) / 250;
  }
  tables_ready = 1;
}

static int32_t lerp(const int32_t *fp, int j, int32_t fr) {
  return fp[j] + (int32_t)(((fx_acc)(fp[j + 1] - fp[j]) * fr + 32768) >> 16);
}
static int32_t interp4(const int32_t *p, int k) { return seg4[k] == 3 ? p[3] : lerp(p, seg4[k], frac4[k]); }
static int32_t interp16(const int32_t *p, int k) { return seg16[k] == 15 ? p[15] : lerp(p, seg16[k], frac16[k]); }

#ifdef FX_CHECK
#include <stdio.h>
static void ring_check(long j, long oldest, int size, const char *what) {
  if (j - oldest >= size) {
    fprintf(stderr, "fx hiss: %s ring too small (%ld samples live)\n", what, j - oldest);
    abort();
  }
}
#define RING_CHECK(j, oldest, size, what) ring_check(j, oldest, size, what)
#else
#define RING_CHECK(j, oldest, size, what) ((void)0)
#endif

/* ------------------------------------------------------------------ 320-point FFT */
#define C5_1 10126                          /* cos(2 pi / 5) Q15 */
#define C5_2 (-26510)                       /* cos(4 pi / 5) */
#define S5_1 31164                          /* sin(2 pi / 5) */
#define S5_2 19261                          /* sin(4 pi / 5) */


/* in place, decimation in time as 5 x m (m = 32 or 64, a 5m-point FFT): output = DFT / m.
   Inputs: |re|, |im| <= 2^23, so the output stays below 2^26.5. sr, si: 5m words of
   scratch each. The STFT is a 320-point real FFT, done as this 160-point complex one
   (m = 32) with fx_rfft_pack/split (or fx_irfft_pre/unpack the other way). */
static void fft5x(int32_t *re, int32_t *im, int32_t *sr, int32_t *si, int m) {
  int n1, k2, t = 64 / m;                      /* twiddle W_5m^i = W_320^(t i) */
  for (n1 = 0; n1 < 5; n1++) {
    int32_t *xr = sr + m * n1, *xi = si + m * n1;
    for (k2 = 0; k2 < m; k2++) xr[k2] = re[5 * k2 + n1], xi[k2] = im[5 * k2 + n1];
    fx_fft(xr, xi, m);
  }
  FX_PROF(32, 1);
#if defined(__TMS320C55X__) && !defined(FX_FFT5_C)
  if (m == 32) {                               /* the loop below in assembly, c55x/fft5.asm */
    fx_fft5_stage32(re, im, sr, si, t);
    FX_PROF(32, 0);
    return;
  }
#endif
  for (k2 = 0; k2 < m; k2++) {
    int32_t zr[5], zi[5], t1r, t1i, t2r, t2i, t3r, t3i, t4r, t4i, a1r, a1i, a2r, a2i, b1r, b1i, b2r, b2i;
    zr[0] = sr[k2], zi[0] = si[k2];
    for (n1 = 1; n1 < 5; n1++) {                 /* Z = Y W^(n1 k2), W = c - j s */
      int i = n1 * k2 * t;                       /* < 320 */
      int16_t c = fx_cos320[i], s = fx_cos320[i + 240 < 320 ? i + 240 : i - 80];
      int32_t yr = sr[m * n1 + k2], yi = si[m * n1 + k2];
      zr[n1] = fx_q15_mac2(yr, c, yi, s), zi[n1] = fx_q15_mac2(yi, c, -yr, s);   /* |c| + |s| < 46342; s may be -32768 */
    }
    t1r = zr[1] + zr[4], t1i = zi[1] + zi[4];
    t2r = zr[2] + zr[3], t2i = zi[2] + zi[3];
    t3r = zr[1] - zr[4], t3i = zi[1] - zi[4];
    t4r = zr[2] - zr[3], t4i = zi[2] - zi[3];
    re[k2] = zr[0] + t1r + t2r;
    im[k2] = zi[0] + t1i + t2i;
    a1r = zr[0] + fx_q15_mac2(t1r, C5_1, t2r, C5_2);   /* fx_q15_mac2 = q15 of the two products, exactly */
    a1i = zi[0] + fx_q15_mac2(t1i, C5_1, t2i, C5_2);
    a2r = zr[0] + fx_q15_mac2(t1r, C5_2, t2r, C5_1);
    a2i = zi[0] + fx_q15_mac2(t1i, C5_2, t2i, C5_1);
    b1r = fx_q15_mac2(t3r, S5_1, t4r, S5_2);
    b1i = fx_q15_mac2(t3i, S5_1, t4i, S5_2);
    b2r = fx_q15_mac2(t3r, S5_2, t4r, -S5_1);
    b2i = fx_q15_mac2(t3i, S5_2, t4i, -S5_1);
    /* X1 = a1 - j b1, X4 = a1 + j b1, X2 = a2 - j b2, X3 = a2 + j b2 */
    re[m + k2] = a1r + b1i, im[m + k2] = a1i - b1r;
    re[4 * m + k2] = a1r - b1i, im[4 * m + k2] = a1i + b1r;
    re[2 * m + k2] = a2r + b2i, im[2 * m + k2] = a2i - b2r;
    re[3 * m + k2] = a2r - b2i, im[3 * m + k2] = a2i + b2r;
  }
  FX_PROF(32, 0);
}

/* ------------------------------------------------------------------ STFT and bands */
typedef struct {
  int32_t re[HN], im[HN], sr[HN], si[HN];   /* FFT work */
} fftw_t;

/* power spectrum of 320 samples (short units) through the sqrt-Hann window (hiss_band.stft):
   P[k] for k = 0..160, with |STFT(x / 2^15)|^2 = P 2^(*ex) (the float layer's units, which
   divides by 32767 or 32768; the 0.0003 dB between them is ignored) */
static void stft_power_(fftw_t *w, const int16_t *x, uint32_t *P, int *ex);
static void stft_power(fftw_t *w, const int16_t *x, uint32_t *P, int *ex) {
  FX_PROF(33, 1);
  stft_power_(w, x, P, ex);
  FX_PROF(33, 0);
}
static void stft_power_(fftw_t *w, const int16_t *x, uint32_t *P, int *ex) {
  int i, s = 0, t;
  uint32_t mx = 0;
  FX_PROF(39, 1);
  for (i = 0; i < HN; i++) {
    w->re[i] = (int32_t)x[i] * fx_hs_win[i];                /* Q15, < 2^30 */
    {
      uint32_t a = (uint32_t)(w->re[i] < 0 ? -w->re[i] : w->re[i]);
      if (a > mx) mx = a;
    }
  }
  if (!mx) {
    memset(P, 0, NBIN * sizeof(uint32_t));
    *ex = 0;
    return;
  }
  while ((mx >> s) >= ((uint32_t)1 << 22)) s++;            /* x 2^-s, largest in [2^21, 2^22) */
  if (s == 0)
    while ((mx << (1 - s)) < ((uint32_t)1 << 22)) s--;
  for (i = 0; i < HN; i++) w->re[i] = s > 0 ? fx_rshr32(w->re[i], s) : (int32_t)((uint32_t)w->re[i] << -s);
  FX_PROF(39, 0);
  FX_PROF(40, 1);
  fx_rfft_pack(w->re, w->im, HN / 2);                      /* real input: 160-point FFT */
  FX_PROF(40, 0);
  FX_PROF(41, 1);
  fft5x(w->re, w->im, w->sr, w->si, 32);
  FX_PROF(41, 0);
  FX_PROF(42, 1);
  fx_rfft_split(w->re, w->im, HN / 2, fx_cos320, HN);       /* bins 0..160, DFT / 64 */
  FX_PROF(42, 0);
  FX_PROF(43, 1);
  {   /* the bins rounded to 19 bits, their powers / 2^8 (< 2^31) */
    uint32_t m = fx_bins_max(w->re, w->im, NBIN);
    for (t = 0; (m >> t) >= ((uint32_t)1 << 19) - 1; t++) ;
    fx_bins_pow(w->re, w->im, P, NBIN, t, 8, 0);
  }
  FX_PROF(43, 0);
  /* X = DFT(x w 2^(15 - s)) / 64 then 2^-t: P = |DFT(x w)|^2 2^(2 (9 - s - t) - 8), and
     |DFT(x w)|^2 = 2^30 |STFT(x / 2^15)|^2 */
  *ex = 2 * s + 2 * t - 10 - 30;
}

/* mean power of the bins k0 <= k < k1 in dB Q8 (hiss_band.bands_db with Hz edges a, b) */
static int32_t bins_db(const uint32_t *P, int ex, int k0, int k1) {
  int k;
  fx_uacc s = 0;
  for (k = k0; k < k1; k++) s += P[k];                     /* <= 161 x 2^31 < 2^39 */
  if (!s) return DB_FLOOR;
  return fx_log2_to_db_pow(fx_log2_acc(s) + (int32_t)ex * 65536 - fx_log2((uint32_t)(k1 - k0)));
}
static int32_t band_db(const uint32_t *P, int ex, int a_hz, int b_hz) {
  return bins_db(P, ex, (a_hz + 49) / 50, (b_hz + 49) / 50);
}
static int32_t total_db(const uint32_t *P, int ex) {
  int k;
  fx_uacc s = 0;
  for (k = 0; k < NBIN; k++) s += P[k];
  if (!s) return DB_FLOOR;
  return fx_log2_to_db_pow(fx_log2_acc(s) + (int32_t)ex * 65536);
}

/* hiss_band.fine_predict: 16 bands at 4-8 kHz (dB re 3.0-3.5 kHz) from the 0-4 kHz bands,
   and the reference level; all dB Q8 */
static void fine_predict(const uint32_t *P, int ex, int32_t *pr, int32_t *ref) {
  int b, o, i;
  int32_t x[16];
  *ref = band_db(P, ex, 3000, 3500);
  x[0] = 256;
  for (b = 0; b < 14; b++) x[1 + b] = band_db(P, ex, 250 + 250 * b, 500 + 250 * b) - *ref;
  x[15] = band_db(P, ex, 3750, 3950) - *ref;
  for (o = 0; o < 16; o++) {
    fx_acc s = 0;
    for (i = 0; i < 16; i++) s += (fx_acc)x[i] * fx_hs_fine_w[i * 16 + o];   /* Q8 x Q14 */
    FX_ACC_CHECK(s);
    pr[o] = fx_rshr(s, 14);
  }
}

/* hiss_band.guess for one frame: the four 4-8 kHz levels from the FB envelope, by class */
static void guess(int cls, const int32_t *E, int32_t *p) {
  int i, b;
  int32_t ref = E[6], feat[8];
  feat[0] = 256;
  for (i = 0; i < 6; i++) feat[1 + i] = E[i] - ref;
  feat[7] = ref;
  for (b = 0; b < 4; b++) {
    fx_acc s = 0;
    for (i = 0; i < 8; i++) s += (fx_acc)feat[i] * fx_hs_guess_w[(cls * 8 + i) * 4 + b];
    FX_ACC_CHECK(s);
    p[b] = fx_rshr(s, 14) + ref;
  }
}

/* ------------------------------------------------------------------ side channel */
static void crc4(const int16_t *b, int n, int16_t *out) {
  int i, r = 0;
  for (i = 0; i < n; i++) {
    int fb = ((r >> 3) & 1) ^ b[i];
    r = ((r << 1) & 0xF) ^ (fb ? 0x3 : 0);
  }
  for (i = 0; i < 4; i++) out[i] = (int16_t)((r >> (3 - i)) & 1);
}

/* hiss_band.pack_pc: 3 speaker-shape coefficients (dB Q8) -> 12 bits + CRC-4 */
static void pack_pc(const int32_t *pc, int16_t *b) {
  int c, i;
  for (c = 0; c < 3; c++) {
    int32_t q = (pc[c] - pc_min_q8[c] + PC_STEP_Q8 / 2) >> 10;   /* rint((pc - min) / 4) */
    int qi = q < 0 ? 0 : q > 15 ? 15 : (int)q;
    for (i = 0; i < 4; i++) b[4 * c + i] = (int16_t)((qi >> (3 - i)) & 1);
  }
  crc4(b, 12, b + 12);
}

/* hiss_band.unpack_pc: 1 and the coefficients when the CRC holds */
static int unpack_pc(const int16_t *b, int32_t *pc) {
  int i;
  int16_t c[4];
  crc4(b, 12, c);
  for (i = 0; i < 4; i++)
    if (c[i] != b[12 + i]) return 0;
  for (i = 0; i < 3; i++) {
    int q = (b[4 * i] << 3) | (b[4 * i + 1] << 2) | (b[4 * i + 2] << 1) | b[4 * i + 3];
    pc[i] = pc_min_q8[i] + PC_STEP_Q8 * q;
  }
  return 1;
}

static int frame_code(const unsigned char *bits) { return bits[0] >> 6; }
static int frame_bit(const unsigned char *bits, int pos) { return (bits[pos >> 3] >> (7 - (pos & 7))) & 1; }
static void set_bit(unsigned char *bits, int pos, int v) {
  if (v) bits[pos >> 3] |= 0x80 >> (pos & 7);
  else bits[pos >> 3] &= ~(0x80 >> (pos & 7));
}
static int index6(const unsigned char *bits) { return (bits[0] & 0x3F); }   /* bits 2..7 */

static uint32_t rng_next(uint32_t *s) {   /* xorshift32, as the float layer */
  uint32_t x = *s;
  x ^= x << 13;
  x ^= x >> 17;
  x ^= x << 5;
  return *s = x;
}

static int16_t sat16(fx_acc v) { return (int16_t)(v > 32767 ? 32767 : v < -32768 ? -32768 : v); }

/* ------------------------------------------------------------------ resamplers */
/* scipy.signal.resample_poly(x, 1, 2): y[n] = sum_j h[j] x16[2n + 20 - j]; x16 at 2n - 20.
   The taps' absolute sum is 1.24, so the Q19 x Q15 sum stays below 2^35. */
static int16_t down2(const int16_t *x) {
#ifdef FX_ARM_DSP
  fx_acc s = FX_JOIN(fx_dot16(tdh, x, NTAPS), fx_dot16(tdl, x, NTAPS));
#else
  /* a half-band filter: the even taps are 0 but the centre one, so only the odd taps and
     x[20] are multiplied (the same sum; the paired MACs above keep all 41) */
  fx_acc h = FX_P16(tdh[20], x[20]), l = FX_P16(tdl[20], x[20]);
  int j;
#ifdef __TMS320C55X__
  for (j = 1; j < NTAPS; j += 2) h += FX_P16(tdh[j], x[j]), l += FX_P16(tdl[j], x[j]);
#else
  /* and symmetric (tap j = tap 40 - j): the two samples are added first, as in up2 */
  for (j = 1; j < 20; j += 2) {
    int32_t v = (int32_t)x[j] + x[40 - j];
    h += (fx_acc)(tdh[j] * v), l += (fx_acc)(tdl[j] * v);
  }
#endif
  fx_acc s = FX_JOIN(h, l);
#endif
  FX_ACC_CHECK(s);
  return sat16(fx_rshr(s, 19));
}

/* resample_poly(x, 2, 1): y[2n + p] = 2 sum_k h[2k + p] x8[n + 10 - k]; x8 at n - 10 */
static void up2(const int16_t *x, int16_t *y) {
  /* half-band: the even outputs' taps are 0 but the centre one, and toh/tol[0] is 0 */
  fx_acc e = FX_JOIN(FX_P16(teh[10], x[10]), FX_P16(tel[10], x[10]));
#if defined(FX_ARM_DSP) || defined(__TMS320C55X__)
  fx_acc o = FX_JOIN(fx_dot16(toh + 1, x + 1, 20), fx_dot16(tol + 1, x + 1, 20));
#else
  /* the odd taps are symmetric (toh/tol[k] = [21 - k]): add the two samples first. lo (< 2^15)
     times a 17-bit sum stays inside 32 bits, so each product is the same as the two apart */
  fx_acc oh = 0, ol = 0;
  int k;
  for (k = 1; k <= 10; k++) {
    int32_t v = (int32_t)x[k] + x[21 - k];
    oh += (fx_acc)(toh[k] * v), ol += (fx_acc)(tol[k] * v);
  }
  fx_acc o = FX_JOIN(oh, ol);
#endif
  FX_ACC_CHECK(e); FX_ACC_CHECK(o);
  y[0] = sat16(fx_rshr(e, 18));
  y[1] = sat16(fx_rshr(o, 18));
}

/* ================================================================== decoder */
#define DY8 256                             /* 8 kHz samples kept: a block plus the resampler's window */
#define DY16 2048                           /* 16 kHz samples: from the output point to the newest */
#define ORING 2048                          /* overlap-add output, samples Q4 */

typedef struct {
  int16_t cls, mask, want;
  int32_t p[4], vt[16], fade;               /* dB Q8 */
} tframe_t;

struct sotto_dec16 {
  struct sotto_dec *core;
  fftw_t w;
  int32_t n;                                /* frames received */
  int32_t nend;                             /* frames in all, once the stream ended, else -1 */
  int16_t y8[DY8];                          /* aligned 8 kHz output, y8[j] at j & (DY8-1) */
  int32_t y8n;                              /* samples so far */
  int16_t y16[DY16];
  int32_t y16n;
  int32_t env[MRING][NFB];                  /* per model frame k, dB Q8 */
  int16_t mcls[MRING];
  int32_t kmax;                             /* model frames known: 0 .. kmax */
  int16_t code[GRING], idx[GRING], run[GRING];   /* per codec frame */
  int32_t pc[GRING][3];
  /* side channel reader (hiss_band._read_pc) */
  int32_t cur[3];
  int16_t got[MSG_BITS], ngot, ok;
  tframe_t tf[TRING];
  int32_t ta, tb;                           /* stage A done for t < ta, stage B for t < tb */
  int32_t last_true;                        /* last STFT frame with want = 1, -1 if none */
  int32_t lv[16], wv;                       /* smoothed vowel layer (dB Q8) and its weight (Q15) */
  int32_t out[ORING];                       /* hiss overlap-add, samples Q4, out[j] at j & (ORING-1) */
  int32_t outn;                             /* samples handed out */
  uint32_t rng;
};

struct sotto_dec16 *sotto_dec16_create(void) {
  struct sotto_dec16 *d = calloc(1, sizeof *d);
  if (!d) return NULL;
  init_tables();
  d->core = sotto_dec_create();
  d->nend = -1;
  d->kmax = -1;
  d->last_true = -1;
  d->ok = 1;
  d->rng = 0x9E3779B9u;
  return d;
}

void sotto_dec16_destroy(struct sotto_dec16 *d) {
  if (!d) return;
  sotto_dec_destroy(d->core);
  free(d);
}
unsigned sotto_fx_dec16_bytes(void) { return (unsigned)sizeof(struct sotto_dec16); }

/* y8 -> y16 for every 8 kHz sample that has its 10 samples of look-ahead */
static void dec_advance_y16(struct sotto_dec16 *d) {
  while (d->y16n / 2 + 10 < d->y8n) {
    int32_t n = d->y16n / 2;
    int16_t x[21], y[2];
    int i;
    if (n >= 10)
      for (i = 0; i < 21; i++) x[i] = d->y8[(n - 10 + i) & (DY8 - 1)];
    else
      for (i = 0; i < 21; i++) {
        int32_t j = n - 10 + i;
        x[i] = j < 0 ? 0 : d->y8[j & (DY8 - 1)];
      }
    up2(x, y);
    RING_CHECK(d->y16n + 1, d->outn < HH * d->ta ? d->outn : HH * d->ta, DY16, "y16");
    d->y16[d->y16n & (DY16 - 1)] = y[0];
    d->y16[(d->y16n + 1) & (DY16 - 1)] = y[1];
    d->y16n += 2;
  }
}

static int32_t dec_frame_of(const struct sotto_dec16 *d, int32_t t) {
  int32_t f = (t + HB_LAG) / 2;
  if (d->nend >= 0 && f > d->nend - 1) f = d->nend - 1;
  return f;
}

/* stage A for STFT frame t (hiss_band.decode_audio16 before the vowel layer): needs model
   frame t + 2, codec frames up to f + 1 and y16 up to 160 t + 320 */
static void dec_stage_a(struct sotto_dec16 *d, int32_t t) {
  tframe_t *s = &d->tf[t % TRING];
  int32_t k = t + HB_LAG, f = dec_frame_of(d, t), src = -1;
  int32_t nlast = d->nend >= 0 ? d->nend - 1 : d->n - 1;
  const int32_t *E, *pc;
  int v, code, run, b, c, i, ex;
  int16_t x[HN];
  uint32_t P[NBIN];
  int32_t pr[16], ref;
  if (k > d->kmax) k = d->kmax;
  E = d->env[k % MRING];
  v = d->mcls[k % MRING];
  code = d->code[f % GRING];
  if (code == 0) src = f;
  else if (v == 0) {
    if (f - 1 >= 0 && d->code[(f - 1) % GRING] == 0) src = f - 1;
    else if (f + 1 <= nlast && d->code[(f + 1) % GRING] == 0) src = f + 1;
  }
  guess(v, E, s->p);
  s->mask = 0;
  if (src >= 0) {
    for (b = 0; b < 4; b++) s->p[b] += fx_hs_cb6[d->idx[src % GRING] * 4 + b];
    s->mask = 1;
  } else if (v == 2) {
    for (b = 0; b < 4; b++) s->p[b] += MIXED_DB;
    s->mask = 3;
  }
  run = d->run[f % GRING];
  s->fade = run == 0 ? 0 : run > 4 ? DB(-80) : -DB(6) * (run - 2 > 0 ? run - 2 : 0);
  for (b = 0; b < 4; b++) s->p[b] += s->fade + (s->mask == 1 ? UV_GAIN : 0);
  s->cls = (int16_t)v;
  s->want = (int16_t)((v == 1 || v == 2) && s->mask != 1);
  /* vowel layer level: the predictor on the decoded 0-4 kHz plus the speaker shape */
  RING_CHECK(d->y16n - 1, HH * t, DY16, "y16 (stage A)");
  for (i = 0; i < HN; i++) x[i] = d->y16[(HH * t + i) & (DY16 - 1)];
  stft_power(&d->w, x, P, &ex);
  fine_predict(P, ex, pr, &ref);
  pc = d->pc[f % GRING];
  for (b = 0; b < 16; b++) {
    fx_acc corr = (fx_acc)fx_hs_fine_m0[b] * 16384;
    for (c = 0; c < 3; c++) corr += (fx_acc)pc[c] * fx_hs_fine_vt[c * 16 + b];
    s->vt[b] = pr[b] + ref + fx_rshr(corr, 14);
  }
}

/* hiss_band.decode_audio16, blend: want with flickers bridged (needs stage A up to t + 4) */
static int dec_want_bridged(struct sotto_dec16 *d, int32_t t) {
  tframe_t *s = &d->tf[t % TRING];
  int32_t g0 = d->last_true + 1, j;
  if (s->want) return 1;
  if (d->last_true < 0) return 0;            /* a gap from the start is not bridged */
  for (j = t + 1; j <= g0 + BRIDGE; j++) {
    if (j >= d->ta) return 0;                /* reaches the end (or beyond what is known) */
    if (d->tf[j % TRING].want) return 1;
  }
  return 0;
}

/* stage B: the two hiss layers of frame t, blended, into the output overlap-add */
static void dec_stage_b(struct sotto_dec16 *d, int32_t t) {
  tframe_t *s = &d->tf[t % TRING];
  int want = dec_want_bridged(d, t), b, k, i, q;
  int32_t lvo[16], mdb[NBIN], mx = INT32_MIN, wv_db = 0, l2;
  if (s->want) d->last_true = t;
  if (t == 0)
    memcpy(d->lv, s->vt, sizeof d->lv);
  else
    for (b = 0; b < 16; b++) d->lv[b] = fx_rshr((fx_acc)SMOOTH_A * d->lv[b] + (fx_acc)SMOOTH_B * s->vt[b], 15);
  d->wv = fx_rshr((fx_acc)SMOOTH_A * d->wv + (want ? (fx_acc)SMOOTH_B * 32768 : 0), 15);
  if (d->wv > 32767) d->wv = 32767;
  if (d->wv > 0) wv_db = fx_log2_to_db_amp(fx_log2((uint32_t)d->wv) - 15 * 65536);
  for (b = 0; b < 16; b++) lvo[b] = d->lv[b] + s->fade + VOICED_TRIM;
  /* level per bin: max of the consonant layer (p x mask) and the vowel layer (lvo x wv), x ramp */
  for (k = 0; k < NBIN; k++) {
    int32_t m = INT32_MIN;
    if (k >= RAMP0) {
      if (s->mask) m = interp4(s->p, k) + (s->mask == 3 ? MASK3_DB : 0);
      if (d->wv > 0) {
        int32_t m2 = interp16(lvo, k) + wv_db;
        if (m2 > m) m = m2;
      }
      if (m != INT32_MIN && k < RAMP0 + 3) m += ramp_db[k - RAMP0];
    }
    mdb[k] = m;
    if (m > mx) mx = m;
  }
  if (mx < DB(-200)) {                       /* nothing to add (the phases are still drawn) */
    for (k = 0; k < NBIN; k++) rng_next(&d->rng);
    return;
  }
  /* amplitudes 2^q x the linear level, the largest just below 2^20, with random phases */
  l2 = fx_db_to_log2_amp(mx);
  q = 20 - (int)((l2 + 65535) >> 16);
  if (q < 2) q = 2;
  for (k = 0; k < NBIN; k++) {
    uint32_t ph = rng_next(&d->rng);         /* Q32 turns */
    int32_t a;
    if (mdb[k] == INT32_MIN) {
      d->w.re[k] = d->w.im[k] = 0;
      continue;
    }
    a = fx_exp2(fx_db_to_log2_amp(mdb[k]), q);
    d->w.re[k] = fx_rshr(fx_mul32x16(a, fx_cos(ph)), 15);
    d->w.im[k] = -fx_rshr(fx_mul32x16(a, fx_sin(ph)), 15);   /* conjugate: the inverse by the forward FFT */
  }
  /* the real signal's DFT / 64 from bins 0..160 (the mirror bins implied): a 160-point FFT */
  FX_PROF(34, 1);
  fx_irfft_pre(d->w.re, d->w.im, HN / 2, fx_cos320, HN);
  fft5x(d->w.re, d->w.im, d->w.sr, d->w.si, 32);
  fx_rfft_unpack(d->w.re, d->w.im, HN / 2);
  /* y = conj(out) 64 / 320: real part / 5, windowed, into the overlap-add as samples Q4 */
  for (i = 0; i < HN; i++) {
    int32_t j = HH * t + i;
    fx_acc v = fx_mul32x16(d->w.re[i], fx_hs_win[i]);
    FX_ACC_CHECK(v);
    v = fx_rshr(v, 15) * (fx_acc)26214;
    FX_ACC_CHECK(v);
    RING_CHECK(j, d->outn, ORING, "out");
    d->out[j & (ORING - 1)] += q > 2 ? fx_rshr(v, q - 2) : (int32_t)v;
  }
  FX_PROF(34, 0);
}
/* On the shift above: Re(out) = 5 x 2^q x y (the conjugate trick with DFT / 64), so
   y_short = Re(out) (win_q15 / 2^15) 2^(15 - q) / 5 = Re(out) win_q15 2^-q / 5, and with
   1/5 = 26214 / 2^17: y_short = (Re(out) win_q15 / 2^15) 26214 2^(-2 - q); in Q4 (x 16) the
   shift right is q - 2. */

/* run stages A and B as far as the inputs allow */
static void dec_run(struct sotto_dec16 *d) {
  int32_t nlast, tmax;
  FX_PROF(25, 1);
  dec_advance_y16(d);
  FX_PROF(25, 0);
  nlast = d->nend >= 0 ? d->nend - 1 : d->n - 1;          /* last codec frame received */
  tmax = d->nend >= 0 ? 2 * d->nend - 2 : INT32_MAX;       /* nfr - 1 */
  for (;;) {
    int32_t t = d->ta, k = t + HB_LAG, f = dec_frame_of(d, t);
    if (t > tmax) break;
    if (HH * t + HN > d->y16n) break;
    if (d->nend < 0 && (k > d->kmax || f + 1 > nlast)) break;
    if (d->nend >= 0 && k > d->kmax) k = d->kmax;
    if (k > d->kmax) break;
    FX_PROF(26, 1);
    dec_stage_a(d, t);
    FX_PROF(26, 0);
    d->ta++;
  }
  for (;;) {
    int32_t t = d->tb;
    if (t >= d->ta) break;
    if (d->nend < 0 && t + BRIDGE >= d->ta) break;
    FX_PROF(27, 1);
    dec_stage_b(d, t);
    FX_PROF(27, 0);
    d->tb++;
  }
}

/* y16 + hiss for samples [outn, outn + 320) */
static void dec_output(struct sotto_dec16 *d, short *speech) {
  int i;
  for (i = 0; i < N16; i++) {
    int32_t j = d->outn + i, v = 0;
    if (j >= 0) {
      v = d->out[j & (ORING - 1)] + (j < d->y16n ? (int32_t)d->y16[j & (DY16 - 1)] * 16 : 0);
      d->out[j & (ORING - 1)] = 0;
    }
    speech[i] = sat16(fx_rshr(v, 4));
  }
  d->outn += N16;
}

static void dec_take_core(struct sotto_dec16 *d, const short *y8, int32_t n, int store_env) {
  const fx_dec_info_t *info = sotto_dec_info(d->core);
  int i, h;
  if (n >= ALIGN_CALLS)
    for (i = 0; i < N8; i++) d->y8[d->y8n++ & (DY8 - 1)] = y8[i];
  if (store_env)
    for (h = 0; h < 2; h++) {
      int32_t k = 2 * (n - 1) + h;
      memcpy(d->env[k % MRING], info->env[h], sizeof info->env[h]);
      d->mcls[k % MRING] = info->cls[h];
      d->kmax = k;
    }
}

/* one received frame: core decode, bookkeeping, hiss stages, one 20 ms block out */
void sotto_decode16(struct sotto_dec16 *d, const unsigned char in[SOTTO_BYTES_PER_FRAME], int good,
                    short speech[SOTTO_SAMPLES_PER_FRAME_16K]) {
  short y8[N8];
  int32_t n = d->n;
  const fx_dec_info_t *info;
  FX_PROF(24, 1);
  sotto_decode(d->core, in, good, y8);
  FX_PROF(24, 0);
  info = sotto_dec_info(d->core);
  dec_take_core(d, y8, n, n >= 1);           /* frame n - 1 was synthesised */
  d->code[n % GRING] = (int16_t)frame_code(info->b2);
  d->idx[n % GRING] = (int16_t)index6(info->b2);
  d->run[n % GRING] = info->run;
  /* hiss_band._read_pc: the speaker shape in force at frame n */
  if (n % WIN_FRAMES == 0) d->ngot = 0, d->ok = 1;
  memcpy(d->pc[n % GRING], d->cur, sizeof d->cur);
  if (d->ok && d->ngot < MSG_BITS) {
    if (!good) d->ok = 0;
    else if (frame_code(in) == 0) {
      d->got[d->ngot++] = (int16_t)frame_bit(in, 8);
      if (d->ngot == MSG_BITS && unpack_pc(d->got, d->cur)) memcpy(d->pc[n % GRING], d->cur, sizeof d->cur);
    }
  }
  d->n++;
  dec_run(d);
  if (d->n > SOTTO_DEC16_DELAY_FRAMES) dec_output(d, speech);
  else memset(speech, 0, N16 * sizeof(short));
}

/* the end of the stream: returns 1 and 320 samples while output remains */
int sotto_dec16_flush(struct sotto_dec16 *d, short speech[SOTTO_SAMPLES_PER_FRAME_16K]) {
  if (d->n == 0) return 0;
  if (d->nend < 0) {
    short y8[N8];
    sotto_dec_flush(d->core, y8);
    dec_take_core(d, y8, d->n, 1);
    d->nend = d->n;
    while (d->y8n < N8 * d->nend + 10) d->y8[d->y8n++ & (DY8 - 1)] = 0;   /* zero padding, as the Python */
    dec_run(d);
  }
  if (d->outn >= N16 * d->nend) return 0;
  dec_output(d, speech);
  return 1;
}

/* ================================================================== encoder */
#define EX16 1024                           /* 16 kHz input samples kept */
#define EX8 256
#define LY16 1024
#define OUTQ 64

typedef struct {                            /* features of one STFT frame of the input, dB Q8 */
  int32_t top, low, one, b2530, tot, hb[4], Y[16];
  int16_t vow;
} sframe_t;

typedef struct {                            /* one core frame in the rule ring */
  fx_enc_frame_t raw;
  int16_t code;                             /* after the rules */
  unsigned char bits[SOTTO_BYTES_PER_FRAME];
} rframe_t;

struct sotto_enc16 {
  struct sotto_enc *core;
  struct sotto_dec *ldec;                   /* the encoder's own decoder (hiss_band._low16) */
  fftw_t w;
  int32_t ncall;                            /* 320-sample calls so far */
  int32_t nend;                             /* frames in all once the input ended, else -1 */
  int16_t x16[EX16];                        /* input */
  int32_t x16n;
  int32_t nblk;                             /* 10 ms blocks analysed */
  sframe_t sf[SRING];
  int32_t nst;                              /* input STFT frames computed */
  int32_t pct[FX_PCT_MAX];                  /* sorted frame levels for the running percentile */
  int32_t npct;
  rframe_t rf[FRING];
  int32_t nraw;                             /* core frames in the ring: 0 .. nraw - 1 */
  int32_t nfinal;                           /* frames with final voicing: 0 .. nfinal - 1 */
  int32_t nfed;                             /* frames fed to the local decoder */
  int32_t nemit;                            /* frames finished and queued */
  /* local decoder streams */
  int16_t ly8[EX8];
  int32_t ly8n;
  int16_t ly16[LY16];
  int32_t ly16n;
  int32_t lenv[MRING][NFB];
  int16_t lcls[MRING];
  int32_t lkmax;
  int32_t lnst;                             /* local STFT frames done (pr) */
  int32_t pr[SRING][16];
  fx_acc acc[16];                           /* speaker-shape sums, dB Q8 */
  int32_t cnt;
  int32_t tacc;                             /* STFT frames accumulated: 0 .. tacc - 1 */
  int16_t msg[MSG_BITS], msgpos;
  unsigned char outq[OUTQ][SOTTO_BYTES_PER_FRAME];
  int16_t qh, qn;
  int16_t code[FRING], c0[FRING];           /* rule scratch */
};

struct sotto_enc16 *sotto_enc16_create(void) {
  struct sotto_enc16 *e = calloc(1, sizeof *e);
  if (!e) return NULL;
  init_tables();
  e->core = sotto_enc_create();
  e->ldec = sotto_dec_create();
  e->nend = -1;
  e->lkmax = -1;
  e->msgpos = MSG_BITS;
  return e;
}

void sotto_enc16_destroy(struct sotto_enc16 *e) {
  if (!e) return;
  sotto_enc_destroy(e->core);
  sotto_dec_destroy(e->ldec);
  free(e);
}
unsigned sotto_fx_enc16_bytes(void) { return (unsigned)sizeof(struct sotto_enc16); }

/* np.percentile(v, 60) of the levels so far, then insert the new one: v > q, exactly, with
   q = a0 + frac (a1 - a0) at position 0.6 n of the n + 1 values */
static int enc_above_pct(struct sotto_enc16 *e, int32_t v) {
  int above = 0;
  if (e->npct) {
    int32_t n = e->npct, i = 6 * n / 10, frac10 = 6 * n - 10 * i, a[2], lo = 0, hi = n;
    int r;
    while (lo < hi) {                                       /* below = stored values < v (sorted) */
      int32_t mid = lo + (hi - lo) / 2;
      if (e->pct[mid] < v) lo = mid + 1; else hi = mid;
    }
    for (r = 0; r < 2; r++) {
      int32_t rank = i + r, below = lo;
      if (rank > n) rank = n;
      a[r] = rank < below ? e->pct[rank] : rank == below ? v : e->pct[rank - 1];
    }
    if (i + 1 <= n) above = (fx_acc)(v - a[0]) * 10 > (fx_acc)frac10 * (a[1] - a[0]);
    else above = v > a[0];
  }
  if (e->npct < FX_PCT_MAX) {
    int32_t i = e->npct;
    while (i > 0 && e->pct[i - 1] > v) e->pct[i] = e->pct[i - 1], i--;
    e->pct[i] = v;
    e->npct++;
  }
  return above;
}

/* features of input STFT frame t (needs x16 up to 160 t + 320) */
static void enc_input_stft(struct sotto_enc16 *e, int32_t t) {
  sframe_t *s = &e->sf[t % SRING];
  int16_t x[HN];
  uint32_t P[NBIN];
  int32_t ref;
  int i, b, ex, above;
  for (i = 0; i < HN; i++) x[i] = e->x16[(HH * t + i) & (EX16 - 1)];
  stft_power(&e->w, x, P, &ex);
  s->top = band_db(P, ex, 4000, 8000);
  s->low = band_db(P, ex, 0, 3900);
  s->one = band_db(P, ex, 0, 1000);
  s->b2530 = band_db(P, ex, 2500, 3000);
  s->tot = total_db(P, ex);
  for (b = 0; b < 4; b++) s->hb[b] = band_db(P, ex, 4000 + 1000 * b, 5000 + 1000 * b);
  ref = band_db(P, ex, 3000, 3500);
  for (b = 0; b < 16; b++) s->Y[b] = band_db(P, ex, 4000 + 250 * b, 4250 + 250 * b) - ref;
  above = enc_above_pct(e, s->tot);
  s->vow = (int16_t)((s->one - s->b2530 > DB(10)) && t >= PCT_WARM && above);
}

/* ---- the rules over the ring (hiss_band.encode_low) ---- */
static int32_t enc_frame_of(const struct sotto_enc16 *e, int32_t t) {
  int32_t f = (t + HB_LAG) / 2;
  if (e->nend >= 0 && f > e->nend - 1) f = e->nend - 1;
  return f;
}

/* twice the median (so even counts need no halving) */
static int32_t median2(int32_t *v, int n) {
  int i;
  for (i = 1; i < n; i++) {                 /* insertion sort, n is small */
    int32_t x = v[i];
    int j = i;
    while (j > 0 && v[j - 1] > x) v[j] = v[j - 1], j--;
    v[j] = x;
  }
  return n % 2 ? 2 * v[n / 2] : v[n / 2 - 1] + v[n / 2];
}

static void enc_rules(struct sotto_enc16 *e) {
  int32_t g0 = e->nraw > FRING ? e->nraw - FRING : 0, g1 = e->nraw, t, t0;   /* frames [g0, g1) */
  int n = (int)(g1 - g0), i, j;
  int16_t *code = e->code, *c0 = e->c0;
  rframe_t *rf = e->rf;
  if (g1 == g0) return;
#define RF(g) (&rf[(g) % FRING])
  for (i = 0; i < n; i++) {
    rframe_t *r = RF(g0 + i);
    memcpy(r->bits, r->raw.bits, SOTTO_BYTES_PER_FRAME);
    code[i] = c0[i] = (int16_t)frame_code(r->raw.bits);
  }
  /* _onsets: a loud, low-heavy unvoiced frame next to a voiced or mixed one -> MM with its pitch */
  for (i = 0; i < n; i++) {
    const fx_enc_frame_t *f = &RF(g0 + i)->raw;
    static const int dd[6] = {1, -1, 2, -2, 3, -3};
    if (c0[i] || !f->loud[0] || f->tilt[0] <= ONSET_LOWDOM) continue;
    for (j = 0; j < 6; j++) {
      int h = i + dd[j];
      if (h >= 0 && h < n && (c0[h] == 1 || c0[h] == 2)) {
        unsigned char *b = RF(g0 + i)->bits;
        const unsigned char *nb = RF(g0 + h)->raw.bits;
        int p;
        code[i] = 2;
        for (p = 2; p < 9; p++) set_bit(b, p, frame_bit(nb, p));
        break;
      }
    }
  }
  /* _whisper (lag = SOTTO_RULE_LAG): runs of >= 10 mixed/edge frames with a weak, wandering pitch */
  for (i = 0; i < n;) {
    int h;
    if (code[i] != 2 && code[i] != 3) {
      i++;
      continue;
    }
    h = i;
    while (h < n && (code[h] == 2 || code[h] == 3)) h++;
    if (h - i >= WHISPER_MIN) {
      int hj = h < i + SOTTO_RULE_LAG + 1 ? h : i + SOTTO_RULE_LAG + 1, g, ns = 0, nj = 0;
      int32_t sv[2 * (SOTTO_RULE_LAG + 1)], jv[2 * (SOTTO_RULE_LAG + 1)];
      for (g = i; g < hj; g++) {
        const fx_enc_frame_t *f = &RF(g0 + g)->raw;
        int half;
        for (half = 0; half < 2; half++) {
          int32_t prev, dl;
          if (f->snr[half] != FX_SNR_NONE) sv[ns++] = f->snr[half];
          prev = half ? f->lwo[0] : (g0 + g > 0 ? RF(g0 + g - 1)->raw.lwo[1] : f->lwo[0]);
          dl = f->lwo[half] - prev;
          jv[nj++] = g0 + g == 0 && half == 0 ? 0 : (dl < 0 ? -dl : dl);
        }
      }
      if (ns && median2(sv, ns) < WHISPER_SNR2 && median2(jv, nj) >= WHISPER_JIT2)
        for (g = i; g < h; g++) code[g] = 0;
    }
    i = h;
  }
  /* _vsmooth: edge blips inside voiced runs -> M; V/M flutter -> M */
  for (i = 0; i < n;) {
    int h, g;
    if (code[i] != 3) {
      i++;
      continue;
    }
    h = i;
    while (h < n && code[h] == 3) h++;
    if (i > 0 && h < n && h - i <= 2 && (code[i - 1] == 1 || code[i - 1] == 2) && (code[h] == 1 || code[h] == 2))
      for (g = i; g < h; g++) code[g] = 2;
    i = h;
  }
  memcpy(c0, code, n * sizeof(int16_t));
  for (i = 0; i < n; i++) {
    int w[7], nw = 0, sw = 0;
    if (c0[i] != 1 && c0[i] != 2) continue;
    for (j = i - 3 < 0 ? 0 : i - 3; j <= i + 3 && j < n; j++)
      if (c0[j] > 0) w[nw++] = c0[j];
    for (j = 1; j < nw; j++) sw += w[j] != w[j - 1] && w[j] < 3 && w[j - 1] < 3;
    if (sw >= 3) code[i] = 2;
  }
  /* burst and sibilant rules on the input STFT frames of the recent frames */
  t0 = e->nst > SRING ? e->nst - SRING : 0;
  for (t = t0 < 1 ? 1 : t0; t < e->nst; t++) {
    int32_t g = enc_frame_of(e, t);
    const sframe_t *s = &e->sf[t % SRING], *q = &e->sf[(t - 1) % SRING];
    if (g < g0 || g >= g1) continue;
    if (s->top - q->top > BURST_DB && s->top > s->low - DB(15)) code[g - g0] = 0;
  }
  for (t = t0; t < e->nst; t++) {
    int32_t g = enc_frame_of(e, t);
    const sframe_t *s = &e->sf[t % SRING];
    if (g < g0 || g >= g1) continue;
    if (s->top - s->one > SIB_DB && code[g - g0] != 0) code[g - g0] = 0;
  }
  for (i = 0; i < n; i++) {
    rframe_t *r = RF(g0 + i);
    r->code = code[i];
    set_bit(r->bits, 0, code[i] >> 1);
    set_bit(r->bits, 1, code[i] & 1);
  }
#undef RF
}

static void enc_take_local(struct sotto_enc16 *e, const short *y8, int32_t g, int store_env) {
  const fx_dec_info_t *info = sotto_dec_info(e->ldec);
  int i, h;
  if (g >= ALIGN_CALLS)
    for (i = 0; i < N8; i++) e->ly8[e->ly8n++ & (EX8 - 1)] = y8[i];
  if (store_env)
    for (h = 0; h < 2; h++) {
      int32_t k = 2 * (g - 1) + h;
      memcpy(e->lenv[k % MRING], info->env[h], sizeof info->env[h]);
      e->lcls[k % MRING] = info->cls[h];
      e->lkmax = k;
    }
}

/* feed frame g (final voicing) to the local decoder: its output is frame g - 1 */
static void enc_feed_local(struct sotto_enc16 *e, int32_t g) {
  short y8[N8];
  FX_PROF(20, 1);
  sotto_decode(e->ldec, e->rf[g % FRING].bits, 1, y8);
  FX_PROF(20, 0);
  enc_take_local(e, y8, g, g >= 1);
}

/* the local decoder's 16 kHz output and its STFT predictor, as far as possible (an STFT
   frame as soon as its samples are in, so the small ly16 ring is enough when the flush feeds
   SOTTO_RULE_LAG frames at once) */
static void enc_advance_local(struct sotto_enc16 *e) {
  for (;;) {
    if (HH * e->lnst + HN <= e->ly16n) {
      int16_t x[HN];
      uint32_t P[NBIN];
      int32_t ref;
      int i, ex;
      for (i = 0; i < HN; i++) x[i] = e->ly16[(HH * e->lnst + i) & (LY16 - 1)];
      stft_power(&e->w, x, P, &ex);
      fine_predict(P, ex, e->pr[e->lnst % SRING], &ref);
      e->lnst++;
    } else if (e->ly16n / 2 + 10 < e->ly8n) {
      int32_t n = e->ly16n / 2;
      int16_t x[21], y[2];
      int i;
      if (n >= 10)
        for (i = 0; i < 21; i++) x[i] = e->ly8[(n - 10 + i) & (EX8 - 1)];
      else
        for (i = 0; i < 21; i++) {
          int32_t j = n - 10 + i;
          x[i] = j < 0 ? 0 : e->ly8[j & (EX8 - 1)];
        }
      up2(x, y);
      RING_CHECK(e->ly16n + 1, HH * e->lnst, LY16, "ly16");
      e->ly16[e->ly16n & (LY16 - 1)] = y[0];
      e->ly16[(e->ly16n + 1) & (LY16 - 1)] = y[1];
      e->ly16n += 2;
    } else break;
  }
}

/* hiss_band._side_pc: add the vowel frames up to STFT frame tmax to the speaker-shape mean */
static void enc_accumulate(struct sotto_enc16 *e, int32_t tmax) {
  while (e->tacc <= tmax && e->tacc < e->lnst && e->tacc < e->nst) {
    int32_t t = e->tacc++;
    const sframe_t *s = &e->sf[t % SRING];
    int b;
    if (!s->vow) continue;
    for (b = 0; b < 16; b++) e->acc[b] += s->Y[b] - e->pr[t % SRING][b];
    e->cnt++;
  }
}

/* finish frame g: the 6-bit index of an all-unvoiced frame and its side-channel bit */
static void enc_finish(struct sotto_enc16 *e, int32_t g) {
  rframe_t *r = &e->rf[g % FRING];
  int i, b, c;
  enc_accumulate(e, 2 * g - 3);
  if (g % WIN_FRAMES == 0) {                 /* a new window: its message from the frames before it */
    int32_t pc[3] = {0, 0, 0};
    if (e->cnt >= SIDE_MIN_FRAMES)
      for (c = 0; c < 3; c++) {
        fx_acc s = 0;
        for (b = 0; b < 16; b++) {
          int32_t mean = (int32_t)(e->acc[b] / e->cnt);
          s += (fx_acc)fx_hs_fine_vt[c * 16 + b] * (mean - fx_hs_fine_m0[b]);
        }
        pc[c] = fx_rshr(s, 14);
      }
    pack_pc(pc, e->msg);
    e->msgpos = 0;
  }
  if (r->code == 0) {
    int32_t res[4] = {0, 0, 0, 0}, t;
    int nt = 0, best = 0;
    for (t = 2 * g - 2; t <= 2 * g - 1; t++) {
      int32_t k = t + HB_LAG, p[4];
      if (t < 0 || t >= e->nst) continue;
      if (k > e->lkmax) k = e->lkmax;
      guess(0, e->lenv[k % MRING], p);
      for (b = 0; b < 4; b++) res[b] += e->sf[t % SRING].hb[b] - p[b];
      nt++;
    }
    if (nt) {                                /* nearest cb6 vector to res / nt: compare nt cb6 - res */
      fx_acc bd = 0;
      for (i = 0; i < 64; i++) {
        fx_acc d = 0;
        for (b = 0; b < 4; b++) {
          int32_t x = nt * (int32_t)fx_hs_cb6[i * 4 + b] - res[b];
          d += (fx_acc)x * x;
        }
        if (i == 0 || d < bd) bd = d, best = i;
      }
    }
    for (i = 0; i < 6; i++) set_bit(r->bits, 2 + i, (best >> (5 - i)) & 1);
    if (e->msgpos < MSG_BITS) set_bit(r->bits, 8, e->msg[e->msgpos++]);
  }
  memcpy(e->outq[(e->qh + e->qn++) % OUTQ], r->bits, SOTTO_BYTES_PER_FRAME);
}

/* pull core frames, apply the rules, feed the local decoder, finish what can be finished */
static void enc_run(struct sotto_enc16 *e) {
  fx_enc_frame_t f;
  int32_t nfinal, nfinish;
  FX_PROF(23, 1);
  while (sotto_enc_pop_frame(e->core, &f)) e->rf[e->nraw++ % FRING].raw = f;
  FX_PROF(23, 0);
  FX_PROF(19, 1);
  enc_rules(e);
  FX_PROF(19, 0);
  nfinal = e->nend >= 0 ? e->nraw : (e->nraw - SOTTO_RULE_LAG > 0 ? e->nraw - SOTTO_RULE_LAG : 0);
  while (e->nfed < nfinal) enc_feed_local(e, e->nfed++);
  if (e->nend >= 0 && e->nfed == e->nend && e->ly8n < N8 * e->nend + 10) {
    short y8[N8];
    sotto_dec_flush(e->ldec, y8);
    enc_take_local(e, y8, e->nend, 1);
    while (e->ly8n < N8 * e->nend + 10) e->ly8[e->ly8n++ & (EX8 - 1)] = 0;
  }
  FX_PROF(21, 1);
  enc_advance_local(e);
  FX_PROF(21, 0);
  e->nfinal = nfinal;
  /* a frame is finished one frame after its voicing: its index needs its own decoded
     envelope (synthesised when the next frame was fed), the side channel the local
     output up to the window boundary */
  nfinish = e->nend >= 0 ? e->nend : (nfinal - 1 > 0 ? nfinal - 1 : 0);
  FX_PROF(22, 1);
  while (e->nemit < nfinish && e->qn < OUTQ) enc_finish(e, e->nemit++);
  FX_PROF(22, 0);
}

static int enc_pop(struct sotto_enc16 *e, unsigned char *out) {
  if (!e->qn) return 0;
  memcpy(out, e->outq[e->qh], SOTTO_BYTES_PER_FRAME);
  e->qh = (int16_t)((e->qh + 1) % OUTQ);
  e->qn--;
  return 1;
}

/* decimate block nblk (80 output samples from x16[160 nblk - 20 ..]) for the core; samples
   outside [0, limit) read as zero */
static void enc_decimate_block(struct sotto_enc16 *e, int32_t limit) {
  int32_t n0 = 80 * e->nblk;
  short x8[80];
  int i, j;
  for (i = 0; i < 80; i++) {
    int16_t x[NTAPS];
    int32_t m = 2 * (n0 + i) - 20;
    if (m >= 0 && m + NTAPS <= limit)                      /* the usual case, without tests */
      for (j = 0; j < NTAPS; j++) x[j] = e->x16[(m + j) & (EX16 - 1)];
    else
      for (j = 0; j < NTAPS; j++) x[j] = m + j < 0 || m + j >= limit ? 0 : e->x16[(m + j) & (EX16 - 1)];
    x8[i] = down2(x);
  }
  FX_PROF(17, 1);
  sotto_enc_analyse_block(e->core, x8);
  FX_PROF(17, 0);
  e->nblk++;
}

/* decimate and analyse every 10 ms block whose 20 input samples of look-ahead are in */
static void enc_analyse(struct sotto_enc16 *e) {
  while (2 * (80 * e->nblk + 79) + 20 < e->x16n) {
    FX_PROF(16, 1);
    enc_decimate_block(e, INT32_MAX);
    FX_PROF(16, 0);
  }
  FX_PROF(18, 1);
  while (HH * e->nst + HN <= e->x16n) enc_input_stft(e, e->nst++);
  FX_PROF(18, 0);
}

int sotto_encode16(struct sotto_enc16 *e, const short speech[SOTTO_SAMPLES_PER_FRAME_16K],
                   unsigned char out[SOTTO_BYTES_PER_FRAME]) {
  int i;
  for (i = 0; i < N16; i++) e->x16[e->x16n++ & (EX16 - 1)] = speech[i];
  e->ncall++;
  enc_analyse(e);
  enc_run(e);
  return enc_pop(e, out);
}

int sotto_enc16_flush(struct sotto_enc16 *e, unsigned char out[SOTTO_BYTES_PER_FRAME]) {
  if (e->nend < 0) {
    int32_t n16 = e->x16n;
    fx_enc_frame_t f;
    /* the remaining blocks of the input itself, the resampler reading zeros past its end */
    while (80 * e->nblk + 80 <= n16 / 2) enc_decimate_block(e, n16);
    sotto_enc_flush_core(e->core);
    while (sotto_enc_pop_frame(e->core, &f)) e->rf[e->nraw++ % FRING].raw = f;
    e->nend = e->nraw;
    enc_run(e);
  }
  return enc_pop(e, out);
}
