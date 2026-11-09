/* aispoll.h: the polled AIS capture shared by aiscap.c (records bursts for the host) and
   aisclk.c (decodes them on the radio): the IF tap on PB1 read 256 times a count in straight-line
   Thumb (~22.9 M samples/s, ~81 kHz of counts), a band-pass energy trigger over the floor, a ring
   of NCAP counts that keeps PRE of them from before the trigger, TIM17's cycle counts for the
   count rate, and the module's own 100 us clock. aiscap.c's header has the details.

   The including module defines K6_MODULE_LEVEL and includes k6_mod.h first (and aisrx.h, if
   it uses it, whose ar_udiv then serves here). AISCAP_EMU: a count is one read of 0x480004F0
   (the emulator's poll, tests/k6_live_emu.py). SPDX-License-Identifier: MIT */
#ifndef AISPOLL_H
#define AISPOLL_H

#ifndef NCAP
#define NCAP 3600u /* ~40 ms, 100 y lines */
#endif
#ifndef PRE
#define PRE 1024u /* ~11 ms kept from before the trigger */
#endif
#ifndef NCAL
#define NCAL 32768u /* ~0.37 s, 32 floor blocks; calibrate() divides by it as >> 11 x 16 */
#endif
#ifndef TRIG_X
#define TRIG_X 3 /* energy over the floor for a burst (4.8 dB) */
#endif
#ifndef GAIN13
#define GAIN13 0x03FFu
#endif
#ifndef WIDE
#define WIDE 1
#endif

/* the band-pass: butter(1, [11000, 23000], 'band', fs=89500), Q14 */
#define BP_B0 5069
#define BP_A1 (-9136)
#define BP_A2 6245

#define GPIOB 0x48000400u
#define RCC_APB2ENR 0x40021018u
#define TIM17 0x40014800u
#define TIM17EN (1u << 18)

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

static char *hex4(char *p, uint32_t v) {
  int s;
  for (s = 12; s >= 0; s -= 4) {
    uint32_t d = (v >> s) & 15;
    *p++ = (char)(d < 10 ? '0' + d : 'a' + d - 10);
  }
  return p;
}

#ifdef AISRX_H
#define udiv ar_udiv
#else
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
#endif

#ifdef AISCAP_EMU
static uint32_t count256(void) { return rd(0x480004F0u); }
#else
/* the number of high samples on PB1 in 256 reads of GPIOB IDR (gp0fm.c's loop) */
NOINLINE static uint32_t count256(void) {
  volatile const uint32_t *idr = (volatile const uint32_t *)(GPIOB + 0x10);
  uint32_t zero = 0, c;
  __asm__ volatile(
      "movs %[c], #0\n"
      "movs r4, #8\n"
      "1:\n"
      ".rept 32\n"
      "ldr r3, [%[idr]]\n"
      "lsrs r3, r3, #2\n"
      "adcs %[c], %[z]\n"
      ".endr\n"
      "subs r4, #1\n"
      "bne 1b\n"
      : [c] "=&l"(c)
      : [idr] "l"(idr), [z] "l"(zero)
      : "r3", "r4", "cc");
  return c;
}
#endif

typedef struct {
  int32_t x1, x2, y1, y2, e;
} bp_t;

/* one count through the band-pass into the leaky energy (x ones - 128) */
static int32_t energy(bp_t *b, int32_t x) {
#ifdef RAW_TRIG /* the whole band the counts carry, not just around the IF (aiscap_sweep) */
  int32_t y = x;
#else
  int32_t y = (BP_B0 * (x - b->x2) - BP_A1 * b->y1 - BP_A2 * b->y2) >> 14;
#endif
  b->x2 = b->x1, b->x1 = x, b->y2 = b->y1, b->y1 = y;
  b->e += y * y - (b->e >> 6);
  return b->e;
}

static int8_t clamp8(int32_t x) { return (int8_t)(x > 127 ? 127 : x); }

/* the module's own clock in 100 us ticks (api->ticks wraps every 6.5 s; listen reads it every
   ~50 ms) */
typedef struct {
  uint32_t last, t;
} ap_ck_t;

static uint32_t ap_now(const k6_api_t *api, ap_ck_t *k) {
  uint32_t n = api->ticks() & 0xFFFF;
  k->t += (n - k->last) & 0xFFFF;
  k->last = n;
  return k->t;
}

typedef struct {
  uint32_t head, end_ms, end_t, c0, c1;
  int32_t emax;
  int got; /* 1 a burst recorded, 0 the turn ended, -1 a key */
} cap_t;

#define TIM17_CNT (TIM17 + 0x24)

/* count into the ring until a burst has been recorded (or the turn's end, or a key). TIM17
   counts the CPU's cycles: c0 is the cycles of the last 1024 counts before the trigger, c1 of
   the NCAP - PRE after it (the two paths differ by a few cycles a count, so the host puts the
   counts on one time grid) */
NOINLINE static void listen(const k6_api_t *api, ap_ck_t *ck, int8_t *buf, bp_t *b, int32_t thr,
                            uint32_t until_ms, cap_t *c) {
  uint32_t head = 0, n = 0, post = 0, t, tl, cyc = 0;
  int32_t x, e;
  c->got = 0, c->emax = 0, c->c0 = 0, c->c1 = 0;
  tl = rd(TIM17_CNT);
  for (;;) {
    x = (int32_t)count256() - 128;
    t = rd(TIM17_CNT);
    cyc += (t - tl) & 0xFFFF;
    tl = t;
    buf[head] = clamp8(x);
    if (++head == NCAP) head = 0;
    e = energy(b, x);
    n++;
    if (post) {
      if (e > c->emax) c->emax = e;
      if (!--post) break;
      continue;
    }
    if (!(n & 1023)) c->c0 = cyc, cyc = 0;
    if (e > thr && n > PRE + 1024) {
      post = NCAP - PRE, c->emax = e, cyc = 0;
      continue;
    }
    if (!(n & 4095)) {
      ap_now(api, ck);
      if (api->ms() >= until_ms) break;
      if (!(n & 32767) && api->key_down()) {
        c->got = -1;
        break;
      }
      tl = rd(TIM17_CNT); /* not the call's time */
    }
  }
  c->end_t = ap_now(api, ck); /* the last count's end, to a tick */
  c->head = head; /* the oldest count */
  c->c1 = cyc;
  c->end_ms = api->ms();
  if (post == 0 && c->emax) c->got = 1;
}

/* NCAL counts on the empty channel: the count rate (Hz) and the floor (the quietest 1024-count
   block's mean energy) */
NOINLINE static uint32_t calibrate(bp_t *b, int32_t *floor) {
  uint32_t i, t, tl, cyc = 0;
  int32_t sum = 0, lo = 0x7FFFFFFF;
  for (i = 0; i < 256; i++) energy(b, (int32_t)count256() - 128); /* the filter settles */
  tl = rd(TIM17_CNT);
  for (i = 1; i <= NCAL; i++) {
    sum += energy(b, (int32_t)count256() - 128) >> 4;
    t = rd(TIM17_CNT);
    cyc += (t - tl) & 0xFFFF;
    tl = t;
    if (!(i & 1023)) {
      if (sum < lo) lo = sum;
      sum = 0;
    }
  }
  *floor = (lo >> 6) + 1; /* sum of e / 16 over 1024 counts / 64: the block's mean e */
  /* hz = 96 MHz / (cyc / NCAL): 16 x the cycles a count over 16 x 96 MHz */
  return udiv(1536000000u, cyc >> 11);
}

/* dB of e over fl (0..40) */
static int db_over(int32_t e, int32_t fl) {
  /* 10^(d/10) in Q4 for d = 0..40, steps of 1 dB */
  static const uint32_t P[41] = {16,    20,    25,    32,    40,    51,    64,    80,    101,
                                 127,   160,   201,   254,   319,   402,   506,   637,   802,
                                 1010,  1271,  1600,  2014,  2536,  3193,  4019,  5060,  6370,
                                 8020,  10096, 12710, 16000, 20143, 25358, 31924, 40190, 50596,
                                 63696, 80188, 100950, 127088, 160000};
  int d = 0;
  uint32_t ue = (uint32_t)e << 4;
  while (d < 40 && (uint32_t)fl * P[d + 1] <= ue) d++;
  return d;
}

#endif
