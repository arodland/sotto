/* remapspeed: does mapping SRAM at address 0 slow the K6's code down? (2026-11-08)
   The diagnostic image with hal::txdog's SRAM vector table on (006ee1b vlrh-txdog) booted fine,
   but its boot timings ran about 20% slower than 843b8e0's (K6 CB bench 40 us a read against
   33-34). This times 20,000 calls of api->ticks (code in MCU flash) and a loop in this module
   (code in SRAM) in the current MEM_MODE, then the other one, then the current again, interrupts
   on throughout. Receive only, touches no radio register. Says (under 60 characters, times in
   hex 100 us ticks):
     rsp m2=0123 m3=0123 again=0123 loop 0123 0123
   m2: MEM_MODE 2 (flash at 0), m3: 3 (SRAM at 0), the current mode first, then the other, then
   the current again; loop: the SRAM loop in the current mode, then the other. On an image with
   txdog's table in (mode 3), TIM14's interrupt is masked while flash is at 0. Returns
   other << 16 | current. API level 1 (say, ticks).
   SPDX-License-Identifier: MIT */
#include "../k6_mod.h"

#define RCC_APB2ENR 0x40021018u
#define SYSCFG_CFGR1 0x40010000u
#define SRAM0 0x20000000u
#define BOOT_VEC 0x08000000u
#define NVEC 48u
#define N 20000u

static inline uint32_t rd(uint32_t a) { return *(volatile uint32_t *)(uintptr_t)a; }
static inline void wr(uint32_t a, uint32_t v) { *(volatile uint32_t *)(uintptr_t)a = v; }

static char *put(char *p, const char *s) {
  while (*s) *p++ = *s++;
  return p;
}

static char *hex(char *p, uint32_t v, int digits) {
  const char *x = "0123456789abcdef";
  while (digits--) *p++ = x[(v >> (4 * digits)) & 15];
  return p;
}

static uint32_t t_calls(const k6_api_t *api) {
  uint32_t i, t = api->ticks(), s = 0;
  for (i = 0; i < N; i++) s += api->ticks();
  __asm volatile("" : : "r"(s));
  return (api->ticks() - t) & 0xFFFFu;
}

static uint32_t t_loop(const k6_api_t *api) {
  volatile uint32_t s = 0;
  uint32_t i, t = api->ticks();
  for (i = 0; i < 20u * N; i++) s += i;
  return (api->ticks() - t) & 0xFFFFu;
}

int k6_main(const k6_api_t *api) {
  uint32_t save[NVEC], i, apb2 = rd(RCC_APB2ENR), cfgr1, cur, other, a, b, a2, la, lb;
  uint32_t iser;
  char line[64], *p;
  wr(RCC_APB2ENR, apb2 | 1u);
  cfgr1 = rd(SYSCFG_CFGR1);
  cur = cfgr1 & 3u;
  a = t_calls(api);
  la = t_loop(api);
  for (i = 0; i < NVEC; i++) save[i] = rd(SRAM0 + 4u * i);
  iser = rd(0xE000E100u);
  __asm volatile("cpsid i" ::: "memory");
  if (cur == 3u) {
    /* txdog's table is in: flash at 0 sends TIM14 and HardFault to the bootloader's `b .`, so
       TIM14's interrupt is masked meanwhile (a wrap waits, pending, till it comes back) */
    wr(0xE000E180u, 1u << 19);
    other = 2u;
  } else {
    for (i = 0; i < NVEC; i++) wr(SRAM0 + 4u * i, rd(4u * i));   /* the live table, as is */
    other = 3u;
  }
  wr(SYSCFG_CFGR1, (cfgr1 & ~3u) | other);
  __asm volatile("cpsie i" ::: "memory");
  b = t_calls(api);
  lb = t_loop(api);
  __asm volatile("cpsid i" ::: "memory");
  wr(SYSCFG_CFGR1, cfgr1);
  for (i = 0; i < NVEC; i++) wr(SRAM0 + 4u * i, save[i]);
  wr(0xE000E100u, iser & (1u << 19));
  __asm volatile("cpsie i" ::: "memory");
  a2 = t_calls(api);
  wr(RCC_APB2ENR, apb2);
  p = hex(put(line, "rsp m"), cur, 1);
  p = hex(put(p, "="), a, 4);
  p = hex(put(p, " m"), other, 1);
  p = hex(put(p, "="), b, 4);
  p = hex(put(p, " again="), a2, 4);
  p = hex(put(p, " loop "), la, 4);
  p = hex(put(p, " "), lb, 4);
  *p = 0;
  api->say(line);
  return (int)(b << 16 | a);
}
