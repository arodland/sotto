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
#define WQ_WF 24    /* the screen's waterfall rows (level 6): 0.64 s each, 15 s in all */
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

/* ---- the screen (module API level 6, on images that have it) ----
   WSPR RX  440850 kHz       (inverted)
   1500Hz +-99  cal -166
   <waterfall: 32 bins of 6.2 Hz, 4 px each, 0.64 s a row, newest on top>
   1:47 of 240 s  lost 0
   Each 40 ms row a 32-point DFT of the newest 32 samples (161 ms) keeps each bin's largest
   power; every 16 rows the bins become a pixel row, each bin's 4 pixels lit from the middle
   out at 2, 4, 8 and 16 times the row's mean power (3 dB steps). Drawn every second pixel row:
   a 20-40 ms flush is zero-filled in the stream. ft8k's own DFT rows would be 1.3 KB more. */
static const int8_t COS32[32] = {127, 125, 117, 106, 90, 71, 49, 25, 0, -25, -49, -71, -90,
                                 -106, -117, -125, -127, -125, -117, -106, -90, -71, -49, -25,
                                 0, 25, 49, 71, 90, 106, 117, 125};

static void wq_dft(const fk_t *k, uint32_t *pw) {
  int c, n;
  for (c = 0; c < 32; c++) {                /* column c: (c - 16) x 6.2 Hz */
    int32_t re = 0, im = 0;
    for (n = 0; n < 32; n++) {
      int at = (k->sp + n) & 31, i = ((c + 16) * n) & 31;
      int co = COS32[i], si = COS32[(i + 24) & 31];
      re += k->si[at] * co + k->sq[at] * si;
      im += k->sq[at] * co - k->si[at] * si;
    }
    re >>= 11, im >>= 11;
    {
      uint32_t v = (uint32_t)(re * re + im * im);
      if (v > pw[c]) pw[c] = v;
    }
  }
}

static void wq_px(uint8_t *px, uint32_t *pw) {
  uint32_t mean = 0;
  int c;
  for (c = 0; c < 32; c++) mean += pw[c] >> 5;
  for (c = 0; c < 32; c++) {
    /* the bin's 4 pixels (bit 3 leftmost): the middle two at 2 and 4 x the mean, then the
       left at 8 and the right at 16 */
    int o = (pw[c] > mean << 1) << 2 | (pw[c] > mean << 2) << 1 | (pw[c] > mean << 3) << 3 |
            (pw[c] > mean << 4);
    if (c & 1) px[c >> 1] |= (uint8_t)o;
    else px[c >> 1] = (uint8_t)(o << 4);
    pw[c] = 0;
  }
}

static void wq_text(const k6_api_t *api, const rx_t *r) {
  char l[24], *p;
  p = kv(l, "t ", fk_udiv(r->rows, 25));
  p = kv(p, " of " WQ_STR(WSPR_SECONDS) " s  lost ", r->lost);
  *p = 0;
  api->lcd_text(5, l);
  api->lcd_flush();
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
  uint8_t wf[WQ_WF * 16];
  uint32_t pw[32];
  int wh = 0, nacc = 0, ui = api->level >= 6;
  ret = rx_setup(api, &sv, &r, (uint8_t *)ring);
  if (ret) return ret;
  p = put(line, "wspriq start f=" WQ_STR(WSPR_AUDIO) " inv=" WQ_STR(WSPR_INV) " n=" WQ_STR(WQ_N));
  p = sdec(put(p, " cal="), r.cal);
  say0(api, line, p);
  p = rx_gain(api, &sv, &r, &r2, put(line, "wspriq gain"));
  say0(api, line, p);
  if (ui) {
    p = put(dec(put(line, "WSPR RX  "), fk_udiv(api->rx_hz(), 1000)), " kHz");
    *p = 0;
    api->lcd_text(0, line);
    p = sdec(put(line, WQ_STR(WSPR_AUDIO) "Hz +-99  cal "), (r.cal * 41) >> 12); /* 0.01 Hz to Hz, no divide */
    *p = 0;
    api->lcd_text(1, line);
    for (n = 0; n < WQ_WF * 16; n++) wf[n] = 0; /* blank rows until the first 15 s are in */
    for (n = 0; n < 32; n++) pw[n] = 0;
    n = 0;
  }
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
    if (ui) {
      wq_dft(&k, pw);
      if (++nacc == 16) {                     /* 0.64 s */
        nacc = 0;
        wh = wh ? wh - 1 : WQ_WF - 1;           /* the newest on top */
        wq_px(wf + 16 * wh, pw);
        api->lcd_wf(wf, WQ_WF, wh);
        if (wh & 1) wq_text(api, &r);
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
