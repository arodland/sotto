/* gp0dma: sample the FD6818's GP0 IF stream (code 13 on MCU PB1) by DMA instead of a busy
   loop, and FM-demodulate it on the MCU while it runs (docs/k6-fd6818-audio.md).

   The MCU has no SPI or counting timer on PB1 (STM32F030 pin map: TIM14_CH1, TIM3_CH4,
   TIM1_CH3N, ADC_IN9), so TIM17's update event (TIM17EN, unused by Aura and the pager) paces
   DMA channel 1 (TIM17_UP, unused: the pager's PWM audio is TIM1 + channel 5) reading GPIOB IDR
   a byte at a time into a circular ring in scratch RAM. No interrupt: the CPU polls CNDTR.

   The CPU takes the ring L samples at a time (L = rate / 93.75 kHz: one output per 10.7 us),
   counts the ones on PB1 (4 samples a word, masked and summed), and demodulates the density
   around its middle: each rising zero crossing, interpolated, gives a period and a frequency
   (93.75 kHz * 256 / period, Q8), averaged over 4 crossings (about 0.24 ms: -0.9 dB at 1 kHz).
   Per block of BLK outputs it keeps the mean IF (crossings over their time span), the swing
   (peak deviation), the tone (the swing's crossings of the last block's mean frequency, 200 Hz
   hysteresis), the ring overruns, and the time the CPU spent waiting on the DMA
   (idle, against spins timed with the DMA off).

   Rates 24, 12, 6, 3 and 1.5 MS/s (TIM17 ARR 3, 7, 15, 31, 63 at 96 MHz). Steps: the upload's
   carrier drops, one block per rate on the empty channel, wait up to 90 s for a carrier, then
   NBLK blocks per rate in one continuous run each (printed after the run, as `say` takes the
   CPU). Lines:
     dma start floor=-103 ring=1024 s0=N                      (idle spins per tick, DMA off)
     r 6000 t=1000 got=1000 lost=0                            (kS/s, ticks, permille served)
     b 6000 3 if=16920 tone=1000 dev=4290 lost=0 idle=85      (block 3 of that run)
   Writes REG_34[3:0] and REG_33 bit 8 (the PR #68 image) and the MCU's RCC APB2ENR TIM17EN,
   AHBENR DMAEN, TIM17 and DMA channel 1 registers, all put back (Jeff OK'd TIM17 + DMA
   channel 1, 2026-11-06). No TX. Returns the rates run, or -2 (a key), -3 (old image), -4 (PB1
   not an input), -5 (REG_33 refused), -6 (no carrier), -7 (TIM17 or DMA channel 1 in use).
   SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 2
#include "../k6_mod.h"

#define GPIOB 0x48000400u
#define RCC_AHBENR 0x40021014u
#define RCC_APB2ENR 0x40021018u
#define TIM17 0x40014800u
#define DMA 0x40020000u
#define DMA_CCR1 (DMA + 0x08)
#define DMA_CNDTR1 (DMA + 0x0C)
#define DMA_CPAR1 (DMA + 0x10)
#define DMA_CMAR1 (DMA + 0x14)
#define TIM17EN (1u << 18)
#define SEL_REG 0x34
#define CODE 13
#define RING 1024u
#define FS_Q8 (93750u * 256u)
#ifndef BLK
#define BLK 9375u            /* outputs per block: 100 ms */
#endif
#ifndef NBLK
#define NBLK 10
#endif
#ifndef CAL_SPINS
#define CAL_SPINS 200000u
#endif
#define NRATE 5

#define NOINLINE __attribute__((noinline))

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

static uint32_t rd(uint32_t a) { return *(volatile uint32_t *)a; }
static void wr(uint32_t a, uint32_t v) { *(volatile uint32_t *)a = v; }

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

static int rssi(const k6_api_t *api) { return KM_RSSI_DBM(api->fd_read(0x67)); }

static void say0(const k6_api_t *api, char *line, char *p) {
  *p = 0;
  api->say(line);
}

typedef struct {
  uint32_t ifm, tone, dev, lost, idle;
} blk_t;

typedef struct {
  /* the ring */
  uint32_t laps, lastw, produced, consumed, spins;
  /* the demodulator */
  int prev, have, state, ref;
  uint32_t n, tlast, f4[4], k4, sum4;
  uint32_t fsum, nf, fmin, fmax, flips, ncr, tb0, tb1;
} run_t;

static uint32_t poll(run_t *s) {
  uint32_t w = RING - (rd(DMA_CNDTR1) & 0xFFFF);
  if (w < s->lastw) s->laps++;
  s->lastw = w;
  return s->produced = s->laps * RING + w;
}

/* one output: x = 2 * ones - L, around 0 */
static void demod(run_t *s, int x) {
  if (s->prev < 0 && x >= 0) {
    uint32_t t = ((s->n - 1) << 8) + udiv((uint32_t)(-s->prev) << 8, (uint32_t)(x - s->prev));
    if (!s->ncr++) s->tb0 = t;
    s->tb1 = t;
    if (s->have) {
      uint32_t f = udiv(FS_Q8, t - s->tlast), fs;
      s->sum4 += f - s->f4[s->k4 & 3];
      s->f4[s->k4++ & 3] = f;
      if (s->k4 >= 4) {
        fs = s->sum4 >> 2;
        s->fsum += fs, s->nf++;
        if (fs < s->fmin) s->fmin = fs;
        if (fs > s->fmax) s->fmax = fs;
        if (s->ref) {
          if (!s->state && (int)fs > s->ref + 200) s->state = 1, s->flips++;
          if (s->state && (int)fs < s->ref - 200) s->state = 0, s->flips++;
        }
      }
    }
    s->have = 1;
    s->tlast = t;
  }
  s->prev = x;
  s->n++;
}

/* L samples at ring offset r: the ones on PB1 (bit 1 of each byte) */
static int ones(const uint8_t *ring, uint32_t r, uint32_t L) {
  const uint32_t *p = (const uint32_t *)(ring + r);
  uint32_t acc = 0, i;
  for (i = 0; i < L / 4; i++) acc += p[i] & 0x02020202u;
  acc >>= 1;
  acc = (acc & 0x00FF00FFu) + ((acc >> 8) & 0x00FF00FFu);
  return (int)((acc & 0xFFFF) + (acc >> 16));
}

static void stop(void) {
  wr(TIM17 + 0x00, 0);       /* CR1 */
  wr(TIM17 + 0x0C, 0);       /* DIER */
  wr(DMA_CCR1, 0);
  wr(DMA + 0x04, 0xF);       /* IFCR: channel 1 flags */
}

/* one continuous run of nb blocks at TIM17 ARR arr, L samples an output */
NOINLINE static uint32_t run(uint8_t *ring, uint32_t arr, uint32_t L, int nb, blk_t *out,
                             uint32_t per1, uint32_t *lost_total) {
  run_t s;
  uint32_t *z = (uint32_t *)&s, i;
  int b;
  for (i = 0; i < sizeof s / 4; i++) z[i] = 0;
  wr(DMA_CCR1, 0);
  wr(DMA + 0x04, 0xF);
  wr(DMA_CPAR1, GPIOB + 0x10);
  wr(DMA_CMAR1, (uint32_t)ring);
  wr(DMA_CNDTR1, RING);
  wr(DMA_CCR1, (3u << 12) | (1u << 7) | (1u << 5) | 1u); /* PL very high, MINC, CIRC, EN; 8-bit */
  wr(TIM17 + 0x00, 0);
  wr(TIM17 + 0x28, 0);       /* PSC */
  wr(TIM17 + 0x2C, arr);     /* ARR */
  wr(TIM17 + 0x30, 0);       /* RCR */
  wr(TIM17 + 0x14, 1);       /* EGR UG: load PSC/ARR */
  wr(TIM17 + 0x10, 0);       /* SR */
  wr(TIM17 + 0x0C, 1u << 8); /* DIER UDE */
  wr(TIM17 + 0x00, 1);       /* CR1 CEN */
  for (b = 0; b < nb; b++) {
    uint32_t outs = 0, lost = 0;
    s.fsum = s.nf = s.fmax = s.flips = s.ncr = 0;
    s.fmin = 0xFFFFFFFFu;
    s.spins = 0;
    while (outs < BLK) {
      uint32_t avail = poll(&s) - s.consumed;
      if (avail > RING - L) {
        lost++;
        s.consumed = s.produced & ~(L - 1);
        s.have = 0;
        continue;
      }
      if (avail < L) {
        s.spins++;
        continue;
      }
      demod(&s, 2 * ones(ring, s.consumed & (RING - 1), L) - (int)L);
      s.consumed += L;
      outs++;
    }
    /* the mean IF: crossings over their time span (the mean of fsum leans high under FM) */
    out[b].ifm = s.ncr > 1 ? udiv(FS_Q8 << 7, udiv((s.tb1 - s.tb0) << 7, s.ncr - 1)) : 0;
    s.ref = (int)udiv(s.fsum, s.nf);
    out[b].dev = s.nf ? (s.fmax - s.fmin) >> 1 : 0;
    out[b].tone = udiv(s.flips * 93750u, 2 * BLK);
    out[b].lost = lost;
    out[b].idle = udiv(s.spins, per1);
    *lost_total += lost;
    s.state = 0;
  }
  stop();
  poll(&s);
  return s.produced;
}

int k6_main(const k6_api_t *api) {
  static const uint8_t arrs[NRATE] = {3, 7, 15, 31, 63};
  blk_t blk[NBLK];
  uint8_t *ring;
  uint32_t room, t0, ahb, apb2, s0, per1, k;
  uint16_t r33, r34, c0;
  int floor, ret, q, b, pass;
  char line[64], *p;
  if (api->level < 2) return -3;
  t0 = api->ms();
  while (rssi(api) > -80 && api->ms() - t0 < 30000) {
    if (api->key_down()) return -2;
    api->delay_us(10000);
  }
  api->delay_us(200000);
  floor = rssi(api);
  ring = (uint8_t *)(((uint32_t)api->scratch(&room) + 3) & ~3u);
  if (room < RING + 4) return -8;
  if (rd(GPIOB) & (3u << 2)) {
    api->say("dma PB1 is not an input: no writes");
    return -4;
  }
  apb2 = rd(RCC_APB2ENR);
  ahb = rd(RCC_AHBENR);
  if ((apb2 & TIM17EN) || (rd(DMA_CCR1) & 1)) {
    api->say("dma TIM17 or DMA channel 1 busy: no writes");
    return -7;
  }
  r33 = api->fd_read(0x33);
  r34 = api->fd_read(SEL_REG);
  if ((r33 & 0x0100) && api->fd_write(0x33, (uint16_t)(r33 & ~0x0100)) != 0) {
    api->say("dma REG_33 refused: needs the PR #68 image");
    return -5;
  }
  api->fd_write(SEL_REG, (uint16_t)((r34 & 0xFFF0) | CODE));
  wr(RCC_AHBENR, ahb | 1u);
  wr(RCC_APB2ENR, apb2 | TIM17EN);
  /* idle spins with the DMA off: the same poll the run's wait loop makes */
  {
    run_t s;
    s.laps = s.lastw = 0;
    c0 = (uint16_t)api->ticks();
    for (k = 0; k < CAL_SPINS; k++) poll(&s);
    t0 = (uint16_t)((uint16_t)api->ticks() - c0);
    if (!t0) t0 = 1;
    s0 = udiv(CAL_SPINS, t0);                     /* spins a tick */
    per1 = udiv(s0 * udiv(BLK * 10000u, 93750u), 100); /* spins a block, per cent */
    if (!per1) per1 = 1;
  }
  p = put(line, "dma start floor=");
  p = sdec(p, floor);
  p = kv(p, " ring=", RING);
  p = kv(p, " s0=", s0);
  say0(api, line, p);
  ret = NRATE;
  for (pass = 0; pass < 2; pass++) {
    if (pass) {
      api->say("dma waiting for a carrier");
      t0 = api->ms();
      while (rssi(api) < floor + 15) {
        if (api->key_down() || api->ms() - t0 > 90000) {
          ret = api->key_down() ? -2 : -6;
          api->say("dma no carrier");
          goto out;
        }
        api->delay_us(5000);
      }
      api->delay_us(600000);
    }
    for (q = 0; q < NRATE; q++) {
      uint32_t arr = arrs[q], L = udiv(1024, arr + 1), got, want, lost = 0; /* 93.75 kHz out */
      int nb = pass ? NBLK : 1;
      c0 = (uint16_t)api->ticks();
      got = run(ring, arr, L, nb, blk, per1, &lost);
      t0 = (uint16_t)((uint16_t)api->ticks() - c0);
      want = udiv(9600u * t0, arr + 1); /* 96 MHz / (arr + 1) samples a second, 10^4 ticks */
      want = udiv(want, 1000);
      p = kv(line, "r ", udiv(96000, arr + 1));
      p = kv(p, " t=", t0);
      p = kv(p, " got=", udiv(got, want ? want : 1));
      p = kv(p, " lost=", lost);
      say0(api, line, p);
      for (b = 0; b < nb; b++) {
        p = kv(line, "b ", udiv(96000, arr + 1));
        p = kv(p, " ", (uint32_t)b);
        p = kv(p, " if=", blk[b].ifm);
        p = kv(p, " tone=", blk[b].tone);
        p = kv(p, " dev=", blk[b].dev);
        p = kv(p, " lost=", blk[b].lost);
        p = kv(p, " idle=", blk[b].idle);
        say0(api, line, p);
      }
      if (api->key_down()) {
        ret = -2;
        goto out;
      }
    }
  }
out:
  stop();
  wr(TIM17 + 0x2C, 0xFFFF);
  wr(TIM17 + 0x28, 0);
  wr(DMA_CNDTR1, 0);
  wr(DMA_CPAR1, 0);
  wr(DMA_CMAR1, 0);
  wr(RCC_APB2ENR, apb2);
  wr(RCC_AHBENR, ahb);
  api->fd_write(SEL_REG, r34);
  api->fd_write(0x33, r33);
  api->say("dma done");
  return ret;
}
