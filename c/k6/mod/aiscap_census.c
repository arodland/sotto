/* aiscap_census: an hour of aiscap_dual (162.009 MHz, both AIS channels in one capture), to count
   the ships the bench can hear before building a ship list module. Receive only. Dumps bursts 8
   dB over the floor (the 4 s local junk reaches 7); the host decodes them with
   `python3 -m sim.k6if.ais cap LOG --tune 162009000`. SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 2
#define K6_MODULE_OPT z
#define CH_A_HZ 162009000u
#define CH_B_HZ 162009000u
#define SWITCH_S 60u
#define RUN_S 3600u
#define PRE 384u
#define NCAP 7600u
#define TRIG_X 2
#define DUMP_DB 8
#define MAXCAP 400u
#define RAW_TRIG 1
#include "aiscap.c"
