/* Very-low-rate sotto decoder (sotto 600, sotto 400) in fixed point, integer only.
 *
 * SottoVLR.frame_params (sim/sotto_vlr.py) in a stream: each 48-bit superframe becomes N
 * per-20 ms field sets (voicing code, pitch index, energy code, 19 shape coefficients), which
 * the v3.7 fixed-point decoder core synthesises (sotto_dec_frame, as sotto 1200 does). The
 * shape of a frame between two anchors is on the straight line between them; a lone lost
 * superframe is rebuilt from its neighbours; other losses repeat the last good superframe
 * under the core's hold-and-fade. Superframe s is played when s + 2 has arrived: the frames
 * after s's last anchor run to the next anchor, which is s + 2's first when s + 1 is rebuilt.
 * It plays one 20 ms frame at a time (sotto_vlr_decode_frame), so a caller needs no buffer of
 * N x 160 samples (the K6 pager); sotto_vlr_decode is N of those calls. With -DVLR_CB_SPI the
 * shape codebooks are read a vector at a time (VLR_CB_VEC, sotto_vlr_tables.h).
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

typedef struct {
  int16_t v, wc, ws, em, er, a[VLR_MAX_ANCHORS][VLR_MAX_STAGES];
} vlr_fields_t;

struct sotto_vlr_dec {
  struct sotto_dec *core;
  const vlr_mode_t *m;
  vlr_fields_t in[4];               /* the last 4 superframes received (a lost one: the last good one's) */
  vlr_fields_t last;                /* the last good superframe (all zero before the first) */
  unsigned char good[4];
  int n_in, s, run, flushed;        /* superframes in; the one playing (or next); concealment run */
  int k;                            /* the next frame of superframe s, or -1 between superframes */
  int32_t pa[VLR_NC];               /* the last anchor played and its frame */
  int pa_f;
  int16_t lcode, lwi, lei;          /* the last frame played */
  /* superframe s, set up by begin(): its anchors, the next one after them, the frame before */
  int reb, na, nf, avail, af[VLR_MAX_ANCHORS];
  int32_t anc[VLR_MAX_ANCHORS][VLR_NC], nx[VLR_NC];
  int16_t lcode0, lwi0, lei0;
};

static int get_bits(const unsigned char *b, int *pos, int w) {
  int x = 0;
  for (int i = 0; i < w; i++, (*pos)++) x = (x << 1) | ((b[*pos >> 3] >> (7 - (*pos & 7))) & 1);
  return x;
}

static void unpack(const vlr_mode_t *m, const unsigned char *b, vlr_fields_t *f) {
  int pos = 0;
  f->v = (int16_t)get_bits(b, &pos, m->bv);
  f->wc = (int16_t)get_bits(b, &pos, 7);
  f->ws = (int16_t)get_bits(b, &pos, m->bs);
  f->em = (int16_t)get_bits(b, &pos, 5);
  f->er = (int16_t)get_bits(b, &pos, m->br);
  for (int j = 0; j < m->na; j++)
    for (int i = 0; i < m->ns[j]; i++) f->a[j][i] = (int16_t)get_bits(b, &pos, m->sbits[j][i]);
}

static void anchor(const vlr_mode_t *m, const vlr_fields_t *f, int j, int32_t *c) {
  int8_t buf[VLR_NC];
  for (int k = 0; k < VLR_NC; k++) c[k] = 0;
  for (int i = 0; i < m->ns[j]; i++) {
    const int8_t *y = VLR_CB_VEC(m, j, i, f->a[j][i], buf);
    int32_t st = m->step[j][i];
    for (int k = 0; k < VLR_NC; k++) c[k] += (int32_t)y[k] * st;
  }
}

static int floor_div(int32_t x, int32_t d) { return x >= 0 ? x / d : -((-x + d - 1) / d); }

static int clampi(int x, int lo, int hi) { return x < lo ? lo : x > hi ? hi : x; }

/* SottoVLR.pitch_frames / energy_frames for frame k */
static int pitch(const vlr_mode_t *m, const vlr_fields_t *f, int k) {
  return clampi(floor_div(32 * f->wc + m->slope16[f->ws] * (2 * k - (m->n - 1)) + 16, 32), 0, 127);
}

static int energy(const vlr_mode_t *m, const vlr_fields_t *f, int k) {
  return clampi(floor_div(16 * f->em + m->eres16[f->er * m->n + k] + 8, 16), 0, 31);
}

/* frame k of n between a (the frame before) and b (the frame after), rounded half up */
static int lerp_round(int a, int b, int k, int n) {
  int num = a * (n - k) + b * (k + 1);
  return (2 * num + n + 1) / (2 * (n + 1));
}

static int is_good(const struct sotto_vlr_dec *d, int t) { return t >= 0 && t < d->n_in && d->good[t & 3]; }

static int rebuilt(const struct sotto_vlr_dec *d, int t) {
  return t >= 1 && t + 1 < d->n_in && !is_good(d, t) && is_good(d, t - 1) && is_good(d, t + 1);
}

/* set up superframe s: its anchors and the anchor after them (s + 1's first, or s + 2's if
   s + 1 is rebuilt) */
static void begin(struct sotto_vlr_dec *d) {
  const vlr_mode_t *m = d->m;
  int n = m->n, s = d->s;
  const vlr_fields_t *f = &d->in[s & 3];
  d->reb = rebuilt(d, s);
  d->na = d->reb ? 0 : m->na;
  d->avail = d->reb || is_good(d, s);
  d->nf = -1;
  for (int j = 0; j < d->na; j++) anchor(m, f, j, d->anc[j]), d->af[j] = s * n + m->aoff[j];
  if (s + 1 < d->n_in) {
    int u = rebuilt(d, s + 1) ? s + 2 : s + 1;
    anchor(m, &d->in[u & 3], 0, d->nx);
    d->nf = u * n + m->aoff[0];
  }
  d->lcode0 = d->lcode, d->lwi0 = d->lwi, d->lei0 = d->lei;   /* the frame before this superframe */
  d->k = 0;
}

/* frame d->k of superframe s */
static void frame(struct sotto_vlr_dec *d, short *out) {
  const vlr_mode_t *m = d->m;
  int n = m->n, s = d->s, k = d->k, na = d->na, nf = d->nf, g = s * n + k, fa, fb, i;
  const vlr_fields_t *f = &d->in[s & 3], *fn = &d->in[(s + 1) & 3];
  const int32_t *ca, *cb;
  fx_fparams_t p;
  if (na && d->af[0] <= g) {
    for (i = 0; i + 1 < na && d->af[i + 1] <= g; i++) {}
    ca = d->anc[i], fa = d->af[i];
    if (i + 1 < na) cb = d->anc[i + 1], fb = d->af[i + 1];
    else if (nf >= 0) cb = d->nx, fb = nf;
    else cb = ca, fb = -1;
  } else {                          /* a rebuilt superframe: from the last anchor played */
    ca = d->pa, fa = d->pa_f;
    if (na) cb = d->anc[0], fb = d->af[0];
    else if (nf >= 0) cb = d->nx, fb = nf;
    else cb = ca, fb = -1;
  }
  for (int c = 0; c < VLR_NC; c++) {
    if (fb < 0 || g == fa) p.c[c] = ca[c];
    else {
      int32_t x = (cb[c] - ca[c]) * (g - fa), dd = fb - fa;
      p.c[c] = ca[c] + (x >= 0 ? (x + dd / 2) / dd : -((-x + dd / 2) / dd));
    }
  }
  if (!d->reb) {
    p.code = m->vpat[f->v * n + k];
    p.wi = (int16_t)pitch(m, f, k);
    p.ei = (int16_t)energy(m, f, k);
  } else {                          /* between the last frame played and s + 1's first */
    int lcode = d->lcode0, lwi = d->lwi0, lei = d->lei0;
    int cn = m->vpat[fn->v * n], code = 2 * (k + 1) < n + 1 ? lcode : cn;
    int wb = pitch(m, fn, 0), va = lcode != 0, vb = cn != 0;
    p.code = (int16_t)(code == 3 ? 2 : code);
    p.wi = (int16_t)(va && vb ? lerp_round(lwi, wb, k, n) : (va || !vb) ? lwi : wb);
    p.ei = (int16_t)lerp_round(lei, energy(m, fn, 0), k, n);
  }
  d->run = d->avail ? 0 : d->run + 1;
#ifdef VLR_DEBUG
  fprintf(stderr, "F %d %d %d %d %d %ld %ld\n", g, p.code, p.wi, p.ei, d->run, (long)p.c[0], (long)p.c[5]);
#endif
  sotto_dec_frame(d->core, &p, d->run, out);
  d->lcode = p.code, d->lwi = p.wi, d->lei = p.ei;
  if (++d->k == n) {                /* the superframe is done */
    if (na) memcpy(d->pa, d->anc[na - 1], sizeof d->pa), d->pa_f = d->af[na - 1];
    d->s++;
    d->k = -1;
  }
}

static void ingest(struct sotto_vlr_dec *d, const unsigned char *in, int good) {
  vlr_fields_t *f = &d->in[d->n_in & 3];
  if (good) unpack(d->m, in, f), d->last = *f;
  else *f = d->last;
  d->good[d->n_in++ & 3] = (unsigned char)(good != 0);
}

struct sotto_vlr_dec *sotto_vlr_dec_create(int rate) {
  const vlr_mode_t *m = rate == 600 ? &vlr_mode600 : rate == 400 ? &vlr_mode400 : NULL;
  struct sotto_vlr_dec *d;
  if (!m || !(d = calloc(1, sizeof *d))) return NULL;
  d->m = m;
  d->k = -1;
  if (!(d->core = sotto_dec_create())) {
    free(d);
    return NULL;
  }
  return d;
}

void sotto_vlr_dec_destroy(struct sotto_vlr_dec *d) {
  if (!d) return;
  sotto_dec_destroy(d->core);
  free(d);
}

int sotto_vlr_samples(const struct sotto_vlr_dec *d) { return d->m->n * SOTTO_SAMPLES_PER_FRAME; }

void sotto_vlr_decode(struct sotto_vlr_dec *d, const unsigned char in[SOTTO_BYTES_PER_FRAME], int good,
                      short *speech) {
  for (int k = 0; k < d->m->n; k++)
    sotto_vlr_decode_frame(d, in, good, k, speech + k * SOTTO_SAMPLES_PER_FRAME);
}

int sotto_vlr_decode_frame(struct sotto_vlr_dec *d, const unsigned char in[SOTTO_BYTES_PER_FRAME], int good, int k,
                           short speech[SOTTO_SAMPLES_PER_FRAME]) {
  if (k == 0) {
    ingest(d, in, good);
    if (d->n_in - 1 - SOTTO_VLR_DEC_DELAY >= 0) begin(d);
  }
  if (d->k < 0) {                   /* the start-up delay */
    memset(speech, 0, SOTTO_SAMPLES_PER_FRAME * sizeof(short));
    return 0;
  }
  frame(d, speech);
  return 1;
}

int sotto_vlr_dec_flush_frame(struct sotto_vlr_dec *d, short speech[SOTTO_SAMPLES_PER_FRAME]) {
  if (d->k < 0 && d->s < d->n_in) begin(d);
  if (d->k >= 0) {
    frame(d, speech);
    return 1;
  }
  memset(speech, 0, SOTTO_SAMPLES_PER_FRAME * sizeof(short));
  if (d->flushed || d->n_in == 0) return 0;
  sotto_dec_flush(d->core, speech);     /* the core's last frame */
  d->flushed = 1;
  return 1;
}

int sotto_vlr_dec_flush(struct sotto_vlr_dec *d, short *speech) {
  int ns = sotto_vlr_samples(d), any = 0;
  memset(speech, 0, (size_t)ns * sizeof(short));
  if (d->k < 0 && d->s < d->n_in) {     /* a whole superframe */
    for (int k = 0; k < d->m->n; k++) sotto_vlr_dec_flush_frame(d, speech + k * SOTTO_SAMPLES_PER_FRAME);
    return 1;
  }
  if (d->k < 0 && !d->flushed && d->n_in) any = sotto_vlr_dec_flush_frame(d, speech);
  return any;
}

int sotto_vlr_dec_bytes(void) { return (int)sizeof(struct sotto_vlr_dec); }
