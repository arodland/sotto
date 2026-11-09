/* aisships: the ships around the K6, from AIS (docs/k6-ais.md "aisships"; the scope Jeff chose
   to build 2026-11-09). Receive only. Tuned once to 162.009 MHz, between the two AIS channels:
   the IF tap is taken before the channel filter (run 5), so channel A (161.975) lands at 17.1 kHz
   in the counts, mirrored, and channel B (162.025) at 32.9 kHz (run 6). Every burst over DEC_DB is
   decoded at both places (c/k6/aisrx.h's ar_decode_if), so no channel switching.

   Kept, for up to NSHIP stations (the oldest goes when it's full):
     1, 2, 3  class A position report: position, speed, course
     18       class B position report: the same
     4, 11    base station report: position (the first one also sets home, see below)
     24       class B static data, part A: name
   (5, 19 and 21 run two slots, longer than the ring's 31 ms.) Distance and bearing are
   from home: the first base station heard, until a home position is set some other way.

   Says (one per frame, then the frame as NMEA for OpenCPN and the like):
     ais ships start hz=162009000 g13=03ff
     f hz=81850 fl=330                        each turn: the count rate and the floor
     a 1 366123456 B r=12 d=12.3 b=245        type, MMSI, channel, dB over the floor, km, degrees
     !AIVDM,1,1,,B,13u?etPv2;0n:dDPwUM1U1Cb069D,0*24
     m n=12 fr=3 ships=4 ms=60105             each turn (SWITCH_S): bursts decoded, frames, table
     ais ships done fr=40 ships=5
   Screen (level 6): a title line and the five nearest stations, name (or MMSI), km and bearing.
   LED (level 7) lit while frames are said. A key ends it (else RUN_S). Writes REG_34[3:0], REG_33
   bit 8, REG_7E and REG_13, the MCU's TIM17 and its clock enable, all put back. Returns the
   stations in the table, or -2 (a key at the start), -3 (old image), -4 (PB1 not an input), -5
   (REG_33 refused), -7 (TIM17 in use), -8 (scratch too small: upload with --telemetry 0).
   SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 2
#define K6_MODULE_OPT z
#include "../k6_mod.h"
#include "../aisrx.h"

#ifndef TUNE_HZ
#define TUNE_HZ 162009000u
#endif
/* where each channel lands in the counts, 2^32 x Hz / 96 MHz: |16927 + (channel - TUNE_HZ)| */
#ifndef IF_A_Q32
#define IF_A_Q32 763833u /* 17,073 Hz */
#endif
#ifndef IF_B_Q32
#define IF_B_Q32 1473129u /* 32,927 Hz */
#endif
#ifndef SWITCH_S
#define SWITCH_S 60u /* a turn: the floor measured again, an m line */
#endif
#ifndef RUN_S
#define RUN_S 900u
#endif
#ifndef DEC_DB
#define DEC_DB 8 /* decode a burst this far over the floor (whole band: the 4 s local junk is 7) */
#endif
#ifndef NSHIP
#define NSHIP 16
#endif
#define MAXF 3
#define PRE 384u
#define NCAP (PRE + 2576u)
#define TRIG_X 2
#define RAW_TRIG 1 /* the trigger watches the whole band: channel B is outside the band-pass */
#include "aispoll.h"

#define NOPOS 0x7FFFFFFF
#define NODIST 0xFFFF

typedef struct {
  uint32_t mmsi;
  int32_t lat, lon; /* 1/10000 minute (AIS's units), NOPOS: none yet */
  uint16_t dist;    /* 0.1 km from home, NODIST: unknown */
  uint16_t brg;     /* degrees from home */
  uint16_t seen;    /* the module's seconds at its last frame */
  char name[14];    /* "" until a 24 names it (the screen shows 10) */
} ship_t;

typedef struct {
  ship_t s[NSHIP];
  int32_t hlat, hlon; /* home, NOPOS: none yet */
  int32_t hcos;       /* cos(home latitude), Q15 */
  uint32_t nfr, seq;
  char ln[60]; /* a say line: an NMEA sentence is at most 58 characters */
} tab_t;

/* cos at 0, 6 .. 90 degrees, Q15 */
static const uint16_t COS6[16] = {32767, 32587, 32051, 31163, 29934, 28377, 26509, 24351,
                                  21925, 19260, 16384, 13328, 10126, 6813,  3425,  0};

static int32_t sbits(const uint8_t *b, uint32_t a, uint32_t n) {
  return (int32_t)(ar_uint(b, a, n) << (32 - n)) >> (32 - n);
}

/* the ring's oldest count to the front (three reversals) */
static void rev(int8_t *a, uint32_t i, uint32_t j) {
  while (i + 1 < j) {
    int8_t t = a[i];
    a[i++] = a[--j], a[j] = t;
  }
}

/* atan2 and length by CORDIC (aisrx.h's table): *ang in 2^16 a turn from x towards y; returns
   the length x 1.6468 */
static uint32_t cordic(int32_t x, int32_t y, uint32_t *ang) {
  int32_t t, j;
  uint32_t a = 0;
  if (x < 0) x = -x, y = -y, a = 32768;
  for (j = 0; j < 11; j++) {
    t = x;
    if (y > 0) x += y >> j, y -= t >> j, a += AR_ATN[j];
    else x -= y >> j, y += t >> j, a -= AR_ATN[j];
  }
  *ang = a & 0xFFFF;
  return (uint32_t)x;
}

/* a station's distance and bearing from home (flat earth: fine to a few hundred km) */
static void place(const tab_t *t, ship_t *s) {
  uint32_t ang, len;
  int32_t dy, dx;
  s->dist = NODIST;
  if (t->hlat == NOPOS || s->lat == NOPOS) return;
  dy = s->lat - t->hlat; /* north, 0.1852 m */
  dx = s->lon - t->hlon; /* east, in longitude */
  if (dy > 4000000 || dy < -4000000 || dx > 8000000 || dx < -8000000) return; /* far */
  dx = dx < 0 ? -(int32_t)ar_muldiv((uint32_t)-dx, (uint32_t)t->hcos, 32768)
              : (int32_t)ar_muldiv((uint32_t)dx, (uint32_t)t->hcos, 32768);
  len = cordic(dy, dx, &ang);
  len = udiv(len + 444, 889); /* x 0.1852 m / 1.6468 / 100 m */
  if (len >= NODIST) return;
  s->dist = (uint16_t)len;
  s->brg = (uint16_t)((ang * 360u + 32768u) >> 16);
  if (s->brg == 360 || !len) s->brg = 0; /* !len: home itself */
}

static void set_home(tab_t *t, int32_t lat, int32_t lon) {
  uint32_t a = (uint32_t)(lat < 0 ? -lat : lat), i = udiv(a, 3600000), f; /* 6 degrees */
  int k;
  if (i > 14) i = 14;
  f = a - i * 3600000; /* into the step, < 3,600,000: x 32767 needs care */
  t->hcos = COS6[i] - (int32_t)ar_muldiv(COS6[i] - COS6[i + 1], f, 3600000);
  t->hlat = lat, t->hlon = lon;
  for (k = 0; k < NSHIP; k++) place(t, &t->s[k]);
}

static ship_t *find(tab_t *t, uint32_t mmsi, uint16_t now) {
  int k, old = 0;
  ship_t *s;
  for (k = 0; k < NSHIP; k++) {
    if (t->s[k].mmsi == mmsi) return &t->s[k];
    if (!t->s[k].mmsi) break;
    if ((uint16_t)(now - t->s[k].seen) > (uint16_t)(now - t->s[old].seen)) old = k;
  }
  s = &t->s[k < NSHIP ? k : old];
  s->mmsi = mmsi, s->lat = s->lon = NOPOS, s->dist = NODIST, s->name[0] = 0;
  return s;
}

static void name6(char *d, const uint8_t *b, uint32_t a) {
  int i, end = 0;
  for (i = 0; i < 13; i++) {
    uint32_t c = ar_uint(b, a + 6 * (uint32_t)i, 6);
    if (!c) break; /* '@': the end */
    c = c < 32 ? c + 64 : c;
    d[i] = (char)c;
    if (c != ' ') end = i + 1;
  }
  d[end] = 0;
}

static char hex1(uint32_t v) { return (char)(v < 10 ? '0' + v : 'A' + v - 10); }

/* the frame as !AIVDM sentences of at most 38 payload characters (a say line holds 60) */
static void nmea(const k6_api_t *api, tab_t *t, const ar_frame_t *f, int b) {
  uint32_t bits = (uint32_t)f->nbytes * 8, nch = udiv(bits + 5, 6), nfr = udiv(nch + 37, 38);
  uint32_t i = 0, fr, j, cs;
  char *p, *q;
  t->seq = t->seq >= 9 ? 0 : t->seq + 1;
  for (fr = 1; fr <= nfr; fr++) {
    p = put(t->ln, "!AIVDM,");
    *p++ = (char)('0' + nfr), *p++ = ',', *p++ = (char)('0' + fr), *p++ = ',';
    if (nfr > 1) *p++ = (char)('0' + t->seq);
    *p++ = ',', *p++ = b ? 'B' : 'A', *p++ = ',';
    for (j = 0; j < 38 && i < nch; j++, i++) {
      uint32_t a = 6 * i, v = a + 6 <= bits ? ar_uint(f->b, a, 6) : ar_uint(f->b, a, bits - a)
                                                                         << (a + 6 - bits);
      *p++ = (char)(v < 40 ? v + 48 : v + 56);
    }
    *p++ = ',', *p++ = (char)('0' + (fr == nfr ? nch * 6 - bits : 0)), *p++ = '*';
    for (cs = 0, q = t->ln + 1; q < p - 1; q++) cs ^= (uint8_t)*q;
    *p++ = hex1(cs >> 4), *p++ = hex1(cs & 15), *p = 0;
    api->say(t->ln);
  }
}

/* km to 0.1 under 100 km, padded to 4 characters when pad */
static char *dist_str(char *p, const ship_t *s, int pad) {
  if (s->dist == NODIST) return put(p, "  --");
  if (s->dist < 1000) {
    uint32_t w = udiv(s->dist, 10);
    if (pad && w < 10) *p++ = ' ';
    p = dec(p, w);
    *p++ = '.', *p++ = (char)('0' + s->dist - 10 * w);
  } else {
    p = dec(p, udiv(s->dist + 5, 10));
  }
  return p;
}

static void frames(const k6_api_t *api, tab_t *t, const ar_frame_t *f, int nf, int b, int d) {
  int i;
  uint16_t now = (uint16_t)udiv(api->ms(), 1000);
  for (i = 0; i < nf; i++) {
    const uint8_t *m = f[i].b;
    uint32_t typ = ar_uint(m, 0, 6), len = (uint32_t)f[i].nbytes * 8;
    int32_t lat = NOPOS, lon = NOPOS;
    ship_t *s = find(t, ar_uint(m, 8, 30), now);
    char *p;
    if (typ <= 3 && typ && len >= 168) lon = sbits(m, 61, 28), lat = sbits(m, 89, 27);
    else if (typ == 18 && len >= 168) lon = sbits(m, 57, 28), lat = sbits(m, 85, 27);
    else if ((typ == 4 || typ == 11) && len >= 168) lon = sbits(m, 79, 28), lat = sbits(m, 107, 27);
    else if (typ == 24 && len >= 160 && !ar_uint(m, 38, 2)) name6(s->name, m, 40);
    if (lat != NOPOS && lat < 54000000 && lat > -54000000 && lon < 108000000 && lon > -108000000) {
      s->lat = lat, s->lon = lon;
      if ((typ == 4 || typ == 11) && t->hlat == NOPOS) set_home(t, lat, lon);
      place(t, s);
    }
    s->seen = now;
    t->nfr++;
    p = put(t->ln, "a ");
    p = dec(p, typ);
    *p++ = ' ';
    p = dec(p, s->mmsi);
    p = put(p, b ? " B r=" : " A r=");
    p = dec(p, (uint32_t)d);
    if (s->dist != NODIST) p = put(p, " d="), p = dist_str(p, s, 0), p = put(p, " b="), p = dec(p, s->brg);
    *p = 0;
    api->say(t->ln);
    nmea(api, t, &f[i], b);
  }
}

static uint32_t nships(const tab_t *t) {
  uint32_t n = 0;
  int k;
  for (k = 0; k < NSHIP; k++) n += t->s[k].mmsi != 0;
  return n;
}

/* the title and the five nearest (unplaced ones after them, newest first) */
static void screen(const k6_api_t *api, tab_t *t) {
  uint32_t shown = 0, line;
  char *p;
  int k;
  if (api->level < 6) return;
  p = put(t->ln, "AIS ");
  p = dec(p, nships(t));
  p = put(p, " stations");
  *p = 0;
  api->lcd_text(0, t->ln);
  for (line = 1; line < 6; line++) {
    int best = -1;
    for (k = 0; k < NSHIP; k++) {
      const ship_t *s = &t->s[k];
      if (!s->mmsi || (shown >> k & 1)) continue;
      if (best < 0 || s->dist < t->s[best].dist ||
          (s->dist == t->s[best].dist && s->seen > t->s[best].seen))
        best = k;
    }
    p = t->ln;
    if (best >= 0) {
      const ship_t *s = &t->s[best];
      char *e = p + 11;
      shown |= 1u << best;
      if (s->name[0]) {
        const char *q = s->name;
        while (*q && p < e - 1) *p++ = *q++;
      } else {
        p = dec(p, s->mmsi);
      }
      while (p < e) *p++ = ' ';
      p = dist_str(p, s, 1);
      if (s->dist != NODIST) {
        *p++ = ' ';
        if (s->brg < 100) *p++ = '0';
        if (s->brg < 10) *p++ = '0';
        p = dec(p, s->brg);
      }
    }
    *p = 0;
    api->lcd_text((int)line, t->ln);
  }
  api->lcd_flush();
}

int k6_main(const k6_api_t *api) {
  int8_t *buf;
  uint16_t *ph;
  ar_frame_t *fr;
  tab_t *t;
  bp_t bp;
  cap_t c;
  ap_ck_t ck;
  uint32_t room, turn_n, turn_f, until, hz, apb2, need;
  uint16_t r33, r34, r7e, r13;
  int32_t fl;
  int quit = 0, d, nf, b;
  char *p;
  if (api->abi != 1 || api->level < 2) return -3;
  if (api->key_down()) return -2;
  buf = (int8_t *)api->scratch(&room);
  need = NCAP + 2 * AR_NPH(NCAP, PRE);
  need = (need + 3) & ~3u;
  if (room < need + MAXF * sizeof(ar_frame_t) + sizeof(tab_t)) return -8;
  ph = (uint16_t *)(buf + NCAP);
  fr = (ar_frame_t *)(buf + need);
  t = (tab_t *)(fr + MAXF);
  if (rd(GPIOB) & (3u << 2)) return -4;
  apb2 = rd(RCC_APB2ENR);
  if (apb2 & TIM17EN) return -7;
  r33 = api->fd_read(0x33);
  r34 = api->fd_read(0x34);
  r7e = api->fd_read(0x7E);
  r13 = api->fd_read(0x13);
  if ((r33 & 0x0100) && api->fd_write(0x33, (uint16_t)(r33 & ~0x0100)) != 0) return -5;
  wr(RCC_APB2ENR, apb2 | TIM17EN);
  wr(TIM17 + 0x00, 0);
  wr(TIM17 + 0x28, 0);      /* PSC: 96 MHz */
  wr(TIM17 + 0x2C, 0xFFFF); /* ARR */
  wr(TIM17 + 0x00, 1);      /* CEN */
  ck.last = api->ticks() & 0xFFFF, ck.t = 0;
  for (p = (char *)t; p < (char *)(t + 1); p++) *(volatile char *)p = 0;
  t->hlat = NOPOS;
  p = put(t->ln, "ais ships start hz=");
  p = dec(p, TUNE_HZ);
  p = put(p, " g13=");
  p = hex4(p, GAIN13);
  *p = 0;
  api->say(t->ln);
  bp.x1 = bp.x2 = bp.y1 = bp.y2 = bp.e = 0;
  api->tune(TUNE_HZ, WIDE);
  api->fd_write(0x34, (uint16_t)((r34 & 0xFFF0) | 13));
  if (GAIN13) {
    api->fd_write(0x7E, (uint16_t)((r7e & 0x0FFF) | 0x8000 | (3u << 12)));
    api->fd_write(0x13, GAIN13);
  }
  api->delay_us(30000);
  screen(api, t);
  while (!quit && api->ms() < RUN_S * 1000u) {
    hz = calibrate(&bp, &fl);
    ap_now(api, &ck);
    p = put(t->ln, "f hz=");
    p = dec(p, hz);
    p = put(p, " fl=");
    p = dec(p, (uint32_t)fl);
    *p = 0;
    api->say(t->ln);
    turn_n = 0, turn_f = 0;
    until = api->ms() + SWITCH_S * 1000u;
    if (until > RUN_S * 1000u) until = RUN_S * 1000u;
    for (;;) {
      bp.e = fl;
      listen(api, &ck, buf, &bp, fl * TRIG_X, until, &c);
      if (c.got < 0) quit = 1;
      if (c.got <= 0) break;
      d = db_over(c.emax, fl);
      if (d >= DEC_DB) {
        turn_n++;
        rev(buf, 0, c.head), rev(buf, c.head, NCAP), rev(buf, 0, NCAP);
        for (b = 0; b < 2; b++) {
          nf = ar_decode_if(buf, NCAP, PRE, c.c1, b ? IF_B_Q32 : IF_A_Q32, ph, fr, MAXF);
          if (nf) {
            if (api->level >= 7) api->led(1);
            frames(api, t, fr, nf, b, d);
            if (api->level >= 7) api->led(0);
            turn_f += (uint32_t)nf;
          }
        }
        if (turn_f) screen(api, t);
      }
      if (api->ms() >= until) break;
    }
    p = put(t->ln, "m n=");
    p = dec(p, turn_n);
    p = put(p, " fr=");
    p = dec(p, turn_f);
    p = put(p, " ships=");
    p = dec(p, nships(t));
    p = put(p, " ms=");
    p = dec(p, api->ms());
    *p = 0;
    api->say(t->ln);
  }
  wr(TIM17 + 0x00, 0);
  wr(RCC_APB2ENR, apb2);
  api->fd_write(0x13, r13);
  api->fd_write(0x7E, r7e);
  api->fd_write(0x34, r34);
  api->fd_write(0x33, r33);
  p = put(t->ln, "ais ships done fr=");
  p = dec(p, t->nfr);
  p = put(p, " ships=");
  p = dec(p, nships(t));
  *p = 0;
  api->say(t->ln);
  return (int)nships(t);
}
