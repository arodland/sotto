/* Shared between sotto.c (v3.7 core) and sotto_hiss.c (v3.8 layer). Not installed.
 * SPDX-License-Identifier: MIT */
#ifndef SOTTO_INTERNAL_H
#define SOTTO_INTERNAL_H
#include "sotto.h"

#define SOTTO_K 20                    /* rate-K envelope points */
#define SOTTO_NFB 7                   /* hiss_band.FB: 1250 .. 3800 Hz */

/* the fields of one 20 ms frame as the decoder core takes them (sotto 1200 builds them) */
typedef struct {
  int code, wi, ei;                   /* voicing code, 7-bit pitch index, 5-bit energy code */
  double c[SOTTO_K - 1];              /* shape: DCT coefficients 1..K-1, dB */
} sotto_fparams_t;

/* one 20 ms frame out of the v3.7 encoder core, with what the v3.8 rules need per 10 ms half */
typedef struct {
  unsigned char bits[SOTTO_BYTES_PER_FRAME];
  double en[2];                       /* mean of the rate-K envelope, dB (hiss_band._onsets) */
  double tilt[2];                     /* mean rk below 1 kHz - mean rk above 2 kHz, dB */
  double snr[2];                      /* harmonic SNR below 1 kHz; NAN when unvoiced */
  double lwo[2];                      /* log of the tracked Wo (pitch jitter) */
  int loud[2];                        /* above the causal loudness reference */
  sotto_fparams_t p;                  /* the frame's fields before shape quantization (c = target) */
} sotto_enc_frame_t;

/* what the v3.8 decoder layer reads back after each core frame */
typedef struct {
  double env[2][SOTTO_NFB];           /* dB envelope at FB of the two halves just synthesised */
  int cls[2];                         /* their class: 0 unvoiced, 1 voiced, 2 mixed */
  unsigned char b2[SOTTO_BYTES_PER_FRAME];   /* the bits the frame just received decodes (concealed) */
  int run;                            /* lost frames in a row, 0 for a good frame */
} sotto_dec_info_t;

void sotto_enc_analyse_block(struct sotto_enc *e, const short x8[80]);
void sotto_enc_flush_core(struct sotto_enc *e);
int sotto_enc_pop_frame(struct sotto_enc *e, sotto_enc_frame_t *f);
const sotto_dec_info_t *sotto_dec_info(const struct sotto_dec *d);
void sotto_dec_frame(struct sotto_dec *d, const sotto_fparams_t *p, int run, short speech[160]);
double sotto_env_db(const double *rk, double hz);   /* np.interp(hz, KFREQ, rk) */
#endif
