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

struct sotto_lr_dec {
  struct sotto_dec *core;
  lr_fields_t pend, last;    /* superframe n-1 (its B waits for A(n)); the last good one */
  int16_t have_pend, have_good, run_b;
};

static int get_bits(const unsigned char *in, int *pos, int nb) {
  int i, v = 0;
  for (i = 0; i < nb; i++, (*pos)++) v = (v << 1) | ((in[*pos >> 3] >> (7 - (*pos & 7))) & 1);
  return v;
}

static void unpack(const unsigned char *b, lr_fields_t *f) {
  int i, pos = 0;
  f->ca = (int16_t)get_bits(b, &pos, 2);
  f->wa = (int16_t)get_bits(b, &pos, 7);
  f->ea = (int16_t)get_bits(b, &pos, 5);
  for (i = 0; i < LR_NA; i++) f->sa[i] = (int16_t)get_bits(b, &pos, fx_cb_bits[i]);
  f->cb = (int16_t)get_bits(b, &pos, 2);
  f->dw = (int16_t)get_bits(b, &pos, LR_WB_BITS);
  f->de = (int16_t)get_bits(b, &pos, LR_EB_BITS);
  f->sb = (int16_t)get_bits(b, &pos, fx_cb_bits[LR_BST]);
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

static int wo_b(const lr_fields_t *a, const lr_fields_t *n) {
  int wint = wo_interp(a->wa, a->ca != 0, n->wa, n->ca != 0);
  if (wint < 0) return a->cb ? lr_w_abs[a->dw] : a->wa;
  if (a->cb == 0) return wint;
  int w = wint + lr_w_tab[a->dw];
  return w < 0 ? 0 : w > 127 ? 127 : w;
}

static void push_a(struct sotto_lr_dec *d, const lr_fields_t *a, int run, short *out) {
  fx_fparams_t p;
  p.code = a->ca, p.wi = a->wa, p.ei = a->ea;
  a_shape(a->sa, p.c);
  sotto_dec_frame(d->core, &p, run, out);
}

static void push_b(struct sotto_lr_dec *d, const lr_fields_t *a, const lr_fields_t *n, int run, short *out) {
  fx_fparams_t p;
  int32_t cn[NC];
  int k;
  const int16_t *y = fx_cb[LR_BST] + a->sb * FX_CBW;
  a_shape(a->sa, p.c);
  a_shape(n->sa, cn);
  /* (cA + cN) / 2 + 1.5 y = (cA + cN + 3 y) / 2 */
  for (k = 0; k < NC; k++) p.c[k] = fx_rshr((fx_acc)p.c[k] + cn[k] + 3 * (int32_t)FX_CB_VAL(y, k) * fx_cb_step[LR_BST], 1);
  p.code = a->cb;
  p.wi = (int16_t)wo_b(a, n);
  p.ei = (int16_t)e_b(a->ea, n->ea, a->de);
  sotto_dec_frame(d->core, &p, run, out);
}

const char *sotto_lr_format(void) { return LR_FORMAT; }

struct sotto_lr_dec *sotto_lr_dec_create(void) {
  struct sotto_lr_dec *d = calloc(1, sizeof *d);
  if (!d) return NULL;
  d->core = sotto_dec_create();
  if (!d->core) {
    free(d);
    return NULL;
  }
  return d;
}

void sotto_lr_dec_destroy(struct sotto_lr_dec *d) {
  if (!d) return;
  sotto_dec_destroy(d->core);
  free(d);
}


void sotto_lr_decode(struct sotto_lr_dec *d, const unsigned char in[SOTTO_BYTES_PER_FRAME], int good,
                     short speech[SOTTO_LR_SAMPLES_PER_FRAME]) {
  lr_fields_t f;
  int ra, rb;
  if (good) {
    unpack(in, &f);
    d->last = f;
    d->have_good = 1;
    ra = rb = 0;
  } else {
    if (d->have_good) f = d->last;
    else memset(&f, 0, sizeof f), f.dw = LR_W_ZERO, f.de = LR_E_ZERO;
    ra = d->run_b + 1, rb = d->run_b + 2;
  }
  if (d->have_pend) push_b(d, &d->pend, good ? &f : &d->pend, d->run_b, speech);
  else memset(speech, 0, SOTTO_SAMPLES_PER_FRAME * sizeof(short));
  push_a(d, &f, ra, speech + SOTTO_SAMPLES_PER_FRAME);
  d->pend = f;
  d->have_pend = 1;
  d->run_b = (int16_t)rb;
}

void sotto_lr_dec_flush(struct sotto_lr_dec *d, short speech[SOTTO_LR_SAMPLES_PER_FRAME]) {
  if (!d->have_pend) {
    memset(speech, 0, SOTTO_LR_SAMPLES_PER_FRAME * sizeof(short));
    return;
  }
  push_b(d, &d->pend, &d->pend, d->run_b, speech);
  sotto_dec_flush(d->core, speech + SOTTO_SAMPLES_PER_FRAME);
  d->have_pend = 0;
}

