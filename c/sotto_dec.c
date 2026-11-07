/* sotto_dec [-16] IN.bit OUT.raw [GOOD.u8]: frames -> 8 kHz 16-bit mono raw speech.
 * -16: 16 kHz output through the v3.8 decoder (VARIANT=v3.8 build); its delay is removed too.
 * -1200: sotto 1200 superframes (40 ms each); GOOD.u8 then has one byte per superframe.
 * -1200half: the same through the 20 ms half calls (sotto_lr_decode_half), as the K6 plays it.
 * -16 -1200: sotto 1200 hiss, 16 kHz out (sotto_lr_decode16, VARIANT=v3.8 build).
 * -16 -600 / -16 -400: sotto 600 / 400 hiss, 16 kHz out (sotto_vlr_decode16, VARIANT=v3.8
 * build); GOOD.u8 has one byte per superframe. (Plain sotto 600 / 400: fixed/sotto_vlr_dec_fx.)
 * GOOD.u8 holds one byte per frame, 0 for a frame that failed its CRC. The output is
 * aligned with the encoder input: the decoder's 20 ms of delay is removed.
 * SPDX-License-Identifier: MIT */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "sotto.h"
#include "sotto_vlr.h"

int main(int argc, char **argv) {
  int k16 = argc > 1 && !strcmp(argv[1], "-16");
  argv += k16, argc -= k16;
  int half = argc > 1 && !strcmp(argv[1], "-1200half");
  int lr = half || (argc > 1 && !strcmp(argv[1], "-1200"));
  argv += lr, argc -= lr;
  int vlr = k16 && argc > 1 && (!strcmp(argv[1], "-600") || !strcmp(argv[1], "-400")) ? -atoi(argv[1]) : 0;
  argv += vlr != 0, argc -= vlr != 0;
  if (argc != 3 && argc != 4) {
    fprintf(stderr, "usage: %s [-16] [-1200 | -1200half] | -16 -600 | -16 -400 IN.bit OUT.raw [GOOD.u8]\n", argv[0]);
    return 1;
  }
  FILE *in = strcmp(argv[1], "-") ? fopen(argv[1], "rb") : stdin;
  FILE *out = strcmp(argv[2], "-") ? fopen(argv[2], "wb") : stdout;
  FILE *gf = argc == 4 ? fopen(argv[3], "rb") : NULL;
  if (!in || !out || (argc == 4 && !gf)) {
    perror("sotto_dec");
    return 1;
  }
#ifndef SOTTO_V38
  if (k16) {
    fprintf(stderr, "sotto_dec: -16 needs a VARIANT=v3.8 build\n");
    return 1;
  }
#endif
  unsigned char b[SOTTO_BYTES_PER_FRAME];
#ifdef SOTTO_V38
  if (vlr) {
    struct sotto_vlr_dec16 *d = sotto_vlr_dec16_create(vlr);
    short y[SOTTO_VLR_MAX_SAMPLES16];
    long n = 0;
    if (!d) {
      fprintf(stderr, "sotto_dec: this build has no sotto %d hiss decoder (use the float build)\n", vlr);
      return 1;
    }
    int ns = sotto_vlr_samples16(d);
    while (fread(b, 1, sizeof b, in) == sizeof b) {
      int good = 1, c;
      if (gf && (c = fgetc(gf)) != EOF) good = c != 0;
      sotto_vlr_decode16(d, b, good, y);
      if (n++ >= SOTTO_VLR_DEC16_DELAY(vlr)) fwrite(y, sizeof(short), ns, out);
    }
    while (sotto_vlr_dec16_flush(d, y)) fwrite(y, sizeof(short), ns, out);
    sotto_vlr_dec16_destroy(d);
    return 0;
  }
  if (lr && k16) {
    struct sotto_lr_dec16 *d = sotto_lr_dec16_create();
    short y[SOTTO_LR_SAMPLES_PER_FRAME_16K];
    long n = 0;
    if (!d) {
      fprintf(stderr, "sotto_dec: this build has no sotto 1200 hiss decoder (use the float build)\n");
      return 1;
    }
    while (fread(b, 1, sizeof b, in) == sizeof b) {
      int good = 1, c;
      if (gf && (c = fgetc(gf)) != EOF) good = c != 0;
      sotto_lr_decode16(d, b, good, y);
      if (n++ >= SOTTO_LR_DEC16_DELAY) fwrite(y, sizeof(short), SOTTO_LR_SAMPLES_PER_FRAME_16K, out);
    }
    while (sotto_lr_dec16_flush(d, y)) fwrite(y, sizeof(short), SOTTO_LR_SAMPLES_PER_FRAME_16K, out);
    sotto_lr_dec16_destroy(d);
    return 0;
  }
#endif
  if (lr) {
    struct sotto_lr_dec *d = sotto_lr_dec_create();
    short y[SOTTO_LR_SAMPLES_PER_FRAME];
    long n = 0;
    while (fread(b, 1, sizeof b, in) == sizeof b) {
      int good = 1, c;
      if (gf && (c = fgetc(gf)) != EOF) good = c != 0;
      if (half) {
        sotto_lr_decode_half(d, b, good, 0, y);
        sotto_lr_decode_half(d, NULL, 0, 1, y + SOTTO_SAMPLES_PER_FRAME);
      } else {
        sotto_lr_decode(d, b, good, y);
      }
      if (n++ >= SOTTO_LR_DEC_DELAY) fwrite(y, sizeof(short), SOTTO_LR_SAMPLES_PER_FRAME, out);
    }
    for (;; n++) {
      if (half) {
        if (!sotto_lr_dec_flush_half(d, 0, y)) break;
        sotto_lr_dec_flush_half(d, 1, y + SOTTO_SAMPLES_PER_FRAME);
      } else if (!sotto_lr_dec_flush(d, y)) {
        break;
      }
      if (n >= SOTTO_LR_DEC_DELAY) fwrite(y, sizeof(short), SOTTO_LR_SAMPLES_PER_FRAME, out);
    }
    sotto_lr_dec_destroy(d);
    return 0;
  }
#ifdef SOTTO_V38
  if (k16) {
    struct sotto_dec16 *d = sotto_dec16_create();
    short y[SOTTO_SAMPLES_PER_FRAME_16K];
    long n = 0;
    while (fread(b, 1, sizeof b, in) == sizeof b) {
      int good = 1, c;
      if (gf && (c = fgetc(gf)) != EOF) good = c != 0;
      sotto_decode16(d, b, good, y);
      if (n++ >= SOTTO_DEC16_DELAY_FRAMES) fwrite(y, sizeof(short), SOTTO_SAMPLES_PER_FRAME_16K, out);
    }
    while (sotto_dec16_flush(d, y)) fwrite(y, sizeof(short), SOTTO_SAMPLES_PER_FRAME_16K, out);
    sotto_dec16_destroy(d);
    return 0;
  }
#endif
  struct sotto_dec *d = sotto_dec_create();
  short y[SOTTO_SAMPLES_PER_FRAME];
  long n = 0;
  while (fread(b, 1, sizeof b, in) == sizeof b) {
    int good = 1, c;
    if (gf && (c = fgetc(gf)) != EOF) good = c != 0;
    sotto_decode(d, b, good, y);
    if (n++) fwrite(y, sizeof(short), SOTTO_SAMPLES_PER_FRAME, out);
  }
  if (n) {
    sotto_dec_flush(d, y);
    fwrite(y, sizeof(short), SOTTO_SAMPLES_PER_FRAME, out);
  }
  sotto_dec_destroy(d);
  return 0;
}
