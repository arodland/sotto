/* sotto_vlr_dec_fx: decode sotto 600 / sotto 400 superframes with the fixed-point decoder.
 * Usage: sotto_vlr_dec_fx 600|400 in.bit out.raw [good.u8]
 *   in.bit: 6-byte superframes; out.raw: 8 kHz 16-bit mono, the decoder's SOTTO_VLR_DEC_DELAY
 *   superframes of start-up dropped; good.u8: one byte per superframe, 0 = lost.
 * SPDX-License-Identifier: MIT */
#include <stdio.h>
#include <stdlib.h>
#include "../sotto_vlr.h"

int main(int argc, char **argv) {
  if (argc < 4) {
    fprintf(stderr, "usage: %s 600|400 in.bit out.raw [good.u8]\n", argv[0]);
    return 2;
  }
  struct sotto_vlr_dec *d = sotto_vlr_dec_create(atoi(argv[1]));
  FILE *fi = fopen(argv[2], "rb"), *fo = fopen(argv[3], "wb"), *fg = argc > 4 ? fopen(argv[4], "rb") : NULL;
  if (!d || !fi || !fo || (argc > 4 && !fg)) {
    fprintf(stderr, "%s: bad rate or file\n", argv[0]);
    return 1;
  }
  unsigned char b[SOTTO_BYTES_PER_FRAME];
  short pcm[SOTTO_VLR_MAX_SAMPLES];
  int ns = sotto_vlr_samples(d), n = 0;
  while (fread(b, 1, sizeof b, fi) == sizeof b) {
    int good = 1;
    if (fg) {
      int c = fgetc(fg);
      good = c != 0 && c != EOF;
    }
    sotto_vlr_decode(d, b, good, pcm);
    if (n++ >= SOTTO_VLR_DEC_DELAY) fwrite(pcm, sizeof(short), (size_t)ns, fo);
  }
  while (sotto_vlr_dec_flush(d, pcm)) fwrite(pcm, sizeof(short), (size_t)ns, fo);
  sotto_vlr_dec_destroy(d);
  fclose(fo);
  return 0;
}
