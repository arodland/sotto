/* sotto 1200 streaming decoder, shared by the float (sotto_lr.c) and fixed-point
 * (fixed/sotto_fx_lr.c) builds; each defines before including it:
 *   lr_params_t                  the decoder core's per-frame fields (code, wi, ei, c[NC])
 *   lr_shape_a(sa, c)            A's shape from its stage indices
 *   lr_shape_b(ca, cn, sb, c)    halfway between shapes ca and cn, plus 1.5 x stage-3
 *                                codeword sb when sb >= 0
 *   lr_fields_t, unpack(), and the v3.7 core: sotto_dec_frame, sotto_dec_create/destroy/flush.
 *
 * Sotto1200.frame_params in a stream. Superframe s is played when s + 2 has arrived (or at
 * the flush), so that a lost superframe between two good ones can be rebuilt from both (by
 * B's rule, with no steps or residual). Other losses repeat the last good superframe under
 * v3.5's hold-and-fade.
 * SPDX-License-Identifier: MIT */

#define LR_DELAY 2                    /* superframes; LR_INTERP needs the one after the next */
typedef char lr_delay_matches_header[LR_DELAY == SOTTO_LR_DEC_DELAY ? 1 : -1];

struct sotto_lr_dec {
  struct sotto_dec *core;
  lr_fields_t in[4];            /* the last 4 superframes received (a lost one: the last good one's) */
  lr_fields_t last;             /* the last good superframe */
  lr_params_t a;                /* A(s), s = n - 1 - LR_DELAY: the last A frame pushed */
  unsigned char good[4];
  unsigned n;                   /* calls so far (superframes in, then flushes) */
  int n_in, have_good, run, rebuilt;   /* run: concealment run of the last frame pushed */
};

static int is_good(const struct sotto_lr_dec *d, int t) { return t >= 0 && t < d->n_in && d->good[t & 3]; }

/* the frame halfway between frames a and n (Sotto1200._b_frame); sb < 0: no residual.
   p may be n: each field of n is read before p's is written */
static void b_frame(const lr_params_t *a, const lr_params_t *n, int cb, int dw, int de, int sb, lr_params_t *p) {
  int wint = wo_interp(a->wi, a->code != 0, n->wi, n->code != 0);
  if (wint < 0) p->wi = cb ? lr_w_abs[dw] : a->wi;
  else if (cb == 0) p->wi = wint;
  else {
    int w = wint + lr_w_tab[dw];
    p->wi = w < 0 ? 0 : w > 127 ? 127 : w;
  }
  p->code = cb;
  p->ei = e_b(a->ei, n->ei, de);
  lr_shape_b(a->c, n->c, sb, p->c);
}

static void a_params(const lr_fields_t *f, lr_params_t *p) {
  p->code = f->ca, p->wi = f->wa, p->ei = f->ea;
  lr_shape_a(f->sa, p->c);
}

static int run_of(struct sotto_lr_dec *d, int avail) { return d->run = avail ? 0 : d->run + 1; }

/* one 40 ms output for call t = d->n: superframe s = t - LR_DELAY (B(s), then A(s + 1); the
   core is one 20 ms frame behind), or silence before the first; 0 past the last */
static int step(struct sotto_lr_dec *d, short *out) {
  int t = (int)d->n++, s = t - LR_DELAY, u = s + 1, have_u = u >= 0 && u < d->n_in;
  int reb = LR_INTERP && have_u && !is_good(d, u) && is_good(d, s) && is_good(d, u + 1);
  int avail = is_good(d, u) || reb, rb = d->rebuilt;
  lr_params_t an, b;            /* A(u), B(s) */
  an.code = 0;                  /* (set whenever it is read; quiets -Wmaybe-uninitialized) */
  short *o = out;
  if (s >= d->n_in) return 0;
  if (have_u) {                 /* A(u): received, the last good one, or rebuilt in place from A(s), A(u + 1) */
    a_params(&d->in[(u + reb) & 3], &an);
    if (reb) b_frame(&d->a, &an, d->a.code, LR_W_ZERO, LR_E_ZERO, -1, &an);
  }
  if (s >= 0) {                 /* B(s): by its fields, or (rebuilt) halfway to A(u) */
    const lr_fields_t *f = &d->in[s & 3];
    b_frame(&d->a, have_u && avail ? &an : &d->a, rb ? an.code : f->cb, rb ? LR_W_ZERO : f->dw,
            rb ? LR_E_ZERO : f->de, rb ? -1 : f->sb, &b);
    sotto_dec_frame(d->core, &b, run_of(d, rb || is_good(d, s)), o);
    o += SOTTO_SAMPLES_PER_FRAME;
  }
  if (have_u) sotto_dec_frame(d->core, &an, run_of(d, avail), o), d->a = an;
  else if (s >= 0) sotto_dec_flush(d->core, o);
  if (s < 0) memset(out, 0, SOTTO_LR_SAMPLES_PER_FRAME * sizeof(short));   /* A(0)'s output is silent */
  d->rebuilt = reb;
  return 1;
}

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
  lr_fields_t *f = &d->in[d->n_in & 3];
  if (good) unpack(in, f), d->last = *f, d->have_good = 1;
  else if (d->have_good) *f = d->last;
  else memset(f, 0, sizeof *f), f->dw = LR_W_ZERO, f->de = LR_E_ZERO;
  d->good[d->n_in++ & 3] = (unsigned char)(good != 0);
  step(d, speech);
}

int sotto_lr_dec_flush(struct sotto_lr_dec *d, short speech[SOTTO_LR_SAMPLES_PER_FRAME]) {
  if (step(d, speech)) return 1;
  memset(speech, 0, SOTTO_LR_SAMPLES_PER_FRAME * sizeof(short));
  return 0;
}
