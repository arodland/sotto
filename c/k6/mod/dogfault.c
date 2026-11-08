/* dogfault: bench test of the crash backstop (hal::txdog in the Aura patch, 2026-11-08). Says
   "dog fault: UDF next" and executes UDF #0, a HardFault. With txdog the radio reboots at once
   and the next boot says
     K6 FAULT last boot pc=2000xxxx lr=...
   with pc inside this module; without it the bootloader's `b .` hangs the radio (receive
   only): power-cycle it. Upload it without ARQ. API level 1. SPDX-License-Identifier: MIT */
#include "../k6_mod.h"

int k6_main(const k6_api_t *api) {
  uint32_t t = api->ticks();
  api->say("dog fault: UDF next");
  while (((api->ticks() - t) & 0xFFFF) < 2000) {}   /* 200 ms: the say line leaves first */
  __asm volatile(".short 0xde00" ::: "memory");
  return -1;
}
