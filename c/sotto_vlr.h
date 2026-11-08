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
 *
 * Or 20 ms at a time (the K6 pager, which has no room for N x 160 samples):
 * sotto_vlr_decode_frame(d, in, good, k, pcm) for k = 0 .. N-1 with the same superframe (it is
 * taken in at k = 0) writes frame k; it returns 0 (and silence) for the frames of the start-up
 * delay. Then sotto_vlr_dec_flush_frame until it returns 0. Don't mix the two ways in one stream.
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
SOTTO_API int sotto_vlr_decode_frame(struct sotto_vlr_dec *d, const unsigned char in[SOTTO_BYTES_PER_FRAME], int good,
                                     int k, short speech[SOTTO_SAMPLES_PER_FRAME]);
SOTTO_API int sotto_vlr_dec_flush_frame(struct sotto_vlr_dec *d, short speech[SOTTO_SAMPLES_PER_FRAME]);
SOTTO_API int sotto_vlr_dec_bytes(void);         /* sizeof the decoder's own state (the core's apart) */

/* ---- sotto 600 / 400 hiss: v3.8's 4-8 kHz band in the same 48 bits, 16 kHz (VARIANT=v3.8) ----
 * docs/sotto-vlr.md, "Hiss band"; sim/sotto_vlr_hiss.py. The pitch fields that carry nothing
 * (superframes with no pitched frame, or one) hold the hiss, so sotto_vlr_decode plays a hiss
 * stream at 8 kHz (with a wrong pitch slope where one frame is pitched). Float VARIANT=v3.8
 * builds only (sotto_hiss.c); the fixed-point build's create functions return NULL.
 *
 * sotto_vlr_enc16_create(600 or 400). sotto_vlr_encode16 takes n160 blocks of 160 samples
 * (10 ms at 16 kHz; up to about a second per call); then call sotto_vlr_enc16_pop until it
 * returns 0. A superframe comes out about 10 superframes (sotto 600, 800 ms) or 9 (sotto 400,
 * 1.08 s) after its speech went in: the core's look-ahead and v3.8's rules (16 frames), the
 * anchor search, the local decoder's 2 superframes, and the hiss of the 2 superframes after it.
 * At the end call sotto_vlr_enc16_flush until it returns 0.
 *
 * sotto_vlr_dec16_create(600 or 400). sotto_vlr_decode16 takes one superframe (good = 0: lost)
 * and writes sotto_vlr_samples16(d) samples (N x 320 at 16 kHz), SOTTO_VLR_DEC16_DELAY(rate)
 * superframes behind (the first calls give silence): sotto 600 / 400's 2 plus v3.8's 16 kHz
 * layer's 5 frames, rounded up. Then sotto_vlr_dec16_flush until it returns 0. */
#ifdef FX_HISS_FB
#define SOTTO_VLR_DEC16_DELAY(rate) 2       /* superframes, then SOTTO_VLR_DEC16_DELAY_FRAMES 20 ms more */
#define SOTTO_VLR_DEC16_DELAY_FRAMES 2      /* the K6 layer's (and 20 samples) */
#else
#define SOTTO_VLR_DEC16_DELAY(rate) ((rate) == 400 ? 3 : 4)
#define SOTTO_VLR_DEC16_DELAY_FRAMES 0
#endif
#define SOTTO_VLR_MAX_SAMPLES16 (6 * SOTTO_SAMPLES_PER_FRAME_16K)

struct sotto_vlr_enc16;
SOTTO_API struct sotto_vlr_enc16 *sotto_vlr_enc16_create(int rate);
SOTTO_API void sotto_vlr_enc16_destroy(struct sotto_vlr_enc16 *e);
SOTTO_API void sotto_vlr_encode16(struct sotto_vlr_enc16 *e, const short *speech, int n160);
SOTTO_API int sotto_vlr_enc16_pop(struct sotto_vlr_enc16 *e, unsigned char out[SOTTO_BYTES_PER_FRAME]);
SOTTO_API int sotto_vlr_enc16_flush(struct sotto_vlr_enc16 *e, unsigned char out[SOTTO_BYTES_PER_FRAME]);

struct sotto_vlr_dec16;
SOTTO_API struct sotto_vlr_dec16 *sotto_vlr_dec16_create(int rate);
SOTTO_API void sotto_vlr_dec16_destroy(struct sotto_vlr_dec16 *d);
SOTTO_API int sotto_vlr_samples16(const struct sotto_vlr_dec16 *d);
SOTTO_API void sotto_vlr_decode16(struct sotto_vlr_dec16 *d, const unsigned char in[SOTTO_BYTES_PER_FRAME], int good,
                                  short *speech);
SOTTO_API int sotto_vlr_dec16_flush(struct sotto_vlr_dec16 *d, short *speech);
/* the K6 build (fixed point, FX_HISS_FB) also plays a superframe one 320-sample frame at a
   time, as sotto_vlr_decode_frame / sotto_vlr_dec_flush_frame do at 8 kHz */
SOTTO_API int sotto_vlr_decode16_frame(struct sotto_vlr_dec16 *d, const unsigned char in[SOTTO_BYTES_PER_FRAME], int good,
                                       int k, short speech[SOTTO_SAMPLES_PER_FRAME_16K]);
SOTTO_API int sotto_vlr_dec16_flush_frame(struct sotto_vlr_dec16 *d, short speech[SOTTO_SAMPLES_PER_FRAME_16K]);

#endif
