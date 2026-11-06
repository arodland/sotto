/* gp0raw: gp0fm storing GP0's raw 22.9 M samples/s bitstream instead of its density, about
   0.8 ms a capture, so the host can test slower sampling rates (docs/k6-fd6818-audio.md).
   SPDX-License-Identifier: MIT */
#define GP0_RAW 1
#include "gp0fm.c"
