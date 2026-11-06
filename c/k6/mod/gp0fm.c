/* gp0fm: record the receiver's low IF from the FD6818's GP0 bitstream, decimated as it is
   sampled, so the host can FM-demodulate it (docs/k6-fd6818-audio.md).

   GP0 output code 13 (REG_34[3:0]) is a ~6.5 MHz 1-bit sigma-delta stream whose density follows
   the receive low IF: about 17 kHz on a bare carrier, swinging with the FM deviation (gp0tone,
   on air 2026-11-06). Here the MCU reads PB1 (GPIOB IDR, read only) 256 times per output in
   straight-line Thumb (ldr, lsrs, adcs into a counter, about 4 cycles a sample, 22.9 M samples/s
   measured by i2scap) and stores the count halved (0..128) as one byte: about 89 k outputs a
   second, 11.2 us each, so the scratch RAM holds 30-ish ms of IF.

   It waits for the upload's carrier to drop, times CAL captures on the empty channel for the
   output rate, waits up to 90 s for a carrier, waits SETTLE_MS, records NREC captures SPACE_MS
   apart and prints each as hex, 24 bytes a line:
     fm start floor=-103 bytes=2900 cal caps=10 t=3270      (output rate = 10*bytes/(t*100us))
     fm rec 0 rssi=-21 t=327
     x 0 0000 40424a5056...                                 (capture, byte offset, bytes)
   Writes only REG_34[3:0] and REG_33 bit 8 (the PR #68 image), both put back. No TX.
   Returns the bytes per capture, or -2 (a key), -3 (old image), -4 (PB1 not an input), -5
   (REG_33 refused), -6 (no carrier in 90 s).

   Built with GP0_RAW defined (gp0raw.c) it stores the raw samples instead, 32 to a word, first
   sample in bit 31, words little-endian: about 0.8 ms of the 22.9 M samples/s stream per capture,
   to find on the host how slowly the stream can be sampled (by DMA, say) and still give the IF.
   SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 2
#include "../k6_mod.h"

#define GPIOB 0x48000400u
#define SEL_REG 0x34
#define CODE 13
#define CAL 10
#define NREC 2
#define SETTLE_MS 400
#define SPACE_MS 1500
#define MAXB 3200
#define PERLINE 24

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

static char *sdec(char *p, int v) {
  if (v < 0) *p++ = '-', v = -v;
  return dec(p, (uint32_t)v);
}

static volatile const uint32_t *reg32(uint32_t a) { return (volatile const uint32_t *)a; }

static int rssi(const k6_api_t *api) { return KM_RSSI_DBM(api->fd_read(0x67)); }

#ifdef GP0_RAW
/* n bytes of raw samples (n a multiple of 4) */
NOINLINE static void capd(uint8_t *out, uint32_t n) {
  volatile const uint32_t *idr = reg32(GPIOB + 0x10);
  n /= 4;
  __asm__ volatile(
      "1:\n"
      ".rept 32\n"
      "ldr r3, [%[idr]]\n"
      "lsrs r3, r3, #2\n"
      "adcs r2, r2\n"
      ".endr\n"
      "stmia %[out]!, {r2}\n"
      "subs %[n], #1\n"
      "bne 1b\n"
      : [out] "+l"(out), [n] "+l"(n)
      : [idr] "l"(idr)
      : "r2", "r3", "cc", "memory");
}
#else
/* n outputs, each the number of high samples in 256, halved */
NOINLINE static void capd(uint8_t *out, uint32_t n) {
  volatile const uint32_t *idr = reg32(GPIOB + 0x10);
  uint32_t zero = 0;
  __asm__ volatile(
      "1:\n"
      "movs r2, #0\n"
      "movs r4, #8\n"
      "2:\n"
      ".rept 32\n"
      "ldr r3, [%[idr]]\n"
      "lsrs r3, r3, #2\n"
      "adcs r2, %[z]\n"
      ".endr\n"
      "subs r4, #1\n"
      "bne 2b\n"
      "lsrs r2, r2, #1\n"
      "strb r2, [%[out]]\n"
      "adds %[out], #1\n"
      "subs %[n], #1\n"
      "bne 1b\n"
      : [out] "+l"(out), [n] "+l"(n)
      : [idr] "l"(idr), [z] "l"(zero)
      : "r2", "r3", "r4", "cc", "memory");
}
#endif

static void say0(const k6_api_t *api, char *line, char *p) {
  *p = 0;
  api->say(line);
}

NOINLINE static void dump(const k6_api_t *api, unsigned k, const uint8_t *b, uint32_t n) {
  char line[64], *p;
  uint32_t i, j;
  for (i = 0; i < n; i += PERLINE) {
    p = put(line, "x ");
    p = dec(p, k);
    *p++ = ' ';
    p = hex(p, i, 4);
    *p++ = ' ';
    for (j = i; j < n && j < i + PERLINE; j++) p = hex(p, b[j], 2);
    say0(api, line, p);
  }
}

int k6_main(const k6_api_t *api) {
  uint8_t *buf;
  uint32_t room, n, t0, k;
  uint16_t r33, r34, s0, dt;
  int floor;
  char line[64], *p;
  if (api->level < 2) return -3;
  t0 = api->ms();
  while (rssi(api) > -80 && api->ms() - t0 < 30000) {
    if (api->key_down()) return -2;
    api->delay_us(10000);
  }
  api->delay_us(200000);
  floor = rssi(api);
  buf = (uint8_t *)api->scratch(&room);
  n = (room > MAXB ? MAXB : room) & ~3u;
  if (*reg32(GPIOB) & (3u << 2)) {
    api->say("fm PB1 is not an input: no writes");
    return -4;
  }
  r33 = api->fd_read(0x33);
  r34 = api->fd_read(SEL_REG);
  if ((r33 & 0x0100) && api->fd_write(0x33, (uint16_t)(r33 & ~0x0100)) != 0) {
    api->say("fm REG_33 refused: needs the PR #68 image");
    return -5;
  }
  api->fd_write(SEL_REG, (uint16_t)((r34 & 0xFFF0) | CODE));
  api->delay_us(2000);
  s0 = (uint16_t)api->ticks();
  for (k = 0; k < CAL; k++) capd(buf, n);
  dt = (uint16_t)((uint16_t)api->ticks() - s0);
  p = put(line, "fm start floor=");
  p = sdec(p, floor);
  p = put(p, " bytes=");
  p = dec(p, n);
  p = put(p, " cal caps=");
  p = dec(p, CAL);
  p = put(p, " t=");
  p = dec(p, dt);
  say0(api, line, p);
  api->say("fm waiting for a carrier");
  t0 = api->ms();
  while (rssi(api) < floor + 15) {
    if (api->key_down() || api->ms() - t0 > 90000) {
      api->fd_write(SEL_REG, r34);
      api->fd_write(0x33, r33);
      api->say("fm no carrier");
      return api->key_down() ? -2 : -6;
    }
    api->delay_us(5000);
  }
  api->delay_us(SETTLE_MS * 1000u);
  for (k = 0; k < NREC; k++) {
    int r = rssi(api);
    if (k) api->delay_us(SPACE_MS * 1000u);
    s0 = (uint16_t)api->ticks();
    capd(buf, n);
    dt = (uint16_t)((uint16_t)api->ticks() - s0);
    p = put(line, "fm rec ");
    p = dec(p, k);
    p = put(p, " rssi=");
    p = sdec(p, r);
    p = put(p, " t=");
    p = dec(p, dt);
    say0(api, line, p);
    dump(api, k, buf, n);
    if (api->key_down()) break;
  }
  api->fd_write(SEL_REG, r34);
  api->fd_write(0x33, r33);
  api->say("fm done");
  return api->key_down() ? -2 : (int)n;
}
