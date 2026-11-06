/* i2sprobe: does Jeff's FD6818 (ID 0x4819) have the guide's MCBSP/I2S port, and can its pins
   reach the MCU? (docs/k6-fd6818-audio.md)

   The only FD6818 GPIO wired to the MCU is GP0: R175 (100 ohm), net CTCSS, MCU pin 19, which is
   PB1 (pin 18 is PB0, keypad row L0). Aura leaves PB1 an input; on the radio it has its
   pull-up on (PUPDR 01), so it reads high while GP0 doesn't drive it.
   This module reads PB1 straight from the MCU's GPIOB input register (0x48000410, read only:
   it writes no MCU register) and counts its edges in 100 ms windows while it changes what GP0
   outputs:

     1  a register report: the GPIO and I2S candidates, then every register 0x00-0x7F
        except 0x5F (a read there pops the FSK FIFO)
     2  base: PB1 as the radio left it
     3  sweep: GP0's output select. On the 0x4819 the GPIO block looks moved by 0x0E from the
        guide's numbering (guide REG_25H gpio_oen_b/out_val = REG_33 here, which Aura uses
        that way; guide REG_28H PA = REG_36 here, Aura's note), so the guide's REG_26H
        (gpio3..0_out_sel, reset 0x0005) would be REG_34 and REG_27H would be REG_35. Each
        of the 16 values of REG_34[3:0] in turn; guide codes 1 INT, 2 SQ, 3 VOX, 5 CTCSS code,
        12 I2S DOUT, 13 DFS (frame), 14 DCLK, 15 XTAL/2 (13 MHz: a flood of edges)
     4  i2s: with GP0 on DFS, DCLK and DOUT, the MCBSP enable candidates: none, REG_31 bit 0
        (the guide's REG_04H[0] mcbsp_en, if REG_04H is REG_31 here as REG_03H is REG_30) and
        REG_04 bit 0 (the guide's own address). The guide's reset values for its REG_75H/76H
        are already master mode, 32-bit frames, FM-demodulated data, so only the enable and
        the pin are tried. A frame clock near 8 kHz is about 1,600 edges per window.

   Every write is to a register API level 2 allows (0x34, 0x31, 0x04), only the bits named,
   and each is put back before the next step and at the end. GP0's output is off on Aura
   (REG_33 = 0xd300 on the radio: only GP2, GP3 and GP5 enabled), so the module also clears
   REG_33 bit 8, GP0's enable-bar. Level 2 refuses REG_33, except in images from 2026-11-06
   06:30 UTC on, which let a module change bits 8 and 0 (GP0's) and nothing else there, and
   put REG_33 back when the module returns. It writes nothing unless PB1 is an input.
   No TX: level 2 can't.

   On air 2026-11-06 06:02 UTC (image 6473ca7): REG_33 d300, REG_34/35 0000, REG_75 d42d,
   REG_76 f50b (not the guide's 0x0FB4/0xE307: the guide's I2S set-up isn't there on this
   chip), REG_31 and REG_04 0000; PB1 high in every sample; ret -5 (the old gate).

   Lines (K6 MOD say ...), per window: e edges, n samples of PB1, h samples high, t the
   window in 100 us ticks (n/t = samples per tick; edges per second = e * 10000 / t), then
   "d" lines with the first gaps between edges, in samples (the raw capture).
   Returns the most edges seen in a DFS window, or -2 (a key), -3 (old image), -4 (PB1 not an
   input), -5 (GP0's output is off and this image won't let a module turn it on).
   Build: python -m modem.k6 mod i2s.wav c/k6/mod/i2sprobe.c --key-file KEY --phy F --payload XL
   SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 2
#include "../k6_mod.h"

#define GPIOB 0x48000400u
#define PB1 2u
#define WIN_T 1000          /* 100 ms */
#define NE 33               /* edge times kept per window: 32 gaps */
#define NOISE_MAX_DBM (-80)     /* the floor reads about -105 here, the IC-7100 next door -5 */
#define SEL_REG 0x34

#define NOINLINE __attribute__((noinline))

typedef struct {
  uint32_t e, n, h, t;
  uint32_t at[NE];
} win_t;

static char *put(char *p, const char *s) {
  while (*s) *p++ = *s++;
  return p;
}

static char *hex(char *p, uint32_t v, int digits) {
  const char *x = "0123456789abcdef";
  while (digits--) *p++ = x[(v >> (4 * digits)) & 15];
  return p;
}

/* unsigned decimal without a divide */
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

/* count PB1's edges for WIN_T ticks */
NOINLINE static void count(const k6_api_t *api, win_t *w) {
  volatile const uint32_t *idr = reg32(GPIOB + 0x10);
  uint32_t last = *idr & PB1, n = 0, e = 0, h = 0, i, v;
  uint16_t t0 = (uint16_t)api->ticks(), dt;
  for (;;) {
    for (i = 0; i < 256; i++) {
      v = *idr & PB1;
      h += v;
      if (v != last) {
        last = v;
        if (e < NE) w->at[e] = n + i;
        e++;
      }
    }
    n += 256;
    dt = (uint16_t)((uint16_t)api->ticks() - t0);
    if (dt >= WIN_T) break;
  }
  w->e = e, w->n = n, w->h = h >> 1, w->t = dt;
}

NOINLINE static void report(const k6_api_t *api, const char *tag, uint16_t sel, win_t *w,
                            int gaps) {
  char line[64], *p = put(line, tag);
  uint32_t i, k;
  p = put(p, " 34=");
  p = hex(p, sel, 4);
  p = put(p, " e=");
  p = dec(p, w->e);
  p = put(p, " n=");
  p = dec(p, w->n);
  p = put(p, " h=");
  p = dec(p, w->h);
  p = put(p, " t=");
  p = dec(p, w->t);
  *p = 0;
  api->say(line);
  if (!gaps || w->e < 2) return;
  k = w->e < NE ? w->e : NE;
  p = put(line, "d");
  for (i = 1; i < k; i++) {
    *p++ = ' ';
    p = dec(p, w->at[i] - w->at[i - 1]);
    if (p - line > 50 || i == k - 1) {
      *p = 0;
      api->say(line);
      p = put(line, "d");
    }
  }
}

NOINLINE static void regs(const k6_api_t *api) {
  static const uint8_t focus[] = {0x31, 0x33, 0x34, 0x35, 0x04, 0x24, 0x25, 0x26, 0x27, 0x28};
  char line[64], *p;
  unsigned i, r;
  for (i = 0; i < sizeof focus; i++) {
    if (i % 5 == 0) p = put(line, "i2s");
    *p++ = ' ';
    p = hex(p, focus[i], 2);
    *p++ = '=';
    p = hex(p, api->fd_read(focus[i]), 4);
    if (i % 5 == 4) *p = 0, api->say(line);
  }
  p = put(line, "pb moder=");
  p = hex(p, *reg32(GPIOB + 0x00), 8);
  p = put(p, " pupdr=");
  p = hex(p, *reg32(GPIOB + 0x0C), 8);
  p = put(p, " idr=");
  p = hex(p, *reg32(GPIOB + 0x10), 4);
  *p = 0;
  api->say(line);
  for (r = 0; r < 0x80; r += 8) {
    p = put(line, "r");
    p = hex(p, r, 2);
    for (i = 0; i < 8; i++) {
      *p++ = ' ';
      if (r + i == 0x5F) p = put(p, "----");
      else p = hex(p, api->fd_read((uint8_t)(r + i)), 4);
    }
    *p = 0;
    api->say(line);
  }
}

/* GP0 on sel, settle, count, report; 1 if a key is down */
static int one(const k6_api_t *api, const char *tag, uint16_t base34, unsigned sel, win_t *w,
               int gaps) {
  uint16_t v = (uint16_t)((base34 & 0xFFF0) | sel);
  api->fd_write(SEL_REG, v);
  api->delay_us(2000);
  count(api, w);
  report(api, tag, api->fd_read(SEL_REG), w, gaps);
  return api->key_down();
}

int k6_main(const k6_api_t *api) {
  static const uint8_t en_reg[] = {0, 0x31, 0x04};
  static const uint8_t i2s_sel[] = {13, 14, 12};
  win_t w;
  uint16_t r34, r33, orig;
  uint32_t moder, t0, best = 0;
  unsigned s, k;
  char line[64], *p;
  if (api->level < 2) return -3;
  /* the sender's carrier may still be up, repeating the module's chunks: let it go (SQ and
     INT on GP0 read cleaner without it; the I2S clocks don't care) */
  t0 = api->ms();
  while (KM_RSSI_DBM(api->fd_read(0x67)) > NOISE_MAX_DBM && api->ms() - t0 < 30000) {
    if (api->key_down()) return -2;
    api->delay_us(10000);
  }
  api->delay_us(200000);
  p = put(line, "i2s start rssi=");
  {
    int d = KM_RSSI_DBM(api->fd_read(0x67));
    if (d < 0) *p++ = '-', d = -d;
    p = dec(p, (uint32_t)d);
  }
  p = put(p, " wait=");
  p = dec(p, api->ms() - t0);
  *p = 0;
  api->say(line);
  regs(api);
  r34 = api->fd_read(SEL_REG);
  r33 = api->fd_read(0x33);
  count(api, &w);
  report(api, "base", r34, &w, 1);
  moder = *reg32(GPIOB);
  if (moder & (3u << 2)) {
    api->say("i2s PB1 is not an input: no writes");
    return -4;
  }
  if ((r33 & 0x0100) && api->fd_write(0x33, (uint16_t)(r33 & ~0x0100)) != 0) {
    api->say("i2s GP0 output disabled (REG_33 bit 8): no writes");
    return -5;
  }
  p = put(line, "i2s 33 ");
  p = hex(p, r33, 4);
  p = put(p, " -> ");
  p = hex(p, api->fd_read(0x33), 4);
  *p = 0;
  api->say(line);
  for (s = 0; s < 16; s++) {
    char tag[8];
    tag[0] = 's', tag[1] = 'e', tag[2] = 'l', tag[3] = ' ';
    tag[4] = (char)(s < 10 ? '0' + s : 'a' + s - 10);
    tag[5] = 0;
    if (one(api, tag, r34, s, &w, 0)) {
      api->fd_write(SEL_REG, r34);
      api->fd_write(0x33, r33);
      return -2;
    }
  }
  api->fd_write(SEL_REG, r34);
  for (k = 0; k < sizeof en_reg; k++) {
    orig = 0;
    if (en_reg[k]) {
      orig = api->fd_read(en_reg[k]);
      api->fd_write(en_reg[k], (uint16_t)(orig | 1));
      p = put(line, "i2s en ");
      p = hex(p, en_reg[k], 2);
      p = put(p, " ");
      p = hex(p, orig, 4);
      p = put(p, " -> ");
      p = hex(p, api->fd_read(en_reg[k]), 4);
      *p = 0;
      api->say(line);
    }
    for (s = 0; s < sizeof i2s_sel; s++) {
      char tag[8];
      int key;
      tag[0] = 'e', tag[1] = 'n', tag[2] = ' ';
      tag[3] = en_reg[k] ? (char)('0' + (en_reg[k] >> 4)) : '-';
      tag[4] = en_reg[k] ? (char)('0' + (en_reg[k] & 15)) : '-';
      tag[5] = 0;
      key = one(api, tag, r34, i2s_sel[s], &w, 1);
      if (i2s_sel[s] == 13 && w.e > best) best = w.e;
      if (key) break;
    }
    api->fd_write(SEL_REG, r34);
    if (en_reg[k]) api->fd_write(en_reg[k], orig);
    if (api->key_down()) break;
  }
  api->fd_write(0x33, r33);
  if (api->key_down()) return -2;
  p = put(line, "i2s done 34=");
  p = hex(p, api->fd_read(SEL_REG), 4);
  p = put(p, " 31=");
  p = hex(p, api->fd_read(0x31), 4);
  p = put(p, " 33=");
  p = hex(p, api->fd_read(0x33), 4);
  p = put(p, " best=");
  p = dec(p, best);
  *p = 0;
  api->say(line);
  return (int)best;
}
