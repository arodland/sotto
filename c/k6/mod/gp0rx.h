/* gp0rx.h: the receive-IF front end the FT8/WSPR modules share (ft8rx, ft8wf, docs/k6-ft8-wspr.md):
   DMA channel 1 reading GP0 code 13 (the FD6818's low IF on PB1) into a ring flat out, as
   gp0dma does (PR #69); a 96 MHz clock kept unwrapped from TIM17 and Aura's 100 us TIM14; counts
   of 64 samples handed to ft8k's mixer with their real-time phase; the noise-filling gain set-up.
   Include it after k6_mod.h and ft8k.c, with FT8_AUDIO (the window's centre, Hz), FT8_INV,
   FT8_RMS_LO and FT8_RMS_HI defined. SPDX-License-Identifier: MIT */
#ifndef GP0RX_H
#define GP0RX_H
#define GPIOB 0x48000400u
#define RCC_AHBENR 0x40021014u
#define RCC_APB2ENR 0x40021018u
#define TIM14_CNT 0x40002024u
#define TIM17 0x40014800u
#define TIM17_CNT (TIM17 + 0x24)
#define DMA 0x40020000u
#define DMA_CCR1 (DMA + 0x08)
#define DMA_CNDTR1 (DMA + 0x0C)
#define DMA_CPAR1 (DMA + 0x10)
#define DMA_CMAR1 (DMA + 0x14)
#define TIM17EN (1u << 18)
#define RSZ 512u
#define LCNT 64u
/* the mixer: phase a 96 MHz cycle for 26 MHz / 1536 +- FT8_AUDIO, in 2^32 a turn */
#define FMIX_MHZ (FT8_INV ? 16927083ull - FT8_AUDIO * 1000ull : 16927083ull + FT8_AUDIO * 1000ull)
#define KMIX ((uint32_t)((FMIX_MHZ << 32) / 96000000000ull))
#define ROW_CYC 3840000u /* 40 ms */

static uint32_t rd(uint32_t a) { return *(volatile uint32_t *)a; }
static void wr(uint32_t a, uint32_t v) { *(volatile uint32_t *)a = v; }

static char *put(char *p, const char *s) {
  while (*s) *p++ = *s++;
  return p;
}

static char *dec(char *p, uint32_t v) {
  char t[10];
  int n = 0;
  do {
    uint32_t q = fk_udiv(v, 10);
    t[n++] = (char)('0' + v - 10 * q);
    v = q;
  } while (v);
  while (n) *p++ = t[--n];
  return p;
}

__attribute__((unused)) static char *sdec(char *p, int v) {
  if (v < 0) *p++ = '-', v = -v;
  return dec(p, (uint32_t)v);
}

static char *kv(char *p, const char *k, uint32_t v) { return dec(put(p, k), v); }

/* tenths: 47 -> "4.7" */
__attribute__((unused)) static char *tenths(char *p, uint32_t v) {
  uint32_t q = fk_udiv(v, 10);
  p = dec(p, q);
  *p++ = '.';
  *p++ = (char)('0' + v - 10 * q);
  return p;
}

static char *hex4(char *p, uint32_t v) {
  int i;
  for (i = 12; i >= 0; i -= 4) {
    uint32_t d = (v >> i) & 15;
    *p++ = (char)(d < 10 ? '0' + d : 'a' + d - 10);
  }
  return p;
}

static void say0(const k6_api_t *api, char *line, char *p) {
  *p = 0;
  api->say(line);
}

typedef struct {
  /* clock: 96 MHz cycles, unwrapped */
  uint32_t t, c17, c14;
  /* the ring */
  uint8_t *ring;
  uint32_t laps, lastw, produced, consumed;
  uint32_t cps8;                 /* 96 MHz cycles a DMA transfer, Q8 */
  uint32_t p0, t0;               /* the start of this gap-free stretch */
  uint32_t lost, rows;
  uint32_t tc, tcok;             /* the next count's time, once the receiver has had one */
} rx_t;

static uint32_t now(rx_t *r) {
  uint32_t c17 = rd(TIM17_CNT) & 0xFFFF, c14 = rd(TIM14_CNT) & 0xFFFF;
  uint32_t coarse = ((c14 - r->c14) & 0xFFFF) * 9600u, fine = (c17 - r->c17) & 0xFFFF;
  /* the step = fine mod 2^16, the one nearest the coarse 100 us count */
  int32_t d = (int32_t)(int16_t)(uint16_t)(fine - coarse);
  r->c17 = c17, r->c14 = c14;
  r->t += coarse + (uint32_t)d;
  return r->t;
}

static uint32_t poll(rx_t *r) {
  uint32_t w = RSZ - (rd(DMA_CNDTR1) & 0xFFFF);
  if (w < r->lastw) r->laps++;
  r->lastw = w;
  return r->produced = r->laps * RSZ + w;
}

/* after a pause (a row's decode, a report line) the ring has lapped unseen: start again from
   where the DMA is now */
static void resync(rx_t *r) {
  uint32_t w = RSZ - (rd(DMA_CNDTR1) & 0xFFFF);
  r->laps = 0;
  r->lastw = w;
  r->produced = w;
  r->consumed = w & ~(LCNT - 1);
  r->p0 = r->consumed;
  r->t0 = now(r);
}

static int ones(const uint8_t *ring, uint32_t at) {
  const uint32_t *p = (const uint32_t *)(ring + at);
  uint32_t acc = 0, i;
  for (i = 0; i < LCNT / 4; i++) acc += p[i] & 0x02020202u;
  acc >>= 1;
  acc = (acc & 0x00FF00FFu) + ((acc >> 8) & 0x00FF00FFu);
  return (int)((acc & 0xFFFF) + (acc >> 16));
}

/* take every whole count the DMA has made; with k, mix them into the receiver. Returns the
   counts, and the sum of their squares in *sq */
static uint32_t drain(rx_t *r, fk_t *k, uint32_t *sq) {
  uint32_t n = 0, t, P;
  P = poll(r);
  t = now(r);
  if (P - r->consumed > RSZ - LCNT) { /* overrun */
    r->lost++;
    resync(r);
    return 0;
  }
  while (P - r->consumed >= LCNT) {
    int x = 2 * ones(r->ring, r->consumed & (RSZ - 1)) - (int)LCNT;
    if (k) {
      /* the count's middle sample was taken (P - that) transfers before t */
      uint32_t ago = ((P - r->consumed - LCNT / 2) * r->cps8) >> 8, tx = t - ago, ph;
      uint32_t cpc = (r->cps8 * LCNT) >> 8;
      /* counts missed while the CPU was away (a report line, a row's decode): zeros, so the
         decimated samples stay evenly spaced in real time. Skipping them instead would slip
         the 200 Hz clock and smear the tones across a row (sim: -16 dB 75% -> 20% with one
         16 ms line every 160 ms; 68% with the zeros) */
      if (r->tcok)
        while ((int32_t)(tx - r->tc) > (int32_t)(cpc >> 1)) fk_in(k, 0, 0), r->tc += cpc;
      r->tc = tx + cpc, r->tcok = 1;
      ph = KMIX * tx;
      fk_in(k, x, FT8_INV ? 0u - ph : ph); /* mirrored: mix by e^{+j...} below the IF */
    }
    if (sq) *sq += (uint32_t)(x * x);
    r->consumed += LCNT;
    n++;
  }
  return n;
}

static void dma_on(uint8_t *ring) {
  wr(DMA_CCR1, 0);
  wr(DMA + 0x04, 0xF);
  wr(DMA_CPAR1, GPIOB + 0x10);
  wr(DMA_CMAR1, (uint32_t)ring);
  wr(DMA_CNDTR1, RSZ);
  wr(DMA_CCR1, (3u << 12) | (1u << 7) | (1u << 5) | 1u); /* PL very high, MINC, CIRC, EN; 8-bit */
}

/* the counts' rms (x 10) over about 50 ms, and the DMA's rate into r->cps8 */
static uint32_t noise(rx_t *r) {
  uint32_t sq = 0, n = 0, t1, p1;
  resync(r);
  while (n < 4096) n += drain(r, 0, &sq);
  p1 = r->consumed, t1 = r->t;
  r->cps8 = fk_udiv((t1 - r->t0) << 8, p1 - r->p0);
  {
    uint32_t v = fk_udiv(sq * 4, n) * 25, s = 0, b = 1u << 30; /* (rms x 10)^2 */
    while (b > v) b >>= 2;
    while (b) {
      if (v >= s + b) v -= s + b, s = (s >> 1) + b;
      else s >>= 1;
      b >>= 2;
    }
    return s;
  }
}

static const uint16_t GAINS[4] = {0x03FF, 0x03BE, 0x037E, 0x033E};

typedef struct {
  uint32_t ahb, apb2;
  uint16_t r33, r34, r73, r7e, r13;
} saved_t;

static void teardown(const k6_api_t *api, const saved_t *sv) {
  wr(TIM17 + 0x00, 0);
  wr(TIM17 + 0x0C, 0);
  wr(DMA_CCR1, 0);
  wr(DMA + 0x04, 0xF);
  wr(DMA_CNDTR1, 0);
  wr(DMA_CPAR1, 0);
  wr(DMA_CMAR1, 0);
  wr(RCC_APB2ENR, sv->apb2);
  wr(RCC_AHBENR, sv->ahb);
  api->fd_write(0x13, sv->r13);
  api->fd_write(0x7E, sv->r7e);
  api->fd_write(0x73, sv->r73);
  api->fd_write(0x34, sv->r34);
  api->fd_write(0x33, sv->r33);
}


/* clocks, GP0 on PB1, AFC off, TIM17 counting, the DMA running; 0 or the module's error */
static int rx_setup(const k6_api_t *api, saved_t *sv, rx_t *r, uint8_t *ring) {
  uint32_t i = api->ms();
  if (api->level < 2) return -3;
  /* the upload's carrier gone */
  while (KM_RSSI_DBM(api->fd_read(0x67)) > -80 && api->ms() - i < 30000) {
    if (api->key_down()) return -2;
    api->delay_us(10000);
  }
  api->delay_us(200000);
  if (rd(GPIOB) & (3u << 2)) return -4;
  sv->apb2 = rd(RCC_APB2ENR);
  sv->ahb = rd(RCC_AHBENR);
  wr(RCC_AHBENR, sv->ahb | 1u); /* the DMA's registers read junk with its clock off */
  if ((sv->apb2 & TIM17EN) || (rd(DMA_CCR1) & 1)) {
    wr(RCC_AHBENR, sv->ahb);
    return -7;
  }
  sv->r33 = api->fd_read(0x33);
  sv->r34 = api->fd_read(0x34);
  sv->r73 = api->fd_read(0x73);
  sv->r7e = api->fd_read(0x7E);
  sv->r13 = api->fd_read(0x13);
  if ((sv->r33 & 0x0100) && api->fd_write(0x33, (uint16_t)(sv->r33 & ~0x0100)) != 0) {
    wr(RCC_AHBENR, sv->ahb);
    return -5;
  }
  api->fd_write(0x34, (uint16_t)((sv->r34 & 0xFFF0) | 13));
  api->fd_write(0x73, (uint16_t)(sv->r73 | 0x0010)); /* AFC off: it would chase the tones */
  wr(RCC_APB2ENR, sv->apb2 | TIM17EN);
  /* TIM17 free-running at 96 MHz with its update request on (the DMA runs flat out anyway) */
  wr(TIM17 + 0x00, 0);
  wr(TIM17 + 0x28, 0);
  wr(TIM17 + 0x2C, 0xFFFF);
  wr(TIM17 + 0x14, 1);
  wr(TIM17 + 0x10, 0);
  wr(TIM17 + 0x0C, 1u << 8);
  wr(TIM17 + 0x00, 1);
  r->t = 0, r->c17 = rd(TIM17_CNT) & 0xFFFF, r->c14 = rd(TIM14_CNT) & 0xFFFF;
  r->lost = r->rows = r->tcok = 0;
  r->ring = ring;
  dma_on(ring);
  return 0;
}

/* the gain: if the receiver noise is quiet in the 1-bit path at the pager's AGC, fix the AGC and
   take the highest REG_13 gain whose noise stays under FT8_RMS_HI. Then the second CIC's
   decimation for about 200 Hz. Appends "rms=4.7 g=03ff rms=9.1 r2=27 ks=5490" to p */
static char *rx_gain(const k6_api_t *api, const saved_t *sv, rx_t *r, uint32_t *r2, char *p) {
  uint32_t rms0 = noise(r), rms = rms0;
  int g;
  p = put(p, " rms=");
  p = tenths(p, rms0);
  if (rms0 < FT8_RMS_LO) {
    for (g = 0; g < 4; g++) {
      api->fd_write(0x7E, (uint16_t)((sv->r7e & 0x0FFF) | 0x8000 | (3u << 12)));
      api->fd_write(0x13, GAINS[g]);
      api->delay_us(20000);
      rms = noise(r);
      if (rms <= FT8_RMS_HI) break;
    }
    if (g == 4 || rms < rms0) { /* no better: the pager's AGC */
      api->fd_write(0x13, sv->r13);
      api->fd_write(0x7E, sv->r7e);
    } else {
      p = put(p, " g=");
      p = hex4(p, GAINS[g]);
      p = put(p, " rms=");
      p = tenths(p, rms);
    }
  }
  noise(r);
  *r2 = fk_udiv(120000u + (r->cps8 >> 1), r->cps8); /* about 200 Hz after 64 x 16 x r2 */
  p = kv(p, " r2=", *r2);
  return kv(p, " ks=", fk_udiv(96000u * 256u, r->cps8));
}

/* after a row: the DMA's rate over the stretch since the last pause, then start a new one */
static void rx_rate(rx_t *r) {
  if (r->consumed - r->p0 > 20000u)
    r->cps8 = (3 * r->cps8 + fk_udiv((r->t - r->t0) << 8, r->consumed - r->p0)) >> 2;
}
#endif
