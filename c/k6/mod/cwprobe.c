/* cwprobe: which FD6818 detector follows a repeater's CW ID? (docs/k6-cwid.md)

   The K6's MCU can't hear the receive audio, so a CW ID has to come from one of the RF chip's
   own detectors. This module listens on the pager's channel for up to PROBE_S seconds (any
   key stops it) and tracks two of them, which feed its Morse decoder (cwid.c) by turns, one
   transmission each (the stack has room for one decoder):

     A  REG_6F[6:0], the AF amplitude in dB (BK4819 map; the FD6818 guide's REG_87H)
     B  REG_64, the voice amplitude (linear), through cw_db

   and sets up the third, the tone detector, in single-tone (SELCALL) mode with 16 tones from
   600 to 1350 Hz (REG_24, REG_09; the guide's REG_94H and its 1050 Hz recipe), logging what it
   does (REG_0B, REG_0C bit 0, REG_02) with times, since how it reports a keyed tone is what
   the bench has to find out. Each finished transmission gives, on the serial report,

     K6 MOD say A 20wpm lag=5 n=61 lo=3 hi=38 dv=1        speed, mark lag, levels (dB)
     K6 MOD say A AG7EW SOTTO TEST                         the text
     K6 MOD say A d +63 -61 +183 ...                       the marks (+) and spaces (-), ms

   then T lines (the tone detector's events) and, at the end, each register's range. Lines
   wait until the transmission is over, so printing never skews the timing.

   Build: python -m modem.k6 mod out.wav c/k6/mod/cwprobe.c --key-file KEY --phy F --payload XL
   Needs a K6 image with module API level 2 (fd_read, delay_us, ms, key_down).
   SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 2
#define K6_MODULE_OPT z      /* -Oz: small enough to leave scratch RAM for the decoder */
#define CW_NDUR 72           /* AG7EW SOTTO TEST is 61 marks and spaces */
#define CW_LVL_LOOK 8
#include "../k6_mod.h"
#include "../cwid.c"

#ifndef PROBE_S
#define PROBE_S 600          /* listen this long at most (an ID comes every 10 minutes) */
#endif
#ifndef POLL_MS
#define POLL_MS 2
#endif
#ifndef TONE_TH
#define TONE_TH 24           /* tone detector threshold, REG_24[12:7], 1 dB/step (UV-K5: 24) */
#endif
#ifndef TONE_IDLE
#define TONE_IDLE 1          /* REG_24[6]: 1 = symbol, idle, symbol (DTMF style), 0 = 5-tone */
#endif
#ifndef AF_ON
#define AF_ON 1              /* route the receive audio to the speaker (REG_47), to hear the ID */
#endif
#ifndef PROBE_USE
#define PROBE_USE 2          /* 0: A only, 1: B only, 2: A and B by turns, one ID each */
#endif
#ifndef PROBE_CALL
#define PROBE_CALL 0         /* the callsign line (cw_call, ~600 B): the host finds it in the text */
#endif
#define NLOG 12

#define NOINLINE __attribute__((noinline))   /* keeps their line buffers off k6_main's frame */

static char *put(char *p, const char *s) { while (*s) *p++ = *s++; return p; }
static char *hex(char *p, uint32_t v, int n) {
  const char *x = "0123456789abcdef";
  while (n--) *p++ = x[(v >> (4 * n)) & 15];
  return p;
}
static char *sdec(char *p, int32_t v) {
  if (v < 0) { *p++ = '-'; v = -v; }
  return cw_utoa(p, (uint32_t)v);
}

/* the tone detector's 16 tones: f = 600 + 50 i Hz, coefficient 128 cos(2 pi f / 8463 Hz)
   (8463 Hz fits Aura's DTMF table and the guide's 1050 Hz entry to within 1.5) */
static const uint8_t tone_coef[16] = {
    116, 113, 111, 109, 106, 103, 100, 97, 94, 91, 88, 84, 80, 77, 73, 69};

typedef struct {
  cw_t cw;
  cw_lvl_t lv[2];           /* the trackers for A and B, both always running */
  uint8_t use;              /* which one feeds the decoder in this transmission */
  uint8_t pending;          /* a finished transmission not yet reported */
  uint8_t lo, hi, dv;       /* its tracker's levels when it finished, dB */
} det_t;

typedef struct {
  uint16_t t;               /* ms since the start, mod 65536 */
  uint16_t v;               /* what changed: see report */
  uint8_t what;             /* 'b' REG_0B high byte changed, 'i' interrupt (REG_02 cause) */
} ev_t;

NOINLINE static void report(const k6_api_t *api, det_t *d) {
  char line[64], *p, name = (char)('A' + d->use);
  const cw_t *c = &d->cw;
  int i;
#if PROBE_CALL
  char call[11];
  uint8_t fl;
#endif
  d->pending = 0;
  if (!c->nd) return;
  p = line; *p++ = name; *p++ = ' ';
  p = cw_utoa(p, c->wpm); p = put(p, "wpm lag="); p = sdec(p, c->lag);
  p = put(p, " n="); p = cw_utoa(p, c->nd);
  p = put(p, " lo="); p = cw_utoa(p, d->lo); p = put(p, " hi="); p = cw_utoa(p, d->hi);
  p = put(p, " dv="); p = cw_utoa(p, d->dv);
  if (c->why) { p = put(p, c->why == CW_LONG ? " long" : " full"); }
  *p = 0; api->say(line);
  p = line; *p++ = name; *p++ = ' '; p = put(p, c->text); *p = 0; api->say(line);
#if PROBE_CALL
  if (cw_call(c->text, call, &fl)) {
    p = line; *p++ = name; p = put(p, " call="); p = put(p, call);
    p = put(p, " fl="); p = cw_utoa(p, fl); *p = 0; api->say(line);
  }
#endif
  p = line;
  for (i = 0; i < c->nd; i++) {
    if (p == line) { *p++ = name; p = put(p, " d"); }
    *p++ = ' '; *p++ = (i & 1) ? '-' : '+';
    p = cw_utoa(p, (uint32_t)c->d[i] * CW_Q);
    if (p - line > 52 || i == c->nd - 1) { *p = 0; api->say(line); p = line; }
  }
}

NOINLINE static void say_events(const k6_api_t *api, const ev_t *ev, int nev, int lost) {
  char line[24], *p;
  int i;
  for (i = 0; i <= nev; i++) {
    if (i == nev && !lost) break;
    p = put(line, "T ");
    if (i == nev) { p = put(p, "lost "); p = cw_utoa(p, (uint32_t)lost); }
    else {
      p = cw_utoa(p, ev[i].t); *p++ = ' '; *p++ = (char)ev[i].what; *p++ = '=';
      p = hex(p, ev[i].v, 4);
    }
    *p = 0;
    api->say(line);
  }
}

NOINLINE static void log_ev(ev_t *ev, int *nev, int *lost, uint32_t t, uint16_t v, char what) {
  if (*nev < NLOG) { ev[*nev].t = (uint16_t)t; ev[*nev].v = v; ev[*nev].what = (uint8_t)what; (*nev)++; }
  else (*lost)++;
}

/* one poll: both trackers, and the decoder from the one in use; on a finished transmission
   keep the levels it ended with */
NOINLINE static void step(det_t *d, uint32_t t, int db_a, int db_b, uint32_t dt, int stop) {
  int ka = cw_lvl(&d->lv[0], db_a, dt), kb = cw_lvl(&d->lv[1], db_b, dt);
  if (stop ? cw_flush(&d->cw, t) : cw_feed(&d->cw, t, d->use ? kb : ka)) {
    const cw_lvl_t *l = &d->lv[d->use];
    d->pending = 1;
    d->lo = (uint8_t)(l->lo >> 8); d->hi = (uint8_t)(l->hi >> 8); d->dv = (uint8_t)(l->dev >> 8);
  }
}

NOINLINE static void range_line(const k6_api_t *api, uint8_t reg, uint16_t lo, uint16_t hi) {
  char line[24], *p = line;
  p = put(p, "R "); p = hex(p, reg, 2); p = put(p, " "); p = hex(p, lo, 4);
  p = put(p, ".."); p = hex(p, hi, 4); *p = 0;
  api->say(line);
}

NOINLINE static void say_start(const k6_api_t *api, const void *sp, const void *w) {
  char line[48], *p;
  p = put(line, "cwprobe sp=0x"); p = hex(p, (uint32_t)(uintptr_t)sp, 8);
  p = put(p, " w=0x"); p = hex(p, (uint32_t)(uintptr_t)w, 8); *p = 0;
  api->say(line);
  p = put(line, "cwprobe rx="); p = cw_utoa(p, api->rx_hz());
  p = put(p, " th="); p = cw_utoa(p, TONE_TH); p = put(p, " idle="); p = cw_utoa(p, TONE_IDLE);
  p = put(p, " s="); p = cw_utoa(p, PROBE_S); *p = 0;
  api->say(line);
}

NOINLINE static void say_end(const k6_api_t *api, int stop, int msgs) {
  char line[40], *p;
  p = put(line, "cwprobe end "); p = put(p, stop == 2 ? "key" : "time");
  p = put(p, " msgs="); p = cw_utoa(p, (uint32_t)msgs); *p = 0;
  api->say(line);
}

typedef det_t work_t;      /* what goes in scratch RAM if it fits */

NOINLINE static int run(const k6_api_t *api, work_t *w) {
  det_t *d = w;
  ev_t ev[NLOG];
  int nev = 0, lost = 0, i, stop = 0, msgs = 0;
  uint32_t t, last = 0, start;
  uint16_t prev0b = 0, mn[6], mx[6];
  static const uint8_t regs[6] = {0x6F, 0x64, 0x65, 0x67, 0x0B, 0x0C};

  cw_init(&d->cw); cw_lvl_init(&d->lv[0]); cw_lvl_init(&d->lv[1]);
  d->pending = 0; d->use = PROBE_USE == 1;
  for (i = 0; i < 6; i++) { mn[i] = 0xFFFF; mx[i] = 0; }

  /* the tone detector: single tone, 16 tones, its interrupt only (the firmware re-arms the
     pager's set-up when the module returns) */
  for (i = 0; i < 16; i++) api->fd_write(0x09, (uint16_t)((i << 12) | tone_coef[i]));
  api->fd_write(0x24, (uint16_t)(0x8000 | (TONE_TH << 7) | (TONE_IDLE << 6) | 0x20 | 0x0F));
  api->fd_write(0x3F, 0x0800);
  api->fd_write(0x02, 0);
#if AF_ON
  api->fd_write(0x47, 0x6040 | (1 << 8));   /* Aura's AF_OUT_BASE | RX audio (AF_STATE 1) */
#endif

  say_start(api, &i, w);

  start = api->ms();
  while (!stop) {
    uint16_t v[6];
    t = api->ms() - start;
    if (t >= (uint32_t)PROBE_S * 1000u) stop = 1;
    if (api->key_down()) stop = 2;
    for (i = 0; i < 6; i++) {
      v[i] = api->fd_read(regs[i]);
      if (v[i] < mn[i]) mn[i] = v[i];
      if (v[i] > mx[i]) mx[i] = v[i];
    }
    /* the tone detector: REG_0B[12:8] (ready, symbol) changes, and interrupts */
    if ((v[4] & 0x1F00) != (prev0b & 0x1F00)) { log_ev(ev, &nev, &lost, t, v[4], 'b'); prev0b = v[4]; }
    if (v[5] & 1) {
      api->fd_write(0x02, 0);                 /* clear, then read: the write latches the causes */
      log_ev(ev, &nev, &lost, t, api->fd_read(0x02), 'i');
    }
    step(d, t, v[0] & 0x7F, cw_db(v[1]), t - last, stop);
    last = t;
    /* report between transmissions (a.cw.active would be set again by a new one) */
    if (d->pending && !d->cw.active) {
      if (d->cw.nd) msgs++;
      report(api, d);
      say_events(api, ev, nev, lost);
      nev = 0; lost = 0;
      if (PROBE_USE == 2 && d->cw.nd) d->use ^= 1;   /* the next ID on the other register */
    }
    {
      uint32_t used = api->ms() - start - t;
      if (used < POLL_MS) api->delay_us((POLL_MS - used) * 1000u);
    }
  }
  say_events(api, ev, nev, lost);
  for (i = 0; i < 6; i++) range_line(api, regs[i], mn[i], mx[i]);
  say_end(api, stop, msgs);
  return msgs;
}

NOINLINE static int run_on_stack(const k6_api_t *api) {
  work_t w;
  return run(api, &w);
}

int k6_main(const k6_api_t *api) {
  uint32_t n = 0;
  void *s;
  if (api->abi != 1 || api->level < 2) return -1;
  /* the decoder's state in the store's spare RAM if it fits, else on the stack */
  s = api->scratch(&n);
  if (s && n >= sizeof(work_t)) return run(api, (work_t *)s);
  return run_on_stack(api);
}
