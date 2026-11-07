/* wspriq: the K6 as a WSPR receiver's front end. It streams the IF's complex baseband to the PC,
   which decodes it (tools/k6_ft8.py wsprdecode LOG; docs/k6-ft8-wspr.md).

   WSPR is 6 Hz wide and 110.6 s long, and a decode needs the whole frame at once: 162 symbols
   of four tones' powers, then a Fano decoder over a K = 32 code. That is far beyond the 4 KB
   store, so the K6 does what ft8wf does for FT8: the IF front end (gp0rx.h: GP0 by DMA, the
   gain, the real-time mixer at the IF + WSPR_AUDIO, two CICs to about 200 Hz) and the serial
   report. Here it sends the CIC output itself, not a waterfall: 15 complex samples a line
   (about 75 ms), I and Q as signed bytes with one shift for the line, and the 96 MHz time of
   the line's last sample. The host places each line by its time (lines it never got become
   zeros), fits the sample rate (the DMA's rate moves a few % with the CPU's load), resamples to
   WSPR's 375/8 Hz and runs sim/k6digi/wsprref.py's search and Fano decoder.

   A USB signal at dial rx_hz + WSPR_AUDIO lands at 0 Hz, and the window is about +-99 Hz: the
   calibration record (level 4 images) takes out the K6's own IF offset, and what is left (the
   two radios' crystals drifting) the decoder searches.

   Lines:
     wspriq start f=1500 inv=0 n=15 cal=-16580
     wspriq gain rms=4.7 r2=27 ks=5490                      (as ft8wf)
     q 1a2b3c4d 3 <40 base64 chars>                         (time, shift, I Q I Q ... bytes)
     wspriq st t=15 lost=0 ks=5492 idle=61                  (every 15 s)
     wspriq done n=3200 lost=0
   Runs WSPR_SECONDS (240: two WSPR slots, so a frame started any time in the first two minutes
   is caught whole) or until a key; returns the lines sent, or gp0rx's set-up errors. Receive
   only. SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 2
#define K6_MODULE_OPT z
#include "../k6_mod.h"
#include "../ft8/ft8k.c"

#ifndef WSPR_AUDIO
#define WSPR_AUDIO 1500
#endif
#ifndef WSPR_INV
#define WSPR_INV 0
#endif
#ifndef WSPR_SECONDS
#define WSPR_SECONDS 240
#endif
#define FT8_AUDIO WSPR_AUDIO
#define FT8_INV WSPR_INV
#ifndef FT8_RMS_LO
#define FT8_RMS_LO 80
#endif
#ifndef FT8_RMS_HI
#define FT8_RMS_HI 140
#endif
#include "gp0rx.h"

#define WQ_N 15    /* samples a line: 30 bytes, 40 base64 characters, a 53-character line */
#define WQ_STAT 375 /* rows (40 ms) between status lines */
#define WQ_STR1(x) #x
#define WQ_STR(x) WQ_STR1(x)

/* the line for the WQ_N samples in s (I, Q, I, Q ...), the last taken at 96 MHz time t */
static char *qline(char *p, const int16_t *s, uint32_t t) {
  uint8_t b[2 * WQ_N];
  int i, m = 0, e = 0;
  for (i = 0; i < 2 * WQ_N; i++) {
    int v = s[i] < 0 ? -s[i] : s[i];
    if (v > m) m = v;
  }
  while ((m >> e) > 127) e++;
  for (i = 0; i < 2 * WQ_N; i++) {
    int v = e ? (s[i] + (1 << (e - 1))) >> e : s[i];
    b[i] = (uint8_t)(v > 127 ? 127 : v);
  }
  *p++ = 'q', *p++ = ' ';
  p = hex4(hex4(p, t >> 16), t);
  *p++ = ' ', *p++ = (char)('0' + e), *p++ = ' ';
  return base64(p, b, 2 * WQ_N);
}

int k6_main(const k6_api_t *api) {
  fk_t k;
  uint32_t ring[RSZ / 4]; /* on the stack, as ft8wf (teardown stops the DMA before this returns) */
  int16_t s[2 * WQ_N];
  saved_t sv;
  rx_t r;
  uint32_t r2, next, stat = 0, sent = 0;
  char line[64], *p;
  int ret, n = 0;
  uint8_t sp;
  ret = rx_setup(api, &sv, &r, (uint8_t *)ring);
  if (ret) return ret;
  p = put(line, "wspriq start f=" WQ_STR(WSPR_AUDIO) " inv=" WQ_STR(WSPR_INV) " n=" WQ_STR(WQ_N));
  p = sdec(put(p, " cal="), r.cal);
  say0(api, line, p);
  p = rx_gain(api, &sv, &r, &r2, put(line, "wspriq gain"));
  say0(api, line, p);
  fk_init(&k, 32, (int)r2, 0, 0, 0); /* the front end only */
  RX_FK(&k);
  resync(&r);
  sp = k.sp;
  next = r.t + ROW_CYC;
  while (r.rows < (uint32_t)WSPR_SECONDS * 25u) {
    drain(&r, &k, 0);
    if ((int32_t)(r.t - next) < 0) continue;
    rx_rate(&r);
    next += ROW_CYC;
    r.rows++;
    {
      /* the samples since the last row (about 8), the newest made (n2 r1 + n1) counts before
         the next count's time, each one a CIC2 decimation (r1 r2 counts) after the one before */
      uint32_t cpc = (r.cps8 * LCNT) >> 8, td = cpc * k.r1 * k.r2;
      uint32_t m = (uint32_t)(k.sp - sp) & 31, tl = r.tc - (k.n2 * k.r1 + k.n1) * cpc, j;
      for (j = 0; j < m; j++) {
        s[2 * n] = k.si[sp], s[2 * n + 1] = k.sq[sp];
        sp = (uint8_t)((sp + 1) & 31);
        if (++n == WQ_N) {
          say0(api, line, qline(line, s, tl - (m - 1 - j) * td));
          sent++;
          n = 0;
        }
      }
    }
    if (++stat == WQ_STAT) {
      stat = 0;
      p = kv(line, "wspriq st t=", fk_udiv(r.rows, 25));
      p = kv(p, " lost=", r.lost);
      p = kv(p, " ks=", fk_udiv(96000u * 256u, r.cps8));
      p = kv(p, " idle=", fk_udiv(r.idle, WQ_STAT * 38400u));
      r.idle = 0;
      say0(api, line, p);
    }
    if (api->key_down()) break; /* once a row, as ft8wf: never in the drain loop */
    resync(&r);
  }
  teardown(api, &sv);
  p = kv(line, "wspriq done n=", sent);
  p = kv(p, " lost=", r.lost);
  say0(api, line, p);
  return (int)sent;
}
