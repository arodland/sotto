/* gw_call: GROUNDWAVE (gw.c) for hands-free bench runs: radio play, this radio calls (P1). The bot aims (power from the
   distance, corrected by where its last shot came down), shops at random and fires 2.5 s into
   each turn; the module leaves after one game (or 10 minutes in the lobby). Keys still work.
   SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 6
#define K6_MODULE_OPT z
#define GW_AUTO 1
#include "gw.c"
