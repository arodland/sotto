/* cwlisten_lo: cwlisten with the tone bank moved down to 400-1300 Hz (60 Hz steps), for
   low-pitched CW IDs. A real one on 443.525 (2026-11-08 07:37Z, "...7 RPT", ~400 Hz, under
   voice) sits below the stock 600-1350 Hz bank; on the replay bench (tools/k6_cwbench.py
   --td-lo 400 --td-step 60) this bank still reads WA7DMR (1000 Hz) and VE7RVA (800 Hz) and
   locks onto the 400 Hz one. This module checks that the FD6818's detector follows a 400 Hz
   tone on the air before the explorer's bank (tonedet.h) moves. SPDX-License-Identifier: MIT */
#define K6_MODULE_OPT z
#define TONE_LO 400
#define TONE_STEP 60
#include "cwlisten.c"
