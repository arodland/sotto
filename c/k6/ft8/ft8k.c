/* ft8k: a small fixed-point FT8 receiver for the K6's Cortex-M0. See ft8k.h and
   docs/k6-ft8-wspr.md. Everything here is integer, divide-free on the hot paths (fk_udiv where
   a quotient is unavoidable), and keeps no state outside fk_t and the caller's buffers, so it
   builds into a position-independent K6 RAM module as well as on the host.
   SPDX-License-Identifier: MIT */
#include "ft8k.h"

#include "ft8k_tables.h"
#ifdef FK_HOST_DEBUG
#include <stdio.h>
#endif

#define NN 79
/* the first CIC: k->r1 (16) and k->sh1 (16): x cos (Q12) and the CIC's 16^3 = 2^12, keep about
   15 bits */
#define SH2 15 /* CIC r2^3, r2 26..28: about 2^14.3 */

static const uint8_t COSTAS[7] = {3, 1, 4, 0, 6, 5, 2};
static const uint8_t GRAY[8] = {0, 1, 3, 2, 5, 6, 4, 7};
/* cos(2 pi i / 64) x 4096: the mixer and the DFT */
static const int16_t COS64[64] = {
    4096, 4076, 4017, 3920, 3784, 3612, 3406, 3166, 2896, 2598, 2276, 1931,
    1567, 1189, 799, 401, 0, -401, -799, -1189, -1567, -1931, -2276, -2598,
    -2896, -3166, -3406, -3612, -3784, -3920, -4017, -4076, -4096, -4076, -4017, -3920,
    -3784, -3612, -3406, -3166, -2896, -2598, -2276, -1931, -1567, -1189, -799, -401,
    0, 401, 799, 1189, 1567, 1931, 2276, 2598, 2896, 3166, 3406, 3612,
    3784, 3920, 4017, 4076,
};
/* Hann over 32: sin^2(pi i / 32) x 256 */
static const uint8_t HANN32[32] = {0,   2,   10,  22,  37,  57,  79,  103, 128, 153, 177,
                                   199, 219, 234, 246, 254, 255, 254, 246, 234, 219, 199,
                                   177, 153, 128, 103, 79,  57,  37,  22,  10,  2};
/* the second CIC's droop (order 3, decimating to the DFT's rate): 60 log10 |sinc(d / 64)| at d
   half bins from the centre, in log2 power Q4, added back to each bin */
static const uint8_t DROOP[33] = {0,  0,  0,  1,  1,  1,  2,  3,  4,  5,  6,  7,  8,  10, 11, 13, 15,
                                  16, 19, 21, 23, 25, 28, 31, 34, 37, 40, 43, 47, 50, 54, 58, 63};
/* log2(1 + i/16) x 16 */
static const uint8_t LOG2F[16] = {0, 1, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 14, 15};

uint32_t fk_udiv(uint32_t n, uint32_t d) {
  uint32_t q = 0, bit = 1;
  if (!d) return 0;
  while (d < n && !(d & 0x80000000u)) d <<= 1, bit <<= 1;
  while (bit) {
    if (n >= d) n -= d, q |= bit;
    d >>= 1, bit >>= 1;
  }
  return q;
}

/* log2(v) in 1/16 octaves (v > 0), without CLZ (the M0 has none) */
static int log2q4(uint32_t v) {
  int p = 0;
  if (v >> 16) v >>= 16, p += 16;
  if (v >> 8) v >>= 8, p += 8;
  if (v >> 4) v >>= 4, p += 4;
  if (v >> 2) v >>= 2, p += 2;
  if (v >> 1) p += 1;
  return p * 16;
}

static int log2q4f(uint32_t v) {
  int p = log2q4(v) >> 4;
  uint32_t m = p >= 4 ? v >> (p - 4) : v << (4 - p); /* 16 .. 31 */
  return p * 16 + LOG2F[m & 15];
}

void fk_init(fk_t *k, int nb, int r2, uint8_t *wf, int16_t *sc, void *work) {
  uint8_t *z = (uint8_t *)k;
  unsigned i;
  for (i = 0; i < sizeof *k; i++) z[i] = 0;
  if (wf) /* null: a front end only (fk_rowq) */
    for (i = 0; i < (unsigned)FK_WF_BYTES(nb); i++) wf[i] = 0;
  if (sc)
    for (i = 0; i < (unsigned)FK_SC_WORDS(nb); i++) sc[i] = -32000;
  k->nb = (uint8_t)nb;
  k->r2 = (uint8_t)r2;
  k->r1 = 16;
  k->sh1 = 16;
  k->qs = 3;     /* one waterfall step = 8/16 octave of power = 1.5 dB */
  k->thr = 20;   /* 1.25 steps a neighbour (in 1/16 step): strong signals score low, as
                    their skirts fill the neighbours */
  k->hann = 0;
  k->fosr = 1;
  k->tosr = 1;
  k->wf = wf;
  k->sc = sc;
  k->work = work;
}

static void push(fk_t *k, int i, int q) {
  k->si[k->sp] = (int16_t)i;
  k->sq[k->sp] = (int16_t)q;
  k->sp = (uint8_t)((k->sp + 1) & 31);
  if (k->nsamp < 32) k->nsamp++;
}

void fk_sample(fk_t *k, int i, int q) { push(k, i, q); }

void fk_in(fk_t *k, int x, uint32_t phase) {
  uint32_t a = phase >> 26;
  int c = COS64[a], s = COS64[(a + 48) & 63]; /* cos(t - pi/2) = sin t; Q12 */
  uint32_t *i1 = (uint32_t *)k->i1;
  /* x e^{-jt}: I = x cos, Q = -x sin. Wrapping uint32 arithmetic: a CIC only needs its output
     to fit. */
  i1[0] += (uint32_t)(x * c), i1[1] += i1[0], i1[2] += i1[1];
  i1[3] += (uint32_t)(-x * s), i1[4] += i1[3], i1[5] += i1[4];
  if (++k->n1 < k->r1) return;
  k->n1 = 0;
  {
    uint32_t *c1 = (uint32_t *)k->c1, *i2 = (uint32_t *)k->i2, *c2 = (uint32_t *)k->c2;
    uint32_t y[2], t;
    int ch;
    for (ch = 0; ch < 2; ch++) {
      uint32_t v = i1[3 * ch + 2];
      t = v - c1[3 * ch], c1[3 * ch] = v, v = t;
      t = v - c1[3 * ch + 1], c1[3 * ch + 1] = v, v = t;
      t = v - c1[3 * ch + 2], c1[3 * ch + 2] = v, v = t;
      y[ch] = (uint32_t)((int32_t)v >> k->sh1);
      i2[3 * ch] += y[ch], i2[3 * ch + 1] += i2[3 * ch], i2[3 * ch + 2] += i2[3 * ch + 1];
    }
    if (++k->n2 < k->r2) return;
    k->n2 = 0;
    for (ch = 0; ch < 2; ch++) {
      uint32_t v = i2[3 * ch + 2];
      t = v - c2[3 * ch], c2[3 * ch] = v, v = t;
      t = v - c2[3 * ch + 1], c2[3 * ch + 1] = v, v = t;
      t = v - c2[3 * ch + 2], c2[3 * ch + 2] = v, v = t;
      y[ch] = (uint32_t)((int32_t)v >> SH2);
    }
    push(k, (int)(int32_t)y[0], (int)(int32_t)y[1]);
  }
}

/* the waterfall: FK_ROWS rows of nb 4-bit values */
static int wget(const fk_t *k, unsigned slot, int bin) {
  uint8_t b = k->wf[slot * (k->nb >> 1) + (bin >> 1)];
  return bin & 1 ? b >> 4 : b & 15;
}

/* s + d in the ring (d < FK_ROWS; one compare, not a loop, which clang would make a modulo) */
static unsigned slot_add(unsigned s, unsigned d) {
  s += d;
  return s >= FK_ROWS ? s - FK_ROWS : s;
}

/* the row's 32-point DFT, as log2 power (Q4) of the window's nb bins into u */
/* log2 power (Q4) of one DFT bin: phase step m of 64 a sample (2 per whole bin) */
static int dft_bin(const int32_t *xr, const int32_t *xi, int m, int sh) {
  int32_t ar = 0, ai = 0;
  uint32_t pr, pi, p;
  int n, idx = 0, e = 0;
  for (n = 0; n < 32; n++) {
    int c = COS64[idx], s = COS64[(idx + 48) & 63];
    int32_t r = xr[n] >> sh, im = xi[n] >> sh;
    ar += r * c + im * s; /* (r + j im)(c - j s) */
    ai += im * c - r * s;
    idx = (idx + m) & 63;
  }
  if (ar < 0) ar = -ar;
  if (ai < 0) ai = -ai;
  /* |X|^2 in 31 bits: drop to 15 bits each first */
  while ((ar >> e) >= 32768 || (ai >> e) >= 32768) e++;
  pr = (uint32_t)(ar >> e), pi = (uint32_t)(ai >> e);
  p = pr * pr + pi * pi;
  return (p ? log2q4f(p) : 0) + 32 * (e + sh);
}

/* the row's 32-point DFT, as log2 power (Q4) of the window's nb bins into u. With fosr, each
   bin keeps the larger of itself and the half bin above it, which wins back most of the
   scalloping loss of a tone between bins for about 1 dB more noise */
static void dft_row(fk_t *k, int *u) {
  int32_t xr[32], xi[32], mx = 0;
  int n, j, sh = 0;
  int nb = k->nb;
  for (n = 0; n < 32; n++) {
    int w = k->hann ? HANN32[n] : 256, at = (k->sp + n) & 31;
    int32_t a, b;
    xr[n] = (k->si[at] * w) >> 4; /* |s| < 2^13: < 2^17 */
    xi[n] = (k->sq[at] * w) >> 4;
    a = xr[n] < 0 ? -xr[n] : xr[n], b = xi[n] < 0 ? -xi[n] : xi[n];
    if (a > mx) mx = a;
    if (b > mx) mx = b;
  }
  /* block floating point: scale so 32 x |x| x 4096 stays in 31 bits */
  while ((mx >> sh) >= 8192) sh++;
  for (j = 0; j < nb; j++) {
    int m = (2 * (j - (nb >> 1))) & 63;
    int d = 2 * (j - (nb >> 1));
    u[j] = dft_bin(xr, xi, m, sh);
    if (k->r2) u[j] += DROOP[d < 0 ? -d : d];
    if (k->fosr) {
      int h = dft_bin(xr, xi, (m + 1) & 63, sh);
      if (k->r2) h += DROOP[d + 1 < 0 ? -d - 1 : d + 1];
      if (h > u[j]) u[j] = h;
    }
  }
}

/* Costas score of a start slot s at bin f, raw (the sum of differences, in steps) */
static int sync_raw(const fk_t *k, unsigned s, int f) {
  int m, kk, sc = 0;
  for (m = 0; m < 3; m++) {
    unsigned base = slot_add(s, 72u * (unsigned)m);
    for (kk = 0; kk < 7; kk++) {
      unsigned at = slot_add(base, 2u * (unsigned)kk);
      int t = COSTAS[kk], a = wget(k, at, f + t);
      if (t > 0) sc += a - wget(k, at, f + t - 1);
      if (t < 7) sc += a - wget(k, at, f + t + 1);
      if (kk > 0) sc += a - wget(k, slot_add(at, FK_ROWS - 2), f + t);
      if (kk < 6) sc += a - wget(k, slot_add(at, 2), f + t);
    }
  }
  return sc;
}

/* neighbours a score sums: per Costas symbol, (t > 0) + (t < 7) + (k > 0) + (k < 6) */
static int sync_num(void) {
  int kk, n = 0;
  for (kk = 0; kk < 7; kk++) n += (COSTAS[kk] > 0) + (COSTAS[kk] < 7) + (kk > 0) + (kk < 6);
  return 3 * n;
}

int fk_crc_ok(const uint8_t bits[91]) {
  unsigned r = 0, i;
  for (i = 0; i < 82; i++) {
    unsigned b = i < 77 ? bits[i] : 0, top = (r >> 13) & 1;
    r = (r << 1) & 0x3FFF;
    if (top ^ b) r ^= 0x2757;
  }
  for (i = 0; i < 14; i++)
    if (((r >> (13 - i)) & 1) != bits[77 + i]) return 0;
  return 1;
}

/* layered normalised min-sum (x 7/8). llr > 0: a 1 (inside, L > 0 is a 0, the convention the
   min-sum sign rule needs). Returns iterations, or -1 */
int fk_ldpc(int16_t llr[174], uint8_t bits[174], void *work, int iters) {
  int16_t *L = (int16_t *)work;
  int8_t *R = (int8_t *)work + 2 * 174;
  int it, m, j;
  for (j = 0; j < 174; j++) L[j] = (int16_t)-llr[j]; /* llr may be L itself */
  for (j = 0; j < 83 * 7; j++) R[j] = 0;
  for (it = 0; it <= iters; it++) {
    int bad = 0;
    for (j = 0; j < 174; j++) bits[j] = L[j] < 0;
    for (m = 0; m < 83; m++) {
      int p = 0;
      for (j = 0; j < 7 && FK_NM[m][j]; j++) p ^= bits[FK_NM[m][j] - 1];
      bad += p;
    }
    if (!bad) return it;
    if (it == iters) break;
    for (m = 0; m < 83; m++) {
      int q[7], min1 = 32767, min2 = 32767, at = -1, sgn = 0, n = 0;
      int8_t *r = R + 7 * m;
      for (j = 0; j < 7 && FK_NM[m][j]; j++, n++) {
        int v = L[FK_NM[m][j] - 1] - r[j], a = v < 0 ? -v : v;
        q[j] = v;
        sgn ^= v < 0;
        if (a < min1) min2 = min1, min1 = a, at = j;
        else if (a < min2) min2 = a;
      }
      for (j = 0; j < n; j++) {
        int mag = (j == at ? min2 : min1), v;
        mag = (mag * 7) >> 3;
        if (mag > 127) mag = 127;
        v = (sgn ^ (q[j] < 0)) ? -mag : mag;
        r[j] = (int8_t)v;
        v += q[j];
        if (v > 32000) v = 32000;
        if (v < -32000) v = -32000;
        L[FK_NM[m][j] - 1] = (int16_t)v;
      }
    }
  }
  return -1;
}

static int sdiv(int n, int d) {
  return n < 0 ? -(int)fk_udiv((uint32_t)-n, (uint32_t)d) : (int)fk_udiv((uint32_t)n, (uint32_t)d);
}

static int max4(int a, int b, int c, int d) {
  if (b > a) a = b;
  if (d > c) c = d;
  return a > c ? a : c;
}

/* decode the candidate at start slot s, bin f; 1 if it checks */
static int try_decode(fk_t *k, unsigned s, int f, fk_dec_t *out) {
  int16_t *llr = (int16_t *)k->work; /* fk_ldpc works in place */
  uint8_t bits[174];
  int sym, i, it, sig = 0;
  for (sym = 0, i = 0; sym < NN; sym++) {
    unsigned at = slot_add(s, 2u * (unsigned)sym);
    int v[8], j;
    if (sym < 7 || (sym >= 36 && sym < 43) || sym >= 72) {
      int c = sym < 7 ? sym : sym < 43 ? sym - 36 : sym - 72;
      sig += wget(k, at, f + COSTAS[c]);
      continue;
    }
    for (j = 0; j < 8; j++) v[j] = wget(k, at, f + GRAY[j]);
    /* max-log, x 4 for the LDPC's integer resolution */
    llr[i++] = (int16_t)(4 * (max4(v[4], v[5], v[6], v[7]) - max4(v[0], v[1], v[2], v[3])));
    llr[i++] = (int16_t)(4 * (max4(v[2], v[3], v[6], v[7]) - max4(v[0], v[1], v[4], v[5])));
    llr[i++] = (int16_t)(4 * (max4(v[1], v[3], v[5], v[7]) - max4(v[0], v[2], v[4], v[6])));
  }
  it = fk_ldpc(llr, bits, k->work, 30);
#ifdef FK_HOST_DEBUG
  {
    int h = 0;
    for (i = 0; i < 174; i++) h += llr[i] > 0;
    fprintf(stderr, "try row=%u f=%d it=%d crc=%d ones=%d llr0..9", (unsigned)k->nrow, f, it,
            it >= 0 ? fk_crc_ok(bits) : -1, h);
    for (i = 0; i < 174; i++) fprintf(stderr, " %d", llr[i]);
    fprintf(stderr, "\n");
  }
#endif
  if (it < 0 || !fk_crc_ok(bits)) return 0;
  for (i = 0; i < 91 && !bits[i]; i++) {
  }
  if (i == 91) return 0; /* all zeros: the LDPC's trivial answer to no signal */
  for (i = 0; i < 10; i++) out->msg[i] = 0;
  for (i = 0; i < 77; i++) out->msg[i >> 3] |= (uint8_t)(bits[i] << (7 - (i & 7)));
  out->iters = (uint16_t)it;
  out->bin = (int16_t)f;
  /* SNR: the Costas tones' mean step over the noise's (the floor sits at step 4, its mean log
     ~2.5 dB under the mean power), 1.5 dB a step, less 10 log10(2500 / 6.25) = 26 dB */
  out->snr = (int16_t)(sdiv(sig - 84, 14) - 26 - 2);
  return 1;
}

static int seen(fk_t *k, const uint8_t *msg, uint32_t row) {
  int i, j;
  for (i = 0; i < k->nlast; i++) {
    int d = (int)(row - k->last_row[i]);
    if (d < -8 || d > 8) continue;
    for (j = 0; j < 10 && k->last_msg[i][j] == msg[j]; j++) {
    }
    if (j == 10) return 1;
  }
  return 0;
}

static void remember(fk_t *k, const uint8_t *msg, uint32_t row) {
  int i, at = k->nlast < 4 ? k->nlast++ : 0;
  if (at == 0 && k->nlast == 4) {
    for (i = 0; i < 3; i++) {
      int j;
      k->last_row[i] = k->last_row[i + 1];
      for (j = 0; j < 10; j++) k->last_msg[i][j] = k->last_msg[i + 1][j];
    }
    at = 3;
  }
  k->last_row[at] = row;
  for (i = 0; i < 10; i++) k->last_msg[at][i] = msg[i];
}

/* a bin's 4-bit value: the floor at 4, then 1.5 dB steps to 9 dB over it (10), then 6 dB
   steps to about 39 dB (15), so strong signals keep their shape */
static int quant(const fk_t *k, int u) {
  int step = 1 << k->qs, e = (u - (int)(k->flo >> 4) + 4 * step + (step >> 1)) >> k->qs;
  if (e <= 10) return e < 0 ? 0 : e;
  e = 10 + ((e - 8) >> 2);
  return e > 15 ? 15 : e;
}

int fk_rowq(fk_t *k, uint8_t *row) {
  int u[FK_NBMAX], j, mean = 0, nb = k->nb;
  if (k->nsamp < 32) return 0;
  dft_row(k, u);
  if (k->tosr) {
    /* called every 40 ms: the odd call keeps its DFT, the even one makes the row from the
       larger of the two, so a symbol that starts between rows still finds a window */
    if (!(k->half ^= 1)) {
      for (j = 0; j < nb; j++) k->uh[j] = (int16_t)u[j];
      return 0;
    }
    for (j = 0; j < nb; j++)
      if (k->uh[j] > u[j]) u[j] = k->uh[j];
  }
  /* the floor: the row's mean log power (a few strong bins barely move a mean of logs) */
  for (j = 0; j < nb; j++) mean += u[j];
  mean = (int)fk_udiv((uint32_t)mean, (uint32_t)nb) << 4;
  if (!k->nq++) k->flo = mean;
  else k->flo += (mean - k->flo) >> 3;
  for (j = 0; j < nb; j += 2) row[j >> 1] = (uint8_t)(quant(k, u[j]) | (quant(k, u[j + 1]) << 4));
  return 1;
}

int fk_row(fk_t *k) {
  uint8_t row[FK_NBMAX / 2];
  k->ndec = 0;
  return fk_rowq(k, row) ? fk_putrow(k, row) : 0;
}

int fk_putrow(fk_t *k, const uint8_t *in) {
  int j, nb = k->nb, nf = nb - 7;
  int16_t *s0 = k->sc, *s1 = k->sc + nf, *s2 = k->sc + 2 * nf;
  uint8_t *row = k->wf + k->slot * (nb >> 1);
  k->ndec = 0;
  for (j = 0; j < nb >> 1; j++) row[j] = in[j];
  k->slot = (uint16_t)slot_add(k->slot, 1);
  k->nrow++;
  /* scores: s0 for the start two rows back, s1 one row back, s2 the start whose last Costas
     row this is (156 rows back) */
  for (j = 0; j < nf; j++) s0[j] = s1[j], s1[j] = s2[j], s2[j] = 0;
  if (k->nrow >= 157) {
    unsigned st = slot_add(k->slot, FK_ROWS - 157);
    int num = sync_num();
    for (j = 0; j < nf; j++) {
      int raw = sync_raw(k, st, j);
      s2[j] = raw > 0 ? (int16_t)fk_udiv((uint32_t)raw << 4, (uint32_t)num) : 0;
    }
  }
  /* the previous start (s1): a local maximum over time and bins, above the threshold */
  if (k->nrow >= 158) {
    unsigned st = slot_add(k->slot, FK_ROWS - 158);
    int tries = 0;
    while (tries < FK_MAXDEC) {
      int best = -1, bs = k->thr - 1;
      for (j = 0; j < nf; j++) {
        int v = s1[j];
        if (v <= bs || v < s0[j] || v < s2[j]) continue;
        if (j > 0 && v < s1[j - 1]) continue;
        if (j + 1 < nf && v < s1[j + 1]) continue;
        best = j, bs = v;
      }
      if (best < 0) break;
      tries++;
      {
        fk_dec_t *d = &k->dec[k->ndec];
        if (try_decode(k, st, best, d)) {
          d->score = (int16_t)bs;
          d->row = k->nrow - 158;
          if (!seen(k, d->msg, d->row)) {
            remember(k, d->msg, d->row);
            k->ndec++;
          }
        }
      }
      s1[best] = (int16_t)-s1[best]; /* not again */
    }
    for (j = 0; j < nf; j++)
      if (s1[j] < 0) s1[j] = (int16_t)-s1[j];
  }
  return k->ndec;
}

/* text */
static char *puts_(char *p, const char *s) {
  while (*s) *p++ = *s++;
  return p;
}

static const char A37[] = " 0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ";

static char *call28(char *p, uint32_t n, int suffix) {
  char c[6];
  int i;
  if (n < 2063592u) {
    if (n == 0) return puts_(p, "DE");
    if (n == 1) return puts_(p, "QRZ");
    if (n == 2) return puts_(p, "CQ");
    p = puts_(p, "CQ ");
    if (n <= 1002) { /* CQ nnn */
      uint32_t v = n - 3, h = fk_udiv(v, 100), t = fk_udiv(v - 100 * h, 10);
      *p++ = (char)('0' + h), *p++ = (char)('0' + t), *p++ = (char)('0' + v - 100 * h - 10 * t);
      return p;
    }
    if (n <= 532443u) { /* CQ ABCD */
      uint32_t v = n - 1003;
      for (i = 3; i >= 0; i--) {
        uint32_t q = fk_udiv(v, 27);
        c[i] = (char)(v - q * 27 ? 'A' + v - q * 27 - 1 : ' ');
        v = q;
      }
      for (i = 0; i < 4; i++)
        if (c[i] != ' ') *p++ = c[i];
      return p;
    }
    return p;
  }
  n -= 2063592u;
  if (n < 4194304u) return puts_(p, "<...>");
  n -= 4194304u;
  for (i = 5; i >= 3; i--) {
    uint32_t q = fk_udiv(n, 27);
    uint32_t r = n - q * 27;
    c[i] = r ? (char)('A' + r - 1) : ' ';
    n = q;
  }
  {
    uint32_t q = fk_udiv(n, 10);
    c[2] = (char)('0' + (n - q * 10));
    n = q;
    q = fk_udiv(n, 36);
    c[1] = A37[1 + (n - q * 36)];
    n = q;
    c[0] = n < 37 ? A37[n] : '?';
  }
  for (i = 0; i < 6; i++)
    if (c[i] != ' ') *p++ = c[i];
  if (suffix) p = puts_(p, "/R");
  return p;
}

static char *dec2(char *p, int v) {
  *p++ = v < 0 ? '-' : '+';
  if (v < 0) v = -v;
  *p++ = (char)('0' + fk_udiv((uint32_t)v, 10));
  *p++ = (char)('0' + (v - 10 * (int)fk_udiv((uint32_t)v, 10)));
  return p;
}

static uint32_t field(const uint8_t *m, int at, int n) {
  uint32_t v = 0;
  int i;
  for (i = 0; i < n; i++, at++) v = (v << 1) | ((m[at >> 3] >> (7 - (at & 7))) & 1);
  return v;
}

char *fk_text(char *p, const uint8_t msg[10]) {
  uint32_t i3 = field(msg, 74, 3);
  if (i3 == 1 || i3 == 2) {
    uint32_t g = field(msg, 59, 15), ir = field(msg, 58, 1);
    p = call28(p, field(msg, 0, 28), (int)field(msg, 28, 1) && i3 == 1);
    *p++ = ' ';
    p = call28(p, field(msg, 29, 28), (int)field(msg, 57, 1) && i3 == 1);
    if (g <= 32400) {
      uint32_t a = fk_udiv(g, 1800), b = fk_udiv(g - a * 1800, 100), d = g - a * 1800 - b * 100;
      *p++ = ' ';
      if (ir) p = puts_(p, "R ");
      *p++ = (char)('A' + a);
      *p++ = (char)('A' + b);
      *p++ = (char)('0' + fk_udiv(d, 10));
      *p++ = (char)('0' + d - 10 * fk_udiv(d, 10));
    } else {
      int r = (int)(g - 32400);
      if (r == 2) p = puts_(p, " RRR");
      else if (r == 3) p = puts_(p, " RR73");
      else if (r == 4) p = puts_(p, " 73");
      else if (r > 4) {
        *p++ = ' ';
        if (ir) *p++ = 'R';
        p = dec2(p, r - 35);
      }
    }
  } else {
    int i;
    for (i = 0; i < 10; i++) {
      int h = msg[i] >> 4, l = msg[i] & 15;
      *p++ = (char)(h < 10 ? '0' + h : 'a' + h - 10);
      *p++ = (char)(l < 10 ? '0' + l : 'a' + l - 10);
    }
  }
  *p = 0;
  return p;
}
