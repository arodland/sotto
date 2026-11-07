/* scan: Phase 1 of the K6 signal explorer, the fast two-stage sweep (docs/k6-scan.md, the
   explorer design). Receive only: tune is the way to move, no register is written.

   Up to 4 segments, swept in turn until max_s or a key. Each bin of a segment:
     1 coarse   tune, wait quick_us, read RSSI once (REG 0x67).
     2 noise    each bin keeps its own floor of those quick reads (it falls halfway to a lower
                read, and rises 1 dB every 8 sweeps while the bin reads within noise_db of it).
                Phase 0: at 1.5 ms wide a carrier reads 35 dB over an empty channel read at the
                same moment after the tune, so the floor is a quick read too, never the settled
                level. Under floor + noise_db: next bin. The first sweep only learns (each floor
                capped 3 dB over the segment's quick floor, so a carrier up all along still shows).
     3 confirm  wait on to confirm_us after the tune and read twice, 1 ms apart: a hit if that is
                hit_db over the segment's settled floor (the second lowest of 8 bins' full reads at
                the start, as its quick floor is of their quick reads; it follows every confirm
                that fails).
     4 refine   (flags bit 1) a hit with no entry within half a step: narrow reads 5 ms after the
                tune every 2.5 kHz over +-(step + 5 kHz); the middle of those within 3 dB of the
                loudest, snapped to snap_hz, and a full read there for its level. Bins within 1.5
                steps of an entry that's up skip the confirm (its skirt in the wide filter).
     5 revisit  hits go on a hot list of HOT entries that the sweep visits between blocks of
                BLOCK bins, every hot_ms: a quick read says up or down (hit_db over the bin's
                quick floor). An over starts with a full read (its level); it ends after 2 down
                reads, with one H line. Up longer than spur_s within 3 dB (quick reads): a spur
                (X line), never visited again.
   Lines (the serial report, K6 MOD say ...; the telemetry keeps the newest 684 bytes, so the
   summary comes last):
     F0 -137 q-160: -137 -136 ...           a segment's settled and quick floors at the start,
                                            then its 8 full reads
     N 146.940 -71 fl-137 z0 b123           a new hit: centre, level, segment floor, REG 0x63, bin
     H 146.940 2030 -71 n3                  an over ended: centre, ms up, level, overs so far
     X 146.940 -61                          a spur
     R0 0 0000a30000...                     (flags bit 2) a sweep's row, max-pooled to cols
                                            columns (0: one a bin): '0' + dB over the floor, 0..63
     S0 12 702 c14 h1                       segment 0's sweep 12: 702 ms, 14 confirms, 1 hit
     T 146.940 n3 t5 -71 d2                 the summary, one an entry: overs, s up, middle level of
                                            the overs, their spread in dB; " s" = a spur
     Z sw=40 bins=16000 ms=30010 bps=533 hot=3      last: sweeps, bins, ms, bins a second
   Memory: the state, hot list and row in the scratch RAM; the floors (a byte a bin, FLMAX bins
   in all) on the stack: Phase 0's run measured 3,504 B of stack room at a module's call, and
   this module takes about 1.2 KB of it.
   Returns the hot entries that aren't spurs, or -3 (an image below level 2), -8 (more than
   FLMAX bins, or scratch too small for the row), -11 (a bad parameter block). A key ends the
   run normally.
   CI builds it as mod-scan.bin; tools/k6_scan.py patches the parameter block before it signs.
   No libraries, no division, no writable statics. SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 2
#define K6_MODULE_OPT z
#include "../k6_mod.h"

#define NSEG 4
/* byte fields first: Thumb-1 loads reach bytes at offsets below 32 in one instruction */
typedef struct {
  char magic[8];          /*  0 "SWEEP v1": tools/k6_scan.py finds the block by it */
  uint8_t nseg;           /*  8 */
  uint8_t wide;           /*  9 1 wide filter, 0 narrow (coarse, confirm, revisit) */
  uint8_t noise_db;       /* 10 */
  uint8_t hit_db;         /* 11 */
  uint8_t cols;           /* 12 R line columns (0: one a bin) */
  uint8_t flags;          /* 13 bit 1 refine, bit 2 R lines (bit 0 spare) */
  uint8_t spare[2];       /* 14 */
  uint16_t quick_us;      /* 16 coarse read, after the tune */
  uint16_t confirm_us;    /* 18 full read, after the tune */
  uint16_t max_s;         /* 20 run length (0: until a key) */
  uint16_t hot_ms;        /* 22 revisit interval */
  uint16_t spur_s;        /* 24 */
  uint16_t nbins[NSEG];   /* 26 */
  uint16_t snap_hz[NSEG]; /* 34 channel raster a refined hit snaps to (0: none) */
  uint16_t pad;           /* 42 */
  uint32_t lo_hz[NSEG];   /* 44 a segment's first bin */
  uint32_t step_hz[NSEG]; /* 60 its raster */
} par_t;                  /* 76 */

/* Defaults: 2 m, 144-148 MHz at 10 kHz, wide, 1.5 ms quick reads, 60 s */
static const volatile par_t PAR = {
    {'S', 'W', 'E', 'E', 'P', ' ', 'v', '1'}, 1, 1, 6, 10, 128, 6, {0, 0}, 1500, 12000, 60, 100, 60,
    {400}, {5000}, 0, {144000000u}, {10000u}};

#define HOT 11
#define FLMAX 1024      /* bins, all segments: their floors live on the stack (3.5 KB free at the call) */
#define BLOCK 32
#define CLEAR_DBM (-100)
#define SPUR_DB 3

typedef struct {
  uint8_t lvl, lo, hi;    /* this over: its level, quick reads' lowest and highest (dBm + 174) */
  uint8_t olo, ohi;       /* every over's level: lowest, highest */
  uint8_t miss, spur, pad;
  uint16_t bin, overs;    /* the coarse bin (all segments counted), for its quick floor */
  uint32_t hz;            /* centre (0: a free entry) */
  uint32_t last, up;      /* ms last seen up; ms this over started (0: down) */
  uint32_t on;            /* ms up, overs that ended */
} hot_t;                  /* 28 */

typedef struct {
  par_t q;
  const k6_api_t *api;
  hot_t *hot;
  uint8_t *fl;            /* quick floor a bin, dBm + 174, all segments (on the stack) */
  uint8_t *row;
  uint32_t sweeps, bins, rev;
  uint8_t sfl[NSEG];      /* settled floor a segment */
  uint8_t qfl[NSEG];      /* the first sweep's cap on a bin's quick floor */
  char line[64];
} sw_t;

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
  while (d < n && !(d & 0x80000000u)) d <<= 1, bit <<= 1;
  while (bit) {
    if (n >= d) n -= d, q |= bit;
    d >>= 1, bit >>= 1;
  }
  return q;
}

static char *fq(char *p, const char *k, uint32_t hz) { return num(p, k, (int)udiv(hz, 1000), 3); }

static void say(sw_t *s, char *p) {
  *p = 0;
  s->api->say(s->line);
}

static int rssi(const k6_api_t *api) { return KM_RSSI_DBM(api->fd_read(0x67)); }
static int key(const k6_api_t *api) { return api->level >= 4 ? api->key_poll() : api->key_down(); }
static uint8_t st8(int dbm) { return (uint8_t)(dbm < -174 ? 0 : dbm > 81 ? 255 : dbm + 174); }
static int dbm8(uint8_t v) { return (int)v - 174; }

/* (tune and) wait until confirm_us after the tune (spent us of it gone), the mean of 2 reads */
static int full(sw_t *s, uint32_t hz, uint32_t spent) {
  const k6_api_t *api = s->api;
  int r;
  if (!spent) api->tune(hz, s->q.wide);
  api->delay_us(s->q.confirm_us - spent);
  r = rssi(api);
  api->delay_us(1000);
  return (r + rssi(api)) >> 1;
}

/* refine a hit near hz: narrow reads 5 ms after the tune every 2.5 kHz over +-(step + 5 kHz)
   (+-37.5 kHz at most); the middle of those within 3 dB of the loudest, snapped to snap_hz */
static uint32_t refine(sw_t *s, uint32_t hz, int g) {
  const k6_api_t *api = s->api;
  int8_t r[31];
  int best = -128, i, k = (int)udiv(s->q.step_hz[g] + 5000, 2500), n = 0;
  uint32_t sum = 0, c, snap = s->q.snap_hz[g];
  if (k > 15) k = 15;
  hz -= 2500u * (uint32_t)k;
  for (i = 0; i <= 2 * k; i++) {
    int v;
    api->tune(hz + 2500u * (uint32_t)i, 0);
    api->delay_us(5000);
    v = rssi(api) + 46;            /* -174..-47 dBm in an int8 */
    r[i] = (int8_t)(v > 127 ? 127 : v);
    if (r[i] > best) best = r[i];
  }
  for (i = 0; i <= 2 * k; i++)
    if (r[i] >= best - 3) sum += 2500u * (uint32_t)i, n++;
  c = hz + udiv(sum, (uint32_t)n);
  if (snap) c = udiv(c + snap / 2, snap) * snap;
  return c;
}

/* an entry within tol of hz (up: only one that's up) */
static hot_t *near(sw_t *s, uint32_t hz, uint32_t tol, int up) {
  hot_t *h = s->hot;
  int i;
  for (i = 0; i < HOT; i++, h++)
    if (h->hz && (!up || h->up) && (h->hz > hz ? h->hz - hz : hz - h->hz) <= tol) return h;
  return 0;
}

static void starts(hot_t *h, uint32_t now, int lvl, int r) {
  h->up = now | 1, h->last = now, h->overs++, h->miss = 0;
  h->lvl = st8(lvl), h->lo = h->hi = st8(r);
}

static void ends(sw_t *s, hot_t *h) {
  char *p;
  uint32_t dur = h->last > h->up ? h->last - h->up : 0;
  h->on += dur, h->up = 0;
  if (h->overs == 1 || h->lvl < h->olo) h->olo = h->lvl;
  if (h->overs == 1 || h->lvl > h->ohi) h->ohi = h->lvl;
  p = num(fq(s->line, "H ", h->hz), " ", (int)dur, 0);
  p = num(p, " ", dbm8(h->lvl), 0);
  say(s, num(p, " n", h->overs, 0));
}

/* a confirmed bin: an entry's next over, or (refined, read again at its centre) a new entry */
static void new_hit(sw_t *s, int g, uint32_t b, uint32_t hz, int lvl, int r) {
  uint32_t c = hz, half = s->q.step_hz[g] >> 1;
  hot_t *h = near(s, hz, half, 0), *o = 0;
  char *p;
  int i;
  if (!h && (s->q.flags & 2)) {
    c = refine(s, hz, g);
    h = near(s, c, half, 0);
    if (!h) lvl = full(s, c, 0);
  }
  if (h) {
    if (!h->up && !h->spur) starts(h, s->api->ms(), lvl, r);
    return;
  }
  /* a free entry, else the one down longest (spurs stay: they keep their bins quiet) */
  for (i = 0, h = s->hot; i < HOT; i++, h++) {
    if (!h->hz) break;
    if (!h->up && !h->spur && (!o || h->last < o->last)) o = h;
  }
  if (i == HOT && !(h = o)) return;
  {
    uint8_t *d = (uint8_t *)h;
    for (i = 0; i < (int)sizeof *h; i++) d[i] = 0;
  }
  h->hz = c, h->bin = (uint16_t)b;
  starts(h, s->api->ms(), lvl, r);
  p = num(fq(s->line, "N ", c), " ", lvl, 0);
  p = num(p, " fl", dbm8(s->sfl[g]), 0);
  p = num(p, " z", s->api->fd_read(0x63), 0);
  say(s, num(p, " b", (int)b, 0));
}

static void revisit(sw_t *s) {
  const k6_api_t *api = s->api;
  hot_t *h = s->hot;
  int i;
  for (i = 0; i < HOT; i++, h++) {
    uint32_t now;
    int r;
    uint8_t v;
    if (!h->hz || h->spur) continue;
    api->tune(h->hz, s->q.wide);
    api->delay_us(s->q.quick_us);
    r = rssi(api);
    now = api->ms();
    v = st8(r);
    if (r >= dbm8(s->fl[h->bin]) + s->q.hit_db) {
      if (!h->up) {
        starts(h, now, full(s, 0, s->q.quick_us), r);
        continue;
      }
      h->last = now, h->miss = 0;
      if (v < h->lo) h->lo = v;
      if (v > h->hi) h->hi = v;
      if (now - h->up > (uint32_t)s->q.spur_s * 1000u && h->hi - h->lo <= SPUR_DB) {
        h->spur = 1;
        say(s, num(fq(s->line, "X ", h->hz), " ", dbm8(h->lvl), 0));
      }
    } else if (h->up && ++h->miss >= 2) {
      ends(s, h);
    }
  }
  s->rev = api->ms();
}

/* one sweep of segment g, its bins' floors from fl. 1: a key */
static int sweep(sw_t *s, int g, uint8_t *fl) {
  const k6_api_t *api = s->api;
  uint32_t b, n = s->q.nbins[g], hz = s->q.lo_hz[g], step = s->q.step_hz[g], t0 = api->ms();
  uint32_t cols = s->q.cols, col = 0, acc = 0, conf = 0, hits = 0;
  int sf = dbm8(s->sfl[g]);
  char *p;
  if (!cols || cols > n) cols = n;
  for (b = 0; b < cols; b++) s->row[b] = 0;
  /* park on the first bin first: the first run (2026-11-07) read bin 0 hot after every wrap from
     the segment's top, a 4 MHz jump on 2 m */
  api->tune(hz, s->q.wide);
  api->delay_us(s->q.confirm_us);
  for (b = 0; b < n; b++, hz += step) {
    int r, v, f = fl[b];
    if ((b & (BLOCK - 1)) == 0) {
      if (key(api)) return 1;
      if (api->ms() - s->rev >= s->q.hot_ms) revisit(s);
    }
    api->tune(hz, s->q.wide);
    api->delay_us(s->q.quick_us);
    r = st8(rssi(api));
    if (!s->sweeps) {
      fl[b] = (uint8_t)(r < s->qfl[g] ? r : s->qfl[g]);
    } else if (r < f) {
      fl[b] = (uint8_t)(f - ((f - r + 1) >> 1));
    } else if (r < f + s->q.noise_db) {
      if ((s->sweeps & 7) == (b & 7) && f < 255) fl[b] = (uint8_t)(f + 1);
    } else if (!near(s, hz, step + (step >> 1), 1)) {   /* not an up entry's skirt */
      int lvl = full(s, hz, s->q.quick_us);
      conf++;
      if (lvl >= sf + s->q.hit_db) {
        hits++;
        new_hit(s, g, (uint32_t)(fl - s->fl) + b, hz, lvl, dbm8((uint8_t)r));
        r = lvl - sf + f;          /* the row shows a hit at its settled level over the floor */
      } else {
        if (lvl < sf) sf = lvl;
        else if ((conf & 3) == 0) sf++;
        s->sfl[g] = st8(sf);
      }
    }
    v = r - fl[b];
    v = v < 0 ? 0 : v > 63 ? 63 : v;
    if (v > s->row[col]) s->row[col] = (uint8_t)v;
    acc += cols;
    while (acc >= n && col + 1 < cols) acc -= n, col++;
  }
  s->bins += n;
  t0 = api->ms() - t0;
  if (s->q.flags & 4)
    for (b = 0; b < cols;) {
      p = num(num(s->line, "R", g, 0), " ", (int)b, 0);
      *p++ = ' ';
      for (; b < cols && p - s->line < 58; b++) *p++ = (char)('0' + s->row[b]);
      say(s, p);
    }
  p = num(num(s->line, "S", g, 0), " ", (int)s->sweeps, 0);
  p = num(p, " ", (int)t0, 0);
  p = num(p, " c", (int)conf, 0);
  say(s, num(p, " h", (int)hits, 0));
  return 0;
}

int k6_main(const k6_api_t *api) {
  uint8_t floors[FLMAX];
  sw_t *s;
  uint8_t *mem;
  const volatile uint8_t *src = (const volatile uint8_t *)&PAR;
  uint32_t room = 0, i, total = 0, most = 0, t0, ret = 0;
  int g, r;
  char *p;
  hot_t *h;
  if (api->level < 2) return -3;
  mem = (uint8_t *)api->scratch(&room);
  s = (sw_t *)mem;
  if (room < sizeof *s + HOT * sizeof(hot_t)) return -8;
  for (i = 0; i < sizeof *s + HOT * sizeof(hot_t); i++) mem[i] = 0;
  for (i = 0; i < sizeof s->q; i++) mem[i] = src[i];
  s->api = api;
  if (!s->q.nseg || s->q.nseg > NSEG || s->q.confirm_us <= s->q.quick_us) return -11;
  for (g = 0; g < s->q.nseg; g++) {
    if (s->q.nbins[g] < 8 || !s->q.step_hz[g]) return -11;
    total += s->q.nbins[g];
    if (s->q.nbins[g] > most) most = s->q.nbins[g];
  }
  if (s->q.cols && s->q.cols < most) most = s->q.cols;
  s->hot = (hot_t *)(mem + sizeof *s);
  s->fl = floors;
  s->row = (uint8_t *)(s->hot + HOT);
  if (total > FLMAX || sizeof *s + HOT * sizeof(hot_t) + most > room) return -8;
  /* the upload's own carrier on the pager frequency: wait (up to 30 s) for it to drop */
  r = full(s, api->rx_hz(), 0) - 15;
  t0 = api->ms();
  while ((i = (uint32_t)rssi(api)), (int)i > r && (int)i > CLEAR_DBM && api->ms() - t0 < 30000) {
    if (key(api)) return 0;
    api->delay_us(10000);
  }
  api->delay_us(200000);
  /* each segment's floors: quick and full reads on 8 of its bins, off the round frequencies
     (the K6's clock harmonics), the second lowest of each. The first sweep caps a bin's quick
     floor 3 dB over the quick one, so a carrier that's up all through it still shows. */
  for (g = 0; g < s->q.nseg; g++) {
    uint32_t k = s->q.nbins[g] >> 3, f = s->q.lo_hz[g] + ((k >> 1) + 3) * s->q.step_hz[g];
    uint8_t a[2] = {255, 255}, b[2] = {255, 255};
    p = put(s->line + 20, ":");
    for (i = 0; i < 16; i++) {
      uint8_t v;
      int j = i & 1;
      if (!j) {
        api->tune(f, s->q.wide);
        api->delay_us(s->q.quick_us);
        v = st8(rssi(api));
      } else {
        v = st8(full(s, f, s->q.quick_us));
        p = num(p, " ", dbm8(v), 0);
        f += k * s->q.step_hz[g];
      }
      if (v < a[j]) b[j] = a[j], a[j] = v;
      else if (v < b[j]) b[j] = v;
    }
    s->qfl[g] = b[0] + 3, s->sfl[g] = b[1];
    {
      /* "F0 -137 q-160" then the 8 full reads, written past it above: close the gap */
      char *e = p, *src = s->line + 20;
      p = num(num(num(s->line, "F", g, 0), " ", dbm8(b[1]), 0), " q", dbm8(b[0]), 0);
      while (src < e) *p++ = *src++;
      say(s, p);
    }
  }
  t0 = s->rev = api->ms();
  for (;;) {
    uint8_t *fl = s->fl;
    for (g = 0; g < s->q.nseg; fl += s->q.nbins[g], g++)
      if (sweep(s, g, fl)) goto done;
    s->sweeps++;
    if (s->q.max_s && api->ms() - t0 >= (uint32_t)s->q.max_s * 1000u) break;
  }
done:
  for (i = 0, h = s->hot; i < HOT; i++, h++) {
    if (!h->hz) continue;
    if (h->up) h->last = api->ms(), ends(s, h);
    if (!h->spur) ret++;
    p = num(fq(s->line, "T ", h->hz), " n", h->overs, 0);
    p = num(p, " t", (int)udiv(h->on + 500, 1000), 0);
    p = num(p, " ", dbm8((uint8_t)((h->olo + h->ohi + 1) >> 1)), 0);
    p = num(p, " d", h->ohi - h->olo, 0);
    say(s, h->spur ? put(p, " s") : p);
  }
  t0 = api->ms() - t0;
  p = num(s->line, "Z sw=", (int)s->sweeps, 0);
  p = num(p, " bins=", (int)s->bins, 0);
  p = num(p, " ms=", (int)t0, 0);
  p = num(p, " bps=", (int)udiv(s->bins * 1000u, t0 | 1), 0);
  say(s, num(p, " hot=", (int)ret, 0));
  return (int)ret;
}
