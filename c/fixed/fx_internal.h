/* Shared between the fixed-point core (sotto_fx_enc.c, sotto_fx_dec.c) and the v3.8 layer
 * (sotto_fx_hiss.c): the integer twin of ../sotto_internal.h. Not installed.
 * SPDX-License-Identifier: MIT */
#ifndef SOTTO_FX_INTERNAL_H
#define SOTTO_FX_INTERNAL_H
#include "../sotto.h"
#include "fx.h"

#define FX_NFB 7                      /* hiss_band.FB: 1250 .. 3800 Hz */
#define FX_SNR_NONE INT32_MIN         /* snr of an unvoiced half (the float's NAN) */

/* one 20 ms frame out of the v3.7 encoder core, with what the v3.8 rules need per 10 ms half */
typedef struct {
  unsigned char bits[SOTTO_BYTES_PER_FRAME];
  int32_t en[2];                      /* mean of the rate-K envelope, dB Q8 (hiss_band._onsets) */
  int32_t tilt[2];                    /* mean rk below 1 kHz - mean rk above 2 kHz, dB Q8 */
  int32_t snr[2];                     /* harmonic SNR below 1 kHz, dB Q8; FX_SNR_NONE when unvoiced */
  int32_t lwo[2];                     /* ln of the tracked Wo in radians, Q16 (pitch jitter) */
  int16_t loud[2];                    /* above the causal loudness reference */
} fx_enc_frame_t;

/* what the v3.8 decoder layer reads back after each core frame */
typedef struct {
  int32_t env[2][FX_NFB];             /* dB Q8 envelope at FB of the two halves just synthesised */
  int16_t cls[2];                     /* their class: 0 unvoiced, 1 voiced, 2 mixed */
  unsigned char b2[SOTTO_BYTES_PER_FRAME];   /* the bits the frame just received decodes (concealed) */
  int16_t run;                        /* lost frames in a row, 0 for a good frame */
} fx_dec_info_t;

void sotto_enc_analyse_block(struct sotto_enc *e, const short x8[80]);
void sotto_enc_flush_core(struct sotto_enc *e);
int sotto_enc_pop_frame(struct sotto_enc *e, fx_enc_frame_t *f);
const fx_dec_info_t *sotto_dec_info(const struct sotto_dec *d);
#endif
