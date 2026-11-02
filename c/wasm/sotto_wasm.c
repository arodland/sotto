/* WebAssembly entry points for the v3.8 codec (16 kHz), used by docs/index.html.
 * The browser hands over a whole stream: speech to sotto_wasm_encode16 (frames out, like
 * `sotto_enc -16`), received frames to sotto_wasm_decode16 (speech out, aligned with the
 * encoder input like `sotto_dec -16`: the decoder delay is removed).
 * SPDX-License-Identifier: MIT */
#include <stdint.h>
#include <stdlib.h>
#include "sotto.h"

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
#endif
