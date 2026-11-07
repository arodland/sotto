/* sotto_enc [-16] IN.raw OUT.bit: 8 kHz 16-bit mono raw speech -> 6 bytes per 20 ms frame.
 * -16: 16 kHz input through the v3.8 encoder (needs a VARIANT=v3.8 build).
 * -1200: sotto 1200, 6 bytes per 40 ms superframe (8 kHz input).
 * -16 -1200: sotto 1200 hiss, 6 bytes per 40 ms from 16 kHz input (VARIANT=v3.8 build).
 * -600 / -400: sotto 600 / 400, 6 bytes per 80 / 120 ms superframe (8 kHz input).
 * SPDX-License-Identifier: MIT */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "sotto.h"
#include "sotto_vlr.h"

int main(int argc, char **argv) {
  int k16 = argc > 1 && !strcmp(argv[1], "-16");
  argv += k16, argc -= k16;
  int lr = argc > 1 && !strcmp(argv[1], "-1200");
  argv += lr, argc -= lr;
  int vlr = argc > 1 && (!strcmp(argv[1], "-600") || !strcmp(argv[1], "-400")) ? -atoi(argv[1]) : 0;
  argv += vlr != 0, argc -= vlr != 0;
  if (argc != 3) {
    fprintf(stderr, "usage: %s [-16] [-1200] | -600 | -400 IN.raw OUT.bit   (- for stdin/stdout)\n", argv[0]);
    return 1;
  }
  FILE *in = strcmp(argv[1], "-") ? fopen(argv[1], "rb") : stdin;
  FILE *out = strcmp(argv[2], "-") ? fopen(argv[2], "wb") : stdout;
  if (!in || !out) {
    perror("sotto_enc");
    return 1;
  }
#ifndef SOTTO_V38
  if (k16) {
    fprintf(stderr, "sotto_enc: -16 needs a VARIANT=v3.8 build\n");
    return 1;
  }
#endif
  unsigned char b[SOTTO_BYTES_PER_FRAME];
  size_t n;
  if (vlr) {
    struct sotto_vlr_enc *e = sotto_vlr_enc_create(vlr);
    short x[80];
    if (!e) {
      fprintf(stderr, "sotto_enc: this build has no sotto %d encoder (use the float build)\n", vlr);
      return 1;
    }
    while (fread(x, sizeof(short), 80, in) == 80) {   /* whole 10 ms blocks */
      sotto_vlr_encode(e, x, 1);
      while (sotto_vlr_enc_pop(e, b)) fwrite(b, 1, sizeof b, out);
    }
    while (sotto_vlr_enc_flush(e, b)) fwrite(b, 1, sizeof b, out);
    sotto_vlr_enc_destroy(e);
    return 0;
  }
#ifdef SOTTO_V38
  if (lr && k16) {                                  /* whole 40 ms blocks */
    struct sotto_lr_enc16 *e = sotto_lr_enc16_create();
    short x[SOTTO_LR_SAMPLES_PER_FRAME_16K];
    if (!e) {
      fprintf(stderr, "sotto_enc: this build has no sotto 1200 hiss encoder (use the float build)\n");
      return 1;
    }
    while (fread(x, sizeof(short), SOTTO_LR_SAMPLES_PER_FRAME_16K, in) == SOTTO_LR_SAMPLES_PER_FRAME_16K)
      if (sotto_lr_encode16(e, x, b)) fwrite(b, 1, sizeof b, out);
    while (sotto_lr_enc16_flush(e, b)) fwrite(b, 1, sizeof b, out);
    sotto_lr_enc16_destroy(e);
    return 0;
  }
#endif
  if (lr) {
    struct sotto_lr_enc *e = sotto_lr_enc_create();
    short x[SOTTO_LR_SAMPLES_PER_FRAME];
    if (!e) {
      fprintf(stderr, "sotto_enc: this build has no sotto 1200 encoder (use the float build)\n");
      return 1;
    }
    while ((n = fread(x, sizeof(short), SOTTO_LR_SAMPLES_PER_FRAME, in)) == SOTTO_LR_SAMPLES_PER_FRAME)
      if (sotto_lr_encode(e, x, b)) fwrite(b, 1, sizeof b, out);
    sotto_lr_encode_part(e, x, (int)(n / 80));     /* whole 10 ms blocks of the tail */
    while (sotto_lr_enc_flush(e, b)) fwrite(b, 1, sizeof b, out);
    sotto_lr_enc_destroy(e);
    return 0;
  }
#ifdef SOTTO_V38
  if (k16) {
    struct sotto_enc16 *e = sotto_enc16_create();
    short x[SOTTO_SAMPLES_PER_FRAME_16K];
    while (fread(x, sizeof(short), SOTTO_SAMPLES_PER_FRAME_16K, in) == SOTTO_SAMPLES_PER_FRAME_16K)
      if (sotto_encode16(e, x, b)) fwrite(b, 1, sizeof b, out);
    while (sotto_enc16_flush(e, b)) fwrite(b, 1, sizeof b, out);
    sotto_enc16_destroy(e);
    return 0;
  }
#endif
  struct sotto_enc *e = sotto_enc_create();
  short x[SOTTO_SAMPLES_PER_FRAME];
  while ((n = fread(x, sizeof(short), SOTTO_SAMPLES_PER_FRAME, in)) == SOTTO_SAMPLES_PER_FRAME)
    if (sotto_encode(e, x, b)) fwrite(b, 1, sizeof b, out);
  /* a last half frame (80 samples) is analysed too; anything shorter is dropped */
  if (n >= SOTTO_SAMPLES_PER_FRAME / 2) {
    memset(x + n, 0, (SOTTO_SAMPLES_PER_FRAME - n) * sizeof(short));
    sotto_encode_half(e, x);
  }
  while (sotto_enc_flush(e, b)) fwrite(b, 1, sizeof b, out);
  sotto_enc_destroy(e);
  return 0;
}
