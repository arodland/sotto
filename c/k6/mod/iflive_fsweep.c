/* iflive_fsweep: iflive.c's 4FSK receiver with the AFC loop's zero moved a different way on
   each step, at two timing settings, all at one gain (gain step 2, REG_13 0x03BE). Steps 0-4
   add ((step % 5) - 2) * 240 (Q12) to the AFC detector's output, Im(c2 c1*) / mean |c|^2,
   and steps 5-9 the same with the timing detector's +600 of iflive_tsweep (its best step on
   Jeff's K6, 2026-11-07: 3.4% against 4.6%). On the gp0cap captures the AFC detector's slope
   is about 0.0022 a Hz (4FSK), so 240 is about 27 Hz. Both biases only once tracking, riding
   in ifrx_t's slips and lead_err (unkept in a module, IFRX_STATS 0). Its line has no r= or i=.
   docs/k6-if-modem.md. SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 2
#define K6_MODULE_OPT z
#define IFRX_DT_NOISE(s) ((s)->state == IFRX_TRACK ? (int32_t)(s)->slips : 0)
#define IFRX_DF_NOISE(s) ((s)->state == IFRX_TRACK ? (s)->lead_err : 0)
#define STEP_GAIN(step) 2
#define LINE_LEAN
#define STEP_HOOK(rx, step)                                                                     \
  ((rx)->slips = (step) >= 5 ? 600u : 0u, (rx)->lead_err = ((int32_t)(step) % 5 - 2) * 240)
#include "iflive.c"
