/* iflive_tsweep: iflive.c's 4FSK receiver with the timing loop's zero moved a different way on
   each step, all at one gain (gain step 2, REG_13 0x03BE), to find where on the real signal
   the decisions are best. The timing detector's output, (|L|^2 - |E|^2) / |C|^2 in Q12, gets
   (step - 4) * 600 added, so the loop settles where the detector reads minus that: steps 0..8
   run -2400 .. +2400, step 4 is the plain receiver. On the gp0cap captures the detector's
   slope is about 0.58 a half-sample (4FSK), so +-2400 is about +-1 half-sample (positive:
   later). Only once tracking: on the lead it threw the lock out past +-600 (emulator). The
   bias rides in ifrx_t's slips, unkept in a module (IFRX_STATS 0). Run and read
   it as iflive (tools/k6_ifcap.py live 4fsk, livelog), less the r= and i= columns. docs/k6-if-modem.md.
   SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 2
#define K6_MODULE_OPT z
#define IFRX_DT_NOISE(s) ((s)->state == IFRX_TRACK ? (int32_t)(s)->slips : 0)
#define IFRX_DF_NOISE(s) 0
#define STEP_GAIN(step) 2
#define LINE_LEAN /* its line has no RSSI or idle: no room */
#define STEP_HOOK(rx, step) ((rx)->slips = (uint32_t)(((int32_t)(step) - 4) * 600))
#include "iflive.c"
