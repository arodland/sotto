/* gp0cap with the AFC off (REG_73 bit 4, then an RX restart so it takes hold): where a steady
   USB tone really lands on the IF, and whether it holds still (docs/k6-ft8-wspr.md, run 5).
   SPDX-License-Identifier: MIT */
#define CAP_AFC_OFF 1
#include "gp0cap.c"
