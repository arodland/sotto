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

/* round-to-nearest right shift (n >= 1) */
static inline int32_t fx_rshr(fx_acc x, int n) { return (int32_t)((x + ((fx_acc)1 << (n - 1))) >> n); }
/* the same for a 32-bit x (|x| < 2^31 - 1) by a variable n >= 1, with 32-bit shifts only:
   floor((floor(x / 2^(n-1)) + 1) / 2) = floor((x + 2^(n-1)) / 2^n). On the C55x a variable
   shift of a long long is a library call, of a long one instruction */
static inline int32_t fx_rshr32(int32_t x, int n) { return ((x >> (n - 1)) + 1) >> 1; }

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
