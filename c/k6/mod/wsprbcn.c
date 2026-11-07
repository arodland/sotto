/* wsprbcn: wsprtx as a beacon: six WSPR frames, one every 10 minutes (an hour), each with its
   MCW ID; a held key stops it. SPDX-License-Identifier: MIT */
#define WSPR_REPEAT 6
#define WSPR_EVERY 5
#include "wsprtx.c"
