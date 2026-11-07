/* ramprobe: is there SRAM above Aura's region? Aura's memory.x gives the application
   0x20001028 + 17720 bytes, so its RAM (and the boot stack, at the region's top) ends at
   0x20005560. The KD32F328 is sold with 24 KB of SRAM, which would end at 0x20006000: 2720
   bytes nobody has claimed, and the hiss band image (docs/k6-hiss-port.md) wants them for a
   fourth PWM segment and its message store. This module writes and reads every word of
   0x20005560-0x20005FFF, holds a pattern there for 50 ms, restores what it found, and says
   how many words held. If the SRAM really ends lower, the first access past the end faults
   and the radio resets: run it when a reset is acceptable. Says, for example:
     ram probe 20005560..20006000 good=0x2a8 first_bad=none held=0x2a8
   good = words that took both patterns (0x2a8 = 680 = all 2720 bytes), held = words whose
   pattern survived 50 ms. Returns the good count's low byte. API level 1 (say, ticks).
   SPDX-License-Identifier: MIT */
#include "../k6_mod.h"

#define LO 0x20005560u
#define HI 0x20006000u
#define WORDS ((HI - LO) / 4)

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
  volatile uint32_t *m = (volatile uint32_t *)(uintptr_t)LO;
  char line[64], *p;
  uint32_t i, good = 0, held = 0, first_bad = 0, t0;
  for (i = 0; i < WORDS; i++) {                    /* each word takes two patterns */
    uint32_t keep = m[i], a = 0xA5A5A5A5u ^ (LO + 4 * i), ok;
    m[i] = a;
    ok = m[i] == a;
    m[i] = ~a;
    ok = ok && m[i] == ~a;
    m[i] = keep;
    if (ok) good++;
    else if (!first_bad) first_bad = LO + 4 * i;
  }
  for (i = 0; i < WORDS; i++) m[i] = 0x5A5A0000u | (i & 0xFFFF);   /* then a 50 ms hold */
  t0 = api->ticks();
  while (((api->ticks() - t0) & 0xFFFF) < 500) {}
  for (i = 0; i < WORDS; i++) held += m[i] == (0x5A5A0000u | (i & 0xFFFF));
  for (i = 0; i < WORDS; i++) m[i] = 0;             /* leave it clean (it held nothing of Aura's) */
  p = put(line, "ram probe ");
  p = hex(p, LO, 8);
  p = put(p, "..");
  p = hex(p, HI, 8);
  p = put(p, " good=0x");
  p = hex(p, good, 3);
  p = put(p, " first_bad=");
  if (first_bad) p = hex(p, first_bad, 8);
  else p = put(p, "none");
  p = put(p, " held=0x");
  p = hex(p, held, 3);
  *p = 0;
  api->say(line);
  return (int)(good & 0xFF);
}
