/* sp_novoice: the voice decoder left out, for the EXPLORE image (firmware/k6-aura/build.sh
   aura-explore; docs/k6-explore-app.md). sotto_pager.c links against these instead of
   sotto_fx_dec.c, so the image keeps the pager's packets, its RAM modules and its control
   packets but plays no voice: sotto_dec_create gives no decoder, and sp_play_frame then plays
   nothing. The v3.7 decoder and its tables are about 35 KB of the pager image's flash; without
   them the explorer and its screens fit with room to grow. SPDX-License-Identifier: MIT */
#include "../sotto.h"

struct sotto_dec *sotto_dec_create(void) { return 0; }
void sotto_dec_destroy(struct sotto_dec *d) { (void)d; }
void sotto_decode(struct sotto_dec *d, const unsigned char in[SOTTO_BYTES_PER_FRAME], int good,
                  short speech[SOTTO_SAMPLES_PER_FRAME]) {
  int i;
  (void)d, (void)in, (void)good;
  for (i = 0; i < SOTTO_SAMPLES_PER_FRAME; i++) speech[i] = 0;
}
void sotto_dec_flush(struct sotto_dec *d, short speech[SOTTO_SAMPLES_PER_FRAME]) {
  sotto_decode(d, 0, 0, speech);
}
