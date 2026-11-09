/* aisclk: set the K6's clock from AIS (docs/k6-ais.md). Receive only.

   The capture is aiscap's (aispoll.h): the IF tap on PB1 polled 256 reads a count, AIS channel
   B (162.025 MHz) and A (161.975) a minute each, the gain frozen at the maximum, and a burst
   recorded whole when its energy rises over the floor. A burst DEC_DB or more over the floor
   (run 2: frames came only from 12 dB up) is decoded on the radio (aisrx.h: mixer, CIC, CORDIC,
   8 clock phases, HDLC, CRC, from four start points), ~20 ms of CPU, and its frames set the clock:

     message 4 or 11 (a base station's report, or a UTC response): the date and the UTC second.
       With a SOTDMA slot number (sync state 0 or 1, time-out 2, 4 or 6) whose second agrees,
       the burst's start flag was at slot x 60/2250 s + 32 bits into that minute: a fine fix
       (UTC to well under a millisecond, less the radio path's ~70 us at 20 km). Without one, a
       coarse fix (the second, to within a second).
     any slot number from a station on UTC (messages 1, 2, 4, 11, 18) once the clock is within
       a few seconds: the minute is the nearest one, the slot gives the rest: a fine fix.
   The flag's time on the module's clock is the capture's end (api->ticks, 100 us) less the
   counts after the flag at the count rate TIM17 measured, less the receiver's delay (the chip
   filter and CIC, ~0.6 bit on the model, AR_DELAY_US). A fine fix more than JUMP_MS from the
   last within 10 minutes is refused (a station off UTC) and said as such.

   The clock is the UTC (100 us of day) less the module's 100 us clock, as the core clock
   (proposed API level 8, docs/k6-aprs-clock.md) will take it from either this module or
   aprsclk: clock_set(utc, date, w) with w the fix's interval, AIS's ~2 ms or APRS's ~1000.
   Until that exists, both say the same clk line.

   Lines (serial / telemetry, at most 60 characters):
     ais clk start a=161975000 b=162025000 g13=03ff
     f B hz=80870 fl=1234                      a turn: count rate, floor
     a 4 3669705 B r=15 s=784 t=123456         a frame: type, MMSI, channel, dB over the floor,
                                               slot (if any), its flag on the module's clock
                                               (100 us)
     clk 03:20:20.907 w=2 v=3 d=09/11 j=-3 p=-23
                                               UTC now, interval ms (2 fine, 1000 coarse), fine
                                               fixes, day/month, this fix less the clock before
                                               it (100 us), the K6's clock rate against UTC
                                               (ppm, once two fine fixes are a minute apart)
     x 3669705 j=12345 refused                 a fine fix too far from the clock
     m B n=12 fr=3 ms=60105                    the turn's bursts over DEC_DB, frames
     ais clk none                              no frame in GIVEUP_S: no AIS here (the caller
                                               then runs aprsclk: AIS first, APRS if none, as
                                               Jeff asked 2026-11-09)
     ais clk done fr=9 fine=7 utc=03:25:31.512
   Screen (level 6) after each decode, LED (level 7) lit while frames are said. A key ends it
   (else RUN_S). Writes REG_34[3:0], REG_33 bit 8, REG_7E and REG_13, the MCU's TIM17 and its
   clock enable, all put back. Returns the fine fixes, or -2 (a key at the start), -3 (old image),
   -4 (PB1 not an input), -5 (REG_33 refused), -7 (TIM17 in use), -8 (scratch too small).
   SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 2
#define K6_MODULE_OPT z
#include "../k6_mod.h"
#include "../aisrx.h"

#ifndef CH_A_HZ
#define CH_A_HZ 161975000u
#endif
#ifndef CH_B_HZ
#define CH_B_HZ 162025000u
#endif
#ifndef FIRST_B
#define FIRST_B 1
#endif
#ifndef SWITCH_S
#define SWITCH_S 60u
#endif
#ifndef RUN_S
#define RUN_S 900u
#endif
#ifndef DEC_DB
#define DEC_DB 11 /* decode a burst this far over the floor */
#endif
#ifndef AR_DELAY_US
#define AR_DELAY_US 60 /* the receiver's delay to the flag (model: ~0.6 bit) */
#endif
#ifndef JUMP_MS
#define JUMP_MS 100
#endif
#ifndef GIVEUP_S
#define GIVEUP_S 120u /* no frame by then: no AIS here, hand over to aprsclk (0: never) */
#endif
#include "aispoll.h"

#define DAY100 864000000u
#define MIN100 600000u
#define MAXF 4

typedef struct {
  uint32_t off;   /* UTC (100 us of day) less the module's clock */
  uint32_t tfix;  /* the module's clock at the last fine fix */
  uint32_t off0, t0; /* the first fine fix, for the rate */
  int32_t ppm;
  uint32_t nfine, nfr;
  uint8_t set; /* 0 not yet, 1 to the second, 2 fine */
  uint8_t day, mon, haveppm;
  char ln[64]; /* the line being said (one buffer: the stack is small) */
} clk_t;

static char *dec2(char *p, uint32_t v) {
  *p++ = (char)('0' + udiv(v, 10));
  *p++ = (char)('0' + v - 10 * udiv(v, 10));
  return p;
}

static char *sdec(char *p, int32_t v) {
  if (v < 0) *p++ = '-', v = -v;
  return dec(p, (uint32_t)v);
}

/* 100 us of day -> hh:mm:ss.mmm */
static char *hms(char *p, uint32_t t) {
  uint32_t ms = udiv(t, 10), s = udiv(ms, 1000), m = udiv(s, 60), h = udiv(m, 60);
  p = dec2(p, h);
  *p++ = ':';
  p = dec2(p, m - 60 * h);
  *p++ = ':';
  p = dec2(p, s - 60 * m);
  *p++ = '.';
  ms -= 1000 * s;
  *p++ = (char)('0' + udiv(ms, 100));
  return dec2(p, ms - 100 * udiv(ms, 100));
}

/* v within a day or two of [0, DAY100) into it */
static uint32_t mod_day(int32_t v) {
  if (v < 0) v += (int32_t)DAY100;
  if (v < 0) v += (int32_t)DAY100;
  if (v >= (int32_t)DAY100) v -= (int32_t)DAY100;
  if (v >= (int32_t)DAY100) v -= (int32_t)DAY100;
  return (uint32_t)v;
}

static int32_t wrap_day(int32_t v) {
  if (v > (int32_t)(DAY100 / 2)) v -= (int32_t)DAY100;
  if (v <= -(int32_t)(DAY100 / 2)) v += (int32_t)DAY100;
  return v;
}

/* the ring's oldest count to the front (three reversals) */
static void rev(int8_t *a, uint32_t i, uint32_t j) {
  while (i + 1 < j) {
    int8_t t = a[i];
    a[i++] = a[--j], a[j] = t;
  }
}

static void say_clk(const k6_api_t *api, clk_t *k, ap_ck_t *ck, int32_t j) {
  char *p = put(k->ln, "clk ");
  p = hms(p, mod_day((int32_t)(k->off + ap_now(api, ck))));
  p = put(p, k->set == 2 ? " w=2 v=" : " w=1000 v=");
  p = dec(p, k->nfine);
  if (k->day) p = put(p, " d="), p = dec2(p, k->day), *p++ = '/', p = dec2(p, k->mon);
  if (k->set == 2) p = put(p, " j="), p = sdec(p, j);
  if (k->haveppm) p = put(p, " p="), p = sdec(p, k->ppm);
  *p = 0;
  api->say(k->ln);
}

/* a fine fix: the flag at UTC utc (100 us of day) and at t on the module's clock */
static void fine(const k6_api_t *api, clk_t *k, uint32_t utc, uint32_t t, uint32_t mmsi,
                 ap_ck_t *ck) {
  uint32_t off = mod_day((int32_t)(utc - t));
  int32_t j = k->set ? wrap_day((int32_t)(off - k->off)) : 0;
  char *p;
  if (k->set == 2 && (j > JUMP_MS * 10 || j < -JUMP_MS * 10) && t - k->tfix < 10 * MIN100) {
    p = put(k->ln, "x ");
    p = dec(p, mmsi);
    p = put(p, " j=");
    p = sdec(p, j);
    p = put(p, " refused");
    *p = 0;
    api->say(k->ln);
    return;
  }
  if (!k->nfine) k->off0 = off, k->t0 = t;
  else if (t - k->t0 >= MIN100) {
    /* (off - off0) / (t - t0) in ppm: UTC gains on the module's clock when it runs slow */
    int32_t d = wrap_day((int32_t)(off - k->off0));
    uint32_t u = ar_muldiv((uint32_t)(d < 0 ? -d : d), 1000000u, t - k->t0);
    k->ppm = d < 0 ? (int32_t)u : -(int32_t)u, k->haveppm = 1;
  }
  k->off = off, k->tfix = t, k->set = 2, k->nfine++;
  say_clk(api, k, ck, j);
}

/* the frames of one burst */
static void frames(const k6_api_t *api, clk_t *k, const ar_frame_t *f, int nf, const cap_t *c,
                   int b, int d, ap_ck_t *ck) {
  int i;
  char *p;
  for (i = 0; i < nf; i++) {
    const uint8_t *m = f[i].b;
    uint32_t typ = ar_uint(m, 0, 6), mmsi = ar_uint(m, 8, 30), comm = 0, sec = 60, t, cyc;
    uint32_t slot = 0xFFFF, sync, to;
    int len = f[i].nbytes * 8;
    /* the flag on the module's clock: the capture's end (half a tick on) less the counts
       after the flag at the count rate, less the receiver's delay */
    cyc = ar_muldiv((NCAP << 16) - f[i].pos, c->c1, (NCAP - PRE) << 16) + AR_DELAY_US * 96u;
    t = c->end_t - udiv(cyc, 9600); /* end_t + 1/2 less cyc, rounded */
    if (len >= 168 && (typ == 1 || typ == 2 || typ == 4 || typ == 11 || typ == 18)) {
      comm = ar_uint(m, 149, 19);
      if (typ <= 2) sec = ar_uint(m, 137, 6);
      if (typ == 18) {
        sec = ar_uint(m, 133, 6);
        if (ar_uint(m, 148, 1)) comm = 0xFFFFFFFF; /* ITDMA */
      }
      if (typ == 4 || typ == 11) sec = ar_uint(m, 72, 6);
      sync = comm >> 17, to = (comm >> 14) & 7;
      if (comm != 0xFFFFFFFF && sync <= 1 && (to == 2 || to == 4 || to == 6) &&
          (comm & 0x3FFF) < 2250)
        slot = comm & 0x3FFF;
    }
    k->nfr++;
    p = put(k->ln, "a ");
    p = dec(p, typ);
    *p++ = ' ';
    p = dec(p, mmsi);
    p = put(p, b ? " B r=" : " A r=");
    p = dec(p, (uint32_t)d);
    if (slot != 0xFFFF) p = put(p, " s="), p = dec(p, slot);
    p = put(p, " t=");
    p = dec(p, t);
    *p = 0;
    api->say(k->ln);
    {
      /* the slot's time in its minute (100 us): slot x 800/3 + 32 bits; its second */
      uint32_t inmin = slot != 0xFFFF ? udiv(slot * 800u + 101u, 3) : 0;
      uint32_t ssec = udiv(inmin, 10000);
      int agree = slot != 0xFFFF && (sec == ssec || sec + 1 == ssec || sec == ssec + 1);
      if ((typ == 4 || typ == 11) && len >= 168) {
        uint32_t y = ar_uint(m, 38, 14), mo = ar_uint(m, 52, 4), dd = ar_uint(m, 56, 5);
        uint32_t h = ar_uint(m, 61, 5), mi = ar_uint(m, 66, 6);
        if (y < 2020 || y > 2099 || !mo || mo > 12 || !dd || h > 23 || mi > 59 || sec > 59)
          continue;
        k->day = (uint8_t)dd, k->mon = (uint8_t)mo;
        if (agree) {
          fine(api, k, (h * 60 + mi) * MIN100 + inmin, t, mmsi, ck);
        } else if (k->set < 2) {
          k->off = mod_day((int32_t)(((h * 60 + mi) * 60 + sec) * 10000u + 5000u - t));
          k->set = 1;
          say_clk(api, k, ck, 0);
        }
      } else if (slot != 0xFFFF && k->set && (sec > 59 || agree)) {
        /* the nearest minute to the clock's time for the slot's start */
        uint32_t m0 = mod_day((int32_t)(k->off + t - inmin + MIN100 / 2));
        m0 = udiv(m0, MIN100) * MIN100;
        fine(api, k, mod_day((int32_t)(m0 + inmin)), t, mmsi, ck);
      }
    }
  }
}

static void screen(const k6_api_t *api, clk_t *k, uint32_t now, int b) {
  char *line = k->ln, *p;
  if (api->level < 6) return;
  api->lcd_text(0, "AIS CLOCK");
  p = put(line, "UTC ");
  if (k->set) p = hms(p, mod_day((int32_t)(k->off + now)));
  else p = put(p, "--:--:--");
  *p = 0;
  api->lcd_text(1, line);
  p = put(line, k->set == 2 ? "fine " : k->set ? "to 1 s " : "listening ");
  p = dec(p, k->nfine);
  p = put(p, b ? " ch B" : " ch A");
  *p = 0;
  api->lcd_text(2, line);
  p = put(line, "frames ");
  p = dec(p, k->nfr);
  *p = 0;
  api->lcd_text(3, line);
  api->lcd_flush();
}

int k6_main(const k6_api_t *api) {
  int8_t *buf;
  uint16_t *ph;
  ar_frame_t *fr;
  bp_t bp;
  cap_t c;
  ap_ck_t ck;
  clk_t k;
  uint32_t room, turn_n, turn_f, until, hz, apb2, need;
  uint16_t r33, r34, r7e, r13;
  int32_t fl;
  int b = FIRST_B, quit = 0, d, nf;
  char line[64], *p;
  if (api->abi != 1 || api->level < 2) return -3;
  if (api->key_down()) return -2;
  buf = (int8_t *)api->scratch(&room);
  need = NCAP + 2 * AR_NPH(NCAP, PRE);
  need = (need + 3) & ~3u;
  if (room < need + MAXF * sizeof(ar_frame_t)) return -8;
  ph = (uint16_t *)(buf + NCAP);
  fr = (ar_frame_t *)(buf + need);
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
  k.set = 0, k.nfine = 0, k.nfr = 0, k.day = 0, k.mon = 0, k.haveppm = 0, k.ppm = 0;
  k.off = 0, k.tfix = 0, k.off0 = 0, k.t0 = 0;
  p = put(line, "ais clk start a=");
  p = dec(p, CH_A_HZ);
  p = put(p, " b=");
  p = dec(p, CH_B_HZ);
  p = put(p, " g13=");
  p = hex4(p, GAIN13);
  *p = 0;
  api->say(line);
  bp.x1 = bp.x2 = bp.y1 = bp.y2 = bp.e = 0;
  while (!quit && api->ms() < RUN_S * 1000u) {
    api->tune(b ? CH_B_HZ : CH_A_HZ, WIDE);
    api->fd_write(0x34, (uint16_t)((r34 & 0xFFF0) | 13));
    if (GAIN13) {
      api->fd_write(0x7E, (uint16_t)((r7e & 0x0FFF) | 0x8000 | (3u << 12)));
      api->fd_write(0x13, GAIN13);
    }
    api->delay_us(30000);
    screen(api, &k, ap_now(api, &ck), b);
    hz = calibrate(&bp, &fl);
    ap_now(api, &ck);
    p = put(line, b ? "f B hz=" : "f A hz=");
    p = dec(p, hz);
    p = put(p, " fl=");
    p = dec(p, (uint32_t)fl);
    *p = 0;
    api->say(line);
    turn_n = 0, turn_f = 0;
    until = api->ms() + SWITCH_S * 1000u;
    if (until > RUN_S * 1000u) until = RUN_S * 1000u;
    if (GIVEUP_S && !k.nfr && until > GIVEUP_S * 1000u && api->ms() < GIVEUP_S * 1000u)
      until = GIVEUP_S * 1000u;
    for (;;) {
      bp.e = fl;
      listen(api, &ck, buf, &bp, fl * TRIG_X, until, &c);
      if (c.got < 0) quit = 1;
      if (c.got <= 0) break;
      d = db_over(c.emax, fl);
      if (d >= DEC_DB) {
        turn_n++;
        rev(buf, 0, c.head), rev(buf, c.head, NCAP), rev(buf, 0, NCAP);
        nf = ar_decode(buf, NCAP, PRE, c.c1, ph, fr, MAXF);
        if (nf) {
          if (api->level >= 7) api->led(1);
          frames(api, &k, fr, nf, &c, b, d, &ck);
          if (api->level >= 7) api->led(0);
          screen(api, &k, ap_now(api, &ck), b);
          turn_f += (uint32_t)nf;
        }
      }
      if (api->ms() >= until) break;
    }
    if (GIVEUP_S && !k.nfr && api->ms() >= GIVEUP_S * 1000u) quit = 2;
    p = put(line, b ? "m B n=" : "m A n=");
    p = dec(p, turn_n);
    p = put(p, " fr=");
    p = dec(p, turn_f);
    p = put(p, " ms=");
    p = dec(p, api->ms());
    *p = 0;
    api->say(line);
    b = !b;
  }
  wr(TIM17 + 0x00, 0);
  wr(RCC_APB2ENR, apb2);
  api->fd_write(0x13, r13);
  api->fd_write(0x7E, r7e);
  api->fd_write(0x34, r34);
  api->fd_write(0x33, r33);
  if (quit == 2) api->say("ais clk none");
  p = put(line, "ais clk done fr=");
  p = dec(p, k.nfr);
  p = put(p, " fine=");
  p = dec(p, k.nfine);
  if (k.set) p = put(p, " utc="), p = hms(p, mod_day((int32_t)(k.off + ap_now(api, &ck))));
  *p = 0;
  api->say(line);
  return (int)k.nfine;
}
