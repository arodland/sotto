/* hello: the first K6 RAM module. It says where it is running and how long a small loop
   took, then returns 0x6B. Built by `python -m modem.k6mod build` (clang -fropi: all code
   and constants reached PC-relative, so it runs wherever it lands). No libraries, no
   division (the M0 has no divide instruction), no writable statics.
   SPDX-License-Identifier: MIT */
#include "../k6_mod.h"

static char *put(char *p, const char *s) {
  while (*s) *p++ = *s++;
  return p;
}

static char *hex(char *p, uint32_t v, int digits) {
  const char *x = "0123456789abcdef";
  while (digits--) *p++ = x[(v >> (4 * digits)) & 15];
  return p;
}

int k6_main(const k6_api_t *api) {
  char line[64], *p;
  uint32_t t0, t1, i;
  volatile uint32_t acc = 0;      /* volatile: the loop really runs, not folded at build time */
  p = put(line, "hello world from RAM at 0x");
  p = hex(p, (uint32_t)(uintptr_t)&k6_main, 8);
  *p = 0;
  api->say(line);
  t0 = api->ticks();
  for (i = 1; i <= 10000; i++) acc += i * i;      /* 333383335000 mod 2^32 */
  t1 = api->ticks();
  p = put(line, "sum of squares 0x");
  p = hex(p, acc, 8);
  p = put(p, " in 0x");
  p = hex(p, (t1 - t0) & 0xFFFF, 4);
  p = put(p, " ticks of 100 us");
  *p = 0;
  api->say(line);
  return 0x6B;
}
