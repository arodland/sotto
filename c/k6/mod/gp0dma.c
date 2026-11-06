/* gp0dma: sample the FD6818's GP0 IF stream (code 13 on MCU PB1) by DMA instead of a busy
   loop, and FM-demodulate it on the MCU while it runs (docs/k6-fd6818-audio.md).

   The MCU has no SPI or counting timer on PB1 (STM32F030 pin map: TIM14_CH1, TIM3_CH4,
   TIM1_CH3N, ADC_IN9), so DMA channel 1 (TIM17_UP; unused: the pager's PWM audio is TIM1 and
   channel 5) reads GPIOB IDR a byte at a time into a circular 1 KB ring in scratch RAM, with
   TIM17 (unused by Aura) as its request. No interrupt: the CPU polls CNDTR.

   Run 7 (2026-11-06) found that TIM17 does not pace the channel on this chip: at ARR 3 to 63
   the DMA ran flat out, about 5.71 M transfers/s. So the module now lets it: TIM17 counts
   0..0xFFFF at 96 MHz with its update request on, and its counter is the clock that times each
   block (the real sample rate) and the CPU's waits on the DMA (idle). A probe first counts the
   transfers in 2 ms for a few TIM17 set-ups, to pin down what drives the channel:
     p0 timer off, p1 UDE only, p2 UDE + counting to 0xFFFF, p3 = p2 while the CPU clears SR,
     p4 UDE + ARR 95 (a 1 MHz update), p5 CC1DE + ARR 95 + CCR1 48.

   The CPU takes the ring L samples at a time, counts the ones on PB1 (4 samples a word, masked
   and summed), and demodulates the density around its middle: each rising zero crossing,
   interpolated, gives a period and a frequency, averaged over 4 crossings (about 0.24 ms:
   -0.9 dB at 1 kHz). Frequencies are worked in units of one output = 1/93750 s and scaled by
   the block's measured time. Per block of BLK outputs: the mean IF (crossings over their time
   span), the swing (peak deviation), the tone (the swing's crossings of the last block's mean
   IF, or 17 kHz, with 200 Hz hysteresis), the rms of each IF cycle's frequency, the ring
   overruns, the CPU's idle per cent, and the sample rate.

   Steps: the upload's carrier drops, the probe, one block for each L (64, 32, 128: outputs at
   about 89, 178 and 45 kHz) on the empty channel, up to 90 s for a carrier, then one
   continuous run for each L: 30 blocks (3 s) at L 64, 10 at 32 and 128 (printed after each
   run, as `say` takes the CPU). Lines:
     dma start floor=-103 ring=1024
     p 0 0 11420 11420 2 11420                                 (transfers in 2 ms, p0..p5)
     r 64 t=1050 lost=0                                        (L, ticks of 100 us, overruns)
     b 64 3 ks=5712 if=16920 tone=1000 dev=4290 idle=85        (block 3: kS/s, Hz, %)
   Writes REG_34[3:0] and REG_33 bit 8 (the PR #68 image) and the MCU's RCC APB2ENR TIM17EN,
   AHBENR DMAEN, TIM17 and DMA channel 1 registers, all put back (Jeff OK'd TIM17 + DMA
   channel 1, 2026-11-06). No TX. Returns the runs done, or -2 (a key), -3 (old image), -4
   (PB1 not an input), -5 (REG_33 refused), -6 (no carrier), -7 (TIM17 or DMA channel 1 in use),
   -8 (scratch under 516 bytes). The ring is 1 KB, or 512 bytes if scratch is short.
   SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 2
#include "../k6_mod.h"

#define GPIOB 0x48000400u
#define RCC_AHBENR 0x40021014u
#define RCC_APB2ENR 0x40021018u
#define TIM14_CNT 0x40002024u /* Aura's 100 us uptime counter: read only */
#define TIM17 0x40014800u
#define TIM17_CNT (TIM17 + 0x24)
#define DMA 0x40020000u
#define DMA_CCR1 (DMA + 0x08)
#define DMA_CNDTR1 (DMA + 0x0C)
#define DMA_CPAR1 (DMA + 0x10)
#define DMA_CMAR1 (DMA + 0x14)
#define TIM17EN (1u << 18)
#define SEL_REG 0x34
#define CODE 13
#define FS_Q8 (93750u * 256u)
#ifdef GP0_SWEEP
#ifndef TAG
#define TAG "sweep"
#endif
#ifndef BLK
#define BLK 4688u            /* outputs per block: about 52 ms at 89 kHz */
#endif
#ifndef MAXB
#define MAXB 1200u           /* blocks: about 70 s */
#endif
#else
#define TAG "dma"
#endif
#ifndef BLK
#define BLK 9375u            /* outputs per block: about 105 ms at 89 kHz */
#endif
#ifndef NBLK
#define NBLK 30
#endif
#define BLK_CYC_MAX 400000000u /* a block that takes over ~4 s: the DMA stopped */

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

static uint32_t isqrt(uint32_t v) {
  uint32_t r = 0, bit = 1u << 30;
  while (bit > v) bit >>= 2;
  while (bit) {
    if (v >= r + bit) v -= r + bit, r = (r >> 1) + bit;
    else r >>= 1;
    bit >>= 2;
  }
  return r;
}

static int rssi(const k6_api_t *api) { return KM_RSSI_DBM(api->fd_read(0x67)); }

static void say0(const k6_api_t *api, char *line, char *p) {
  *p = 0;
  api->say(line);
}

typedef struct {
  uint32_t ifm, tone, dev, idle, rms, ks, lost;
} blk_t;

typedef struct {
  /* the ring */
  uint32_t laps, lastw, produced, consumed, rsz;
  /* the demodulator */
  int prev, have, state, ref, se, eref;
  uint32_t n, tlast, f4[4], k4, sum4;
  uint32_t fsum, nf, fmin, fmax, flips, ncr, tb0, tb1, ne, se2;
} run_t;

static uint32_t poll(run_t *s) {
  uint32_t w = s->rsz - (rd(DMA_CNDTR1) & 0xFFFF);
  if (w < s->lastw) s->laps++;
  s->lastw = w;
  return s->produced = s->laps * s->rsz + w;
}

/* one output: x = 2 * ones - L, around 0 */
static void demod(run_t *s, int x) {
  if (s->prev < 0 && x >= 0) {
    uint32_t t = ((s->n - 1) << 8) + udiv((uint32_t)(-s->prev) << 8, (uint32_t)(x - s->prev));
    if (!s->ncr++) s->tb0 = t;
    s->tb1 = t;
    if (s->have) {
      uint32_t f = udiv(FS_Q8, t - s->tlast), fs;
      int e = ((int)f - s->eref) >> 3; /* one IF cycle's frequency, for the block's rms */
      s->se += e, s->se2 += (uint32_t)(e * e), s->ne++;
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

static void dma_on(uint8_t *ring, uint32_t rsz) {
  wr(DMA_CCR1, 0);
  wr(DMA + 0x04, 0xF);
  wr(DMA_CPAR1, GPIOB + 0x10);
  wr(DMA_CMAR1, (uint32_t)ring);
  wr(DMA_CNDTR1, rsz);
  wr(DMA_CCR1, (3u << 12) | (1u << 7) | (1u << 5) | 1u); /* PL very high, MINC, CIRC, EN; 8-bit */
}

/* TIM17: PSC 0, ARR arr, CCR1 ccr, DIER dier, counting if cen */
static void tim_on(uint32_t arr, uint32_t ccr, uint32_t dier, uint32_t cen) {
  wr(TIM17 + 0x00, 0);
  wr(TIM17 + 0x28, 0);       /* PSC */
  wr(TIM17 + 0x2C, arr);     /* ARR */
  wr(TIM17 + 0x30, 0);       /* RCR */
  wr(TIM17 + 0x34, ccr);     /* CCR1 (CCMR1 stays 0: frozen output compare) */
  wr(TIM17 + 0x14, 1);       /* EGR UG: load PSC/ARR */
  wr(TIM17 + 0x10, 0);       /* SR */
  wr(TIM17 + 0x0C, dier);
  wr(TIM17 + 0x00, cen);
}

#ifndef GP0_SWEEP
/* transfers in 20 ticks (2 ms) with TIM17 so; clr: keep clearing its SR meanwhile */
NOINLINE static uint32_t probe(uint8_t *ring, uint32_t rsz, uint32_t arr, uint32_t ccr,
                               uint32_t dier, uint32_t cen, int clr) {
  run_t s;
  uint32_t t0;
  s.laps = s.lastw = 0;
  s.rsz = rsz;
  dma_on(ring, rsz);
  tim_on(arr, ccr, dier, cen);
  t0 = rd(TIM14_CNT);
  while (((rd(TIM14_CNT) - t0) & 0xFFFF) < 20) {
    poll(&s);
    if (clr) wr(TIM17 + 0x10, 0);
  }
  stop();
  return poll(&s);
}
#endif

/* one continuous run of nb blocks, L samples an output; ref: the IF to start from (Hz) */
NOINLINE static uint32_t run(uint8_t *ring, uint32_t rsz, uint32_t L, int nb, blk_t *out,
                             int ref) {
  run_t s;
  uint32_t *z = (uint32_t *)&s, i, lost_total = 0;
  int b;
  for (i = 0; i < sizeof s / 4; i++) z[i] = 0;
  s.ref = (int)udiv((uint32_t)ref * L * 15, 914); /* in units of 1/93750 s at 5.71 MS/s */
  s.rsz = rsz;
  dma_on(ring, rsz);
  tim_on(0xFFFF, 0, 1u << 8, 1); /* UDE; the counter is the 96 MHz clock */
  for (b = 0; b < nb; b++) {
    uint32_t outs = 0, lost = 0, cyc = 0, idle = 0, c, c0 = rd(TIM17_CNT) & 0xFFFF, k;
    s.fsum = s.nf = s.fmax = s.flips = s.ncr = s.ne = s.se2 = 0;
    s.se = 0;
    s.eref = s.ref ? s.ref : 17000;
    s.fmin = 0xFFFFFFFFu;
    while (outs < BLK && cyc < BLK_CYC_MAX) {
      uint32_t avail = poll(&s) - s.consumed;
      c = rd(TIM17_CNT) & 0xFFFF;
      k = (c - c0) & 0xFFFF;
      c0 = c;
      cyc += k;
      if (avail > s.rsz - L) {
        lost++;
        s.consumed = s.produced & ~(L - 1);
        s.have = 0;
        continue;
      }
      if (avail < L) {
        idle += k;
        continue;
      }
      demod(&s, 2 * ones(ring, s.consumed & (s.rsz - 1), L) - (int)L);
      s.consumed += L;
      outs++;
    }
    /* scale from units of 1/93750 s to Hz: x BLK outputs / (cyc / 96 MHz) / 93750 */
    k = cyc >> 10;
    if (!k) k = 1;
    out[b].ifm = s.ncr > 1 ? udiv(FS_Q8 << 7, udiv((s.tb1 - s.tb0) << 7, s.ncr - 1)) : 0;
    out[b].ifm = udiv(out[b].ifm * BLK, k);
    s.ref = (int)out[b].ifm;
    out[b].dev = s.nf ? udiv(((s.fmax - s.fmin) >> 1) * BLK, k) : 0;
    out[b].tone = udiv(s.flips * 46875u, k);  /* flips / 2 / (cyc / 96 MHz) */
    out[b].idle = udiv(udiv(idle, 1000) * 100, udiv(cyc, 1000) ? udiv(cyc, 1000) : 1);
    out[b].ks = udiv(BLK * L * 96, udiv(cyc, 1000) ? udiv(cyc, 1000) : 1);
    out[b].lost = outs < BLK ? 0xFFFF : lost;
    out[b].rms = 0;
    if (s.ne > 1) {
      uint32_t m = (uint32_t)(s.se < 0 ? -s.se : s.se), v;
      m = udiv(m, s.ne);
      v = udiv(s.se2, s.ne);
      out[b].rms = v > m * m ? udiv((isqrt(v - m * m) << 3) * BLK, k) : 0;
    }
    /* the next block's reference, in the demodulator's units */
    s.ref = (int)udiv(s.ref * k, BLK);
    s.state = 0;
    lost_total += out[b].lost;
    if (outs < BLK) break;
  }
  stop();
  return lost_total;
}

typedef struct {
  uint32_t ahb, apb2;
  uint16_t r33, r34;
  int floor;
  uint32_t rsz;  /* the ring: 1024 bytes, or 512 if scratch is short */
} saved_t;

static char *hex(char *p, uint32_t v, int digits) {
  while (digits--) {
    uint32_t d = (v >> (4 * digits)) & 15;
    *p++ = (char)(d < 10 ? '0' + d : 'a' + d - 10);
  }
  return p;
}

/* the registers this module touches: RCC APB2ENR AHBENR, TIM17 CR1 DIER, DMA CCR1 CNDTR1
   CPAR1 CMAR1 */
NOINLINE static void regs(const k6_api_t *api) {
  static const uint32_t at[8] = {RCC_APB2ENR, RCC_AHBENR, TIM17,     TIM17 + 0x0C,
                                 DMA_CCR1,    DMA_CNDTR1, DMA_CPAR1, DMA_CMAR1};
  char line[64], *p = line;
  int i;
  for (i = 0; i < 8; i++) {
    *p++ = i ? ' ' : 'R';
    *p = ' ', p += !i;
    p = hex(p, rd(at[i]), i == 2 || i == 3 || i == 4 || i == 5 ? 4 : 8);
  }
  say0(api, line, p);
}

/* the upload's carrier gone, PB1 an input, TIM17 and channel 1 free, GP0 on code 13, clocks on */
NOINLINE static int setup(const k6_api_t *api, saved_t *sv, uint8_t **ring) {
  uint32_t room, t0;
  if (api->level < 2) return -3;
  t0 = api->ms();
  while (rssi(api) > -80 && api->ms() - t0 < 30000) {
    if (api->key_down()) return -2;
    api->delay_us(10000);
  }
  api->delay_us(200000);
  sv->floor = rssi(api);
  *ring = (uint8_t *)(((uint32_t)api->scratch(&room) + 3) & ~3u);
  sv->rsz = room >= 1028 ? 1024 : 512;
  if (room < sv->rsz + 4) return -8;
  if (rd(GPIOB) & (3u << 2)) {
    api->say(TAG " PB1 is not an input: no writes");
    return -4;
  }
  sv->apb2 = rd(RCC_APB2ENR);
  sv->ahb = rd(RCC_AHBENR);
  /* the DMA's registers read junk with its clock off (run 8: 0x37 everywhere): clock it first */
  wr(RCC_AHBENR, sv->ahb | 1u);
  regs(api);
  if ((sv->apb2 & TIM17EN) || (rd(DMA_CCR1) & 1)) {
    wr(RCC_AHBENR, sv->ahb);
    api->say(TAG " TIM17 or DMA channel 1 busy: no writes");
    return -7;
  }
  sv->r33 = api->fd_read(0x33);
  sv->r34 = api->fd_read(SEL_REG);
  if ((sv->r33 & 0x0100) && api->fd_write(0x33, (uint16_t)(sv->r33 & ~0x0100)) != 0) {
    api->say(TAG " REG_33 refused: needs the PR #68 image");
    return -5;
  }
  api->fd_write(SEL_REG, (uint16_t)((sv->r34 & 0xFFF0) | CODE));
  wr(RCC_AHBENR, sv->ahb | 1u);
  wr(RCC_APB2ENR, sv->apb2 | TIM17EN);
  return 0;
}

NOINLINE static void teardown(const k6_api_t *api, const saved_t *sv) {
  stop();
  wr(TIM17 + 0x2C, 0xFFFF);
  wr(TIM17 + 0x28, 0);
  wr(DMA_CNDTR1, 0);
  wr(DMA_CPAR1, 0);
  wr(DMA_CMAR1, 0);
  regs(api);
  wr(RCC_APB2ENR, sv->apb2);
  wr(RCC_AHBENR, sv->ahb);
  api->fd_write(SEL_REG, sv->r34);
  api->fd_write(0x33, sv->r33);
  api->say(TAG " done");
}

/* up to 90 s for a carrier 15 dB over the floor: 0, or -2 (a key), -6 */
static int carrier(const k6_api_t *api, int floor) {
  uint32_t t0 = api->ms();
  api->say(TAG " waiting for a carrier");
  while (rssi(api) < floor + 15) {
    if (api->key_down()) return -2;
    if (api->ms() - t0 > 90000) {
      api->say(TAG " no carrier");
      return -6;
    }
    api->delay_us(5000);
  }
  return 0;
}

#ifndef GP0_SWEEP
static char *blkline(char *p, uint32_t L, int b, const blk_t *x) {
  p = kv(p, "b ", L);
  p = kv(p, " ", (uint32_t)b);
  p = kv(p, " ks=", x->ks);
  p = kv(p, " if=", x->ifm);
  p = kv(p, " tone=", x->tone);
  p = kv(p, " dev=", x->dev);
  p = kv(p, " idle=", x->idle);
  if (x->lost) p = kv(p, " lost=", x->lost);
  return p;
}

int k6_main(const k6_api_t *api) {
  static const uint8_t Ls[3] = {64, 32, 128}; /* 64 first: the long run */
  blk_t blk[NBLK];
  saved_t sv;
  uint8_t *ring;
  uint32_t n[6], lost;
  uint16_t c0, dt;
  int ret, q, b, pass, i;
  char line[64], *p;
  ret = setup(api, &sv, &ring);
  if (ret) return ret;
  p = put(line, "dma start floor=");
  p = sdec(p, sv.floor);
  p = kv(p, " ring=", sv.rsz);
  say0(api, line, p);
  p = put(line, "p");
  for (i = 0; i < 6; i++) {
    /* off, UDE, UDE counting, the same clearing SR, UDE at 1 MHz, CC1DE at 1 MHz */
    static const uint8_t dier[6] = {0, 1, 1, 1, 1, 2}, cen[6] = {0, 0, 1, 1, 1, 1};
    n[i] = probe(ring, sv.rsz, i < 4 ? 0xFFFF : 95, i == 5 ? 48 : 0, (uint32_t)dier[i] << 8,
                 cen[i], i == 3);
    p = kv(p, " ", n[i]);
  }
  say0(api, line, p);
  ret = 0;
  for (pass = 0; pass < 2; pass++) {
    if (pass) {
      ret = carrier(api, sv.floor);
      if (ret) goto out;
      api->delay_us(600000);
    }
    for (q = 0; q < 3; q++) {
      uint32_t L = Ls[q];
      int nb = !pass ? 1 : L == 64 ? NBLK : NBLK / 3;
      c0 = (uint16_t)api->ticks();
      lost = run(ring, sv.rsz, L, nb, blk, 17000);
      dt = (uint16_t)((uint16_t)api->ticks() - c0);
      p = kv(line, "r ", L);
      p = kv(p, " t=", dt);
      p = kv(p, " lost=", lost);
      say0(api, line, p);
      for (b = 0; b < nb; b++) {
        p = blkline(line, L, b, &blk[b]);
        say0(api, line, p);
      }
      if (pass) ret++;
      if (api->key_down()) {
        ret = -2;
        goto out;
      }
    }
  }
out:
  teardown(api, &sv);
  return ret;
}
#elif !defined(GP0_NO_MAIN)
/* the sweep: blocks of BLK outputs at L 64 while the carrier lasts, each with REG_6F */
int k6_main(const k6_api_t *api) {
  blk_t blk;
  saved_t sv;
  uint8_t *ring;
  uint32_t k = 0;
  int ret;
  char line[64], *p;
  blk.ifm = 17000;
  ret = setup(api, &sv, &ring);
  if (ret) return ret;
  p = put(line, "sweep start floor=");
  p = sdec(p, sv.floor);
  p = kv(p, " blk=", BLK);
  say0(api, line, p);
  ret = carrier(api, sv.floor);
  if (ret) goto out;
  while (k < MAXB && rssi(api) >= sv.floor + 15) {
    uint32_t af, lost;
    lost = run(ring, sv.rsz, 64, 1, &blk, (int)blk.ifm);
    af = api->fd_read(0x6F) & 0x3F;
    p = kv(line, "s ", k++);
    p = kv(p, " tone=", blk.tone);
    p = kv(p, " rms=", blk.rms);
    p = kv(p, " dev=", blk.dev);
    p = kv(p, " af=", af);
    p = kv(p, " if=", blk.ifm);
    if (lost) p = kv(p, " lost=", lost);
    say0(api, line, p);
    if (api->key_down()) {
      ret = -2;
      goto out;
    }
  }
  ret = (int)k;
out:
  teardown(api, &sv);
  return ret;
}
#endif
