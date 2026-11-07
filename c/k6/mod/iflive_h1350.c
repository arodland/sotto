/* iflive_h1350: iflive.c's receiver for 4FSK at 1350 baud with four tones on a half-baud grid
   (675, 1350, 2025, 2700 Hz: every symbol starts on audio phase 0 or a half turn, so one
   template a tone, ../ifrx_tables_4h.h), against `tools/k6_ifcap.py live 4h1350`. The faster
   4FSK's live bit error rate on the bench before any image carries it (docs/k6-if-fec.md).
   SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 2
#define K6_MODULE_OPT z
#define IFRX_ONLY 2 /* IFRX_4H */
#define IFRX_BAUD 1350
#include "iflive.c"
