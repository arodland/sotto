/* aprsclk: set a clock from APRS traffic heard on 144.390 MHz (docs/k6-aprs-clock.md).

   Receive only. The K6 tunes to APRS_HZ (wide filter, the chip's AFC left on), taps the
   FD6818's low IF on PB1 (GP0 code 13) by DMA as the FT8 modules do (gp0rx.h, PR #69), and
   demodulates Bell 202 AFSK 1200 on the MCU:

     counts (2 * ones - 64 per 64 DMA samples, ~87 kHz)
       -> mixed down by the nominal IF (64-entry sine), a 2nd-order CIC decimating by 6 (~14.5 kHz)
       -> phase by CORDIC; the phase step is the FM discriminator (no division)
       -> DC removed, one-bit sliding correlators at 1200 and 2200 Hz, mark against space
       -> a DPLL bit clock (Dire Wolf's: nudged towards each transition), NRZI
       -> HDLC (flags, bit stuffing), CRC-16/X.25 (good residue 0xF0B8)
       -> the APRS information field's timestamps:
            '/' '@' positions, ';' objects, '>' status:  DDHHMMz (60 s resolution), HHMMSSh (1 s)
            '$GPRMC' / '$GNRMC' raw NMEA (status A):     hhmmss[.ss] and ddmmyy
            '_' positionless weather:                    MMDDHHMM (60 s; many send local time, so
                                                         weather stamps alone never set the clock)
          ("DDHHMM/" local time, Mic-E, '}' third party and the rest carry no usable time)

   Each timestamp says the true UTC at the frame's first bit was in [stamp, stamp + resolution +
   delay], delay 3 s heard direct, 8 s digipeated (an H bit in the path). In offsets
   (UTC ms of day less the module's ms) those intervals go into a table, one per station (a
   digipeated copy of the same stamp is not a second vote), and Marzullo's algorithm (NTP's)
   picks the offset the most stations agree on, the narrowest such interval among ties. With two
   or more stations agreeing, the clock is set: its estimate is the interval's low edge plus half
   its width, at most 0.5 s (a stamp can't be later than the send, and trackers key within a
   second of the fix). A 1 s stamp gives the second; DDHHMMz alone gives the minute.

   Lines (serial / telemetry):
     aprs start hz=144390000 ks=5603 n=12
     f VE7ABC-9 n=58 h=1 ts=175201h                        (a good frame: source, bytes, hops, stamp)
     clk 17:52:04.250 w=1000 v=2 d=08                      (the clock: UTC, interval ms, votes, day)
     aprs end ok=12 bad=3 ts=5 v=2 utc=17:59:59.120 lost=0
   Screen (level 6) once a second when no frame is coming, LED blink a good frame (level 7). A key ends it (else RUN_MS). Writes REG_34[3:0]
   and REG_33 bit 8 (GP0), the MCU's TIM17, DMA channel 1 and their clock enables, all put back.
   Returns the votes behind the clock (0: none), or -2 (a key before any frame), -3 (old image),
   -4 (PB1 not an input), -5 (REG_33 refused), -7 (TIM17 or DMA channel 1 in use), -8 (scratch).
   SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 2
#define K6_MODULE_OPT z
#include "../k6_mod.h"

#ifndef APRS_HZ
#define APRS_HZ 144390000u
#endif
#ifndef RUN_MS
#define RUN_MS 900000u /* 15 min */
#endif
#define DMAX_DIRECT 3000
#define DMAX_DIGI 8000
#define NC 12   /* stations kept */
#define MAXF 332 /* AX.25 frame bytes, with the FCS */
#define DEC 6   /* counts an audio sample */
#define NW 16   /* correlator ring (the window is n of it) */
#define FIF_CHZ 1692708u /* 26 MHz / 1536 in 0.01 Hz */
#define DAY 86400000

#define GPIOB 0x48000400u
#define RCC_AHBENR 0x40021014u
#define RCC_APB2ENR 0x40021018u
#define TIM14_CNT 0x40002024u
#define TIM17 0x40014800u
#define TIM17_CNT (TIM17 + 0x24)
#define DMA 0x40020000u
#define DMA_CCR1 (DMA + 0x08)
#define DMA_CNDTR1 (DMA + 0x0C)
#define DMA_CPAR1 (DMA + 0x10)
#define DMA_CMAR1 (DMA + 0x14)
#define TIM17EN (1u << 18)
#define RSZ 1024u


/* a station's interval: offsets (UTC ms of day less module ms) relative to the table's ref */
typedef struct {
  uint8_t call[7];  /* address bytes as sent (SSID byte masked to its SSID) */
  uint8_t kind;     /* 'z', 'h', 'r' (RMC) or 'w' (weather MMDDHHMM), 0 empty */
  int32_t lo, hi;
  uint32_t age;     /* insertion count, for replacing the oldest */
  uint32_t key;     /* the stamp (ms of day, day << 27), to spot a digipeated copy */
  uint8_t day, mon, yr, pad;
} cand_t;

typedef struct {
  uint8_t ring[RSZ];           /* first: word aligned at the scratch's start */
  uint8_t fr[MAXF];
  int32_t cr[4][NW];           /* the correlators' products: mark cos, mark sin, space cos, space sin */
  cand_t c[NC];
  char ln[64];
  /* clock and DMA */
  uint32_t t, c17, c14, laps, lastw, produced, consumed, t0, p0, cps8, lost;
  /* mixer and CIC */
  uint32_t ph, dph;
  int32_t i1, i2, q1, q2, ci, cq, di, dq, k;
  /* discriminator, correlators, bit clock */
  int32_t ang, dc, sum[4];
  uint32_t ph1, ph2, st1, st2, pll, stb;
  int32_t n, w, lastv, lastbit;
  /* HDLC */
  uint32_t sr, bb, nbits, flag_ms, run, pre, min_ms;
  int32_t len, in;
  /* results */
  uint32_t ok, bad, nts, seq, now_ms, scr_ms;
  int32_t ref, haveref, votes, est, wid, hops;
  uint8_t day, mon, yr, stn;
} st_t;

static const int8_t SIN[64] = {0,   12,  25,  37,  49,  60,  71,  81,  90,  98,  106, 112, 117,
                               122, 125, 126, 127, 126, 125, 122, 117, 112, 106, 98,  90,  81,
                               71,  60,  49,  37,  25,  12,  0,   -12, -25, -37, -49, -60, -71,
                               -81, -90, -98, -106, -112, -117, -122, -125, -126, -127, -126, -125,
                               -122, -117, -112, -106, -98, -90, -81, -71, -60, -49, -37, -25, -12};
/* atan(2^-i) in 2^16 a turn */
static const uint16_t ATN[11] = {8192, 4836, 2555, 1297, 651, 326, 163, 81, 41, 20, 10};

static uint32_t udiv(uint32_t n, uint32_t d) {
  uint32_t q = 0, bit = 1;
  while (d < n && !(d & 0x80000000u)) d <<= 1, bit <<= 1;
  while (bit) {
    if (n >= d) n -= d, q |= bit;
    d >>= 1, bit >>= 1;
  }
  return q;
}

/* a * b / c in 64 bits by hand (an M0 module has no run-time library), c < 2^31 */
static uint32_t muldiv(uint32_t a, uint32_t b, uint32_t c) {
  uint32_t al = a & 0xFFFF, ah = a >> 16, bl = b & 0xFFFF, bh = b >> 16;
  uint32_t lo = al * bl, m1 = al * bh, m2 = ah * bl, hi = ah * bh, t, r = 0, q = 0;
  int i;
  t = lo + (m1 << 16);
  hi += (m1 >> 16) + (t < lo);
  lo = t, t = lo + (m2 << 16);
  hi += (m2 >> 16) + (t < lo);
  lo = t;
  for (i = 0; i < 64; i++) {
    r = (r << 1) | (hi >> 31);
    hi = (hi << 1) | (lo >> 31);
    lo <<= 1;
    q <<= 1;
    if (r >= c) r -= c, q |= 1;
  }
  return q;
}

static char *put(char *p, const char *s) {
  while (*s) *p++ = *s++;
  return p;
}

/* v in decimal, at least w digits */
static char *dec(char *p, uint32_t v, int w) {
  char t[10];
  int n = 0;
  do {
    uint32_t q = udiv(v, 10);
    t[n++] = (char)('0' + v - 10 * q);
    v = q;
  } while (v || n < w);
  while (n) *p++ = t[--n];
  return p;
}

static char *kv(char *p, const char *k, uint32_t v) { return dec(put(p, k), v, 1); }

/* ms of day -> hh:mm:ss (and .mmm) */
static char *hms(char *p, uint32_t ms, int frac) {
  uint32_t s = udiv(ms, 1000), m = udiv(s, 60), h = udiv(m, 60);
  p = dec(p, h, 2);
  *p++ = ':';
  p = dec(p, m - 60 * h, 2);
  *p++ = ':';
  p = dec(p, s - 60 * m, 2);
  if (frac) *p++ = '.', p = dec(p, ms - 1000 * s, 3);
  return p;
}

/* a callsign from its 7 address bytes: "VE7ABC-9" */
static char *call(char *p, const uint8_t *a) {
  int i, ssid = (a[6] >> 1) & 15;
  for (i = 0; i < 6 && (a[i] >> 1) != ' '; i++) *p++ = (char)(a[i] >> 1);
  if (ssid) *p++ = '-', p = dec(p, (uint32_t)ssid, 1);
  return p;
}

static void say(const k6_api_t *api, st_t *s, char *p) {
  *p = 0;
  api->say(s->ln);
}

/* ---- the IF front end (gp0rx.h's, without the FT8 mixer) ---- */
#ifndef APRS_HOST
static uint32_t rd(uint32_t a) { return *(volatile uint32_t *)a; }
static void wr(uint32_t a, uint32_t v) { *(volatile uint32_t *)a = v; }

static uint32_t now(st_t *s) {
  uint32_t c17 = rd(TIM17_CNT) & 0xFFFF, c14 = rd(TIM14_CNT) & 0xFFFF;
  uint32_t coarse = ((c14 - s->c14) & 0xFFFF) * 9600u, fine = (c17 - s->c17) & 0xFFFF;
  int32_t d = (int32_t)(int16_t)(uint16_t)(fine - coarse);
  s->c17 = c17, s->c14 = c14;
  return s->t += coarse + (uint32_t)d;
}

static uint32_t poll(st_t *s) {
  uint32_t w = RSZ - (rd(DMA_CNDTR1) & 0xFFFF);
  if (w < s->lastw) s->laps++;
  s->lastw = w;
  return s->produced = s->laps * RSZ + w;
}

static void resync(st_t *s) {
  uint32_t w = RSZ - (rd(DMA_CNDTR1) & 0xFFFF);
  s->laps = 0, s->lastw = w, s->produced = w;
  s->consumed = s->p0 = w & ~63u;
  s->t0 = now(s);
}

static int ones64(const uint8_t *q) {
  const uint32_t *p = (const uint32_t *)q, m = 0x02020202u;
  uint32_t a = 0, i;
  for (i = 0; i < 16; i += 4) a += (p[i] & m) + (p[i + 1] & m) + (p[i + 2] & m) + (p[i + 3] & m);
  a >>= 1;
  a = (a & 0x00FF00FFu) + ((a >> 8) & 0x00FF00FFu);
  return (int)((a & 0xFFFF) + (a >> 16));
}
#endif

/* the steps from the DMA's rate (cps8: 96 MHz cycles a transfer, Q8). Counts a second
   R = 3.84e8 / cps8, audio fa = R / DEC */
static void steps(st_t *s) {
  uint32_t c = s->cps8;
  s->dph = muldiv(FIF_CHZ << 4, c << 16, 9375000u);            /* 2^32 FIF / R */
  s->st1 = muldiv(1200u * c * DEC, 1u << 26, 6000000u);        /* 2^32 f / fa */
  s->st2 = muldiv(2200u * c * DEC, 1u << 26, 6000000u);
  s->stb = s->st1;                                             /* 1200 baud */
}

/* ---- the clock: Marzullo over the stations' intervals ---- */
static void vote(st_t *s) {
  int i, j, best = 0, bw = 0, blo = 0;
  for (i = 0; i < NC; i++) {
    int32_t x = s->c[i].lo, hi = 0x7FFFFFFF;
    int n = 0, sn = 0;
    if (!s->c[i].kind) continue;
    for (j = 0; j < NC; j++)
      if (s->c[j].kind && s->c[j].lo <= x && x <= s->c[j].hi) {
        n++, sn += s->c[j].kind != 'w';
        if (s->c[j].hi < hi) hi = s->c[j].hi;
      }
    if (!sn && n > 1) n = 1; /* weather stations in one zone sending local time agree with each other */
    if (n > best || (n == best && hi - x < bw)) best = n, bw = hi - x, blo = x;
  }
  s->votes = best, s->wid = bw;
  s->est = blo + (bw / 2 < 500 ? bw / 2 : 500);
  /* the date: the newest agreeing stamp that has one */
  for (i = 0, j = -1; i < NC; i++)
    if (s->c[i].kind && s->c[i].day && s->c[i].lo <= s->est && s->est <= s->c[i].hi &&
        (j < 0 || s->c[i].age > s->c[j].age))
      j = i;
  if (j >= 0) s->day = s->c[j].day, s->mon = s->c[j].mon, s->yr = s->c[j].yr;
}

static int32_t wrap(int32_t v) {
  if (v > DAY / 2) v -= DAY;
  if (v <= -DAY / 2) v += DAY;
  return v;
}

/* UTC ms of day at module ms t, once set */
static uint32_t utc(st_t *s, uint32_t t) {
  int32_t v = (int32_t)t + s->ref + s->est;
  if (v < 0) v += DAY;
  if (v < 0) v += DAY;
  if (v >= DAY) v -= DAY;
  if (v >= DAY) v -= DAY;
  return (uint32_t)v;
}

static int same(const uint8_t *a, const uint8_t *b, int n) {
  while (n--)
    if (*a++ != *b++) return 0;
  return 1;
}

static int d2(const uint8_t *p) {
  if (p[0] < '0' || p[0] > '9' || p[1] < '0' || p[1] > '9') return -99;
  return (p[0] - '0') * 10 + p[1] - '0';
}

/* six digits as three two-digit fields under hi0 (24 for an hour, 32 for a day), 60, 60: x * 3600
   + y * 60 + z (a day-hour-minute: day * 3600 + hour * 60 + minute), or -1 */
static int32_t tri(const uint8_t *p, int hi0) {
  int x = d2(p), y = d2(p + 2), z = d2(p + 4);
  if (x < 0 || x >= hi0 || y < 0 || y >= 60 || z < 0 || z >= 60) return -1;
  return (x * 60 + y) * 60 + z;
}

/* a stamp at src (7 address bytes): ms of day stamped, resolution, frame start (module ms) */
static void add(const k6_api_t *api, st_t *s, const uint8_t *src, int kind, uint32_t tod, int res,
                uint32_t t0, int day, int mon, int yr) {
  uint32_t key = tod | (uint32_t)day << 27;
  int i, k = -1;
  int32_t lo;
  cand_t *c;
  char *p;
  if (!s->haveref) s->ref = (int32_t)tod - (int32_t)t0, s->haveref = 1;
  lo = wrap((int32_t)tod - (int32_t)t0 - s->ref);
  for (i = 0; i < NC; i++) {
    c = &s->c[i];
    if (c->kind && same(c->call, src, 6) && ((c->call[6] ^ src[6]) & 0x1E) == 0) {
      if (c->kind == kind && c->key == key) return; /* a digipeated copy */
      k = i;
      break;
    }
    if (k < 0 || !c->kind || (s->c[k].kind && c->age < s->c[k].age)) k = i;
  }
  c = &s->c[k];
  for (i = 0; i < 6; i++) c->call[i] = src[i];
  c->call[6] = src[6] & 0x1E, c->key = key;
  c->kind = (uint8_t)kind, c->lo = lo, c->hi = lo + res + (s->hops ? DMAX_DIGI : DMAX_DIRECT);
  c->age = ++s->seq, c->day = (uint8_t)day, c->mon = (uint8_t)mon, c->yr = (uint8_t)yr;
  s->nts++;
  vote(s);
  p = put(s->ln, "clk ");
  if (s->votes >= 2) p = hms(p, utc(s, s->now_ms), 1);
  else p = put(p, "--:--:--");
  p = kv(p, " w=", (uint32_t)s->wid);
  p = kv(p, " v=", (uint32_t)s->votes);
  if (s->day) p = dec(put(p, " d="), s->day, 2), p = s->mon ? dec(put(p, "/"), s->mon, 2) : p;
  say(api, s, p);
}

/* a good frame: its source, and a timestamp if it has one */
static void frame(const k6_api_t *api, st_t *s) {
  const uint8_t *f = s->fr, *q;
  int n = s->len - 2, a, i, kind = 0, res = 0, day = 0, mon = 0, yr = 0;
  uint32_t tod = 0, t0;
  char *p;
  for (a = 0; a < n && !(f[a] & 1); a++) {} /* the address field ends at a byte with bit 0 set */
  /* after 2 to 10 whole addresses: a + 1 a multiple of 7 (times 7's inverse mod 2^32, no division) */
  if (a < 13 || a > 69 || (uint32_t)(a + 1) * 0xB6DB6DB7u > 0x24924924u || a >= n - 2 ||
      f[a + 1] != 0x03 || f[a + 2] != 0xF0)
    return;
  s->hops = 0;
  for (i = 20; i <= a; i += 7) s->hops += f[i] >> 7; /* digipeaters that have repeated it */
  q = f + a + 3, n -= a + 3;
  /* when its first bit went out: the bits since the opening flag at 1200 baud */
  t0 = s->now_ms - udiv((s->nbits + 8) * 5u, 6u);
  if (n >= 8 && (q[0] == '/' || q[0] == '@' || (q[0] == '>' && q[7] == 'z')))
    kind = q[7], q++;
  else if (n >= 18 && q[0] == ';' && (q[10] == '*' || q[10] == '_'))
    kind = q[17], q += 11;
  else if (n >= 9 && q[0] == '_') { /* _MMDDHHMM: the month, then a day-hour-minute */
    mon = d2(q + 1), q += 3, kind = 'w';
    if (mon < 1 || mon > 12) kind = mon = 0;
  }
  if (kind == 'z' || kind == 'h' || kind == 'w') {
    int32_t v = tri(q, kind == 'h' ? 24 : 32);
    day = d2(q);
    if (v < 0 || (kind != 'h' && !day)) kind = 0;
    else if (kind != 'h') tod = (uint32_t)(v - day * 3600) * 60000u, res = 60000; /* hour < 24 */
    else tod = (uint32_t)v * 1000u, res = 1000, day = 0;
    if (kind != 'h' && tod >= DAY) kind = 0;
    if (!kind) mon = 0;
  } else if (n >= 20 && q[0] == '$' && q[1] == 'G' && q[3] == 'R' && q[4] == 'M' && q[5] == 'C' &&
             q[6] == ',') {
    /* $GPRMC,hhmmss[.ss],A,lat,N,lon,E,speed,course,ddmmyy */
    const uint8_t *e = q + n, *r = q + 7;
    int32_t v = tri(r, 24);
    int fld = 1;
    kind = 0;
    if (v >= 0) {
      tod = (uint32_t)v * 1000u, res = 1000;
      if (r[6] == '.' && d2(r + 7) >= 0) tod += (uint32_t)d2(r + 7) * 10u, res = 10;
      for (; r < e && fld < 9; r++)
        if (*r == ',') {
          fld++;
          if (fld == 2 && r + 1 < e && r[1] != 'A') break;
          if (fld == 2) kind = 'r';
        }
      if (kind && fld == 9 && r + 6 <= e) {
        day = d2(r), mon = d2(r + 2), yr = d2(r + 4);
        if (day < 1 || day > 31 || mon < 1 || mon > 12 || yr < 0) day = mon = yr = 0;
      }
    }
  } else kind = 0;
  p = put(s->ln, "f ");
  p = call(p, f + 7);
  p = kv(p, " n=", (uint32_t)s->len);
  p = kv(p, " h=", (uint32_t)s->hops);
  if (kind) {
    p = put(p, " ts=");
    p = hms(p, tod, res < 1000);
    *p++ = (char)kind;
  }
  say(api, s, p);
  if (kind) add(api, s, f + 7, kind, tod, res, t0, day, mon, yr);
}

static int crc_ok(const uint8_t *b, int n) {
  uint32_t c = 0xFFFF;
  int i, j;
  for (i = 0; i < n; i++) {
    c ^= b[i];
    for (j = 0; j < 8; j++) c = (c >> 1) ^ (0x8408u & (0u - (c & 1)));
  }
  return c == 0xF0B8;
}

/* one bit out of the NRZI decoder */
static void hdlc(const k6_api_t *api, st_t *s, uint32_t bit) {
  uint32_t sr = s->sr = (s->sr << 1) | bit;
  if ((sr & 0xFF) == 0x7E) {
    s->flag_ms = s->now_ms;
    /* a preamble: flags back to back (7 bits since the last: its last 0 is this one's first).
       Noise makes a lone flag every second or so, four in a row hardly ever */
    s->run = s->in && s->nbits == 7 ? s->run + 1 : 0;
    if (s->run == 3) s->pre++;
    if (s->in && s->len >= 18) {
      if (crc_ok(s->fr, s->len)) {
        s->ok++;
        if (api->level >= 7) api->led(1);
        frame(api, s);
#ifndef APRS_HOST
        resync(s); /* the line took the CPU for a while */
#endif
      } else s->bad++;
    }
    s->in = 1, s->len = 0, s->bb = 0x80, s->nbits = 0;
    return;
  }
  if ((sr & 0x7F) == 0x7F) {
    s->in = 0;
    return;
  }
  if (!s->in) return;
  s->nbits++;
  if ((sr & 0x3F) == 0x3E) return; /* a stuffed zero */
  if (bit) s->bb |= 0x100;
  if (s->bb & 1) {
    if (s->len >= MAXF) s->in = 0;
    else s->fr[s->len++] = (uint8_t)(s->bb >> 1);
    s->bb = 0x80;
  } else s->bb >>= 1;
}

static int32_t mag(int32_t a, int32_t b) {
  if (a < 0) a = -a;
  if (b < 0) b = -b;
  return a > b ? a + ((3 * b) >> 3) : b + ((3 * a) >> 3);
}

/* one complex sample at ~14.5 kHz */
static void audio(const k6_api_t *api, st_t *s, int32_t x, int32_t y) {
  int32_t ang = 0, d, v, m, sp, t, i;
  uint32_t prev;
  /* CORDIC vectoring: the phase in 2^16 a turn */
  if (x < 0) x = -x, y = -y, ang = 32768;
  for (i = 0; i < 11; i++) {
    t = x;
    if (y > 0) x += y >> i, y -= t >> i, ang += ATN[i];
    else x -= y >> i, y += t >> i, ang -= ATN[i];
  }
  d = (int16_t)(uint16_t)(ang - s->ang); /* the FM discriminator: the phase step */
  s->ang = ang;
  d -= s->dc >> 8;
  s->dc += d;
  /* sliding correlators over the last n samples */
  {
    int c1 = SIN[((s->ph1 >> 26) + 16) & 63], s1 = SIN[s->ph1 >> 26];
    int c2 = SIN[((s->ph2 >> 26) + 16) & 63], s2 = SIN[s->ph2 >> 26];
    int32_t *r0 = s->cr[0], *r1 = s->cr[1], *r2 = s->cr[2], *r3 = s->cr[3];
    int w = s->w, o = (w - s->n) & (NW - 1);
    s->ph1 += s->st1, s->ph2 += s->st2;
    r0[w] = d * c1, r1[w] = d * s1, r2[w] = d * c2, r3[w] = d * s2;
    s->sum[0] += r0[w] - r0[o];
    s->sum[1] += r1[w] - r1[o];
    s->sum[2] += r2[w] - r2[o];
    s->sum[3] += r3[w] - r3[o];
    s->w = (w + 1) & (NW - 1);
  }
  m = mag(s->sum[0], s->sum[1]) >> 11;
  sp = mag(s->sum[2], s->sum[3]) >> 11;
  /* mark against space, as they come: a peak tracker on each tone (for a sender's
     pre-emphasis twist) lost frames at +6 dB and gained nothing elsewhere (host sweep) */
  v = m > sp;
#ifdef APRS_TRACE
  APRS_TRACE(s, d, m, sp);
#endif
  /* the bit clock: sample as the phase passes half a turn, pulled towards each transition */
  prev = s->pll;
  s->pll += s->stb;
  if ((int32_t)prev >= 0 && (int32_t)s->pll < 0) {
    hdlc(api, s, v == s->lastbit);
    s->lastbit = v;
  }
  if (v != s->lastv) {
    int32_t p = (int32_t)s->pll;
    s->pll = (uint32_t)(s->in ? p - (p >> 2) : p >> 1);
    s->lastv = v;
  }
}

/* one count (2 * ones - 64) through the mixer and CIC */
static void count(const k6_api_t *api, st_t *s, int c) {
  uint32_t ix = s->ph >> 26;
  s->ph += s->dph;
  s->i1 += c * SIN[(ix + 16) & 63], s->i2 += s->i1;
  s->q1 -= c * SIN[ix], s->q2 += s->q1;
  if (++s->k == DEC) {
    int32_t a = s->i2 - s->ci, b = s->q2 - s->cq, x = a - s->di, y = b - s->dq;
    s->ci = s->i2, s->cq = s->q2, s->di = a, s->dq = b, s->k = 0;
    audio(api, s, x >> 4, y >> 4);
  }
}

#ifndef APRS_HOST
/* every whole count the DMA has made, through the mixer and CIC */
static void drain(const k6_api_t *api, st_t *s) {
  uint32_t P = poll(s);
  if (P - s->consumed > RSZ - 64) {
    s->lost++;
    resync(s);
    return;
  }
  /* s->produced, not P: a frame's line resyncs the ring from inside count() */
  while (s->produced - s->consumed >= 64) {
    const uint8_t *q = s->ring + (s->consumed & (RSZ - 1));
    s->consumed += 64; /* before count(): its resync sets it */
    count(api, s, 2 * ones64(q) - 64);
  }
}

static void dma_on(uint8_t *ring) {
  wr(DMA_CCR1, 0);
  wr(DMA + 0x04, 0xF);
  wr(DMA_CPAR1, GPIOB + 0x10);
  wr(DMA_CMAR1, (uint32_t)ring);
  wr(DMA_CNDTR1, RSZ);
  wr(DMA_CCR1, (3u << 12) | (1u << 7) | (1u << 5) | 1u);
}

static void screen(const k6_api_t *api, st_t *s) {
  char *p;
  p = dec(put(s->ln, " APRS CLOCK "), udiv(APRS_HZ, 1000000), 3);
  *p++ = '.';
  p = dec(p, udiv(APRS_HZ, 1000) - 1000 * udiv(APRS_HZ, 1000000), 3);
  *p = 0;
  api->lcd_text(0, s->ln);
  p = put(s->ln, "UTC ");
  if (s->votes >= 2) p = hms(p, utc(s, s->now_ms), 0);
  else p = put(p, "--:--:--");
  *p = 0;
  api->lcd_text(1, s->ln);
  p = kv(s->ln, "stations ", (uint32_t)s->votes);
  if (s->votes >= 2) p = put(p, s->wid < 30000 ? " (sec)" : " (min)");
  *p = 0;
  api->lcd_text(2, s->ln);
  p = kv(s->ln, "ok ", s->ok);
  p = kv(p, " bad ", s->bad);
  p = kv(p, " ts ", s->nts);
  *p = 0;
  api->lcd_text(3, s->ln);
  if (s->day) {
    p = dec(put(s->ln, "day "), s->day, 2);
    if (s->mon) p = dec(put(p, "/"), s->mon, 2), p = dec(put(p, "/"), s->yr, 2);
    *p = 0;
    api->lcd_text(4, s->ln);
  }
  api->lcd_text(5, "any key: done");
  api->lcd_flush();
}

#endif /* APRS_HOST */

#ifndef APRS_HOST
int k6_main(const k6_api_t *api) {
  st_t *s;
  uint32_t room, apb2, ahb, i, tkey = 0;
  uint16_t r33, r34;
  char *p;
  if (api->level < 2) return -3;
  s = (st_t *)api->scratch(&room);
  if (room < sizeof(st_t)) return -8;
  for (i = 0; i < sizeof(st_t) / 4; i++) ((uint32_t *)s)[i] = 0;
  if (rd(GPIOB) & (3u << 2)) return -4;
  api->tune(APRS_HZ, 1);
  api->delay_us(50000);
  apb2 = rd(RCC_APB2ENR), ahb = rd(RCC_AHBENR);
  wr(RCC_AHBENR, ahb | 1u);
  if ((apb2 & TIM17EN) || (rd(DMA_CCR1) & 1)) {
    wr(RCC_AHBENR, ahb);
    return -7;
  }
  r33 = api->fd_read(0x33), r34 = api->fd_read(0x34);
  if ((r33 & 0x0100) && api->fd_write(0x33, (uint16_t)(r33 & ~0x0100)) != 0) {
    wr(RCC_AHBENR, ahb);
    return -5;
  }
  api->fd_write(0x34, (uint16_t)((r34 & 0xFFF0) | 13));
  wr(RCC_APB2ENR, apb2 | TIM17EN);
  wr(TIM17 + 0x00, 0);
  wr(TIM17 + 0x28, 0);
  wr(TIM17 + 0x2C, 0xFFFF);
  wr(TIM17 + 0x14, 1);
  wr(TIM17 + 0x10, 0);
  wr(TIM17 + 0x0C, 1u << 8);
  wr(TIM17 + 0x00, 1);
  s->c17 = rd(TIM17_CNT) & 0xFFFF, s->c14 = rd(TIM14_CNT) & 0xFFFF;
  dma_on(s->ring);
  /* the DMA's rate over about 50 ms */
  resync(s);
  while (poll(s) - s->p0 < 256u * 1024u) {}
  now(s);
  s->cps8 = muldiv(s->t - s->t0, 256, s->produced - s->p0);
  steps(s);
  s->n = (int32_t)udiv(64000000u + 600u * s->cps8, 1200u * s->cps8); /* fa / 1200, rounded */
  if (s->n > NW - 1) s->n = NW - 1;
  p = put(s->ln, "aprs start hz=");
  p = dec(p, APRS_HZ, 1);
  p = kv(p, " ks=", udiv(96000u * 256u, s->cps8));
  p = kv(p, " n=", (uint32_t)s->n);
  say(api, s, p);
  resync(s);
  for (;;) {
    drain(api, s);
    if ((int32_t)(s->now_ms - tkey) >= 50) {
      int key;
      s->now_ms = api->ms();
      tkey = s->now_ms;
      key = api->level >= 4 ? api->key_poll() : api->key_down();
      if (key || s->now_ms >= RUN_MS) break;
      if (api->level >= 7 && s->now_ms - s->flag_ms > 120) api->led(0);
      /* the screen takes 20-40 ms (the ring holds 0.2 ms): once a second, and never while a
         frame or its flags are coming (the HDLC is inside a frame: up to 2.3 s since its flag) */
      if (s->now_ms - s->scr_ms >= 1000 && s->now_ms - s->flag_ms > 100 &&
          !(s->in && s->now_ms - s->flag_ms < 2400)) {
        s->scr_ms = s->now_ms;
        /* the DMA's rate again over the stretch since the last pause (it moves with the CPU's
           load): about 0.2 ms of sums, so here, where a pause costs nothing */
        if (s->consumed - s->p0 > (1u << 22)) {
          uint32_t c;
          now(s);
          c = muldiv(s->t - s->t0, 256, s->consumed - s->p0);
          /* a stretch past TIM14's 6.5 s wrap reads wrong: only a rate within 1/16 */
          if (c - s->cps8 + (s->cps8 >> 4) <= (s->cps8 >> 3)) s->cps8 = (3 * s->cps8 + c) >> 2, steps(s);
        }
        if (s->now_ms - s->min_ms >= 60000) { /* once a minute: the channel, for the bench */
          s->min_ms = s->now_ms;
          p = kv(s->ln, "m ", udiv(s->now_ms, 60000));
          p = put(p, " rssi=-");
          p = dec(p, (uint32_t)-KM_RSSI_DBM(api->fd_read(0x67)), 1);
          p = kv(p, " pre=", s->pre);
          p = kv(p, " ok=", s->ok);
          p = kv(p, " bad=", s->bad);
          say(api, s, p);
        }
        if (api->level >= 6) screen(api, s);
        resync(s);
      }
    } else s->now_ms = api->ms();
  }
  wr(TIM17 + 0x00, 0);
  wr(TIM17 + 0x0C, 0);
  wr(DMA_CCR1, 0);
  wr(DMA + 0x04, 0xF);
  wr(DMA_CNDTR1, 0);
  wr(DMA_CPAR1, 0);
  wr(DMA_CMAR1, 0);
  wr(RCC_APB2ENR, apb2);
  wr(RCC_AHBENR, ahb);
  api->fd_write(0x34, r34);
  api->fd_write(0x33, r33);
  p = put(s->ln, "aprs end ok=");
  p = dec(p, s->ok, 1);
  p = kv(p, " bad=", s->bad);
  p = kv(p, " ts=", s->nts);
  p = kv(p, " v=", (uint32_t)s->votes);
  if (s->votes >= 2) p = hms(put(p, " utc="), utc(s, s->now_ms), 1);
  p = kv(p, " lost=", s->lost);
  p = kv(p, " pre=", s->pre);
  say(api, s, p);
  if (!s->ok && s->now_ms < RUN_MS) return -2;
  return s->votes;
}
#endif
