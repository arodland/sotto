/* sotto 1200 decoder in fixed point (integer only): the K6 pager's decoder.
 *
 * Same API and the same output, to within rounding, as the float decoder in ../sotto_lr.c:
 * each 48-bit superframe's fields become two 20 ms frames' fields (A as sent; B halfway
 * between this A and the next plus 1.5 x a stage-3 codeword, with its pitch and energy
 * stepped from the interpolated indices), which the v3.7 fixed-point decoder core
 * (sotto_fx_dec.c) synthesises. No tables beyond v3.7's. There is no fixed-point sotto 1200
 * encoder: sotto_lr_enc_create returns NULL in this build (the K6 only receives).
 * SPDX-License-Identifier: MIT */
#define SOTTO_BUILD
#include <stdlib.h>
#include <string.h>
#include "../sotto.h"
#include "../sotto_lr_tables.h"
#include "fx.h"
#include "fx_internal.h"
#include "fx_tables.h"

#define NC FX_NC

typedef struct {
  int16_t ca, wa, ea, sa[LR_NA], cb, dw, de, sb;
} lr_fields_t;

/* field widths in transmission order: v3.7's voicing, pitch, energy; stages 1-2 (9 + 9);
   B's voicing, pitch and energy steps; stage 3 (8) */
static const unsigned char lr_width[9] = {2, 7, 5, 9, 9, 2, LR_WB_BITS, LR_EB_BITS, 8};
typedef char lr_fields_are_9_int16[sizeof(lr_fields_t) == 9 * sizeof(int16_t) ? 1 : -1];

static void unpack(const unsigned char *b, lr_fields_t *f) {
  int16_t v[9];
  int i, j, pos = 0;
  for (i = 0; i < 9; i++) {
    int x = 0;
    for (j = 0; j < lr_width[i]; j++, pos++) x = (x << 1) | ((b[pos >> 3] >> (7 - (pos & 7))) & 1);
    v[i] = (int16_t)x;
  }
  memcpy(f, v, sizeof v);
}

/* A's shape, DCT coefficients dB Q16 */
static void a_shape(const int16_t *sa, int32_t *c) {
  int s, k;
  for (k = 0; k < NC; k++) c[k] = 0;
  for (s = 0; s < LR_NA; s++) {
    const int16_t *y = fx_cb[s] + sa[s] * FX_CBW;
    for (k = 0; k < NC; k++) c[k] += (int32_t)FX_CB_VAL(y, k) * fx_cb_step[s];
  }
}

static int wo_interp(int wa, int va, int wb, int vb) {
  if (va && vb) return (wa + wb + 1) >> 1;
  if (va) return wa;
  if (vb) return wb;
  return -1;
}

static int e_b(int ea, int en, int de) {
  if (ea == 0 && en == 0 && lr_e_tab[de] <= 0) return 0;
  int e = ((ea + en + 1) >> 1) + lr_e_tab[de];
  return e < 0 ? 0 : e > 31 ? 31 : e;
}

/* B's shape: (cA + cN) / 2 + 1.5 y = (cA + cN + 3 y) / 2 (sb < 0: no residual y) */
static void b_shape(const int32_t *ca, const int32_t *cn, int sb, int32_t *c) {
  const int16_t *y = fx_cb[LR_BST] + sb * FX_CBW;
  int k;
  for (k = 0; k < NC; k++)
    c[k] = fx_rshr((fx_acc)ca[k] + cn[k] + (sb >= 0 ? 3 * (int32_t)FX_CB_VAL(y, k) * fx_cb_step[LR_BST] : 0), 1);
}

const char *sotto_lr_format(void) { return LR_FORMAT; }

#define lr_params_t fx_fparams_t
#define lr_shape_a a_shape
#define lr_shape_b b_shape
#include "../sotto_lr_dec.inc"
