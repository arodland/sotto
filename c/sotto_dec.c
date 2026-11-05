/* sotto_dec [-16] IN.bit OUT.raw [GOOD.u8]: frames -> 8 kHz 16-bit mono raw speech.
 * -16: 16 kHz output through the v3.8 decoder (VARIANT=v3.8 build); its delay is removed too.
 * -1200: sotto 1200 superframes (40 ms each); GOOD.u8 then has one byte per superframe.
 * GOOD.u8 holds one byte per frame, 0 for a frame that failed its CRC. The output is
 * aligned with the encoder input: the decoder's 20 ms of delay is removed.
 * SPDX-License-Identifier: MIT */
#include <stdio.h>
#include <string.h>
#include "sotto.h"

int main(int argc, char **argv) {
  int k16 = argc > 1 && !strcmp(argv[1], "-16");
  argv += k16, argc -= k16;
  int lr = argc > 1 && !strcmp(argv[1], "-1200");
  argv += lr, argc -= lr;
  if (argc != 3 && argc != 4) {
    fprintf(stderr, "usage: %s [-16 | -1200] IN.bit OUT.raw [GOOD.u8]\n", argv[0]);
    return 1;
  }
  FILE *in = strcmp(argv[1], "-") ? fopen(argv[1], "rb") : stdin;
  FILE *out = strcmp(argv[2], "-") ? fopen(argv[2], "wb") : stdout;
  FILE *gf = argc == 4 ? fopen(argv[3], "rb") : NULL;
  if (!in || !out || (argc == 4 && !gf)) {
    perror("sotto_dec");
    return 1;
  }
  unsigned char b[SOTTO_BYTES_PER_FRAME];
  if (lr) {
    struct sotto_lr_dec *d = sotto_lr_dec_create();
    short y[SOTTO_LR_SAMPLES_PER_FRAME];
    long n = 0;
    while (fread(b, 1, sizeof b, in) == sizeof b) {
      int good = 1, c;
      if (gf && (c = fgetc(gf)) != EOF) good = c != 0;
      sotto_lr_decode(d, b, good, y);
      if (n++) fwrite(y, sizeof(short), SOTTO_LR_SAMPLES_PER_FRAME, out);
    }
    if (n) {
      sotto_lr_dec_flush(d, y);
      fwrite(y, sizeof(short), SOTTO_LR_SAMPLES_PER_FRAME, out);
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
#else
  if (k16) {
    fprintf(stderr, "sotto_dec: -16 needs a VARIANT=v3.8 build\n");
    return 1;
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
