/* gp0cap: record short stretches of the FD6818's receive IF (GP0 code 13 on PB1, sampled by DMA
   as gp0dma does) at stepped front-end gains, for the host to run the IF and FM-discriminator
   detectors on the same captures (docs/k6-if-modem.md, tools/k6_ifcap.py).

   Each capture is NCAP outputs, one per 64 DMA samples (about 85.8 kHz: 25 ms), each 2 * ones - 64
   as a signed byte, kept on the stack and then sent in base64:
     c 1 3 7 ks=5490 r=-71 t=1234 lost=0          pass, gain step, capture, kS/s, RSSI dBm,
                                                     100 us ticks at its start, ring overruns
     y 7 0000 <48 base64 chars>                      capture 7's bytes from offset 0 (36 a line)
   Gain steps: 0 = the pager's AGC as it was; 1..NG-1 = AGC fixed (REG_7E bit 15, index 3) with
   REG_13 = gains[step] (BK4819 layout: [9:8] LNA short, [7:5] LNA, [4:3] mixer, [2:0] PGA;
   egzumer's AGC table has 0x03BE at about -7 dB, 0x037B -24, 0x027B -43, 0x007A -58), so the
   signal falls against the receiver's and the 1-bit path's noise.

   Steps: the upload's carrier drops; pass 0 (no carrier): NOISE captures at steps 0, 1 and 2
   (the GP0 noise floor at the pager's gain, the highest, and the AGC's top entry); then passes 1
   and 2 (the sender's FFSK and 4FSK, up to WAIT_S each for a carrier): K captures per step.
   Writes REG_34[3:0], REG_33 bit 8 (PR #68 image), REG_7E, REG_13, the MCU's TIM17, DMA channel
   1 and their clock enables, all put back. No TX. Returns the passes done, or -2 (a key), -3
   (old image), -4 (PB1 not an input), -5 (REG_33 refused), -6 (no carrier), -7 (TIM17 or DMA
   busy), -8 (scratch under 1028 bytes).
   SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 2
#include "../k6_mod.h"

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
#ifndef NCAP
#define NCAP 2160u   /* outputs a capture: 25 ms (30 symbols at 1200 baud), on the module call's 3.5 KB stack */
#endif
#ifndef K
#define K 10         /* captures a gain step, with a carrier */
#endif
#ifndef NOISE
#define NOISE 2      /* captures a gain step, without */
#endif
#ifndef WAIT_S
#define WAIT_S 90
#endif
#define NG 10
static const uint16_t gains[NG] = {0,      0x03FF, 0x03BE, 0x037E, 0x033E,
                                   0x031E, 0x021E, 0x011E, 0x001E, 0x0018};

#define NOINLINE __attribute__((noinline))

static uint32_t rd(uint32_t a) { return *(volatile uint32_t *)a; }
static void wr(uint32_t a, uint32_t v) { *(volatile uint32_t *)a = v; }

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

static char *hex4(char *p, uint32_t v) {
  int s;
  for (s = 12; s >= 0; s -= 4) {
    uint32_t d = (v >> s) & 15;
    *p++ = (char)(d < 10 ? '0' + d : 'a' + d - 10);
  }
  return p;
}

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

static void stop(void) {
  wr(TIM17 + 0x00, 0);
  wr(TIM17 + 0x0C, 0);
  wr(DMA_CCR1, 0);
  wr(DMA + 0x04, 0xF);
}

/* L samples at ring offset r: the ones on PB1 (bit 1 of each byte) */
static int ones(const uint8_t *ring, uint32_t r) {
  const uint32_t *p = (const uint32_t *)(ring + r);
  uint32_t acc = 0, i;
  for (i = 0; i < L / 4; i += 4)
    acc += (p[i] & 0x02020202u) + (p[i + 1] & 0x02020202u) + (p[i + 2] & 0x02020202u) +
           (p[i + 3] & 0x02020202u);
  acc >>= 1;
  acc = (acc & 0x00FF00FFu) + ((acc >> 8) & 0x00FF00FFu);
  return (int)((acc & 0xFFFF) + (acc >> 16));
}

typedef struct {
  uint32_t ks, lost;
} cap_t;

/* one capture of NCAP outputs into buf, the DMA started afresh at the ring's start */
NOINLINE static void capture(uint8_t *ring, int8_t *buf, cap_t *c) {
  uint32_t laps = 0, lastw = 0, consumed = 0, produced, w, n = 0, cyc = 0, c0, cn, avail;
  c->lost = 0;
  wr(DMA_CCR1, 0);
  wr(DMA + 0x04, 0xF);
  wr(DMA_CPAR1, GPIOB + 0x10);
  wr(DMA_CMAR1, (uint32_t)ring);
  wr(DMA_CNDTR1, RSZ);
  wr(TIM17 + 0x00, 0);
  wr(TIM17 + 0x28, 0);
  wr(TIM17 + 0x2C, 0xFFFF);
  wr(TIM17 + 0x14, 1);
  wr(TIM17 + 0x10, 0);
  wr(TIM17 + 0x0C, 1u << 8);
  wr(TIM17 + 0x00, 1);
  wr(DMA_CCR1, (3u << 12) | (1u << 7) | (1u << 5) | 1u); /* PL very high, MINC, CIRC, EN */
  c0 = rd(TIM17_CNT) & 0xFFFF;
  while (n < NCAP && cyc < 200000000u) {
    w = RSZ - (rd(DMA_CNDTR1) & 0xFFFF);
    if (w < lastw) laps++;
    lastw = w;
    produced = laps * RSZ + w;
    cn = rd(TIM17_CNT) & 0xFFFF;
    if (n) cyc += (cn - c0) & 0xFFFF;
    c0 = cn;
    avail = produced - consumed;
    if (avail > RSZ - L) {
      c->lost++;
      consumed = produced & ~(L - 1);
      continue;
    }
    if (avail < L) continue;
    buf[n++] = (int8_t)(2 * ones(ring, consumed & (RSZ - 1)) - (int)L);
    consumed += L;
  }
  stop();
  /* kS/s = outputs * L / (cycles / 96 MHz) / 1000 */
  c->ks = udiv(n * L * 96, udiv(cyc, 1000) ? udiv(cyc, 1000) : 1);
  if (n < NCAP) c->lost = 0xFFFF;
}

static const char b64[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

NOINLINE static void dump(const k6_api_t *api, const int8_t *buf, uint32_t k) {
  char line[64], *p;
  uint32_t off, i;
  for (off = 0; off < NCAP; off += 36) {
    p = put(line, "y ");
    p = dec(p, k);
    *p++ = ' ';
    p = hex4(p, off);
    *p++ = ' ';
    for (i = off; i < off + 36 && i < NCAP; i += 3) {
      uint32_t v = (uint32_t)(uint8_t)buf[i] << 16;
      if (i + 1 < NCAP) v |= (uint32_t)(uint8_t)buf[i + 1] << 8;
      if (i + 2 < NCAP) v |= (uint8_t)buf[i + 2];
      *p++ = b64[(v >> 18) & 63];
      *p++ = b64[(v >> 12) & 63];
      *p++ = b64[(v >> 6) & 63];
      *p++ = b64[v & 63];
    }
    *p = 0;
    api->say(line);
  }
}

typedef struct {
  uint32_t ahb, apb2;
  uint16_t r33, r34, r7e, r13, r73;
  int floor;
  uint32_t k; /* captures so far */
} saved_t;

static void gain(const k6_api_t *api, const saved_t *sv, int g) {
  if (!g) {
    api->fd_write(0x13, sv->r13);
    api->fd_write(0x7E, sv->r7e);
  } else {
    api->fd_write(0x7E, (uint16_t)((sv->r7e & 0x0FFF) | 0x8000 | (3u << 12)));
    api->fd_write(0x13, gains[g]);
  }
  api->delay_us(30000);
}

NOINLINE static void one(const k6_api_t *api, saved_t *sv, uint8_t *ring, int pass, int g) {
  int8_t buf[NCAP];
  cap_t c;
  char line[64], *p;
  uint32_t t = api->ticks() & 0xFFFF;
  int r = rssi(api);
  capture(ring, buf, &c);
  p = put(line, "c ");
  p = dec(p, (uint32_t)pass);
  *p++ = ' ';
  p = dec(p, (uint32_t)g);
  *p++ = ' ';
  p = dec(p, sv->k);
  p = put(p, " ks=");
  p = dec(p, c.ks);
  p = put(p, " r=");
  p = sdec(p, r);
  p = put(p, " t=");
  p = dec(p, t);
  p = put(p, " lost=");
  p = dec(p, c.lost);
  *p = 0;
  api->say(line);
  dump(api, buf, sv->k++);
}

static int wait_level(const k6_api_t *api, int above, int lvl, uint32_t ms) {
  uint32_t t0 = api->ms();
  while ((rssi(api) >= lvl) != above) {
    if (api->key_down()) return -2;
    if (api->ms() - t0 > ms) return -6;
    api->delay_us(5000);
  }
  return 0;
}

int k6_main(const k6_api_t *api) {
  saved_t sv;
  uint8_t *ring;
  uint32_t room;
  int ret = 0, pass, g, i;
  char line[64], *p;
  if (api->abi != 1 || api->level < 2) return -3;
  if (wait_level(api, 0, -80, 30000) == -2) return -2;
  api->delay_us(300000);
  sv.floor = rssi(api);
  sv.k = 0;
  ring = (uint8_t *)(((uint32_t)api->scratch(&room) + 3) & ~3u);
  if (room < RSZ + 4) return -8;
  if (rd(GPIOB) & (3u << 2)) {
    api->say("cap PB1 is not an input: no writes");
    return -4;
  }
  sv.apb2 = rd(RCC_APB2ENR);
  sv.ahb = rd(RCC_AHBENR);
  wr(RCC_AHBENR, sv.ahb | 1u); /* the DMA's registers read junk with its clock off */
  if ((sv.apb2 & TIM17EN) || (rd(DMA_CCR1) & 1)) {
    wr(RCC_AHBENR, sv.ahb);
    api->say("cap TIM17 or DMA channel 1 busy: no writes");
    return -7;
  }
  sv.r33 = api->fd_read(0x33);
  sv.r34 = api->fd_read(0x34);
  sv.r7e = api->fd_read(0x7E);
  sv.r13 = api->fd_read(0x13);
  if ((sv.r33 & 0x0100) && api->fd_write(0x33, (uint16_t)(sv.r33 & ~0x0100)) != 0) {
    wr(RCC_AHBENR, sv.ahb);
    api->say("cap REG_33 refused: needs the PR #68 image");
    return -5;
  }
  api->fd_write(0x34, (uint16_t)((sv.r34 & 0xFFF0) | 13));
  sv.r73 = api->fd_read(0x73);
#ifdef CAP_AFC_OFF
  /* AFC off, then RX on again for it to take hold (the FT8 thread's run 3: the AFC pulls a
     steady USB tone toward the IF's centre, and REG_73 latches like REG_38/3B) */
  api->fd_write(0x73, (uint16_t)(sv.r73 | 0x0010));
  api->tune(api->rx_hz(), api->fd_read(0x43) == 0x3028);
#endif
  wr(RCC_APB2ENR, sv.apb2 | TIM17EN);
  p = put(line, "cap start floor=");
  p = sdec(p, sv.floor);
  p = put(p, " ncap=");
  p = dec(p, NCAP);
  p = put(p, " r7e=");
  p = hex4(p, sv.r7e);
  p = put(p, " r13=");
  p = hex4(p, sv.r13);
  p = put(p, " r73=");
  p = hex4(p, api->fd_read(0x73));
  *p = 0;
  api->say(line);
  for (pass = 0; pass < 3; pass++) {
    if (pass) {
      gain(api, &sv, 0);
      if (pass > 1) {
        ret = wait_level(api, 0, sv.floor + 10, 30000);
        if (ret) break;
      }
      p = put(line, "cap pass ");
      p = dec(p, (uint32_t)pass);
      p = put(p, " waiting");
      *p = 0;
      api->say(line);
      ret = wait_level(api, 1, sv.floor + 15, WAIT_S * 1000u);
      if (ret) break;
      api->delay_us(2000000); /* the sender's 1 s lead-in, after paplay's 0.3-0.8 s start */
    }
    for (g = 0; g < (pass ? NG : 3); g++) {
      gain(api, &sv, g);
      for (i = 0; i < (pass ? K : NOISE); i++) one(api, &sv, ring, pass, g);
      if (api->key_down()) {
        ret = -2;
        goto out;
      }
    }
    ret = pass;
  }
out:
  stop();
  gain(api, &sv, 0);
  wr(TIM17 + 0x2C, 0xFFFF);
  wr(DMA_CNDTR1, 0);
  wr(DMA_CPAR1, 0);
  wr(DMA_CMAR1, 0);
  wr(RCC_APB2ENR, sv.apb2);
  wr(RCC_AHBENR, sv.ahb);
  api->fd_write(0x34, sv.r34);
  api->fd_write(0x33, sv.r33);
  api->fd_write(0x73, sv.r73);
  api->say("cap done");
  return ret;
}
