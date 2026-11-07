/* Very-low-rate sotto decoder (sotto 600, sotto 400) in fixed point, integer only: the
 * K6 pager's. The decoder itself is sotto_vlr_dec.inc (shared with the float build); here it
 * runs on the v3.7 fixed-point decoder core.
 * SPDX-License-Identifier: MIT */
#define SOTTO_BUILD
#include <stdlib.h>
#include <string.h>
#ifdef VLR_DEBUG
#include <stdio.h>
#endif
#include "../sotto_vlr.h"
#include "../sotto_vlr_tables.h"
#include "fx.h"
#include "fx_internal.h"

typedef char vlr_nc_matches_core[VLR_NC == FX_NC ? 1 : -1];

#define vlr_params_t fx_fparams_t
#define VLR_C(q16) (q16)
#include "../sotto_vlr_dec.inc"
