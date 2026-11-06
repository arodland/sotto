/* i2scap: a fast look at what the FD6818 puts on GP0 (MCU PB1) for its fast output selects,
   after i2sprobe found codes 12, 13, 14 (the guide's I2S DOUT, DFS, DCLK) and 15 (XTAL/2)
   all toggling faster than its ~5 M samples/s loop could resolve (docs/k6-fd6818-audio.md).

   Capture: PB1 read from GPIOB IDR (0x48000410, read only) 32 times per word in straight-line
   Thumb (ldr, lsrs, adcs: 4 cycles a sample if the GPIO load takes 2, about 24 M samples/s),
   a store and the loop branch after every 32 (a few cycles more, once per word), into the
   module's scratch RAM; interrupts stay on (a timer tick shows as one long run). Then, per
   code, run lengths in samples: a histogram of high runs and of low runs (1..31, and 32 for
   longer), the longest of each, and the first runs as captured. A clock shows one run length;
   a frame pulse a short high run among long low ones (or the reverse); data a spread.

   Codes 9 and 11 (reserved in the guide) gave slow edges: 1 s windows with the slower
   counter, and the first 32 gaps between edges in its samples.

   Steps: carrier wait (the upload's repeat copies), GP0's output on (REG_33 bit 8 clear: an
   image from PR #68 on), the sample rate (sel 0, 100 ms of captures against the 100 us ticks),
   captures for 12, 13, 14, 15 and 13 again with REG_31 bit 0 set, the slow windows, then
   REG_34, REG_31, REG_33 put back. Writes only 0x34[3:0], 0x31 bit 0, 0x33 bit 8. No TX.
   Returns the number of words captured per code, or -2 (a key), -3 (old image), -4 (PB1 not
   an input), -5 (REG_33 refused: an image before PR #68), -6 (scratch too small).
   SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 2
#include "../k6_mod.h"

#define GPIOB 0x48000400u
#define PB1 2u
#define NOISE_MAX_DBM (-80)
#define SEL_REG 0x34
#define MAXW 512            /* words per capture: 16384 samples, about 0.7 ms */
#define NRAW 24
#define NE 33

#define NOINLINE __attribute__((noinline))

static char *put(char *p, const char *s) {
  while (*s) *p++ = *s++;
  return p;
}

static char *hex(char *p, uint32_t v, int digits) {
  const char *x = "0123456789abcdef";
  while (digits--) *p++ = x[(v >> (4 * digits)) & 15];
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

static volatile const uint32_t *reg32(uint32_t a) { return (volatile const uint32_t *)a; }

/* words x 32 samples of PB1, first sample in bit 31 of buf[0] */
NOINLINE static void cap(uint32_t *buf, uint32_t words) {
  volatile const uint32_t *idr = reg32(GPIOB + 0x10);
  __asm__ volatile(
      "1:\n"
      ".rept 32\n"
      "ldr r3, [%[idr]]\n"
      "lsrs r3, r3, #2\n"
      "adcs r2, r2\n"
      ".endr\n"
      "stmia %[buf]!, {r2}\n"
      "subs %[n], #1\n"
      "bne 1b\n"
      : [buf] "+l"(buf), [n] "+l"(words)
      : [idr] "l"(idr)
      : "r2", "r3", "cc", "memory");
}

static void say_line(const k6_api_t *api, char *line, char *p) {
  *p = 0;
  api->say(line);
}

/* one histogram line set: "<tag> len:count ..." for the nonzero bins */
NOINLINE static void hist(const k6_api_t *api, const char *tag, const uint16_t *h) {
  char line[64], *p = put(line, tag);
  int i, any = 0;
  for (i = 1; i <= 32; i++) {
    if (!h[i]) continue;
    *p++ = ' ';
    p = dec(p, (uint32_t)i);
    *p++ = ':';
    p = dec(p, h[i]);
    any = 1;
    if (p - line > 48) {
      say_line(api, line, p);
      p = put(line, tag);
      any = 0;
    }
  }
  if (any) say_line(api, line, p);
}

typedef struct {
  uint16_t hi[33], lo[33];
  uint16_t raw[NRAW];
  uint32_t runs, ones, maxhi, maxlo;
  uint8_t first, nraw;
} runs_t;

NOINLINE static void analyse(const k6_api_t *api, const char *tag, const uint32_t *buf,
                             uint32_t words) {
  runs_t r;
  char line[64], *p;
  uint32_t w, b, cur, len = 0, i, k = 0;
  for (i = 0; i < 33; i++) r.hi[i] = r.lo[i] = 0;
  r.runs = r.ones = r.maxhi = r.maxlo = 0;
  r.nraw = 0;
  cur = buf[0] >> 31;
  r.first = (uint8_t)cur;
  for (w = 0; w < words; w++) {
    for (b = 0; b < 32; b++) {
      uint32_t v = (buf[w] >> (31 - b)) & 1;
      r.ones += v;
      if (v == cur) {
        len++;
        continue;
      }
      /* a run ended; the first one is cut by the capture's start, so not in the histograms */
      if (k) {
        uint16_t *h = cur ? r.hi : r.lo;
        h[len > 32 ? 32 : len]++;
        if (cur && len > r.maxhi) r.maxhi = len;
        if (!cur && len > r.maxlo) r.maxlo = len;
      }
      if (r.nraw < NRAW) r.raw[r.nraw++] = (uint16_t)(len > 65535 ? 65535 : len);
      k++;
      cur = v;
      len = 1;
    }
  }
  r.runs = k;
  p = put(line, "cap ");
  p = put(p, tag);
  p = put(p, " runs=");
  p = dec(p, r.runs);
  p = put(p, " ones=");
  p = dec(p, r.ones);
  p = put(p, " max=");
  p = dec(p, r.maxhi);
  *p++ = '/';
  p = dec(p, r.maxlo);
  say_line(api, line, p);
  hist(api, " hi", r.hi);
  hist(api, " lo", r.lo);
  p = put(line, " raw");
  for (i = 0; i < r.nraw; i++) {
    *p++ = ' ';
    *p++ = ((r.first ^ i) & 1) ? '+' : '-';
    p = dec(p, r.raw[i]);
    if (p - line > 50) {
      say_line(api, line, p);
      p = put(line, " raw");
    }
  }
  if (p - line > 4) say_line(api, line, p);
}

/* the slow counter: edges in t ticks, the first gaps in samples */
NOINLINE static void count(const k6_api_t *api, const char *tag, uint32_t ticks) {
  volatile const uint32_t *idr = reg32(GPIOB + 0x10);
  uint32_t at[NE];
  uint32_t last = *idr & PB1, n = 0, e = 0, h = 0, i, v;
  uint16_t t0 = (uint16_t)api->ticks(), dt;
  char line[64], *p;
  for (;;) {
    for (i = 0; i < 256; i++) {
      v = *idr & PB1;
      h += v;
      if (v != last) {
        last = v;
        if (e < NE) at[e] = n + i;
        e++;
      }
    }
    n += 256;
    dt = (uint16_t)((uint16_t)api->ticks() - t0);
    if (dt >= ticks) break;
  }
  p = put(line, "slow ");
  p = put(p, tag);
  p = put(p, " e=");
  p = dec(p, e);
  p = put(p, " n=");
  p = dec(p, n);
  p = put(p, " h=");
  p = dec(p, h >> 1);
  p = put(p, " t=");
  p = dec(p, dt);
  say_line(api, line, p);
  p = put(line, "d");
  for (i = 1; i < e && i < NE; i++) {
    *p++ = ' ';
    p = dec(p, at[i] - at[i - 1]);
    if (p - line > 50 || i == e - 1 || i == NE - 1) {
      say_line(api, line, p);
      p = put(line, "d");
    }
  }
}

static void sel(const k6_api_t *api, uint16_t r34, unsigned s) {
  api->fd_write(SEL_REG, (uint16_t)((r34 & 0xFFF0) | s));
  api->delay_us(2000);
}

int k6_main(const k6_api_t *api) {
  static const uint8_t fast[] = {12, 13, 14, 15};
  uint32_t *buf, room, words, t0, k;
  uint16_t r33, r34, r31, dt, s0;
  unsigned i;
  char line[64], *p, tag[8];
  if (api->level < 2) return -3;
  t0 = api->ms();
  while (KM_RSSI_DBM(api->fd_read(0x67)) > NOISE_MAX_DBM && api->ms() - t0 < 30000) {
    if (api->key_down()) return -2;
    api->delay_us(10000);
  }
  api->delay_us(200000);
  buf = (uint32_t *)api->scratch(&room);
  words = room / 4;
  if (words > MAXW) words = MAXW;
  if (words < 64) return -6;
  if (*reg32(GPIOB) & (3u << 2)) {
    api->say("cap PB1 is not an input: no writes");
    return -4;
  }
  r33 = api->fd_read(0x33);
  r34 = api->fd_read(SEL_REG);
  r31 = api->fd_read(0x31);
  if ((r33 & 0x0100) && api->fd_write(0x33, (uint16_t)(r33 & ~0x0100)) != 0) {
    api->say("cap REG_33 refused: needs the PR #68 image");
    return -5;
  }
  /* the sample rate: captures of a still line for 100 ms */
  sel(api, r34, 0);
  k = 0;
  s0 = (uint16_t)api->ticks();
  do {
    cap(buf, words);
    k++;
    dt = (uint16_t)((uint16_t)api->ticks() - s0);
  } while (dt < 1000);
  p = put(line, "cap start 33=");
  p = hex(p, r33, 4);
  p = put(p, " words=");
  p = dec(p, words);
  p = put(p, " caps=");
  p = dec(p, k);
  p = put(p, " t=");
  p = dec(p, dt);
  say_line(api, line, p);
  for (i = 0; i <= sizeof fast; i++) {
    unsigned s = i < sizeof fast ? fast[i] : 13;
    if (i == sizeof fast) api->fd_write(0x31, (uint16_t)(r31 | 1));
    sel(api, r34, s);
    cap(buf, words);
    tag[0] = (char)('0' + s / 10), tag[1] = (char)('0' + s % 10), tag[2] = 0;
    if (i == sizeof fast) tag[2] = '+', tag[3] = 0;
    analyse(api, tag, buf, words);
    if (api->key_down()) break;
  }
  api->fd_write(0x31, r31);
  if (!api->key_down()) {
    sel(api, r34, 9);
    count(api, "09", 10000);
    sel(api, r34, 11);
    count(api, "11", 10000);
  }
  api->fd_write(SEL_REG, r34);
  api->fd_write(0x33, r33);
  p = put(line, "cap done 34=");
  p = hex(p, api->fd_read(SEL_REG), 4);
  p = put(p, " 31=");
  p = hex(p, api->fd_read(0x31), 4);
  p = put(p, " 33=");
  p = hex(p, api->fd_read(0x33), 4);
  say_line(api, line, p);
  return api->key_down() ? -2 : (int)words;
}
