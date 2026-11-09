/* aiscap_sweep: how wide is the FD6818's IF, as the MCU sees it on the GP0 tap? (docs/k6-ais.md
   "RDS": RDS needs it to pass sidebands 19-57 kHz off the carrier.) Receive only. The test signal
   is the shore base station heard in runs 2 and 3 (162.025 MHz, a burst every 20 s and another
   half a second later, 12-15 dB over the floor at the centre). Each 40 s turn tunes the K6 that
   many kHz off it, so the burst lands that far from the 16.9 kHz IF; the trigger watches the
   whole band the counts carry (RAW_TRIG), and up to 2 bursts a turn are dumped. On the host,
   each capture's spectrum shows where the burst came through and how strongly.
   SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 2
#define K6_MODULE_OPT z
#define SWEEP_HZ 162025000u
#define SWEEP_KHZ 0, 5, -5, 10, -10, 15, -15, 20, -20, 30, -30, 40, -40, 60, -60
#define SWITCH_S 40u
#define RUN_S 620u
#define TRIG_X 2
#define DUMP_DB 2
#define TURN_CAPS 2u
#define MAXCAP 30u
#define RAW_TRIG 1
#include "aiscap.c"
