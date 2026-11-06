/* gp0tone: do the FD6818's GP0 output codes 11 and 9 (reserved in the guide) carry the sign of
   the received audio? (docs/k6-fd6818-audio.md)

   On an empty channel i2scap found codes 11 and 9 changing on a 26 MHz / 1024 = 25.39 kHz grid
   (every gap a whole number of 39.4 us steps), about 900 and 200 edges a second, and codes
   12-14 a fast bitstream (runs of 1-3 units of about 160 ns, 50% ones). If 11 or 9 is a hard
   limited audio signal, a received tone of f Hz makes it toggle 2f times a second with gaps
   alternating near 12.7 steps (1 kHz); if the bitstream is a sigma-delta of the audio, its
   density follows the tone.

   The module waits for the upload's carrier to drop, runs 2 cycles on the empty channel, waits
   up to 90 s for a carrier (RSSI 15 dB over the floor) and runs cycles until it drops (at most
   MAXC). A cycle: code 11 for 250 ms, code 9 for 250 ms (edges, the first gaps in samples of
   the edge counter, about 7.3 M samples/s), then code 13 for one 22.9 M samples/s burst of
   375 words reported as ones per 8 words (256 samples, 11.2 us: 47 numbers 0-256). Lines:
     c N rssi=-50 11 e=500 n= t= g 286 572 ...      per code window
     c N 13 d 128 130 ...                          the density
   Writes only REG_34[3:0], REG_33 bit 8 (needs the PR #68 image), all put back. No TX.
   Returns the cycles run, or -2 (a key), -3 (old image), -4 (PB1 not an input), -5 (REG_33
   refused).
   SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 2
#include "../k6_mod.h"

#define GPIOB 0x48000400u
#define PB1 2u
#define SEL_REG 0x34
#define WIN_T 2500          /* 250 ms */
#define NG 10               /* gaps printed per window */
#define MAXC 40
#define CAPW 375

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

static volatile const uint32_t *reg32(uint32_t a) { return (volatile const uint32_t *)a; }

static int rssi(const k6_api_t *api) { return KM_RSSI_DBM(api->fd_read(0x67)); }

static void sel(const k6_api_t *api, uint16_t r34, unsigned s) {
  api->fd_write(SEL_REG, (uint16_t)((r34 & 0xFFF0) | s));
  api->delay_us(1000);
}

static char *head(char *p, unsigned c) {
  p = put(p, "c ");
  return dec(p, c);
}

NOINLINE static void window(const k6_api_t *api, unsigned c, const char *code) {
  volatile const uint32_t *idr = reg32(GPIOB + 0x10);
  uint32_t at[NG + 1];
  uint32_t last = *idr & PB1, n = 0, e = 0, i, v;
  uint16_t t0 = (uint16_t)api->ticks(), dt;
  char line[64], *p;
  int r = rssi(api);
  for (;;) {
    for (i = 0; i < 256; i++) {
      v = *idr & PB1;
      if (v != last) {
        last = v;
        if (e <= NG) at[e] = n + i;
        e++;
      }
    }
    n += 256;
    dt = (uint16_t)((uint16_t)api->ticks() - t0);
    if (dt >= WIN_T) break;
  }
  p = head(line, c);
  p = put(p, " rssi=");
  p = sdec(p, r);
  *p++ = ' ';
  p = put(p, code);
  p = put(p, " e=");
  p = dec(p, e);
  p = put(p, " n=");
  p = dec(p, n);
  p = put(p, " t=");
  p = dec(p, dt);
  *p = 0;
  api->say(line);
  if (e < 2) return;
  p = head(line, c);
  *p++ = ' ';
  p = put(p, code);
  p = put(p, " g");
  for (i = 1; i < e && i <= NG; i++) {
    *p++ = ' ';
    p = dec(p, at[i] - at[i - 1]);
  }
  *p = 0;
  api->say(line);
}

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

static uint32_t ones(uint32_t x) {
  x = x - ((x >> 1) & 0x55555555u);
  x = (x & 0x33333333u) + ((x >> 2) & 0x33333333u);
  x = (x + (x >> 4)) & 0x0F0F0F0Fu;
  return (x * 0x01010101u) >> 24;
}

NOINLINE static void density(const k6_api_t *api, unsigned c, uint32_t *buf, uint32_t words) {
  char line[64], *p;
  uint32_t w, s;
  cap(buf, words);
  p = head(line, c);
  p = put(p, " 13 d");
  for (w = 0; w + 8 <= words; w += 8) {
    uint32_t k;
    for (s = 0, k = 0; k < 8; k++) s += ones(buf[w + k]);
    *p++ = ' ';
    p = dec(p, s);
    if (p - line > 52) {
      *p = 0;
      api->say(line);
      p = head(line, c);
      p = put(p, " 13 d");
    }
  }
  if (p[-1] != 'd') {
    *p = 0;
    api->say(line);
  }
}

NOINLINE static void cycle(const k6_api_t *api, unsigned c, uint16_t r34, uint32_t *buf,
                           uint32_t words) {
  sel(api, r34, 11);
  window(api, c, "11");
  sel(api, r34, 9);
  window(api, c, "09");
  sel(api, r34, 13);
  density(api, c, buf, words);
}

int k6_main(const k6_api_t *api) {
  uint32_t *buf, room, words, t0;
  uint16_t r33, r34;
  unsigned c = 0;
  int floor, ret;
  char line[64], *p;
  if (api->level < 2) return -3;
  t0 = api->ms();
  while (rssi(api) > -80 && api->ms() - t0 < 30000) {
    if (api->key_down()) return -2;
    api->delay_us(10000);
  }
  api->delay_us(200000);
  floor = rssi(api);
  buf = (uint32_t *)api->scratch(&room);
  words = room / 4;
  if (words > CAPW) words = CAPW;
  if (*reg32(GPIOB) & (3u << 2)) {
    api->say("tone PB1 is not an input: no writes");
    return -4;
  }
  r33 = api->fd_read(0x33);
  r34 = api->fd_read(SEL_REG);
  if ((r33 & 0x0100) && api->fd_write(0x33, (uint16_t)(r33 & ~0x0100)) != 0) {
    api->say("tone REG_33 refused: needs the PR #68 image");
    return -5;
  }
  p = put(line, "tone start floor=");
  p = sdec(p, floor);
  p = put(p, " words=");
  p = dec(p, words);
  *p = 0;
  api->say(line);
  for (; c < 2; c++) cycle(api, c, r34, buf, words);
  api->say("tone waiting for a carrier");
  t0 = api->ms();
  ret = (int)c;
  while (rssi(api) < floor + 15) {
    if (api->key_down() || api->ms() - t0 > 90000) {
      ret = api->key_down() ? -2 : (int)c;
      api->say("tone no carrier");
      goto out;
    }
    api->delay_us(5000);
  }
  while (c < MAXC && rssi(api) >= floor + 15) {
    if (api->key_down()) {
      ret = -2;
      goto out;
    }
    cycle(api, c++, r34, buf, words);
  }
  ret = (int)c;
out:
  api->fd_write(SEL_REG, r34);
  api->fd_write(0x33, r33);
  p = put(line, "tone done cycles=");
  p = dec(p, c);
  *p = 0;
  api->say(line);
  return ret;
}
