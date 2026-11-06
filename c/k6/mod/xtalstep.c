/* xtalstep: does writing the FD6818's crystal setting (REG_3B) move its synthesizer at once,
   and how fast? Receive only. The answer decides how the K6 can send FSK it writes itself
   (docs/k6-own-fsk.md): the guide's DMR mode modulates "by adjusting the external TCXO", and
   REG_3B/3C is the chip's idea of the crystal, xtal = ((REG_3C >> 8) << 16 + REG_3B) * 5 Hz
   (26 MHz = 0x4F5880). If the PLL takes a new value straight away, one step moves the LO by
   f * 5 Hz / 26 MHz, about 85 Hz at 440.850 MHz, and the MCU can write 2FSK/4FSK levels.

   With the IC-7100 sending a bare carrier on the pager's frequency, it measures the received
   IF from GP0 code 13 on PB1 as gp0dma does (DMA channel 1 flat out into a 1 KB ring, 64
   samples an output, about 89 k outputs/s, rising zero crossings), with AFC off (REG_73 bit 4):
     A  REG_3B = base + k for k = 0 6 -6 0 24 -24 0 48 -48 0, the mean IF over 8192 outputs
        (about 92 ms, after 1024 to settle) for each. Live: about 85 Hz a step.
     B  REG_38 (the frequency word, 10 Hz units) + 100, - 100, 0 without re-RXON (the guide
        says a new frequency needs RXON/TXON): live or latched.
     L  if A saw no move: REG_3B + 24 and a retune to the same frequency (RXON), then back:
        is the value taken at RXON?
     C  if A moved: REG_3B squared between base + 24 and base - 24 every 192 outputs (about
        2.15 ms), 1000 writes; each IF cycle's frequency binned by its time after the write
        began (4 outputs, about 45 us, a bin; 16 bins, 0.7 ms), up and down steps apart: the
        step response, which says how many symbols a second the synthesizer can follow.
   Frequencies are true Hz, scaled by the sample rate measured on TIM17's 96 MHz count (ks=,
   kS/s). Lines (K6 MOD say ...):
     xs regs 3b=5880 3c=4f00 38=.... 39=.... 73=.... 43=3028
     xs a 24 if=15890 n=1700 ks=5712          (k, IF, IF cycles counted, sample rate)
     xs b 100 if=...  /  xs l 24 if=...
     xs c up 0: f f f f f f f f               (bins 0-7 and 8-15, then dn)
     xs c wr=6 n=1000 lost=0                  (outputs a write took, writes, overruns)
   Restores REG_3B, REG_38, REG_73, REG_33, REG_34, TIM17 and DMA channel 1 (Jeff OK'd TIM17
   and DMA channel 1, 2026-11-06). No TX. Returns the IF move for k = +24 in Hz (negative if
   the IF fell), or -2 (a key), -3 (old image), -4 (PB1 not an input), -5 (REG_33
   refused), -6 (no carrier), -7 (TIM17 or DMA busy), -8 (scratch too small).
   SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 2
#define K6_MODULE_OPT z
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
#define FS_Q8 (93750u * 256u)
#define L 64u
#define RSZ 1024u
#ifndef HALF
#define HALF 192u    /* outputs between writes in C */
#endif
#ifndef WRITES
#define WRITES 1000u
#endif
#ifndef NA
#define NA 8192u     /* outputs a static mean is taken over, after SKIP */
#endif
#ifndef SKIP
#define SKIP 1024u
#endif
#define NBIN 16
#define KC 24

#define NOINLINE __attribute__((noinline))

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

static char *put(char *p, const char *s) {
  while (*s) *p++ = *s++;
  return p;
}

static char *dec(char *p, uint32_t v) {
  char b[10];
  int i = 0;
  do b[i++] = (char)('0' + (v - 10 * udiv(v, 10))), v = udiv(v, 10);
  while (v);
  while (i) *p++ = b[--i];
  return p;
}

static char *sdec(char *p, int v) {
  if (v < 0) *p++ = '-', v = -v;
  return dec(p, (uint32_t)v);
}

static char *hex(char *p, uint32_t v, int n) {
  while (n--) {
    uint32_t d = (v >> (4 * n)) & 15;
    *p++ = (char)(d < 10 ? '0' + d : 'a' + d - 10);
  }
  return p;
}

static int rssi(const k6_api_t *api) { return KM_RSSI_DBM(api->fd_read(0x67)); }

typedef struct {
  uint8_t *ring;
  uint32_t laps, lastw, produced, consumed, c0, ks;
  uint32_t sum[2][NBIN];
  uint16_t cnt[2][NBIN];
} g_t;

static uint32_t poll(g_t *g) {
  uint32_t w = RSZ - (rd(DMA_CNDTR1) & 0xFFFF);
  if (w < g->lastw) g->laps++;
  g->lastw = w;
  return g->produced = g->laps * RSZ + w;
}

/* start counting again from the DMA's position now (after a say or a delay lost track) */
static void resync(g_t *g) {
  g->laps = 0;
  g->lastw = RSZ - (rd(DMA_CNDTR1) & 0xFFFF);
  g->produced = g->lastw;
  g->consumed = g->c0 = g->lastw & ~(L - 1);
}

static int ones(const uint8_t *ring, uint32_t r) {
  const uint32_t *p = (const uint32_t *)(ring + r);
  uint32_t acc = 0, i;
  for (i = 0; i < L / 4; i++) acc += p[i] & 0x02020202u;
  acc >>= 1;
  acc = (acc & 0x00FF00FFu) + ((acc >> 8) & 0x00FF00FFu);
  return (int)((acc & 0xFFFF) + (acc >> 16));
}

typedef struct {
  uint32_t ifm, ncr, ks, lost, writes, wr_outs;
} res_t;

/* nout outputs; the mean IF over those after skip; sq: square REG_3B around base and bin */
NOINLINE static void go(const k6_api_t *api, g_t *g, uint32_t skip, uint32_t nout, int sq,
                        uint16_t base, res_t *r) {
  uint32_t n = 0, tlast = 0, tb0 = 0, tb1 = 0, next = 2 * HALF, tw = 0, tw_prev = 0, wsum = 0;
  uint32_t p0, cyc = 0, c, cp = rd(TIM17 + 0x24) & 0xFFFF;
  int prev = 0, have = 0, dir = 0, dir_prev = 0, i;
  r->ncr = r->lost = r->writes = 0;
  if (sq)
    for (i = 0; i < NBIN; i++) g->sum[0][i] = g->sum[1][i] = 0, g->cnt[0][i] = g->cnt[1][i] = 0;
  resync(g);
  p0 = g->produced;
  while (n < nout) {
    uint32_t prod = poll(g), avail = prod - g->consumed;
    c = rd(TIM17 + 0x24) & 0xFFFF; /* the 96 MHz clock: cycles, to scale to true Hz */
    cyc += (c - cp) & 0xFFFF;
    cp = c;
    if (sq && (prod - g->c0) / L >= next && r->writes < WRITES) {
      uint32_t ta = (prod - g->c0) / L;
      dir_prev = dir, tw_prev = tw;
      dir = (int)(r->writes & 1); /* 0 up (base + KC), 1 down */
      api->fd_write(0x3B, (uint16_t)(dir ? base - KC : base + KC));
      wsum += (poll(g) - g->c0) / L - ta;
      tw = ta;
      next = ta + HALF;
      r->writes++;
      continue;
    }
    if (avail > RSZ - L) {
      r->lost++;
      g->consumed = g->produced & ~(L - 1);
      n = (g->consumed - g->c0) / L;
      have = 0;
      continue;
    }
    if (avail < L) continue;
    {
      int x = 2 * ones(g->ring, g->consumed & (RSZ - 1)) - (int)L;
      if (prev < 0 && x >= 0) {
        uint32_t t = ((n - 1) << 8) + udiv((uint32_t)(-prev) << 8, (uint32_t)(x - prev));
        if (n >= skip) {
          if (!r->ncr++) tb0 = t;
          tb1 = t;
        }
        if (sq && have && t > tlast && r->writes > 2) {
          uint32_t f = udiv(FS_Q8, t - tlast), mid = (t + tlast) >> 9, ref = 0;
          int d = -1;
          if (mid >= tw) ref = tw, d = dir;
          else if (mid >= tw_prev) ref = tw_prev, d = dir_prev;
          if (d >= 0) {
            uint32_t bin = (mid - ref) >> 2;
            if (bin < NBIN && f < 60000) g->sum[d][bin] += f, g->cnt[d][bin]++;
          }
        }
        have = 1;
        tlast = t;
      }
      prev = x;
      n++;
      g->consumed += L;
    }
  }
  /* mean IF: crossings over their span (in Q4 to keep the precision), in Hz for 93750
     outputs/s, then scaled by the outputs' true rate: x (outputs / (cyc / 96 MHz)) / 93750 */
  r->ifm = r->ncr > 1 ? udiv(FS_Q8 << 4, udiv((tb1 - tb0) << 4, r->ncr - 1)) : 0;
  p0 = g->produced - p0;
  if (!sq) r->ifm = udiv(r->ifm * (p0 / L), (cyc >> 10) ? cyc >> 10 : 1);
  r->ks = g->ks = udiv(p0 * 96, udiv(cyc, 1000) ? udiv(cyc, 1000) : 1); /* kS/s */
  r->wr_outs = r->writes ? udiv(wsum, r->writes) : 0;
  if (sq) api->fd_write(0x3B, base);
}

NOINLINE static void line(const k6_api_t *api, const char *what, int k, const res_t *r) {
  char s[64], *p = put(s, what);
  p = sdec(p, k);
  p = put(p, " if=");
  p = dec(p, r->ifm);
  p = put(p, " n=");
  p = dec(p, r->ncr);
  p = put(p, " ks=");
  p = dec(p, r->ks);
  if (r->lost) p = put(p, " lost="), p = dec(p, r->lost);
  *p = 0;
  api->say(s);
}

NOINLINE static void stat(const k6_api_t *api, g_t *g, uint8_t reg, uint16_t val,
                          const char *what, int k, res_t *r) {
  api->fd_write(reg, val);
  go(api, g, SKIP, SKIP + NA, 0, 0, r);
  line(api, what, k, r);
}

NOINLINE static void bins(const k6_api_t *api, const g_t *g) {
  char s[64], *p;
  int d, j, i;
  for (d = 0; d < 2; d++)
    for (j = 0; j < NBIN; j += 8) {
      p = put(s, d ? "c dn " : "c up ");
      p = dec(p, (uint32_t)j);
      *p++ = ':';
      for (i = j; i < j + 8 && i < NBIN; i++)
        *p++ = ' ', p = dec(p, g->cnt[d][i] ? udiv(udiv(g->sum[d][i], g->cnt[d][i]) * g->ks, 6000) : 0);
      *p = 0;
      api->say(s);
    }
}

int k6_main(const k6_api_t *api) {
  static const int8_t ks[10] = {0, 6, -6, 0, 24, -24, 0, 48, -48, 0};
  static const uint8_t rr[6] = {0x3B, 0x3C, 0x38, 0x39, 0x73, 0x43};
  uint16_t v[6], r33, r34;
  uint32_t room, apb2, ahb, t0;
  uint8_t *sc;
  g_t *g;
  res_t r;
  int ret = 0, i, if0 = 0, ifp = 0, ifm = 0, floor;
  char s[64], *p;
  if (api->level < 2) return -3;
  /* the upload's carrier gone */
  t0 = api->ms();
  while (rssi(api) > -80 && api->ms() - t0 < 30000) {
    if (api->key_down()) return -2;
    api->delay_us(10000);
  }
  api->delay_us(200000);
  floor = rssi(api);
  sc = (uint8_t *)(((uint32_t)api->scratch(&room) + 3) & ~3u);
  if (room < RSZ + sizeof(g_t) + 8) return -8;
  g = (g_t *)(sc + RSZ);
  g->ring = sc;
  p = put(s, "xs regs");
  for (i = 0; i < 6; i++) {
    v[i] = api->fd_read(rr[i]);
    *p++ = ' ', p = hex(p, rr[i], 2);
    *p++ = '=', p = hex(p, v[i], 4);
  }
  *p = 0;
  api->say(s);
  if (rd(GPIOB) & (3u << 2)) return -4;
  apb2 = rd(RCC_APB2ENR);
  ahb = rd(RCC_AHBENR);
  wr(RCC_AHBENR, ahb | 1u);
  if ((apb2 & TIM17EN) || (rd(DMA_CCR1) & 1)) {
    wr(RCC_AHBENR, ahb);
    return -7;
  }
  r33 = api->fd_read(0x33);
  r34 = api->fd_read(0x34);
  if ((r33 & 0x0100) && api->fd_write(0x33, (uint16_t)(r33 & ~0x0100)) != 0) {
    wr(RCC_AHBENR, ahb);
    return -5;
  }
  api->fd_write(0x34, (uint16_t)((r34 & 0xFFF0) | 13));
  wr(RCC_APB2ENR, apb2 | TIM17EN);
  /* DMA channel 1: GPIOB IDR bytes into the ring, flat out; TIM17 counting with UDE */
  wr(DMA_CCR1, 0);
  wr(DMA + 0x04, 0xF);
  wr(DMA_CPAR1, GPIOB + 0x10);
  wr(DMA_CMAR1, (uint32_t)sc);
  wr(DMA_CNDTR1, RSZ);
  wr(DMA_CCR1, (3u << 12) | (1u << 7) | (1u << 5) | 1u);
  wr(TIM17 + 0x00, 0);
  wr(TIM17 + 0x28, 0);
  wr(TIM17 + 0x2C, 0xFFFF);
  wr(TIM17 + 0x14, 1);
  wr(TIM17 + 0x10, 0);
  wr(TIM17 + 0x0C, 1u << 8);
  wr(TIM17 + 0x00, 1);
  api->fd_write(0x73, (uint16_t)(v[4] | 0x0010)); /* AFC off */
  api->say("xs wait carrier");
  t0 = api->ms();
  while (rssi(api) < floor + 15) {
    if (api->key_down()) { ret = -2; goto out; }
    if (api->ms() - t0 > 90000) { ret = -6; goto out; }
    api->delay_us(5000);
  }
  api->delay_us(600000);
  /* A */
  for (i = 0; i < 10; i++) {
    stat(api, g, 0x3B, (uint16_t)(v[0] + ks[i]), "xs a ", ks[i], &r);
    if (i == 3) if0 = (int)r.ifm;
    if (i == 4) ifp = (int)r.ifm;
    if (i == 5) ifm = (int)r.ifm;
  }
  ret = ifp - if0;
  /* B */
  stat(api, g, 0x38, (uint16_t)(v[2] + 100), "xs b ", 100, &r);
  stat(api, g, 0x38, (uint16_t)(v[2] - 100), "xs b ", -100, &r);
  stat(api, g, 0x38, v[2], "xs b ", 0, &r);
  if (ifp - ifm < 300 && ifm - ifp < 300) {
    /* L: is REG_3B taken at RXON? */
    int wide = (v[5] & 0xF000) == 0x3000, k;
    for (k = KC; k >= 0; k -= KC) {
      api->fd_write(0x3B, (uint16_t)(v[0] + k));
      api->tune(api->rx_hz(), wide);
      api->fd_write(0x73, (uint16_t)(v[4] | 0x0010));
      api->delay_us(20000);
      stat(api, g, 0x3B, (uint16_t)(v[0] + k), "xs l ", k, &r);
    }
  } else {
    go(api, g, 0, 2 * HALF * (WRITES + 3), 1, v[0], &r);
    bins(api, g);
    p = put(s, "xs c wr=");
    p = dec(p, r.wr_outs);
    p = put(p, " n=");
    p = dec(p, r.writes);
    p = put(p, " lost=");
    p = dec(p, r.lost);
    p = put(p, " ks=");
    p = dec(p, r.ks);
    *p = 0;
    api->say(s);
  }
  if (api->key_down()) ret = -2;
out:
  wr(TIM17 + 0x00, 0);
  wr(TIM17 + 0x0C, 0);
  wr(DMA_CCR1, 0);
  wr(DMA + 0x04, 0xF);
  wr(DMA_CNDTR1, 0);
  wr(DMA_CPAR1, 0);
  wr(DMA_CMAR1, 0);
  wr(RCC_APB2ENR, apb2);
  wr(RCC_AHBENR, ahb);
  api->fd_write(0x3B, v[0]);
  api->fd_write(0x38, v[2]);
  api->fd_write(0x73, v[4]);
  api->fd_write(0x34, r34);
  api->fd_write(0x33, r33);
  api->say("xs done");
  return ret;
}
