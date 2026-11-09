/* aiscap_dual: both AIS channels in one capture (docs/k6-ais.md "Run 6"). Receive only. Run 5
   showed the GP0 tap is taken before the channel filter: a station D kHz off the tuned frequency
   lands at |16.9 + D| kHz in the counts, folded at the count rate (~83.8 kHz with RAW_TRIG).
   Tuned to 162.009 MHz, channel A (34 kHz below) lands at ~17 kHz, mirrored, and channel B (16
   kHz above) at ~33 kHz, so the two sit apart in one stream. Both "channels" of aiscap tune the
   same frequency here; the trigger watches the whole band (RAW_TRIG). The base station sends
   message 4 on A and then on B 53 ms later, so each capture holds 86 ms after the trigger
   (7,216 counts) to catch both. On the host, each capture is decoded twice, once at each
   channel's place. SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 2
#define K6_MODULE_OPT z
#define CH_A_HZ 162009000u
#define CH_B_HZ 162009000u
#define SWITCH_S 60u
#define RUN_S 600u
#define PRE 384u
#define NCAP 7600u
#define TRIG_X 2
#define DUMP_DB 9
#define MAXCAP 60u
#define RAW_TRIG 1
#include "aiscap.c"
