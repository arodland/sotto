/* Very-low-rate sotto decoder (sotto 600, sotto 400), floating point: sotto_vlr_dec.inc on the
 * float v3.7 decoder core. The same streams and the same fields as the fixed-point decoder
 * (fixed/sotto_fx_vlr.c, the K6's): the shapes are interpolated in dB Q16 and handed to the
 * core in dB. In the v3.8 build it also carries the hook that sotto 600 / 400 hiss
 * (sotto_vlr_dec16, sotto_hiss.c) feeds its 16 kHz layer from.
 * SPDX-License-Identifier: MIT */
#define SOTTO_BUILD
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include "sotto.h"
#include "sotto_internal.h"
#include "sotto_vlr.h"
#include "sotto_vlr_tables.h"

typedef char vlr_nc_matches_core[VLR_NC == SOTTO_K - 1 ? 1 : -1];

#ifdef SOTTO_V38
#define VLR_HISS
#define VLR_HISS_HOOK_T vlr_hiss_hook_t
#endif
#define vlr_params_t sotto_fparams_t
#define VLR_C(q16) ((q16) / 65536.0)
#include "sotto_vlr_dec.inc"

#ifdef VLR_HISS
void sotto_vlr_dec_set_hook(struct sotto_vlr_dec *d, vlr_hiss_hook_t hook, void *ctx) { d->hook = hook, d->hctx = ctx; }

const struct sotto_dec *sotto_vlr_dec_core(const struct sotto_vlr_dec *d) { return d->core; }
#endif
