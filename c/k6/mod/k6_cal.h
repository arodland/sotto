/* k6_cal.h: the radio's calibration record, in SPI flash at KM_CAL_ADDR (module API level 4:
   cal_read / cal_write; docs/k6-modules.md "Calibration").

   One record of 32 bytes, written by the ifcal module (c/k6/mod/ifcal.c) and read by any
   module that taps the receive IF (gp0rx.h with GP0_CAL):

     0  'K' '6' 'C' 'L'
     4  version (u16, 1), flags (u16: bit 0 = the IF offset is good)
     8  off_chz (i32): where a bare carrier on hz lands on the GP0 IF, less the nominal
        26 MHz / 1536 = 16927.083 Hz, in hundredths of a Hz. Run 5 (2026-11-06, the IC-7100 on
        440.850 MHz, AFC off) read about -14550: the K6 and the IC-7100 together, as a pair
     12 ppb (i32): the same as parts per billion of hz, for other frequencies
     16 hz (u32): the frequency it was measured on
     20 blocks (u16) averaged, spread_dhz (u16): their highest less their lowest, 0.1 Hz
     24 rssi (i16, dBm, the carrier's), 2 spare bytes
     28 check (u16): the sum of the 16-bit words 0..13, plus 0x6B36; 2 spare bytes

   A module mixing at the nominal IF plus x (or minus x, below it) mixes at that plus
   k6cal_off_chz() / 100 instead, either way. Everything here is static inline and divide-free,
   for modules. SPDX-License-Identifier: MIT */
#ifndef K6_CAL_H
#define K6_CAL_H
#include "../k6_mod.h"

#define K6CAL_BYTES 32u
#define K6CAL_NOMINAL_CHZ 1692708 /* 26 MHz / 1536 in 0.01 Hz */
#define K6CAL_MAGIC 0x4C43364Bu  /* "K6CL" as a little-endian word */

typedef struct {
  uint8_t magic[4];
  uint16_t version, flags;
  int32_t off_chz, ppb;
  uint32_t hz;
  uint16_t blocks, spread_dhz;
  int16_t rssi;
  uint16_t spare0;
  uint16_t check, spare1;
} k6_cal_t;

static inline uint16_t k6cal_sum(const k6_cal_t *c) {
  const uint16_t *w = (const uint16_t *)c;
  uint32_t s = 0x6B36, i;
  for (i = 0; i < 14; i++) s += w[i];
  return (uint16_t)s;
}

/* 0 and *c filled if the radio has a good record (API level 4 and the check), else -1 */
static inline int k6cal_get(const k6_api_t *api, k6_cal_t *c) {
  if (api->level < 4 || api->cal_read(c, K6CAL_BYTES) != (int)K6CAL_BYTES) return -1;
  const uint32_t *w = (const uint32_t *)c;
  /* "K6CL", then version 1 and flags bit 0 (the IF offset is good) as one word */
  if (w[0] != K6CAL_MAGIC || (w[1] & 0x1FFFFu) != 0x10001u || c->check != k6cal_sum(c))
    return -1;
  return 0;
}

/* The IF offset at hz, 0.01 Hz, from ppb (hz in 16384 Hz steps): 0.12% high, 0.2 Hz at 165 Hz,
   so no case for the frequency it was measured on. |ppb| up to 4500 (2 kHz at 440 MHz) and hz
   up to 450 MHz stay inside 32 bits */
static inline int32_t k6cal_off_chz(const k6_cal_t *c, uint32_t hz) {
  int32_t v = (c->ppb * (int32_t)(hz >> 14)) >> 6;
  return (v * 215) >> 11; /* x 16384 x 64 / 1e7, as x 215 / 2048 */
}

/* the offset as a step of a mixer that turns 2^32 a cycle of 96 MHz (gp0rx.h's KMIX units):
   x 2^32 / 9.6e9, as x 458 / 1024 (0.03% low) */
static inline int32_t k6cal_kmix(int32_t off_chz) { return (off_chz * 458) >> 10; }

#endif
