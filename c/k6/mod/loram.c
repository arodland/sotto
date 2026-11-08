/* loram: does anything write the bottom of SRAM while Aura runs? (2026-11-08)
   txdog (hal::txdog) puts its vector table in 0x20000000-0x200000BF and maps SRAM at address 0.
   That RAM is below Aura's region (it starts at 0x20001028): it belongs to the bootloader, whose
   forwarding interrupt handlers (SysTick, USART1 and the other UserVectors) still run on every
   interrupt. If one of them keeps a counter or a buffer there, it overwrites the table and the
   next interrupt through that entry faults, which with txdog means a reboot: the voice image with
   the table (acd06ba) reset in a loop about 5 times a second.
   This watches 0x20000000-0x20000FFF for one second, receive only, touching nothing, and says
   (under 60 characters):
     lo sp=20001000 chg=000000000000 blk=0000
   sp: the bootloader's initial stack pointer (its vector 0); chg: the words of 0x20000000-0xBF
   (the table's 48) that changed, bit i = word i, word 47 first; blk: the 256-byte blocks of
   0x20000000-0x20000FFF whose sum changed, bit i = block i. Returns the number of table words
   that changed. API level 1 (say, ticks). SPDX-License-Identifier: MIT */
#include "../k6_mod.h"

#define NVEC 48u
#define NBLK 16u

static inline uint32_t rd(uint32_t a) { return *(volatile uint32_t *)(uintptr_t)a; }

static char *put(char *p, const char *s) {
  while (*s) *p++ = *s++;
  return p;
}

static char *hex(char *p, uint32_t v, int digits) {
  const char *x = "0123456789abcdef";
  while (digits--) *p++ = x[(v >> (4 * digits)) & 15];
  return p;
}

static uint32_t blk_sum(uint32_t b) {
  uint32_t a = 0x20000000u + 256u * b, s = 0, i;
  for (i = 0; i < 64u; i++) s = (s << 1 | s >> 31) ^ rd(a + 4u * i);
  return s;
}

int k6_main(const k6_api_t *api) {
  uint32_t snap[NVEC], sum[NBLK], lo = 0, hi = 0, blk = 0, i, n = 0, t;
  char line[64], *p;
  for (i = 0; i < NVEC; i++) snap[i] = rd(0x20000000u + 4u * i);
  for (i = 0; i < NBLK; i++) sum[i] = blk_sum(i);
  t = api->ticks();
  while (((api->ticks() - t) & 0xFFFFu) < 10000u) {        /* one second */
    for (i = 0; i < NVEC; i++)
      if (rd(0x20000000u + 4u * i) != snap[i]) {
        if (i < 32u) lo |= 1u << i;
        else hi |= 1u << (i - 32u);
      }
    for (i = 0; i < NBLK; i++)
      if (blk_sum(i) != sum[i]) blk |= 1u << i;
  }
  for (i = 0; i < 32u; i++) n += (lo >> i & 1u) + (hi >> i & 1u);
  p = hex(put(line, "lo sp="), rd(0x08000000u), 8);
  p = hex(put(p, " chg="), hi, 4);
  p = hex(p, lo, 8);
  p = hex(put(p, " blk="), blk, 4);
  *p = 0;
  api->say(line);
  return (int)n;
}
