/* Fixed-point basics for the sotto DSP build.
 *
 * Written for the TI C55x (no FPU, 16-bit int and char, 32-bit long, 40-bit accumulators)
 * and tested on x86. Rules that keep the two the same:
 *  - only exact-width types (int16_t, int32_t, uint32_t) for values; plain int only for
 *    loop counters and small indices (|x| < 32768);
 *  - sums of products go in fx_acc. On x86 that is int64_t. On the C55x it is the 40-bit
 *    accumulator, so every fx_acc sum must stay below 2^39 in magnitude. Build with
 *    -DFX_CHECK to check that bound at run time (fx_acc_check);
 *  - the C55x has no 64-bit type (long long is 40 bits), so no value may need more than
 *    40 bits, scalar steps included: products of two 32-bit values go through the helpers
 *    below or are pre-shifted. fx_uacc is the unsigned 40-bit type;
 *  - int is 16 bits on the C55x: shifts of 1 by more than 14 use a 32-bit 1, and products
 *    of two ints must stay below 2^15;
 *  - the TI compiler is C89 with mixed declarations: no declarations in for (...);
 *  - formats are written Qn: an integer x stands for x / 2^n.
 *
 * Common formats in this code:
 *  - levels in dB, Q8 in int32_t;
 *  - frequencies as turns per sample, Q32 in uint32_t (2^32 = one turn, 2 pi radians), so
 *    phase accumulators wrap for free;
 *  - log2 values, Q16 in int32_t.
 * SPDX-License-Identifier: MIT */
#ifndef SOTTO_FX_H
#define SOTTO_FX_H
#include <stdint.h>

#ifdef __TMS320C55X__
typedef long long fx_acc;              /* 40 bits */
typedef unsigned long long fx_uacc;
#else
typedef int64_t fx_acc;
typedef uint64_t fx_uacc;
#endif

#ifdef FX_CHECK
void fx_acc_check(fx_acc a);
#define FX_ACC_CHECK(a) fx_acc_check(a)
#else
#define FX_ACC_CHECK(a) ((void)0)
#endif

/* sizes of the encoder and decoder states, in bytes (C55x: 16-bit words); the 16 kHz
   v3.8 layer's states hold their own core */
unsigned sotto_fx_enc_bytes(void);
unsigned sotto_fx_dec_bytes(void);
unsigned sotto_fx_enc16_bytes(void);
unsigned sotto_fx_dec16_bytes(void);

/* optional per-block cycle counts (c55x/bench.c): FX_PROF(id, 1) starts block id,
   FX_PROF(id, 0) ends it */
#ifdef FX_PROFILE
void fx_prof(int id, int start);
#define FX_PROF(id, start) fx_prof(id, start)
#else
#define FX_PROF(id, start) ((void)0)
#endif

/* 32 x 16-bit products as two 16 x 16-bit ones, exactly: the C55x multiplies 17 x 17 bits
   in one cycle, but its compiler turns any wider product into a library call. A v with
   |v| < 2^30 is FX_HI(v) 2^15 + FX_LO(v), both halves signed 16-bit values (an unsigned
   low half miscompiles in loops with CGT 4.4.1). Loops keep the two partial sums apart
   and combine them once: FX_JOIN(hi, lo) = hi 2^15 + lo. */
#ifdef FX_CHECK
int16_t fx_hi_check(int32_t v);
#define FX_HI(v) fx_hi_check(v)
#else
#define FX_HI(v) ((int16_t)((v) >> 15))
#endif
#define FX_LO(v) ((int16_t)((v) & 0x7FFF))
#define FX_JOIN(hi, lo) ((hi) * 32768 + (lo))
static inline fx_acc fx_mul32x16(int32_t a, int16_t b) {
  return FX_JOIN((fx_acc)FX_HI(a) * b, (fx_acc)FX_LO(a) * b);
}
/* a^2 for |a| < 2^19.5 (so the result is < 2^39) */
static inline fx_acc fx_sq32(int32_t a) {
  int16_t h = FX_HI(a), l = FX_LO(a);
  FX_ACC_CHECK((fx_acc)a * a * 2);
  return ((fx_acc)h * h) * 32768 * 32768 + (fx_acc)h * l * 65536 + (fx_acc)l * l;
}

/* a b for |a|, |b| < 2^30 whose product fits 40 bits, as four 16 x 16 products (the C55x
   compiler's 32 x 32 multiply is a library call of about 90 cycles): with a = ah 2^15 + al
   and b = bh 2^15 + bl, a b = ((ah bh) 2^15 + ah bl + al bh) 2^15 + al bl, exactly */
static inline fx_acc fx_mul32(int32_t a, int32_t b) {
  int16_t ah = FX_HI(a), al = FX_LO(a), bh = FX_HI(b), bl = FX_LO(b);
  return ((fx_acc)ah * bh * 32768 + (fx_acc)ah * bl + (fx_acc)al * bh) * 32768 + (fx_acc)al * bl;
}

/* round-to-nearest right shift (n >= 1) */
static inline int32_t fx_rshr(fx_acc x, int n) { return (int32_t)((x + ((fx_acc)1 << (n - 1))) >> n); }
/* fx_rshr(a c + b s, 15) for |a|, |b| < 2^30 and |c| + |s| <= 65535, exactly, in 32-bit
   arithmetic: with a = ah 2^15 + al (al in 0..32767), a c + b s = (ah c + bh s) 2^15 +
   (al c + bl s), and both brackets fit 32 bits. No 40-bit sum, so the C55x compiler
   needn't switch accumulator modes inside a loop. */
static inline int32_t fx_q15_mac2(int32_t a, int16_t c, int32_t b, int16_t s) {
  return (int32_t)FX_HI(a) * c + (int32_t)FX_HI(b) * s + (((int32_t)FX_LO(a) * c + (int32_t)FX_LO(b) * s + 16384) >> 15);
}
/* the same for a 32-bit x (|x| < 2^31 - 1) by a variable n >= 1, with 32-bit shifts only:
   floor((floor(x / 2^(n-1)) + 1) / 2) = floor((x + 2^(n-1)) / 2^n). On the C55x a variable
   shift of a long long is a library call, of a long one instruction */
static inline int32_t fx_rshr32(int32_t x, int n) { return ((x >> (n - 1)) + 1) >> 1; }

/* loops over the bins of a spectrum (re, im: int32 arrays of n bins, |value| < 2^30); on the
   C5517 these are c55x/bins.asm, elsewhere the C in fx_math.c:
   fx_bins_max: the largest |re[b]|, |im[b]|
   fx_bins_to16: sw[2b], sw[2b+1] = the bins rounded right by s bits (s > 0) or shifted
     left by -s (the results must fit 16 bits)
   fx_bins_pow_max: the largest p(b) = sq(rshr(re[b], t)) + sq(rshr(im[b], t)) with t >= 0
     (rshr: round-to-nearest right shift, no shift for t = 0; the rounded values must be
     below 2^19 in magnitude, so a power is < 2^39)
   fx_bins_pow: p[b] = (p(b) + rnd) >> sp, the same p(b), for 0 <= sp <= 31 */
uint32_t fx_bins_max(const int32_t *re, const int32_t *im, int n);
void fx_bins_to16(const int32_t *re, const int32_t *im, int16_t *sw, int n, int s);
fx_acc fx_bins_pow_max(const int32_t *re, const int32_t *im, int n, int t);
void fx_bins_pow(const int32_t *re, const int32_t *im, uint32_t *p, int n, int t, int32_t sp, uint32_t rnd);
/* the radix-5 stage of the 160-point FFT (c55x/fft5.asm; the C is the loop in
   sotto_fx_hiss.c fft5x, m = 32): sr, si hold the five 32-point FFTs, t the twiddle step */
void fx_fft5_stage32(int32_t *re, int32_t *im, const int32_t *sr, const int32_t *si, int t);
/* the decoder's minimum-phase sums (c55x/mp_sums.asm on the C55x): the dot products of
   row[0..n1+n2) with h and l, split at n1: out = { h.row, l.row (j < n1), h.row, l.row (j >= n1) } */
void fx_mp_sums(const int16_t *h, const int16_t *l, const int16_t *row, int n1, int n2, fx_acc out[4]);
/* the decoder's overlap-add sums for samples n and n + 1 (c55x/oa_pair.asm on the C55x):
   a(n) = sum_j re[j] cos512[bin[j] n mod 512] - im[j] cos512[(bin[j] n - 128) mod 512],
   a(n) returned, a(n + 1) stored at *a1 */
fx_acc fx_oa_pair(const int16_t *bin, const int16_t *re, const int16_t *im, int nb, int n, fx_acc *a1);
/* the codebook search's residual (c55x/cb_resid.asm on the C55x): r[k] = ((c[k] - recon[k])
   istep) >> 24 clamped to +-32767 for k < n, returning the sum of the r[k]^2 */
fx_acc fx_cb_resid(const int32_t *c, const int32_t *recon, int16_t *r, int n, int32_t istep);
/* the loops of the encoder's harmonic fit over nb bins (c55x/hfit.asm on the C55x): sw
   points at the first fitted bin of the interleaved 16-bit spectrum, w at its window value.
   fx_hfit_sums: out = { sum sw[2i] w[i], sum sw[2i+1] w[i], sum w[i]^2 }
   fx_hfit_err: sum of (sw[2i] - rshr(ar w[i], 15))^2 + (sw[2i+1] - rshr(ai w[i], 15))^2
     (products as fx_mul32x16, squares as fx_sq32)
   fx_hfit_pow: sum of the squares of n values */
void fx_hfit_sums(const int16_t *sw, const int16_t *w, int nb, fx_acc out[3]);
fx_acc fx_hfit_err(const int16_t *sw, const int16_t *w, int nb, int32_t ar, int32_t ai);
fx_acc fx_hfit_pow(const int16_t *sw, int n);
/* the fit's amplitudes: q1, q2 = n1 2^15 / d, n2 2^15 / d truncated toward zero, d > 0
   rounded down to 16 bits, |q| capped at 2^31 - 1 (c55x/divq15.asm; the C version is
   div_q15_2 in sotto_fx_enc.c) */
void fx_div_q15_2(fx_acc n1, fx_acc n2, fx_acc d, int32_t *q1, int32_t *q2);

/* round(2^48 / x) for 2^20 <= x < 2^28 (sotto_fx_enc.c; C55x: c55x/recip48.asm) */
uint32_t fx_recip48_c(uint32_t x);

/* cos and sin of a Q32 phase, Q15, from the 512-point table with linear interpolation */
int16_t fx_cos(uint32_t ph);
static inline int16_t fx_sin(uint32_t ph) { return fx_cos(ph - 0x40000000u); }

/* log2(x) for x > 0, Q16 */
int32_t fx_log2(uint32_t x);
/* log2(x) of a 40-bit x > 0, Q16 */
int32_t fx_log2_acc(fx_uacc x);
/* floor(a 2^sh / d) for d < 2^36, exactly, every step inside 40 bits; *rem gets the
   remainder when rem is not NULL. The quotient must fit in 40 bits. */
fx_uacc fx_udiv_shl(fx_uacc a, int sh, fx_uacc d, fx_uacc *rem);
/* in-place radix-2 FFT (n <= 512, a power of 2), forward, output = DFT / n */
void fx_fft(int32_t *re, int32_t *im, int n);
/* its butterfly loops (c55x/fft.asm on the C55x): cnt butterflies with the one twiddle
   cs = c << 16 | (s & 0xFFFF), the pointers stepping by stride values; and the 2q
   butterflies of one group with the twiddles fx_cos512[k step], k = 0 .. 2q - 1 */
void fx_fft_bfly1(int32_t *ar, int32_t *ai, int32_t *br, int32_t *bi, int cnt, int stride, int32_t cs);
void fx_fft_bfly_grp(int32_t *ar, int32_t *ai, int32_t *br, int32_t *bi, int q, int step);
/* real-input FFT helpers (fx_math.c): pack, n2-point fx_fft, split gives bins 0..n2 of
   the n = 2 n2 real samples as DFT / n; pre, n2-point FFT, unpack gives the real DFT / n
   of a Hermitian spectrum given as bins 0..n2. ct: cos(2 pi i / n) Q15. */
void fx_rfft_pack(int32_t *re, int32_t *im, int n2);
void fx_rfft_split(int32_t *re, int32_t *im, int n2, const int16_t *ct, int n);
void fx_irfft_pre(int32_t *re, int32_t *im, int n2, const int16_t *ct, int n);
void fx_rfft_unpack(int32_t *re, const int32_t *im, int n2);
/* 2^(y / 2^16) * 2^q, rounded and saturated to int32 (q may be negative) */
int32_t fx_exp2(int32_t y, int q);

/* dB (Q8) to log2 (Q16) of the amplitude (dB / 20) or the power (dB / 10), and back */
int32_t fx_db_to_log2_amp(int32_t db);
int32_t fx_db_to_log2_pow(int32_t db);
int32_t fx_log2_to_db_amp(int32_t l2);
int32_t fx_log2_to_db_pow(int32_t l2);

/* 10 log10(sum 10^(v[i]/10)) for dB values in Q8, without overflow */
int32_t fx_db_power_sum(const int32_t *v, int n);

#endif
