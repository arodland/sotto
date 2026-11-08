/* surfauto: surf.c for unattended bench runs (docs/k6-surf.md): SIDE1 presses itself every
   SURF_AUTO_MS, each press says "sel <MHz> <dBm>" first (over-air telemetry with
   tools/k6_upload.py --telemetry 12), the window wraps to the band's bottom at the top, and the
   module leaves after SURF_AUTO_S. Receive only. SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 6
#define K6_MODULE_OPT z
#define SURF_AUTO_S 60
#define SURF_AUTO_MS 4000
#include "surf.c"
