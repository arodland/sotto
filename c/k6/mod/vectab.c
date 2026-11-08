/* vectab: is the bootloader's vector table what the K6 really runs on? (2026-11-08)
   txdog (hal::txdog) copied 48 words from flash 0x08000000 to SRAM and mapped SRAM at 0; the
   voice image built with it (acd06ba) then reset in a loop about 5 times a second, before its
   first line. vecprobe only ever took a HardFault through the copy, never an IRQ, so a copy that
   doesn't match the live table would pass it and fail at the first SysTick or USART1 interrupt.
   This compares the live table (address 0, with flash mapped there) against 0x08000000 and checks
   each of entries 2-47 is 0 or an odd address in the bootloader (0x08000000-0x08001FFF).
   Receive only, touches nothing. Says (under 60 characters):
     vt same=30 bad=00 v15=080001c5 v43=080001d1
   same: entries equal at both addresses (hex, of 48 = 0x30); bad: entries 2-47 at address 0 that
   are neither 0 nor a bootloader address; v15, v43: SysTick's and USART1's at address 0.
   Returns same | bad << 8. API level 1 (say). SPDX-License-Identifier: MIT */
#include "../k6_mod.h"

#define NVEC 48u

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

int k6_main(const k6_api_t *api) {
  uint32_t i, same = 0, bad = 0;
  char line[64], *p;
  for (i = 0; i < NVEC; i++) {
    uint32_t v = rd(4u * i);
    same += v == rd(0x08000000u + 4u * i);
    if (i >= 2 && v != 0 && (!(v & 1u) || v < 0x08000000u || v >= 0x08002000u)) bad++;
  }
  p = hex(put(line, "vt same="), same, 2);
  p = hex(put(p, " bad="), bad, 2);
  p = hex(put(p, " v15="), rd(4u * 15u), 8);
  p = hex(put(p, " v43="), rd(4u * 43u), 8);
  *p = 0;
  api->say(line);
  return (int)(same | bad << 8);
}
