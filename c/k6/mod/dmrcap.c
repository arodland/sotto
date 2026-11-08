/* dmrcap: record the FD6818's receive IF (GP0 code 13 on PB1, the DMA as in gp0cap.c and dmrdet.c)
   on one frequency whenever a carrier comes up, for the host to run the DMR detector on what the
   K6 itself hears (docs/k6-dmr.md: dmrdet found no syncs on a real 0.5 s DMR burst at -91 dBm
   that the IC-7100's audio showed clean).

   Lines, in gp0cap's format so tools/k6_ifcap.py's parser reads them:
     dcap start f=440925000 floor=-108 r43=3028 r73=.. r7e=.. r13=.. ncap=3240
     c 1 2 7 ks=5374 r=-91 t=1234 lost=0       carrier (pass 1; 0 = the no-carrier reference),
                                               gain step 2, capture 7, kS/s, RSSI dBm at its
                                               start (under that gain), 100 us ticks, overruns
     y 7 0000 <48 base64 chars>                capture 7's counts (2 * ones - 64, int8, ~84 kHz)
   A capture is NCAP counts (3240: ~38 ms, so a repeater's stream, a sync every 30 ms, has a
   whole sync in each), on the stack. The first starts within ~5 ms of the RSSI crossing
   floor + SQ_DB; up to PER_CAR follow while the carrier stays (each dump takes ~0.3 s on the
   cable), cycling through NG gain steps: 0 = the pager's AGC, then the AGC frozen (REG_7E bit
   15, index 3) with REG_13 from gains[] (gp0cap's layout). The first bench run (AGC only) had
   the DMR carriers at -85..-90 dBm only 4-5 dB over the 1-bit path's noise in the IF band,
   with the counts' rms down to 4 from 13 with no carrier: the AGC turns the IF down below
   what GP0 needs, so this finds the gain that does better. The cable is needed: the dumps are
   too long for telemetry.
   Writes REG_34[3:0], REG_33 bit 8, REG_7E, REG_13, the MCU's TIM17, DMA channel 1 and their clock enables, all
   put back; the firmware retunes after. No TX. Returns the carrier captures, or -2 (a key held
   at the start), -3 (old image), -4 (PB1 not an input), -5 (REG_33 refused), -7 (TIM17 or DMA
   busy), -8 (scratch under 1028 bytes).
   SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 2
#include "../k6_mod.h"

#ifndef FREQ_HZ
#define FREQ_HZ 440925000u
#endif
#ifndef WIDE
#define WIDE 1
#endif
#ifndef RUN_S
#define RUN_S 600u
#endif
#ifndef SQ_DB
#define SQ_DB 8
#endif
#ifndef NCAP
#define NCAP 3240u
#endif
#ifndef PER_CAR
#define PER_CAR 8
#endif
#define NG 4
static const uint16_t gains[NG] = {0, 0x03FF, 0x03BE, 0x037E};

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

/* one capture of NCAP counts into buf, the DMA started afresh at the ring's start */
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

/* one capture and its lines: pass 1 with a carrier, 0 without */
NOINLINE static void one(const k6_api_t *api, uint8_t *ring, int pass, int g, uint32_t k) {
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
  p = dec(p, k);
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
  dump(api, buf, k);
}

/* gain step g: 0 the pager's AGC as it was, else frozen at gains[g]; then 30 ms to settle */
NOINLINE static void gain(const k6_api_t *api, uint16_t r7e, uint16_t r13, int g) {
  if (!g) {
    api->fd_write(0x13, r13);
    api->fd_write(0x7E, r7e);
  } else {
    api->fd_write(0x7E, (uint16_t)((r7e & 0x0FFF) | 0x8000 | (3u << 12)));
    api->fd_write(0x13, gains[g]);
  }
  api->delay_us(30000);
}

int k6_main(const k6_api_t *api) {
  uint8_t *ring;
  uint32_t room, ahb, apb2, k = 0, i;
  uint16_t r33, r34, r7e, r13;
  int floor, ncar = 0, nc = 0;
  char line[64], *p;
  if (api->abi != 1 || api->level < 2) return -3;
  if (api->key_down()) return -2;
  ring = (uint8_t *)(((uint32_t)api->scratch(&room) + 3) & ~3u);
  if (room < RSZ + 4) return -8;
  if (rd(GPIOB) & (3u << 2)) return -4;
  apb2 = rd(RCC_APB2ENR);
  ahb = rd(RCC_AHBENR);
  wr(RCC_AHBENR, ahb | 1u); /* the DMA's registers read junk with its clock off */
  if ((apb2 & TIM17EN) || (rd(DMA_CCR1) & 1)) {
    wr(RCC_AHBENR, ahb);
    return -7;
  }
  r33 = api->fd_read(0x33);
  r34 = api->fd_read(0x34);
  r7e = api->fd_read(0x7E);
  r13 = api->fd_read(0x13);
  if ((r33 & 0x0100) && api->fd_write(0x33, (uint16_t)(r33 & ~0x0100)) != 0) {
    wr(RCC_AHBENR, ahb);
    return -5;
  }
  api->tune(FREQ_HZ, WIDE);
  api->fd_write(0x34, (uint16_t)((r34 & 0xFFF0) | 13));
  api->delay_us(100000);
  floor = rssi(api);
  wr(RCC_APB2ENR, apb2 | TIM17EN);
  p = put(line, "dcap start f=");
  p = dec(p, FREQ_HZ);
  p = put(p, " floor=");
  p = sdec(p, floor);
  p = put(p, " r43=");
  p = hex4(p, api->fd_read(0x43));
  p = put(p, " r73=");
  p = hex4(p, api->fd_read(0x73));
  *p = 0;
  api->say(line);
  p = put(line, "dcap r7e=");
  p = hex4(p, api->fd_read(0x7E));
  p = put(p, " r13=");
  p = hex4(p, api->fd_read(0x13));
  p = put(p, " ncap=");
  p = dec(p, NCAP);
  *p = 0;
  api->say(line);
  if (rssi(api) < floor + SQ_DB) one(api, ring, 0, 0, k++); /* the no-carrier reference */
  while (api->ms() < RUN_S * 1000u && !api->key_down()) {
    if (rssi(api) < floor + SQ_DB) {
      api->delay_us(5000);
      continue;
    }
    /* the carrier checked under the pager's AGC each time: a frozen gain moves the RSSI */
    for (i = 0; i < PER_CAR && rssi(api) >= floor + SQ_DB; i++) {
      gain(api, r7e, r13, (int)(i % NG));
      one(api, ring, 1, (int)(i % NG), k++), nc++;
      gain(api, r7e, r13, 0);
    }
    ncar++;
    p = put(line, "dcap car ");
    p = dec(p, (uint32_t)ncar);
    p = put(p, " ms=");
    p = dec(p, api->ms());
    *p = 0;
    api->say(line);
    while (rssi(api) >= floor + SQ_DB && api->ms() < RUN_S * 1000u && !api->key_down())
      api->delay_us(5000);
  }
  stop();
  wr(TIM17 + 0x2C, 0xFFFF);
  wr(DMA_CNDTR1, 0);
  wr(DMA_CPAR1, 0);
  wr(DMA_CMAR1, 0);
  wr(RCC_APB2ENR, apb2);
  wr(RCC_AHBENR, ahb);
  gain(api, r7e, r13, 0);
  api->fd_write(0x34, r34);
  api->fd_write(0x33, r33);
  api->say("dcap done");
  return nc;
}
