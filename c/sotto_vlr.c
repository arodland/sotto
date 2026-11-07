/* Very-low-rate sotto encoder (sotto 600, sotto 400), floating point: the C port of
 * SottoVLR.encode_params (sim/sotto_vlr.py). See sotto_vlr.h and docs/sotto-vlr.md.
 *
 * The v3.7 encoder core (pitch tracker, voicing classes, energy, shape target) runs unchanged;
 * this layer codes its 20 ms frames N at a time. Each superframe's voicing pattern, pitch line
 * and gain-shape energy depend on its own frames only. The shape anchors are chosen by a
 * Viterbi search over each anchor's M-best codebook paths that counts the interpolated frames
 * between anchors (SottoVLR.q_shapes). The Python reference runs it over the whole utterance;
 * here an anchor is final as soon as every surviving path agrees on it, which gives the same
 * choice with a delay of a few anchors. (If they haven't agreed after VLR_FORCE anchors, the
 * best path so far decides and the paths that disagree are dropped; that hasn't been seen on
 * speech.) A superframe is written once its anchors are final.
 * SPDX-License-Identifier: MIT */
#define SOTTO_BUILD
#include <math.h>
#include <stdlib.h>
#include <string.h>

#include "sotto.h"
#include "sotto_internal.h"
#include "sotto_vlr.h"
#include "sotto_vlr_tables.h"

#define NC VLR_NC
#define MB 16                 /* SottoVLR.M_BEST */
#define QA 256                /* anchors held */
#define QF 1024               /* 20 ms frames held */
#define QO 256                /* superframes waiting to be popped */
#define VLR_FORCE 96          /* undecided anchors before a forced decision */

typedef struct {
  int f, j, n;                /* frame, slot, candidates */
  double rec[MB][NC];
  short path[MB][VLR_MAX_STAGES];
  unsigned char back[MB];     /* best predecessor (candidate of anchor k - 1) */
} vlr_anchor_t;

struct sotto_vlr_enc {
  struct sotto_enc *core;
  const vlr_mode_t *m;
  double cb[VLR_MAX_ANCHORS][VLR_MAX_STAGES][512 * NC];   /* the int8 codebooks in dB */
  sotto_fparams_t fr[QF];
  long nf;                    /* frames in */
  vlr_anchor_t an[QA];
  long nan, nfinal;           /* anchors added, anchors decided */
  int choice[QA];
  double cost[MB];
  int ncost;
  long ns_out;                /* superframes coded */
  int prev_wc, done;
  unsigned char out[QO][SOTTO_BYTES_PER_FRAME];
  long nout, npop;
};

#define FR(e, i) (&(e)->fr[(i) % QF])
#define AN(e, k) (&(e)->an[(k) % QA])

/* sotto_vlr.V_COST: a frame's voicing code as two 10 ms halves (U, V, M), edge as (M, U) */
static double v_cost(int t, int c) {
  static const int h[4][2] = {{0, 0}, {1, 1}, {2, 2}, {2, 0}};
  static const double hc[3][3] = {{0, 1.0, 0.6}, {1.0, 0, 0.3}, {0.6, 0.3, 0}};
  return hc[h[t][0]][h[c][0]] + hc[h[t][1]][h[c][1]];
}

static double wgt(int ei) { return ei >= 4 ? 1.0 : 0.1; }

static double sq(const double *a, const double *b) {
  double s = 0;
  for (int k = 0; k < NC; k++) s += (a[k] - b[k]) * (a[k] - b[k]);
  return s;
}

static struct sotto_vlr_enc *create(int rate, int core) {
  const vlr_mode_t *m = rate == 600 ? &vlr_mode600 : rate == 400 ? &vlr_mode400 : NULL;
  struct sotto_vlr_enc *e;
  if (!m || !(e = calloc(1, sizeof *e))) return NULL;
  e->m = m;
  if (core && !(e->core = sotto_enc_create())) {
    free(e);
    return NULL;
  }
  for (int j = 0; j < m->na; j++)
    for (int i = 0; i < m->ns[j]; i++) {
      double st = m->step[j][i] / 65536.0;
      for (int x = 0; x < (NC << m->sbits[j][i]); x++) e->cb[j][i][x] = m->cb[j][i][x] * st;
    }
  e->prev_wc = 64;
  return e;
}

struct sotto_vlr_enc *sotto_vlr_enc_create(int rate) { return create(rate, 1); }

/* sotto 600 / 400 hiss (sotto_hiss.c): no core; the frames come from v3.8's front end */
struct sotto_vlr_enc *sotto_vlr_enc_create_frames(int rate) { return create(rate, 0); }

void sotto_vlr_enc_destroy(struct sotto_vlr_enc *e) {
  if (!e) return;
  sotto_enc_destroy(e->core);
  free(e);
}

/* ------------------------------------------------------------------ shape anchors */
/* M-best paths through slot j's stages for target c, best first (SottoVLR._cands) */
static void cands(const struct sotto_vlr_enc *e, int j, const double *c, vlr_anchor_t *a) {
  const vlr_mode_t *m = e->m;
  double rec[MB][NC], nrec[MB][NC];
  short path[MB][VLR_MAX_STAGES], npath[MB][VLR_MAX_STAGES];
  int np = 1;
  memset(rec[0], 0, sizeof rec[0]);
  for (int i = 0; i < m->ns[j]; i++) {
    int ncw = 1 << m->sbits[j][i], nk = 0;
    const double *cb = e->cb[j][i];
    double kd[MB];
    int kp[MB], kc[MB];
    for (int p = 0; p < np; p++) {
      double r[NC], rr = 0;
      for (int k = 0; k < NC; k++) r[k] = c[k] - rec[p][k], rr += r[k] * r[k];
      for (int w = 0; w < ncw; w++) {
        const double *y = cb + w * NC;
        double rc = 0, cc = 0;
        for (int k = 0; k < NC; k++) rc += r[k] * y[k], cc += y[k] * y[k];
        double d = rr - 2 * rc + cc;
        if (nk == MB && d >= kd[MB - 1]) continue;
        int q = nk < MB ? nk++ : MB - 1;     /* insert, keeping the earlier of equal distances first */
        while (q > 0 && kd[q - 1] > d) kd[q] = kd[q - 1], kp[q] = kp[q - 1], kc[q] = kc[q - 1], q--;
        kd[q] = d, kp[q] = p, kc[q] = w;
      }
    }
    for (int q = 0; q < nk; q++) {
      for (int k = 0; k < NC; k++) nrec[q][k] = rec[kp[q]][k] + cb[kc[q] * NC + k];
      for (int t = 0; t < i; t++) npath[q][t] = path[kp[q]][t];
      npath[q][i] = (short)kc[q];
    }
    np = nk;
    memcpy(rec, nrec, sizeof rec);
    memcpy(path, npath, sizeof path);
  }
  a->n = np;
  memcpy(a->rec, rec, sizeof rec);
  memcpy(a->path, path, sizeof path);
}

/* decide anchors lo..hi by the path ending in candidate x of anchor hi */
static void decide(struct sotto_vlr_enc *e, long lo, long hi, int x) {
  for (long k = hi; k >= lo; k--) {
    e->choice[k % QA] = x;
    if (k > lo) x = AN(e, k)->back[x];
  }
}

/* the candidate of anchor lv that state x of the newest anchor descends from */
static int ancestor(const struct sotto_vlr_enc *e, int x, long lv) {
  for (long k = e->nan - 1; k > lv; k--) x = AN(e, k)->back[x];
  return x;
}

/* every anchor whose candidate all live paths share is final */
static void settle(struct sotto_vlr_enc *e) {
  long top = e->nan - 1;
  int cur[MB], nl = 0;
  for (int i = 0; i < e->ncost; i++)
    if (e->cost[i] < HUGE_VAL) cur[nl++] = i;
  for (long k = top; k >= e->nfinal; k--) {
    int same = 1;
    for (int i = 1; i < nl; i++) same &= cur[i] == cur[0];
    if (same) {
      decide(e, e->nfinal, k, cur[0]);
      e->nfinal = k + 1;
      return;
    }
    if (k > e->nfinal)
      for (int i = 0; i < nl; i++) cur[i] = AN(e, k)->back[cur[i]];
  }
  if (top + 1 - e->nfinal > VLR_FORCE) {    /* force: the best path decides the older half */
    int b = 0;
    for (int i = 1; i < e->ncost; i++) if (e->cost[i] < e->cost[b]) b = i;
    long lv = e->nfinal + VLR_FORCE / 2;
    int x = ancestor(e, b, lv);
    decide(e, e->nfinal, lv, x);
    e->nfinal = lv + 1;
    for (int i = 0; i < e->ncost; i++)
      if (ancestor(e, i, lv) != x) e->cost[i] = HUGE_VAL;
  }
}

static void add_anchor(struct sotto_vlr_enc *e) {
  const vlr_mode_t *m = e->m;
  long k = e->nan;
  vlr_anchor_t *a = AN(e, k);
  a->j = (int)(k % m->na);
  a->f = (int)((k / m->na) * m->n + m->aoff[a->j]);
  const sotto_fparams_t *pf = FR(e, a->f);
  cands(e, a->j, pf->c, a);
  double wf = wgt(pf->ei);
  if (k == 0) {
    for (int b = 0; b < a->n; b++) e->cost[b] = wf * sq(a->rec[b], pf->c);
  } else {
    const vlr_anchor_t *pa = AN(e, k - 1);
    double nc[MB];
    for (int b = 0; b < a->n; b++) {
      double own = wf * sq(a->rec[b], pf->c), best = HUGE_VAL;
      int arg = 0;
      for (int p = 0; p < e->ncost; p++) {
        double t = e->cost[p] + own;
        for (int g = pa->f + 1; g < a->f; g++) {
          const sotto_fparams_t *q = FR(e, g);
          double tt = (double)(g - pa->f) / (a->f - pa->f), s = 0;
          for (int c = 0; c < NC; c++) {
            double r = (1 - tt) * pa->rec[p][c] + tt * a->rec[b][c] - q->c[c];
            s += r * r;
          }
          t += wgt(q->ei) * s;
        }
        if (t < best) best = t, arg = p;
      }
      nc[b] = best, a->back[b] = (unsigned char)arg;
    }
    memcpy(e->cost, nc, sizeof nc);
  }
  e->ncost = a->n;
  e->nan++;
  settle(e);
}

/* ------------------------------------------------------------------ the other fields */
static void put_bits(unsigned char *b, int *pos, int v, int w) {
  for (int i = w - 1; i >= 0; i--, (*pos)++)
    if ((v >> i) & 1) b[*pos >> 3] |= (unsigned char)(0x80 >> (*pos & 7));
}

static int pitch_at(const vlr_mode_t *m, int wc, int ws, int k) {
  double t = k - (m->n - 1) / 2.0;
  int w = (int)floor(wc + m->slope16[ws] / 16.0 * t + 0.5);
  return w < 0 ? 0 : w > 127 ? 127 : w;
}

static int energy_at(const vlr_mode_t *m, int em, int er, int k) {
  int x = (int)floor(em * 31 / 31.0 + m->eres16[er * m->n + k] / 16.0 + 0.5);
  return x < 0 ? 0 : x > 31 ? 31 : x;
}

static void code_superframe(struct sotto_vlr_enc *e, long s) {
  const vlr_mode_t *m = e->m;
  int n = m->n, code[VLR_MAX_N], wi[VLR_MAX_N], ei[VLR_MAX_N], voiced[VLR_MAX_N], nv = 0;
  double w[VLR_MAX_N];
  for (int k = 0; k < n; k++) {
    const sotto_fparams_t *p = FR(e, s * n + k);
    code[k] = p->code, wi[k] = p->wi, ei[k] = p->ei, w[k] = wgt(p->ei);
  }
  /* voicing pattern */
  int v = 0;
  double vb = HUGE_VAL;
  for (int q = 0; q < (1 << m->bv); q++) {
    double c = 0;
    for (int k = 0; k < n; k++) c += v_cost(code[k], m->vpat[q * n + k]) * w[k];
    if (c < vb) vb = c, v = q;
  }
  /* pitch line through the frames voiced both in the input and in the pattern (else the input's) */
  for (int k = 0; k < n; k++) nv += voiced[k] = code[k] != 0 && m->vpat[v * n + k] != 0;
  if (!nv)
    for (int k = 0; k < n; k++) nv += voiced[k] = code[k] != 0;
  int wc = e->prev_wc, ws = 0;
  if (!nv) {
    for (int q = 1; q < (1 << m->bs); q++)
      if (abs(m->slope16[q]) < abs(m->slope16[ws])) ws = q;
  } else {
    double c;
    if (nv == 1) {
      int k = 0;
      while (!voiced[k]) k++;
      c = wi[k];
    } else {
      double sn = 0, st = 0, stt = 0, sy = 0, sty = 0;
      for (int k = 0; k < n; k++)
        if (voiced[k]) {
          double t = k - (n - 1) / 2.0;
          sn += 1, st += t, stt += t * t, sy += wi[k], sty += t * wi[k];
        }
      double sl = (sn * sty - st * sy) / (sn * stt - st * st);
      c = (sy - sl * st) / sn;
    }
    int c0 = (int)rint(c);
    c0 = c0 < 0 ? 0 : c0 > 127 ? 127 : c0;
    long best = -1;
    for (int x = c0 > 0 ? c0 - 1 : 0; x <= (c0 < 127 ? c0 + 1 : 127); x++)
      for (int q = 0; q < (1 << m->bs); q++) {
        long err = 0;
        for (int k = 0; k < n; k++)
          if (voiced[k]) {
            long d = pitch_at(m, x, q, k) - wi[k];
            err += d * d;
          }
        if (best < 0 || err < best) best = err, wc = x, ws = q;
      }
  }
  e->prev_wc = wc;
  /* energy: mean and residual */
  double mean = 0;
  for (int k = 0; k < n; k++) mean += ei[k];
  int m0 = (int)rint(mean / n * 31 / 31), em = 0, er = 0;
  long eb = -1;
  for (int x = m0 > 0 ? m0 - 1 : 0; x <= (m0 < 31 ? m0 + 1 : 31); x++)
    for (int q = 0; q < (1 << m->br); q++) {
      long err = 0;
      for (int k = 0; k < n; k++) {
        int y = energy_at(m, x, q, k);
        err += (long)(y - ei[k]) * (y - ei[k]) + 4 * ((y == 0) != (ei[k] == 0));
      }
      if (eb < 0 || err < eb) eb = err, em = x, er = q;
    }
  unsigned char *b = e->out[e->nout % QO];
  int pos = 0;
  memset(b, 0, SOTTO_BYTES_PER_FRAME);
  put_bits(b, &pos, v, m->bv);
  put_bits(b, &pos, wc, 7);
  put_bits(b, &pos, ws, m->bs);
  put_bits(b, &pos, em, 5);
  put_bits(b, &pos, er, m->br);
  for (int j = 0; j < m->na; j++) {
    long k = s * m->na + j;
    const vlr_anchor_t *a = AN(e, k);
    for (int i = 0; i < m->ns[j]; i++) put_bits(b, &pos, a->path[e->choice[k % QA]][i], m->sbits[j][i]);
  }
  e->nout++;
}

/* code every superframe whose frames are in and whose anchors are decided */
static void drain(struct sotto_vlr_enc *e) {
  const vlr_mode_t *m = e->m;
  for (;;) {
    long k = e->nan;
    long f = (k / m->na) * m->n + m->aoff[k % m->na];
    if (f >= e->nf) break;
    add_anchor(e);
  }
  while ((e->ns_out + 1) * m->n <= e->nf && (e->ns_out + 1) * m->na <= e->nfinal && e->nout - e->npop < QO)
    code_superframe(e, e->ns_out++);
}

static void pull(struct sotto_vlr_enc *e) {
  sotto_enc_frame_t f;
  while (sotto_enc_pop_frame(e->core, &f)) *FR(e, e->nf++) = f.p;
  drain(e);
}

void sotto_vlr_enc_push(struct sotto_vlr_enc *e, const sotto_fparams_t *p) {
  *FR(e, e->nf++) = *p;
  drain(e);
}

/* no more frames: pad the last superframe, settle the last anchors */
void sotto_vlr_enc_end(struct sotto_vlr_enc *e) {
  const vlr_mode_t *m = e->m;
  if (e->done) return;
  e->done = 1;
  if (e->nf > 0) {
    while (e->nf % m->n) *FR(e, e->nf) = *FR(e, e->nf - 1), e->nf++;   /* pad the last superframe */
    drain(e);
    /* the frames after the last anchor hold it; then the best path decides the rest */
    const vlr_anchor_t *a = AN(e, e->nan - 1);
    for (int b = 0; b < e->ncost; b++)
      for (long g = a->f + 1; g < e->nf; g++) e->cost[b] += wgt(FR(e, g)->ei) * sq(a->rec[b], FR(e, g)->c);
    int b = 0;
    for (int i = 1; i < e->ncost; i++) if (e->cost[i] < e->cost[b]) b = i;
    if (e->nfinal < e->nan) decide(e, e->nfinal, e->nan - 1, b);
    e->nfinal = e->nan;
  }
  drain(e);
}

void sotto_vlr_encode(struct sotto_vlr_enc *e, const short *speech, int n80) {
  for (int i = 0; i < n80; i++) sotto_enc_analyse_block(e->core, speech + 80 * i);
  pull(e);
}

int sotto_vlr_enc_pop(struct sotto_vlr_enc *e, unsigned char out[SOTTO_BYTES_PER_FRAME]) {
  if (e->npop >= e->nout) return 0;
  memcpy(out, e->out[e->npop++ % QO], SOTTO_BYTES_PER_FRAME);
  return 1;
}

int sotto_vlr_enc_flush(struct sotto_vlr_enc *e, unsigned char out[SOTTO_BYTES_PER_FRAME]) {
  if (!e->done) {
    sotto_enc_flush_core(e->core);
    pull(e);
    sotto_vlr_enc_end(e);
  }
  drain(e);
  return sotto_vlr_enc_pop(e, out);
}
