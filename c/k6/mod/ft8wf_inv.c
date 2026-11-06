/* ft8wf for a mirrored IF: the upper sideband below 26 MHz / 1536, so the window sits at the
   IF - FT8_AUDIO and mixes by e^{+j...} (c/k6/mod/ft8wf.c). Send this one when
   `tools/k6_ft8.py tone` says the K6's IF is mirrored. SPDX-License-Identifier: MIT */
#define K6_MODULE_OPT z
#define FT8_INV 1
#include "ft8wf.c"
