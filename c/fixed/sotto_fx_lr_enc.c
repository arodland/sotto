/* sotto 1200 encoder in fixed point (integer only), and stubs for the encoders the fixed
 * build still lacks.
 *
 * sotto 1200: the port of ../sotto_lr.c's encoder (Sotto1200 in sim/sotto_lr.py). The v3.7
 * fixed-point core (sotto_fx_enc.c) runs in shape-only mode and hands over each 20 ms frame's
 * fields with its shape target (fx_enc_frame_t.p); this layer codes them in pairs (A, B):
 *   - A's shape: the core's M-best search (fx_msvq_paths) over stages 1-2, all surviving paths
 *     best first; the A choice adds the errors of the B frames on both sides of it, each with
 *     its best stage-3 residual (the joint search);
 *   - B's shape: the mean of the neighbouring A reconstructions + 1.5 x one stage-3 codeword,
 *     searched exactly in codebook steps: d = rr + 192 (48 |y|^2 - r.y), with r the residual
 *     in Q6 steps (the core's d = rr + 128 (32 |y|^2 - r.y) with the 1.5 folded in);
 *   - all costs in the core's final-pick units (dB Q8 squared), weights 10 and 1 for the
 *     float's 1.0 and 0.1;
 *   - reconstructions as the fixed decoder builds them (sotto_fx_lr.c), so the encoder's
 *     model of B is the decoder's.
 * Pitch and energy steps are integer in the float too, so they match it exactly.
 * VLR (sotto 600 / 400) and the 16 kHz hiss variants still have no fixed-point encoder: their
 * create functions return NULL; use the float build (../sotto_vlr.c, ../sotto_hiss.c).
 * SPDX-License-Identifier: MIT */
#define SOTTO_BUILD
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#include "../sotto.h"
#include "../sotto_vlr.h"
#include "../sotto_lr_tables.h"
#include "fx.h"
#include "fx_internal.h"
#include "fx_tables.h"

#define NC FX_NC
#define MB FX_MBEST
#define LR_Q 8                       /* 20 ms frames held (ring) */

typedef struct {
  int16_t ca, wa, ea, sa[LR_NA], cb, dw, de, sb;
} lr_fields_t;

typedef struct {
  fx_fparams_t p;
  int16_t si[LR_NA];                 /* A's shape path (first pass, then joint) */
  int32_t rec[NC];                   /* its reconstruction, dB Q16 */
} lr_aframe_t;

typedef struct {                     /* one A frame's surviving paths, best first */
  long t;                            /* its frame number (-1: empty) */
  int np;
  int16_t path[MB][5];
  int32_t rec[MB][NC];
} lr_paths_t;

struct sotto_lr_enc {
  struct sotto_enc *core;
  lr_paths_t pc[3];                  /* the A frames' searches, each done once: A(n+1), A(n+2) and
                                        the next one (the float redoes them up to three times) */
  fx_fparams_t fr[LR_Q];             /* 20 ms frames from the core not yet coded */
  long nin, ncoded;                  /* frames received, frames coded (always even) */
  int flushed, have_prev;
  lr_aframe_t prevA;                 /* A(n-1), final, for the joint search of A(n) */
  unsigned char out[4][SOTTO_BYTES_PER_FRAME];
  int out_head, out_n;
  int32_t hv[512];                   /* scratch for the codebook search */
};

#define FR(e, t) (&(e)->fr[(t) % LR_Q])

static void put_bits(unsigned char *out, int *pos, int v, int nb) {
  int i;
  for (i = nb - 1; i >= 0; i--, (*pos)++)
    if ((v >> i) & 1) out[*pos >> 3] |= 0x80 >> (*pos & 7);
}

static void pack(const lr_fields_t *f, unsigned char *out) {
  int pos = 0, i;
  memset(out, 0, SOTTO_BYTES_PER_FRAME);
  put_bits(out, &pos, f->ca, 2);
  put_bits(out, &pos, f->wa, 7);
  put_bits(out, &pos, f->ea, 5);
  for (i = 0; i < LR_NA; i++) put_bits(out, &pos, f->sa[i], fx_cb_bits[i]);
  put_bits(out, &pos, f->cb, 2);
  put_bits(out, &pos, f->dw, LR_WB_BITS);
  put_bits(out, &pos, f->de, LR_EB_BITS);
  put_bits(out, &pos, f->sb, fx_cb_bits[LR_BST]);
}

/* every surviving A path over stages 1..LR_NA, best first (stable), with reconstructions:
   Sotto1200._a_search(all_paths=True) */
static int a_paths(const int32_t *c, int16_t path[MB][5], int32_t rec[MB][NC], int32_t *hv) {
  int16_t p0[MB][5];
  int32_t r0[MB][NC];
  fx_acc d[MB];
  int o[MB], np = fx_msvq_paths(c, LR_NA, p0, r0, hv), p, i;
  for (p = 0; p < np; p++) {
    d[p] = fx_shape_dist(c, r0[p]);
    for (i = p; i > 0 && d[o[i - 1]] > d[p]; i--) o[i] = o[i - 1];
    o[i] = p;
  }
  for (i = 0; i < np; i++) memcpy(path[i], p0[o[i]], sizeof path[0]), memcpy(rec[i], r0[o[i]], sizeof rec[0]);
  return np;
}

/* B's best stage-3 residual over base (dB Q16) for target, and its error in dB Q8 squared */
static fx_acc b_best(const int32_t *base, const int32_t *target, int16_t *sb) {
  int16_t r[NC + 1], cb_buf[FX_CBW];
  int n = 1 << fx_cb_bits[LR_BST], v, k, best = 0;
  const int32_t *e2 = fx_cb_e2[LR_BST];
  int32_t step = fx_cb_step[LR_BST];
  fx_acc hb = 0;
  {   /* fx_cb_resid with the difference clamped first: B's residual over the anchors' mean can
         be far larger than a stage-3 residual (an onset), and its product with istep would pass
         40 bits; r saturates at +-32767 steps anyway, so nothing that fits changes */
    int32_t dc[NC], z[NC];
    int32_t lim = (int32_t)(((fx_acc)32767 << 24) / fx_cb_istep[LR_BST]);
    for (k = 0; k < NC; k++) {
      fx_acc dd = (fx_acc)target[k] - base[k];
      dc[k] = (int32_t)(dd > lim ? lim : dd < -lim ? -lim : dd);
      z[k] = 0;
    }
    fx_cb_resid(dc, z, r, NC, fx_cb_istep[LR_BST]);
  }
  for (v = 0; v < n; v++) {
    const int16_t *y = FX_CB_VEC(LR_BST, v, cb_buf);
    fx_acc ry = 0, h;
    for (k = 0; k < NC; k++) ry += FX_P16(r[k], (int16_t)FX_CB_VAL(y, k));
    h = (fx_acc)e2[v] * 48 - ry;                       /* d = rr + 192 h */
    if (v == 0 || h < hb) hb = h, best = v;
  }
  *sb = (int16_t)best;
  {   /* the chosen codeword's error, as the decoder reconstructs B: (2 base + 3 y step) / 2 */
    const int16_t *y = FX_CB_VEC(LR_BST, best, cb_buf);
    fx_acc d = 0;
    for (k = 0; k < NC; k++) {
      int32_t q = (int32_t)fx_rshr((fx_acc)target[k] * 2 - ((fx_acc)base[k] * 2 + 3 * (fx_acc)FX_CB_VAL(y, k) * step), 9);
      d += (fx_acc)q * q;
    }
    return d;
  }
}

static fx_acc wgt(int ei) { return ei > 0 ? 10 : 1; }   /* the float's 1.0 and 0.1, x 10 */

/* the decoder's midpoint of two A reconstructions (sotto_fx_lr.c b_shape without residual) */
static void mid(const int32_t *a, const int32_t *b, int32_t *m) {
  int k;
  for (k = 0; k < NC; k++) m[k] = (int32_t)fx_rshr((fx_acc)a[k] + b[k], 1);
}

/* frame t's A search, from the cache or done now */
static const lr_paths_t *paths_of(struct sotto_lr_enc *e, long t, const fx_fparams_t *p) {
  lr_paths_t *c = &e->pc[(t >> 1) % 3];
  if (c->t != t) c->t = t, c->np = a_paths(p->c, c->path, c->rec, e->hv);
  return c;
}

static void a_first(struct sotto_lr_enc *e, long t, lr_aframe_t *a) {
  const lr_paths_t *c = paths_of(e, t, FR(e, t));
  a->p = *FR(e, t);
  memcpy(a->si, c->path[0], sizeof a->si);
  memcpy(a->rec, c->rec[0], sizeof a->rec);
}

/* Sotto1200._joint for anchor s: prev = A(s-1) final (or NULL), b0 = B(s-1), b1 = B(s),
   next = A(s+1) first pass (or NULL at the end) */
static void a_joint(struct sotto_lr_enc *e, long t, lr_aframe_t *a, const lr_aframe_t *prev, const fx_fparams_t *b0,
                    const fx_fparams_t *b1, const lr_aframe_t *next) {
  int16_t sb;
  int32_t base[NC];
  const lr_paths_t *c = paths_of(e, t, &a->p);
  const int16_t (*path)[5] = c->path;
  const int32_t (*rec)[NC] = c->rec;
  int np = c->np, i, best = 0;
  fx_acc bd = 0;
  for (i = 0; i < np; i++) {
    fx_acc t = wgt(a->p.ei) * fx_shape_dist(rec[i], a->p.c);
    if (prev) {
      mid(prev->rec, rec[i], base);
      t += wgt(b0->ei) * b_best(base, b0->c, &sb);     /* LR_JOINT is 1.0 (tools/check_fx_lr_enc.py checks) */
    }
    if (b1) {
      mid(rec[i], next ? next->rec : rec[i], base);
      t += wgt(b1->ei) * b_best(base, b1->c, &sb);
    }
    if (i == 0 || t < bd) bd = t, best = i;
  }
  memcpy(a->si, path[best], sizeof a->si);
  memcpy(a->rec, rec[best], sizeof a->rec);
}

static int wo_interp(int wa, int va, int wb, int vb) {
  if (va && vb) return (wa + wb + 1) >> 1;
  if (va) return wa;
  if (vb) return wb;
  return -1;
}

static int iabs(int v) { return v < 0 ? -v : v; }   /* no libc call: the bare-metal builds' headers lack abs */

/* index of the table entry nearest x (the first of equals), as np.argmin */
static int q_near(const signed char *tab_s, const unsigned char *tab_u, int n, int x) {
  int i, b = 0;
  for (i = 1; i < n; i++) {
    int ti = tab_s ? tab_s[i] : tab_u[i], tb = tab_s ? tab_s[b] : tab_u[b];
    if (iabs(ti - x) < iabs(tb - x)) b = i;
  }
  return b;
}

/* superframe n = frames t, t+1 (A, B) with A(n+1) = frame t+2 (final) */
static void code_superframe(struct sotto_lr_enc *e, const lr_aframe_t *a, const fx_fparams_t *b,
                            const lr_aframe_t *nx) {
  lr_fields_t f;
  int32_t base[NC];
  int wint;
  f.ca = a->p.code, f.wa = a->p.wi, f.ea = a->p.ei;
  memcpy(f.sa, a->si, sizeof f.sa);
  f.cb = b->code;
  f.de = (int16_t)q_near(lr_e_tab, NULL, 1 << LR_EB_BITS, b->ei - ((a->p.ei + nx->p.ei + 1) >> 1));
  wint = wo_interp(a->p.wi, a->p.code != 0, nx->p.wi, nx->p.code != 0);
  if (b->code == 0) f.dw = LR_W_ZERO;
  else if (wint < 0) f.dw = (int16_t)q_near(NULL, lr_w_abs, 1 << LR_WB_BITS, b->wi);
  else f.dw = (int16_t)q_near(lr_w_tab, NULL, 1 << LR_WB_BITS, b->wi - wint);
  mid(a->rec, nx->rec, base);
  b_best(base, b->c, &f.sb);
  pack(&f, e->out[(e->out_head + e->out_n++) % 4]);
}

struct sotto_lr_enc *sotto_lr_enc_create(void) {
  struct sotto_lr_enc *e = calloc(1, sizeof *e);
  if (!e) return NULL;
  e->core = sotto_enc_create();
  if (!e->core) {
    free(e);
    return NULL;
  }
  sotto_enc_set_shape_only(e->core, 1);
  e->pc[0].t = e->pc[1].t = e->pc[2].t = -1;
  return e;
}

void sotto_lr_enc_destroy(struct sotto_lr_enc *e) {
  if (!e) return;
  sotto_enc_destroy(e->core);
  free(e);
}

/* code every superframe whose look-ahead is in: superframe n needs A(n+1) final, which
   needs frames up to A(n+2) (its joint search looks at A(n+2)'s first pass) */
#ifndef LR_AHEAD
#define LR_AHEAD 1   /* 0: A(n+1) is chosen without B(n+1) / A(n+2), 40 ms less delay */
#endif
static void drain(struct sotto_lr_enc *e) {
  for (;;) {
    long t = e->ncoded;
    if (!e->flushed && e->nin < t + (LR_AHEAD ? 5 : 3)) return;   /* A(n), B(n), A(n+1)[, B(n+1), A(n+2)] */
    if (e->nin == t + 1) *FR(e, t + 1) = *FR(e, t), e->nin = t + 2;   /* odd count: B = A */
    if (e->nin < t + 2) return;
    {
      int has1 = e->nin >= t + (LR_AHEAD ? 4 : 3), has2 = LR_AHEAD && e->nin >= t + 5;
      const fx_fparams_t *b = FR(e, t + 1);
      lr_aframe_t a, n1, n2;
      if (has1) a_first(e, t + 2, &n1);
      if (e->have_prev) a = e->prevA;
      else {                                             /* the first superframe */
        a_first(e, t, &a);
        a_joint(e, t, &a, NULL, NULL, b, has1 ? &n1 : NULL);
      }
      if (has1) {
        if (has2) a_first(e, t + 4, &n2);
        a_joint(e, t + 2, &n1, &a, b, LR_AHEAD ? FR(e, t + 3) : NULL, has2 ? &n2 : NULL);
        code_superframe(e, &a, b, &n1);
        e->prevA = n1;
        e->have_prev = 1;
      } else {
        code_superframe(e, &a, b, &a);                   /* the last superframe: next = itself */
      }
    }
    e->ncoded = t + 2;
  }
}

static void pull(struct sotto_lr_enc *e) {
  fx_enc_frame_t f;
  while (sotto_enc_pop_frame(e->core, &f)) *FR(e, e->nin++) = f.p;
}

static int pop(struct sotto_lr_enc *e, unsigned char out[SOTTO_BYTES_PER_FRAME]) {
  if (!e->out_n) return 0;
  memcpy(out, e->out[e->out_head], SOTTO_BYTES_PER_FRAME);
  e->out_head = (e->out_head + 1) % 4;
  e->out_n--;
  return 1;
}

int sotto_lr_encode(struct sotto_lr_enc *e, const short speech[SOTTO_LR_SAMPLES_PER_FRAME],
                    unsigned char out[SOTTO_BYTES_PER_FRAME]) {
  int i;
  for (i = 0; i < 4; i++) sotto_enc_analyse_block(e->core, speech + 80 * i);
  pull(e);
  drain(e);
  return pop(e, out);
}

void sotto_lr_encode_part(struct sotto_lr_enc *e, const short *speech, int n80) {
  int i;
  for (i = 0; i < n80; i++) sotto_enc_analyse_block(e->core, speech + 80 * i);
  pull(e);
  drain(e);
}

/* the next coded superframe, after sotto_lr_encode_part (as the float build's) */
int sotto_lr_enc_pop(struct sotto_lr_enc *e, unsigned char out[SOTTO_BYTES_PER_FRAME]) { return pop(e, out); }

int sotto_lr_enc_flush(struct sotto_lr_enc *e, unsigned char out[SOTTO_BYTES_PER_FRAME]) {
  if (!e->flushed) {
    sotto_enc_flush_core(e->core);
    pull(e);
    e->flushed = 1;
  }
  drain(e);
  return pop(e, out);
}

/* no fixed-point sotto 600 / 400 encoder */
struct sotto_vlr_enc *sotto_vlr_enc_create(int rate) { (void)rate; return NULL; }
void sotto_vlr_enc_destroy(struct sotto_vlr_enc *e) { (void)e; }
void sotto_vlr_encode(struct sotto_vlr_enc *e, const short *speech, int n80) { (void)e, (void)speech, (void)n80; }
int sotto_vlr_enc_pop(struct sotto_vlr_enc *e, unsigned char out[SOTTO_BYTES_PER_FRAME]) { (void)e, (void)out; return 0; }
int sotto_vlr_enc_flush(struct sotto_vlr_enc *e, unsigned char out[SOTTO_BYTES_PER_FRAME]) { (void)e, (void)out; return 0; }

#ifdef SOTTO_V38
/* no fixed-point sotto 1200 hiss */
struct sotto_lr_enc16 *sotto_lr_enc16_create(void) { return NULL; }
void sotto_lr_enc16_destroy(struct sotto_lr_enc16 *e) { (void)e; }
int sotto_lr_encode16(struct sotto_lr_enc16 *e, const short speech[SOTTO_LR_SAMPLES_PER_FRAME_16K],
                      unsigned char out[SOTTO_BYTES_PER_FRAME]) { (void)e, (void)speech, (void)out; return 0; }
int sotto_lr_enc16_flush(struct sotto_lr_enc16 *e, unsigned char out[SOTTO_BYTES_PER_FRAME]) { (void)e, (void)out; return 0; }
#ifndef FX_HISS_FB                  /* with it, sotto_fx_hiss.c has the K6 hiss decoders */
struct sotto_lr_dec16 *sotto_lr_dec16_create(void) { return NULL; }
void sotto_lr_dec16_destroy(struct sotto_lr_dec16 *d) { (void)d; }
void sotto_lr_decode16(struct sotto_lr_dec16 *d, const unsigned char in[SOTTO_BYTES_PER_FRAME], int good,
                       short speech[SOTTO_LR_SAMPLES_PER_FRAME_16K]) { (void)d, (void)in, (void)good, (void)speech; }
int sotto_lr_dec16_flush(struct sotto_lr_dec16 *d, short speech[SOTTO_LR_SAMPLES_PER_FRAME_16K]) { (void)d, (void)speech; return 0; }
#endif

/* no fixed-point sotto 600 / 400 hiss */
struct sotto_vlr_enc16 *sotto_vlr_enc16_create(int rate) { (void)rate; return NULL; }
void sotto_vlr_enc16_destroy(struct sotto_vlr_enc16 *e) { (void)e; }
void sotto_vlr_encode16(struct sotto_vlr_enc16 *e, const short *speech, int n160) { (void)e, (void)speech, (void)n160; }
int sotto_vlr_enc16_pop(struct sotto_vlr_enc16 *e, unsigned char out[SOTTO_BYTES_PER_FRAME]) { (void)e, (void)out; return 0; }
int sotto_vlr_enc16_flush(struct sotto_vlr_enc16 *e, unsigned char out[SOTTO_BYTES_PER_FRAME]) { (void)e, (void)out; return 0; }
#ifndef FX_HISS_FB
struct sotto_vlr_dec16 *sotto_vlr_dec16_create(int rate) { (void)rate; return NULL; }
void sotto_vlr_dec16_destroy(struct sotto_vlr_dec16 *d) { (void)d; }
int sotto_vlr_samples16(const struct sotto_vlr_dec16 *d) { (void)d; return 0; }
void sotto_vlr_decode16(struct sotto_vlr_dec16 *d, const unsigned char in[SOTTO_BYTES_PER_FRAME], int good,
                        short *speech) { (void)d, (void)in, (void)good, (void)speech; }
int sotto_vlr_dec16_flush(struct sotto_vlr_dec16 *d, short *speech) { (void)d, (void)speech; return 0; }
#endif
#endif
