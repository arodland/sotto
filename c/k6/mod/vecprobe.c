/* vecprobe: can Aura catch its own HardFaults? (2026-11-08, after the K6 hung keyed for 16 min)

   The KD32F328 has no VTOR, and the bootloader leaves its own vector table (flash 0x08000000)
   in use: its HardFault vector is a `b .` loop, so a fault anywhere hangs the radio silently,
   with the transmitter on if it was keyed (docs/k6-modules.md, Security). An STM32F0-style
   SYSCFG can map SRAM at address 0 instead (CFGR1 MEM_MODE = 3): with a copy of the bootloader's
   table at 0x20000000 whose HardFault entry is Aura's, a fault would reach a handler that can
   reset the radio (boot soft-resets the FD6818, transmitter off). Images that put the message
   store at 0x20000100 hung at boot (2026-11-04), so this checks first that the low RAM takes
   writes at run time, then tries the remap, faults once on purpose and returns, and puts
   everything back. Receive only, touches no radio register. Says (under 60 characters):
     vec cfgr1=00000000 ram=30/30 alias=1 fault=1 back=1
   cfgr1: SYSCFG CFGR1 as found (00000002 on the K6, 2026-11-08 17:43Z: MEM_MODE 10, main flash); ram: words of 0x20000000-0x200000BF that took two patterns, of
   48; alias: address 0 read the SRAM copy once remapped; fault: the deliberate UDF reached this
   module's handler (it skips the instruction and returns); back: MEM_MODE and the RAM restored.
   If the remap or the fault goes wrong the radio hangs (no transmitter is on): power-cycle it.
   Upload it without ARQ (modem.k6 mod + an F/1800 send), so the K6 sends no report.
   Returns alias | fault << 1 | back << 2. API level 1 (say). SPDX-License-Identifier: MIT */
#include "../k6_mod.h"

#define RCC_APB2ENR 0x40021018u
#define SYSCFG_CFGR1 0x40010000u
#define SRAM0 0x20000000u
#define BOOT_VEC 0x08000000u
#define NVEC 48u                  /* 16 system + 29 IRQs, rounded up; 45-47 unused */
#define FLAG (SRAM0 + 4u * 47u)   /* the handler's mark, in an unused entry of the copy */

static inline uint32_t rd(uint32_t a) { return *(volatile uint32_t *)(uintptr_t)a; }
static inline void wr(uint32_t a, uint32_t v) { *(volatile uint32_t *)(uintptr_t)a = v; }

/* HardFault: mark FLAG with the faulting PC, step over the 2-byte UDF and return */
__attribute__((naked)) static void vp_fault(void) {
  __asm volatile(
      "mrs r0, msp\n"
      "ldr r1, [r0, #24]\n"      /* the stacked PC */
      "movs r2, #0x20\n"
      "lsls r2, r2, #24\n"
      "adds r2, #0xBC\n"         /* FLAG = 0x200000BC */
      "str r1, [r2]\n"
      "adds r1, #2\n"
      "str r1, [r0, #24]\n"
      "bx lr\n");
}

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
  uint32_t save[NVEC], i, ok = 0, apb2 = rd(RCC_APB2ENR), cfgr1, alias, fault, back;
  char line[64], *p;
  for (i = 0; i < NVEC; i++) save[i] = rd(SRAM0 + 4u * i);
  for (i = 0; i < NVEC; i++) {               /* does the low RAM take writes now? */
    uint32_t a = 0xA5A5A5A5u ^ (SRAM0 + 4u * i);
    wr(SRAM0 + 4u * i, a);
    if (rd(SRAM0 + 4u * i) == a) {
      wr(SRAM0 + 4u * i, ~a);
      ok += rd(SRAM0 + 4u * i) == ~a;
    }
  }
  wr(RCC_APB2ENR, apb2 | 1u);                 /* SYSCFG's clock */
  cfgr1 = rd(SYSCFG_CFGR1);
  alias = fault = 0;
  /* MEM_MODE x0 is main flash at 0 (the K6 reads 2: the boot pins' value), 01 system flash */
  if (ok == NVEC && (cfgr1 & 1u) == 0) {
    for (i = 0; i < NVEC; i++) wr(SRAM0 + 4u * i, rd(BOOT_VEC + 4u * i));
    wr(SRAM0 + 12u, (uint32_t)(uintptr_t)&vp_fault | 1u);
    wr(FLAG, 0);
    wr(SYSCFG_CFGR1, (cfgr1 & ~3u) | 3u);     /* SRAM at address 0 */
    alias = rd(12u) == rd(SRAM0 + 12u) && rd(12u) != rd(BOOT_VEC + 12u);
    if (alias) {
      __asm volatile(".short 0xde00" ::: "memory");   /* UDF #0: a HardFault */
      fault = rd(FLAG) != 0;
    }
    wr(SYSCFG_CFGR1, cfgr1);                  /* flash at address 0 again */
  }
  for (i = 0; i < NVEC; i++) wr(SRAM0 + 4u * i, save[i]);
  back = (rd(SYSCFG_CFGR1) & 3u) == (cfgr1 & 3u);
  for (i = 0; i < NVEC; i++) back = back && rd(SRAM0 + 4u * i) == save[i];
  wr(RCC_APB2ENR, apb2);
  p = hex(put(line, "vec cfgr1="), cfgr1, 8);
  p = hex(put(p, " ram="), ok, 2);
  p = hex(put(p, "/"), NVEC, 2);
  p = hex(put(p, " alias="), alias, 1);
  p = hex(put(p, " fault="), fault, 1);
  p = hex(put(p, " back="), back, 1);
  *p = 0;
  api->say(line);
  return (int)(alias | fault << 1 | back << 2);
}
