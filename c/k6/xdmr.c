/* xdmr: DMR detect-and-mute inside Listen for ID (xdmr.h). The DMA and clock set-up are the
   dmrdet module's (c/k6/mod/dmrdet.c), which muted real DMR on 440.925 on the K6 (2026-11-08).
   SPDX-License-Identifier: MIT */
#include "xdmr.h"
#include "cwid.h"

#define GPIOB 0x48000400u
#define RCC_AHBENR 0x40021014u
#define RCC_APB2ENR 0x40021018u
#define TIM14_CNT 0x40002024u
#define TIM17 0x40014800u
#define TIM17_CNT (TIM17 + 0x24)
#define TIM17EN (1u << 18)
#define DMA 0x40020000u
#define DMA_IFCR (DMA + 0x04)
#define DMA_CCR1 (DMA + 0x08)
#define DMA_CNDTR1 (DMA + 0x0C)
#define DMA_CPAR1 (DMA + 0x10)
#define DMA_CMAR1 (DMA + 0x14)
#define DMA_ON ((3u << 12) | (1u << 7) | (1u << 5) | 1u) /* PL very high, MINC, CIRC, EN */
#define AF_BITS 0x0F00u        /* REG_47's AF output select: 0 mute */
#define GAIN13 0x03FFu         /* REG_13 frozen: LNA short, LNA, mixer, PGA all max */
#define CPS8_0 4560u           /* 5.39 MS/s, what dmrdet measured on the K6 (ks=5374-5393) */
#define RATE_MIN (40u * 96000u)/* a gap-free stretch this long measures the DMA's rate */

static uint32_t rd(uint32_t a) { return *(volatile uint32_t *)a; }
static void wr(uint32_t a, uint32_t v) { *(volatile uint32_t *)a = v; }

/* TIM17 free-running at 96 MHz with its DMA request on; DMA channel 1 from GPIOB's IDR (the
   ring's address and size go in each step) */
static const uint32_t XD_SETUP[][2] = {
    {TIM17 + 0x00, 0}, {TIM17 + 0x28, 0}, {TIM17 + 0x2C, 0xFFFF}, {TIM17 + 0x14, 1},
    {TIM17 + 0x10, 0}, {TIM17 + 0x0C, 1u << 8}, {TIM17 + 0x00, 1}, {DMA_CCR1, 0},
    {DMA_IFCR, 0xF}, {DMA_CPAR1, GPIOB + 0x10}};
static const uint32_t XD_TEARDOWN[][2] = {
    {TIM17 + 0x00, 0}, {TIM17 + 0x0C, 0}, {DMA_CCR1, 0}, {DMA_IFCR, 0xF}};

static void xd_wr_list(const uint32_t (*l)[2], int n) {
  int i;
  for (i = 0; i < n; i++) wr(l[i][0], l[i][1]);
}

static char *xd_put(char *p, const char *s) {
  while (*s) *p++ = *s++;
  return p;
}

static char *xd_kv(char *p, const char *k, int v) {
  p = xd_put(p, k);
  if (v < 0) *p++ = '-', v = -v;
  return cw_utoa(p, (uint32_t)v);
}

static void xd_say(xd_t *s, char *line, char *p) {
  *p = 0;
  s->api->say(line);
}

static int xd_rssi(const k6_api_t *api) { return KM_RSSI_DBM(api->fd_read(0x67)); }

static uint32_t xd_now(xd_t *s) {
  uint32_t c17 = rd(TIM17_CNT) & 0xFFFF, c14 = rd(TIM14_CNT) & 0xFFFF;
  uint32_t coarse = ((c14 - s->c14) & 0xFFFF) * 9600u, fine = (c17 - s->c17) & 0xFFFF;
  int32_t d = (int32_t)(int16_t)(uint16_t)(fine - coarse);
  s->c17 = c17, s->c14 = c14;
  s->t += coarse + (uint32_t)d;
  return s->t;
}

/* the speaker's audio on or off (only when the listen plays on it) */
static void xd_af(xd_t *s, int on) {
  if (!s->spk || on == s->af) return;
  s->af = (uint8_t)on;
  s->api->fd_write(0x47, (uint16_t)(on ? s->r47 : s->r47 & ~AF_BITS));
}

/* one step's ring and detector, on xd_step's stack */
typedef struct {
  xd_t *s;
  uint8_t *ring;
  uint32_t laps, lastw, consumed, p0, t0, tp, lap, busy;
} xd_run_t;

static uint32_t xd_poll(xd_run_t *r) {
  uint32_t w = XD_RSZ - (rd(DMA_CNDTR1) & 0xFFFF);
  if (w < r->lastw) r->laps++;
  r->lastw = w;
  return r->laps * XD_RSZ + w;
}

/* the DMA's rate over the gap-free stretch that ends now */
static void xd_rate(xd_run_t *r, uint32_t t) {
  xd_t *s = r->s;
  uint32_t dt = t - r->t0, dp = r->laps * XD_RSZ + r->lastw - r->p0, c;
  if (dt < RATE_MIN || dt >= (1u << 24) || !dp) return;
  c = ds_udiv(dt << 8, dp);
  if (c <= 4000u || c >= 5200u) return;
  /* the first measurement of a listen stands (a 2% wrong grid finds syncs but no
     confirmations); after that, a running mean */
  s->cps8 = s->rated ? (3u * s->cps8 + c) >> 2 : c;
  s->rated = 1;
}

/* count again from where the DMA is now (the step's start, an overrun, a long pause). The
   samples in between are lost: the detector's clocks (its grid index and count number) move on
   by that time, so its slot raster still holds; after more than ~2.8 s it starts afresh. */
__attribute__((noinline)) static void xd_resync(xd_run_t *r, uint32_t t) {
  xd_t *s = r->s;
  ds_t *d = &s->d;
  uint32_t w = XD_RSZ - (rd(DMA_CNDTR1) & 0xFFFF), gap;
  if (r->t0) {                                /* the stretch that ends: up to its last count */
    xd_rate(r, t);
    s->tc = r->t0 + (((r->consumed - r->p0) >> 4) * s->cps8 >> 4);
  }
  gap = t - s->tc;
  if (!s->tc || gap > (1u << 28)) {
    ds_init(d, s->cps8);
  } else {
    d->j += ds_udiv(gap, 10000u);             /* 96 MHz / 9600 grid points a second */
    d->n += ds_udiv(gap << 2, s->cps8);       /* a count is 64 transfers: gap * 256 / 64 / cps8 */
    ds_gap(d);
  }
  ds_rate(d, s->cps8);
  r->laps = 0, r->lastw = w;
  r->consumed = r->p0 = w & ~63u;
  r->t0 = r->tp = s->tc = t;
}

/* the ones on PB1 (bit 1 of each byte) in 64 samples */
static int xd_ones64(const uint8_t *q) {
  const uint32_t *p = (const uint32_t *)q, m = 0x02020202u;
  uint32_t a = 0, i;
  for (i = 0; i < 16; i += 4) a += (p[i] & m) + (p[i + 1] & m) + (p[i + 2] & m) + (p[i + 3] & m);
  a >>= 1;
  a = (a & 0x00FF00FFu) + ((a >> 8) & 0x00FF00FFu);
  return (int)((a & 0xFFFF) + (a >> 16));
}

/* a sync came out: a confirmed one, or one of DMR's family while it lasts, holds DMR (and the
   mute) XD_KEEP_MS more */
static void xd_hit(xd_run_t *r, int k) {
  xd_t *s = r->s;
  ds_t *d = &s->d;
  uint32_t ms;
  s->hits++;
  if (s->dmr ? d->hfam != s->fam : k != DS_CONF) return;
  ms = s->api->ms();
  s->until = ms + XD_KEEP_MS;
  if (s->dmr) return;
  s->dmr = s->ever = s->pend = 1, s->ndmr++;
  s->fam = (uint8_t)d->hfam, s->sign = d->hsign > 0, s->r2 = (uint8_t)(d->hr2 > 255 ? 255 : d->hr2);
  s->on_ms = ms;
  xd_af(s, 0);
}

/* xl_step's wait between tone detector polls: the ring through the detector for us */
static void xd_wait(void *c, uint32_t us) {
  xd_run_t *r = (xd_run_t *)c;
  xd_t *s = r->s;
  uint32_t t = xd_now(s), end = t + us * 96u, i, k = 0;
  uint32_t ms0 = s->api->ms(), lim = (us >> 10) + 3u;  /* ~us / 1000, no division */
  /* longer than a lap since the last look (a report line): the ring has gone round */
  if (t - r->tp > r->lap) s->lost++, xd_resync(r, t);
  for (;;) {
    uint32_t P = xd_poll(r), t0;
    t = xd_now(s);
    if ((int32_t)(t - end) >= 0) break;
    /* a second clock: if TIM17 or TIM14 stopped, the wait still ends (and says so once) */
    if (!(++k & 255u) && s->api->ms() - ms0 > lim) {
      if (!s->stuck++) s->api->say("DMR stuck");
      break;
    }
    if (P - r->consumed > XD_RSZ - 64u) {
      s->lost++;
      xd_resync(r, t);
      continue;
    }
    if (P - r->consumed < 64u) continue;
    t0 = t;
    for (i = 0; i < 4 && P - r->consumed >= 64u; i++) {
      int k = ds_count(&s->d, 2 * xd_ones64(r->ring + (r->consumed & (XD_RSZ - 1))) - 64);
      r->consumed += 64u;
      if (k) xd_hit(r, k);
    }
    r->busy += xd_now(s) - t0;
  }
  r->tp = t;
}

static const char XD_FAM[DS_NFAM][3] = {"bs", "ms", "t1", "t2"};

/* the 50 standard CTCSS tones, 0.1 Hz */
static const uint16_t XD_CTCSS[50] = {
    670, 693, 719, 744, 770, 797, 825, 854, 885, 915, 948, 974, 1000, 1035, 1072, 1109, 1148,
    1188, 1230, 1273, 1318, 1365, 1413, 1462, 1514, 1567, 1598, 1622, 1655, 1679, 1713, 1738,
    1773, 1799, 1835, 1862, 1899, 1928, 1966, 1995, 2035, 2065, 2107, 2181, 2257, 2291, 2336,
    2418, 2503, 2541};

/* the sub-audio scan's reading, once a step: a standard tone twice in a row is the tone */
static void xd_ctcss(xd_t *s) {
  const k6_api_t *api = s->api;
  uint32_t r = api->fd_read(0x68), tenths, i;
  uint16_t std = 0;
  char line[24], *p;
  if (r & 0x8000) return;
  tenths = ds_udiv((r & 0x1FFF) * 100000u, 206489u);
  for (i = 0; i < 50; i++) {
    uint32_t d = tenths > XD_CTCSS[i] ? tenths - XD_CTCSS[i] : XD_CTCSS[i] - tenths;
    if (d < 10) std = XD_CTCSS[i];
  }
  s->treads++;
  if (std && std == s->tcand && std != s->tone) {
    s->tone = std;
    p = cw_utoa(xd_put(line, "CTCSS "), std);
    xd_say(s, line, p);
  }
  s->tcand = std;
}

int xd_step(xd_t *s, xl_t *x) {
  uint32_t ring[XD_RSZ / 4], t, t1, ms, i;
  xd_run_t r;
  char line[48], *p = line;
  int res;
  if (!s->ok) {
    res = xl_step(x);
    if (s->r51 != 0xFFFF) xd_ctcss(s);
    return res;
  }
  for (i = 0; i < sizeof r; i++) ((uint8_t *)&r)[i] = 0;
  r.s = s, r.ring = (uint8_t *)ring;
  r.lap = ((XD_RSZ - 128u) * s->cps8) >> 8;
  wr(DMA_CCR1, 0);
  wr(DMA_IFCR, 0xF);
  wr(DMA_CMAR1, (uint32_t)ring);
  wr(DMA_CNDTR1, XD_RSZ);
  wr(DMA_CCR1, DMA_ON);
  t = xd_now(s);
  xd_resync(&r, t);
  res = xl_step_w(x, xd_wait, &r);
  t1 = xd_now(s);
  xd_rate(&r, t1);
  s->tc = r.t0 + (((r.consumed - r.p0) >> 4) * s->cps8 >> 4);
  wr(DMA_CCR1, 0);
  s->steps++, s->win += (t1 - t) >> 10, s->busy += r.busy >> 10;
  ms = s->api->ms();
  if (s->pend) {
    s->pend = 0;
    p = xd_put(xd_put(xd_put(line, "DMR on "), XD_FAM[s->fam]), s->sign ? " v" : " d");
    p = xd_kv(xd_kv(p, " r2=", s->r2), " +", (int)(s->on_ms - s->start));
  } else if (s->dmr && (int32_t)(ms - s->until) > 0) {
    s->dmr = 0;
    p = xd_kv(line, "DMR off ", (int)(ms - s->on_ms));
  }
  xd_af(s, !s->dmr && ms - s->start >= XD_HOLD_MS);
  if (p != line) xd_say(s, line, p);
  xd_ctcss(s);
  return res;
}

void xd_start(xd_t *s, const k6_api_t *api, int spk, uint32_t cps8) {
  char line[48], *p;
  uint32_t i;
  for (i = 0; i < sizeof *s; i++) ((uint8_t *)s)[i] = 0;
  s->api = api, s->spk = spk ? 1 : 0, s->af = 1, s->cps8 = cps8 > 4000u && cps8 < 5200u ? cps8 : CPS8_0;
  s->start = api->ms();
  s->r51 = api->fd_read(0x51);
  api->fd_write(0x51, 0x0300);                /* the sub-audio scan: wide filter, TX tone off */
  if (rd(GPIOB) & (3u << 2)) {               /* PB1 not an input: no IF to read */
    api->say("DMR off pb1");
    return;
  }
  s->r33 = api->fd_read(0x33), s->r34 = api->fd_read(0x34), s->r47 = api->fd_read(0x47);
  s->r7e = api->fd_read(0x7E), s->r13 = api->fd_read(0x13);
  if ((s->r33 & 0x0100) && api->fd_write(0x33, (uint16_t)(s->r33 & ~0x0100)) != 0) {
    api->say("DMR off r33");
    return;
  }
  api->fd_write(0x34, (uint16_t)((s->r34 & 0xFFF0) | 13));
  xd_af(s, 0);                               /* shut for XD_HOLD_MS */
  s->lvl = (int16_t)xd_rssi(api);
  if (s->lvl <= XD_FREEZE_DBM) {
    int a;
    api->fd_write(0x7E, (uint16_t)((s->r7e & 0x0FFF) | 0xB000)); /* AGC fixed at index 3 */
    api->fd_write(0x13, GAIN13);
    api->delay_us(20000);
    a = xd_rssi(api) - s->lvl;
    s->adj = (int16_t)(a < 0 ? 0 : a > 15 ? 15 : a);
    s->frozen = 1;
  }
  s->ahb = rd(RCC_AHBENR), s->apb2 = rd(RCC_APB2ENR);
  wr(RCC_AHBENR, s->ahb | 1u);                /* the DMA's registers read junk with its clock off */
  wr(RCC_APB2ENR, s->apb2 | TIM17EN);
  xd_wr_list(XD_SETUP, sizeof XD_SETUP / sizeof XD_SETUP[0]);
  s->c17 = rd(TIM17_CNT) & 0xFFFF, s->c14 = rd(TIM14_CNT) & 0xFFFF;
  s->ok = 1;
  p = xd_kv(xd_put(line, "DMR listen"), " lvl=", s->lvl);
  p = xd_kv(xd_kv(p, " frozen=", s->frozen), " adj=", s->adj);
  xd_say(s, line, p);
}

int xd_stop(xd_t *s) {
  const k6_api_t *api = s->api;
  char line[80], *p;   /* two lines, each under say's 60 characters (one 64-byte line overran
                          the buffer on the K6 once h, steps and the reads grew: 2026-11-08) */
  if (s->r51 != 0xFFFF) api->fd_write(0x51, s->r51), s->r51 = 0xFFFF;
  if (!s->ok) return 0;
  s->ok = 0;
  xd_wr_list(XD_TEARDOWN, sizeof XD_TEARDOWN / sizeof XD_TEARDOWN[0]);
  wr(RCC_APB2ENR, s->apb2);
  wr(RCC_AHBENR, s->ahb);
  api->fd_write(0x47, s->r47);
  api->fd_write(0x13, s->r13);
  api->fd_write(0x7E, s->r7e);
  api->fd_write(0x34, s->r34);
  api->fd_write(0x33, s->r33);
  p = xd_kv(xd_kv(line, "DMR end n=", s->ndmr), " h=", (int)s->hits);
  p = xd_kv(xd_kv(p, " lost=", (int)s->lost), " steps=", (int)s->steps);
  xd_say(s, line, p);
  p = xd_kv(line, "DMR rate busy=", (int)ds_udiv(s->busy * 100u, s->win + 1u));
  p = xd_kv(p, " ks=", (int)ds_udiv(96000u * 256u, s->cps8));
  p = xd_kv(xd_kv(p, " ctcss=", s->tone), "/", s->treads);
  if (s->stuck) p = xd_kv(p, " stuck=", (int)s->stuck);
  xd_say(s, line, p);
  return s->ndmr;
}

uint32_t xd_flags(const xd_t *s) { return (s->ok && s->dmr ? XD_NOW : 0) | (s->ever ? XD_EVER : 0); }
int xd_adj(const xd_t *s) { return s->ok && s->frozen ? s->adj : 0; }
uint32_t xd_rate8(const xd_t *s) { return s->cps8; }
uint32_t xd_tone(const xd_t *s) { return s->tone; }
uint32_t xd_state_bytes(void) { return sizeof(xd_t); }
