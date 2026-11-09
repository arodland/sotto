/* gw_auto: GROUNDWAVE (gw.c) for hands-free bench runs: hot seat, both stations played by the bot. The bot aims (power from the
   distance, corrected by where its last shot came down), shops at random and fires 2.5 s into
   each turn; the module leaves after one game (or 10 minutes in the lobby). Keys still work.
   SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 6
#define K6_MODULE_OPT z
#define GW_AUTO 0
#include "gw.c"
