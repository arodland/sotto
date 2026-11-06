/* iflive: the IF matched-filter receiver (../ifrx.c) running live on the K6, on GP0 code 13's
   stream (PB1, DMA channel 1 as gp0dma and gp0cap run it), against the sender's live run
   (tools/k6_ifcap.py live 4fsk: NG segments of STEP_MS, each a 1.5 s lead of tones 0 and M-1
   in turn, then PN9). This file is the 4FSK receiver; iflive_ffsk.c the FFSK one.
   docs/k6-if-modem.md.

   It waits for the carrier, times the DMA (kS/s), then streams: every 64 DMA samples' count
   goes to ifrx, which finds the lead, locks, and decides symbols. The bits go through a PN9
   check (each bit against the two 5 and 9 back: x^9 + x^5 + 1 needs no sync, and one wrong
   bit fails three checks), counted once 32 checks in a row have passed (the data has
   started). Each segment is a gain step as in gp0cap (0: the pager's AGC, 1..9 REG_7E fixed
   with REG_13 falling) and a fresh acquisition: STEP_MS after the first lock less LEAD_MS,
   before the next segment's lead, the module prints the step's lines, sets the next gain and
   starts the receiver over, so nothing it does between steps (a line holds the CPU ~5 ms, the
   ring lasts 186 us) can upset the count index. Lines:
     live ks=5490 rx=704                    the DMA's kS/s, the receiver's state bytes
     l 3 b=10200 e=12 r=-95 f=9 s=2 q=110 w=4 j=0 L=0 i=61
                                            a step: bits, check fails, RSSI, AFC (1.31 Hz
                                            units), ifrx state (0: no lock), lead fit (of 256),
                                            sweeps, rejects, counts lost, CPU idle %
   BER ~ e / 3 / b. Writes REG_34[3:0], REG_33 bit 8 (PR #68 image), REG_7E, REG_13, the MCU's
   TIM17, DMA channel 1 and their clock enables, all put back. No TX. The ring and the
   receiver live on the stack (~2 KB). Returns NG (the steps done), or -2 (a key), -3 (old
   image), -4 (PB1 not an input), -5 (REG_33 refused), -6 (no carrier), -7 (TIM17 or DMA busy).
   SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 2
#define K6_MODULE_OPT z
#ifndef IFRX_ONLY
#define IFRX_ONLY 1 /* IFRX_4FSK */
#endif
#include "../k6_mod.h"
#include "../ifrx.c"

#define GPIOB 0x48000400u
#define RCC_AHBENR 0x40021014u
#define RCC_APB2ENR 0x40021018u
#define TIM17 0x40014800u
#define TIM17_CNT (TIM17 + 0x24)
#define DMA 0x40020000u
#define DMA_CCR1 (DMA + 0x08)
#define DMA_CNDTR1 (DMA + 0x0C)
#define DMA_CPAR1 (DMA + 0x10)
#define DMA_CMAR1 (DMA + 0x14)
#define TIM17EN (1u << 18)
#define RSZ 1024u
#define L 64u
#ifndef WAIT_S
#define WAIT_S 90
#endif
#ifndef STEP_MS
#define STEP_MS 6000u
#endif
#ifndef LEAD_MS
#define LEAD_MS 1500u /* the sender's lead a segment */
#endif
#ifndef NG
#define NG 10
#endif
#define NCAL 4096u /* counts timed for the DMA's rate */
#define CARRIER_DBM -100
static const uint16_t gains[10] = {0,      0x03FF, 0x03BE, 0x037E, 0x033E,
                                   0x031E, 0x021E, 0x011E, 0x001E, 0x0018};

static uint32_t rd(uint32_t a) { return *(volatile uint32_t *)a; }
static void wr(uint32_t a, uint32_t v) { *(volatile uint32_t *)a = v; }
static uint32_t tim(void) { return rd(TIM17_CNT) & 0xFFFF; }

/* say f with each '%' a signed decimal from v (about 60 characters at most) */
IFRX_NOINLINE static void say_f(const k6_api_t *api, const char *f, const int32_t *v) {
  char line[72], *p = line, t[10];
  while (*f && p < line + 60) {
    if (*f != '%') {
      *p++ = *f++;
    } else {
      int32_t x = *v++;
      uint32_t u, n = 0;
      f++;
      if (x < 0) *p++ = '-', x = -x;
      u = (uint32_t)x;
      do {
        uint32_t q = ifrx_udiv(u, 10);
        t[n++] = (char)('0' + u - q * 10);
        u = q;
      } while (u);
      while (n) *p++ = t[--n];
    }
  }
  *p = 0;
  api->say(line);
}

static int rssi(const k6_api_t *api) { return KM_RSSI_DBM(api->fd_read(0x67)); }

static void stop(void) {
  wr(TIM17 + 0x00, 0);
  wr(TIM17 + 0x0C, 0);
  wr(DMA_CCR1, 0);
  wr(DMA + 0x04, 0xF);
}

static void start(uint8_t *ring) {
  stop();
  wr(DMA_CPAR1, GPIOB + 0x10);
  wr(DMA_CMAR1, (uint32_t)ring);
  wr(DMA_CNDTR1, RSZ);
  wr(TIM17 + 0x28, 0);
  wr(TIM17 + 0x2C, 0xFFFF);
  wr(TIM17 + 0x14, 1);
  wr(TIM17 + 0x10, 0);
  wr(TIM17 + 0x0C, 1u << 8);
  wr(TIM17 + 0x00, 1);
  wr(DMA_CCR1, (3u << 12) | (1u << 7) | (1u << 5) | 1u); /* PL very high, MINC, CIRC, EN */
}

/* L samples at ring offset r: the ones on PB1 (bit 1 of each byte), as a count */
static int count(const uint8_t *ring, uint32_t r) {
  const uint32_t *p = (const uint32_t *)(ring + r);
  uint32_t acc = 0, i;
  for (i = 0; i < L / 4; i += 4)
    acc += (p[i] & 0x02020202u) + (p[i + 1] & 0x02020202u) + (p[i + 2] & 0x02020202u) +
           (p[i + 3] & 0x02020202u);
  acc >>= 1;
  acc = (acc & 0x00FF00FFu) + ((acc >> 8) & 0x00FF00FFu);
  return 2 * (int)((acc & 0xFFFF) + (acc >> 16)) - (int)L;
}

typedef struct {
  uint32_t laps, lastw, produced, consumed;
} ring_t;

static uint32_t avail(ring_t *g) {
  uint32_t w = RSZ - (rd(DMA_CNDTR1) & 0xFFFF);
  if (w < g->lastw) g->laps++;
  g->lastw = w;
  g->produced = g->laps * RSZ + w;
  return g->produced - g->consumed;
}

typedef struct {
  uint32_t ahb, apb2;
  uint16_t r33, r34, r7e, r13;
} saved_t;

static void gain(const k6_api_t *api, const saved_t *sv, int g) {
  api->fd_write(0x7E, g ? (uint16_t)((sv->r7e & 0x0FFF) | 0xB000) : sv->r7e);
  api->fd_write(0x13, g ? gains[g] : sv->r13);
}

/* until the RSSI is at or above (above 1) or under (0) CARRIER_DBM: 0, -2 a key, -6 timeout */
static int wait_level(const k6_api_t *api, int above, uint32_t ms) {
  uint32_t t0 = api->ms();
  while ((rssi(api) >= CARRIER_DBM) != above) {
    if (api->key_down()) return -2;
    if (api->ms() - t0 > ms) return -6;
    api->delay_us(5000);
  }
  return 0;
}

typedef struct {
  uint32_t h, n, err;     /* the last bits, bits counted, check fails */
  uint32_t have, run, on; /* bits in h, checks passed in a row, the data has started */
} pn_t;

IFRX_NOINLINE static void pn_bit(pn_t *p, uint32_t b) {
  if (p->have >= 9) {
    uint32_t f = (b ^ (p->h >> 4) ^ (p->h >> 8)) & 1;
    if (!p->on) {
      p->run = f ? 0 : p->run + 1;
      p->on = p->run >= 32;
    } else {
      p->n++;
      p->err += f;
    }
  } else
    p->have++;
  p->h = (p->h << 1) | b;
}

/* wait for the carrier, time the DMA (its first NCAL counts on TIM17: kS/s), then stream the
   IF into the receiver for NG steps. The steps' ends are reckoned from the first lock (the
   lead's start plus 0.6 .. 1.3 s): STEP_MS on, less LEAD_MS, falls before the next lead. */
IFRX_NOINLINE static int run(const k6_api_t *api, const saved_t *sv, uint8_t *rb) {
  ifrx_t rx;
  ring_t g;
  pn_t pn;
  uint32_t t_end = 0, t0, c0, k = 0, lost = 0, idle = 0, tot = 0, ks = 0;
  int step = 0, r;
  int32_t v[11];
  r = wait_level(api, 1, WAIT_S * 1000u);
  if (r) return r;
  t0 = api->ms();
  ifrx_zero(&g, sizeof(g) / 4);
  gain(api, sv, 0);
  start(rb);
  c0 = tim();
  for (;;) {
    uint32_t a = avail(&g), d, cn = tim();
    d = (cn - c0) & 0xFFFF;
    c0 = cn;
    tot += d;
    if (!ks) {
      g.consumed = g.produced;
      if (g.produced < NCAL * L) continue;
      ks = ifrx_udiv(g.produced * 96, ifrx_udiv(tot, 1000) | 1);
      v[0] = (int32_t)ks, v[1] = sizeof(ifrx_t);
      say_f(api, "live ks=% rx=%", v);
      goto fresh;
    }
    if (a > RSZ - L) {
      /* the ring overran (the receiver too slow): skip what's gone */
      uint32_t nc = a / L;
      g.consumed += nc * L;
      lost += nc;
      ifrx_skip(&rx, nc);
      pn.have = 0;
      continue;
    }
    if (a < L) {
      idle += d;
      continue;
    }
    if (ifrx_push(&rx, count(rb, g.consumed & (RSZ - 1)))) {
      uint32_t s = (uint32_t)(rx.sym ^ (rx.sym >> 1)), b;
      if (rx.gap) pn.have = 0, rx.gap = 0;
      if (!t_end) t_end = api->ms() + STEP_MS - LEAD_MS;
      for (b = (uint32_t)rx.k; b--;) pn_bit(&pn, (s >> b) & 1);
    }
    g.consumed += L;
    if (++k & 255) continue;
    if (api->key_down()) {
      r = -2;
      break;
    }
    if (!t_end) {
      r = -6;
      if (api->ms() - t0 > 3 * STEP_MS) break; /* no lead */
      continue;
    }
    if ((int32_t)(api->ms() - t_end) < 0) continue;
    /* the step's end: its line, the next gain, the receiver over */
    stop();
    v[0] = step, v[1] = (int32_t)pn.n, v[2] = (int32_t)pn.err, v[3] = rssi(api);
    v[4] = rx.dphi_f >> 16, v[5] = rx.state, v[6] = rx.acq_q, v[7] = rx.acq_tries;
    v[8] = rx.acq_rej, v[9] = (int32_t)lost;
    v[10] = (int32_t)ifrx_udiv(idle >> 8, (tot >> 16) | 1) * 100 >> 8;
    say_f(api, "l % b=% e=% r=% f=% s=% q=% w=% j=% L=% i=%", v);
    r = NG;
    if (++step >= NG) break;
    t_end += STEP_MS;
  fresh:
    gain(api, sv, step);
    ifrx_init(&rx, IFRX_ONLY, ks);
    ifrx_zero(&pn, sizeof(pn) / 4);
    ifrx_zero(&g, sizeof(g) / 4);
    lost = idle = tot = 0;
    start(rb);
    c0 = tim();
  }
  stop();
  return r;
}

int k6_main(const k6_api_t *api) {
  saved_t sv;
  uint32_t ring[RSZ / 4];
  int ret;
  if (api->abi != 1 || api->level < 2) return -3;
  /* the module came in on the sender's carrier: wait for it to drop */
  if (wait_level(api, 0, 30000) == -2) return -2;
  if (rd(GPIOB) & (3u << 2)) return -4;
  sv.apb2 = rd(RCC_APB2ENR);
  sv.ahb = rd(RCC_AHBENR);
  wr(RCC_AHBENR, sv.ahb | 1u); /* the DMA's registers read junk with its clock off */
  ret = -7;
  if ((sv.apb2 & TIM17EN) || (rd(DMA_CCR1) & 1)) goto out_ahb;
  sv.r33 = api->fd_read(0x33);
  sv.r34 = api->fd_read(0x34);
  sv.r7e = api->fd_read(0x7E);
  sv.r13 = api->fd_read(0x13);
  ret = -5;
  if ((sv.r33 & 0x0100) && api->fd_write(0x33, (uint16_t)(sv.r33 & ~0x0100)) != 0) goto out_ahb;
  api->fd_write(0x34, (uint16_t)((sv.r34 & 0xFFF0) | 13));
  wr(RCC_APB2ENR, sv.apb2 | TIM17EN);
  ret = run(api, &sv, (uint8_t *)ring);
  gain(api, &sv, 0);
  wr(RCC_APB2ENR, sv.apb2);
  api->fd_write(0x34, sv.r34);
  api->fd_write(0x33, sv.r33);
out_ahb:
  wr(RCC_AHBENR, sv.ahb);
  return ret;
}
