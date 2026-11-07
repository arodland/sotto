/* iflive_k6t: iflive.c's 4FSK receiver with templates trained on the K6's own gp0cap captures
   of the IC-7100 (c/k6/ifrx_tables.h under IFRX_T4_K6, `tools/k6_ifcap.py tables`) in place
   of the model's: 2.3% BER held out on those captures against 5.6%. docs/k6-if-modem.md.
   SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 2
#define K6_MODULE_OPT z
#define IFRX_T4_K6 1
#include "iflive.c"
