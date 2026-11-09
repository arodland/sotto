/* aiscap_acars: aiscap.c's polled IF capture on the ACARS channels 131.550 and 130.025 MHz,
   receive only, for whether the K6 hears ACARS (AM, MSK 2400 bit/s: 1200 and 2400 Hz tones on
   the envelope) well enough to decode it (docs/k6-ais.md "ACARS"). Airband is below the K6's
   136-174 MHz filter. Each burst 6 dB over the floor is dumped (40 ms: the carrier and the
   tones' spectrum, not a whole message), 20 a channel, with the pager's AGC left on. The K6
   never transmits on airband; nothing here keys up. SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 2
#define K6_MODULE_OPT z
#define CH_A_HZ 130025000u
#define CH_B_HZ 131550000u
#define SWITCH_S 60u
#define RUN_S 120u
#define DUMP_DB 6
#define MAXCAP 40u
#define GAIN13 0u
#include "aiscap.c"
