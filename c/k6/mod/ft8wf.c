/* ft8wf: the K6 as an FT8 front end, its waterfall streamed to the host (docs/k6-ft8-wspr.md).

   The whole receiver (ft8rx.c, c/k6/ft8/ft8k.c) is 7 KB of M0 code and needs about 4 KB of
   RAM besides: more than the 4 KB module store and the ~3.5 KB of stack a module has today.
   So this module runs the half that fits: the IF front end (gp0rx.h: GP0 by DMA, the noise-
   filling gain, the real-time mixer, two CICs to about 200 Hz) and ft8k's DFT rows, and sends
   each 80 ms row of FT8_NB 4-bit bins over the serial report. The host decodes them with the
   same ft8k code (tools/k6_ft8.py decode LOG), which also proves the whole RF path on the radio
   before the receiver moves onto the K6.

   Lines (base64 of 2 rows, FT8_NB / 2 bytes each, two bins a byte, low nibble first):
     ft8wf start nb=32 f=1500 inv=0
     ft8wf gain rms=4.7 g=03ff rms=9.1 r2=27 ks=5490        (noise rms at the AGC, at the gain)
     w 0 <44 base64 chars>                                  (the first row's number)
     ft8wf st t=15 lost=12 ks=5492 flo=812                  (every 15 s)
     ft8wf done rows=3750 lost=180
   Runs FT8_SECONDS (300) or until a key; returns the rows sent, or ft8rx's errors (not -8:
   the ring is on the stack). Writes what
   ft8rx writes, all put back. Receive only. SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 2
#define K6_MODULE_OPT z
#include "../k6_mod.h"
#include "../ft8/ft8k.c"

#ifndef FT8_NB
#define FT8_NB 32
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

#define RB (FT8_NB / 2) /* bytes a row */

static char b64(uint32_t v) {
  v &= 63;
  return (char)(v < 26 ? 'A' + v : v < 52 ? 'a' + v - 26 : v < 62 ? '0' + v - 52 : v == 62 ? '+' : '/');
}

static char *base64(char *p, const uint8_t *b, int n) {
  int i;
  for (i = 0; i < n; i += 3) {
    uint32_t v = (uint32_t)b[i] << 16 | (i + 1 < n ? (uint32_t)b[i + 1] << 8 : 0) |
                 (i + 2 < n ? b[i + 2] : 0);
    *p++ = b64(v >> 18), *p++ = b64(v >> 12);
    *p++ = i + 1 < n ? b64(v >> 6) : '=';
    *p++ = i + 2 < n ? b64(v) : '=';
  }
  return p;
}

int k6_main(const k6_api_t *api) {
  fk_t k;
  uint32_t ring[RSZ / 4]; /* the DMA ring: on the stack, as the code leaves too little scratch
                             (teardown stops the DMA before this returns) */
  uint8_t rows[2 * RB];
  saved_t sv;
  rx_t r;
  uint32_t r2, next, stat = 0, sent = 0;
  char line[64], *p;
  int ret, have = 0;
  ret = rx_setup(api, &sv, &r, (uint8_t *)ring);
  if (ret) return ret;
  p = kv(put(line, "ft8wf start"), " nb=", FT8_NB);
  p = kv(p, " f=", FT8_AUDIO);
  p = kv(p, " inv=", FT8_INV);
  say0(api, line, p);
  p = rx_gain(api, &sv, &r, &r2, put(line, "ft8wf gain"));
  say0(api, line, p);
  fk_init(&k, FT8_NB, (int)r2, 0, 0, 0); /* the front end only: no waterfall here */
  resync(&r);
  next = r.t + ROW_CYC;
  while (!api->key_down() && r.rows < (uint32_t)FT8_SECONDS * 25u) {
    drain(&r, &k, 0);
    if ((int32_t)(r.t - next) < 0) continue;
    rx_rate(&r);
    next += ROW_CYC;
    r.rows++;
    if (fk_rowq(&k, rows + have * RB) && ++have == 2) {
      p = kv(line, "w ", sent);
      *p++ = ' ';
      p = base64(p, rows, 2 * RB);
      say0(api, line, p);
      sent += 2;
      have = 0;
    }
    if (++stat == 375) { /* 15 s */
      stat = 0;
      p = kv(line, "ft8wf st t=", fk_udiv(r.rows, 25));
      p = kv(p, " lost=", r.lost);
      p = kv(p, " ks=", fk_udiv(96000u * 256u, r.cps8));
      p = kv(p, " flo=", (uint32_t)(k.flo >> 4));
      say0(api, line, p);
    }
    resync(&r);
  }
  teardown(api, &sv);
  p = kv(line, "ft8wf done rows=", sent);
  p = kv(p, " lost=", r.lost);
  say0(api, line, p);
  return (int)sent;
}
