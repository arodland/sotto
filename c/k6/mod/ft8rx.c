/* ft8rx: an FT8 receiver on the K6, from the receive IF (docs/k6-ft8-wspr.md).

   The IC-7100 (or anyone) sends FT8 in USB on the pager's frequency; the K6 stays in its FM
   receive set-up, so a USB tone at audio f sits on the FD6818's low IF at 26 MHz / 1536 + f
   (or - f: FT8_INV). GP0 code 13 puts the IF on PB1 as a 1-bit sigma-delta (PR #68), DMA
   channel 1 reads it flat out into a ring (gp0dma, PR #69), and this module counts 64 samples
   at a time and hands each count to c/k6/ft8/ft8k.c with the mixer phase for the moment it
   was taken. The phase comes from real time (TIM17 at 96 MHz, kept unwrapped with Aura's
   100 us TIM14), not from counting samples, because the DMA's rate moves with bus load and
   stops while a row is decoded.

   ft8k keeps a 162-row waterfall of FT8_NB bins around FT8_AUDIO Hz (24 bins: 150 Hz, enough
   for the K6's offset from the IC-7100 of tens of Hz) and decodes as it goes; there is no UTC
   clock, so transmissions can start whenever they like. Before it starts, the module sets
   the front end's gain so the receiver noise fills the 1-bit path well (the simulation: the
   pager's AGC level costs 2-4 dB, 10 dB more gain about none): it measures the counts' rms at
   the AGC, and if that is under FT8_RMS_LO it fixes the AGC (REG_7E bit 15, index 3) and
   takes the highest REG_13 gain whose noise is under FT8_RMS_HI.

   Lines:
     ft8 start r2=27 ks=5490 rms=4.7 g=0 03ff rms=9.1     (DMA kS/s; noise rms at each gain)
     ft8 CQ AG7EW CN87 f=1503 s=-12 t=12.4                 (tone 0 Hz, SNR dB, start s)
     ft8 st t=15 rows=375 lost=2 ks=5492 dec=1 idle=88     (every 15 s)
   Runs FT8_SECONDS (300) or until a key, and returns the decodes; or -2 (a key before the
   start), -3 (old image), -4 (PB1 not an input), -5 (REG_33 refused), -7 (TIM17 or DMA channel
   1 in use), -8 (scratch too small). Writes REG_34[3:0], REG_33 bit 8, REG_73 bit 4 (AFC off),
   REG_7E and REG_13 (the gain), TIM17 and DMA channel 1 with their clocks, all put back.
   Receive only. SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 2
#define K6_MODULE_OPT z
#include "../k6_mod.h"
#include "../ft8/ft8k.c"

#ifndef FT8_NB
#define FT8_NB 24
#endif
#ifndef FT8_AUDIO
#define FT8_AUDIO 1500 /* Hz: the window's centre, as a USB dial offset */
#endif
#ifndef FT8_INV
#define FT8_INV 0      /* 1: the IF is mirrored (the tone at IF - f) */
#endif
#ifndef FT8_SECONDS
#define FT8_SECONDS 300
#endif
#ifndef FT8_RMS_LO
#define FT8_RMS_LO 80  /* counts' rms x 10 under which the gain goes up */
#endif
#ifndef FT8_RMS_HI
#define FT8_RMS_HI 140
#endif

#include "gp0rx.h"

int k6_main(const k6_api_t *api) {
  fk_t *k;
  int16_t *sc;
  uint8_t *work, *ring, wf[FK_WF_BYTES(FT8_NB)];
  saved_t sv;
  rx_t r;
  uint32_t room, r2, next, stat = 0, ndec = 0;
  uint8_t *s;
  char line[64], *p;
  int ret;
  /* the scratch: the DMA ring, the receiver, its scores and the LDPC's work */
  s = (uint8_t *)(((uint32_t)api->scratch(&room) + 3) & ~3u);
  if (room < RSZ + sizeof(fk_t) + 2 * FK_SC_WORDS(FT8_NB) + FK_WORK + 16) return -8;
  ring = s;
  k = (fk_t *)(s + RSZ);
  sc = (int16_t *)((uint8_t *)k + ((sizeof(fk_t) + 3) & ~3u));
  work = (uint8_t *)sc + ((2 * FK_SC_WORDS(FT8_NB) + 3) & ~3u);
  ret = rx_setup(api, &sv, &r, ring);
  if (ret) return ret;
  p = rx_gain(api, &sv, &r, &r2, put(line, "ft8 start"));
  say0(api, line, p);
  fk_init(k, FT8_NB, (int)r2, wf, sc, work);
  resync(&r);
  next = r.t + ROW_CYC;
  while (!api->key_down() && r.rows < (uint32_t)FT8_SECONDS * 25u) {
    drain(&r, k, 0);
    if ((int32_t)(r.t - next) < 0) continue;
    rx_rate(&r);
    next += ROW_CYC;
    r.rows++;
    if (fk_row(k)) {
      int j;
      for (j = 0; j < k->ndec; j++) {
        const fk_dec_t *d = &k->dec[j];
        /* tone 0: the window's centre + (bin - nb/2) bins of about 6.25 Hz */
        p = fk_text(put(line, "ft8 "), d->msg);
        p = put(p, " f=");
        p = sdec(p, FT8_AUDIO + ((d->bin - FT8_NB / 2) * 25) / 4);
        p = put(p, " s=");
        p = sdec(p, d->snr);
        p = put(p, " t=");
        p = tenths(p, fk_udiv(d->row * 8, 10)); /* rows of 80 ms since the start */
        say0(api, line, p);
        ndec++;
      }
    }
    if (++stat == 375) { /* 15 s */
      stat = 0;
      p = kv(line, "ft8 st t=", fk_udiv(r.rows, 25));
      p = kv(p, " lost=", r.lost);
      p = kv(p, " ks=", fk_udiv(96000u * 256u, r.cps8));
      p = kv(p, " dec=", ndec);
      p = kv(p, " flo=", (uint32_t)(k->flo >> 4));
      say0(api, line, p);
    }
    resync(&r);
  }
  teardown(api, &sv);
  p = kv(line, "ft8 done rows=", r.rows);
  p = kv(p, " dec=", ndec);
  p = kv(p, " lost=", r.lost);
  say0(api, line, p);
  return (int)ndec;
}
