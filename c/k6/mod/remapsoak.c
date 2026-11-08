/* remapsoak: does the K6 keep running on a copy of the bootloader's vector table in SRAM?
   (2026-11-08)
   hal::txdog copies the bootloader's 48 vectors (flash 0x08000000) to 0x20000000, points
   HardFault and TIM14 at Aura and maps SRAM at address 0; the voice image with it (acd06ba)
   reset in a loop about 5 times a second. vectab showed the copy is the live table and loram
   that nothing writes 0x20000000-0x20000FFF while Aura runs; vecprobe held the remap for a few
   microseconds only. This holds it for 10 s with interrupts on (SysTick, USART1, DMA through the
   copy): 3 s with only HardFault changed, then 7 s with a TIM14 update handler too, enough for one
   6.55 s wrap. Then it puts everything back.
   HardFault while remapped: this module's handler sends
     K6 RS FAULT pc=xxxxxxxx lr=xxxxxxxx psr=xxxxxxxx
   straight to USART1 (waiting on each byte), maps flash at 0 again and resets the radio, which
   boots as usual. Receive only, touches no radio register. Says (under 60 characters):
     rs cfgr1=00000002 alias=1 a=1 t14=01 back=1 pri=0
   alias: address 0 read the copy; a: the 3 s with the copy passed; t14: TIM14 updates handled in
   the 7 s; back: MEM_MODE, TIM14's interrupt and the RAM restored. Upload it without ARQ.
   If the core runs modules with interrupts off, the soak says pri=1 and proves little.
   Returns alias | a << 1 | back << 2 | t14 << 8. API level 1 (say, ticks).
   SPDX-License-Identifier: MIT */
#include "../k6_mod.h"

#define RCC_APB2ENR 0x40021018u
#define SYSCFG_CFGR1 0x40010000u
#define TIM14_DIER 0x4000200Cu
#define TIM14_SR 0x40002010u
#define NVIC_ISER 0xE000E100u
#define NVIC_ICER 0xE000E180u
#define USART1_ISR 0x4001381Cu
#define USART1_TDR 0x40013828u
#define AIRCR 0xE000ED0Cu
#define SRAM0 0x20000000u
#define BOOT_VEC 0x08000000u
#define NVEC 48u
#define V_TIM14 35u
#define CFG_SAVE (SRAM0 + 4u * 45u)   /* CFGR1 as found, for the fault handler; 45-47 unused */
#define T14_N (SRAM0 + 4u * 46u)      /* TIM14 updates handled */

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

/* from the HardFault stub, with the stacked frame (r0-r3, r12, lr, pc, psr) */
__attribute__((used, noreturn)) void rs_fault_c(const uint32_t *f) {
  char line[64], *p = line, *q;
  uint32_t n;
  p = hex(put(p, "\r\nK6 RS FAULT pc="), f[6], 8);
  p = hex(put(p, " lr="), f[5], 8);
  p = hex(put(p, " psr="), f[7], 8);
  p = put(p, "\r\n");
  for (q = line; q < p; q++) {
    for (n = 0; n < 200000u && !(rd(USART1_ISR) & 0x80u); n++) {}   /* TXE, or give up */
    wr(USART1_TDR, (uint8_t)*q);
  }
  for (n = 0; n < 200000u && !(rd(USART1_ISR) & 0x40u); n++) {}     /* TC: the last byte out */
  wr(SYSCFG_CFGR1, rd(CFG_SAVE));                                     /* flash at 0 again */
  __asm volatile("dsb" ::: "memory");
  wr(AIRCR, 0x05FA0004u);                                             /* SYSRESETREQ */
  for (;;) {}
}

__attribute__((naked)) static void rs_fault(void) {
  __asm volatile("mrs r0, msp\n"
                 "b rs_fault_c\n");
}

static void rs_tim14(void) {
  wr(TIM14_SR, 0);
  wr(T14_N, rd(T14_N) + 1u);
}

static uint32_t primask(void) {
  uint32_t r;
  __asm volatile("mrs %0, primask" : "=r"(r));
  return r & 1u;
}

static void wait_ms(const k6_api_t *api, uint32_t ms) {
  while (ms) {
    uint32_t step = ms > 5000u ? 5000u : ms, t = api->ticks();
    while (((api->ticks() - t) & 0xFFFFu) < step * 10u) {}
    ms -= step;
  }
}

int k6_main(const k6_api_t *api) {
  uint32_t save[NVEC], i, apb2 = rd(RCC_APB2ENR), cfgr1, dier, alias = 0, a = 0, t14 = 0, back;
  uint32_t off = primask();                   /* interrupts as the core left them, restored */
  char line[64], *p;
  for (i = 0; i < NVEC; i++) save[i] = rd(SRAM0 + 4u * i);
  wr(RCC_APB2ENR, apb2 | 1u);                 /* SYSCFG's clock */
  cfgr1 = rd(SYSCFG_CFGR1);
  dier = rd(TIM14_DIER);
  if ((cfgr1 & 1u) == 0) {
    __asm volatile("cpsid i" ::: "memory");
    for (i = 0; i < NVEC; i++) wr(SRAM0 + 4u * i, rd(BOOT_VEC + 4u * i));
    wr(SRAM0 + 12u, (uint32_t)(uintptr_t)&rs_fault | 1u);
    wr(CFG_SAVE, cfgr1);
    wr(T14_N, 0);
    wr(SYSCFG_CFGR1, (cfgr1 & ~3u) | 3u);     /* SRAM at address 0 */
    alias = rd(12u) == rd(SRAM0 + 12u) && rd(12u) != rd(BOOT_VEC + 12u) &&
            rd(4u * 15u) == rd(BOOT_VEC + 4u * 15u);
    __asm volatile("cpsie i" ::: "memory");
    if (alias) {
      wait_ms(api, 3000);                     /* the bootloader's handlers, through the copy */
      a = 1;
      __asm volatile("cpsid i" ::: "memory");
      wr(SRAM0 + 4u * V_TIM14, (uint32_t)(uintptr_t)&rs_tim14 | 1u);
      wr(TIM14_SR, 0);
      wr(TIM14_DIER, dier | 1u);              /* update interrupt: one per 6.55 s wrap */
      wr(NVIC_ISER, 1u << 19);
      __asm volatile("cpsie i" ::: "memory");
      wait_ms(api, 7000);
      __asm volatile("cpsid i" ::: "memory");
      wr(TIM14_DIER, dier);
      wr(NVIC_ICER, 1u << 19);
      wr(TIM14_SR, 0);
      t14 = rd(T14_N);
    } else {
      __asm volatile("cpsid i" ::: "memory");
    }
    wr(SYSCFG_CFGR1, cfgr1);                  /* flash at address 0 again */
    for (i = 0; i < NVEC; i++) wr(SRAM0 + 4u * i, save[i]);
    if (!off) __asm volatile("cpsie i" ::: "memory");
  }
  back = (rd(SYSCFG_CFGR1) & 3u) == (cfgr1 & 3u) && rd(TIM14_DIER) == dier;
  for (i = 0; i < NVEC; i++) back = back && rd(SRAM0 + 4u * i) == save[i];
  wr(RCC_APB2ENR, apb2);
  p = hex(put(line, "rs cfgr1="), cfgr1, 8);
  p = hex(put(p, " alias="), alias, 1);
  p = hex(put(p, " a="), a, 1);
  p = hex(put(p, " t14="), t14, 2);
  p = hex(put(p, " back="), back, 1);
  p = hex(put(p, " pri="), off, 1);
  *p = 0;
  api->say(line);
  return (int)(alias | a << 1 | back << 2 | t14 << 8);
}
