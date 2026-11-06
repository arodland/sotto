/* iflive_ffsk: iflive.c's receiver for the sender's FFSK pass (tools/k6_ifcap.py run --only
   ffsk --lead 2). SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 2
#define K6_MODULE_OPT z
#define IFRX_ONLY 0 /* IFRX_FFSK */
#include "iflive.c"
