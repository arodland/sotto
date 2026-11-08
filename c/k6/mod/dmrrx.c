/* dmrrx: dmrdet (mute DMR on one frequency) plus its metadata: colour code, slot, talkgroup,
   source ID and talker alias of each call, and the CSBKs and data headers that pass their
   CRC (docs/k6-dmr.md, "Decoding"; c/k6/mod/dmrdec.inc, c/k6/dmrburst.h). Receive only; no
   voice (AMBE+2 is out). Lines, besides dmrdet's:
     L TS1 CC1 TG 3027 src 3025107 F0 [comb 3]   a voice LC header or terminator (FLCO 0 group,
                                                 3 unit to unit; comb: only the soft sum of
                                                 this many copies decoded)
     E TS1 TG 3027 src 3025107                   the embedded LC of a voice superframe, when it
                                                 says something new (a late entry)
     A AG7EW TEST                                talker alias so far
     K TS1 bd00800104c2bf301cd5                  a CSBK's 10 bytes (here a preamble)
     H TS1 ...                                   a data header's
   On an image with the module screen (level 6) the LCD shows "DMR CC1 TS1", "TG 3027",
   "ID 3025107" and the alias. Code and stack: ~6 KB of code (the shared module arena, PR #142,
   not the old 4 KB store), ~5 KB of stack.
   SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 2       /* the screen (level 6) only where the image has it */
#define K6_MODULE_OPT s         /* -Os: the decoder's inner loops want the speed */
#define DMR_DECODE 1
#ifndef RSZ
#define RSZ 2048u               /* 373 us of DMA: an idle burst's decode fits; a header's may
                                   not, and the clock counts the laps it missed */
#endif
#include "dmrdet.c"
