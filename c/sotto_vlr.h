/* Very-low-rate sotto: sotto 600 (48 bits / 80 ms) and sotto 400 (48 bits / 120 ms).
 * Fixed-point decoder only for now (fixed/sotto_fx_vlr.c, on the v3.7 decoder core); the
 * encoder is the Python reference sim/sotto_vlr.py. See docs/sotto-vlr.md.
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

struct sotto_vlr_dec;
SOTTO_API struct sotto_vlr_dec *sotto_vlr_dec_create(int rate);
SOTTO_API void sotto_vlr_dec_destroy(struct sotto_vlr_dec *d);
SOTTO_API int sotto_vlr_samples(const struct sotto_vlr_dec *d);
SOTTO_API void sotto_vlr_decode(struct sotto_vlr_dec *d, const unsigned char in[SOTTO_BYTES_PER_FRAME], int good,
                                short *speech);
SOTTO_API int sotto_vlr_dec_flush(struct sotto_vlr_dec *d, short *speech);

#endif
