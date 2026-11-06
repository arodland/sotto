/* cwlisten: decode a repeater's CW ID on the K6's channel with the FD6818's tone detector
   (docs/k6-cwid.md)

   The probe run on 2026-11-06 (cwprobe.c) settled which detector to use: the tone detector, in
   single-tone mode, follows CW keying to about 10 ms (REG_0B bit 12 set while a programmed
   tone is present, [11:8] which one), while the AF level REG_6F moves only every ~120 ms and
   REG_64 doesn't move in receive at all. So this module programs 16 tones (600 to 1350 Hz),
   feeds REG_0B bit 12 straight to the Morse decoder (cwid.c) and, for each transmission, says

     K6 MOD say CWID 20wpm lag=10 tone=800 q=98 n=61         speed, mark lag (ms), the tone
                                                               heard most, % of key-down polls on it
     K6 MOD say CWID text AG7EW SOTTO TEST
     K6 MOD say CWID call=AG7EW fl=0 seen=1                  the callsign in it, and how many
                                                               transmissions in a row gave it
     K6 MOD say CWID d +60 -60 +184 ...                      marks and spaces, ms (LISTEN_DUR)

   and once two in a row give the same callsign (the repeater table's rule)

     K6 MOD say CWID ok AG7EW fl=0 rx=440850000 tone=800 wpm=20

   It stops there (LISTEN_STOP), on any key, or after LISTEN_S seconds, and returns 1 when it
   confirmed a callsign. It writes no flash: the repeater table update comes with the explorer's
   rt_* calls; until then a host reads the ok line.

   Build: python -m modem.k6 mod out.wav c/k6/mod/cwlisten.c --key-file KEY --phy F --payload XL
   Needs a K6 image with module API level 2 (fd_read, delay_us, ms, key_down, scratch).
   SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 2
#define K6_MODULE_OPT z
#include "../k6_mod.h"
#include "../cwid.c"

#ifndef LISTEN_S
#define LISTEN_S 1500        /* listen this long at most: two IDs, 10 minutes apart, and some */
#endif
#ifndef LISTEN_STOP
#define LISTEN_STOP 1        /* 1: return as soon as a callsign is confirmed */
#endif
#ifndef LISTEN_DUR
#define LISTEN_DUR 1         /* 1: the CWID d lines (the bench wants them while it's new) */
#endif
#ifndef POLL_MS
#define POLL_MS 2
#endif
#ifndef TONE_TH
#define TONE_TH 24           /* tone detector threshold, REG_24[12:7] (as on the probe run) */
#endif
#ifndef TONE_IDLE
#define TONE_IDLE 1          /* REG_24[6], as on the probe run */
#endif
#ifndef AF_ON
#define AF_ON 1              /* route the receive audio to the speaker (REG_47), to hear the ID */
#endif
#ifndef LISTEN_MIN
#define LISTEN_MIN 21        /* fewer marks and spaces than this (11 marks) with no callsign in it
                                is no ID: only counted. On air a module upload's FFSK gave bursts
                                of 1-7 marks on tone 15; the shortest call (K1E) is 9 marks */
#endif
#define TONE_LO 600
#define TONE_STEP 50

#define NOINLINE __attribute__((noinline))   /* keeps their line buffers off run's frame */

static char *put(char *p, const char *s) { while (*s) *p++ = *s++; return p; }
static char *sdec(char *p, int32_t v) {
  if (v < 0) { *p++ = '-'; v = -v; }
  return cw_utoa(p, (uint32_t)v);
}

/* f = TONE_LO + TONE_STEP i Hz, coefficient 128 cos(2 pi f / 8463 Hz): the probe's table, which
   found a 600 Hz ID as tone 0 */
static const uint8_t tone_coef[16] = {
    116, 113, 111, 109, 106, 103, 100, 97, 94, 91, 88, 84, 80, 77, 73, 69};

typedef struct {
  cw_t cw;
  uint16_t hist[16];        /* key-down polls on each tone, this transmission */
  char last[11];            /* the callsign the last transmission with one gave */
  uint8_t seen;             /* how many transmissions in a row gave it */
  uint8_t fl;               /* its flags (CW_CALL_*) */
  uint8_t tone, q;          /* this transmission's tone index, and % of key-down polls on it */
} work_t;

static char *line_start(char *p) { return put(p, "CWID "); }

/* a finished transmission: report it, and return 1 if it confirmed a callsign, -1 if it was
   too short to be an ID (not reported) */
NOINLINE static int report(const k6_api_t *api, work_t *w) {
  char line[64], call[11], *p;
  const cw_t *c = &w->cw;
  uint32_t sum = 0, top = 0;
  uint8_t fl = 0;
  int i, ok = 0, n = cw_call(c->text, call, &fl);
  if (!n && c->nd < LISTEN_MIN) { ok = -1; goto done; }
  for (i = 0; i < 16; i++) {
    sum += w->hist[i];
    if (w->hist[i] > top) { top = w->hist[i]; w->tone = (uint8_t)i; }
  }
  w->q = (uint8_t)(sum ? cw_div(top * 100u, sum) : 0);
  p = line_start(line);
  p = cw_utoa(p, c->wpm); p = put(p, "wpm lag="); p = sdec(p, c->lag);
  p = put(p, " tone="); p = cw_utoa(p, TONE_LO + TONE_STEP * w->tone);
  p = put(p, " q="); p = cw_utoa(p, w->q);
  p = put(p, " n="); p = cw_utoa(p, c->nd);
  if (c->why) p = put(p, c->why == CW_LONG ? " long" : " full");
  *p = 0; api->say(line);
  p = put(line_start(line), "text "); p = put(p, c->text); *p = 0; api->say(line);
  if (n) {
    for (i = 0; call[i] == w->last[i] && call[i]; i++) {}
    if (call[i] == w->last[i] && fl == w->fl) { if (w->seen < 255) w->seen++; }
    else { for (i = 0; i < 11; i++) w->last[i] = call[i]; w->fl = fl; w->seen = 1; }
    p = put(line_start(line), "call="); p = put(p, call);
    p = put(p, " fl="); p = cw_utoa(p, fl); p = put(p, " seen="); p = cw_utoa(p, w->seen);
    *p = 0; api->say(line);
    if (w->seen == 2) {
      p = put(line_start(line), "ok "); p = put(p, call);
      p = put(p, " fl="); p = cw_utoa(p, fl);
      p = put(p, " rx="); p = cw_utoa(p, api->rx_hz());
      p = put(p, " tone="); p = cw_utoa(p, TONE_LO + TONE_STEP * w->tone);
      p = put(p, " wpm="); p = cw_utoa(p, c->wpm);
      *p = 0; api->say(line);
      ok = 1;
    }
  }
#if LISTEN_DUR
  p = line;
  for (i = 0; i < c->nd; i++) {
    if (p == line) p = put(line_start(line), "d");
    *p++ = ' '; *p++ = (i & 1) ? '-' : '+';
    p = cw_utoa(p, (uint32_t)c->d[i] * CW_Q);
    if (p - line > 52 || i == c->nd - 1) { *p = 0; api->say(line); p = line; }
  }
#endif
done:
  for (i = 0; i < 16; i++) w->hist[i] = 0;
  return ok;
}

NOINLINE static void say_start(const k6_api_t *api) {
  char line[48], *p;
  p = put(line_start(line), "listen rx="); p = cw_utoa(p, api->rx_hz());
  p = put(p, " th="); p = cw_utoa(p, TONE_TH); p = put(p, " s="); p = cw_utoa(p, LISTEN_S);
  *p = 0; api->say(line);
}

NOINLINE static void say_end(const k6_api_t *api, int why, int msgs, int junk) {
  char line[40], *p;
  p = put(line_start(line), "end ");
  p = put(p, why == 3 ? "ok" : why == 2 ? "key" : "time");
  p = put(p, " msgs="); p = cw_utoa(p, (uint32_t)msgs);
  p = put(p, " short="); p = cw_utoa(p, (uint32_t)junk); *p = 0;
  api->say(line);
}

NOINLINE static int run(const k6_api_t *api, work_t *w) {
  int i, stop = 0, msgs = 0, junk = 0, ok = 0;
  uint32_t t, start;

  cw_init(&w->cw);
  for (i = 0; i < 16; i++) w->hist[i] = 0;
  w->last[0] = 0; w->seen = 0; w->fl = 0; w->tone = 0; w->q = 0;

  /* the tone detector as the probe set it up: single tone, 16 tones, its interrupt (the
     firmware re-arms the pager's set-up when the module returns) */
  for (i = 0; i < 16; i++) api->fd_write(0x09, (uint16_t)((i << 12) | tone_coef[i]));
  api->fd_write(0x24, (uint16_t)(0x8000 | (TONE_TH << 7) | (TONE_IDLE << 6) | 0x20 | 0x0F));
  api->fd_write(0x3F, 0x0800);
  api->fd_write(0x02, 0);
#if AF_ON
  api->fd_write(0x47, 0x6040 | (1 << 8));   /* Aura's AF_OUT_BASE | RX audio (AF_STATE 1) */
#endif
  say_start(api);

  start = api->ms();
  while (!stop) {
    uint16_t b;
    int keyed;
    t = api->ms() - start;
    if (t >= (uint32_t)LISTEN_S * 1000u) stop = 1;
    if (api->key_down()) stop = 2;
    b = api->fd_read(0x0B);
    keyed = (b >> 12) & 1;
    if (keyed && w->hist[(b >> 8) & 15] < 0xFFFF) w->hist[(b >> 8) & 15]++;
    if (api->fd_read(0x0C) & 1) { api->fd_write(0x02, 0); (void)api->fd_read(0x02); }
    if (stop ? cw_flush(&w->cw, t) : cw_feed(&w->cw, t, keyed)) {
      if (w->cw.nd) {
        int r = report(api, w);
        if (r < 0) junk++;
        else msgs++;
        if (r > 0) { ok = 1; if (LISTEN_STOP && !stop) stop = 3; }
      }
    }
    {
      uint32_t used = api->ms() - start - t;
      if (used < POLL_MS) api->delay_us((POLL_MS - used) * 1000u);
    }
  }
  say_end(api, stop, msgs, junk);
  return ok;
}

NOINLINE static int run_on_stack(const k6_api_t *api) {
  work_t w;
  return run(api, &w);
}

int k6_main(const k6_api_t *api) {
  uint32_t n = 0;
  void *s;
  if (api->abi != 1 || api->level < 2) return -1;
  s = api->scratch(&n);
  if (s && n >= sizeof(work_t)) return run(api, (work_t *)s);
  return run_on_stack(api);
}
