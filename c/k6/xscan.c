/* xscan: the explorer's sweep for the core image, a block of bins at a time (xscan.h).
   The steps and their reasons are the scan module's (c/k6/mod/scan.c), which ran on air at
   372-405 bins/s (docs/k6-scan.md); the comments there explain each rule.
   SPDX-License-Identifier: MIT */
#include "xscan.h"

#define FLMAX 1024
#define CLEAR_DBM (-100)
#define SPUR_DB 3

static char *put(char *p, const char *s) {
  while (*s) *p++ = *s++;
  return p;
}

/* k then v in decimal, without a divide; dot 3: v in kHz, written as MHz */
static char *num(char *p, const char *k, int v, int dot) {
  static const uint32_t pw[] = {1000000000, 100000000, 10000000, 1000000, 100000,
                                10000,      1000,      100,      10,      1};
  uint32_t u;
  int i, started = 0;
  p = put(p, k);
  if (v < 0) *p++ = '-', v = -v;
  u = (uint32_t)v;
  for (i = 0; i < 10; i++) {
    int d = 0;
    while (u >= pw[i]) u -= pw[i], d++;
    if (d || started || i >= 9 - dot) *p++ = (char)('0' + d), started = 1;
    if (dot && i == 6) *p++ = '.';
  }
  return p;
}

static uint32_t udiv(uint32_t n, uint32_t d) {
  uint32_t q = 0, bit = 1;
  if (!d) return 0;
  while (d < n && !(d & 0x80000000u)) d <<= 1, bit <<= 1;
  while (bit) {
    if (n >= d) n -= d, q |= bit;
    d >>= 1, bit >>= 1;
  }
  return q;
}

static char *fq(char *p, const char *k, uint32_t hz) { return num(p, k, (int)udiv(hz, 1000), 3); }

static void say(xs_t *s, char *p) {
  *p = 0;
  s->api->say(s->line);
}

static int rssi(const k6_api_t *api) { return KM_RSSI_DBM(api->fd_read(0x67)); }
static int key(const k6_api_t *api) { return api->level >= 4 ? api->key_poll() : api->key_down(); }
#define XS_CONFIRMS 6

static uint8_t st8(int dbm) { return (uint8_t)(dbm < -174 ? 0 : dbm > 81 ? 255 : dbm + 174); }
static int dbm8(uint8_t v) { return (int)v - 174; }

void xs_defaults(xs_t *s) {
  uint8_t *d = (uint8_t *)s;
  uint32_t i;
  for (i = 0; i < sizeof *s; i++) d[i] = 0;
  s->wide = 1, s->noise_db = 6, s->hit_db = 10, s->refine = 1;
  s->quick_us = 1500, s->confirm_us = 12000, s->hot_ms = 100, s->spur_s = 60;
  s->refloor_s = 60;
}

static uint32_t cols_of(const xs_seg_t *g) { return g->nbins < XS_COLS ? g->nbins : XS_COLS; }

uint32_t xs_mem_bytes(const xs_t *s) {
  uint32_t total = 0, most = 0;
  int g;
  for (g = 0; g < s->nseg && g < XS_NSEG; g++) {
    total += s->seg[g].nbins;
    if (cols_of(&s->seg[g]) > most) most = cols_of(&s->seg[g]);
  }
  return total + most;
}

int xs_init(xs_t *s, const k6_api_t *api, ex_t *ex, uint8_t *mem, uint32_t bytes) {
  uint32_t total = 0, i;
  int g;
  if (!s->nseg || s->nseg > XS_NSEG || s->confirm_us <= s->quick_us) return -11;
  for (g = 0; g < s->nseg; g++) {
    if (s->seg[g].nbins < 8 || !s->seg[g].step_hz) return -11;
    total += s->seg[g].nbins;
  }
  if (total > FLMAX || !mem || bytes < xs_mem_bytes(s)) return -8;
  s->api = api, s->ex = ex;
  s->fl = mem, s->row = mem + total;
  for (i = 0; i < bytes; i++) mem[i] = 0;
  {
    uint8_t *h = (uint8_t *)s->hot;
    for (i = 0; i < sizeof s->hot; i++) h[i] = 0;
  }
  s->sweeps = s->bins = 0;
  s->phase = XS_FLOORS, s->g = 0, s->b = 0, s->base = 0;
  return 0;
}

/* (tune and) wait until confirm_us after the tune (spent us of it gone), the mean of 2 reads */
static int full(xs_t *s, uint32_t hz, uint32_t spent) {
  const k6_api_t *api = s->api;
  int r;
  if (!spent) api->tune(hz, s->wide);
  api->delay_us(s->confirm_us - spent);
  r = rssi(api);
  api->delay_us(1000);
  return (r + rssi(api)) >> 1;
}

int xs_level(xs_t *s, uint32_t hz) { return full(s, hz, 0); }

/* refine a hit near hz: narrow reads 5 ms after the tune every 2.5 kHz over +-(step + 5 kHz);
   the middle of those within 3 dB of the loudest, snapped to snap_hz */
static uint32_t refine(xs_t *s, uint32_t hz, int g) {
  const k6_api_t *api = s->api;
  int8_t r[31];
  int best = -128, i, k = (int)udiv(s->seg[g].step_hz + 5000, 2500), n = 0;
  uint32_t sum = 0, c, snap = s->seg[g].snap_hz;
  if (k > 15) k = 15;
  hz -= 2500u * (uint32_t)k;
  for (i = 0; i <= 2 * k; i++) {
    int v;
    api->tune(hz + 2500u * (uint32_t)i, 0);
    api->delay_us(5000);
    v = rssi(api) + 46;
    r[i] = (int8_t)(v > 127 ? 127 : v);
    if (r[i] > best) best = r[i];
  }
  for (i = 0; i <= 2 * k; i++)
    if (r[i] >= best - 3) sum += 2500u * (uint32_t)i, n++;
  c = hz + udiv(sum, (uint32_t)n);
  if (snap) c = udiv(c + snap / 2, snap) * snap;
  return c;
}

static xs_hot_t *near(xs_t *s, uint32_t hz, uint32_t tol, int up) {
  xs_hot_t *h = s->hot;
  int i;
  for (i = 0; i < XS_HOT; i++, h++)
    if (h->hz && (!up || h->up) && (h->hz > hz ? h->hz - hz : hz - h->hz) <= tol) return h;
  return 0;
}

static void starts(xs_hot_t *h, uint32_t now, int lvl, int r) {
  h->up = now | 1, h->last = now, h->overs++, h->miss = 0;
  h->lvl = st8(lvl), h->lo = h->hi = st8(r);
}

static void ends(xs_t *s, xs_hot_t *h) {
  char *p;
  uint32_t a = h->up, dur = h->last > a ? h->last - a : 0;
  h->on += dur, h->up = 0;
  if (h->overs == 1 || h->lvl < h->olo) h->olo = h->lvl;
  if (h->overs == 1 || h->lvl > h->ohi) h->ohi = h->lvl;
  if (s->ex) ex_over(s->ex, h->hz, a, dur, dbm8(h->lvl));
  p = num(fq(s->line, "H ", h->hz), " ", (int)dur, 0);
  p = num(p, " ", dbm8(h->lvl), 0);
  say(s, num(p, " a", (int)a, 0));
}

/* a free entry, else the one down longest, else the weakest if this hit is hit_db louder */
static xs_hot_t *slot(xs_t *s, int lvl) {
  xs_hot_t *h = s->hot, *o = 0, *w = 0;
  int i;
  for (i = 0; i < XS_HOT; i++, h++) {
    if (!h->hz) return h;
    if (!h->up && !h->spur && (!o || h->last < o->last)) o = h;
    if (!w || h->lvl < w->lvl) w = h;
  }
  return o ? o : st8(lvl) >= w->lvl + s->hit_db ? w : 0;
}

/* h passes for a spur: the revisits leave it, it's never in xs_up, its slot stays taken (so it
   isn't found again) */
static void spur(xs_t *s, xs_hot_t *h) {
  h->spur = 1;
  if (s->ex) ex_spur(s->ex, h->hz, dbm8(h->lvl));
  say(s, num(fq(s->line, "X ", h->hz), " ", dbm8(h->lvl), 0));
}

static int new_hit(xs_t *s, int g, uint32_t b, uint32_t hz, int lvl, int r) {
  uint32_t c = hz, half = s->seg[g].step_hz >> 1;
  xs_hot_t *h = near(s, hz, half, 0);
  char *p;
  int i;
  if (!h) {
    if (!slot(s, lvl)) return 0;
    if (s->refine) {
      c = refine(s, hz, g);
      h = near(s, c, half, 0);
      if (!h) lvl = full(s, c, 0);
    }
  }
  if (h) {
    if (!h->up && !h->spur) starts(h, s->api->ms(), lvl, r);
    return 1;
  }
  if (!(h = slot(s, lvl))) return 0;
  {
    uint8_t *d = (uint8_t *)h;
    for (i = 0; i < (int)sizeof *h; i++) d[i] = 0;
  }
  h->hz = c, h->bin = (uint16_t)b;
  starts(h, s->api->ms(), lvl, r);
  if (s->news < 255) s->news++;
  p = num(fq(s->line, "N ", c), " ", lvl, 0);
  p = num(p, " fl", dbm8(s->sfl[g]), 0);
  say(s, num(p, " b", (int)b, 0));
  /* the K6's own clock: a spur at once, not after spur_s up (bench 2: 144.000 listed a minute).
     It comes in through the antenna path like a station (birdie-path-1: the front end off took
     55 dB off both), so only its frequency gives it away */
  if (ex_own_clock(c)) spur(s, h);
  return 1;
}

static void revisit(xs_t *s) {
  const k6_api_t *api = s->api;
  xs_hot_t *h = s->hot;
  int i;
  for (i = 0; i < XS_HOT; i++, h++) {
    uint32_t now;
    int r;
    uint8_t v;
    if (!h->hz || h->spur) continue;
    api->tune(h->hz, s->wide);
    api->delay_us(s->quick_us);
    r = rssi(api);
    now = api->ms();
    v = st8(r);
    if (r >= dbm8(s->fl[h->bin]) + s->hit_db) {
      if (!h->up) {
        starts(h, now, full(s, 0, s->quick_us), r);
        continue;
      }
      h->last = now, h->miss = 0;
      if (v < h->lo) h->lo = v;
      if (v > h->hi) {
        /* louder than at its start (a transmitter still coming up): read its level again, so
           the over carries its real level (the skirt cue compares them) */
        int l = full(s, 0, s->quick_us);
        h->hi = v;
        if (st8(l) > h->lvl) h->lvl = st8(l);
      }
      if (now - h->up > (uint32_t)s->spur_s * 1000u && h->hi - h->lo <= SPUR_DB) {
        spur(s, h);
      }
    } else if (h->up && ++h->miss >= 2) {
      ends(s, h);
    }
  }
  s->rev = api->ms();
}

/* segment g's floors: full and quick reads on 8 of its bins, off the round frequencies (one
   segment a step: about 130 ms). Each quick read is one bin over a full read's settled tune, as in
   the sweep: straight after the 1/8-segment hop it read 40 dB low on air (q-134, bench 3). */
static void floors(xs_t *s, int g) {
  const k6_api_t *api = s->api;
  uint32_t i, step = s->seg[g].step_hz;
  char *p;
  {
    uint32_t k = (uint32_t)s->seg[g].nbins >> 3, f = s->seg[g].lo_hz + ((k >> 1) + 3) * step;
    uint8_t a[2] = {255, 255}, b[2] = {255, 255}, rd[8];
    for (i = 0; i < 16; i++) {
      uint8_t v;
      int j = !(i & 1);
      if (j) {
        v = rd[i >> 1] = st8(full(s, f, 0));
      } else {
        api->tune(f + step, s->wide);
        api->delay_us(s->quick_us);
        v = st8(rssi(api));
        f += k * step;
      }
      if (v < a[j]) b[j] = a[j], a[j] = v;
      else if (v < b[j]) b[j] = v;
    }
    s->qfl[g] = (uint8_t)(b[0] + 3), s->sfl[g] = b[1];
    s->fl_t[g] = api->ms();
    p = num(num(num(s->line, "F", g, 0), " ", dbm8(b[1]), 0), " q", dbm8(b[0]), 0);
    *p++ = ':';
    for (i = 0; i < 8; i++) p = num(p, " ", dbm8(rd[i]), 0);
    say(s, p);
  }
}

/* Read segment g's floor again before its sweep, every refloor_s. A strong carrier pulls the
   reads down while it's up (desense: the bench's own -23 dBm ID did it in explorer-scan-6), and
   the floors follow them down but have nothing to bring them back while the band is quiet, so a
   bin at the old floor looked 13 dB up for 20 minutes. Not while a carrier 30 dB over the floor
   is up in the segment (that's when the reads are low), unless that has put it off 4 times. A
   spur doesn't count: it's up for good (the 144.000 birdie held 2 m's floor off for 4 minutes). */
static int refloor_due(xs_t *s) {
  const xs_seg_t *G = &s->seg[s->g];
  uint32_t age = s->api->ms() - s->fl_t[s->g], every = s->refloor_s * 1000u, top = G->lo_hz + G->nbins * G->step_hz;
  const xs_hot_t *h = s->hot;
  int i;
  if (!every || age < every) return 0;
  if (age >= 4 * every) return 1;
  for (i = 0; i < XS_HOT; i++, h++)
    if (h->up && !h->spur && h->hz >= G->lo_hz && h->hz < top && h->lvl >= s->sfl[s->g] + 30) return 0;
  return 1;
}

/* no bin's floor more than 6 dB under the segment's quick floor just read. A floor 15 dB or more
   under the last one is a strong carrier's desense, not a quieter band: bench 4 (21:09) read 70 cm
   at -139 for -109 while a -18 dBm ID was still up after a listen, and five phantoms (444.550 at
   -116) came up against it. That read is held once and tried again in 5 s; a second one stands. */
static void refloor(xs_t *s) {
  uint8_t *fl = s->fl + s->base, lo, osf = s->sfl[s->g], oqf = s->qfl[s->g], bit = (uint8_t)(1u << s->g);
  uint32_t b;
  floors(s, s->g);
  if (s->sfl[s->g] + XS_DESENSE_DB <= osf && !(s->held & bit)) {
    s->held |= bit;
    s->sfl[s->g] = osf, s->qfl[s->g] = oqf;
    s->fl_t[s->g] = s->api->ms() - s->refloor_s * 1000u + 5000u;
    say(s, num(num(s->line, "F", s->g, 0), " held ", dbm8(osf), 0));
    return;
  }
  s->held &= (uint8_t)~bit;
  lo = (uint8_t)(s->qfl[s->g] > 9 ? s->qfl[s->g] - 9 : 0);
  for (b = 0; b < s->seg[s->g].nbins; b++)
    if (fl[b] < lo) fl[b] = lo;
}

/* XS_BURST new hits in one sweep of a segment: its floor rose under it, not a crowd keying up at
   once. Bench 6 (2026-11-07 22:32-22:38): with the serial cable unplugged and plugged back in, 55
   one-over hits 2-5 dB over the floor filled the list before the minute's refloor. Read the floor
   now, and let this sweep's new entries go quietly (no H line, no candidate) unless they still
   stand hit_db over it. */
static void burst(xs_t *s) {
  const xs_seg_t *G = &s->seg[s->g];
  uint32_t top = G->lo_hz + G->nbins * G->step_hz;
  xs_hot_t *h = s->hot;
  int i, n = 0;
  refloor(s);
  for (i = 0; i < XS_HOT; i++, h++)
    if (h->hz >= G->lo_hz && h->hz < top && h->up && !h->spur && h->overs == 1 &&
        h->up - s->sweep_t0 < 0x80000000u && h->lvl < s->sfl[s->g] + s->hit_db) {
      uint8_t *d = (uint8_t *)h;
      unsigned k;
      for (k = 0; k < sizeof *h; k++) d[k] = 0;
      n++;
    }
  say(s, num(num(num(s->line, "B", s->g, 0), " ", s->news, 0), " drop ", n, 0));
}

/* the start of segment g's sweep: clear its row and park on its first bin */
static void sweep_start(xs_t *s) {
  const xs_seg_t *G = &s->seg[s->g];
  uint32_t i;
  s->cols = (uint8_t)cols_of(G);
  for (i = 0; i < s->cols; i++) s->row[i] = 0;
  s->b = 0, s->hz = G->lo_hz, s->conf = s->hits = s->col = s->acc = 0, s->news = 0;
  s->sf = (int16_t)dbm8(s->sfl[s->g]);
  s->sweep_t0 = s->api->ms();
  s->api->tune(s->hz, s->wide);
  s->api->delay_us(s->confirm_us);
}

static void sweep_end(xs_t *s) {
  char *p;
  uint32_t t = s->api->ms() - s->sweep_t0;
  s->bins += s->seg[s->g].nbins;
  s->last_ms = (uint16_t)(t > 65535 ? 65535 : t);
  s->last_g = s->g;
  if (s->say_rows) {
    uint32_t b;
    for (b = 0; b < s->cols;) {
      p = num(num(s->line, "R", s->g, 0), " ", (int)b, 0);
      *p++ = ' ';
      for (; b < s->cols && p - s->line < 58; b++) *p++ = (char)('0' + s->row[b]);
      say(s, p);
    }
  }
  if (!s->quiet) {
    p = num(num(s->line, "S", s->g, 0), " ", (int)s->sweeps, 0);
    p = num(p, " ", (int)t, 0);
    p = num(p, " c", (int)s->conf, 0);
    say(s, num(p, " h", (int)s->hits, 0));
  }
  s->base = (uint16_t)(s->base + s->seg[s->g].nbins);
  if (++s->g >= s->nseg) s->g = 0, s->base = 0, s->sweeps++;
}

int xs_step(xs_t *s) {
  const k6_api_t *api = s->api;
  const xs_seg_t *G;
  uint8_t *fl;
  uint32_t n, end, step, nc = 0;
  if (key(api)) return XS_KEY;
  if (s->phase == XS_FLOORS) {
    floors(s, s->g);
    if (++s->g < s->nseg) return XS_RUN;
    s->t0 = s->rev = api->ms();
    s->phase = XS_SWEEP;
    s->g = 0, s->base = 0;
    sweep_start(s);
    return XS_RUN;
  }
  if (!s->b && refloor_due(s)) {
    refloor(s);
    sweep_start(s);
    return XS_RUN;
  }
  G = &s->seg[s->g];
  fl = s->fl + s->base;
  n = G->nbins, step = G->step_hz;
  if (api->ms() - s->rev >= s->hot_ms) revisit(s);
  end = s->b + XS_BLOCK;
  if (end > n) end = n;
  for (; s->b < end; s->b++, s->hz += step) {
    uint32_t b = s->b, hz = s->hz;
    int r, v, f = fl[b];
    api->tune(hz, s->wide);
    api->delay_us(s->quick_us);
    r = st8(rssi(api));
    if (!s->sweeps) {
      fl[b] = (uint8_t)(r < s->qfl[s->g] ? r : s->qfl[s->g]);
    } else if (r < f) {
      fl[b] = (uint8_t)(f - ((f - r + 1) >> 1));
    } else if (r < f + s->noise_db) {
      if ((s->sweeps & 7) == (b & 7) && f < 255) fl[b] = (uint8_t)(f + 1);
    } else if (!near(s, hz, step + (step >> 1), 1)) {
      int lvl = full(s, hz, s->quick_us), sf = s->sf;
      s->conf++;
      if (++nc >= XS_CONFIRMS) end = s->b + 1;   /* 12 ms each: a step holds at most a few */
      if (lvl >= sf + s->hit_db) {
        s->hits++, end = s->b + 1;   /* a hit's refine is ~100 ms: end the step here */
        if (!new_hit(s, s->g, s->base + b, hz, lvl, dbm8((uint8_t)r))) fl[b] = (uint8_t)(r - 3);
        r = lvl - sf + f;
      } else {
        if (lvl < sf) {
          if (lvl >= sf - 6) sf--;
        } else if ((s->conf & 3) == 0) {
          sf++;
        }
        s->sf = (int16_t)sf;
        s->sfl[s->g] = st8(sf);
        fl[b] = (uint8_t)(r - 3);
      }
    }
    v = r - fl[b];
    v = v < 0 ? 0 : v > 63 ? 63 : v;
    if (v > s->row[s->col]) s->row[s->col] = (uint8_t)v;
    s->acc += s->cols;
    while (s->acc >= n && s->col + 1 < s->cols) s->acc -= n, s->col++;
  }
  if (s->b < n) return XS_RUN;
  if (s->news >= XS_BURST) burst(s);
  sweep_end(s);
  sweep_start(s);
  return XS_ROW;
}

void xs_resume(xs_t *s) {
  if (s->phase != XS_SWEEP) return;
  refloor(s);
  sweep_start(s);
}

void xs_close(xs_t *s) {
  xs_hot_t *h = s->hot;
  int i;
  for (i = 0; i < XS_HOT; i++, h++)
    if (h->hz && h->up) h->last = s->api->ms(), ends(s, h);
}

void xs_summary(xs_t *s) {
  xs_hot_t *h = s->hot;
  uint32_t t, ret = 0;
  int i;
  char *p;
  for (i = 0; i < XS_HOT; i++, h++) {
    if (!h->hz) continue;
    if (!h->spur) ret++;
    p = num(fq(s->line, "T ", h->hz), " n", h->overs, 0);
    p = num(p, " t", (int)udiv(h->on + 500, 1000), 0);
    p = num(p, " ", dbm8((uint8_t)((h->olo + h->ohi + 1) >> 1)), 0);
    p = num(p, " d", h->ohi - h->olo, 0);
    say(s, h->spur ? put(p, " s") : p);
  }
  t = s->api->ms() - s->t0;
  p = num(s->line, "Z sw=", (int)s->sweeps, 0);
  p = num(p, " bins=", (int)s->bins, 0);
  p = num(p, " ms=", (int)t, 0);
  p = num(p, " bps=", (int)udiv(s->bins * 1000u, t | 1), 0);
  say(s, num(p, " hot=", (int)ret, 0));
}

uint32_t xs_state_bytes(void) { return sizeof(xs_t); }

void xs_info(const xs_t *s, xs_info_t *o) {
  const xs_hot_t *h = s->hot;
  int i;
  o->sweeps = s->sweeps, o->bins = s->bins;
  o->ms = s->phase == XS_SWEEP ? s->api->ms() - s->t0 : 0;
  o->last_ms = s->last_ms, o->g = s->g, o->phase = s->phase;
  for (i = 0; i < XS_NSEG; i++) o->floor_dbm[i] = (int16_t)(i < s->nseg ? dbm8(s->sfl[i]) : 0);
  o->hot = o->up = 0, o->pad[0] = o->pad[1] = 0;
  for (i = 0; i < XS_HOT; i++, h++) {
    if (!h->hz || h->spur) continue;
    o->hot++;
    if (h->up) o->up++;
  }
}

/* up now, loudest first, not a spur, and not within XS_SKIRT_HZ of one up XS_SKIRT_DB louder: the bench's
   -24 dBm ID showed up at 440.900 too (-81), and that's where the automatic listener went. And
   XS_PICK_DB over its segment's floor: bench 5 (21:37) sat 2 minutes on a steady 440.7375 only
   10 dB up (-103, fl-113), too weak for the tone detector anyway, and missed the keeper's ID.
   XS_OFFGRID_DB more off the band plan's raster (Jeff 2026-11-07): every 12.5 kHz-offset hit on
   70 cm so far was junk (skirts, 440.7375, the band edge), but splinter channels exist */
#define XS_SKIRT_HZ 100000u
#define XS_SKIRT_DB 20
#define XS_PICK_DB 15
#define XS_OFFGRID_DB 10
static int floor_of(const xs_t *s, uint32_t hz) {
  int g;
  for (g = 0; g < s->nseg; g++)
    if (hz >= s->seg[g].lo_hz && hz - s->seg[g].lo_hz < (uint32_t)s->seg[g].nbins * s->seg[g].step_hz)
      return s->sfl[g];
  return 0;
}

unsigned xs_up(const xs_t *s, uint32_t *out, unsigned max) {
  const xs_hot_t *up[XS_HOT];
  unsigned i, j, n = 0;
  for (i = 0; i < XS_HOT; i++) {
    const xs_hot_t *h = &s->hot[i], *o;
    if (!h->hz || !h->up || h->spur || h->lvl < floor_of(s, h->hz) + XS_PICK_DB + (ex_on_raster(h->hz) ? 0 : XS_OFFGRID_DB))
      continue;
    for (j = 0, o = s->hot; j < XS_HOT; j++, o++)
      if (o != h && o->hz && o->up && (o->hz > h->hz ? o->hz - h->hz : h->hz - o->hz) <= XS_SKIRT_HZ &&
          o->lvl >= h->lvl + XS_SKIRT_DB)
        break;
    if (j < XS_HOT) continue;
    /* loudest first (Jeff 2026-11-07: listen to the strongest first) */
    for (j = n++; j && up[j - 1]->lvl < h->lvl; j--) up[j] = up[j - 1];
    up[j] = h;
  }
  if (n > max) n = max;
  for (i = 0; i < n; i++) out[i] = up[i]->hz;
  return n;
}
