/* iflive_g2: iflive.c's 4FSK receiver at one gain on every step (gain step 2, REG_13 0x03BE),
   for runs where the sender changes something per segment instead (tools/k6_ifcap.py live
   4fsk --levels ...). docs/k6-if-modem.md. SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 2
#define K6_MODULE_OPT z
#define STEP_GAIN(step) 2
#include "iflive.c"
