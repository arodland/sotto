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

#ifdef FX_HISS_FB                   /* the K6 hiss layer (sotto_fx_hiss.c) hooks in */
#define VLR_HISS
#define VLR_HISS_HOOK_T fx_vlr_hiss_hook_t
#endif
#define vlr_params_t fx_fparams_t
#define VLR_C(q16) (q16)
#include "../sotto_vlr_dec.inc"

#ifdef VLR_HISS
void sotto_vlr_dec_set_hook(struct sotto_vlr_dec *d, fx_vlr_hiss_hook_t hook, void *ctx) { d->hook = hook, d->hctx = ctx; }
const struct sotto_dec *sotto_vlr_dec_core(const struct sotto_vlr_dec *d) { return d->core; }
#endif
