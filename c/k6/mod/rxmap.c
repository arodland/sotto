/* rxmap: how far does the K6 really receive, and how well? A receive-only sweep of the FD6818
   (docs/k6-rxmap.md). Two modes, chosen by the parameter block below, which tools/k6_rxmap.py
   patches in the built .bin before it signs and sends it (hamlet has no clang):

   map   RSSI across a segment, bin by bin. In each bin it steps the receiver every step_hz with a
         quick read (quick_us after the tune; explorer Phase 0: at 3 ms wide a carrier already
         reads far over an empty channel at the same moment, so the quick reads rank the bin's
         channels), then reads the loudest and the quietest of them properly (full_us after the
         tune, the mean of 4 reads 1 ms apart): the bin's peak and its noise floor. Off-air
         signals (FM broadcast, NOAA, TV, pagers, cellular) are the test signals: where the K6's
         front-end filters cut, their peaks sink into the floor. Nothing is transmitted.
   cal   the calibrated points, with the IC-7100 as the signal: it keys a carrier on one point
         after another (2 m / 70 cm only, from tools/k6_rxmap.py cal). The K6 cycles the points
         with full reads; each carrier it sees above det_db over that point's floor becomes one
         line when it drops: mean and max level, the floor, the readings, and the FM noise
         indicator (REG_63) while it was up (0 = full quieting; it rises as the signal fades).

   Both start by waiting for the upload's own carrier on the pager frequency to drop. Lines (the
   serial report, K6 MOD say ...; with a reply-wanted upload the last 12 x 57 bytes also come back
   over the air as telemetry, so the summary comes last and fits that):
     map, while it runs (serial):   b 162.000 -131 -61 @162.550 z0000
     map, the summary (telemetry):  T 162.550:-61 98.100:-35 ...    (strongest over floor first)
                                    F0 <chars>                       (floor, one char a bin)
                                    P0 <chars>                       (peak)
                                    M lo=18.000 b=2000 n=160 s=25000 q=3000 sig=12 ms=55120
     cal:                           C 440.850:-131 146.550:-129 ... (the floors)
                                    c 440.850 -61 -60 fl-131 n8 z0
                                    K ev=6 ms=61000
   A bin's char is '0' + (dBm + 160) / 2, 0..63: '0' = -160 dBm or less, 'o' = -34 or more.
   Returns: map, the bins with a peak 10 dB or more over their floor; cal, the carriers seen.
   Errors: -2 a key, -3 an image below level 2, -6 cal saw no carrier, -8 scratch too small,
   -11 a bad parameter block. Writes no register (tune is the way to move; the core puts the
   pager's frequency and set-up back afterwards). SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 2
#define K6_MODULE_OPT z
#include "../k6_mod.h"

#define NCAL 16
typedef struct {
  char magic[8];      /*  0 "RXMAP v1": tools/k6_rxmap.py finds the block by it */
  uint32_t lo_hz;     /*  8 map: the first bin's first step */
  uint32_t bin_hz;    /* 12 */
  uint32_t step_hz;   /* 16 */
  uint16_t nbins;     /* 20 */
  uint16_t nsteps;    /* 22 steps a bin (bin_hz / step_hz) */
  uint16_t quick_us;  /* 24 */
  uint16_t full_us;   /* 26 */
  uint8_t mode;       /* 28 0 map, 1 cal */
  uint8_t wide;       /* 29 1 wide filter, 0 narrow */
  uint8_t tops;       /* 30 map: peaks listed in T lines */
  uint8_t det_db;     /* 31 cal: a carrier is this far over the point's floor */
  uint8_t ncal;       /* 32 cal points */
  uint8_t quiet_s;    /* 33 cal: ends this long after the last carrier */
  uint16_t max_s;     /* 34 cal: ends after this long in any case */
  uint32_t cal_hz[NCAL]; /* 36 */
} par_t;

/* volatile: the values are read from the image, never folded into the code, so the host can
   patch them. Defaults: map 144-148 MHz, 40 bins of 100 kHz; cal on 440.850 alone. */
static const volatile par_t PAR = {
    {'R', 'X', 'M', 'A', 'P', ' ', 'v', '1'},
    144000000u, 100000u, 25000u, 40, 4, 3000, 30000, 0, 1, 12, 15, 1, 10, 240,
    {440850000u}};

#define CLEAR_DBM (-100)   /* the upload's carrier is gone below this */
#define SIG_DB 10          /* map: a bin's peak this far over its floor is a signal */
#define ROW 50             /* bins a summary row */

static char *put(char *p, const char *s) {
  while (*s) *p++ = *s++;
  return p;
}

static char *dec(char *p, uint32_t v) {
  static const uint32_t pw[] = {1000000000, 100000000, 10000000, 1000000, 100000,
                                10000,      1000,      100,      10,      1};
  int i, started = 0;
  for (i = 0; i < 10; i++) {
    int d = 0;
    while (v >= pw[i]) v -= pw[i], d++;
    if (d || started || i == 9) *p++ = (char)('0' + d), started = 1;
  }
  return p;
}

static char *sdec(char *p, int v) {
  if (v < 0) *p++ = '-', v = -v;
  return dec(p, (uint32_t)v);
}

static char *kv(char *p, const char *k, uint32_t v) {
  p = put(p, k);
  return dec(p, v);
}

static char *hex4(char *p, uint32_t v) {
  int i;
  for (i = 12; i >= 0; i -= 4) *p++ = "0123456789abcdef"[(v >> i) & 15];
  return p;
}

/* hz as MHz with three decimals (to the kHz, rounded down) */
static char *mhz(char *p, uint32_t hz) {
  static const uint32_t pw[] = {1000000000, 100000000, 10000000, 1000000, 100000, 10000, 1000};
  int i, started = 0;
  for (i = 0; i < 7; i++) {
    int d = 0;
    while (hz >= pw[i]) hz -= pw[i], d++;
    if (d || started || i >= 3) *p++ = (char)('0' + d), started = 1;
    if (i == 3) *p++ = '.';
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

static void say0(const k6_api_t *api, char *line, char *p) {
  *p = 0;
  api->say(line);
}

static int rssi(const k6_api_t *api) { return KM_RSSI_DBM(api->fd_read(0x67)); }

static int key(const k6_api_t *api) { return api->level >= 4 ? api->key_poll() : api->key_down(); }

/* a proper read: tune, wait full_us, the mean of 4 reads 1 ms apart */
static int full(const k6_api_t *api, const par_t *q, uint32_t hz) {
  int s = 0, i;
  api->tune(hz, q->wide);
  api->delay_us(q->full_us);
  for (i = 0; i < 4; i++) {
    if (i) api->delay_us(1000);
    s += rssi(api);
  }
  return s >> 2;
}

/* dBm -> the stored byte (dBm + 174, 0..255) and back */
static uint8_t st8(int dbm) { return (uint8_t)(dbm < -174 ? 0 : dbm > 81 ? 255 : dbm + 174); }
static int dbm8(uint8_t v) { return (int)v - 174; }

/* a bin's summary char: '0' + (dBm + 160) / 2, clamped to 0..63 */
static char ch(uint8_t v) {
  int x = dbm8(v) + 160;
  x = x < 0 ? 0 : x > 126 ? 63 : x >> 1;
  return (char)('0' + x);
}

/* wait (up to 30 s) for the sender's own carrier on the pager frequency to drop */
static int clear(const k6_api_t *api, const par_t *q) {
  uint32_t t0 = api->ms();
  char line[64], *p;
  int r = full(api, q, api->rx_hz());
  if (r <= CLEAR_DBM) return 0;
  p = put(line, "rxmap: waiting for the carrier to drop, ");
  p = sdec(p, r);
  say0(api, line, put(p, " dBm"));
  while (rssi(api) > CLEAR_DBM && rssi(api) > r - 15) {
    if (key(api)) return -2;
    if (api->ms() - t0 > 30000) break; /* a carrier that stays: scan anyway, the M line says */
    api->delay_us(10000);
  }
  api->delay_us(200000);
  return 0;
}

static int map(const k6_api_t *api, const par_t *q, uint8_t *mem, uint32_t room) {
  uint8_t *fl = mem, *pk = mem + q->nbins, *at = mem + 2 * q->nbins;
  uint32_t b, s, f0 = q->lo_hz, t0 = api->ms();
  int sig = 0;
  char line[64], *p;
  if (3u * q->nbins > room) return -8;
  for (b = 0; b < q->nbins; b++, f0 += q->bin_hz) {
    int hi = -999, lo = 999, ihi = 0, ilo = 0, r, z;
    uint32_t f = f0;
    for (s = 0; s < q->nsteps; s++, f += q->step_hz) {
      if (key(api)) return -2;
      api->tune(f, q->wide);
      api->delay_us(q->quick_us);
      r = rssi(api);
      if (r > hi) hi = r, ihi = (int)s;
      if (r < lo) lo = r, ilo = (int)s;
    }
    pk[b] = st8(full(api, q, f0 + (uint32_t)ihi * q->step_hz));
    z = api->fd_read(0x63);
    fl[b] = st8(full(api, q, f0 + (uint32_t)ilo * q->step_hz));
    at[b] = (uint8_t)ihi;
    if (dbm8(pk[b]) >= dbm8(fl[b]) + SIG_DB) sig++;
    p = put(line, "b ");
    p = mhz(p, f0);
    *p++ = ' ';
    p = sdec(p, dbm8(fl[b]));
    *p++ = ' ';
    p = sdec(p, dbm8(pk[b]));
    p = put(p, " @");
    p = mhz(p, f0 + (uint32_t)ihi * q->step_hz);
    p = put(p, " z");
    say0(api, line, hex4(p, (uint32_t)z));
  }
  /* the summary, newest last: what the telemetry keeps */
  {
    int n = 0, k;
    p = put(line, "T");
    for (k = 0; k < q->tops; k++) {
      int best = -1, bd = SIG_DB - 1;
      for (b = 0; b < q->nbins; b++) {
        int d = dbm8(pk[b]) - dbm8(fl[b]);
        if (!(at[b] & 0x80) && d > bd) bd = d, best = (int)b;
      }
      if (best < 0) break;
      at[best] |= 0x80; /* listed (nsteps < 128) */
      if (n == 4) say0(api, line, p), p = put(line, "T"), n = 0;
      *p++ = ' ';
      p = mhz(p, q->lo_hz + (uint32_t)best * q->bin_hz + (uint32_t)(at[best] & 0x7F) * q->step_hz);
      *p++ = ':';
      p = sdec(p, dbm8(pk[best]));
      n++;
    }
    if (n) say0(api, line, p);
  }
  for (s = 0; s < 2; s++) {
    const uint8_t *v = s ? pk : fl;
    for (b = 0; b < q->nbins;) {
      p = put(line, s ? "P" : "F");
      p = dec(p, b);
      *p++ = ' ';
      for (; b < q->nbins && p - line < 6 + ROW; b++) *p++ = ch(v[b]);
      say0(api, line, p);
    }
  }
  p = put(line, "M lo=");
  p = mhz(p, q->lo_hz);
  p = kv(p, " b=", udiv(q->bin_hz, 1000));
  p = kv(p, " n=", q->nbins);
  p = kv(p, " s=", q->step_hz);
  p = kv(p, " q=", q->quick_us);
  p = kv(p, " sig=", (uint32_t)sig);
  p = kv(p, " ms=", api->ms() - t0);
  say0(api, line, p);
  return sig;
}

typedef struct {
  uint8_t floor, max;
  uint16_t n;
  int32_t sum;
  uint32_t zsum;
} pt_t;

static int cal(const k6_api_t *api, const par_t *q, uint8_t *mem, uint32_t room) {
  pt_t *pt = (pt_t *)mem;
  uint32_t i, k, t0 = api->ms(), last = 0;
  int ev = 0, seen = 0;
  char line[64], *p;
  if (q->ncal == 0 || q->ncal > NCAL) return -11;
  if (q->ncal * sizeof(pt_t) > room) return -8;
  /* the floors: the lowest of 3 cycles (a burst on one of them doesn't count) */
  for (i = 0; i < q->ncal; i++) pt[i].floor = 255, pt[i].n = 0, pt[i].sum = 0, pt[i].zsum = 0, pt[i].max = 0;
  for (k = 0; k < 3; k++)
    for (i = 0; i < q->ncal; i++) {
      uint8_t r = st8(full(api, q, q->cal_hz[i]));
      if (r < pt[i].floor) pt[i].floor = r;
    }
  p = put(line, "C");
  for (i = 0; i < q->ncal; i++) {
    if (p - line > 46) say0(api, line, p), p = put(line, "C");
    *p++ = ' ';
    p = mhz(p, q->cal_hz[i]);
    *p++ = ':';
    p = sdec(p, dbm8(pt[i].floor));
  }
  say0(api, line, p);
  for (;;) {
    uint32_t now = api->ms();
    if (now - t0 > (uint32_t)q->max_s * 1000u) break;
    if (seen && now - last > (uint32_t)q->quiet_s * 1000u) break;
    for (i = 0; i < q->ncal; i++) {
      pt_t *x = &pt[i];
      int r, z;
      if (key(api)) return -2;
      r = full(api, q, q->cal_hz[i]);
      z = api->fd_read(0x63);
      if (r >= dbm8(x->floor) + q->det_db) {
        x->sum += r, x->zsum += (uint32_t)(z & 0xFFFF), x->n++;
        if (st8(r) > x->max) x->max = st8(r);
        seen = 1, last = api->ms();
      } else if (x->n) {
        /* the carrier dropped: one line for it */
        uint32_t a = (uint32_t)(-x->sum);
        p = put(line, "c ");
        p = mhz(p, q->cal_hz[i]);
        *p++ = ' ';
        p = sdec(p, -(int)udiv(a + x->n / 2, x->n));
        *p++ = ' ';
        p = sdec(p, dbm8(x->max));
        p = put(p, " fl");
        p = sdec(p, dbm8(x->floor));
        p = kv(p, " n", x->n);
        p = kv(p, " z", udiv(x->zsum + x->n / 2, x->n));
        say0(api, line, p);
        x->n = 0, x->sum = 0, x->zsum = 0, x->max = 0;
        ev++;
      }
    }
  }
  p = kv(line, "K ev=", (uint32_t)ev);
  p = kv(p, " ms=", api->ms() - t0);
  say0(api, line, p);
  return seen ? ev : -6;
}

int k6_main(const k6_api_t *api) {
  par_t q;
  uint8_t *mem, *d = (uint8_t *)&q;
  const volatile uint8_t *src = (const volatile uint8_t *)&PAR;
  uint32_t room = 0, i;
  int r;
  if (api->level < 2) return -3;
  for (i = 0; i < sizeof q; i++) d[i] = src[i];
  if (q.mode > 1 || q.full_us < 1000 || (q.mode == 0 && (!q.nbins || !q.nsteps || q.nsteps > 127)))
    return -11;
  mem = (uint8_t *)api->scratch(&room);
  r = clear(api, &q);
  if (r) return r;
  return q.mode ? cal(api, &q, mem, room) : map(api, &q, mem, room);
}
