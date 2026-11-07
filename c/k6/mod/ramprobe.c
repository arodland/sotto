/* ramprobe: is there SRAM above Aura's region? Aura's memory.x gives the application
   0x20001028 + 17720 bytes, so its RAM (and the boot stack, at the region's top) ends at
   0x20005560. The KD32F328 is sold with 24 KB of SRAM, which would end at 0x20006000: 2720
   bytes nobody has claimed, and the hiss band image (docs/k6-hiss-port.md) wants them for a
   fourth PWM segment and its message store. This module writes and reads every word of
   0x20005560-0x20005FFF, holds a pattern there for 50 ms, restores what it found, and says
   how many words held. If the SRAM really ends lower, the first access past the end faults
   and the radio resets: run it when a reset is acceptable. Says (under 60 characters, the
   say line's limit), for example:
     ram probe good=2a8 bad=none held=2a8 alias=none
   good = words that took both patterns (0x2a8 = 680 = all 2720 bytes), bad = the first word
   that didn't, held = words whose pattern survived 50 ms, alias = the first lower address
   (checked 2, 4, 8 and 16 KB below the probe's first word) that changed when the probe's
   first word was written: a smaller SRAM mirrored into this range would show there. On
   2026-11-07 the K6 (03b8917) said good=2a8 bad=none and ran on: the RAM is there.
   Returns the good count's low byte. API level 1 (say, ticks).
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
  static const uint32_t below[4] = {0x800, 0x1000, 0x2000, 0x4000};
  char line[64], *p;
  uint32_t i, good = 0, held = 0, first_bad = 0, alias = 0, t0;
  for (i = 0; i < 4; i++) {                        /* a mirror of lower RAM would move with m[0] */
    volatile uint32_t *q = (volatile uint32_t *)(uintptr_t)(LO - below[i]);
    uint32_t keep = m[0], was = *q;
    m[0] = ~was;
    if (*q != was && !alias) alias = LO - below[i];
    m[0] = keep;
  }
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
  p = put(line, "ram probe good=");
  p = hex(p, good, 3);
  p = put(p, " bad=");
  if (first_bad) p = hex(p, first_bad, 8);
  else p = put(p, "none");
  p = put(p, " held=");
  p = hex(p, held, 3);
  p = put(p, " alias=");
  if (alias) p = hex(p, alias, 8);
  else p = put(p, "none");
  *p = 0;
  api->say(line);
  return (int)(good & 0xFF);
}
