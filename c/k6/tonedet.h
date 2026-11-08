/* The FD6818's tone detector set up for CW (docs/k6-cwid.md): single-tone mode, 16 tones from
   400 to 1300 Hz in 60 Hz steps (coefficient 128 cos(2 pi f / 8463 Hz); 600-1350 until
   2026-11-08, when a repeater's 400 Hz ID fell below it and the bench heard 400 Hz IDs with
   this bank, mod/cwlisten_lo.c), threshold 24, read in REG_0B (bit 12
   a tone now, [11:8] which). Shared by xlisten (Listen for ID) and xscan (the peek at an up
   carrier). BK4819 register numbers (Jeff's 0x4819 chip). SPDX-License-Identifier: MIT */
#ifndef TONEDET_H
#define TONEDET_H
#include <stdint.h>
#include "k6_mod.h"

#define TD_TH 24
#define TD_IDLE 1
#define TD_LO 400
#define TD_STEP 60
#define TD_ON 0x1000u      /* REG_0B: a tone now */

static const uint8_t td_coef[16] = {
    122, 121, 119, 116, 114, 111, 108, 105, 102, 98, 94, 90, 86, 82, 77, 73};

static inline void td_on(const k6_api_t *api) {
  int i;
  for (i = 0; i < 16; i++) api->fd_write(0x09, (uint16_t)((i << 12) | td_coef[i]));
  api->fd_write(0x24, (uint16_t)(0x8000 | (TD_TH << 7) | (TD_IDLE << 6) | 0x20 | 0x0F));
  api->fd_write(0x3F, 0x0800);
  api->fd_write(0x02, 0);
}

static inline void td_off(const k6_api_t *api) { api->fd_write(0x24, 0); }

/* REG_0B now; a pending tone interrupt is cleared */
static inline uint16_t td_read(const k6_api_t *api) {
  uint16_t b = api->fd_read(0x0B);
  if (api->fd_read(0x0C) & 1) {
    api->fd_write(0x02, 0);
    (void)api->fd_read(0x02);
  }
  return b;
}
#endif
