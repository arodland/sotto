/* clkget: say the radio's clock (module API level 8, docs/k6-aprs-clock.md). For the bench: run
   aisclk or aprsclk, then this, minutes later, to see the core kept the time between module runs
   and how far its bound has grown.

     core clk 04:52:10.123 w=14 d=09/11/2026     (UTC now, the bound in ms, the date it has)
     core clk none                               (no fix since boot)

   On screen too (level 6). Returns the bound in ms (at most 0x7FFFFFFF), -1 with no clock, or
   -3 on an image before level 8 (the header's level stops it there first).
   SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 8
#include "../k6_mod.h"

static char *put(char *p, const char *s) {
  while (*s) *p++ = *s++;
  return p;
}

/* n / d by shift and subtract (a module has no run-time library) */
static uint32_t udiv(uint32_t n, uint32_t d) {
  uint32_t q = 0, bit = 1;
  while (d < n && !(d & 0x80000000u)) d <<= 1, bit <<= 1;
  while (bit) {
    if (n >= d) n -= d, q |= bit;
    d >>= 1, bit >>= 1;
  }
  return q;
}

/* v in decimal, at least w digits */
static char *dec(char *p, uint32_t v, int w) {
  char t[10];
  int n = 0;
  do {
    uint32_t q = udiv(v, 10);
    t[n++] = (char)('0' + v - 10 * q);
    v = q;
  } while (v || n < w);
  while (n) *p++ = t[--n];
  return p;
}

int k6_main(const k6_api_t *api) {
  char line[64], *p;
  uint32_t ms, date, w, h, m, s;
  if (api->abi != 1 || api->level < 8) return -3;
  p = put(line, "core clk ");
  if (api->clock_get(&ms, &date, &w) < 0) {
    p = put(p, "none");
    w = 0xFFFFFFFFu;
  } else {
    s = udiv(ms, 1000), m = udiv(s, 60), h = udiv(m, 60);
    p = dec(p, h, 2), *p++ = ':', p = dec(p, m - 60 * h, 2), *p++ = ':', p = dec(p, s - 60 * m, 2);
    *p++ = '.', p = dec(p, ms - 1000 * s, 3);
    p = dec(put(p, " w="), w, 1);
    if (date & 0xFF) {
      p = dec(put(p, " d="), date & 0xFF, 2);
      if (date >> 8 & 0xFF) p = dec(put(p, "/"), date >> 8 & 0xFF, 2);
      if (date >> 16) p = dec(put(p, "/"), date >> 16, 4);
    }
  }
  *p = 0;
  api->say(line);
  api->lcd_text(0, "CLOCK");
  api->lcd_text(2, line + 5);
  api->lcd_flush();
  return w > 0x7FFFFFFFu ? -1 : (int)w;
}
