/* ifrx: the K6's IF matched-filter receiver (ifrx.h). SPDX-License-Identifier: MIT */
#include "ifrx.h"
#include "ifrx_tables.h"

/* sin(2 pi k / 64), Q14, k = 0..16 (a quarter turn) */
static const int16_t ifrx_qs[17] = {0,     1606,  3196,  4756,  6270,  7723,  9102,  10394, 11585,
                                    12665, 13623, 14449, 15137, 15679, 16069, 16305, 16384};

static int32_t ifrx_sin(uint32_t k) {
  int32_t v = ifrx_qs[(k & 16) ? 16 - (k & 15) : (k & 15)];
  return (k & 32) ? -v : v;
}

/* IFRX_ONLY = IFRX_FFSK or IFRX_4FSK builds one mode, with its sizes as constants (smaller:
   a K6 module); without it both, chosen at ifrx_init.
   Templates come in pairs: the one for an audio start phase half a turn on is the conjugate
   of the other (to 0.999 on the model), so only the first P/2 phases are kept, and one set of
   four sums per kept template (wr tr, wi ti, wi tr, wr ti) gives both correlations. */
#if defined(IFRX_ONLY) && IFRX_ONLY == IFRX_4FSK
#define IFRX_MM(s) 4
#define IFRX_HSH(s) 1
#elif defined(IFRX_ONLY)
#define IFRX_MM(s) 2
#define IFRX_HSH(s) 0
#else
#define IFRX_MM(s) ((s)->M)
#define IFRX_HSH(s) ((s)->hsh)
#endif
#define IFRX_NS(s) (IFRX_MM(s) << IFRX_HSH(s)) /* kept templates */

#define IFRX_NOINLINE __attribute__((noinline))
#define FSH 7 /* filter output scaling: a strong carrier (counts +-32) gives about 2000 */

IFRX_NOINLINE static uint32_t ifrx_udiv(uint32_t n, uint32_t d) {
  uint32_t q = 0, bit = 1;
  if (!d) return 0;
  while (d < n && !(d & 0x80000000u)) d <<= 1, bit <<= 1;
  while (bit) {
    if (n >= d) n -= d, q |= bit;
    d >>= 1, bit >>= 1;
  }
  return q;
}

IFRX_NOINLINE static int32_t ifrx_sdiv(int32_t n, uint32_t d) {
  return n < 0 ? -(int32_t)ifrx_udiv((uint32_t)-n, d) : (int32_t)ifrx_udiv((uint32_t)n, d);
}

static int32_t iabs(int32_t v) { return v < 0 ? -v : v; }

/* |c| to within 7% (max + 3/8 min) */
IFRX_NOINLINE static uint32_t approx_mag(const int32_t c[2]) {
  uint32_t a = (uint32_t)iabs(c[0]), b = (uint32_t)iabs(c[1]);
  return a > b ? a + (b >> 2) + (b >> 3) : b + (a >> 2) + (a >> 3);
}

/* the shift that brings every |v[0..n)| under 2^15, so a sum of two squares fits */
IFRX_NOINLINE static int sh_of(const int32_t *v, int n) {
  uint32_t mx = 0;
  int sh = 0;
  while (n--) mx |= (uint32_t)iabs(v[n]);
  while ((mx >> sh) > 32767) sh++;
  return sh;
}

IFRX_NOINLINE static int32_t clampi(int32_t v, int32_t lim) {
  return v > lim ? lim : v < -lim ? -lim : v;
}

IFRX_NOINLINE static uint32_t sq(const int32_t c[2], int sh) {
  int32_t a = c[0] >> sh, b = c[1] >> sh;
  return (uint32_t)(a * a) + (uint32_t)(b * b);
}

/* n words at p to zero */
IFRX_NOINLINE static void ifrx_zero(void *p, uint32_t n) {
  while (n--) ((volatile uint32_t *)p)[n] = 0;
}

void ifrx_rate(ifrx_t *s, uint32_t ks16) {
  uint32_t q;
  if (ks16 < 3400 * 16) ks16 = 3400 * 16;
  s->pc_q16 = ifrx_udiv(ks16 * 160u, 3); /* ks * 1000 / 64 / 1200 counts a symbol */
  s->pc_q8 = s->pc_q16 >> 8;
  s->step_q16 = ifrx_udiv(s->pc_q16, IFRX_TL);
  q = ifrx_udiv(0xFFFFFFFFu, ks16);
  s->dphi_if = ifrx_udiv(q * 52000u, 3); /* (26e6 / 1536) * 64 / (ks * 1000) turns */
  s->dphi_max = (int32_t)(q * 1536u);    /* 1.5 kHz */
  s->k_afc = ifrx_udiv(85445660u >> 5, s->pc_q8);
}

void ifrx_init(ifrx_t *s, int mode, uint32_t ks16) {
  ifrx_zero(s, sizeof(*s) / 4);
#ifdef IFRX_ONLY
  (void)mode;
#if IFRX_ONLY == IFRX_4FSK
  s->tm = &ifrx_t_4fsk[0][0][0];
  s->M = 4, s->hsh = 1, s->k = 2;
#else
  s->tm = &ifrx_t_ffsk[0][0][0];
  s->M = 2, s->hsh = 0, s->k = 1;
#endif
#else
  if (mode == IFRX_4FSK) {
    s->tm = &ifrx_t_4fsk[0][0][0];
    s->M = 4, s->hsh = 1, s->k = 2;
  } else {
    s->tm = &ifrx_t_ffsk[0][0][0];
    s->M = 2, s->hsh = 0, s->k = 1;
  }
#endif
  s->gain_sh = 6;  /* 1/64 */
  s->afc_sh = 5;   /* 1/32 */
  ifrx_rate(s, ks16);
  s->tau = IFRX_NT;
  s->th_tau = IFRX_NT * s->dphi_if;
  s->prev = -9;
  s->i = -1;
  s->ni = IFRX_NT - 1;
}

/* the filter's output at count m (in the ring), turned down by phase th */
IFRX_NOINLINE static void sample(const ifrx_t *s, uint32_t m, uint32_t th, int16_t out[2]) {
  /* the ring is kept twice over, so the taps walk back from m without a wrap */
  const int8_t *p = s->ring + IFRX_RING + (m & (IFRX_RING - 1));
  const uint32_t *g = ifrx_g, *e = g + IFRX_NT;
  int32_t re = 0, im = 0, co, si;
  do {
    int32_t c = *p--;
    uint32_t t = *g++;
    re += (int16_t)t * c;
    im += ((int32_t)t >> 16) * c;
  } while (g != e);
  re >>= FSH, im >>= FSH;
  si = ifrx_sin(th >> 26);
  co = ifrx_sin((th >> 26) + 16);
  out[0] = (int16_t)((re * co + im * si) >> 14);
  out[1] = (int16_t)((im * co - re * si) >> 14);
}

/* a template sample's re and im (4 bits each, -7..7) */
#define IFRX_TRE(b) ((int32_t)(int8_t)(b) >> 4)
#define IFRX_TIM(b) ((int32_t)((uint32_t)(b) << 28) >> 28)

/* the four sums over n samples w[0..] against template t */
/* one component of n samples (v, every other int16) against template t: the sums with its re
   and im parts. A pass a component keeps everything in the M0's eight low registers */
IFRX_NOINLINE static void corr2(const int16_t *v, const uint8_t *t, int n, int32_t *x, int32_t *y) {
  int32_t sr = 0, si = 0;
  const uint8_t *e = t + n;
  do {
    int32_t c = *v;
    sr += c * IFRX_TRE(*t);
    si += c * IFRX_TIM(*t);
    v += 2;
  } while (++t != e);
  *x = sr, *y = si;
}

IFRX_NOINLINE static void corr4(const int16_t (*w)[2], const uint8_t *t, int32_t a[4], int n) {
  corr2(&w[0][0], t, n, &a[0], &a[3]);
  corr2(&w[0][1], t, n, &a[2], &a[1]);
}

/* the four sums -> w . conj(t) (cj 0) or w . t, the conjugate template's (cj 1) */
IFRX_NOINLINE static void cof(const int32_t a[4], int cj, int32_t c[2]) {
  int32_t r = cj ? a[0] - a[1] : a[0] + a[1], i = cj ? a[2] + a[3] : a[2] - a[3];
  c[0] = r, c[1] = i;
}

static uint32_t ni_of(const ifrx_t *s, int i) {
  return s->tau + (uint32_t)((int32_t)(s->tau_f + 0x8000u + (uint32_t)(i * (int32_t)s->step_q16)) >> 16);
}

/* move the symbol clock on by adv (Q16 counts) and start the next symbol */
IFRX_NOINLINE static void advance(ifrx_t *s, int32_t adv) {
  uint32_t old = s->tau;
  adv += (int32_t)s->tau_f;
  s->tau += (uint32_t)(adv >> 16);
  s->tau_f = (uint32_t)adv & 0xFFFF;
  s->th_tau += (s->tau - old) * (s->dphi_if + (uint32_t)s->dphi_f);
  s->q++;
  s->i = -1;
  s->ni = ni_of(s, -1);
}

/* the acquisition's sweep, one symbol: the lead's fit for this offset. At the end of an
   offset's IFRX_HSYMS symbols the clock moves on a step; at the end of a sweep, on to the first
   offset a symbol later, or (it found the lead) to the lead's timing. Only ever forward: a move back
   would leave the receiver a burst of samples to work through at once, longer than the DMA's
   ring lasts on the K6. Returns the move, Q16 counts. */
#if IFRX_TL * 2 != IFRX_HYP * 3
#error sweep() takes its step as 1.5 samples
#endif
IFRX_NOINLINE static int32_t sweep(ifrx_t *s) {
  int32_t m0 = (int32_t)(s->met[0] >> 5), m1 = (int32_t)(s->met[IFRX_MM(s) - 1] >> 5), step, h, par;
  uint32_t j;
  s->tot += (s->q & 1) ? m1 - m0 : m0 - m1;
  s->norm += (uint32_t)(m0 + m1);
  if (++s->hk < IFRX_HSYMS) return 0;
  s->fit[s->h] = s->tot;
  j = (uint32_t)iabs(s->tot);
  if (j > s->bfit) s->bfit = j, s->bnorm = s->norm, s->bh = s->h;
  s->tot = 0, s->norm = 0, s->hk = 0;
  step = (int32_t)(s->step_q16 * 3) >> 1; /* pc / IFRX_HYP */
  if (++s->h < IFRX_HYP) return step;
  s->h = 0;
  s->acq_tries++;
  s->acq_q = (int32_t)ifrx_udiv(s->bfit << 8, s->bnorm | 1);
  /* the fit against the offset is a plateau a third of a symbol wide (4FSK), and noise picks
     its best anywhere on it; the lead's timing is at its leading edge, and its late side is a
     side peak the timing loop holds. So back from the best while the fit keeps its sign and half
     its size (a step back past the first offset is the last a symbol earlier: the other sign).
     Over 8 runs each at -300 .. 300 Hz and two C/N0s, 4FSK missed 2 of 80 on the best, 0 so;
     a step later than the edge, 10 */
  par = s->fit[s->bh] < 0;
  for (h = s->bh;;) { /* ends by a lap: there the fit is the best's, less */
    int32_t v = s->fit[h ? h - 1 : IFRX_HYP - 1];
    if (((par ^ !h) ? -v : v) * 2 <= (int32_t)s->bfit) break;
    if (!h) h = IFRX_HYP, par ^= 1;
    h--;
  }
  s->bfit = 0;
#ifdef IFRX_TRACE
  IFRX_TRACE(s, h);
#endif
  j = (uint32_t)iabs(h - s->prev);
  s->prev = s->acq_q >= IFRX_ACQ_Q ? h : -9;
  if (s->prev >= 0 && (j <= 1 || j == IFRX_HYP - 1)) {
    s->state = IFRX_LEAD;
    if (IFRX_STATS) s->acq_off = h;
    s->parity = par ^ 1; /* a symbol on from where it was measured */
    s->nsym = 0;
    s->win_err = s->win_n = 0;
    if (IFRX_STATS) s->lead_err = 0;
    return (h + 1) * step;
  }
  return step;
}

/* the better of a kept template's two correlations: magnitude, and which in *cj */
IFRX_NOINLINE static uint32_t best2(const int32_t a[4], int *cj) {
  int32_t c[2];
  uint32_t m0, m1;
  cof(a, 0, c);
  m0 = approx_mag(c);
  cof(a, 1, c);
  m1 = approx_mag(c);
  *cj = m1 > m0;
  return m1 > m0 ? m1 : m0;
}

/* the symbol's samples are all in: decide, then move on to the next symbol */
IFRX_NOINLINE static void finish(ifrx_t *s) {
  int32_t a[4], X[6], Y[4], dt, im; /* X: early, late, on time; Y: the two halves */
  uint32_t d;
  int j, win, wcj, cj, tone, bt[4] = {0, 0, 0, 0}, bc[4] = {0, 0, 0, 0}, sh;
  int lead = s->state == IFRX_LEAD;
  const uint8_t *t;
  /* each template's sums over the whole symbol (the first half's were taken half way, for the
     AFC too); then each tone's best template (of its phases), and the best tone. Summed in
     registers at those two points, they cost under half what running sums a sample did */
  for (tone = 0; tone < IFRX_MM(s); tone++) s->met[tone] = 0;
  for (j = 0; j < IFRX_NS(s); j++) {
    uint32_t v;
    int32_t *h = s->A1[j], *f = s->A[j];
    t = s->tm + j * IFRX_TL;
    corr4((const int16_t (*)[2])(s->w + 1 + IFRX_TL / 2), t + IFRX_TL / 2, f, IFRX_TL / 2);
    for (cj = 0; cj < 4; cj++) f[cj] += h[cj];
    v = best2(s->A[j], &cj);
    tone = j >> IFRX_HSH(s);
    if (v >= s->met[tone]) s->met[tone] = v, bt[tone] = j, bc[tone] = cj;
  }
  for (tone = 0, j = 1; j < IFRX_MM(s); j++)
    if (s->met[j] > s->met[tone]) tone = j;
  s->sym = tone;
  if (s->state == IFRX_ACQ) {
    /* the carrier's offset, before the timing is known: FM of zero-mean audio keeps the IF's
       mean frequency on the carrier whatever the tones, so the mean turn between successive
       samples is the offset (a frequency-locked loop, 1/64 a symbol). Only until a sweep has
       seen the lead: the tones bias it (about -100 Hz on the model, through the filter's
       slope), and the decisions' AFC takes over once locked. Over 16 runs each at -300, -164,
       0, 164 and 300 Hz, 4FSK missed 23 without it, 14 running it throughout, 2 this way */
    int32_t fr = 0, fi = 0;
    for (j = 0; j <= IFRX_TL; j++) {
      int32_t ar = s->w[j][0], ai = s->w[j][1], br = s->w[j + 1][0], bi = s->w[j + 1][1];
      fr += (br * ar + bi * ai) >> 4;
      fi += (bi * ar - br * ai) >> 4;
    }
    if (fr > 0 && s->afc_sh && s->prev < 0) {
      while (fr > 0x3FFFF) fr >>= 1, fi >>= 1;
      fi = ifrx_sdiv(clampi(fi, fr) << 12, (uint32_t)fr); /* radians a sample, Q12 */
      s->dphi_f = clampi(s->dphi_f + ((fi * (int32_t)s->k_afc * 9) >> 1), s->dphi_max);
    }
    advance(s, (int32_t)s->pc_q16 + sweep(s));
    return;
  }
  s->nsym++;
  if (lead) {
    /* the lead's symbol is known: steer on its best template */
    tone = ((s->q + (uint32_t)s->parity) & 1) ? IFRX_MM(s) - 1 : 0;
    j = s->sym != tone;
    if (IFRX_STATS) s->lead_err += j;
    s->win_err += j;
    s->win_n++;
  }
  win = bt[tone], wcj = bc[tone];
  /* timing: early (samples -1..TL-2) and late (1..TL) on the winning template,
     (|L|^2 - |E|^2) / 2|C|^2 ~ (|L| - |E|) / |C| (squares: the approximate magnitude's error
     depends on the phase, which would bias the loop) */
  t = s->tm + win * IFRX_TL;
  corr4((const int16_t (*)[2])s->w, t, a, IFRX_TL);
  cof(a, wcj, X);
  corr4((const int16_t (*)[2])(s->w + 2), t, a, IFRX_TL);
  cof(a, wcj, X + 2);
  cof(s->A[win], wcj, X + 4);
  sh = sh_of(X, 6);
  d = sq(X + 4, sh) >> 11;
  dt = d ? ifrx_sdiv((int32_t)sq(X + 2, sh) - (int32_t)sq(X, sh), d) : 0; /* Q12 */
  dt = (clampi(dt, 4096) * (int32_t)s->pc_q8) >> 4;                          /* x pc, Q16 */
  dt >>= (lead && s->nsym < IFRX_LEAD_SYMS / 2) ? s->gain_sh - 2 : s->gain_sh;
  if (IFRX_STATS) s->terr = dt;
  /* the AFC: the turn between the winning template's two halves, Im(c2 c1*) / mean |c|^2 */
  cof(s->A1[win], wcj, Y);
  Y[2] = X[4] - Y[0];
  Y[3] = X[5] - Y[1];
  sh = sh_of(Y, 4) + 1;
  im = (Y[3] >> sh) * (Y[0] >> sh) - (Y[2] >> sh) * (Y[1] >> sh);
  d = (sq(Y, sh) + sq(Y + 2, sh)) >> 13;
  if (d && s->afc_sh)
    s->dphi_f = clampi(s->dphi_f + ifrx_sdiv(im, d) * (int32_t)(s->k_afc << (lead << 1)),
                       s->dphi_max);
  if (lead) {
    if (s->nsym >= IFRX_LEAD_SYMS) s->state = IFRX_TRACK;
    if (s->win_n >= IFRX_WIN) {
      /* a window of the lead that missed too often: the acquisition was wrong, sweep again
         (the first window strictly: a false start on a bare carrier misses half its symbols) */
      if (s->win_err > (s->nsym <= IFRX_WIN ? IFRX_WIN_BAD : IFRX_WIN / 2)) {
        s->state = IFRX_ACQ;
        s->prev = -9; /* (the sweep's sums are clear: it cleared them when it took the lead) */
        s->acq_rej++;
      }
      s->win_err = s->win_n = 0;
    }
  }
  advance(s, (int32_t)s->pc_q16 + dt);
}

IFRX_NOINLINE static void take(ifrx_t *s) {
  int16_t *w = s->w[s->i + 1];
  uint32_t th = s->th_tau + (uint32_t)((int32_t)(s->ni - s->tau) * (int32_t)(s->dphi_if + (uint32_t)s->dphi_f));
  sample(s, s->ni, th, w);
  if (s->i == IFRX_TL / 2 - 1) { /* the first half's sums now: the symbol's end has the rest */
    int j;
    for (j = 0; j < IFRX_NS(s); j++)
      corr4((const int16_t (*)[2])(s->w + 1), s->tm + j * IFRX_TL, s->A1[j], IFRX_TL / 2);
  }
}

IFRX_NOINLINE static void put(ifrx_t *s, int c) {
  int8_t *r = s->ring + (s->n & (IFRX_RING - 1));
  r[0] = r[IFRX_RING] = (int8_t)c;
}

int ifrx_push(ifrx_t *s, int c) {
  uint32_t last;
  int done = 0;
  put(s, c);
  last = s->n++;
  while ((int32_t)(last - s->ni) >= 0) {
    if (last - s->ni > IFRX_RING - IFRX_NT - 1) {
      /* this symbol's counts are gone (a skip): pass it over */
      if (IFRX_STATS) s->slips++;
      s->gap = 1;
      advance(s, (int32_t)s->pc_q16);
      continue;
    }
    take(s);
    if (s->i == IFRX_TL) {
      finish(s);
      done = s->state != IFRX_ACQ;
    } else {
      s->i++;
      s->ni = ni_of(s, s->i);
    }
  }
  return done;
}

void ifrx_skip(ifrx_t *s, uint32_t nskip) {
  /* zeros in place of the lost counts: the filter rings down rather than on stale ones */
  uint32_t k;
  for (k = 0; k < nskip && k < IFRX_RING; k++, s->n++) put(s, 0);
  s->n += nskip - k;
}

int ifrx_afc_hz(const ifrx_t *s) { return ifrx_sdiv(s->dphi_f, 50069u); }
