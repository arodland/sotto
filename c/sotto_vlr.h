/* Very-low-rate sotto: sotto 600 (48 bits / 80 ms) and sotto 400 (48 bits / 120 ms).
 * The encoder is floating point (sotto_vlr.c, in libsotto, on the v3.7 encoder core; the
 * fixed-point build's encoder create returns NULL); the decoder is fixed point
 * (fixed/sotto_fx_vlr.c, on the v3.7 decoder core). See docs/sotto-vlr.md.
 *
 * sotto_vlr_enc_create(600 or 400). sotto_vlr_encode takes n80 blocks of 80 samples (10 ms,
 * 8 kHz); then sotto_vlr_enc_pop gives each 6-byte superframe that is ready (call it until it
 * returns 0). The shape anchors are settled by a Viterbi search, so a superframe comes out
 * once every surviving path agrees on its anchors: usually one or two superframes later.
 * At the end call sotto_vlr_enc_flush until it returns 0 (the last superframe is padded by
 * repeating the last frame).
 *
 * sotto_vlr_dec_create(600 or 400) (NULL for any other rate). sotto_vlr_decode takes one
 * 6-byte superframe (good = 0: lost) and writes sotto_vlr_samples(d) samples (N x 160 at
 * 8 kHz). Its output runs SOTTO_VLR_DEC_DELAY superframes behind its input, so that a lone
 * lost superframe can be rebuilt from both neighbours, plus the v3.7 core's own 20 ms.
 * After the last superframe call sotto_vlr_dec_flush until it returns 0.
 * SPDX-License-Identifier: MIT */
#ifndef SOTTO_VLR_H
#define SOTTO_VLR_H
#include "sotto.h"

#define SOTTO_VLR_DEC_DELAY 2
#define SOTTO_VLR_MAX_SAMPLES (6 * SOTTO_SAMPLES_PER_FRAME)

struct sotto_vlr_enc;
SOTTO_API struct sotto_vlr_enc *sotto_vlr_enc_create(int rate);
SOTTO_API void sotto_vlr_enc_destroy(struct sotto_vlr_enc *e);
SOTTO_API void sotto_vlr_encode(struct sotto_vlr_enc *e, const short *speech, int n80);
SOTTO_API int sotto_vlr_enc_pop(struct sotto_vlr_enc *e, unsigned char out[SOTTO_BYTES_PER_FRAME]);
SOTTO_API int sotto_vlr_enc_flush(struct sotto_vlr_enc *e, unsigned char out[SOTTO_BYTES_PER_FRAME]);

struct sotto_vlr_dec;
SOTTO_API struct sotto_vlr_dec *sotto_vlr_dec_create(int rate);
SOTTO_API void sotto_vlr_dec_destroy(struct sotto_vlr_dec *d);
SOTTO_API int sotto_vlr_samples(const struct sotto_vlr_dec *d);
SOTTO_API void sotto_vlr_decode(struct sotto_vlr_dec *d, const unsigned char in[SOTTO_BYTES_PER_FRAME], int good,
                                short *speech);
SOTTO_API int sotto_vlr_dec_flush(struct sotto_vlr_dec *d, short *speech);

#endif
