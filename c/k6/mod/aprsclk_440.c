/* aprsclk_440: aprsclk.c on the bench channel, 440.850 MHz, for 5 minutes: the on-air loopback.
   The IC-7100 sends timestamped test packets as AG7EW (python -m modem.aprs wav OUT.wav --now,
   then tools/k6_send.sh OUT.wav), so the K6's demodulator and its clock are checked against
   hamlet's NTP without leaving 70 cm (docs/k6-aprs-clock.md "Bench"). SPDX-License-Identifier: MIT */
#define K6_MODULE_OPT z
#define APRS_HZ 440850000u
#define RUN_MS 300000u
#include "aprsclk.c"
