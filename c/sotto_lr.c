/* sotto 1200: 48 bits per 40 ms superframe on the v3.7 core, floating point.
 * C port of sim/sotto_lr.py (Sotto1200); see sotto.h for the API and the frame.
 *
 * Encoder: the v3.7 encoder core (pitch tracker, voicing classes, energy, shape target) runs
 * unchanged; this layer takes its 20 ms frames in pairs (A, B) and codes them as one
 * superframe. A's shape is searched over the first LR_NA stages of the v3.7 codebook, B's
 * shape is the mean of this superframe's and the next one's A shapes plus one codeword of
 * stage LR_BST scaled by LR_BG, and the A choice counts the B frames on both sides of it
 * (Sotto1200._joint). B's pitch and energy are small steps from the interpolated indices.
 * Decoder: rebuilds the 20 ms frames' fields and runs the v3.7 decoder core on them.
 * SPDX-License-Identifier: MIT */
#define SOTTO_BUILD
#include <math.h>
#include <stdlib.h>
#include <string.h>

#include "sotto.h"
#include "sotto_codebook.h"
#include "sotto_internal.h"
#include "sotto_lr_tables.h"

#define NC (SOTTO_K - 1)
#define MBEST 8
#define NSF 320              /* samples per superframe */

/* ------------------------------------------------------------------ shared */
typedef struct {             /* the fields of one superframe */
  int ca, wa, ea, sa[LR_NA], cb, dw, de, sb;
} lr_fields_t;

static void put_bits(unsigned char *out, int *pos, int v, int nb) {
  for (int i = nb - 1; i >= 0; i--, (*pos)++)
    if ((v >> i) & 1) out[*pos >> 3] |= 0x80 >> (*pos & 7);
}

static int get_bits(const unsigned char *in, int *pos, int nb) {
  int v = 0;
  for (int i = 0; i < nb; i++, (*pos)++) v = (v << 1) | ((in[*pos >> 3] >> (7 - (*pos & 7))) & 1);
  return v;
}

static void pack(const lr_fields_t *f, unsigned char *out) {
  int pos = 0;
  memset(out, 0, SOTTO_BYTES_PER_FRAME);
  put_bits(out, &pos, f->ca, 2);
  put_bits(out, &pos, f->wa, 7);
  put_bits(out, &pos, f->ea, 5);
  for (int i = 0; i < LR_NA; i++) put_bits(out, &pos, f->sa[i], sotto_cb_bits[i]);
  put_bits(out, &pos, f->cb, 2);
  put_bits(out, &pos, f->dw, LR_WB_BITS);
  put_bits(out, &pos, f->de, LR_EB_BITS);
  put_bits(out, &pos, f->sb, sotto_cb_bits[LR_BST]);
}

static void unpack(const unsigned char *b, lr_fields_t *f) {
  int pos = 0;
  f->ca = get_bits(b, &pos, 2);
  f->wa = get_bits(b, &pos, 7);
  f->ea = get_bits(b, &pos, 5);
  for (int i = 0; i < LR_NA; i++) f->sa[i] = get_bits(b, &pos, sotto_cb_bits[i]);
  f->cb = get_bits(b, &pos, 2);
  f->dw = get_bits(b, &pos, LR_WB_BITS);
  f->de = get_bits(b, &pos, LR_EB_BITS);
  f->sb = get_bits(b, &pos, sotto_cb_bits[LR_BST]);
}

static void a_shape(const int *sa, double *c) {
  for (int k = 0; k < NC; k++) c[k] = 0;
  for (int s = 0; s < LR_NA; s++) {
    const float *y = sotto_cb[s] + sa[s] * NC;
    for (int k = 0; k < NC; k++) c[k] += y[k];
  }
}

/* Sotto1200.wo_interp: -1 when neither anchor is voiced */
static int wo_interp(int wa, int va, int wb, int vb) {
  if (va && vb) return (wa + wb + 1) >> 1;
  if (va) return wa;
  if (vb) return wb;
  return -1;
}

/* Sotto1200.e_b */
static int e_b(int ea, int en, int de) {
  if (ea == 0 && en == 0 && lr_e_tab[de] <= 0) return 0;
  int e = ((ea + en + 1) >> 1) + lr_e_tab[de];
  return e < 0 ? 0 : e > 31 ? 31 : e;
}

/* B's shape: halfway between the anchors plus the scaled residual codeword (sb < 0: none) */
static void b_shape(const double *ca, const double *cn, int sb, double *c) {
  for (int k = 0; k < NC; k++) c[k] = (4 * ca[k] + 4 * cn[k]) / 8 + (sb >= 0 ? LR_BG * sotto_cb[LR_BST][sb * NC + k] : 0);
}

const char *sotto_lr_format(void) { return LR_FORMAT; }

/* ------------------------------------------------------------------ encoder */
#define LR_Q 4               /* 20 ms frames held: A(n), B(n), A(n+1), B(n+1) .. A(n+2) */

typedef struct {
  sotto_fparams_t p;
  int si[LR_NA];             /* A's shape path (first pass, then joint) */
  double rec[NC];            /* its reconstruction */
} lr_aframe_t;

struct sotto_lr_enc {
  struct sotto_enc *core;
  sotto_fparams_t fr[8];     /* 20 ms frames from the core not yet coded (ring) */
  long nin, ncoded;          /* frames received, frames coded (always even) */
  int flushed, have_prev;
  lr_aframe_t prevA;         /* A(n-1), final, for the joint search of A(n) */
  unsigned char out[4][SOTTO_BYTES_PER_FRAME];
  int out_head, out_n;
};

#define FR(e, t) (&(e)->fr[(t) % 8])

/* M-best over the A stages: every surviving path, best first, with reconstructions */
static int a_paths(const double *c, int path[MBEST][LR_NA], double rec[MBEST][NC]) {
  int np = 1;
  memset(rec, 0, sizeof(double) * MBEST * NC);
  for (int s = 0; s < LR_NA; s++) {
    int n = 1 << sotto_cb_bits[s], nk = 0;
    const float *cb = sotto_cb[s];
    double kd[MBEST];
    int kp[MBEST], kc[MBEST];
    for (int p = 0; p < np; p++) {
      double res[NC];
      for (int k = 0; k < NC; k++) res[k] = c[k] - rec[p][k];
      for (int v = 0; v < n; v++) {
        const float *y = cb + v * NC;
        double d = 0;
        for (int k = 0; k < NC; k++) d += (res[k] - y[k]) * (res[k] - y[k]);
        if (nk < MBEST || d < kd[nk - 1]) {
          int i = nk < MBEST ? nk++ : MBEST - 1;
          while (i > 0 && kd[i - 1] > d) kd[i] = kd[i - 1], kp[i] = kp[i - 1], kc[i] = kc[i - 1], i--;
          kd[i] = d, kp[i] = p, kc[i] = v;
        }
      }
    }
    int npath[MBEST][LR_NA];
    double nrec[MBEST][NC];
    for (int i = 0; i < nk; i++) {
      memcpy(npath[i], path[kp[i]], sizeof npath[0]);
      npath[i][s] = kc[i];
      for (int k = 0; k < NC; k++) nrec[i][k] = rec[kp[i]][k] + cb[kc[i] * NC + k];
    }
    memcpy(path, npath, sizeof npath);
    memcpy(rec, nrec, sizeof nrec);
    np = nk;
  }
  /* best first (stable), as Sotto1200._a_search(all_paths=True) */
  double d[MBEST];
  int o[MBEST];
  for (int p = 0; p < np; p++) {
    d[p] = 0;
    for (int k = 0; k < NC; k++) d[p] += (c[k] - rec[p][k]) * (c[k] - rec[p][k]);
    int i = p;
    while (i > 0 && d[o[i - 1]] > d[p]) o[i] = o[i - 1], i--;
    o[i] = p;
  }
  int tp[MBEST][LR_NA];
  double tr[MBEST][NC];
  for (int i = 0; i < np; i++) memcpy(tp[i], path[o[i]], sizeof tp[0]), memcpy(tr[i], rec[o[i]], sizeof tr[0]);
  memcpy(path, tp, sizeof tp);
  memcpy(rec, tr, sizeof tr);
  return np;
}

/* best residual codeword for B over base, and its squared error */
static double b_best(const double *base, const double *target, int *sb) {
  int n = 1 << sotto_cb_bits[LR_BST];
  const float *cb = sotto_cb[LR_BST];
  double r[NC], best = 0;
  for (int k = 0; k < NC; k++) r[k] = target[k] - base[k];
  for (int v = 0; v < n; v++) {
    double d = 0;
    for (int k = 0; k < NC; k++) {
      double x = r[k] - LR_BG * cb[v * NC + k];
      d += x * x;
    }
    if (v == 0 || d < best) best = d, *sb = v;
  }
  return best;
}

static double wgt(int ei) { return ei > 0 ? 1.0 : 0.1; }

static void a_first(const sotto_fparams_t *p, lr_aframe_t *a) {
  int path[MBEST][LR_NA];
  double rec[MBEST][NC];
  a->p = *p;
  a_paths(p->c, path, rec);
  memcpy(a->si, path[0], sizeof a->si);
  memcpy(a->rec, rec[0], sizeof a->rec);
}

/* Sotto1200._joint for anchor s: prev = A(s-1) final (or NULL), b0 = B(s-1), b1 = B(s),
   next = A(s+1) first pass (or NULL at the end) */
static void a_joint(lr_aframe_t *a, const lr_aframe_t *prev, const sotto_fparams_t *b0,
                    const sotto_fparams_t *b1, const lr_aframe_t *next) {
  int path[MBEST][LR_NA], sb, best = 0;
  double rec[MBEST][NC], bd = 0;
  int np = a_paths(a->p.c, path, rec);
  for (int i = 0; i < np; i++) {
    double e = 0, base[NC];
    for (int k = 0; k < NC; k++) e += (rec[i][k] - a->p.c[k]) * (rec[i][k] - a->p.c[k]);
    e *= wgt(a->p.ei);
    if (prev) {
      for (int k = 0; k < NC; k++) base[k] = 0.5 * (prev->rec[k] + rec[i][k]);
      e += LR_JOINT * wgt(b0->ei) * b_best(base, b0->c, &sb);
    }
    const double *nx = next ? next->rec : rec[i];
    for (int k = 0; k < NC; k++) base[k] = 0.5 * (rec[i][k] + nx[k]);
    e += LR_JOINT * wgt(b1->ei) * b_best(base, b1->c, &sb);
    if (i == 0 || e < bd) bd = e, best = i;
  }
  memcpy(a->si, path[best], sizeof a->si);
  memcpy(a->rec, rec[best], sizeof a->rec);
}

/* index of the table entry nearest x (the first of equals), as np.argmin */
#define Q_NEAR(tab, n, x, out) do { \
    int b_ = 0; \
    for (int i_ = 1; i_ < (n); i_++) \
      if (abs((int)(tab)[i_] - (x)) < abs((int)(tab)[b_] - (x))) b_ = i_; \
    (out) = b_; \
  } while (0)

/* superframe n = frames t, t+1 (A, B) with A(n+1) = frame t+2 (final) */
static void code_superframe(struct sotto_lr_enc *e, const lr_aframe_t *a, const sotto_fparams_t *b,
                            const lr_aframe_t *nx) {
  lr_fields_t f;
  f.ca = a->p.code, f.wa = a->p.wi, f.ea = a->p.ei;
  memcpy(f.sa, a->si, sizeof f.sa);
  f.cb = b->code;
  Q_NEAR(lr_e_tab, 1 << LR_EB_BITS, b->ei - ((a->p.ei + nx->p.ei + 1) >> 1), f.de);
  int wint = wo_interp(a->p.wi, a->p.code != 0, nx->p.wi, nx->p.code != 0);
  if (b->code == 0) f.dw = LR_W_ZERO;
  else if (wint < 0) Q_NEAR(lr_w_abs, 1 << LR_WB_BITS, b->wi, f.dw);
  else Q_NEAR(lr_w_tab, 1 << LR_WB_BITS, b->wi - wint, f.dw);
  double base[NC];
  for (int k = 0; k < NC; k++) base[k] = (4 * a->rec[k] + 4 * nx->rec[k]) / 8;
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
  return e;
}

void sotto_lr_enc_destroy(struct sotto_lr_enc *e) {
  if (!e) return;
  sotto_enc_destroy(e->core);
  free(e);
}

/* code every superframe whose look-ahead is in: superframe n needs A(n+1) final, which
   needs frames up to A(n+2) (its joint search looks at A(n+2)'s first pass) */
static void drain(struct sotto_lr_enc *e) {
  for (;;) {
    long t = e->ncoded;
    if (!e->flushed && e->nin < t + 5) return;  /* A(n), B(n), A(n+1), B(n+1), A(n+2) */
    if (e->nin == t + 1) *FR(e, t + 1) = *FR(e, t), e->nin = t + 2;   /* odd count: B = A */
    if (e->nin < t + 2) return;
    int has1 = e->nin >= t + 4, has2 = e->nin >= t + 5;
    const sotto_fparams_t *b = FR(e, t + 1);
    lr_aframe_t a, n1, n2;
    if (has1) a_first(FR(e, t + 2), &n1);
    if (e->have_prev) a = e->prevA;
    else {                                      /* the first superframe */
      a_first(FR(e, t), &a);
      a_joint(&a, NULL, NULL, b, has1 ? &n1 : NULL);
    }
    if (has1) {
      if (has2) a_first(FR(e, t + 4), &n2);
      a_joint(&n1, &a, b, FR(e, t + 3), has2 ? &n2 : NULL);
      code_superframe(e, &a, b, &n1);
      e->prevA = n1;
      e->have_prev = 1;
    } else {
      code_superframe(e, &a, b, &a);            /* the last superframe: next = itself */
    }
    e->ncoded = t + 2;
  }
}

static void pull(struct sotto_lr_enc *e) {
  sotto_enc_frame_t f;
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
  for (int i = 0; i < 4; i++) sotto_enc_analyse_block(e->core, speech + 80 * i);
  pull(e);
  drain(e);
  return pop(e, out);
}

void sotto_lr_encode_part(struct sotto_lr_enc *e, const short *speech, int n80) {
  for (int i = 0; i < n80; i++) sotto_enc_analyse_block(e->core, speech + 80 * i);
  pull(e);
  drain(e);
}

int sotto_lr_enc_flush(struct sotto_lr_enc *e, unsigned char out[SOTTO_BYTES_PER_FRAME]) {
  if (!e->flushed) {
    sotto_enc_flush_core(e->core);
    pull(e);
    e->flushed = 1;
  }
  drain(e);
  return pop(e, out);
}

#ifdef SOTTO_V38
/* sotto 1200 hiss (sotto_lr_enc16 in sotto_hiss.c): the same coder without its core, fed
   the 20 ms frames after v3.8's voicing rules */
struct sotto_lr_enc *sotto_lr_enc_create_frames(void) { return calloc(1, sizeof(struct sotto_lr_enc)); }

void sotto_lr_enc_push(struct sotto_lr_enc *e, const sotto_fparams_t *p) {
  *FR(e, e->nin++) = *p;
  drain(e);
}

void sotto_lr_enc_end(struct sotto_lr_enc *e) {
  e->flushed = 1;
  drain(e);
}

int sotto_lr_enc_pop(struct sotto_lr_enc *e, unsigned char out[SOTTO_BYTES_PER_FRAME]) { return pop(e, out); }
#endif

/* ------------------------------------------------------------------ decoder */
#ifdef SOTTO_V38
#define LR_HISS                      /* sotto 1200 hiss: sotto_lr_dec16 in sotto_hiss.c hooks in */
#include "sotto_hiss_tables.h"
#define LR_HISS_T double
#define LR_HISS_HOOK_T lr_hiss_hook_t
#define LR_HISS_CB6(i) sotto_hiss_cb6[i]
#define LR_HISS_BCB(i) sotto_lr_hiss_bcb[i]
#define LR_HISS_HALF(x) ((x) / 2)
#endif
#define lr_params_t sotto_fparams_t
#define lr_shape_a a_shape
#define lr_shape_b b_shape
#include "sotto_lr_dec.inc"

#ifdef LR_HISS
void sotto_lr_dec_set_hook(struct sotto_lr_dec *d, lr_hiss_hook_t hook, void *ctx) {
  d->hook = hook, d->hctx = ctx;
}

const struct sotto_dec *sotto_lr_dec_core(const struct sotto_lr_dec *d) { return d->core; }

/* the decoder's A(s) and B(s) from superframe s and the next one (NULL: s is the last) */
void sotto_lr_frames(const unsigned char *sf, const unsigned char *next, sotto_fparams_t *a, sotto_fparams_t *b) {
  lr_fields_t f, fn;
  sotto_fparams_t an;
  unpack(sf, &f);
  a_params(&f, a);
  if (next) unpack(next, &fn), a_params(&fn, &an);
  else an = *a;
  b_frame(a, &an, &f, b);
}

/* the hiss fields: A's pitch field (wa >= 0) and B's pitch step (dw >= 0) */
void sotto_lr_set_hiss(unsigned char *sf, int wa, int dw) {
  lr_fields_t f;
  unpack(sf, &f);
  if (wa >= 0) f.wa = wa;
  if (dw >= 0) f.dw = dw;
  pack(&f, sf);
}
#endif
