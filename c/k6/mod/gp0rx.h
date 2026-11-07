/* gp0rx.h: the receive-IF front end the FT8/WSPR modules share (ft8rx, ft8wf, docs/k6-ft8-wspr.md):
   DMA channel 1 reading GP0 code 13 (the FD6818's low IF on PB1) into a ring flat out, as
   gp0dma does (PR #69); a 96 MHz clock kept unwrapped from TIM17 and Aura's 100 us TIM14; counts
   of 64 samples handed to ft8k's mixer with their real-time phase; the noise-filling gain set-up.
   Include it after k6_mod.h and ft8k.c, with FT8_AUDIO (the window's centre, Hz), FT8_INV,
   FT8_RMS_LO and FT8_RMS_HI defined.

   The mixer follows the radio's calibration record (k6_cal.h, written once by ifcal): on an
   image at API level 4 or above with a good record, rx_setup moves it by the record's IF
   offset at rx_hz, so the sender plays FT8 at its true audio frequency (--f0 1480, not 1624).
   On an older image or with no record it mixes at the nominal IF, as before (r->cal = 0).
   GP0_CAL 0 builds it without. SPDX-License-Identifier: MIT */
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
#ifndef RSZ
#define RSZ 1024u /* the DMA ring: 186 us at 5.5 MS/s */
#endif
#ifndef LCNT
#define LCNT 64u /* samples a count (64 or 128, in 64-sample halves, so the gain set-up's rms
                    stays in 64-sample counts); ft8k's first CIC decimates by 1024 / LCNT.
                    128 halves the per-count work but its boxcar folds more of the
                    sigma-delta's noise onto the IF: sim -16 dB 75% -> 38% (floor 0.15) */
#endif
/* the mixer: phase a 96 MHz cycle for 26 MHz / 1536 +- FT8_AUDIO, in 2^32 a turn */
#define FMIX_MHZ (FT8_INV ? 16927083ull - FT8_AUDIO * 1000ull : 16927083ull + FT8_AUDIO * 1000ull)
#define KMIX ((uint32_t)((FMIX_MHZ << 32) / 96000000000ull))
#define ROW_CYC 3840000u /* 40 ms */
#ifndef GP0_CAL
#define GP0_CAL 1
#endif
#if GP0_CAL
#include "k6_cal.h"
#endif

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

/* base64 of n bytes (the report's binary payloads: ft8wf's rows, wspriq's samples) */
__attribute__((unused)) static char b64(uint32_t v) {
  v &= 63;
  return (char)(v < 26 ? 'A' + v : v < 52 ? 'a' + v - 26 : v < 62 ? '0' + v - 52 : v == 62 ? '+' : '/');
}

__attribute__((unused)) static char *base64(char *p, const uint8_t *b, int n) {
  int i;
  for (i = 0; i < n; i += 3) {
    uint32_t v = (uint32_t)b[i] << 16 | (i + 1 < n ? (uint32_t)b[i + 1] << 8 : 0) |
                 (i + 2 < n ? b[i + 2] : 0);
    *p++ = b64(v >> 18), *p++ = b64(v >> 12);
    *p++ = i + 1 < n ? b64(v >> 6) : '=';
    *p++ = i + 2 < n ? b64(v) : '=';
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
  uint32_t tp, idle, was_idle;   /* the last poll's time; cycles spent waiting for the DMA */
  uint32_t kmix;                 /* the mixer's step a 96 MHz cycle: KMIX plus the calibration */
  int32_t cal;                   /* the IF offset applied, 0.01 Hz (0: no record) */
} rx_t;

/* ft8k's first CIC for counts of LCNT samples: 1024 / LCNT, and the shift that keeps its output
   at the scale of 64-sample counts into 16 */
#define RX_FK(k) ((k)->r1 = (uint8_t)(1024u / LCNT), (k)->sh1 = (uint8_t)(LCNT == 128 ? 14 : 16))

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

/* the ones on PB1 (bit 1 of each byte) in 64 samples, unrolled four words a pass */
static int ones64(const uint8_t *q) {
  const uint32_t *p = (const uint32_t *)q, m = 0x02020202u;
  uint32_t a = 0, i;
  for (i = 0; i < 16; i += 4) a += (p[i] & m) + (p[i + 1] & m) + (p[i + 2] & m) + (p[i + 3] & m);
  a >>= 1;
  a = (a & 0x00FF00FFu) + ((a >> 8) & 0x00FF00FFu);
  return (int)((a & 0xFFFF) + (a >> 16));
}

/* take every whole count the DMA has made; with k, mix them into the receiver. Returns the
   counts, and the sum of their 64-sample halves' squares in *sq */
static uint32_t drain(rx_t *r, fk_t *k, uint32_t *sq) {
  uint32_t n = 0, t, P, cpc, tx, ph = 0, dph = 0;
  P = poll(r);
  t = now(r);
  if (P - r->consumed > RSZ - LCNT) { /* overrun */
    r->lost++;
    resync(r);
    return 0;
  }
  if (P - r->consumed < LCNT) {
    if (r->was_idle) r->idle += t - r->tp;
    r->tp = t, r->was_idle = 1;
    return 0;
  }
  r->tp = t, r->was_idle = 0;
  cpc = (r->cps8 * LCNT) >> 8;
  /* the first count's middle sample was taken (P - that) transfers before t */
  tx = t - (((P - r->consumed - LCNT / 2) * r->cps8) >> 8);
  if (k) {
    /* counts missed while the CPU was away (a report line, a row's decode): zeros, so the
       decimated samples stay evenly spaced in real time. Skipping them instead would slip
       the 200 Hz clock and smear the tones across a row (sim: -16 dB 75% -> 20% with one
       16 ms line every 160 ms; 68% with the zeros). Not after a pause of over 4096 counts. */
    if (r->tcok) {
      int32_t gap = (int32_t)(tx - r->tc);
      if (gap < (int32_t)(cpc << 12))
        for (; gap > (int32_t)(cpc >> 1); gap -= (int32_t)cpc) fk_in(k, 0, 0);
    }
    ph = r->kmix * tx, dph = r->kmix * cpc;
  }
  while (P - r->consumed >= LCNT) {
    const uint8_t *q = r->ring + (r->consumed & (RSZ - 1));
    int a = 2 * ones64(q) - 64, b = LCNT == 128 ? 2 * ones64(q + 64) - 64 : 0;
    if (k) {
      fk_in(k, a + b, FT8_INV ? 0u - ph : ph); /* mirrored: mix by e^{+j...} below the IF */
      ph += dph;
    }
    if (sq) *sq += (uint32_t)(a * a + b * b);
    r->consumed += LCNT;
    n++;
  }
  if (k) r->tc = tx + n * cpc, r->tcok = 1;
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
  while (n < 4096 * 64 / LCNT) n += drain(r, 0, &sq);
  n = n * (LCNT / 64); /* 64-sample halves */
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


/* the mixer's step: the nominal IF +- FT8_AUDIO, moved by the calibration record's offset at
   rx_hz when the image has one (either sideband: the carrier moves both ways alike) */
static void rx_cal(const k6_api_t *api, rx_t *r) {
  r->kmix = KMIX, r->cal = 0;
#if GP0_CAL
  {
    k6_cal_t c;
    int32_t off;
    if (k6cal_get(api, &c)) return;
    off = k6cal_off_chz(&c, api->rx_hz());
    r->kmix = KMIX + (uint32_t)k6cal_kmix(off), r->cal = off;
  }
#else
  (void)api;
#endif
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
  /* and RX on again, for the AFC-off to take hold: run 3 (2026-11-06) saw a steady USB tone's
     IF slide ~700 Hz in 43 s towards the nominal IF (an AFC's pull, time constant ~30 s), and
     with the bit set but no RXON (run 2) FT8 still left the window within seconds. The chip
     latches REG_38/3B the same way (xtalstep). Same frequency and filter (REG_43 = Aura's wide
     0x3028 or narrow 0x4048) */
  api->tune(api->rx_hz(), api->fd_read(0x43) == 0x3028);
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
  r->lost = r->rows = r->tcok = r->idle = r->was_idle = 0;
  r->ring = ring;
  rx_cal(api, r);
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
