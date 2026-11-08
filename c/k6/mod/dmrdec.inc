/* dmrdec.inc: DMR metadata for dmrdet.c's DMR_DECODE build (c/k6/mod/dmrrx.c, docs/k6-dmr.md).
   Receive only. The grid (dmrsync.h, 2 points a symbol) keeps DS_RING values; at each sync hit
   the burst around it is read back through a 13-tap RRC (alpha 0.2, the transmitter's other
   half), scaled by its own sync, sliced, and decoded with c/k6/dmrburst.h:
     data sync   the whole burst once its last symbol is in: TACT, slot type; LC headers and
                 terminators (BPTC, RS: talkgroup, source), CSBKs and data headers (BPTC, their
                 CRC, the raw bytes) are left for housekeeping, which
                 may pause the counts; idle bursts stop at the slot type
     voice sync  burst A: the sync's scale is kept and the middles of B-E (576 grid points
                 apart in one slot) are sliced as they come in: EMB, the 4 embedded LC
                 fragments; E's completes the embedded LC (the LC again, or talker alias)
   Headers and terminators of one transmission are also summed as soft bits (a terminator's
   parity masks turned into a header's: the codes are linear), so a call that no single copy
   decodes at the K6's weak IF (~48 dB-Hz on 440.925) still comes out.
   Python reference: sim/k6if/dmrdec.py. SPDX-License-Identifier: MIT */

#ifndef DX_POL
#define DX_POL 1               /* the GP0 discriminator's sign: data syncs read negative */
#endif
#ifndef DX_TRACK
#ifdef __clang__
#define DX_COLD __attribute__((noinline, minsize)) /* housekeeping's share: size over speed */
#else
#define DX_COLD NOINLINE
#endif
#define DX_FLY 2                /* voice superframes read on in a row without their sync */
#define DX_TRACK 1             /* the grid's step from DMR's slots (dx_time) */
#endif
#define DX_NQ 10               /* pending captures */


/* sim/k6if/dmrdec.py TERM_DELTA: BPTC of the 0x96 ^ 0x99 parity mask difference, 196 bits */
static const uint8_t DX_TDELTA[25] = {0x00, 0x6F, 0x00, 0xD4, 0x00, 0xB0, 0x00, 0x78, 0x00,
                                      0x70, 0x00, 0xA0, 0x11, 0x40, 0x33, 0x80, 0x76, 0x00,
                                      0xC4, 0x00, 0x80, 0x01, 0x90, 0x03, 0x30};

typedef struct {
  uint32_t sj;                 /* the sync's last symbol (grid index) */
  int16_t dd;                  /* it can be read at sj + dd */
  uint8_t k;                   /* 0 a data burst, 1-4 voice burst B-E's middle, 5 A's sync, 6 the flywheel's
                                  next A, 7 a data burst's info */
  uint8_t pat;                 /* the sync's family, its sign in bit 3 (8: data) */
} dx_q_t;

typedef struct {
  dx_q_t q[DX_NQ];
  int nq;
  uint32_t lastj, lastP;       /* the last hit; the last sync placed (quarter grid points) */
  /* voice: burst A's scale, the fragments of B-E */
  int32_t vc, vinv, voff;
  int8_t frag[128];            /* soft (positive = 1) */
  int8_t fold[2][128];         /* the last two superframes' fragments, to add to a weak one */
  uint32_t fmask;
  /* a data burst waiting for housekeeping */
  int8_t soft[196];
  uint32_t dP;                 /* its sync and scale, for the second half of its reading */
  int32_t dc, dinv;
  /* the soft sum of this transmission's headers and terminators (a quarter of each, saturating) */
  int8_t comb[196];
  int ncomb;
  /* what is known, for the lines and the screen */
  uint32_t tg, src, lcd_dirty;
  uint8_t ta[4][7];
  uint32_t tamask;
  char alias[32];
  uint32_t nlc, nemb, ncsbk, nrd, ngol, nmid, nmok;
  /* the last good TACT: its burst (grid point of the sync) and slot; the pending burst's sync,
     the voice's slot */
  uint32_t tsj, psj;
  /* the voice chain: its last superframe's sync (hit or flywheel) */
  uint32_t vj;
  /* small ones, bytes: the voice's older fragments kept; the pending data burst (waiting for
     housekeeping), its type, colour code and slot; an embedded LC to try; the LC known, its
     FLCO, colour code and slot; the last good TACT's slot, the voice's; superframes read on
     without a hit, whether an EMB has checked out since the chain started */
  uint8_t nold, pend, dtype, cc, slot, emb_ready, tg_ok, flco, lcc, lslot, tslot, vslot, nfly, vok;
} dx_t;

/* the matched filter's output (dmrsync.h's f[]) at P quarter grid points, Catmull-Rom between
   grid points: 2 points a symbol leave up to a quarter symbol between the nearest one and the
   symbol's centre (on the K6 model: 1-2 bit errors a burst at any level; linear 0.2, cubic
   0.07) */
static const int16_t DX_CR[4][4] = {{0, 128, 0, 0}, {-9, 111, 29, -3}, {-8, 72, 72, -8},
                                   {-3, 29, 111, -9}};
static int32_t dx_mf(const ds_t *d, uint32_t P) {
  uint32_t g = (P >> 2) - 1u, k;
  const int16_t *w = DX_CR[P & 3];
  int32_t a = 0;
  if (!(P & 3)) return d->f[(g + 1u) & (DS_FRING - 1)] * DX_POL;   /* on a grid point */
  for (k = 0; k < 4; k++) a += w[k] * d->f[(g + k) & (DS_FRING - 1)];
  return (a >> 7) * DX_POL;
}

/* the sync's timing again on the filtered values, to a quarter grid point: the hit came from
   the bare ones, and at 2 points a symbol one point off is half a symbol, the eye's edge.
   -> the sync's last symbol in quarter grid points */
static int32_t dx_corr(const ds_t *d, uint32_t P, int pat) {
  uint32_t m = DS_PAT[pat & 7], i;
  int32_t a = 0;
  for (i = 0; i < 24; i++, m >>= 1, P += 8u) {
    int32_t v = dx_mf(d, P);
    a += (m & 1) ? v : -v;
  }
  return (pat & 8) ? -a : a;
}

NOINLINE static uint32_t dx_align(const ds_t *d, uint32_t sj, int pat) {
  /* the correlation at whole grid points -2..+2 (the filter's own values: cheap), then the
     peak to a quarter from a parabola through the best and its neighbours: offset = (c+ - c-)
     / 2 (2 c0 - c- - c+) grid points, rounded to quarters by comparisons (no division) */
  int32_t c[5], best = 0, o, dl, k, a;
  uint32_t P0 = 4u * (sj - 46u);
  for (o = 0; o < 5; o++) {
    c[o] = dx_corr(d, P0 + 4u * (uint32_t)o - 8u, pat);
    if (c[o] > c[best]) best = o;
  }
  o = 4 * (best - 2);
  if (best == 0 || best == 4) return 4u * sj + (uint32_t)o;
  dl = c[best + 1] - c[best - 1], k = 2 * c[best] - c[best - 1] - c[best + 1], a = dl < 0 ? -dl : dl;
  if (k > 0 && 4 * a > k) o += (dl < 0 ? -1 : 1) * (4 * a > 3 * k ? 2 : 1);
  return 4u * sj + (uint32_t)o;
}

/* the sync ending at P (quarter grid points): its centre, and 768 << 12 / its mean
   |deviation| (the scale that puts +-3 at +-768, Q8 symbols) */
NOINLINE static void dx_scale(const ds_t *d, uint32_t P, int32_t *c, int32_t *inv) {
  int32_t v[24], s = 0, m = 0;
  int i;
  for (i = 0; i < 24; i++) s += v[i] = dx_mf(d, P - 184u + 8u * i);
  s = (s * 2731) >> 16;                                     /* / 24 (about) */
  for (i = 0; i < 24; i++) m += v[i] > s ? v[i] - s : s - v[i];
  m = (m * 2731) >> 16;
  if (m < 4) m = 4;
  *c = s, *inv = (int32_t)ds_udiv(768u << 12, (uint32_t)m);
}

/* n symbols from P0 (quarter grid points), 8 apart, as bits (two a symbol) if b, and soft bits if soft */
NOINLINE static void dx_slice(const ds_t *d, uint32_t P0, int n, int32_t c, int32_t inv,
                              uint8_t *b, int8_t *soft) {
  int i;
  for (i = 0; i < n; i++) {
    int32_t u = ((dx_mf(d, P0 + 8u * i) - c) * inv) >> 12;
    if (b) db_hard(u, b + 2 * i);
    if (soft) db_soft(u, soft + 2 * i);
  }
}

/* a sync hit: queue what to read */
static void dx_push(dx_t *x, uint32_t due, uint32_t sj, int k, int pat) {
  dx_q_t *q = &x->q[x->nq];
  if (x->nq < DX_NQ) q->sj = sj, q->dd = (int16_t)(due - sj), q->k = (uint8_t)k, q->pat = (uint8_t)pat, x->nq++;
}

/* the grid's step from DMR's own timing: syncs (as dx_align places them, to a quarter grid
   point) a whole number m of 30 ms slots apart should be 1152 m quarters apart; each pair moves
   the step 1/8 of the way. The DMA's rate (housekeeping's measure) is good to ~2%, which would
   put a burst's far end 2 symbols off and B-E's middles, up to 240 ms after A, 10 */
__attribute__((unused)) static int dx_locked(const dx_t *x, const ds_t *d) { return x->lastj && d->j - x->lastj < 2000u; }

static void dx_time(dx_t *x, ds_t *d, uint32_t P) {
  uint32_t dP = P - x->lastP, m = ds_udiv(dP + 576u, 1152u);
  int32_t e = (int32_t)dP - (int32_t)(1152u * m);
  if (DX_TRACK && x->lastP && m && m <= 2 && e <= 64 && e >= -64) {
    uint32_t dw = ds_udiv(d->w16 * (uint32_t)(e < 0 ? -e : e), 9216u * m);   /* 1/8 */
    d->w16 += e < 0 ? (uint32_t)-dw : dw;
  }
  x->lastP = P;
}

/* a voice superframe from its sync (burst A's) at sj: A's scale, once its filter has run, then
   B-E's middles */
static void dx_voice(dx_t *x, uint32_t sj, int pat) {
  int i;
  dx_push(x, sj + 12u, sj, 5, pat);
  x->fmask = 0;
  x->voff = 0;
  for (i = 1; i <= 4; i++) dx_push(x, sj + 576u * i + 12u, sj + 576u * i, i, 0);
}

/* may housekeeping run now? Its API calls stop the counting (the radio's for longer than the
   DMA ring) and the filter fills again after them, ~5 ms lost in all: while a voice chain runs,
   only in the other slot's time (its burst A's sync ends at vj; its slot's bursts, CACH to the
   end, are -178 to +110 grid points from there, every 576) */
__attribute__((unused)) static int dx_quiet(const dx_t *x, uint32_t j) {
  uint32_t e = j - x->vj;
  if (e > 3u * 3456u) return 1;
  e -= ds_udiv(e, 576u) * 576u;
  return e - 120u <= 200u;
}

/* sj one or two superframes (3456 grid points) after vj, give or take 24 */
static int dx_cadence(uint32_t vj, uint32_t sj) {
  uint32_t e = sj - vj;
  return e - 3456u + 24u <= 48u || e - 6912u + 24u <= 48u;
}

static void dx_hit(dx_t *x, const ds_t *d) {
  x->lastj = d->hj;
  if (d->hsign * DX_POL < 0) {
    dx_push(x, d->hj + 108u + 12u, d->hj, 0, (int)d->hfam | 8);
    return;
  }
  {
    uint32_t e = d->hj - x->vj;
    int on = dx_cadence(x->vj, d->hj);
    if (e + 24u <= 48u) return;                             /* the superframe the flywheel started */
    if (x->vok && e < 6912u + 48u && !on) return;           /* off a checked chain's beat: false */
    if (!on) x->vok = 0;
    x->vj = d->hj, x->nfly = 0;
    dx_voice(x, d->hj, (int)d->hfam);
  }
}

/* the slot of the burst whose sync is at sj, from the last good TACT (a repeater's slots
   alternate every 288 grid points); 0 if none yet */
static int dx_slotof(dx_t *x, uint32_t sj) {
  int32_t e = (int32_t)(sj - x->tsj);
  if (!x->tslot) return 0;
  return (ds_udiv((uint32_t)(e < 0 ? -e : e) + 144u, 288u) & 1) ? 3 - x->tslot : x->tslot;
}

/* the CACH's TACT before the sync at P: the slot it names, kept with its burst */
static void dx_cach(dx_t *x, ds_t *d, uint32_t P, int32_t c, int32_t inv, uint32_t sj) {
  uint8_t b[24];
  int t, s;
  dx_slice(d, P - 712u, 12, c, inv, b, 0);
  t = db_tact(b);
  s = ((t >> 2) & 1) + 1;
  if ((t & 0x10) && x->tslot && s != dx_slotof(x, sj)) return;  /* corrected, and it disagrees */
  x->tslot = s, x->tsj = sj;
}

/* the counts went on: read what is due (cheap: the slicing and the short codes) */
NOINLINE static void dx_poll(dx_t *x, ds_t *d) {
  static const uint8_t LCSS[5] = {0, 1, 3, 3, 2};
  int i = 0;
  while (i < x->nq) {
    dx_q_t q = x->q[i];
    int err;
    uint32_t P, j;
    int32_t late = (int32_t)(d->j - q.sj) - q.dd;
    if (late < 0) {
      i++;
      continue;
    }
    x->q[i] = x->q[--x->nq];
    if (late > 200) continue;                               /* overwritten meanwhile */
    if (q.k == 6) {                                         /* the next superframe's A, due: */
      if (x->nfly >= DX_FLY || (q.sj - x->vj) - 3456u + 24u > 48u) continue;  /* its hit came, */
      x->nfly++, x->vj = q.sj;                              /* or a newer chain, or enough */
      dx_voice(x, q.sj, q.pat);
      continue;
    }
    if (q.k == 7) {                                         /* a data burst's info, after its slot type */
      dx_slice(d, x->dP - 712u + 8u * 12u, 49, x->dc, x->dinv, 0, x->soft);       /* after the CACH */
      dx_slice(d, x->dP - 712u + 8u * 95u, 49, x->dc, x->dinv, 0, x->soft + 98);  /* after the sync */
      x->pend = 1;
      return;
    }
    if (q.k == 5) {                                         /* voice burst A: its sync's scale */
      dx_push(x, q.sj + 3456u + 24u, q.sj + 3456u, 6, q.pat);  /* the flywheel: 360 ms on */
      P = dx_align(d, q.sj, q.pat);
      dx_time(x, d, P);
      x->voff = (int32_t)(P - 4u * q.sj);
      dx_scale(d, P, &x->vc, &x->vinv);
      dx_cach(x, d, P, x->vc, x->vinv, q.sj);
      x->vslot = dx_slotof(x, q.sj);
      return;
    }
    if (q.k) {                                              /* a voice burst's middle */
      uint32_t e;
      uint8_t b[48];
      int8_t sb[48];
      P = 4u * q.sj + (uint32_t)x->voff;
      dx_slice(d, P - 184u, 24, x->vc, x->vinv, b, sb);
      e = db_emb(b, &err);
      x->nmid++;
      dx_cach(x, d, P, x->vc, x->vinv, q.sj);
      x->vslot = dx_slotof(x, q.sj);
      if (x->tg_ok && !x->lslot && x->vslot) x->lslot = x->vslot, x->lcd_dirty = 1;
      for (j = 0; j < 32; j++) x->frag[32 * (q.k - 1) + j] = sb[8 + j];
      if (err <= 2 && (e & 3) == LCSS[q.k]) x->nmok++, x->vok = 1, x->fmask |= 1u << q.k, x->lcc = (int)(e >> 3);
      /* E's in: the LC's checks will judge it, if at least two EMBs say these are B-E */
      if (q.k == 4 && db_pop(x->fmask) >= 2) x->emb_ready = 1;
      continue;
    }
    {
      int32_t c, inv;
      uint32_t st;
      uint8_t s20[20];
      P = dx_align(d, q.sj, q.pat);
      dx_time(x, d, P);
      dx_scale(d, P, &c, &inv);
      dx_slice(d, P - 184u - 40u, 5, c, inv, s20, 0);         /* the slot type first */
      dx_slice(d, P + 8u, 5, c, inv, s20 + 10, 0);
      st = db_slot(s20, &err);
      x->nrd++;
      if (err > 3) continue;
      x->ngol++;
      dx_cach(x, d, P, c, inv, q.sj);
      if (x->pend) continue;                                /* one waiting: only its slot and timing */
      x->dtype = (int)(st & 15), x->cc = (int)(st >> 4);
      x->slot = dx_slotof(x, q.sj), x->psj = q.sj;
      if (x->dtype == 1 || x->dtype == 2 || x->dtype == 3 || x->dtype == 6)
        x->dP = P, x->dc = c, x->dinv = inv, dx_push(x, d->j, q.sj, 7, 0);  /* the rest next time */
      return;                                               /* one align a call: the ring drains */
    }
  }
}

static char *dx_hex(char *p, const uint8_t *b, int n) {
  static const char H[] = "0123456789abcdef";
  while (n--) *p++ = H[*b >> 4], *p++ = H[*b++ & 15];
  return p;
}

/* the LC's talkgroup / source as known; 1 if it changed */
DX_COLD static int dx_lc(dx_t *x, const uint8_t *by) {
  uint32_t tg = ((uint32_t)by[3] << 16) | ((uint32_t)by[4] << 8) | by[5];
  uint32_t src = ((uint32_t)by[6] << 16) | ((uint32_t)by[7] << 8) | by[8];
  int f = by[0] & 0x3F;
  if (f >= 4 && f <= 7) {                                   /* talker alias: header, blocks 1-3 */
    int i;
    for (i = 0; i < 7; i++) x->ta[f - 4][i] = by[2 + i];
    x->tamask |= 1u << (f - 4);
    return 0;
  }
  if (x->tg_ok && tg == x->tg && src == x->src && f == x->flco) return 0;
  if (src != x->src) x->tamask = 0, x->alias[0] = 0;
  x->tg = tg, x->src = src, x->flco = f, x->tg_ok = 1, x->lcd_dirty = 1;
  return 1;
}

/* talker alias text so far (7-bit or 8-bit formats) */
DX_COLD static void dx_alias(dx_t *x) {
  uint32_t fmt, n, bit = 0, nb, i, k = 0;
  if (!(x->tamask & 1)) return;
  fmt = x->ta[0][0] >> 6, n = (x->ta[0][0] >> 1) & 31;
  nb = 49 + 56 * ((x->tamask & 2) ? ((x->tamask & 4) ? ((x->tamask & 8) ? 3 : 2) : 1) : 0);
  if (fmt) bit = 1;
  while (k < n && k < 31 && bit + (fmt ? 8 : 7) <= nb) {
    uint32_t ch = 0, w = fmt ? 8 : 7;
    for (i = 0; i < w; i++, bit++) {
      /* bit 0 is the header's byte 2 bit 0; then its bytes 3-8, then blocks' bytes 2-8 */
      uint32_t bb = bit, v;
      if (bb == 0) v = x->ta[0][0] & 1;
      else if (bb < 49) v = (x->ta[0][1 + (bb - 1) / 8] >> (7 - (bb - 1) % 8)) & 1;
      else {
        uint32_t o = bb - 49, blk = ds_udiv(o, 56), r = o - 56 * blk;
        v = (x->ta[1 + blk][r / 8] >> (7 - r % 8)) & 1;
      }
      ch = (ch << 1) | v;
    }
    x->alias[k++] = (char)(ch >= 32 && ch < 127 ? ch : '?');
  }
  x->alias[k] = 0;
  x->lcd_dirty = 1;
}

/* housekeeping's share: decode what is waiting, at most one line; returns its end or 0 */
DX_COLD static char *dx_house(dx_t *x, char *line) {
  char *p = line;
  uint8_t by[12], hb[196];
  int j;
  if (x->emb_ready) {
    /* the embedded LC alone, then added to the last superframe's, the one before (talker alias
       LCs between the call's LC), both: a call's LC is the same in each superframe */
    int k, ok = 0;
    x->emb_ready = 0;
    for (k = 0; k < 4 && !ok; k++) {
      if (k > x->nold + (k == 3)) continue;
      for (j = 0; j < 128; j++) {
        int v = x->frag[j];
        if (k & 1) v += x->fold[0][j];
        if (k >= 2) v += x->fold[1][j];
        hb[j] = v >= 0;
      }
      ok = db_emb_lc(hb, by);
    }
    for (j = 0; j < 128; j++) x->fold[1][j] = x->fold[0][j], x->fold[0][j] = x->frag[j];
    if (x->nold < 2) x->nold++;
    if (ok) {
      x->nemb++;
      if (dx_lc(x, by)) p = put(kv(kv(kv(put(line, "E TS"), "", x->vslot), " TG ", (int)x->tg), " src ", (int)x->src), "");
      if ((by[0] & 0x3F) >= 4 && (by[0] & 0x3F) <= 7) {
        dx_alias(x);
        if (p == line && x->alias[0]) p = put(put(line, "A "), x->alias);
      }
    }
    return p == line ? 0 : p;
  }
  if (!x->pend) return 0;
  x->pend = 0;
  if (!x->slot) x->slot = dx_slotof(x, x->psj);             /* a later burst's TACT, if its own missed */
  for (j = 0; j < 196; j++) hb[j] = x->soft[j] >= 0;          /* the hard bits: db_soft's signs */
  db_bptc196(hb, by);
  if (x->dtype == 1 || x->dtype == 2) {
    int ok = db_rs_ok(by, x->dtype == 1 ? 0x96 : 0x99);
    for (j = 0; j < 196; j++) {
      int s = x->soft[j];
      if (x->dtype == 2 && ((DX_TDELTA[j >> 3] >> (7 - (j & 7))) & 1)) s = -s;
      s = (s >> 2) + x->comb[j];
      x->comb[j] = (int8_t)(s > 127 ? 127 : s < -127 ? -127 : s);
    }
    x->ncomb++;
    if (!ok && x->ncomb >= 2) {
      for (j = 0; j < 196; j++) hb[j] = x->comb[j] > 0;
      db_bptc196(hb, by);
      ok = db_rs_ok(by, 0x96) ? 2 : 0;
    }
    if (!ok) return 0;
    x->nlc++, x->lcc = x->cc, x->lslot = x->slot;
    if (!dx_lc(x, by)) return 0;
    p = kv(kv(kv(kv(put(line, "L TS"), "", x->slot), " CC", x->cc), " TG ", (int)x->tg), " src ", (int)x->src);
    p = kv(p, " F", x->flco);
    if (ok == 2) p = kv(p, " comb", x->ncomb);
    return p;
  }
  /* CSBK (3) or data header (6): the 10 bytes if the CRC (init 0, inverted, masked: checked on
     440.925's preambles and GPS headers) fits */
  {
    uint32_t got = ((uint32_t)by[10] << 8) | by[11], m = x->dtype == 3 ? 0xA5A5u : 0xCCCCu;
    if ((db_crc16(by, 10, 0, 0xFFFF) ^ m) != got) return 0;
    x->ncsbk++;
    p = kv(put(line, x->dtype == 3 ? "K TS" : "H TS"), "", x->slot);
    *p++ = ' ';
    p = dx_hex(p, by, 10);
  }
  return p;
}

/* the screen (level 6): colour code and slot, talkgroup, source, alias */
__attribute__((unused)) DX_COLD static int dx_lcd(const k6_api_t *api, dx_t *x) {
  char t[24], *p;
  if (api->level < 6 || !x->lcd_dirty) return 0;
  x->lcd_dirty = 0;
  p = kv(kv(put(t, "DMR CC"), "", x->lcc), " TS", x->lslot);
  *p = 0, api->lcd_text(0, t);
  p = kv(put(t, x->flco == 3 ? "to " : "TG "), "", (int)x->tg);
  *p = 0, api->lcd_text(1, t);
  p = kv(put(t, "ID "), "", (int)x->src);
  *p = 0, api->lcd_text(2, t);
  api->lcd_text(3, x->alias);
  api->lcd_flush();
  return 1;
}

/* the transmission ended (D off): the soft sum starts over */
__attribute__((unused)) static void dx_end(dx_t *x) {
  int j;
  for (j = 0; j < 196; j++) x->comb[j] = 0;
  x->ncomb = 0, x->nq = 0, x->nold = 0;
}
