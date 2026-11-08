/* dmrsync.h: a DMR sync-word detector on the K6's GP0 receive IF (docs/k6-dmr.md). The integer
   version of sim/k6if/dmr.py's detector, for a RAM module (c/k6/mod/dmrdet.c) and the host
   check (c/k6/dmrsync_host.c): header-only, no statics, no division but ds_udiv, no 64-bit.

     counts (2 * ones - 64 at the DMA's rate / 64, ~85.8 kHz)
       -> a band-pass around the IF (a 2nd-order Butterworth biquad, 10.4-23.4 kHz at
          85.8 kHz, Q14 coefficients, out in Q8 counts): the sigma-delta's own noise above the
          band otherwise makes extra crossings (on the bench, real DMR at -86 dBm: |rho| 0.7-0.8
          from bare crossings, 0.95-0.99 band-passed; on the model it buys ~3 dB)
       -> rising zero crossings, interpolated (Q16 counts)
       -> the IF's phase (Q12 cycles) at a grid of 9600 points a second (2 a DMR symbol), the
          grid's step in counts set from the DMA's measured rate (ds_rate)
       -> y = the phase's step, less the nominal IF's (26 MHz / 1536 / 9600 cycles), Q11,
          clamped: the frequency, 4.6875 Hz an LSB
       -> over the last 24 symbols (every other grid value): s1 = sum y, s2 = sum y^2,
          D = 24 s2 - s1^2 (576 x the variance). A window whose spread is outside
          DS_SD_LO..DS_SD_HI Hz rms (a bare carrier, squelch noise) is skipped
       -> for each family (BS, MS, direct TS1, direct TS2), c = sum p_i y_i with p = the
          voice sync's signs: 2 x (the sum over its 12 plus positions) - s1. rho^2 = c^2 / D;
          c > 0 voice sync, c < 0 data sync (data = voice negated)
       -> a hit: rho^2 >= thr^2 and the best within +-2 grid points
       -> confirmed: a hit whose family had another a whole number (1-24) of 30 ms slots
          earlier, +- 2 grid points + 1/256 of the gap

   SPDX-License-Identifier: MIT */
#ifndef DMRSYNC_H
#define DMRSYNC_H
#include <stdint.h>

#define DS_GRID 9600u           /* grid points a second */
#define DS_SLOT 288u            /* grid points a 30 ms slot */
#ifndef DS_RING
#define DS_RING 64u             /* y history (the taps reach 46 back; a power of 2) */
#endif
#define DS_NHIST 3u             /* hits kept per family for the confirmation */
#define DS_MAXSLOTS 24u
#define DS_Y0 7222              /* 16927.083 Hz / 9600 in Q12 cycles */
#define DS_YMAX 2047
/* the spread gate, Q11 units (4.6875 Hz): D = 576 var. 1300 Hz -> 277.3, 2600 -> 554.7 */
#define DS_D_LO 44300000u
#define DS_D_HI 177200000u
/* the band-pass: y = (B0 (x - x2) 256 - A1 y1 - A2 y2) >> 14, scipy's butter(1, [FIF -+ 6500],
   'bandpass', fs=85.8 kHz) = b0 0.3402, a1 -0.4824, a2 0.3196 */
#define DS_BP_B0 5574
#define DS_BP_A1 (-7904)
#define DS_BP_A2 5235
#ifdef DS_FRING
/* the decoder's matched filter (DMR_DECODE builds, c/k6/mod/dmrdec.inc): a 13-tap RRC (alpha
   0.2) at 2 points a symbol, Q14, centre tap first; its output for grid point g, a quarter of
   y's scale in 8 bits, is kept in f[] (DS_FRING of them, a power of 2) once y[g + 6] is in */
static const int16_t DS_RRC[7] = {8547, 5082, -426, -1497, 378, 688, -306};
#endif
#ifndef DS_THR_K16
#define DS_THR_K16 41943u       /* thr^2 in Q16: thr 0.80 */
#endif

/* the voice sync words' 24 symbols as signs, bit i = symbol i (the oldest first) is +3 */
#define DS_NFAM 4
static const uint32_t DS_PAT[DS_NFAM] = {
    /* computed from TS 102 361-1 table 9.2 by sim/k6if/dmr.py: bs 755FD7DF75F7,
       ms 7F7D5DD57DFD, ts1 5D577F7757FF, ts2 7DFFD5F55D5F */
    0x004D263Du, 0x0089EB91u, 0x0007517Bu, 0x003BCE09u};

enum { DS_NONE = 0, DS_HIT = 1, DS_CONF = 2 };

typedef struct {
  /* the band-pass's last two counts in and out (out in Q8) */
  int32_t x1, x2, y1, y2;
  /* crossings */
  int32_t prev;                 /* the last band-passed count, Q8 */
  uint32_t n;                   /* counts so far (Q16 times are n << 16, wrapping) */
  uint32_t tl;                  /* the last crossing, Q16 counts */
  uint32_t k;                   /* crossings so far (whole cycles, Q12 below) */
  int32_t have;                 /* crossings since the last gap */
  /* the grid */
  uint32_t g, w16;              /* the next grid point, Q16 counts; the step */
  uint32_t phl;                 /* the phase at the last grid point, Q12 cycles */
  int32_t okg;                  /* grid points since the last gap */
  uint32_t j;                   /* grid points so far */
  int16_t y[DS_RING];
#ifdef DS_FRING
  int8_t *f;                    /* DS_FRING of them, the caller's (a module: its scratch) */
#endif
  int32_t s1[2];
  uint32_t s2[2];
  /* the candidate hit */
  uint32_t cj, cr2;             /* grid index; rho^2 Q8 */
  int32_t cfam;                 /* family, -1 none; */
  int32_t csign;                /* +1 voice, -1 data */
  /* the last hit out (ds_push's DS_HIT / DS_CONF) */
  uint32_t hj, hr2;
  int32_t hfam, hsign;
  uint32_t last[DS_NFAM][DS_NHIST];
  uint8_t nlast[DS_NFAM];
} ds_t;

__attribute__((noinline)) static uint32_t ds_udiv(uint32_t n, uint32_t d) {
  uint32_t q = 0, bit = 1;
  if (!d) return 0;
  while (d < n && !(d & 0x80000000u)) d <<= 1, bit <<= 1;
  while (bit) {
    if (n >= d) n -= d, q |= bit;
    d >>= 1, bit >>= 1;
  }
  return q;
}

/* the grid's step from the DMA's rate: cps8 = 96 MHz cycles a transfer, Q8. One count is 64
   transfers, one grid step 1/9600 s: 96e6 * 256 / (64 * 9600) = 40000 */
static void ds_rate(ds_t *d, uint32_t cps8) { d->w16 = ds_udiv(40000u * 65536u / 16u, cps8) << 4; }

__attribute__((unused)) static void ds_init(ds_t *d, uint32_t cps8) {
  uint8_t *p = (uint8_t *)d;
  uint32_t i;
  for (i = 0; i < sizeof *d; i++) p[i] = 0;
  d->cfam = -1;
  ds_rate(d, cps8);
}

/* the counts stopped (a ring overrun, a pause): start the phase again */
static void ds_gap(ds_t *d) { d->have = 0, d->okg = 0; }

#ifdef DS_FRING
/* ... after n counts that never came: the decoder's builds keep the grid's index on time across
   pauses (bursts are predicted by it), so the grid steps over them unfed */
__attribute__((unused)) static void ds_skip(ds_t *d, uint32_t n) { d->n += n; }
#endif

static int32_t ds_clamp(int32_t v) { return v > DS_YMAX ? DS_YMAX : v < -DS_YMAX ? -DS_YMAX : v; }

/* a confirmed DMR hit? (and keep it in its family's history) */
static int ds_confirm(ds_t *d, uint32_t j, int32_t f) {
  uint32_t i, n = d->nlast[f], ok = 0, keep = 0;
  uint32_t *h = d->last[f];
  for (i = 0; i < n; i++) {
    uint32_t dj = j - h[i], m, tol;
    if (dj > DS_MAXSLOTS * DS_SLOT + 64u) continue;           /* too old: dropped */
    h[keep++] = h[i];
    m = ds_udiv(dj + DS_SLOT / 2, DS_SLOT);
    tol = 2u + (dj >> 8);
    if (m >= 1 && m <= DS_MAXSLOTS) {
      int32_t e = (int32_t)dj - (int32_t)(m * DS_SLOT);
      if (e <= (int32_t)tol && -e <= (int32_t)tol) ok = 1;
    }
  }
  if (keep == DS_NHIST) {                                     /* full: drop the oldest */
    for (i = 1; i < DS_NHIST; i++) h[i - 1] = h[i];
    keep--;
  }
  h[keep++] = j;
  d->nlast[f] = (uint8_t)keep;
  return ok;
}

/* one grid value; returns DS_HIT or DS_CONF when a hit comes out (in d->h*) */
__attribute__((noinline)) static int ds_grid(ds_t *d, int32_t y) {
  uint32_t j = d->j++, par = j & 1, old = (j - 48u) & (DS_RING - 1);
  /* after a gap the sums start over and fill for 48 grid points before anything leaves */
  int32_t yo = d->okg >= 48 ? d->y[old] : 0, f, ret = DS_NONE;
  d->y[j & (DS_RING - 1)] = (int16_t)y;
#ifdef DS_FRING
  {
    int32_t a = DS_RRC[0] * d->y[(j - 6u) & (DS_RING - 1)];
    uint32_t t;
    for (t = 1; t < 7; t++)
      a += DS_RRC[t] * (d->y[(j - 6u - t) & (DS_RING - 1)] + d->y[(j - 6u + t) & (DS_RING - 1)]);
    a >>= 16;                                               /* / 4: +-3 symbols ~ +-104 */
    d->f[(j - 6u) & (DS_FRING - 1)] = (int8_t)(a > 127 ? 127 : a < -127 ? -127 : a);
  }
#endif
  if (d->okg < 2) d->s1[par] = 0, d->s2[par] = 0;
  d->okg++;
  d->s1[par] += y - yo;
  d->s2[par] += (uint32_t)(y * y) - (uint32_t)(yo * yo);
  /* the candidate's neighbourhood is over: out it goes */
  if (d->cfam >= 0 && j - d->cj > 2u) {
    d->hj = d->cj, d->hr2 = d->cr2, d->hfam = d->cfam, d->hsign = d->csign;
    d->cfam = -1;
    ret = ds_confirm(d, d->hj, d->hfam) ? DS_CONF : DS_HIT;
  }
  if (d->okg >= 48) {
    int32_t s1 = d->s1[par];
    uint32_t a1 = (uint32_t)(s1 < 0 ? -s1 : s1);
    uint32_t D = 24u * d->s2[par] - a1 * a1;
    if (D >= DS_D_LO && D <= DS_D_HI) {
      uint32_t lim = ((D >> 12) * DS_THR_K16) >> 12;          /* thr^2 D / 256: c^2 >> 8 must reach it */
      for (f = 0; f < DS_NFAM; f++) {
        uint32_t m = DS_PAT[f], i, c2;
        int32_t sp = 0, c;
        uint32_t ac;
        /* tap i (0 the oldest) is y[j - 2 (23 - i)] */
        for (i = 0; i < 24; i++, m >>= 1)
          if (m & 1) sp += d->y[(j - 2u * (23u - i)) & (DS_RING - 1)];
        c = 2 * sp - s1;
        ac = (uint32_t)(c < 0 ? -c : c);
        c2 = ac * ac;
        if ((c2 >> 8) >= lim) {
          uint32_t r2 = ds_udiv(c2 >> 8, D >> 16);            /* rho^2, Q8 */
          if (d->cfam < 0 || r2 > d->cr2)
            d->cj = j, d->cr2 = r2, d->cfam = f, d->csign = c > 0 ? 1 : -1;
        }
      }
    }
  }
  return ret;
}

/* one count; returns what ds_grid said for the grid points it made (the last that wasn't
   DS_NONE) */
__attribute__((noinline)) static int ds_count(ds_t *d, int32_t x) {
  int ret = DS_NONE;
  {
    int32_t v = (DS_BP_B0 * ((x - d->x2) << 8) - DS_BP_A1 * d->y1 - DS_BP_A2 * d->y2) >> 14;
    d->x2 = d->x1, d->x1 = x, d->y2 = d->y1, d->y1 = v, x = v;
  }
  if (d->prev < 0 && x >= 0) {
    uint32_t t = ((d->n - 1u) << 16) + ds_udiv((uint32_t)(-d->prev) << 16, (uint32_t)(x - d->prev));
    uint32_t dt = t - d->tl;
    if (d->have && dt < (32u << 16)) {
      /* grid points in [tl, t): phase k - 1 + (g - tl) / dt */
      while ((int32_t)(t - d->g) > 0) {
        uint32_t gl = d->g - d->tl, ph;
        if ((int32_t)gl < 0) gl = 0;
        ph = ((d->k - 1u) << 12) + ds_udiv((gl >> 4) << 12, dt >> 4);
        if (d->have > 1) {
          int32_t y = ds_clamp(((int32_t)(ph - d->phl) - DS_Y0) >> 1);
          int r = ds_grid(d, y);
          if (r) ret = r;
        }
#ifdef DS_FRING
        else d->j++;
#endif
        d->phl = ph;
        d->g += d->w16;
      }
    } else {
      /* a gap: the grid starts again at this crossing (in the decoder's builds, on the old
         grid's points, counting those it skipped) */
#ifdef DS_FRING
      while ((int32_t)(t - d->g) > 0) d->g += d->w16, d->j++;
#else
      d->g = t + d->w16;
#endif
      d->phl = d->k << 12;
      if (d->have) ds_gap(d);
      d->have = 0;
    }
    d->have++;
    d->k++;
    d->tl = t;
  }
  d->prev = x;
  d->n++;
  return ret;
}

#endif
