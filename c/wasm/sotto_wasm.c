/* WebAssembly entry points for the v3.8 decoder (16 kHz), used by docs/index.html.
 * The browser hands over a whole received stream and gets the speech back, aligned with the
 * encoder input like `sotto_dec -16` (the decoder delay is removed).
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

EXPORT(sotto_wasm_format) const char *sotto_wasm_format(void) { return sotto_format(); }
