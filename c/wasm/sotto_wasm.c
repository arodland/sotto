/* WebAssembly entry points for the page's codecs (docs/index.html, docs/sotto.js).
 * The browser hands over a whole stream: speech to an encoder (frames out, like `sotto_enc`),
 * received frames to a decoder (speech out, like `sotto_dec`: the decoder's start-up delay is
 * removed). Three modules are built from this file:
 *   sotto.wasm      (float, v3.8)  2400: sotto_wasm_encode16 / sotto_wasm_decode16, 16 kHz
 *                                  1200 hiss: sotto_wasm_encode_lr16 / sotto_wasm_decode_lr16, 16 kHz
 *                                  600 / 400: sotto_wasm_encode_vlr (8 kHz in), sotto_wasm_down2
 *   sotto_vlr.wasm  (SOTTO_WASM_VLR: fixed point on the v3.7 core, as on the K6)
 *                                  600 / 400: sotto_wasm_decode_vlr, 8 kHz out
 *   sotto_fx.wasm   (SOTTO_FX: the fixed-point v3.8 codec behind the 2400 entry points)
 * SPDX-License-Identifier: MIT */
#include <math.h>
#include <stdint.h>
#include <stdlib.h>
#include "sotto.h"
#include "sotto_vlr.h"

#define EXPORT(name) __attribute__((export_name(#name)))

/* Codec 2's random phase generator (sine.c) keeps its state in an unsigned long, which is
 * 64 bits on the 64-bit hosts the native library and the Python model run on but 32 bits in
 * wasm32, so the sequence differs. The Makefile renames sine.c's copy; this one keeps 64 bits
 * and restarts for every stream, as a fresh `sotto_dec` process does. */
static uint64_t c2_next = 1;
int codec2_rand(void) {
  c2_next = c2_next * 1103515245u + 12345u;
  return (int)((unsigned)(c2_next / 65536) % 32768);
}

EXPORT(sotto_wasm_alloc) void *sotto_wasm_alloc(int n) { return malloc((size_t)n); }
EXPORT(sotto_wasm_free) void sotto_wasm_free(void *p) { free(p); }

#ifdef SOTTO_WASM_VLR
/* ---- sotto 600 / 400 decoder (fixed point, c/fixed/sotto_fx_vlr.c) ---------------------- */
/* Samples sotto_wasm_decode_vlr may write for n superframes at rate 600 or 400 (8 kHz). */
EXPORT(sotto_wasm_vlr_out_len) int sotto_wasm_vlr_out_len(int rate, int n) {
  return (n + 1) * (rate == 400 ? 6 : 4) * SOTTO_SAMPLES_PER_FRAME;
}

/* bits: n superframes of 6 bytes; good: n bytes (0 = lost) or NULL; out:
 * sotto_wasm_vlr_out_len(rate, n) samples at 8 kHz, as `sotto_vlr_dec_fx rate` writes them.
 * Returns the samples written (-1: bad rate or no memory). */
EXPORT(sotto_wasm_decode_vlr) int sotto_wasm_decode_vlr(int rate, const unsigned char *bits, const unsigned char *good,
                                                        int n, short *out) {
  struct sotto_vlr_dec *d = sotto_vlr_dec_create(rate);
  if (!d) return -1;
  int ns = sotto_vlr_samples(d), w = 0, max = sotto_wasm_vlr_out_len(rate, n);
  for (int i = 0; i < n; i++) {
    sotto_vlr_decode(d, bits + SOTTO_BYTES_PER_FRAME * i, good ? good[i] != 0 : 1, out + w);
    if (i >= SOTTO_VLR_DEC_DELAY) w += ns;
  }
  while (w + ns <= max && sotto_vlr_dec_flush(d, out + w)) w += ns;
  sotto_vlr_dec_destroy(d);
  return w;
}

EXPORT(sotto_wasm_format) const char *sotto_wasm_format(void) { return "sotto 600 / 400, fixed point"; }
#else
/* Samples sotto_wasm_decode16 writes for n frames. */
EXPORT(sotto_wasm_out_len16) int sotto_wasm_out_len16(int n) { return n * SOTTO_SAMPLES_PER_FRAME_16K; }

/* bits: n frames of 6 bytes; good: n bytes (0 = failed CRC) or NULL for all good;
 * out: sotto_wasm_out_len16(n) samples. Returns the samples written. */
EXPORT(sotto_wasm_decode16) int sotto_wasm_decode16(const unsigned char *bits, const unsigned char *good, int n,
                                                    short *out) {
  c2_next = 1;
  struct sotto_dec16 *d = sotto_dec16_create();
  if (!d) return -1;
  short y[SOTTO_SAMPLES_PER_FRAME_16K];
  int w = 0;
  for (int i = 0; i < n; i++) {
    sotto_decode16(d, bits + SOTTO_BYTES_PER_FRAME * i, good ? good[i] != 0 : 1, y);
    if (i >= SOTTO_DEC16_DELAY_FRAMES)
      for (int k = 0; k < SOTTO_SAMPLES_PER_FRAME_16K; k++) out[w++] = y[k];
  }
  while (w + SOTTO_SAMPLES_PER_FRAME_16K <= n * SOTTO_SAMPLES_PER_FRAME_16K && sotto_dec16_flush(d, y))
    for (int k = 0; k < SOTTO_SAMPLES_PER_FRAME_16K; k++) out[w++] = y[k];
  sotto_dec16_destroy(d);
  return w;
}

/* Frames sotto_wasm_encode16 may write for n samples (it writes ceil(n / 320)). */
EXPORT(sotto_wasm_max_frames16) int sotto_wasm_max_frames16(int n) { return n / SOTTO_SAMPLES_PER_FRAME_16K + 2; }

/* speech: n samples at 16 kHz (padded with silence to whole 20 ms frames);
 * out: sotto_wasm_max_frames16(n) * 6 bytes. Returns the frames written. */
EXPORT(sotto_wasm_encode16) int sotto_wasm_encode16(const short *speech, int n, unsigned char *out) {
  c2_next = 1;   /* every stream starts the generator afresh, as a new process does: the encoders
                    run Codec 2 code too (sotto 1200 hiss has a local decoder) */
  struct sotto_enc16 *e = sotto_enc16_create();
  if (!e) return -1;
  short x[SOTTO_SAMPLES_PER_FRAME_16K];
  int nf = 0, max = sotto_wasm_max_frames16(n);
  for (int i = 0; i < n; i += SOTTO_SAMPLES_PER_FRAME_16K) {
    for (int k = 0; k < SOTTO_SAMPLES_PER_FRAME_16K; k++) x[k] = i + k < n ? speech[i + k] : 0;
    if (nf < max) nf += sotto_encode16(e, x, out + SOTTO_BYTES_PER_FRAME * nf);
  }
  while (nf < max && sotto_enc16_flush(e, out + SOTTO_BYTES_PER_FRAME * nf)) nf++;
  sotto_enc16_destroy(e);
  return nf;
}

#ifdef SOTTO_FX
/* the fixed-point build (c/fixed, the code that runs on the C5517): sotto_fx.wasm */
EXPORT(sotto_wasm_format) const char *sotto_wasm_format(void) { return "sotto v3.8 fixed point"; }
#else
EXPORT(sotto_wasm_format) const char *sotto_wasm_format(void) { return sotto_format(); }

/* ---- sotto 1200 hiss (48 bits per 40 ms, 16 kHz in and out; c/sotto_hiss.c) ------------- */
EXPORT(sotto_wasm_max_frames_lr16) int sotto_wasm_max_frames_lr16(int n) {
  return n / SOTTO_LR_SAMPLES_PER_FRAME_16K + 16;
}

/* speech: n samples at 16 kHz (padded with silence to whole 40 ms blocks); out:
 * sotto_wasm_max_frames_lr16(n) * 6 bytes. Returns the superframes written. */
EXPORT(sotto_wasm_encode_lr16) int sotto_wasm_encode_lr16(const short *speech, int n, unsigned char *out) {
  c2_next = 1;
  struct sotto_lr_enc16 *e = sotto_lr_enc16_create();
  if (!e) return -1;
  short x[SOTTO_LR_SAMPLES_PER_FRAME_16K];
  int nf = 0, max = sotto_wasm_max_frames_lr16(n);
  for (int i = 0; i < n; i += SOTTO_LR_SAMPLES_PER_FRAME_16K) {
    for (int k = 0; k < SOTTO_LR_SAMPLES_PER_FRAME_16K; k++) x[k] = i + k < n ? speech[i + k] : 0;
    if (nf < max) nf += sotto_lr_encode16(e, x, out + SOTTO_BYTES_PER_FRAME * nf);
  }
  while (nf < max && sotto_lr_enc16_flush(e, out + SOTTO_BYTES_PER_FRAME * nf)) nf++;
  sotto_lr_enc16_destroy(e);
  return nf;
}

EXPORT(sotto_wasm_out_len_lr16) int sotto_wasm_out_len_lr16(int n) { return (n + 1) * SOTTO_LR_SAMPLES_PER_FRAME_16K; }

/* bits: n superframes; good: n bytes or NULL; out: sotto_wasm_out_len_lr16(n) samples, as
 * `sotto_dec -16 -1200` writes them. Returns the samples written. */
EXPORT(sotto_wasm_decode_lr16) int sotto_wasm_decode_lr16(const unsigned char *bits, const unsigned char *good, int n,
                                                          short *out) {
  c2_next = 1;
  struct sotto_lr_dec16 *d = sotto_lr_dec16_create();
  if (!d) return -1;
  short y[SOTTO_LR_SAMPLES_PER_FRAME_16K];
  int w = 0, max = sotto_wasm_out_len_lr16(n);
  for (int i = 0; i < n; i++) {
    sotto_lr_decode16(d, bits + SOTTO_BYTES_PER_FRAME * i, good ? good[i] != 0 : 1, y);
    if (i >= SOTTO_LR_DEC16_DELAY)
      for (int k = 0; k < SOTTO_LR_SAMPLES_PER_FRAME_16K; k++) out[w++] = y[k];
  }
  while (w + SOTTO_LR_SAMPLES_PER_FRAME_16K <= max && sotto_lr_dec16_flush(d, y))
    for (int k = 0; k < SOTTO_LR_SAMPLES_PER_FRAME_16K; k++) out[w++] = y[k];
  sotto_lr_dec16_destroy(d);
  return w;
}

/* ---- sotto 600 / 400 encoder (float, c/sotto_vlr.c; 8 kHz in) ---------------------------- */
#include "sotto_hiss_tables.h"
/* 16 kHz to 8 kHz as scipy.signal.resample_poly(x, 1, 2) does it (the v3.8 encoder's own
 * decimator, sotto_hiss.c down2), rounded: y[m] = sum_j h[j] x[2m + 20 - j], zero outside.
 * Returns ceil(n / 2), the samples written to out. */
EXPORT(sotto_wasm_down2) int sotto_wasm_down2(const short *x, int n, short *out) {
  int m8 = (n + 1) / 2;
  for (int m = 0; m < m8; m++) {
    double s = 0;
    for (int j = 0; j < 41; j++) {
      int i = 2 * m + 20 - j;
      if (i >= 0 && i < n) s += sotto_resample_taps[j] * x[i];
    }
    s = rint(s);
    out[m] = (short)(s > 32767 ? 32767 : s < -32768 ? -32768 : s);
  }
  return m8;
}

EXPORT(sotto_wasm_max_frames_vlr) int sotto_wasm_max_frames_vlr(int rate, int n) {
  return n / ((rate == 400 ? 6 : 4) * SOTTO_SAMPLES_PER_FRAME) + 4;
}

/* rate 600 or 400; speech: n samples at 8 kHz (padded with silence to whole 10 ms blocks);
 * out: sotto_wasm_max_frames_vlr(rate, n) * 6 bytes. Returns the superframes written. */
EXPORT(sotto_wasm_encode_vlr) int sotto_wasm_encode_vlr(int rate, const short *speech, int n, unsigned char *out) {
  c2_next = 1;
  struct sotto_vlr_enc *e = sotto_vlr_enc_create(rate);
  if (!e) return -1;
  short x[80];
  int nf = 0, max = sotto_wasm_max_frames_vlr(rate, n);
  for (int i = 0; i < n; i += 80) {
    for (int k = 0; k < 80; k++) x[k] = i + k < n ? speech[i + k] : 0;
    sotto_vlr_encode(e, x, 1);
    while (nf < max && sotto_vlr_enc_pop(e, out + SOTTO_BYTES_PER_FRAME * nf)) nf++;
  }
  while (nf < max && sotto_vlr_enc_flush(e, out + SOTTO_BYTES_PER_FRAME * nf)) nf++;
  sotto_vlr_enc_destroy(e);
  return nf;
}
#endif
#endif /* SOTTO_WASM_VLR */
