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

#define LR_DELAY (LR_INTERP ? 2 : 1)  /* superframes: B(s) needs A(s + 1); LR_INTERP the one after */
typedef char lr_delay_matches_header[LR_DELAY == SOTTO_LR_DEC_DELAY ? 1 : -1];

struct sotto_lr_dec {
  struct sotto_dec *core;
  lr_fields_t in[4];            /* the last 4 superframes received (a lost one: the last good one's) */
  lr_fields_t last;             /* the last good superframe (silence with no steps before one) */
  lr_params_t a;                /* A(s), s = n - 1 - LR_DELAY: the last A frame pushed */
  lr_params_t an;               /* A(s + 1), pushed by the second half */
  unsigned char good[4];
  unsigned n;                   /* calls so far (superframes in, then flushes) */
  int n_in, run, rebuilt;   /* run: concealment run of the last frame pushed */
  int pend, pend_s, pend_u, avail_u;   /* the second half to come: superframe s, has A(s + 1) */
#ifdef LR_HISS
  /* sotto 1200 hiss (sim/sotto_lr_hiss.py decode_audio16): the 4-band hiss correction of
     (LR_HISS_T: double in dB, or int32_t in dB Q8 for the fixed-point K6 layer, which also sets
     LR_HISS_HOOK_T, LR_HISS_CB6, LR_HISS_BCB and LR_HISS_HALF)
     A(s) and A(u), A(u)'s side bit and whether its superframe arrived, and the hook that
     gets every frame the core synthesises */
  LR_HISS_T hra[4], hran[4];
  int hsu, hsgu;
  LR_HISS_HOOK_T hook;
  void *hctx;
#endif
};

#ifdef LR_HISS
/* Sotto1200Hiss._b_base: the mean of the corrections of the unvoiced anchors, else 0 */
static void h_base(const LR_HISS_T *x, int hx, const LR_HISS_T *y, int hy, LR_HISS_T *o) {
  for (int b = 0; b < 4; b++) o[b] = hx && hy ? LR_HISS_HALF(x[b] + y[b]) : hx ? x[b] : hy ? y[b] : 0;
}

static void h_cb6(int wa, LR_HISS_T *o) {
  for (int b = 0; b < 4; b++) o[b] = LR_HISS_CB6((wa >> 1) * 4 + b);
}
#endif

static int is_good(const struct sotto_lr_dec *d, int t) { return t >= 0 && t < d->n_in && d->good[t & 3]; }

/* the frame halfway between frames a and n by B's fields in f (Sotto1200._b_frame); f->sb < 0:
   no residual. p may be n: each field of n is read before p's is written */
static __attribute__((noinline)) void b_frame(const lr_params_t *a, const lr_params_t *n, const lr_fields_t *f,
                                              lr_params_t *p) {
  int cb = f->cb, dw = f->dw;
  int wint = wo_interp(a->wi, a->code != 0, n->wi, n->code != 0);
  if (wint < 0) p->wi = cb ? lr_w_abs[dw] : a->wi;
  else if (cb == 0) p->wi = wint;
  else {
    int w = wint + lr_w_tab[dw];
    p->wi = w < 0 ? 0 : w > 127 ? 127 : w;
  }
  p->code = cb;
  p->ei = e_b(a->ei, n->ei, f->de);
  lr_shape_b(a->c, n->c, f->sb, p->c);
}

static __attribute__((noinline)) void a_params(const lr_fields_t *f, lr_params_t *p) {
  p->code = f->ca, p->wi = f->wa, p->ei = f->ea;
  lr_shape_a(f->sa, p->c);
}

static int run_of(struct sotto_lr_dec *d, int avail) { return d->run = avail ? 0 : d->run + 1; }

/* one 40 ms output for call t = d->n, in two 20 ms halves: superframe s = t - LR_DELAY, as
   B(s) then A(s + 1) (the core is one 20 ms frame behind), or silence before the first.
   half0 returns 0 past the last superframe. */
static int half0(struct sotto_lr_dec *d, short *out) {
  int t = (int)d->n, s = t - LR_DELAY, u = s + 1, have_u = u >= 0 && u < d->n_in;
  int reb = LR_INTERP && have_u && !is_good(d, u) && is_good(d, s) && is_good(d, u + 1);
  int avail = is_good(d, u) || reb, rb = d->rebuilt;
  lr_params_t *an = &d->an, b;  /* A(u), B(s) */
  lr_fields_t z;                /* B's rule with no steps or residual (rebuilt frames) */
  if (s >= d->n_in) return 0;
  d->n++;
  z.dw = LR_W_ZERO, z.de = LR_E_ZERO, z.sb = -1;
  if (have_u) {                 /* A(u): received, the last good one, or rebuilt in place from A(s), A(u + 1) */
    a_params(&d->in[(u + reb) & 3], an);
    if (reb) z.cb = d->a.code, b_frame(&d->a, an, &z, an);
#ifdef LR_HISS
    memset(d->hran, 0, sizeof d->hran);
    d->hsu = 0, d->hsgu = is_good(d, u);
    if (an->code == 0) {
      if (reb) {                /* rebuilt: halfway between A(s)'s and A(u + 1)'s */
        const lr_fields_t *fn = &d->in[(u + 1) & 3];
        LR_HISS_T q[4];
        if (fn->ca == 0) h_cb6(fn->wa, q);
        h_base(d->hra, d->a.code == 0, q, fn->ca == 0, d->hran);
      } else {                  /* the 6-bit index and the side bit in A's pitch field */
        h_cb6(d->in[u & 3].wa, d->hran);
        d->hsu = d->in[u & 3].wa & 1;
      }
    }
#endif
  }
  if (s >= 0) {                 /* B(s): by its fields, or (rebuilt) halfway to A(u) with its voicing */
    const lr_fields_t *f = &d->in[s & 3];
    if (rb) z.cb = an->code, f = &z;
    b_frame(&d->a, have_u && avail ? an : &d->a, f, &b);
    int run = run_of(d, rb || is_good(d, s));
    sotto_dec_frame(d->core, &b, run, out);
#ifdef LR_HISS
    if (d->hook) {              /* B: the anchors' mean plus a 2-bit level, and a side bit */
      LR_HISS_T r[4];
      int side = 0;
      h_base(d->hra, d->a.code == 0, d->hran, have_u && an->code == 0, r);
      if (b.code != 0) memset(r, 0, sizeof r);
      else if (!rb) {
        for (int k = 0; k < 4; k++) r[k] += LR_HISS_BCB((f->dw >> 1) * 4 + k);
        side = f->dw & 1;
      }
      d->hook(d->hctx, b.code, r, side, is_good(d, s), run, out, 0);
    }
#endif
  } else {
    memset(out, 0, SOTTO_SAMPLES_PER_FRAME * sizeof(short));
  }
  d->rebuilt = reb;
  d->pend = 1, d->pend_s = s, d->pend_u = have_u, d->avail_u = avail;
  return 1;
}

static int half1(struct sotto_lr_dec *d, short *out) {
  if (!d->pend) {
    memset(out, 0, SOTTO_SAMPLES_PER_FRAME * sizeof(short));
    return 0;
  }
  d->pend = 0;
  if (d->pend_u) {
    int run = run_of(d, d->avail_u);
    sotto_dec_frame(d->core, &d->an, run, out), d->a = d->an;
#ifdef LR_HISS
    memcpy(d->hra, d->hran, sizeof d->hra);
    if (d->hook) d->hook(d->hctx, d->an.code, d->hran, d->hsu, d->hsgu, run, out, 0);
#endif
  } else if (d->pend_s >= 0) {
    sotto_dec_flush(d->core, out);
#ifdef LR_HISS
    if (d->hook) d->hook(d->hctx, 0, NULL, 0, 0, 0, out, 1);
#endif
  }
  if (d->pend_s < 0) memset(out, 0, SOTTO_SAMPLES_PER_FRAME * sizeof(short));   /* A(0) in: the core's first frame out is silent */
  return 1;
}

static int step(struct sotto_lr_dec *d, short *out) {
  if (!half0(d, out)) return 0;
  half1(d, out + SOTTO_SAMPLES_PER_FRAME);
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
  d->last.dw = LR_W_ZERO, d->last.de = LR_E_ZERO;
  return d;
}

void sotto_lr_dec_destroy(struct sotto_lr_dec *d) {
  if (!d) return;
  sotto_dec_destroy(d->core);
  free(d);
}

static void put_in(struct sotto_lr_dec *d, const unsigned char *in, int good) {
  lr_fields_t *f = &d->in[d->n_in & 3];
  if (good) unpack(in, f), d->last = *f;
  else *f = d->last;
  d->good[d->n_in++ & 3] = (unsigned char)(good != 0);
}

void sotto_lr_decode(struct sotto_lr_dec *d, const unsigned char in[SOTTO_BYTES_PER_FRAME], int good,
                     short speech[SOTTO_LR_SAMPLES_PER_FRAME]) {
  put_in(d, in, good);
  step(d, speech);
}

int sotto_lr_decode_half(struct sotto_lr_dec *d, const unsigned char in[SOTTO_BYTES_PER_FRAME], int good,
                         int half, short speech[SOTTO_SAMPLES_PER_FRAME]) {
  if (half) return half1(d, speech);
  put_in(d, in, good);
  return half0(d, speech);
}

int sotto_lr_dec_flush_half(struct sotto_lr_dec *d, int half, short speech[SOTTO_SAMPLES_PER_FRAME]) {
  if (half) return half1(d, speech);
  if (half0(d, speech)) return 1;
  memset(speech, 0, SOTTO_SAMPLES_PER_FRAME * sizeof(short));
  return 0;
}

int sotto_lr_dec_flush(struct sotto_lr_dec *d, short speech[SOTTO_LR_SAMPLES_PER_FRAME]) {
  if (step(d, speech)) return 1;
  memset(speech, 0, SOTTO_LR_SAMPLES_PER_FRAME * sizeof(short));
  return 0;
}
