/* aiscap: record AIS bursts as the K6 hears them, for the host's receiver (sim/k6if/ais.py,
   docs/k6-ais.md). Receive only.

   The K6 sits on AIS channel A (161.975 MHz) or B (162.025 MHz), switching every SWITCH_S, with
   the FD6818's low IF on PB1 (GP0 code 13) read by DMA as in dmrcap.c. The counts (2 * ones -
   64 per 64 DMA samples, ~85.8 kHz) run round a ring of NCAP in the scratch all the time, and
   the RSSI is read every CHECK counts. When it rises SQ_DB over the channel's floor, the ring
   keeps going for NCAP - PRE counts more and then stops, so it holds PRE counts (~12 ms) from
   before the RSSI saw the carrier and the whole 27 ms burst after. An AIS burst is far shorter
   than the RSSI's settling plus a start-up, so dmrcap's start-on-the-carrier would miss its
   head, where the message type and the station are.

   The gain is frozen (REG_7E bit 15, index 3) at REG_13 = GAIN13 for the run (0: the pager's
   AGC): on DMR the AGC turned the IF down to 4-5 dB over the 1-bit path's noise, full gain gave
   +10 dB (docs/k6-dmr.md). The floor is read under that gain.

   Lines (the c and y lines in gp0cap's format, so sim/k6if/dmr.py's parse_caps reads them):
     acap start a=161975000 b=162025000 g13=03ff ncap=3456 pre=1024
     m B floor=-117 n=4 caps=3 ms=60123          a channel's turn: its floor, bursts, captures
     c 1 1 7 ks=5490 r=-80 t=1234 lost=0 ch=B rmax=-77 trig=1031 ms=61234
                                                 capture 7: kS/s, RSSI at the trigger and its
                                                 peak, 100 us ticks, overruns, the trigger's
                                                 count in the capture, module ms at the end
     y 7 0000 <48 base64 chars>                  its counts (int8)
     b B r=-95 ms=..                             a burst past MAXCAP (counted, not dumped)
     acap done n=31 caps=31
   The dumps are too long for telemetry: the cable is needed. Writes REG_34[3:0], REG_33 bit 8,
   REG_7E, REG_13, the MCU's TIM17, DMA channel 1 and their clock enables, all put back; the
   firmware retunes after. Everything big is in the scratch (1 KB DMA ring and the NCAP ring), so
   the stack stays small. Returns the captures, or -2 (a key held at the start), -3 (old image),
   -4 (PB1 not an input), -5 (REG_33 refused), -7 (TIM17 or DMA busy), -8 (scratch too small).
   SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 2
#include "../k6_mod.h"

#ifndef CH_A_HZ
#define CH_A_HZ 161975000u
#endif
#ifndef CH_B_HZ
#define CH_B_HZ 162025000u
#endif
#ifndef FIRST_B
#define FIRST_B 1 /* start on channel B */
#endif
#ifndef SWITCH_S
#define SWITCH_S 60u
#endif
#ifndef RUN_S
#define RUN_S 600u
#endif
#ifndef SQ_DB
#define SQ_DB 8
#endif
#ifndef NCAP
#define NCAP 3456u /* ~40 ms */
#endif
#ifndef PRE
#define PRE 1024u /* ~12 ms kept from before the trigger */
#endif
#ifndef CHECK
#define CHECK 64u /* counts between RSSI reads (~0.75 ms) */
#endif
#ifndef MAXCAP
#define MAXCAP 60u
#endif
#ifndef GAIN13
#define GAIN13 0x03FFu
#endif
#ifndef WIDE
#define WIDE 1
#endif

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
  uint32_t ks, lost, head, trig, end_ms;
  int r, rmax, got; /* got: 1 a burst recorded, 0 the turn ended, -1 a key */
} cap_t;

/* run the DMA and the count ring until a burst has been recorded (or the turn's end at
   until_ms, or a key) */
NOINLINE static void listen(const k6_api_t *api, uint8_t *ring, int8_t *buf, int thr,
                            uint32_t until_ms, cap_t *c) {
  uint32_t laps = 0, lastw = 0, consumed = 0, produced, w, cyc = 0, c0, cn, avail;
  uint32_t n = 0, head = 0, since = 0, post = 0, checks = 0;
  int trig = 0, r;
  c->lost = 0, c->got = 0, c->rmax = -200, c->r = -200, c->trig = 0;
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
  for (;;) {
    w = RSZ - (rd(DMA_CNDTR1) & 0xFFFF);
    if (w < lastw) laps++;
    lastw = w;
    produced = laps * RSZ + w;
    cn = rd(TIM17_CNT) & 0xFFFF;
    cyc += (cn - c0) & 0xFFFF;
    c0 = cn;
    avail = produced - consumed;
    if (avail > RSZ - L) {
      c->lost++;
      consumed = produced & ~(L - 1);
      continue;
    }
    if (avail < L) continue;
    buf[head] = (int8_t)(2 * ones(ring, consumed & (RSZ - 1)) - (int)L);
    consumed += L;
    if (++head == NCAP) head = 0;
    n++;
    if (trig) {
      if (!--post) break;
    }
    if (++since < CHECK) continue;
    since = 0;
    r = rssi(api);
    if (trig) {
      if (r > c->rmax) c->rmax = r;
      continue;
    }
    if (r >= thr && n > PRE) {
      trig = 1, post = NCAP - PRE, c->r = c->rmax = r;
      continue;
    }
    if (++checks >= 64) { /* ~50 ms: the turn and the keys */
      checks = 0;
      if (api->ms() >= until_ms) break;
      if (api->key_down()) {
        c->got = -1;
        break;
      }
    }
  }
  stop();
  c->head = head; /* the oldest count */
  c->trig = PRE;
  c->end_ms = api->ms();
  /* kS/s = counts * L / (cycles / 96 MHz) / 1000 */
  c->ks = udiv(n * L * 96, udiv(cyc, 1000) ? udiv(cyc, 1000) : 1);
  if (trig && !post) c->got = 1;
}

static const char b64[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

/* the ring from its oldest count on, in gp0cap's y lines */
NOINLINE static void dump(const k6_api_t *api, const int8_t *buf, uint32_t head, uint32_t k) {
  char line[64], *p;
  uint32_t off, i, j, m, v;
  for (off = 0; off < NCAP; off += 36) {
    p = put(line, "y ");
    p = dec(p, k);
    *p++ = ' ';
    p = hex4(p, off);
    *p++ = ' ';
    j = head + off;
    if (j >= NCAP) j -= NCAP;
    for (i = off; i < off + 36 && i < NCAP; i += 3) {
      v = 0;
      for (m = 0; m < 3; m++) {
        v <<= 8;
        if (i + m < NCAP) v |= (uint8_t)buf[j];
        if (++j == NCAP) j = 0;
      }
      *p++ = b64[(v >> 18) & 63];
      *p++ = b64[(v >> 12) & 63];
      *p++ = b64[(v >> 6) & 63];
      *p++ = b64[v & 63];
    }
    *p = 0;
    api->say(line);
  }
}

NOINLINE static void cap_line(const k6_api_t *api, const cap_t *c, int b, uint32_t k) {
  char line[96], *p;
  p = put(line, "c 1 ");
  p = dec(p, GAIN13 ? 1u : 0u);
  *p++ = ' ';
  p = dec(p, k);
  p = put(p, " ks=");
  p = dec(p, c->ks);
  p = put(p, " r=");
  p = sdec(p, c->r);
  p = put(p, " t=");
  p = dec(p, api->ticks() & 0xFFFF);
  p = put(p, " lost=");
  p = dec(p, c->lost);
  p = put(p, b ? " ch=B rmax=" : " ch=A rmax=");
  p = sdec(p, c->rmax);
  p = put(p, " trig=");
  p = dec(p, c->trig);
  p = put(p, " ms=");
  p = dec(p, c->end_ms);
  *p = 0;
  api->say(line);
}

/* the channel's floor: the lowest of 16 reads 3 ms apart, + 1 (the reads move +-1-2 dB) */
static int floor_of(const k6_api_t *api) {
  int i, r, lo = 0;
  for (i = 0; i < 16; i++) {
    r = rssi(api);
    if (!i || r < lo) lo = r;
    api->delay_us(3000);
  }
  return lo + 1;
}

int k6_main(const k6_api_t *api) {
  uint8_t *ring;
  int8_t *buf;
  cap_t c;
  uint32_t room, ahb, apb2, k = 0, nb = 0, turn_b, turn_n, turn_c, until;
  uint16_t r33, r34, r7e, r13;
  int fl, b = FIRST_B, quit = 0;
  char line[64], *p;
  if (api->abi != 1 || api->level < 2) return -3;
  if (api->key_down()) return -2;
  ring = (uint8_t *)(((uint32_t)api->scratch(&room) + 3) & ~3u);
  if (room < RSZ + NCAP + 4) return -8;
  buf = (int8_t *)(ring + RSZ);
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
  wr(RCC_APB2ENR, apb2 | TIM17EN);
  p = put(line, "acap start a=");
  p = dec(p, CH_A_HZ);
  p = put(p, " b=");
  p = dec(p, CH_B_HZ);
  p = put(p, " g13=");
  p = hex4(p, GAIN13);
  p = put(p, " ncap=");
  p = dec(p, NCAP);
  p = put(p, " pre=");
  p = dec(p, PRE);
  *p = 0;
  api->say(line);
  while (!quit && api->ms() < RUN_S * 1000u) {
    api->tune(b ? CH_B_HZ : CH_A_HZ, WIDE);
    api->fd_write(0x34, (uint16_t)((r34 & 0xFFF0) | 13));
    if (GAIN13) {
      api->fd_write(0x7E, (uint16_t)((r7e & 0x0FFF) | 0x8000 | (3u << 12)));
      api->fd_write(0x13, GAIN13);
    }
    api->delay_us(30000);
    fl = floor_of(api);
    turn_b = (uint32_t)b, turn_n = 0, turn_c = 0;
    until = api->ms() + SWITCH_S * 1000u;
    if (until > RUN_S * 1000u) until = RUN_S * 1000u;
    for (;;) {
      listen(api, ring, buf, fl + SQ_DB, until, &c);
      if (c.got < 0) quit = 1;
      if (c.got <= 0) break;
      turn_n++, nb++;
      if (k < MAXCAP) {
        cap_line(api, &c, b, k);
        dump(api, buf, c.head, k);
        k++, turn_c++;
      } else {
        p = put(line, b ? "b B r=" : "b A r=");
        p = sdec(p, c.rmax);
        p = put(p, " ms=");
        p = dec(p, c.end_ms);
        *p = 0;
        api->say(line);
      }
      /* a long carrier is not AIS: let it go (2 s at most) before listening again */
      while (rssi(api) >= fl + SQ_DB && api->ms() < c.end_ms + 2000u) api->delay_us(2000);
      if (api->ms() >= until) break;
    }
    p = put(line, turn_b ? "m B floor=" : "m A floor=");
    p = sdec(p, fl);
    p = put(p, " n=");
    p = dec(p, turn_n);
    p = put(p, " caps=");
    p = dec(p, turn_c);
    p = put(p, " ms=");
    p = dec(p, api->ms());
    *p = 0;
    api->say(line);
    b = !b;
  }
  stop();
  wr(TIM17 + 0x2C, 0xFFFF);
  wr(DMA_CNDTR1, 0);
  wr(DMA_CPAR1, 0);
  wr(DMA_CMAR1, 0);
  wr(RCC_APB2ENR, apb2);
  wr(RCC_AHBENR, ahb);
  api->fd_write(0x13, r13);
  api->fd_write(0x7E, r7e);
  api->fd_write(0x34, r34);
  api->fd_write(0x33, r33);
  p = put(line, "acap done n=");
  p = dec(p, nb);
  p = put(p, " caps=");
  p = dec(p, k);
  *p = 0;
  api->say(line);
  return (int)k;
}
