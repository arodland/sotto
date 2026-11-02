/* sotto decoder in fixed point (integer only), for the C5517 DSP.
 *
 * Same API and the same output, to within rounding, as the float decoder in ../sotto.c,
 * which uses Codec 2's phase0 synthesis. This is new code, written from the float
 * model's behaviour:
 *  - envelope: 8-bit codebook stages summed in dB, inverse DCT, rate-K postfilter;
 *  - each 10 ms half: harmonic amplitudes from the envelope at multiples of Wo, set to
 *    the frame energy and the concealment gain (all in dB, Q8);
 *  - minimum phase from the log-magnitude envelope: parabolic interpolation through the
 *    harmonics onto 65 bins, real cepstrum, folded, back to phase (two small cos/sin
 *    matrix products instead of 128-point FFTs);
 *  - excitation: a pitch-pulse phase track for voiced harmonics, random phase for the
 *    others (the same generator and call order as Codec 2), and the background-noise
 *    postfilter that randomises harmonics below the noise estimate;
 *  - synthesis: each harmonic is placed on its 512-point DFT bin, and only the 160 output
 *    samples the 10 ms triangular overlap-add needs are computed, as direct sums.
 * With SOTTO_V38 (sotto_fx_hiss.c): unvoiced halves are synthesised with harmonics 50 Hz
 * apart whatever their pitch field says (it carries a hiss index), upward-tilted ones 3 dB
 * louder, a voiced second half between two unvoiced frames keeps its own pitch, and the
 * envelope at the hiss band's reference frequencies is kept for the layer (fx_dec_info_t).
 * SPDX-License-Identifier: MIT */
#define SOTTO_BUILD
#include <stdlib.h>
#include <string.h>
#include "../sotto.h"
#include "fx.h"
#include "fx_internal.h"
#include "fx_tables.h"

#ifdef SOTTO_V38
#define V38 1
#else
#define V38 0
#endif
#define UV_GAIN DB(3)

#define NSAMP 80
#define K FX_K
#define NC FX_NC
#define MAXL 80               /* harmonics below 4 kHz at the lowest pitch (Wo = 2 pi / 160) */
#define NBIN 65               /* phase bins, 0..4 kHz in 62.5 Hz steps */
#define MIXED 2
#define WO_MIN 26843546u      /* 2 pi / 160 as Q32 turns (fx_wo_tab[0]) */
#define WO_UV 53687091u       /* 2 pi / 80, rounded down so it has 40 harmonics as in float */
#define DB(x) ((int32_t)((x) * 256L))
#define PF_GAIN_Q16 88474     /* 1.35 */
#define C_HOLD 2
#define C_SLOPE_DB 6
#define C_MAX_LOST 4
#define BG_THRESH DB(40)
#define BG_MARGIN DB(6)

typedef struct {
  uint32_t wo;                /* Q32 turns per sample */
  int32_t en;                 /* frame energy, dB Q8 */
  int32_t rk[K];              /* envelope, dB Q8 */
  int16_t v[2];               /* voicing of the two halves: 0, 1 or MIXED */
  int32_t g[2];               /* concealment gain of the two halves, dB Q8 */
} dframe_t;

struct sotto_dec {
  int32_t sn[2 * NSAMP];      /* overlap-add buffer, samples Q4 */
  uint32_t ex_phase;          /* pitch-pulse phase, Q32 turns */
  int32_t bg_est;             /* background level, dB Q8 */
  uint32_t seed;              /* random phase generator */
  unsigned char last[SOTTO_BYTES_PER_FRAME];
  int16_t have_last, run, prev_v;
  int32_t n;                  /* frames received */
  dframe_t prev;
  fx_dec_info_t info;
};

const fx_dec_info_t *sotto_dec_info(const struct sotto_dec *d) { return &d->info; }
static const int32_t fb_q4[FX_NFB] = {1250L * 16, 1750L * 16, 2250L * 16, 2750L * 16, 3250L * 16, 3600L * 16, 3800L * 16};

/* mean rk below 1 kHz minus mean rk above 2 kHz, dB Q8 (hiss_band: rk[KLO].mean() - rk[KHI].mean()) */
static int32_t rk_tilt(const int32_t *rk) {
  int k, nlo = 0, nhi = 0;
  int32_t lo = 0, hi = 0;
  for (k = 0; k < K; k++) {
    if (fx_kfreq[k] < 1000 * 16) lo += rk[k], nlo++;
    if (fx_kfreq[k] > 2000 * 16) hi += rk[k], nhi++;
  }
  return lo / nlo - hi / nhi;
}

struct sotto_dec *sotto_dec_create(void) {
  struct sotto_dec *d = calloc(1, sizeof *d);
  if (d) d->seed = 1;
  return d;
}

void sotto_dec_destroy(struct sotto_dec *d) { free(d); }
unsigned sotto_fx_dec_bytes(void) { return (unsigned)sizeof(struct sotto_dec); }

/* the C library's example rand(), as Codec 2 uses: 15 bits */
static uint32_t rnd15(struct sotto_dec *d) {
  d->seed = d->seed * 1103515245u + 12345u;
  return (d->seed >> 16) & 32767u;
}
/* 2 pi r / 32767 as a Q32 phase */
static uint32_t rnd_phase(struct sotto_dec *d) { return rnd15(d) * 131076u; }

static int get_bits(const unsigned char *in, int *pos, int nb) {
  int i;
  int v = 0;
  for (i = 0; i < nb; i++, (*pos)++) v = (v << 1) | ((in[*pos >> 3] >> (7 - (*pos & 7))) & 1);
  return v;
}

/* vocoder_lab.postfilter_rk: raise the envelope's contrast, keep its power */
static void postfilter_rk(int32_t *v) {
  int k;
  int32_t e0, e1;
  for (k = 0; k < K; k++) v[k] += fx_pre[k];
  e0 = fx_db_power_sum(v, K);
  for (k = 0; k < K; k++) v[k] = fx_rshr((fx_acc)v[k] * PF_GAIN_Q16, 16);
  e1 = fx_db_power_sum(v, K);
  for (k = 0; k < K; k++) v[k] -= e1 - e0 + fx_pre[k];
}

static void unpack(struct sotto_dec *d, const unsigned char *b, dframe_t *f) {
  int s, k, n;
  int pos = 0, code = get_bits(b, &pos, 2), wi = get_bits(b, &pos, 7), ei = get_bits(b, &pos, 5);
  if (code < 3) f->v[0] = f->v[1] = (int16_t)code;
  else if (d->prev_v == 0) f->v[0] = 0, f->v[1] = MIXED;
  else f->v[0] = MIXED, f->v[1] = 0;
  d->prev_v = f->v[1];
  f->wo = fx_wo_tab[wi];
  /* 10 + ei * 70 / 31 dB */
  f->en = ei == 0 ? DB(-60) : DB(10) + (int32_t)((ei * (int32_t)17920 + 15) / 31);
  int32_t c[K] = {0};                       /* DCT coefficients, dB Q16 */
  for (s = 0; s < fx_cb_stages; s++) {
    const int16_t *y = fx_cb[s] + get_bits(b, &pos, fx_cb_bits[s]) * FX_CBW;
    for (k = 0; k < NC; k++) c[k + 1] += (int32_t)FX_CB_VAL(y, k) * fx_cb_step[s];
  }
  for (k = 1; k < K; k++) c[k] = fx_rshr(c[k], 8);          /* Q8 */
  for (n = 0; n < K; n++) {
    fx_acc a = 0;
    for (k = 1; k < K; k++) a += (fx_acc)c[k] * fx_dct[k * K + n];
    FX_ACC_CHECK(a);
    f->rk[n] = fx_rshr(a, 15);
  }
  postfilter_rk(f->rk);
}

/* np.interp of the envelope at f (Hz Q4) */
static int32_t env_at(const int32_t *rk, int32_t f) {
  if (f <= fx_kfreq[0]) return rk[0];
  if (f >= fx_kfreq[K - 1]) return rk[K - 1];
  int j = 0;
  while (fx_kfreq[j + 1] < f) j++;
  int32_t fr = (int32_t)(((fx_acc)(f - fx_kfreq[j]) * fx_kinvw[j]) >> 16);   /* Q16 */
  return rk[j] + (int32_t)(((fx_acc)(rk[j + 1] - rk[j]) * fr + 32768) >> 16);
}

/* minimum phase at the 65 bins (Q32 turns) from the harmonic levels A[1..L] (dB Q8) */
static void min_phase(const int32_t *A, int L, uint32_t wo, uint32_t *ph) {
  int i, n, j;
  int32_t G[NBIN], cc[NBIN];
  /* parabolic interpolation through 3 neighbouring harmonics (Codec 2's interp_para). With
     equal spacing, in units of harmonics: y = y2 + t (y3 - y1) / 2 + t^2 (y3 - 2 y2 + y1) / 2 */
  uint32_t rw = (uint32_t)fx_udiv_shl(1, 56, wo, NULL);   /* 2^56 / wo: bins -> harmonics, Q31 */
  int k = 0;
  for (i = 0; i < NBIN; i++) {
    int32_t u = (int32_t)(((fx_uacc)i * rw) >> 15);  /* bin i in harmonics, Q16 */
    while (((int32_t)(k + 2) << 16) < u && k < L - 3) k++;
    int32_t y1 = A[k + 1], y2 = A[k + 2], y3 = A[k + 3];
    int32_t t = u - ((int32_t)(k + 2) << 16);
    int32_t tt = (int32_t)(((fx_acc)t * t) >> 16);
    G[i] = y2 + (int32_t)(((fx_acc)t * (y3 - y1)) >> 17) + (int32_t)(((fx_acc)tt * (y3 - 2 * y2 + y1)) >> 17);
  }
  /* real cepstrum (x 128): c[n] = G0 + (-1)^n G64 + 2 sum_j G_j cos(2 pi j n / 128), Q4 */
  for (n = 1; n < 64; n++) {
    fx_acc a = (fx_acc)(G[0] + ((n & 1) ? -G[64] : G[64])) * 32768, ah = 0, al = 0;
    for (j = 1; j < 64; j++) {                 /* 2 G[j] cos(...), as two 16 x 16 products */
      int16_t c = fx_cos512[((j * n) & 127) * 4];
      ah += (fx_acc)FX_HI(G[j]) * c;
      al += (fx_acc)FX_LO(G[j]) * c;
    }
    a += FX_JOIN(ah, al) * 2;
    FX_ACC_CHECK(a);
    cc[n] = fx_rshr(a, 19);
  }
  /* folded cepstrum back to phase: phi_k = -2 / (128 * 20 / ln 10) sum_n c[n] sin(2 pi k n / 128)
     radians; in Q32 turns from Q19 sums that is x -2.34542 (153709 in Q16) */
  for (i = 0; i < NBIN; i++) {
    fx_acc a, ah = 0, al = 0;
    for (n = 1; n < 64; n++) {
      int16_t c = fx_cos512[(((i * n) & 127) * 4 - 128) & 511];
      ah += (fx_acc)FX_HI(cc[n]) * c;
      al += (fx_acc)FX_LO(cc[n]) * c;
    }
    a = FX_JOIN(ah, al);
    FX_ACC_CHECK(a);
    /* -((a x 153709) >> 16) mod 2^32: split a so no product needs more than 40 bits */
    ph[i] = (uint32_t)0 - ((uint32_t)(a >> 16) * 153709u + (uint32_t)(((a & 0xFFFF) * 153709) >> 16));
  }
}

/* one 10 ms half */
static void synth(struct sotto_dec *d, uint32_t wo, int v, const int32_t *rk, int32_t en, int32_t g,
                  short *out, int half) {
  int h, i, j;
  int32_t A[MAXL + 1] = {0};                 /* harmonic levels, dB Q8 */
  uint32_t phi[MAXL + 1], ph[NBIN];
  if (!v && (V38 || wo < WO_MIN)) wo = WO_MIN;   /* v3.8: unvoiced noise harmonics 50 Hz apart */
  int L = (int)(0x80000000u / wo);          /* 8 (500 Hz) .. 80 */
  if (L > MAXL) L = MAXL;
  for (h = 1; h <= L; h++)
    A[h] = env_at(rk, (int32_t)(((fx_uacc)h * wo * 125) >> 22));   /* h f0, Hz Q4 */
  int32_t gain = en - fx_db_power_sum(A + 1, L) + g;
  if (V38 && !v && rk_tilt(rk) < 0) gain += UV_GAIN;   /* a real fricative */
  for (h = 1; h <= L; h++) A[h] += gain;
  {   /* for the hiss band: the levels interpolated at FB (hiss_band.model_env), and the class */
    int32_t f0 = (int32_t)(((fx_uacc)wo * 125) >> 22);   /* Hz Q4, >= 800 */
    for (j = 0; j < FX_NFB; j++) {
      int32_t fb = fb_q4[j];
      int32_t hh = fb / f0;                  /* harmonic at or below fb */
      if (hh < 1) d->info.env[half][j] = A[1];
      else if (hh >= L) d->info.env[half][j] = A[L];
      else {
        uint32_t fr = (((uint32_t)(fb - hh * f0)) << 16) / (uint32_t)f0;   /* Q16 */
        d->info.env[half][j] = A[hh] + (int32_t)(((fx_acc)(A[hh + 1] - A[hh]) * (int32_t)fr + 32768) >> 16);
      }
    }
    d->info.cls[half] = (int16_t)v;
  }
  int vc = v > 0;                            /* voiced harmonics: all (1), none (0), m < vc */
  if (v == MIXED) {
    vc = (int)(0x20000000u / wo) + 1;        /* 1 kHz / f0 + 1 */
    if (vc < 2) vc = 2;
  }
  FX_PROF(11, 1);
  min_phase(A, L, wo, ph);
  FX_PROF(11, 0);
  d->ex_phase += wo * NSAMP;
  for (h = 1; h <= L; h++) {
    uint32_t b = ((uint32_t)h * wo + ((uint32_t)1 << 24)) >> 25;
    uint32_t ex = (vc && (vc == 1 || h < vc)) ? (uint32_t)h * d->ex_phase : rnd_phase(d);
    phi[h] = ex + ph[b];
  }
  /* background-noise postfilter */
  int32_t e = fx_db_power_sum(A + 1, L) - fx_log2_to_db_pow(fx_log2((uint32_t)L));
  if (e < BG_THRESH && !vc) d->bg_est = fx_rshr((fx_acc)d->bg_est * 58982 + (fx_acc)e * 6554, 16);
  if (vc)
    for (h = 1; h <= L; h++)
      if (A[h] < d->bg_est + BG_MARGIN) phi[h] = rnd_phase(d);
  /* harmonics on 512-point bins, amplitudes 2 A in a common Q(qa) so the largest is <= 2^14 */
  int32_t amax = L > 0 ? A[1] : 0;
  for (h = 2; h <= L; h++)
    if (A[h] > amax) amax = A[h];
  int32_t l2 = fx_db_to_log2_amp(amax);
  int qa = 13 - (int)((l2 + 65535) >> 16);
  if (qa > 30) qa = 30;
  int16_t bin[MAXL], re[MAXL], im[MAXL];
  int nb = 0;
  for (h = 1; h <= L; h++) {
    int16_t b = (int16_t)(((uint32_t)h * wo + ((uint32_t)1 << 22)) >> 23);
    if (b > 255) b = 255;
    int32_t a = fx_exp2(fx_db_to_log2_amp(A[h]) + 65536, qa);
    if (nb && bin[nb - 1] == b) nb--;        /* a later harmonic on the same bin replaces it */
    bin[nb] = b;
    re[nb] = (int16_t)fx_rshr(fx_mul32x16(a, fx_cos(phi[h])), 15);
    im[nb] = (int16_t)fx_rshr(fx_mul32x16(a, fx_sin(phi[h])), 15);
    nb++;
  }
  /* overlap-add (Codec 2's synthesise with shift): samples 433..511 of this frame's
     inverse DFT finish the previous half, samples 0..80 start the next */
  FX_PROF(12, 1);
  for (i = 0; i < NSAMP - 1; i++) d->sn[i] = d->sn[i + NSAMP];
  d->sn[NSAMP - 1] = 0;
  for (i = 0; i < 2 * NSAMP; i++) {
    int n = i < NSAMP - 1 ? 512 - NSAMP + 1 + i : i - (NSAMP - 1);
    fx_acc a = 0;
    for (j = 0; j < nb; j++) {
      int x = (int)(((unsigned)bin[j] * (unsigned)n) & 511u);   /* wraps at 16 bits on the C55x */
      a += (fx_acc)re[j] * fx_cos512[x] - (fx_acc)im[j] * fx_cos512[(x - 128) & 511];
    }
    FX_ACC_CHECK(a);
    int32_t s4 = fx_rshr(a, qa + 11);        /* Q(qa + 15) -> samples Q4 */
    int32_t w = (int32_t)(fx_mul32x16(s4, fx_pn[i]) >> 14);
    if (i < NSAMP - 1) d->sn[i] += w;
    else d->sn[i] = w;
  }
  FX_PROF(12, 0);
  for (i = 0; i < NSAMP; i++) {
    int32_t x = d->sn[i] >= 0 ? d->sn[i] >> 4 : -((-d->sn[i]) >> 4);   /* truncate, as (short) */
    out[i] = (short)(x > 32767 ? 32767 : x < -32767 ? -32767 : x);
  }
}

static void synth_pair(struct sotto_dec *d, const dframe_t *p, const dframe_t *n, short *out) {
  int k;
  synth(d, p->wo, p->v[0], p->rk, p->en, p->g[0], out, 0);
  /* odd half: interpolated with the next frame */
  const dframe_t *q = n ? n : p;
  int32_t rk[K];
  uint32_t wo;
  int va = p->v[0], vi = p->v[1], vb = n ? n->v[0] : p->v[1];
  for (k = 0; k < K; k++) rk[k] = (p->rk[k] + q->rk[k]) >> 1;
  if (vi && va && vb) wo = (p->wo >> 1) + (q->wo >> 1) + (p->wo & q->wo & 1);
  else if (vi) wo = (va || (V38 && !vb)) ? p->wo : q->wo;   /* interp_wo, guard in v3.8 */
  else wo = WO_UV;
  synth(d, wo, vi, rk, (p->en + q->en) >> 1, p->g[1], out + NSAMP, 1);
}

/* concealment gain, dB Q8: hold 2 frames, then 6 dB per frame, silent after 4 */
static int32_t conceal_gain(int run, int half) {
  if (run == 0) return 0;
  if (run > C_MAX_LOST) return DB(-80);
  int32_t x2 = 2 * (run - C_HOLD) + half;   /* twice (run - hold + half / 2) */
  return x2 > 0 ? -x2 * DB(C_SLOPE_DB) / 2 : 0;
}

void sotto_decode(struct sotto_dec *d, const unsigned char in[SOTTO_BYTES_PER_FRAME], int good,
                  short speech[SOTTO_SAMPLES_PER_FRAME]) {
  int h;
  const unsigned char *b = in;
  if (good) {
    memcpy(d->last, in, SOTTO_BYTES_PER_FRAME);
    d->have_last = 1;
    d->run = 0;
  } else {
    d->run++;
    if (d->have_last) b = d->last;
  }
  dframe_t f;
  memcpy(d->info.b2, b, SOTTO_BYTES_PER_FRAME);
  d->info.run = d->run;
  FX_PROF(10, 1);
  unpack(d, b, &f);
  FX_PROF(10, 0);
  for (h = 0; h < 2; h++) f.g[h] = conceal_gain(d->run, h);
  if (d->n == 0) memset(speech, 0, SOTTO_SAMPLES_PER_FRAME * sizeof(short));
  else synth_pair(d, &d->prev, &f, speech);
  d->prev = f;
  d->n++;
}

void sotto_dec_flush(struct sotto_dec *d, short speech[SOTTO_SAMPLES_PER_FRAME]) {
  if (d->n == 0) {
    memset(speech, 0, SOTTO_SAMPLES_PER_FRAME * sizeof(short));
    return;
  }
  synth_pair(d, &d->prev, NULL, speech);
  d->n = 0;
}
