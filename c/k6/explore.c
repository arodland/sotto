/* explore: the K6 explorer's candidate list (explore.h has the cues and grades).
   SPDX-License-Identifier: MIT */
#include "explore.h"

/* US: ARRL 2 m and 1.25 m repeater outputs and simplex, WWARA 70 cm (440-445 outputs +5 MHz in
   the Pacific Northwest), the 70 cm simplex calling range. From memory: check before a release. */
const ex_plan_t ex_plan_us[] = {
    {145100000u, 145500000u, -600000, 0, 0, 0},
    {146400000u, 146580000u, 0, 1, 0, 0},
    {146610000u, 146970000u, -600000, 0, 0, 0},
    {147000000u, 147390000u, 600000, 0, 0, 0},
    {147420000u, 147570000u, 0, 1, 0, 0},
    {223400000u, 223520000u, 0, 1, 0, 0},
    {223850000u, 224980000u, -1600000, 0, 0, 0},
    {440000000u, 445000000u, 5000000, 0, 0, 25},
    {445950000u, 446100000u, 0, 1, 0, 0},
};
const unsigned ex_plan_us_n = sizeof ex_plan_us / sizeof ex_plan_us[0];

static uint32_t dist(uint32_t a, uint32_t b) { return a > b ? a - b : b - a; }

static void zero(void *p, unsigned n) {
  uint8_t *d = p;
  while (n--) *d++ = 0;
}

void ex_init(ex_t *e, ex_cand_t *store, unsigned cap) {
  e->c = store, e->n = 0, e->cap = (uint16_t)cap, e->tol_hz = 5000;
  e->known = 0, e->known_n = e->known_cap = 0, e->now = 0;
}

void ex_known_store(ex_t *e, uint32_t *buf, unsigned cap) {
  e->known = buf, e->known_n = 0, e->known_cap = (uint16_t)cap;
}

static int known(const ex_t *e, uint32_t hz) {
  unsigned i;
  for (i = 0; i < e->known_n; i++)
    if (dist(e->known[i], hz) <= e->tol_hz) return 1;
  return 0;
}

ex_cand_t *ex_find(ex_t *e, uint32_t hz) {
  ex_cand_t *best = 0;
  unsigned i;
  for (i = 0; i < e->n; i++)
    if (dist(e->c[i].hz, hz) <= e->tol_hz && (!best || dist(e->c[i].hz, hz) < dist(best->hz, hz)))
      best = &e->c[i];
  return best;
}

static const ex_plan_t *plan_row(uint32_t hz) {
  unsigned i;
  for (i = 0; i < ex_plan_us_n; i++)
    if (hz >= ex_plan_us[i].lo_hz && hz <= ex_plan_us[i].hi_hz) return &ex_plan_us[i];
  return 0;
}

static int calling(uint32_t hz) {
  return dist(hz, 146520000u) <= 2500 || dist(hz, 223500000u) <= 2500 || dist(hz, 446000000u) <= 2500;
}

void ex_grade(ex_cand_t *c) {
  const ex_plan_t *p = plan_row(c->hz);
  int s = 0, n;
  c->flags &= (uint8_t)~EX_F_SIMPLEX;
  if (c->overs >= 3) {
    if (c->hi - c->lo <= EX_SAME_DB) s += 3;
    else if (c->hi - c->lo > EX_WANDER_DB) s -= 2;
  }
  if (c->flags & EX_F_OFF_HEARD) s += 4;
  if (p && !p->simplex) s += 2;
  if ((p && p->simplex) || calling(c->hz)) s -= 3, c->flags |= EX_F_SIMPLEX;
  if (c->tone) s += 1;
  if (c->call[0]) {
    s += 2;
    for (n = 0; n < 10 && c->call[n]; n++) {}
    if ((n >= 2 && c->call[n - 2] == '/' && c->call[n - 1] == 'R') ||
        (n >= 3 && c->call[n - 3] == 'R' && c->call[n - 2] == 'P' && c->call[n - 1] == 'T'))
      s += 1;
  }
  if (c->ids >= 2) s += 1;
  if (!(c->flags & EX_F_OFF_HEARD)) c->off_hz = p && !p->simplex ? p->off_hz : 0;
  if ((c->flags & EX_F_SPUR) || (c->overs && c->skirts >= c->overs)) s = -100;   /* last, first to go */
  c->score = (int8_t)(s < -100 ? -100 : s > 100 ? 100 : s);
  if (s == -100) c->grade = 's';
  else if (c->overs < EX_MIN_OVERS && !(c->flags & EX_F_OFF_HEARD) && !c->call[0]) c->grade = '?';
  else if ((c->flags & EX_F_SIMPLEX) || s < 0) c->grade = 's';
  else c->grade = s >= 6 ? 'R' : 'r';
}

/* the slot a new candidate takes: a free one, else the lowest score, oldest (ignored and tried
   candidates are kept, so an ignore sticks) */
static ex_cand_t *slot(ex_t *e) {
  ex_cand_t *w = 0;
  unsigned i;
  if (e->n < e->cap) return &e->c[e->n++];
  for (i = 0; i < e->n; i++) {
    ex_cand_t *c = &e->c[i];
    if (c->flags & (EX_F_IGNORED | EX_F_TRIED)) continue;
    if (!w || c->score < w->score || (c->score == w->score && c->last_ms < w->last_ms)) w = c;
  }
  return w;
}

/* the K6's own MCU clock: 24 MHz harmonics, 72-336 MHz seen on rxmap, 144.000 at -72 dBm the
   loudest (the HF thread, 2026-11-07). Nobody transmits on exactly n x 24 MHz, and no repeater
   output sits there. (The FD6818's 13 / 26 MHz family is left alone: 442.000 is a channel.) */
int ex_own_clock(uint32_t hz) {
  uint32_t f;
  for (f = 24000000u; f < 1000000000u; f += 24000000u)
    if (dist(hz, f) <= 2500) return 1;
  return 0;
}

static ex_cand_t *get(ex_t *e, uint32_t hz, uint32_t t) {
  ex_cand_t *c = ex_find(e, hz);
  if (c) return c;
  if (!(c = slot(e))) return 0;
  zero(c, sizeof *c);
  c->hz = hz, c->first_ms = c->last_ms = t, c->grade = '?';
  if (ex_own_clock(hz)) c->flags = EX_F_SPUR;
  if (known(e, hz)) c->flags |= EX_F_KNOWN;
  return c;
}

void ex_know(ex_t *e, uint32_t hz) {
  ex_cand_t *c = ex_find(e, hz);
  if (c) c->flags |= EX_F_KNOWN;
  if (!known(e, hz) && e->known_n < e->known_cap) e->known[e->known_n++] = hz;
}

void ex_unknow(ex_t *e, uint32_t hz) {
  ex_cand_t *c = ex_find(e, hz);
  unsigned i;
  if (c) c->flags &= (uint8_t)~EX_F_KNOWN;
  for (i = 0; i < e->known_n;)
    if (dist(e->known[i], hz) <= e->tol_hz) e->known[i] = e->known[--e->known_n];
    else i++;
}

ex_cand_t *ex_over(ex_t *e, uint32_t hz, uint32_t start_ms, uint32_t dur_ms, int level) {
  ex_cand_t *c = get(e, hz, start_ms);
  unsigned i;
  if (!c) return 0;
  if (!c->overs) c->first_ms = start_ms;   /* ex_pick_id may have made it before its first over */
  if (!c->overs || level < c->lo) c->lo = (int16_t)level;
  if (!c->overs || level > c->hi) c->hi = (int16_t)level;
  /* a short over 5-20 min after the last short one: an ID's rhythm */
  if (dur_ms >= 2000 && dur_ms <= 15000) {
    uint32_t gap = start_ms - c->prev_start;
    if (c->prev_start && gap >= 300000u && gap <= 1200000u) c->ids++;
    c->prev_start = start_ms | 1;
  }
  c->overs++, c->on_ms += dur_ms, c->last_ms = start_ms + dur_ms;
  if (c->last_ms - e->now < 0x80000000u) e->now = c->last_ms;
  /* a skirt: this over and a neighbour's last one ended together, one EX_SKIRT_DB louder */
  for (i = 0; i < e->n; i++) {
    ex_cand_t *o = &e->c[i];
    if (o == c || !o->overs || dist(o->hz, hz) > EX_SKIRT_HZ || dist(o->last_ms, c->last_ms) > 2000)
      continue;
    if (o->hi >= level + EX_SKIRT_DB && c->skirts < c->overs) c->skirts++;
    else if (level >= o->hi + EX_SKIRT_DB && o->skirts < o->overs) o->skirts++, ex_grade(o);
  }
  ex_grade(c);
  return c;
}

void ex_spur(ex_t *e, uint32_t hz, int lvl) {
  ex_cand_t *c = get(e, hz, 0);
  if (!c) return;
  if (!c->overs) c->lo = c->hi = (int16_t)lvl;   /* a birdie up since it was found: its level */
  c->flags |= EX_F_SPUR, ex_grade(c);
}

void ex_input(ex_t *e, uint32_t hz, int32_t off_hz) {
  ex_cand_t *c = ex_find(e, hz);
  if (c) c->off_hz = off_hz, c->flags |= EX_F_OFF_HEARD, ex_grade(c);
}

void ex_tone(ex_t *e, uint32_t hz, uint16_t tone) {
  ex_cand_t *c = ex_find(e, hz);
  if (c) c->tone = tone, ex_grade(c);
}

void ex_cw(ex_t *e, uint32_t hz, const char *call) {
  ex_cand_t *c = ex_find(e, hz);
  int i, end = 0;
  if (!c) return;
  for (i = 0; i < 10; i++) {
    if (!call[i]) end = 1;
    c->call[i] = end ? 0 : call[i];
  }
  ex_grade(c);
}

/* is a ranked before b? */
static int before(const ex_cand_t *a, const ex_cand_t *b) {
  int ka = (a->flags & EX_F_KNOWN) != 0, kb = (b->flags & EX_F_KNOWN) != 0;
  int ia = (a->flags & EX_F_IGNORED) != 0, ib = (b->flags & EX_F_IGNORED) != 0;
  if (ka != kb) return kb;
  if (ia != ib) return ib;
  if (a->score != b->score) return a->score > b->score;
  if (a->overs != b->overs) return a->overs > b->overs;
  return (int32_t)(a->last_ms - b->last_ms) > 0;
}

static int skirt(const ex_cand_t *c) { return c->overs && c->skirts >= c->overs; }

/* the i-th by selection (skirts left out unless all): no sorting in place, so a pointer the
   caller holds stays put */
/* off the screen list: a skirt, or a stale one-over '?' (heard once, nothing more about it, and
   nothing since for EX_STALE_MS: bench 7's 35 noise bins from a low floor read, 2026-11-08) */
static int hidden(const ex_t *e, const ex_cand_t *c) {
  return skirt(c) || (c->overs == 1 && c->grade == '?' && !c->call[0] &&
                      !(c->flags & (EX_F_KNOWN | EX_F_TRIED | EX_F_OFF_HEARD)) &&
                      e->now - c->last_ms > EX_STALE_MS && e->now - c->last_ms < 0x80000000u);
}

static ex_cand_t *nth(ex_t *e, unsigned i, int all) {
  unsigned k = 0, j;
  ex_cand_t *prev = 0;
  for (;;) {
    ex_cand_t *best = 0;
    for (j = 0; j < e->n; j++) {
      ex_cand_t *c = &e->c[j];
      if (prev && !before(prev, c)) {
        /* c ranks at or above prev: only a later index on an exact tie may follow it */
        if (before(c, prev) || c <= prev) continue;
      }
      if (!best || before(c, best)) best = c;
    }
    if (!best) return 0;
    prev = best;
    if (all || !hidden(e, best)) {
      if (k == i) return best;
      k++;
    }
  }
}

ex_cand_t *ex_rank(ex_t *e, unsigned i) { return nth(e, i, 1); }
ex_cand_t *ex_view(ex_t *e, unsigned i) { return nth(e, i, 0); }

unsigned ex_view_n(const ex_t *e) {
  unsigned j, n = 0;
  for (j = 0; j < e->n; j++) n += !hidden(e, &e->c[j]);
  return n;
}

int ex_on_raster(uint32_t hz) {
  const ex_plan_t *p = plan_row(hz);
  uint32_t r, d;
  if (!p || !p->raster_khz) return 1;
  r = p->raster_khz * 1000u, d = hz - p->lo_hz;
  while (d >= r) d -= r;   /* at most a few hundred: no division on the M0 */
  return d <= 1000 || r - d <= 1000;
}

ex_cand_t *ex_pick_id(ex_t *e, const uint32_t *up_hz, unsigned n) {
  ex_cand_t *best = 0;
  unsigned i;
  for (i = 0; i < n; i++) {
    const ex_plan_t *p = plan_row(up_hz[i]);
    ex_cand_t *c;
    /* only a repeater output (144.000, the K6's own 24 MHz harmonic, was picked on the bench);
       a carrier keyed for the first time has no over yet: it gets its candidate now */
    if (!p || p->simplex || !(c = get(e, up_hz[i], 0))) continue;
    if (c->call[0] || c->grade == 's' || (c->flags & (EX_F_SPUR | EX_F_IGNORED | EX_F_SIMPLEX)) ||
        (c->overs && c->skirts >= c->overs))
      continue;
    if (!best || c->listens < best->listens) best = c;   /* a tie: the first, the loudest (xs_up) */
  }
  if (best && best->listens < 255) best->listens++;
  return best;
}

unsigned ex_listen_cap(const ex_cand_t *c, unsigned max_s, unsigned min_s) {
  unsigned t;
  for (t = c->heard_s; t >= EX_HEARD_STEP_S && max_s > min_s; t -= EX_HEARD_STEP_S) max_s >>= 1;
  return max_s < min_s ? min_s : max_s;
}

void ex_heard(ex_t *e, uint32_t hz, unsigned s) {
  ex_cand_t *c = ex_find(e, hz);
  if (c) c->heard_s = (uint16_t)(c->heard_s + s > 0xffffu ? 0xffffu : c->heard_s + s);
}

int ex_dmr_heard(ex_t *e, uint32_t hz, int dmr, unsigned s) {
  ex_cand_t *c = ex_find(e, hz);
  if (!c) return 0;
  if (dmr) {
    c->flags |= EX_F_DMR, c->clean = 0;
    return 0;
  }
  if (!(c->flags & EX_F_DMR) || s < EX_CLEAN_S) return 0;
  if (++c->clean < EX_CLEAN_N) return 0;
  c->flags &= (uint8_t)~EX_F_DMR, c->clean = 0;
  return 1;
}

void ex_to_rt(const ex_cand_t *c, rt_rec_t *r) {
  int i, lvl = c->hi;
  zero(r, sizeof *r);
  r->rx_hz = c->hz;
  r->tx_off = c->off_hz;
  r->rx_tone = r->tx_tone = c->tone;
  r->flags = (uint16_t)(RT_F_WIDE | (c->flags & EX_F_OFF_HEARD ? RT_F_OFF_SURE : 0) |
                        (c->flags & EX_F_DMR ? RT_F_DMR : 0));
  r->best_rssi = (uint8_t)(lvl < -200 ? 0 : lvl > 55 ? 255 : lvl + 200);
  r->score = (uint8_t)c->grade;
  for (i = 0; i < 10; i++) r->call[i] = c->call[i];
  if (c->call[0]) r->call_flags = RT_C_FROM_CW;
}
