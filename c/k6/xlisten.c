/* xlisten: Listen for ID, a step at a time (xlisten.h). The detector set-up and the report
   lines are cwlisten's (c/k6/mod/cwlisten.c), which decoded 6 of 6 IDs on air.
   SPDX-License-Identifier: MIT */
#include "xlisten.h"
#include "tonedet.h"

#define LISTEN_MIN 21

static char *put(char *p, const char *s) {
  while (*s) *p++ = *s++;
  return p;
}
static char *sdec(char *p, int32_t v) {
  if (v < 0) *p++ = '-', v = -v;
  return cw_utoa(p, (uint32_t)v);
}
static char *head(char *p) { return put(p, "CWID "); }

int xl_tone_hz(const xl_t *x) { return TD_LO + TD_STEP * x->tone; }

void xl_start(xl_t *x, const k6_api_t *api, uint32_t rx_hz, uint32_t max_s) {
  uint8_t *d = (uint8_t *)x;
  char line[48], *p;
  uint32_t i;
  for (i = 0; i < sizeof *x; i++) d[i] = 0;
  x->api = api, x->rx_hz = rx_hz, x->max_ms = max_s * 1000u;
  cw_init(&x->cw);
  td_on(api);
  x->start = api->ms();
  p = put(head(line), "listen rx="); p = cw_utoa(p, rx_hz);
  p = put(p, " th="); p = cw_utoa(p, TD_TH); p = put(p, " s="); p = cw_utoa(p, max_s);
  *p = 0;
  api->say(line);
}

/* a finished transmission: 1 if it confirmed a callsign, -1 if too short to be an ID */
static int report(xl_t *x) {
  const k6_api_t *api = x->api;
  const cw_t *c = &x->cw;
  char line[64], call[11], *p;
  uint32_t sum = 0, top = 0;
  uint8_t fl = 0;
  int i, ok = 0, n = cw_call(c->text, call, &fl);
  /* no stack bytes after the call's NUL: they're copied out whole, and the app compares the
     calls of two listens byte for byte (on the bench the same AG7EW never matched) */
  for (i = 0; i < 11 && call[i]; i++) {}
  for (; i < 11; i++) call[i] = 0;
  for (i = 0; i < CW_TEXT; i++) x->text[i] = c->text[i];
  if (!n && c->nd < LISTEN_MIN) {
    ok = -1;
    goto done;
  }
  for (i = 0; i < 16; i++) {
    sum += x->hist[i];
    if (x->hist[i] > top) top = x->hist[i], x->tone = (uint8_t)i;
  }
  x->q = (uint8_t)(sum ? cw_div(top * 100u, sum) : 0);
  p = head(line);
  p = cw_utoa(p, c->wpm); p = put(p, "wpm lag="); p = sdec(p, c->lag);
  p = put(p, " tone="); p = cw_utoa(p, (uint32_t)xl_tone_hz(x));
  p = put(p, " q="); p = cw_utoa(p, x->q);
  p = put(p, " n="); p = cw_utoa(p, c->nd);
  *p = 0;
  api->say(line);
  p = put(head(line), "text "); p = put(p, c->text); *p = 0;
  api->say(line);
  if (n) {
    for (i = 0; call[i] == x->last[i] && call[i]; i++) {}
    if (call[i] == x->last[i] && fl == x->fl) {
      if (x->seen < 255) x->seen++;
    } else {
      for (i = 0; i < 11; i++) x->last[i] = call[i];
      x->fl = fl, x->seen = 1;
    }
    p = put(head(line), "call="); p = put(p, call);
    p = put(p, " fl="); p = cw_utoa(p, fl); p = put(p, " seen="); p = cw_utoa(p, x->seen);
    *p = 0;
    api->say(line);
    if (x->seen == 2) {
      for (i = 0; i < 11; i++) x->call[i] = call[i];
      p = put(head(line), "ok "); p = put(p, call);
      p = put(p, " fl="); p = cw_utoa(p, fl);
      p = put(p, " rx="); p = cw_utoa(p, x->rx_hz);
      p = put(p, " tone="); p = cw_utoa(p, (uint32_t)xl_tone_hz(x));
      p = put(p, " wpm="); p = cw_utoa(p, c->wpm);
      *p = 0;
      api->say(line);
      ok = 1;
    }
  }
done:
  for (i = 0; i < 16; i++) x->hist[i] = 0;
  return ok;
}

int xl_step(xl_t *x) { return xl_step_w(x, 0, 0); }

int xl_step_w(xl_t *x, xl_wait_fn wait, void *c) {
  const k6_api_t *api = x->api;
  uint32_t t0 = api->ms() - x->start, t = t0;
  int res = XL_RUN;
  while (t - t0 < XL_STEP_MS) {
    uint16_t b;
    int r = 0;
    if (t >= x->max_ms) {
      if (cw_flush(&x->cw, t) && x->cw.nd) report(x);
      return XL_TIME;
    }
    b = td_read(api);
    x->keyed = (uint8_t)((b >> 12) & 1);
    if (x->keyed && x->hist[(b >> 8) & 15] < 0xFFFF) x->hist[(b >> 8) & 15]++;
    if (cw_feed(&x->cw, t, x->keyed) && x->cw.nd) {
      r = report(x);
      if (r < 0) x->junk++;
      else x->msgs++, res = XL_MSG;
      if (r > 0) return XL_OK;
    }
    {
      uint32_t used = api->ms() - x->start - t;
      if (used < XL_POLL_MS) {
        if (wait) wait(c, (XL_POLL_MS - used) * 1000u);
        else api->delay_us((XL_POLL_MS - used) * 1000u);
      }
    }
    t = api->ms() - x->start;
  }
  return res;
}

void xl_stop(xl_t *x, int why) {
  const k6_api_t *api = x->api;
  char line[40], *p;
  td_off(api);
  p = put(head(line), "end ");
  p = put(p, why == XL_OK ? "ok" : why == XL_TIME ? "time" : "key");
  p = put(p, " msgs="); p = cw_utoa(p, x->msgs);
  p = put(p, " short="); p = cw_utoa(p, x->junk);
  *p = 0;
  api->say(line);
}

uint32_t xl_state_bytes(void) { return sizeof(xl_t); }

void xl_info(const xl_t *x, xl_info_t *o) {
  int i;
  const char *c = x->call[0] ? x->call : x->last;
  o->ms = x->api->ms() - x->start;
  o->msgs = x->msgs, o->junk = x->junk;
  o->keyed = x->keyed, o->seen = x->seen, o->fl = x->fl, o->wpm = x->cw.wpm;
  o->tone_hz = (uint16_t)xl_tone_hz(x);
  for (i = 0; i < 11; i++) o->call[i] = c[i];
  o->call[10] = 0;
  for (i = 0; i < 20 && x->text[i]; i++) o->text[i] = x->text[i];
  for (; i < 21; i++) o->text[i] = 0;
}
