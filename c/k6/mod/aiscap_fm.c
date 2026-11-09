/* aiscap_fm: aiscap.c's polled IF capture on two FM broadcast stations, 104.3 and 99.3 MHz, for
   whether RDS (57 kHz off the carrier, with the station's clock in group 4A) can reach the K6 at
   all (docs/k6-ais.md "RDS"). Every capture is dumped (no trigger: TRIG_X 0), 6 a station, the
   pager's AGC left on. A broadcast station swings +-75 kHz, so the IF spectrum of its captures
   shows the chip's IF passband: RDS needs it to reach 57 kHz. Receive only.
   SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 2
#define K6_MODULE_OPT z
#define CH_A_HZ 99300000u
#define CH_B_HZ 104300000u
#define SWITCH_S 8u
#define RUN_S 16u
#define TRIG_X 0
#define DUMP_DB 0
#define MAXCAP 12u
#define GAIN13 0u
#include "aiscap.c"
