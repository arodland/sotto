/* dmrdet: listen on one frequency and mute DMR (docs/k6-dmr.md). Receive only.

   The FD6818's receive IF comes in on PB1 (GP0 code 13, the 1-bit stream gp0dma reads by DMA,
   PR #69), and c/k6/dmrsync.h looks for DMR's 48-bit sync words in its FM-demodulated
   frequency. The speaker plays the receive audio (REG_47 AF 1, the amplifier on PB2) while a
   carrier is up (RSSI 10 dB over the floor measured at the start) and no DMR is heard:
     - the audio opens HOLD_MS after the carrier comes up, so a DMR transmission's first syncs
       (its LC headers, or a repeater's idle slot: every 60 ms) can keep it shut;
     - two syncs of one family a whole number of 30 ms slots apart (confirmed) mute it, until
       720 ms after the last sync of that family, so a voice call (a sync every 360 ms a slot)
       stays muted.
   Lines (each `say` costs ~5 ms of samples, so they come only on a change and every 10 s):
     dmr start f=440925000 floor=-121
     D on bs v r2=243 +112                  (DMR: family bs/ms/t1/t2, v voice or d data sync,
                                             rho^2 x 256, ms since the carrier came up)
     D off 2340                             (DMR gone after ms)
     c -95 2400 h=41                        (a carrier came and went: its dBm, ms, and the
                                             syncs heard during it)
     s 40 h=58 lost=0 idle=64 ks=5490       (every 40 s: s, syncs, ring overruns, CPU idle %,
                                             kS/s)
   and the firmware's `K6 MOD ret=` gives the DMR detections.
   Any key stops it. (No screen yet.) The DMA ring and the state sit on the module call's
   stack (~1 KB of its 3.5-6 KB), so the 4 KB store holds only the code.
   The receiver's gain is frozen at GAIN13 (REG_7E bit 15, index 3; REG_13 in BK4819 layout)
   while it listens: the pager's AGC turns a -85 dBm DMR carrier's IF down until it is only
   4-5 dB over GP0's own noise in band, and REG_13 0x03FF gives ~10 dB (dmrcap, 2026-11-08).
   GAIN13 0 leaves the AGC alone.
   Writes REG_33 bit 8, REG_34[3:0] (GP0 code 13), REG_47, REG_7E and REG_13, GPIOB bit 2, the
   MCU's TIM17 and DMA channel 1 (as gp0dma: Jeff OK'd them, 2026-11-06), all put back. No TX.
   Returns the DMR detections, or -3 (old image), -4 (PB1 not an input), -5 (REG_33 refused),
   -6 (dmrrx: under 512 B of scratch for the decoder's filter ring; with 2,720 B the DMA ring and
   the decoder's state go there too, else on the stack).
   SPDX-License-Identifier: MIT */
#ifndef K6_MODULE_LEVEL
#define K6_MODULE_LEVEL 2
#define K6_MODULE_OPT z
#endif
#include "../k6_mod.h"
#ifdef DMR_DECODE
#define DS_FRING 512u           /* 53 ms of matched-filtered grid: a burst and more */
#include "../dmrburst.h"
#endif
#include "../dmrsync.h"

#ifndef FREQ_HZ
#define FREQ_HZ 440925000u
#endif
#ifndef WIDE
#define WIDE 1
#endif
#ifndef RUN_S
#define RUN_S 600u
#endif
#ifndef HOLD_MS
#define HOLD_MS 150u            /* the audio stays shut this long after a carrier comes up */
#endif
#ifndef SQ_DB
#define SQ_DB 10                /* a carrier: RSSI this far over the floor */
#endif
#ifndef S_MS
#define S_MS 40000u             /* the summary line's period: under 44.7 s, where the idle and
                                   window sums (96 MHz cycles) would wrap a u32 */
#endif
#ifndef GAIN13
#define GAIN13 0x03FFu          /* REG_13 while listening: LNA short, LNA, mixer, PGA all max */
#endif
#define CONF_G 6912u            /* 720 ms of grid points */
#define HK_COUNTS 4096u         /* housekeeping every ~48 ms */

#define GPIOB 0x48000400u
#define GPIOB_ODR (GPIOB + 0x14)
#define GPIOB_BSRR (GPIOB + 0x18)
#define GPIOB_BRR (GPIOB + 0x28)
#define SPK (1u << 2)
#define RCC_AHBENR 0x40021014u
#define RCC_APB2ENR 0x40021018u
#define TIM14_CNT 0x40002024u
#define TIM17 0x40014800u
#define TIM17_CNT (TIM17 + 0x24)
#define DMA 0x40020000u
#define DMA_CCR1 (DMA + 0x08)
#define DMA_CNDTR1 (DMA + 0x0C)
#define DMA_CMAR1 (DMA + 0x14)
#define TIM17EN (1u << 18)
#ifndef RSZ
#define RSZ 512u                /* the DMA ring: 93 us at 5.5 MS/s, so housekeeping drains it
                                   between its calls (on the stack) */
#endif
#define AF_FM 0x6140u           /* Aura's AF_OUT_BASE | RX audio */
#define AF_MUTE 0x6040u
#define NOINLINE __attribute__((noinline))

static uint32_t rd(uint32_t a) { return *(volatile uint32_t *)a; }
static void wr(uint32_t a, uint32_t v) { *(volatile uint32_t *)a = v; }

/* register writes in order: TIM17 free-running at 96 MHz with its DMA request on (the channel
   runs flat out anyway, gp0dma run 7), then DMA channel 1 from GPIOB IDR, circular, 8-bit; the
   ring's address goes in CMAR between the two halves */
static const uint32_t SETUP[][2] = {
    {TIM17 + 0x00, 0}, {TIM17 + 0x28, 0}, {TIM17 + 0x2C, 0xFFFF}, {TIM17 + 0x14, 1},
    {TIM17 + 0x10, 0}, {TIM17 + 0x0C, 1u << 8}, {TIM17 + 0x00, 1}, {DMA_CCR1, 0},
    {DMA + 0x04, 0xF}, {DMA + 0x10, GPIOB + 0x10}, {DMA_CNDTR1, RSZ}};
static const uint32_t TEARDOWN[][2] = {
    {TIM17 + 0x00, 0}, {TIM17 + 0x0C, 0}, {DMA_CCR1, 0}, {DMA + 0x04, 0xF}};

static void wr_list(const uint32_t (*l)[2], int n) {
  int i;
  for (i = 0; i < n; i++) wr(l[i][0], l[i][1]);
}

static char *put(char *p, const char *s) {
  while (*s) *p++ = *s++;
  return p;
}

static char *dec(char *p, uint32_t v) {
  char t[10];
  int n = 0;
  do {
    uint32_t q = ds_udiv(v, 10);
    t[n++] = (char)('0' + v - 10 * q);
    v = q;
  } while (v);
  while (n) *p++ = t[--n];
  return p;
}

static char *kv(char *p, const char *k, int v) {
  p = put(p, k);
  if (v < 0) *p++ = '-', v = -v;
  return dec(p, (uint32_t)v);
}

static int rssi(const k6_api_t *api) { return KM_RSSI_DBM(api->fd_read(0x67)); }

#ifdef DMR_DECODE
#include "dmrdec.inc"
#endif

typedef struct {
  ds_t d;
  /* clock: 96 MHz cycles, unwrapped from TIM17 and Aura's 100 us TIM14 */
  uint32_t t, c17, c14;
  /* the ring */
  uint8_t *ring;
  uint32_t laps, lastw, consumed, p0, t0, cps8, lost, idle, tp, tpoll, rhalf;
  uint32_t thk, rem, ms, win, next_s; /* housekeeping's clock: ms since the start */
  /* the listener */
  int floor, db, cdb, car, af, dmr, pend, nd;
  int32_t fam, sign;
  uint32_t car_j, on_j, until, hits, chits, r2;
  uint32_t ahb, apb2, odr, r33, r34, r47, r7e, r13;
#ifdef DMR_DECODE
  dx_t *x; /* in scratch when the block has room (11,776 B images), else on the stack */
#endif
} st_t;

static uint32_t now(st_t *s) {
  uint32_t c17 = rd(TIM17_CNT) & 0xFFFF, c14 = rd(TIM14_CNT) & 0xFFFF;
  uint32_t coarse = ((c14 - s->c14) & 0xFFFF) * 9600u, fine = (c17 - s->c17) & 0xFFFF;
  int32_t d = (int32_t)(int16_t)(uint16_t)(fine - coarse);
  s->c17 = c17, s->c14 = c14;
  s->t += coarse + (uint32_t)d;
  return s->t;
}

static uint32_t poll(st_t *s) {
  uint32_t w = RSZ - (rd(DMA_CNDTR1) & 0xFFFF);
#ifdef DMR_DECODE
  /* a burst's decoding can outlast the ring: the laps from the clock when it was long */
  uint32_t t = now(s), dt = t - s->tpoll;
  s->tpoll = t;
  if (dt > s->rhalf && s->cps8) {
    uint32_t est = ds_udiv(dt << 6, s->cps8 >> 2), frac = (w - s->lastw) & (RSZ - 1);
    if (est < (1u << 24) && est + RSZ / 2 >= frac) s->laps += ds_udiv(est - frac + RSZ / 2, RSZ);
  }
#endif
  if (w < s->lastw) s->laps++;
  s->lastw = w;
  return s->laps * RSZ + w;
}

/* count again from where the DMA is now (after a pause or an overrun) */
NOINLINE static void resync(st_t *s) {
  uint32_t w = RSZ - (rd(DMA_CNDTR1) & 0xFFFF);
#ifdef DMR_DECODE
  {
    /* the decoder predicts bursts by grid index, so the grid must count the time it missed:
       the transfers since the last poll from the clock and the DMA's rate, the whole rings
       among them from the clock (good to far under a ring), the rest from the DMA */
    uint32_t t = now(s), est = ds_udiv((t - s->tpoll) << 6, s->cps8 >> 2);
    uint32_t frac = (w - s->lastw) & (RSZ - 1), laps = ds_udiv(est - frac + RSZ / 2, RSZ);
    uint32_t P = s->laps * RSZ + s->lastw + laps * RSZ + frac;
    s->tpoll = t;
    if (s->cps8 && est < (1u << 24) && est + RSZ / 2 >= frac) {
      ds_skip(&s->d, ((P & ~63u) - s->consumed) >> 6);
    }
  }
#endif
  s->laps = 0, s->lastw = w;
  s->consumed = s->p0 = w & ~63u;
  s->t0 = s->tp = now(s);
  ds_gap(&s->d);
}

/* the ones on PB1 (bit 1 of each byte) in 64 samples */
static int ones64(const uint8_t *q) {
  const uint32_t *p = (const uint32_t *)q, m = 0x02020202u;
  uint32_t a = 0, i;
  for (i = 0; i < 16; i += 4) a += (p[i] & m) + (p[i + 1] & m) + (p[i + 2] & m) + (p[i + 3] & m);
  a >>= 1;
  a = (a & 0x00FF00FFu) + ((a >> 8) & 0x00FF00FFu);
  return (int)((a & 0xFFFF) + (a >> 16));
}

static const char FAM[DS_NFAM][3] = {"bs", "ms", "t1", "t2"};

static void say(const k6_api_t *api, char *line, char *p) {
  *p = 0;
  api->say(line);
}

/* grid points (9600 a second) -> ms */
NOINLINE static int g2ms(uint32_t g) { return (int)ds_udiv(g * 5u, 48u); }

/* a sync came out of the detector: a confirmed one, or any of DMR's family while it lasts,
   keeps the audio shut 720 ms more */
static void hit(st_t *s, int r) {
  ds_t *d = &s->d;
  s->hits++, s->chits++;
#ifdef DMR_DECODE
  dx_hit(s->x, d);
#endif
  if (s->dmr ? d->hfam != s->fam : r != DS_CONF) return;
  s->until = d->hj + CONF_G;
  if (!s->dmr)
    s->dmr = s->pend = 1, s->fam = d->hfam, s->sign = d->hsign, s->r2 = d->hr2, s->on_j = d->hj;
}

/* ~every 48 ms, when the CPU has a moment: the clock, keys, the DMA's rate, the carrier, the
   audio, and at most one line; 1 to stop */
NOINLINE static int housekeeping(const k6_api_t *api, st_t *s) {
  char line[64], *p = line;
#ifdef DMR_DECODE
  char *q8;
#endif
  uint32_t j = s->d.j, t = s->t, P = s->laps * RSZ + s->lastw, dt = t - s->thk, q;
  int car, af;
  s->thk = t, s->win += dt;
  q = ds_udiv(dt + s->rem, 96000u);
  s->rem = dt + s->rem - q * 96000u;
  s->ms += q;
  if ((api->level >= 4 ? api->key_poll() : api->key_down()) || s->ms >= RUN_S * 1000u) return 1;
  /* the DMA's rate over this gap-free stretch, once it is ~90 ms long */
  if (t - s->t0 > (1u << 23)) {
    if (t - s->t0 < (1u << 24) && P != s->p0) {
      s->cps8 = (3 * s->cps8 + ds_udiv((t - s->t0) << 8, P - s->p0)) >> 2;
#ifdef DMR_DECODE
      s->rhalf = (RSZ * (s->cps8 >> 2)) >> 7;               /* half the ring, in cycles */
#endif
#ifdef DMR_DECODE
      if (!dx_locked(s->x, &s->d)) /* while DMR's own slots time the grid, they set its step */
#endif
      ds_rate(&s->d, s->cps8);
    }
    s->t0 = t, s->p0 = P;
  }
  s->db = rssi(api);
  car = s->db >= s->floor + SQ_DB;
  if (car != s->car) {
    s->car = car;
    if (car) {
      s->car_j = j, s->chits = 0, s->cdb = s->db;
    } else {
      if (s->dmr) s->until = j;
#ifdef DMR_DECODE
      dx_end(s->x);
#endif
      p = kv(kv(kv(line, "c ", s->cdb), " ", g2ms(j - s->car_j)), " h=", (int)s->chits);
    }
  } else if (s->pend) {
    s->pend = 0, s->nd++;
    p = put(put(put(line, "D on "), FAM[s->fam]), s->sign > 0 ? " v" : " d");
    p = kv(kv(p, " r2=", (int)s->r2), " +",
           s->car && (int32_t)(s->on_j - s->car_j) > 0 ? g2ms(s->on_j - s->car_j) : 0);
  } else if (s->dmr && (int32_t)(j - s->until) > 0) {
    s->dmr = 0;
    p = kv(line, "D off ", g2ms(j - s->on_j));
#ifdef DMR_DECODE
    dx_end(s->x);
  } else if ((q8 = dx_house(s->x, line)) != 0) {
    p = q8;
#endif
  } else if (s->ms >= s->next_s) {
    s->next_s += S_MS;
    p = kv(kv(line, "s ", (int)ds_udiv(s->ms, 1000u)), " h=", (int)s->hits);
    p = kv(kv(p, " lost=", (int)s->lost), " idle=",
           (int)ds_udiv((s->idle >> 8) * 100u, (s->win >> 8) + 1u));
    p = kv(p, " ks=", (int)ds_udiv(96000u * 256u, s->cps8));
#ifdef DMR_DECODE
    p = kv(kv(p, " L=", (int)s->x->nlc), " E=", (int)s->x->nemb);
#endif
    s->idle = s->win = 0;
  }
  af = s->car && !s->dmr && j - s->car_j >= HOLD_MS * 96u / 10u;
  if (af != s->af) s->af = af, api->fd_write(0x47, af ? AF_FM : AF_MUTE);
#ifdef DMR_DECODE
  if (dx_lcd(api, s->x) && p == line) resync(s);
#endif
  if (p != line) {
    say(api, line, p);
    resync(s);
  }
  return 0;
}

NOINLINE static void run(const k6_api_t *api, st_t *s) {
  char line[64], *p;
  uint32_t i, hk = 0, P;
  /* the grid starts at gp0dma's measured 5.49 MS/s; housekeeping measures the real rate within
     ~90 ms (st_t came zeroed: ds_init's work but the rate and the empty candidate) */
  s->cps8 = 4476u;
  ds_rate(&s->d, s->cps8);
#ifdef DMR_DECODE
  s->rhalf = (RSZ * (s->cps8 >> 2)) >> 7;
#endif
  s->d.cfam = -1;
  p = kv(line, "dmr start f=", (int)FREQ_HZ);
  p = kv(p, " floor=", s->floor);
  say(api, line, p);
  s->c17 = rd(TIM17_CNT) & 0xFFFF, s->c14 = rd(TIM14_CNT) & 0xFFFF;
  resync(s);
  s->thk = s->t, s->next_s = S_MS;
  for (;;) {
    uint32_t t;
    P = poll(s), t = now(s);
    if (P - s->consumed > RSZ - 64u) {
      s->lost++;
      resync(s);
      continue;
    }
    if (P - s->consumed < 64u) {
      s->idle += t - s->tp;
      s->tp = t;
      if (hk < HK_COUNTS) continue;
    } else {
      s->tp = t;
      for (i = 0; i < 4 && P - s->consumed >= 64u; i++) {
        int r = ds_count(&s->d, 2 * ones64(s->ring + (s->consumed & (RSZ - 1))) - 64);
        s->consumed += 64u;
        if (r) hit(s, r);
      }
#ifdef DMR_DECODE
      if (s->x->nq) dx_poll(s->x, &s->d);
#endif
      hk += i;
      if (hk < 4 * HK_COUNTS) continue; /* never idle: housekeeping anyway */
    }
    hk = 0;
    if (housekeeping(api, s)) return;
    s->tp = now(s);
  }
}

static void zero(void *p, uint32_t n) {
  while (n) ((volatile uint8_t *)p)[--n] = 0;
}

NOINLINE static int go(const k6_api_t *api, st_t *s);

#ifdef DMR_DECODE
/* a block too small for the rings and the decoder in scratch (9,760 B): they go on the stack */
NOINLINE static int on_stack(const k6_api_t *api, st_t *s) {
  uint32_t ring[RSZ / 4];
  dx_t x;
  zero(&x, sizeof x);
  s->ring = (uint8_t *)ring;
  s->x = &x;
  return go(api, s);
}
#endif

int k6_main(const k6_api_t *api) {
  st_t st, *s = &st;
  if (api->level < 2) return -3;
  zero(s, sizeof *s);
#ifdef DMR_DECODE
  {
    uint32_t n = 0;           /* scratch: the matched filter's ring, then the DMA ring and the decoder */
    uint8_t *sc = (uint8_t *)api->scratch(&n);
    if (n < DS_FRING) return -6;
    s->d.f = (int8_t *)sc;
    if (n < DS_FRING + RSZ + sizeof(dx_t)) return on_stack(api, s);
    s->ring = sc + DS_FRING;
    s->x = (dx_t *)(sc + DS_FRING + RSZ);
    zero(s->x, sizeof(dx_t));
    return go(api, s);
  }
#else
  {
    uint32_t ring[RSZ / 4];
    s->ring = (uint8_t *)ring;
    return go(api, s);
  }
#endif
}

NOINLINE static int go(const k6_api_t *api, st_t *s) {
  if (rd(GPIOB) & (3u << 2)) return -4;
  s->apb2 = rd(RCC_APB2ENR);
  s->ahb = rd(RCC_AHBENR);
  wr(RCC_AHBENR, s->ahb | 1u); /* the DMA's registers read junk with its clock off */
  s->r33 = api->fd_read(0x33);
  s->r34 = api->fd_read(0x34);
  s->r47 = api->fd_read(0x47);
  s->r7e = api->fd_read(0x7E);
  s->r13 = api->fd_read(0x13);
  if ((s->r33 & 0x0100) && api->fd_write(0x33, (uint16_t)(s->r33 & ~0x0100)) != 0) {
    wr(RCC_AHBENR, s->ahb);
    return -5;
  }
  api->fd_write(0x47, AF_MUTE);
  api->tune(FREQ_HZ, WIDE); /* away from the upload's frequency: no need to wait for it */
  api->fd_write(0x34, (uint16_t)((s->r34 & 0xFFF0) | 13));
  if (GAIN13) {
    api->fd_write(0x7E, (uint16_t)((s->r7e & 0x0FFF) | 0xB000)); /* AGC fixed at index 3 */
    api->fd_write(0x13, GAIN13);
  }
  api->delay_us(100000);
  s->floor = rssi(api); /* the floor: start on a clear channel */
  s->odr = rd(GPIOB_ODR) & SPK;
  wr(GPIOB_BSRR, SPK);
  wr(RCC_APB2ENR, s->apb2 | TIM17EN);
  wr_list(SETUP, sizeof SETUP / sizeof SETUP[0]);
  wr(DMA_CMAR1, (uint32_t)s->ring);
  wr(DMA_CCR1, (3u << 12) | (1u << 7) | (1u << 5) | 1u); /* PL very high, MINC, CIRC, EN */
  run(api, s);
  /* put it all back */
  wr_list(TEARDOWN, sizeof TEARDOWN / sizeof TEARDOWN[0]);
  wr(RCC_APB2ENR, s->apb2);
  wr(RCC_AHBENR, s->ahb);
  wr(s->odr ? GPIOB_BSRR : GPIOB_BRR, SPK);
  api->fd_write(0x47, (uint16_t)s->r47);
  api->fd_write(0x13, (uint16_t)s->r13);
  api->fd_write(0x7E, (uint16_t)s->r7e);
  api->fd_write(0x34, (uint16_t)s->r34);
  api->fd_write(0x33, (uint16_t)s->r33);
  return s->nd;
}
