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
#ifdef FX_HISS_FB
#include "../sotto_vlr.h"
#include "../sotto_vlr_tables.h"
#endif
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
#ifndef FX_HISS_FB
static const int32_t ramp_db[3] = {-3083, -1541, -640};
#endif
#define RAMP0 79
/* interpolation of the 4 and 16 band levels onto the bins (np.interp, clamped): segment and Q16 fraction */
/* shared by the full and light layers and the encoder; the K6 build (FX_HISS_FB without the
   encoder, FX_HISS_NO_ENC) uses none of these */
#if defined(FX_HISS_FB) && defined(FX_HISS_NO_ENC)
#define FB_ONLY 1
#else
#define FB_ONLY 0
#endif
#if !FB_ONLY
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

#endif
#ifndef FX_HISS_FB
static int32_t lerp(const int32_t *fp, int j, int32_t fr) {
  return fp[j] + (int32_t)(((fx_acc)(fp[j + 1] - fp[j]) * fr + 32768) >> 16);
}
static int32_t interp4(const int32_t *p, int k) { return seg4[k] == 3 ? p[3] : lerp(p, seg4[k], frac4[k]); }
static int32_t interp16(const int32_t *p, int k) { return seg16[k] == 15 ? p[15] : lerp(p, seg16[k], frac16[k]); }
#endif

#if !FB_ONLY
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

#endif  /* !FB_ONLY */
#if !FB_ONLY
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
#endif  /* !FB_ONLY */
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
#if !FB_ONLY
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
#endif  /* !FB_ONLY */
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
#if !FB_ONLY
static void set_bit(unsigned char *bits, int pos, int v) {
  if (v) bits[pos >> 3] |= 0x80 >> (pos & 7);
  else bits[pos >> 3] &= ~(0x80 >> (pos & 7));
}
#endif  /* !FB_ONLY */
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
#if !FB_ONLY
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
#endif  /* !FB_ONLY */
#if !defined(FX_HISS_LIGHT)
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

#elif !defined(FX_HISS_FB)   /* FX_HISS_LIGHT: the K6 light layer (docs/k6-hiss-port.md, sim/hiss_band.py options
           vowel_src="model", synth8=True, uv50=False), decoder only. Same stages as above, but
           stage A reads the vowel predictor's bands from the core (fx_dec_info_t.bands) instead
           of an STFT of the decoded audio, and stage B makes the hiss at 8 kHz as its mirror
           about 4 kHz (a 160-point inverse FFT) and folds it in through the interpolator on
           output: out = up2(y8 - s) + 2 s zero-stuffed. No 16 kHz ring, half the overlap-add. */
#define DY8 1024                            /* 8 kHz samples: from the output point to the newest */
#define S8RING 1024                         /* the 8 kHz hiss overlap-add, samples Q4 */
#define LMRING 16                           /* model frames between the core and stage A */
#define LTRING 16                           /* STFT frames between stages A and B */
#define H8 (HH / 2)                         /* 80: the 10 ms hop at 8 kHz */
#define N8W (HN / 2)                        /* 160 */

typedef struct {
  int16_t cls, mask, want;
  int32_t p[4], vt[16], fade;               /* dB Q8 */
} tframe_t;

struct sotto_dec16 {
  struct sotto_dec *core;
  int32_t re[N8W], im[N8W], sr[N8W], si[N8W];   /* FFT work */
  int16_t cos160[N8W];                      /* cos(2 pi i / 160) Q15 */
  int32_t n, nend;
  int16_t y8[DY8];
  int32_t y8n;
  int32_t env[LMRING][NFB], bands[LMRING][16];
  int16_t mcls[LMRING];
  int32_t kmax;
  int16_t code[GRING], idx[GRING], run[GRING];
  int32_t pc[GRING][3];
  int32_t cur[3];
  int16_t got[MSG_BITS], ngot, ok;
  tframe_t tf[LTRING];
  int32_t ta, tb, last_true;
  int32_t lv[16], wv;
  int32_t s8[S8RING];                       /* s[j] at j & (S8RING-1), Q4 */
  int32_t outn;                             /* 16 kHz samples handed out */
  uint32_t rng;
};

struct sotto_dec16 *sotto_dec16_create(void) {
  struct sotto_dec16 *d = calloc(1, sizeof *d);
  int i;
  if (!d) return NULL;
  init_tables();
  d->core = sotto_dec_create();
  for (i = 0; i < N8W; i++) d->cos160[i] = fx_cos320[2 * i];
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

static int32_t dec_frame_of(const struct sotto_dec16 *d, int32_t t) {
  int32_t f = (t + HB_LAG) / 2;
  if (d->nend >= 0 && f > d->nend - 1) f = d->nend - 1;
  return f;
}

/* MODEL_REF_DB in hiss_band.py: the model density against the STFT's 3.0-3.5 kHz band */
#define MODEL_REF DB(-27.6)

static void dec_stage_a(struct sotto_dec16 *d, int32_t t) {
  tframe_t *s = &d->tf[t % LTRING];
  int32_t k = t + HB_LAG, f = dec_frame_of(d, t), src = -1;
  int32_t nlast = d->nend >= 0 ? d->nend - 1 : d->n - 1;
  const int32_t *E, *pc, *B;
  int v, code, run, b, c, i, o;
  int32_t x[16], pr[16], ref;
  if (k > d->kmax) k = d->kmax;
  E = d->env[k % LMRING];
  B = d->bands[k % LMRING];
  v = d->mcls[k % LMRING];
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
  /* vowel layer: hiss_band.fine_levels_model, the ridge on the model's bands */
  ref = B[15] + MODEL_REF;
  x[0] = 256;
  for (b = 0; b < 15; b++) x[1 + b] = B[b] - B[15];
  for (o = 0; o < 16; o++) {
    fx_acc a = 0;
    for (i = 0; i < 16; i++) a += (fx_acc)x[i] * fx_hs_fine_w[i * 16 + o];
    pr[o] = fx_rshr(a, 14);
  }
  pc = d->pc[f % GRING];
  for (b = 0; b < 16; b++) {
    fx_acc corr = (fx_acc)fx_hs_fine_m0[b] * 16384;
    for (c = 0; c < 3; c++) corr += (fx_acc)pc[c] * fx_hs_fine_vt[c * 16 + b];
    s->vt[b] = pr[b] + ref + fx_rshr(corr, 14);
  }
}

static int dec_want_bridged(struct sotto_dec16 *d, int32_t t) {
  tframe_t *s = &d->tf[t % LTRING];
  int32_t g0 = d->last_true + 1, j;
  if (s->want) return 1;
  if (d->last_true < 0) return 0;
  for (j = t + 1; j <= g0 + BRIDGE; j++) {
    if (j >= d->ta) return 0;
    if (d->tf[j % LTRING].want) return 1;
  }
  return 0;
}

/* stage B: the levels per 16 kHz bin as before; bin k (4-8 kHz) goes to 8 kHz bin 160 - k with
   half the amplitude (hiss_band.fold8), and a 160-point inverse FFT makes s */
static void dec_stage_b(struct sotto_dec16 *d, int32_t t) {
  tframe_t *s = &d->tf[t % LTRING];
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
  for (k = RAMP0; k < NBIN; k++) {
    int32_t m = INT32_MIN;
    if (s->mask) m = interp4(s->p, k) + (s->mask == 3 ? MASK3_DB : 0);
    if (d->wv > 0) {
      int32_t m2 = interp16(lvo, k) + wv_db;
      if (m2 > m) m = m2;
    }
    if (m != INT32_MIN && k < RAMP0 + 3) m += ramp_db[k - RAMP0];
    mdb[k] = m;
    if (m > mx) mx = m;
  }
  if (mx < DB(-200)) return;
  l2 = fx_db_to_log2_amp(mx);
  q = 20 - (int)((l2 + 65535) >> 16);
  if (q < 2) q = 2;
  for (i = 0; i <= N8W / 2; i++) {           /* 8 kHz bin i = 16 kHz bin 160 - i */
    int32_t a;
    uint32_t ph;
    k = NBIN - 1 - i;
    if (k < RAMP0 || mdb[k] == INT32_MIN) {
      d->re[i] = d->im[i] = 0;
      continue;
    }
    ph = rng_next(&d->rng);
    a = fx_exp2(fx_db_to_log2_amp(mdb[k]), q);
    d->re[i] = fx_rshr(fx_mul32x16(a, fx_cos(ph)), 15);
    d->im[i] = -fx_rshr(fx_mul32x16(a, fx_sin(ph)), 15);
  }
  fx_irfft_pre(d->re, d->im, N8W / 2, d->cos160, N8W);
  fft5x(d->re, d->im, d->sr, d->si, 16);
  fx_rfft_unpack(d->re, d->im, N8W / 2);
  /* as the 320-point path (Re = 5 2^q y), with half the amplitude: shift q - 1 for Q4 */
  for (i = 0; i < N8W; i++) {
    int32_t j = H8 * t + i;
    fx_acc v = fx_rshr(fx_mul32x16(d->re[i], fx_hs_win[2 * i]), 15) * (fx_acc)26214;
    d->s8[j & (S8RING - 1)] += fx_rshr(v, q - 1);
  }
}

static void dec_run(struct sotto_dec16 *d) {
  int32_t nlast = d->nend >= 0 ? d->nend - 1 : d->n - 1;
  int32_t tmax = d->nend >= 0 ? 2 * d->nend - 2 : INT32_MAX;
  for (;;) {
    int32_t t = d->ta, k = t + HB_LAG, f = dec_frame_of(d, t);
    if (t > tmax) break;
    if (H8 * t + N8W > d->y8n + 10) break;   /* as the 16 kHz path's y16 condition */
    if (d->nend < 0 && (k > d->kmax || f + 1 > nlast)) break;
    if (d->nend >= 0 && k > d->kmax) k = d->kmax;
    if (k > d->kmax) break;
    dec_stage_a(d, t);
    d->ta++;
  }
  for (;;) {
    int32_t t = d->tb;
    if (t >= d->ta) break;
    if (d->nend < 0 && t + BRIDGE >= d->ta) break;
    dec_stage_b(d, t);
    d->tb++;
  }
}

/* the 1:2 interpolator with the taps rounded to Q15, so every product and the sum stay in 32
   bits (the taps' absolute sum is 1.24): the light layer isn't bit-exact with anything, and
   on the M0 this is a third of up2's time. x8 at n - 10, as up2. */
static int16_t th15[11];                    /* th15[k] = tap 21 - 2k (k = 1..10), th15[0] the centre */
static void up2_q15(const int16_t *x, int16_t *y) {
  int32_t o = 0, e = th15[0] * (int32_t)x[10];
  int k;
  if (!th15[0]) {
    th15[0] = (int16_t)((fx_hs_taps[20] + 8) >> 4);
    for (k = 1; k <= 10; k++) th15[k] = (int16_t)((fx_hs_taps[41 - 2 * k] + (fx_hs_taps[41 - 2 * k] >= 0 ? 8 : -8)) / 16);
  }
  for (k = 1; k <= 10; k++) o += th15[k] * ((int32_t)x[k] + x[21 - k]);
  e = (e + (1 << 13)) >> 14, o = (o + (1 << 13)) >> 14;     /* 2 sum, Q15 */
  y[0] = (int16_t)(e > 32767 ? 32767 : e < -32768 ? -32768 : e);
  y[1] = (int16_t)(o > 32767 ? 32767 : o < -32768 ? -32768 : o);
}

/* up2(y8 - s) + 2 s zero-stuffed for 16 kHz samples [outn, outn + 320): y8 - s is formed once
   for the block and its 20 samples of window, then the interpolator slides over it */
static void dec_output(struct sotto_dec16 *d, short *speech) {
  int16_t x[N8 + 20];
  int32_t n0 = d->outn / 2, i, p;
  for (i = 0; i < N8 + 20; i++) {
    int32_t j = n0 - 10 + i, v;
    if (j < 0 || j >= d->y8n) { x[i] = 0; continue; }
    v = (int32_t)d->y8[j & (DY8 - 1)] - ((d->s8[j & (S8RING - 1)] + 8) >> 4);
    x[i] = (int16_t)(v > 32767 ? 32767 : v < -32768 ? -32768 : v);
  }
  for (p = 0; p < N8; p++) {
    int32_t n = n0 + p, v;
    int16_t y[2];
    up2_q15(x + p, y);
    v = y[0] + (n >= 0 ? (d->s8[n & (S8RING - 1)] + 4) >> 3 : 0);   /* + 2 s, Q4 -> short */
    speech[2 * p] = (int16_t)(v > 32767 ? 32767 : v < -32768 ? -32768 : v);
    speech[2 * p + 1] = y[1];
  }
  for (i = 0; i < N8; i++) if (n0 - 10 + i >= 0) d->s8[(n0 - 10 + i) & (S8RING - 1)] = 0;   /* last use */
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
      memcpy(d->env[k % LMRING], info->env[h], sizeof info->env[h]);
      memcpy(d->bands[k % LMRING], info->bands[h], sizeof info->bands[h]);
      d->mcls[k % LMRING] = info->cls[h];
      d->kmax = k;
    }
}

void sotto_decode16(struct sotto_dec16 *d, const unsigned char in[SOTTO_BYTES_PER_FRAME], int good,
                    short speech[SOTTO_SAMPLES_PER_FRAME_16K]) {
  short y8[N8];
  int32_t n = d->n;
  const fx_dec_info_t *info;
  sotto_decode(d->core, in, good, y8);
  info = sotto_dec_info(d->core);
  dec_take_core(d, y8, n, n >= 1);
  d->code[n % GRING] = (int16_t)frame_code(info->b2);
  d->idx[n % GRING] = (int16_t)index6(info->b2);
  d->run[n % GRING] = info->run;
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

int sotto_dec16_flush(struct sotto_dec16 *d, short speech[SOTTO_SAMPLES_PER_FRAME_16K]) {
  if (d->n == 0) return 0;
  if (d->nend < 0) {
    short y8[N8];
    sotto_dec_flush(d->core, y8);
    dec_take_core(d, y8, d->n, 1);
    d->nend = d->n;
    while (d->y8n < N8 * d->nend + 10) d->y8[d->y8n++ & (DY8 - 1)] = 0;
    dec_run(d);
  }
  if (d->outn >= N16 * d->nend) return 0;
  dec_output(d, speech);
  return 1;
}
#else   /* FX_HISS_LIGHT + FX_HISS_FB: the light layer made in step with the core (sim/hiss_band.py
           options vowel_src="model", uv50=False, fb=4, fb_order=3, causal="none", synth8=True,
           fb_pair=True, fb_hop2=True). Each core call's 8 kHz block gets its hiss at once:
           stages A and B run for the odd STFT frame centred on the block's end (frame t reads
           model frame t + 2, the half the core has just made), B sums the frame's 4-8 kHz
           levels into 4 band gains (no inverse FFT), and two order-3 filters at 8 kHz on one
           white noise make the mirror s: the 4-5 and 5-6 kHz bands, and (-1)^n times the same
           for 7-8 and 6-7 kHz. Gains are linear across the block, from the frame centred on its
           start to the one centred on its end; folded in as out = up2(y8 - s) + 2 s
           zero-stuffed. No audio is held back but the interpolator's 10 samples: the output is
           2 frames and 20 samples (16 kHz) behind the input, and the state is under 1 KB.
           Blend's flicker bridge (a look 4 frames ahead) is off. Jeff heard no difference
           between this and four filters with gains every 10 ms (2026-11-07). */
#define LM 2                                /* odd model halves kept (odd frame t reads half t + 2) */
#define GR 4                                /* codec frames kept (f - 1 .. f + 1) */
#define NG 4                                /* frames of band gains kept */
#define FBN 4                               /* bands: 4-5, 5-6, 6-7, 7-8 kHz */
#define FBF 2                               /* filters: 4-5 and 5-6 kHz (the others mirror them) */
#define FBS 5                               /* their sections */
#define XR 32                               /* y8 - s kept for the interpolator (21 used), stored twice */
#define SR8 16                              /* s kept for the even outputs (11 used) */

/* the filters at 8 kHz on the mirror (hiss_band.fb_filters8(4, 3)), Q14: b0 b1 b2 a1 a2. The 6-7
   and 7-8 kHz filters are these with z -> -z, so (-1)^n times these outputs */
static const int16_t fb_sec[FBS][5] = {
  {519, -1038, 519, 6786, 0}, {16384, -16384, 0, 17118, 7825},                                 /* 4-5 kHz: high pass 3 kHz */
  {519, -1038, 519, 9598, 6786}, {16384, 0, -16384, 1289, 10679}, {16384, 32767, 16384, 19374, 12005}};   /* 5-6 kHz */
static const int8_t fb_nsec[FBF] = {2, 3};
/* per band, dB Q8: 20 log10(32768 sc / sigma) + 10 log10(2 / 320^2), sc the band's filter's unit
   in-band gain (bands 2 and 3 use filters 1 and 0), sigma the noise's (uniform +-1024) standard
   deviation; the second term turns the levels' power sum into the istft's power per sample
   (hiss_band.fb_gains) */
static const int16_t fb_amp_db[FBN] = {-1477, -1482, -1482, -1477};
#define SMOOTH2_A 16824                     /* exp(-20 / 30) Q15: the 30 ms one-poles at 20 ms steps */
#define SMOOTH2_B (32768 - SMOOTH2_A)
/* interpolation fractions (Q16) onto the 50 Hz bins: 16 levels at 4125 + 250 i, 4 at 4500 + 1000 i */
static const int32_t fr16[5] = {6554, 19661, 32768, 45875, 58982};   /* Q16 */

typedef struct {
  int16_t cls, mask, want;
  int32_t p[4], vt[16], fade;               /* dB Q8 */
} tframe_t;

#define LVR 13                              /* frames whose hiss level is kept for the sotto 600 / 400 cap (it reaches 12 back) */

/* The layer. sotto_decode16 owns a core and feeds it v3.8 frames; the sotto 1200 and
   sotto 600 / 400 hiss decoders below feed it through their decoders' hooks, each frame the
   core takes, with the hiss residual their bits give. One 320-sample block comes out per frame
   pushed, and each push writes into a 320-sample buffer whose upper half holds the core's 160
   samples (the caller decodes the core there), so no 8 kHz copy is kept. Levels are dB Q8. */
struct sotto_dec16 {
  struct sotto_dec *core;                   /* its own (sotto_decode16), or the hooked decoder's */
  int32_t n, nend;                          /* frames pushed; frames in all once the stream ended, else -1 */
  int32_t y8n;                              /* 8 kHz samples the core has handed over */
  int32_t kmax;                             /* model halves known: 0 .. kmax */
  int32_t ta;                               /* the next STFT frame for stages A and B (odd ones run) */
  int32_t outn, nflush;
  int16_t env[LM][NFB], bands[LM][16], mcls[LM];   /* per odd model half, at (k >> 1) % LM */
  int16_t code[GR], run[GR], res[GR][FBN]; /* per codec frame: voicing, concealment run, coded hiss residual */
  int8_t capd[GR];                          /* sotto 600 / 400: frames back to the one whose level caps this one's, 0 none */
  const int16_t *capx;                      /* what that cap adds per band (dB Q8), when capd is used */
  int16_t lvl[LVR][FBN];                    /* the hiss level each frame's STFT frame was given */
  uint16_t lvlok;                           /* bit per lvl row: it is set */
  int16_t pc[GR][3], cur[3];                /* speaker shape: per frame, and the reader's current */
  uint16_t got;                             /* side channel reader (hiss_band._read_pc): the bits read */
  int16_t ngot, ok, own;
  int32_t lv[16], wv;
  int32_t g[NG][FBN];                       /* band amplitudes at odd frame t's centre, at (t - 1) / 2, Q16 */
  int32_t fs[FBS][2];                       /* filter states (transposed direct form II) */
  int16_t xr[XR], sr[SR8];                  /* rings of y8 - s and s */
  uint32_t rng;
};

static void dec_init(struct sotto_dec16 *d, const struct sotto_dec *core, int own) {
  d->core = (struct sotto_dec *)core;
  d->own = (int16_t)own;
  d->nend = -1;
  d->kmax = -1;
  d->ok = 1;
  d->rng = 0x9E3779B9u;
}

struct sotto_dec16 *sotto_dec16_create(void) {
  struct sotto_dec16 *d = calloc(1, sizeof *d);
  struct sotto_dec *core;
  if (!d) return NULL;
  if (!(core = sotto_dec_create())) {
    free(d);
    return NULL;
  }
  dec_init(d, core, 1);
  return d;
}

void sotto_dec16_destroy(struct sotto_dec16 *d) {
  if (!d) return;
  if (d->own) sotto_dec_destroy(d->core);
  free(d);
}
unsigned sotto_fx_dec16_bytes(void) { return (unsigned)sizeof(struct sotto_dec16); }

static int32_t dec_frame_of(const struct sotto_dec16 *d, int32_t t) {
  int32_t f = (t + HB_LAG) / 2;
  if (d->nend >= 0 && f > d->nend - 1) f = d->nend - 1;
  return f;
}

#define MODEL_REF DB(-27.6)

/* np.interp of levels at uniform centres onto the 50 Hz bins, clamped, walking the bins in order:
   16 levels at 4125 + 250 i (5 bins a segment, fractions fr16) and 4 at 4500 + 1000 i (20 bins,
   fraction r / 20). 32-bit products: level steps clamped to 128 dB, fractions Q15. */
typedef struct { int seg, r; } walk_t;
static int32_t step_db(const int32_t *p, int s) {
  int32_t d = p[s + 1] - p[s];
  return d > 32767 ? 32767 : d < -32767 ? -32767 : d;
}
static int32_t lerp16(const int32_t *p, int k, walk_t *w) {   /* call for k = 80, 81, ... */
  int32_t v;
  if (k <= 82) return w->seg = 0, w->r = 0, p[0];
  if (k >= 158) return p[15];
  v = p[w->seg] + ((step_db(p, w->seg) * (fr16[w->r] >> 1) + 16384) >> 15);
  if (++w->r == 5) w->r = 0, w->seg++;
  return v;
}
static int32_t lerp4(const int32_t *p, int k, walk_t *w) {
  int32_t v;
  if (k <= 90) return w->seg = 0, w->r = 1, p[0];
  if (k >= 150) return p[3];
  v = p[w->seg] + ((step_db(p, w->seg) * (w->r * 1638) + 16384) >> 15);   /* r / 20, Q15 */
  if (++w->r == 20) w->r = 0, w->seg++;
  return v;
}

static void dec_stage_a(struct sotto_dec16 *d, int32_t t, tframe_t *s) {
  int32_t k = t + HB_LAG, f = dec_frame_of(d, t), src = -1;
  int32_t nlast = d->nend >= 0 ? d->nend - 1 : d->n - 1;
  const int16_t *E, *B, *pc;
  int v, code, run, b, c, i, o;
  int32_t x[16], pr[16], ref;
  if (k > d->kmax) k = d->kmax;             /* odd, as kmax */
  E = d->env[(k >> 1) % LM];
  B = d->bands[(k >> 1) % LM];
  v = d->mcls[(k >> 1) % LM];
  code = d->code[f % GR];
  if (code == 0) src = f;
  else if (v == 0) {
    if (f - 1 >= 0 && d->code[(f - 1) % GR] == 0) src = f - 1;
    else if (f + 1 <= nlast && d->code[(f + 1) % GR] == 0) src = f + 1;
  }
  {
    int32_t E32[NFB];
    for (b = 0; b < NFB; b++) E32[b] = E[b];
    guess(v, E32, s->p);
  }
  s->mask = 0;
  d->lvlok &= (uint16_t)~(1u << (f % LVR));   /* each codec frame is read by one odd STFT frame */
  if (src >= 0) {
    int sg = (int)(src % GR);
    for (b = 0; b < 4; b++) s->p[b] += d->res[sg][b];
    if (d->capd[sg]) {                      /* hiss_band._uu_cap: no louder than frame h */
      int32_t h = src - d->capd[sg];
      if (h >= 0 && ((d->lvlok >> (h % LVR)) & 1))
        for (b = 0; b < 4; b++) {
          int32_t cap = d->lvl[h % LVR][b] + d->capx[b];
          if (s->p[b] > cap) s->p[b] = cap;
        }
    }
    for (b = 0; b < 4; b++) d->lvl[src % LVR][b] = sat16(s->p[b]);
    d->lvlok |= (uint16_t)(1u << (src % LVR));
    s->mask = 1;
  } else if (v == 2) {
    for (b = 0; b < 4; b++) s->p[b] += MIXED_DB;
    s->mask = 3;
  }
  run = d->run[f % GR];
  s->fade = run == 0 ? 0 : run > 4 ? DB(-80) : -DB(6) * (run - 2 > 0 ? run - 2 : 0);
  for (b = 0; b < 4; b++) s->p[b] += s->fade + (s->mask == 1 ? UV_GAIN : 0);
  s->cls = (int16_t)v;
  s->want = (int16_t)((v == 1 || v == 2) && s->mask != 1);
  ref = B[15] + MODEL_REF;
  x[0] = 256;
  for (b = 0; b < 15; b++) x[1 + b] = B[b] - B[15];
  for (o = 0; o < 16; o++) {
    fx_acc a = 0;
    for (i = 0; i < 16; i++) a += (fx_acc)x[i] * fx_hs_fine_w[i * 16 + o];
    pr[o] = fx_rshr(a, 14);
  }
  pc = d->pc[f % GR];
  for (b = 0; b < 16; b++) {
    fx_acc corr = (fx_acc)fx_hs_fine_m0[b] * 16384;
    for (c = 0; c < 3; c++) corr += (fx_acc)pc[c] * fx_hs_fine_vt[c * 16 + b];
    s->vt[b] = pr[b] + ref + fx_rshr(corr, 14);
  }
}

/* stage B: the levels per 16 kHz bin as the light layer, summed in power over each band's
   bins (hiss_band.fb_gains: 2 / N^2 sum |X|^2, the istft's power), to an amplitude for the
   band's filter */
static void dec_stage_b(struct sotto_dec16 *d, int32_t t, const tframe_t *s) {
  int b, k, j;
  int32_t lvo[16], mdb[11], wv_db = 0, *g = d->g[((t - 1) / 2) % NG];   /* the last band: 11 of its 21 bins */
  walk_t w4 = {0, 0}, w16 = {0, 0};
  if (t == 1)
    memcpy(d->lv, s->vt, sizeof d->lv);
  else
    for (b = 0; b < 16; b++) d->lv[b] = fx_rshr((fx_acc)SMOOTH2_A * d->lv[b] + (fx_acc)SMOOTH2_B * s->vt[b], 15);
  d->wv = fx_rshr((fx_acc)SMOOTH2_A * d->wv + (s->want ? (fx_acc)SMOOTH2_B * 32768 : 0), 15);
  if (d->wv > 32767) d->wv = 32767;
  if (d->wv > 0) wv_db = fx_log2_to_db_amp(fx_log2((uint32_t)d->wv) - 15 * 65536);
  for (b = 0; b < 16; b++) lvo[b] = d->lv[b] + s->fade + VOICED_TRIM;
  for (j = 0; j < FBN; j++) {                /* band j: bins 80 + 20 j .. 99 + 20 j (the last to 160) */
    int k0 = 80 + 20 * j, k1 = j == FBN - 1 ? NBIN : k0 + 20, any = 0;
    int32_t p;
    for (k = k0; k < k1; k++) {
      int32_t m = INT32_MIN, c4 = lerp4(s->p, k, &w4), c16 = lerp16(lvo, k, &w16);
      if (k & 1) continue;                   /* the power sum reads every other bin (the levels
                                                are linear in dB over 250 Hz): + 3 dB below */
      if (s->mask) m = c4 + (s->mask == 3 ? MASK3_DB : 0);
      if (d->wv > 0) {
        int32_t m2 = c16 + wv_db;
        if (m2 > m) m = m2;
      }
      if (m != INT32_MIN) any = 1;
      else m = DB(-200);
      mdb[(k - k0) >> 1] = m;
    }
    if (!any) { g[j] = 0; continue; }
    p = fx_db_power_sum(mdb, (k1 - k0 + 1) >> 1) + fb_amp_db[j] + (j == FBN - 1 ? 719 : 771);   /* 10 log10(21 / 11), (20 / 10) */
    g[j] = p > DB(48) ? (1 << 19) - 1 : fx_exp2(fx_db_to_log2_amp(p), 16);   /* Q16, < 8 */
  }
}

/* one STFT frame's stages, as soon as its model half and the codes around it are in */
static void dec_run(struct sotto_dec16 *d, int32_t tneed) {
  int32_t nlast = d->nend >= 0 ? d->nend - 1 : d->n - 1;
  int32_t tmax = d->nend >= 0 ? 2 * d->nend - 2 : INT32_MAX;
  while (d->ta <= tneed && d->ta <= tmax) {
    tframe_t s;
    int32_t t = d->ta, k = t + HB_LAG, f = dec_frame_of(d, t);
    if (d->nend < 0 && (k > d->kmax || f + 1 > nlast)) break;
    if (t & 1) {
      dec_stage_a(d, t, &s);
      dec_stage_b(d, t, &s);
    }
    d->ta++;
  }
}

/* the band amplitudes at the start of the block at j0 (a multiple of 160, the centre of odd frame
   t = j0 / 80 - 1) and their step a sample to the next block's: held past the ends. Steps are
   (c - a) / 160 to 0.03 %. */
static void gains_at(const struct sotto_dec16 *d, int32_t j0, int32_t *g, int32_t *dg) {
  int32_t u = j0 / 160 - 1, last = (d->ta - 2) / 2, b;   /* the last odd frame run: 2 last + 1 */
  for (b = 0; b < FBN; b++) {
    int32_t a, c;
    if (d->ta < 2) { g[b] = dg[b] = 0; continue; }
    if (u < 0 || u >= last) { g[b] = d->g[(u < 0 ? 0 : last) % NG][b], dg[b] = 0; continue; }
    a = d->g[u % NG][b], c = d->g[(u + 1) % NG][b];
    dg[b] = ((c - a) * 205 + 16384) >> 15;   /* g < 2^19: (c - a) 205 < 2^27 */
    g[b] = a;
  }
}

static int16_t th15[11];
/* the interpolator on the ring r at r[(b + 0 .. b + 20) & (XR - 1)], y8 at b + 10; as the light layer */
static void up2_q15(const int16_t *r, int32_t b, int16_t *y) {
#define XRI(i) r[(b + (i)) & (XR - 1)]
  int32_t o = 0, e = th15[0] * (int32_t)XRI(10);
  int k;
  if (!th15[0]) {
    th15[0] = (int16_t)((fx_hs_taps[20] + 8) >> 4);
    for (k = 1; k <= 10; k++) th15[k] = (int16_t)((fx_hs_taps[41 - 2 * k] + (fx_hs_taps[41 - 2 * k] >= 0 ? 8 : -8)) / 16);
  }
  for (k = 1; k <= 10; k++) o += th15[k] * ((int32_t)XRI(k) + XRI(21 - k));
#undef XRI
  e = (e + (1 << 13)) >> 14, o = (o + (1 << 13)) >> 14;
  y[0] = (int16_t)(e > 32767 ? 32767 : e < -32768 ? -32768 : e);
  y[1] = (int16_t)(o > 32767 ? 32767 : o < -32768 ? -32768 : o);
}

/* filter f's sections (transposed direct form II, 2 states each) over N8 noise samples w; its
   output times band f's gain plus (-1)^n times its mirror band's (3 - f), into s. j0 is even. */
static void band_run(struct sotto_dec16 *d, int f, int q, const int16_t *w, int16_t *s,
                     int32_t g, int32_t dg, int32_t gm, int32_t dgm) {
  int ns = fb_nsec[f], i, k;
  int32_t z[6];
  for (k = 0; k < ns; k++) z[2 * k] = d->fs[q + k][0], z[2 * k + 1] = d->fs[q + k][1];
  for (i = 0; i < N8; i++, g += dg, gm += dgm) {
    int32_t x = w[i];
    for (k = 0; k < ns; k++) {
      const int16_t *c = fb_sec[q + k];
      int32_t y = (c[0] * x + z[2 * k] + 8192) >> 14;
      z[2 * k] = c[1] * x - c[3] * y + z[2 * k + 1];
      z[2 * k + 1] = c[2] * x - c[4] * y;
      x = y;
    }
    x = x > 4095 ? 4095 : x < -4095 ? -4095 : x;
    x = s[i] + ((((i & 1) ? g - gm : g + gm) * x + 32768) >> 16);   /* |g +- gm| < 2^20, |x| < 2^12 */
    s[i] = (int16_t)(x > 16383 ? 16383 : x < -16384 ? -16384 : x);
  }
  for (k = 0; k < ns; k++) d->fs[q + k][0] = z[2 * k], d->fs[q + k][1] = z[2 * k + 1];
}

/* the block of 8 kHz samples y8 at j0, with its hiss, as 320 samples into speech. y8 may be
   speech + N8 (the caller decoded the core there): sample i is read before outputs 2 i and
   2 i + 1 are written, and 160 + i > 2 i + 1 up to i = 158. The noise and the mirror s live on
   the stack: the layer runs after the core has returned, well inside the core's own depth. */
int sotto_hiss_mute;                        /* sotto.h: the band left out, the layer still run */

static void dec_block(struct sotto_dec16 *d, const short *y8, int32_t j0, short *speech) {
  int16_t w[N8], sb[N8];
  int32_t g[FBN], dg[FBN], i;
  dec_run(d, 2 * (j0 / N8) + 1);              /* the odd frame centred on the block's end */
  gains_at(d, j0, g, dg);
  if (sotto_hiss_mute) {                      /* an A/B on the radio: the core alone, upsampled */
    memset(sb, 0, sizeof sb);
  } else {
    for (i = 0; i < N8; i++) w[i] = (int16_t)((int16_t)(rng_next(&d->rng) >> 16) >> 5), sb[i] = 0;   /* uniform +-1024 */
    band_run(d, 0, 0, w, sb, g[0], dg[0], g[3], dg[3]);
    band_run(d, 1, fb_nsec[0], w, sb, g[1], dg[1], g[2], dg[2]);
  }
  for (i = 0; i < N8; i++) {
    int32_t j = j0 + i, s = sb[i], v = y8[i] - s, p = j - 10, e;
    int16_t y[2];
    d->xr[j & (XR - 1)] = (int16_t)(v > 32767 ? 32767 : v < -32768 ? -32768 : v);
    d->sr[j & (SR8 - 1)] = (int16_t)s;
    up2_q15(d->xr, p - 10, y);                /* before the start the ring is still zeros */
    e = y[0] + (p >= 0 ? 2 * d->sr[p & (SR8 - 1)] : 0);
    speech[2 * i] = (int16_t)(e > 32767 ? 32767 : e < -32768 ? -32768 : e);
    speech[2 * i + 1] = y[1];
  }
  d->outn += N16;
}

/* the odd half of core frame n's output (the layer reads odd halves only: frame t odd reads
   half t + 2): its envelope, model bands and class */
static void dec_take_core(struct sotto_dec16 *d, int32_t n) {
  const fx_dec_info_t *info = sotto_dec_info(d->core);
  int32_t k = 2 * (n - 1) + 1, i = (k >> 1) % LM;
  int b;
  for (b = 0; b < NFB; b++) d->env[i][b] = sat16(info->env[1][b]);
  for (b = 0; b < 16; b++) d->bands[i][b] = sat16(info->bands[0][b]);
  d->mcls[i] = info->cls[1];
  d->kmax = k;
}

/* hiss_band.unpack_pc on the 16 bits read, 1 when the CRC holds */
static int unpack_pc16(uint16_t bits, int16_t *pc) {
  int16_t b[MSG_BITS];
  int32_t v[3];
  int i;
  for (i = 0; i < MSG_BITS; i++) b[i] = (int16_t)((bits >> i) & 1);
  if (!unpack_pc(b, v)) return 0;
  for (i = 0; i < 3; i++) pc[i] = sat16(v[i]);
  return 1;
}

/* one core frame pushed: its 160 samples y8 (the core's output of this call), after which the
   core's info is read; the frame's voicing code, its 4-band hiss residual (dB Q8), concealment
   run, side-channel bit (read when carrier says the frame carries one and its bits arrived,
   sgood), and capd (sotto 600 / 400: frames back to the one whose level caps this one's, 0
   none). One block out, silence for the first ALIGN_CALLS pushes. */
static void dec_push(struct sotto_dec16 *d, const short *y8, int code, const int32_t *res, int run, int side,
                     int carrier, int sgood, int capd, short *speech) {
  int32_t n = d->n;
  int ng = (int)(n % GR), b;
  if (n >= 1) dec_take_core(d, n);
  d->code[ng] = (int16_t)code;
  d->run[ng] = (int16_t)run;
  d->capd[ng] = (int8_t)capd;
  for (b = 0; b < FBN; b++) d->res[ng][b] = sat16(res[b]);
  if (n % WIN_FRAMES == 0) d->ngot = 0, d->ok = 1;
  memcpy(d->pc[ng], d->cur, sizeof d->cur);
  if (d->ok && d->ngot < MSG_BITS) {
    if (!sgood) d->ok = 0;
    else if (carrier) {
      if (side) d->got |= (uint16_t)(1u << d->ngot);
      else d->got &= (uint16_t)~(1u << d->ngot);
      if (++d->ngot == MSG_BITS && unpack_pc16(d->got, d->cur)) memcpy(d->pc[ng], d->cur, sizeof d->cur);
    }
  }
  d->n++;
  if (n >= ALIGN_CALLS) {
    dec_block(d, y8, d->y8n, speech);
    d->y8n += N8;
  } else
    memset(speech, 0, N16 * sizeof(short));
}

/* the core's last output (its flush), in speech + N8: the stream has ended; one block out */
static void dec_end(struct sotto_dec16 *d, short *speech) {
  dec_take_core(d, d->n);
  d->nend = d->n;
  if (d->n < ALIGN_CALLS) memset(speech + N8, 0, N8 * sizeof(short));   /* still the core's start-up delay */
  dec_block(d, speech + N8, d->y8n, speech);
  d->y8n += N8;
  d->nflush = 1;
}

/* after the end: the layer's remaining blocks (silence in, the last hiss and the interpolator's
   tail out), one per call; 0 when there are none */
static int dec_tail(struct sotto_dec16 *d, short *speech) {
  if (d->nend < 0 || d->nflush >= SOTTO_DEC16_DELAY_FRAMES) return 0;
  memset(speech + N8, 0, N8 * sizeof(short));
  dec_block(d, speech + N8, d->y8n, speech);
  d->y8n += N8;
  d->nflush++;
  return 1;
}

void sotto_decode16(struct sotto_dec16 *d, const unsigned char in[SOTTO_BYTES_PER_FRAME], int good,
                    short speech[SOTTO_SAMPLES_PER_FRAME_16K]) {
  const fx_dec_info_t *info;
  int32_t res[4];
  int code, b;
  sotto_decode(d->core, in, good, speech + N8);
  info = sotto_dec_info(d->core);
  code = frame_code(info->b2);
  for (b = 0; b < 4; b++) res[b] = fx_hs_cb6[index6(info->b2) * 4 + b];
  dec_push(d, speech + N8, code, res, info->run, frame_bit(in, 8), code == 0, good, 0, speech);
}

int sotto_dec16_flush(struct sotto_dec16 *d, short speech[SOTTO_SAMPLES_PER_FRAME_16K]) {
  if (d->n == 0) return 0;
  if (d->nend < 0) {
    sotto_dec_flush(d->core, speech + N8);
    dec_end(d, speech);
    return 1;
  }
  return dec_tail(d, speech);
}

/* ------------------------------------------------- sotto 1200 hiss (the K6's sotto 1200 at 16 kHz)
   The fixed-point sotto 1200 decoder (sotto_fx_lr.c, built with FX_HISS_FB) reads the hiss
   fields as the float one does (sotto_lr_dec.inc, LR_HISS) and calls the hook after each core
   frame; the layer makes that frame's block at once. 20 ms halves, as the K6 plays them. */
struct sotto_lr_dec16 {
  struct sotto_dec16 h;
  struct sotto_lr_dec *lr;
  short *out;                               /* the 320-sample block of the call in progress */
  int16_t got;                              /* the hook ran in this call */
};

static void lr_hook(void *ctx, int code, const int32_t *res, int side, int sgood, int run, const short *out,
                    int flushed) {
  struct sotto_lr_dec16 *L = ctx;
  (void)out;                                /* = L->out + N8: the core decoded there */
  L->got = 1;
  if (flushed) dec_end(&L->h, L->out);
  else dec_push(&L->h, L->out + N8, code, res, run, side, code == 0, sgood, 0, L->out);
}

struct sotto_lr_dec16 *sotto_lr_dec16_create(void) {
  struct sotto_lr_dec16 *L = calloc(1, sizeof *L);
  if (!L) return NULL;
  if (!(L->lr = sotto_lr_dec_create())) {
    free(L);
    return NULL;
  }
  dec_init(&L->h, sotto_lr_dec_core(L->lr), 0);
  sotto_lr_dec_set_hook(L->lr, lr_hook, L);
  return L;
}

void sotto_lr_dec16_destroy(struct sotto_lr_dec16 *L) {
  if (!L) return;
  sotto_lr_dec_destroy(L->lr);
  free(L);
}

unsigned sotto_fx_lr_dec16_bytes(void) { return (unsigned)sizeof(struct sotto_lr_dec16); }

int sotto_lr_decode16_half(struct sotto_lr_dec16 *L, const unsigned char in[SOTTO_BYTES_PER_FRAME], int good,
                           int half, short speech[SOTTO_SAMPLES_PER_FRAME_16K]) {
  int r;
  L->out = speech, L->got = 0;
  r = sotto_lr_decode_half(L->lr, in, good, half, speech + N8);
  if (!L->got) memset(speech, 0, N16 * sizeof(short));   /* no core frame this half: the start */
  return r;
}

int sotto_lr_dec16_flush_half(struct sotto_lr_dec16 *L, int half, short speech[SOTTO_SAMPLES_PER_FRAME_16K]) {
  int r;
  L->out = speech, L->got = 0;
  r = sotto_lr_dec_flush_half(L->lr, half, speech + N8);
  if (L->got) return 1;
  if (dec_tail(&L->h, speech)) return 1;
  memset(speech, 0, N16 * sizeof(short));
  return r;
}

void sotto_lr_decode16(struct sotto_lr_dec16 *L, const unsigned char in[SOTTO_BYTES_PER_FRAME], int good,
                       short speech[SOTTO_LR_SAMPLES_PER_FRAME_16K]) {
  sotto_lr_decode16_half(L, in, good, 0, speech);
  sotto_lr_decode16_half(L, NULL, 0, 1, speech + N16);
}

int sotto_lr_dec16_flush(struct sotto_lr_dec16 *L, short speech[SOTTO_LR_SAMPLES_PER_FRAME_16K]) {
  int a = sotto_lr_dec16_flush_half(L, 0, speech);
  int b = sotto_lr_dec16_flush_half(L, 1, speech + N16);
  return a || b;
}

/* --------------------------------------------- sotto 600 / 400 hiss (the K6's at 16 kHz)
   SottoVLRHiss (sim/sotto_vlr_hiss.py) as ../sotto_hiss.c reads it, in integers: the pitch
   fields that carry nothing hold the hiss. Kind 0, no pitched frame in the voicing pattern:
   pitch centre = 6-bit index << 1 | side bit, slope = a level slope across the superframe
   (q = cb6[index] + slope x t). Kind 1, one pitched frame: its pitch is the centre, the slope =
   2-bit offset << 1 | side bit over the neighbours' hiss. Kind 2, two or more (and lost
   superframes): the neighbours' hiss plus k2. The neighbours' hiss of frame g is the mean of
   the nearest kind-0 frame before and after within 2 superframes, h0 when there is none. One
   side bit per kind 0 or 1 superframe, in its first unvoiced frame (the "carrier"). A kind-2
   frame's hiss is capped at the last frame before it with hiss bits + k2 + 4 dB, and with no
   such frame its residual at k2 + 4 dB (decoder only). */
#define VLR_REACH 2                         /* SottoVLRHiss.REACH, superframes */
#define VLR_CAP_DB DB(4)                    /* SottoVLRHiss.CAP_DB, RES_CAP */
#define VRING 5                             /* superframes received: s - 2 .. s + 2 (s plays when s + 2 is in) */

typedef struct {                            /* a superframe's hiss fields */
  int8_t good, kind;
  int16_t v, wc, ws;
} vlr_hf_t;

struct sotto_vlr_dec16 {
  struct sotto_dec16 h;
  struct sotto_vlr_dec *vlr;
  const vlr_mode_t *m;
  int16_t mi;                               /* the hiss tables' row: 0 sotto 600, 1 sotto 400 */
  int16_t taken;                            /* the superframe playing has met its carrier */
  int16_t got;
  int16_t capx[FBN];                        /* k2 + 4 dB */
  int32_t nin;                              /* superframes received */
  vlr_hf_t sf[VRING];
  short *out;
};

static int vlr_field(const unsigned char *b, int pos, int w) {
  int x = 0, i;
  for (i = 0; i < w; i++) x = (x << 1) | frame_bit(b, pos + i);
  return x;
}

/* SottoVLRHiss.kind on the unpacked fields */
static void vlr_parse(const vlr_mode_t *m, const unsigned char *b, int good, vlr_hf_t *f) {
  int np = 0, k;
  f->good = (int8_t)(good != 0);
  f->v = (int16_t)vlr_field(b, 0, m->bv);
  f->wc = (int16_t)vlr_field(b, m->bv, 7);
  f->ws = (int16_t)vlr_field(b, m->bv + 7, m->bs);
  for (k = 0; k < m->n; k++) np += m->vpat[f->v * m->n + k] != 0;
  f->kind = (int8_t)(np == 0 ? 0 : np == 1 ? 1 : 2);
}

/* frame k of a kind-0 superframe: cb6[wc >> 1] + slope[ws] x t, t = k - (n - 1) / 2 */
static void vlr_q(const vlr_mode_t *m, int mi, const vlr_hf_t *f, int k, int32_t *q) {
  int32_t sl = fx_vlr_hiss_slope[mi * 8 + f->ws] * (2 * k - (m->n - 1));   /* twice slope x t */
  int b;
  for (b = 0; b < 4; b++) q[b] = fx_hs_cb6[(f->wc >> 1) * 4 + b] + fx_rshr32(sl, 1);
}

/* SottoVLRHiss._base: the mean of the nearest kind-0 frame before and after g among superframes
   0 .. nsf - 1; returns how many were found */
static int vlr_base(const vlr_hf_t *ring, int32_t nsf, const vlr_mode_t *m, int mi, int32_t g, int32_t *base) {
  int32_t n = m->n, r = VLR_REACH * n, h;
  int nx = 0, dir, b;
  for (b = 0; b < 4; b++) base[b] = 0;
  for (dir = -1; dir <= 1; dir += 2)
    for (h = g + dir; h >= 0 && h < nsf * n && (h > g ? h - g : g - h) <= r; h += dir) {
      const vlr_hf_t *f = &ring[(h / n) % VRING];
      if (f->good && f->kind == 0) {
        int32_t q[4];
        vlr_q(m, mi, f, (int)(h % n), q);
        for (b = 0; b < 4; b++) base[b] += q[b];
        nx++;
        break;
      }
    }
  if (nx == 2)
    for (b = 0; b < 4; b++) base[b] = fx_rshr32(base[b], 1);
  return nx;
}

/* SottoVLRHiss._back: the nearest unvoiced frame before g, within reach and in an earlier
   superframe, that has hiss bits (a received kind-0 or kind-1 superframe); -1 if none */
static int32_t vlr_back(const vlr_hf_t *ring, const vlr_mode_t *m, int32_t g) {
  int32_t n = m->n, h;
  for (h = g - 1; h >= 0 && g - h <= VLR_REACH * n; h--) {
    const vlr_hf_t *f = &ring[(h / n) % VRING];
    if (h / n != g / n && f->good && f->kind <= 1 && !m->vpat[f->v * n + h % n]) return h;
  }
  return -1;
}

/* SottoVLRHiss.decode_audio16 for frame g as the decoder plays it */
static void vlr_hook(void *ctx, int g, int code, int run, int avail, int reb, const short *out, int flushed) {
  struct sotto_vlr_dec16 *V = ctx;
  const vlr_mode_t *m = V->m;
  int k, mi = V->mi, carrier = 0, side = 0, capd = 0, b;
  const vlr_hf_t *f;
  int kind;
  int32_t res[4] = {0, 0, 0, 0};
  (void)out;
  V->got = 1;
  if (flushed) {
    dec_end(&V->h, V->out);
    return;
  }
  k = g % m->n;
  f = &V->sf[(g / m->n) % VRING];
  kind = f->good ? f->kind : 2;
  if (k == 0) V->taken = 0;
  if (code == 0) {
    if (kind == 0) {
      vlr_q(m, mi, f, k, res);
    } else {
      int32_t base[4];
      int nx = vlr_base(V->sf, V->nin, m, mi, g, base);
      for (b = 0; b < 4; b++)
        res[b] = kind == 1 ? (nx ? base[b] : fx_vlr_hiss_h0[mi * 4 + b]) + fx_vlr_hiss_off[mi * 16 + (f->ws >> 1) * 4 + b]
                           : nx ? base[b] + fx_vlr_hiss_k2[mi * 4 + b] : fx_vlr_hiss_h0[mi * 4 + b];
    }
    if (kind <= 1 && !V->taken) carrier = V->taken = 1, side = (kind == 0 ? f->wc : f->ws) & 1;
    if (kind == 2) {                        /* no bits: the cap */
      int32_t h = vlr_back(V->sf, m, g);
      if (h >= 0) capd = g - h;
      else
        for (b = 0; b < 4; b++) {
          int32_t c = fx_vlr_hiss_k2[mi * 4 + b] + VLR_CAP_DB;
          if (res[b] > c) res[b] = c;
        }
    }
  }
  dec_push(&V->h, V->out + N8, code, res, run, side, carrier, avail && !reb, capd, V->out);
}

struct sotto_vlr_dec16 *sotto_vlr_dec16_create(int rate) {
  struct sotto_vlr_dec16 *V;
  int b;
  if (rate != 600 && rate != 400) return NULL;
  if (!(V = calloc(1, sizeof *V))) return NULL;
  V->m = rate == 600 ? &vlr_mode600 : &vlr_mode400;
  V->mi = (int16_t)(rate == 400);
  if (!(V->vlr = sotto_vlr_dec_create(rate))) {
    free(V);
    return NULL;
  }
  for (b = 0; b < FBN; b++) V->capx[b] = (int16_t)(fx_vlr_hiss_k2[V->mi * 4 + b] + VLR_CAP_DB);
  dec_init(&V->h, sotto_vlr_dec_core(V->vlr), 0);
  V->h.capx = V->capx;
  sotto_vlr_dec_set_hook(V->vlr, vlr_hook, V);
  return V;
}

void sotto_vlr_dec16_destroy(struct sotto_vlr_dec16 *V) {
  if (!V) return;
  sotto_vlr_dec_destroy(V->vlr);
  free(V);
}

int sotto_vlr_samples16(const struct sotto_vlr_dec16 *V) { return V->m->n * N16; }
unsigned sotto_fx_vlr_dec16_bytes(void) { return (unsigned)sizeof(struct sotto_vlr_dec16); }

int sotto_vlr_decode16_frame(struct sotto_vlr_dec16 *V, const unsigned char in[SOTTO_BYTES_PER_FRAME], int good, int k,
                             short speech[SOTTO_SAMPLES_PER_FRAME_16K]) {
  int r;
  V->out = speech, V->got = 0;
  if (k == 0) vlr_parse(V->m, in, good, &V->sf[V->nin++ % VRING]);
  r = sotto_vlr_decode_frame(V->vlr, in, good, k, speech + N8);
  if (!V->got) memset(speech, 0, N16 * sizeof(short));   /* the start-up delay */
  return r;
}

int sotto_vlr_dec16_flush_frame(struct sotto_vlr_dec16 *V, short speech[SOTTO_SAMPLES_PER_FRAME_16K]) {
  int r;
  V->out = speech, V->got = 0;
  r = sotto_vlr_dec_flush_frame(V->vlr, speech + N8);
  if (V->got) return 1;
  if (dec_tail(&V->h, speech)) return 1;
  memset(speech, 0, N16 * sizeof(short));
  return r;
}

void sotto_vlr_decode16(struct sotto_vlr_dec16 *V, const unsigned char in[SOTTO_BYTES_PER_FRAME], int good,
                        short *speech) {
  int k;
  for (k = 0; k < V->m->n; k++) sotto_vlr_decode16_frame(V, in, good, k, speech + k * N16);
}

int sotto_vlr_dec16_flush(struct sotto_vlr_dec16 *V, short *speech) {
  int k, any = 0;
  for (k = 0; k < V->m->n; k++)
    if (sotto_vlr_dec16_flush_frame(V, speech + k * N16)) any = 1;
  return any;
}
#endif  /* FX_HISS_LIGHT */

#ifndef FX_HISS_NO_ENC
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
#endif  /* FX_HISS_NO_ENC */
