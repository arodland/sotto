/* morse: Morse RTX on the K6. It hears MCW and draws the dits and dahs on a scrolling tape with
   the letters under them, and it sends MCW from the two orange side keys used as paddles
   (docs/k6-morse.md).

   Jeff, 2026-11-08: "Morse RTX - receives and graphically draws dits and dahs on a scroll on the
   screen; use the two orange side buttons as morse paddles to transmit".

   The screen (module API level 6):

     CW 20wpm IB r18   RX      title: send speed, keyer mode (IB iambic B, IA iambic A, SK
     CQ DE K7ABC               straight key), the speed heard, and RX / TX / ID / PR (practice)
     .. -.- .- -..             the receive tape: each mark drawn as it ends (a dit 2 px, a dah
     C  Q  D  E                6 px), the letter under it once the character's gap comes
     - - - - - - - - - - -
     -.. .  .- --.             the send tape, the same for what the keyer sent
     D  E  A  G
     DE AG7EW                  the text sent

   Receive: the FD6818's tone detector (c/k6/tonedet.h, as cwlisten and EXPLORE's Listen for ID:
   16 tones 400-1300 Hz, REG_0B bit 12) polled every millisecond, debounced 8 ms, the detector's
   ~10 ms lag taken off each mark. The decoder is a real-time one: a mark under twice the dot
   length is a dit, and the dot length follows the dits and dahs heard (8 to 50 wpm), so the
   letter shows as soon as its gap does. (cwid.c, the CW ID decoder, decodes a whole
   transmission once it has ended, which suits an ID but not a tape.) The green LED shows the
   key while it receives (level 7 images).

   Send: Side 1 (MCU PC14) is the dit paddle and Side 2 (PB14) the dah paddle; both read low with
   no keypad row driven, read here straight from the GPIO input registers, so a squeeze shows
   both. Iambic B (the default), iambic A, or a straight key (either side key keys). The first
   touch keys the transmitter up (tx_on: the pager's own frequency only, and this module only
   when that is 430-450 MHz; tx_on takes 1.4-1.9 s, and the paddle that started it is sent
   first once it returns). It sends MCW: the carrier stays up, and the FD6818's tone oscillator
   (REG_70/71, as wsprtx's ID) is keyed at MR_TONE Hz, so an FM receiver hears the tone and
   another K6's tone detector reads it. MR_HANG_MS after the paddles go quiet, the over ends
   with "DE AG7EW", unless AG7EW was sent in it (the project rule: every transmission carries
   the ident), and the transmitter keys down. It never keys up over a busy channel (RSSI at
   or above MR_BUSY_DBM): that over is practice. An over is at most MR_OVER_MS (the paddles stop
   in time for the ident). The side keys give a sidetone on the speaker (TIM1 on PA11, the
   voice prompts' pin, as spksweep), put back as found. Off 430-450 MHz, on an image without the
   transmitter, or built with MR_TX 0, the paddles drive the keyer, the sidetone and the send
   tape only: practice (PR).

   Keys (read with the paddles up): EXIT ends the module (after the ident, if keyed), UP / DOWN
   the speed (5-40 wpm), MENU the keyer mode, * swaps the paddles.

   It says
     K6 MOD say morse 20wpm IB tone=700 rx=440850000 tx=on
     K6 MOD say rx 18wpm CQ DE K7ABC                 (2 s after a received transmission)
     K6 MOD say tx 20wpm ms=9480 id=auto CQ DE AG7EW (each over)
     K6 MOD say morse end overs=1
   and returns the overs sent, or -1 (an image without the module screen, level 6).

   Build: python -m modem.k6 mod out.wav c/k6/mod/morse.c --key-file KEY (CI: mod-morse.bin)
   SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 6
#define K6_MODULE_OPT z
#include "../k6_mod.h"
#include "../tonedet.h"

#ifndef MR_WPM
#define MR_WPM 20            /* send speed at the start, and the receive decoder's first guess */
#endif
#ifndef MR_MODE
#define MR_MODE 0            /* 0 iambic B, 1 iambic A, 2 straight key */
#endif
#ifndef MR_SWAP
#define MR_SWAP 0            /* 1: Side 2 dits, Side 1 dahs */
#endif
#ifndef MR_TONE
#define MR_TONE 700          /* Hz: the MCW tone sent (tone detector index 2) and the sidetone */
#endif
#ifndef MR_GAIN
#define MR_GAIN 60           /* REG_70 tone gain, as wsprtx's ID */
#endif
#ifndef MR_HANG_MS
#define MR_HANG_MS 3000      /* paddles quiet this long ends the over */
#endif
#ifndef MR_OVER_MS
#define MR_OVER_MS 60000     /* the longest over (tx_on's limit; 18 s on images before level 7) */
#endif
#ifndef MR_TX
#define MR_TX 1              /* 0: practice only, never keys up */
#endif
#ifndef MR_SIDETONE
#define MR_SIDETONE 1
#endif
#ifndef MR_AF
#define MR_AF 1              /* receive audio to the speaker (REG_47), to hear the other station */
#endif
#ifndef MR_LAG
#define MR_LAG 10            /* ms the tone detector adds to a mark (cwlisten measured 8-12) */
#endif
#ifndef MR_BUSY_DBM
#define MR_BUSY_DBM -90      /* a paddle doesn't key up while the channel is this strong */
#endif
#ifndef MR_RUN_MS
#define MR_RUN_MS 0          /* stop after this long (the tests); 0: until EXIT */
#endif
#ifndef MR_ID
#define MR_ID "AG7EW"
#endif
#define TX_LO 430000000u     /* 70 cm, north of Line A */
#define TX_HI 450000000u
#define GLITCH 8
#define POLL_US 1000u

#define WORD(hz) ((uint16_t)((uint32_t)(hz) * 1032444u / 100000u))
#define REG70_ON ((uint16_t)(0x8000u | ((MR_GAIN & 0x7Fu) << 8)))

/* ---- the MCU: GPIO and TIM1 (STM32F0-style, as Aura's PAC) ---- */
#define GPIOA 0x48000000u
#define GPIOB 0x48000400u
#define GPIOC 0x48000800u
#define GPIOF 0x48001400u
#define MODER 0x00u
#define OSPEEDR 0x08u
#define IDR 0x10u
#define ODR 0x14u
#define BSRR 0x18u
#define AFRH 0x24u
#define BRR 0x28u
#define RCC_APB2ENR 0x40021018u
#define TIM1 0x40012C00u
#define T_CR1 0x00u
#define T_DIER 0x0Cu
#define T_SR 0x10u
#define T_EGR 0x14u
#define T_CCMR2 0x1Cu
#define T_CCER 0x20u
#define T_PSC 0x28u
#define T_ARR 0x2Cu
#define T_RCR 0x30u
#define T_CCR4 0x40u
#define T_BDTR 0x44u
#define SPK (1u << 2)        /* PB2: the speaker amplifier */
#define ST_ARR (12000000u / MR_TONE - 1u)   /* TIM1 at 96 MHz / 8 */
#define ST_ON (ST_ARR >> 4)  /* a 1/16 duty pulse: quiet enough */

static uint32_t rd(uint32_t a) { return *(volatile uint32_t *)a; }
static void wr(uint32_t a, uint32_t v) { *(volatile uint32_t *)a = v; }

/* keypad columns low, bit 0-3 = PC13 PC14 PF6 PB14 (Aura's board/keypad.rs) */
static unsigned cols(void) {
  unsigned c = 0, ci = rd(GPIOC + IDR);
  if (!(ci & (1u << 13))) c |= 1;
  if (!(ci & (1u << 14))) c |= 2;
  if (!(rd(GPIOF + IDR) & (1u << 6))) c |= 4;
  if (!(rd(GPIOB + IDR) & (1u << 14))) c |= 8;
  return c;
}

/* one keypad row (a GPIOB pin) driven low: its columns */
static unsigned row(const k6_api_t *api, uint32_t pin) {
  unsigned c;
  wr(GPIOB + BRR, pin);
  api->delay_us(3);
  c = cols();
  wr(GPIOB + BSRR, pin);
  return c;
}

enum { K_EXIT = 1, K_UP = 2, K_DOWN = 4, K_MENU = 8, K_STAR = 16 };

/* EXIT, UP, DOWN, MENU (row PB11: MENU UP EXIT DOWN) and * (row PB10: 1 2 * 3); 0 while a side
   key is down (they share the columns) */
static unsigned keys(const k6_api_t *api) {
  unsigned r, k = 0;
  if (cols()) return 0;
  r = row(api, 1u << 11);
  if (r & 1) k |= K_MENU;
  if (r & 2) k |= K_UP;
  if (r & 4) k |= K_EXIT;
  if (r & 8) k |= K_DOWN;
  if (row(api, 1u << 10) & 4) k |= K_STAR;
  return k;
}

/* ---- Morse ---- */
/* by code, as cwkey.c: start at 1, then 0 for a dot and 1 for a dash (E = 2, A = 5) */
static const char tab[] = "~~ETIANMSURWDKGOHVF~L~PJBXCYZQ~~54~3~~~2~~+~~~~16=/~~~~~7~~~8~90";

static char decode(unsigned c) {
  if (c < 64) return tab[c] == '~' ? '*' : tab[c];
  return c == 76 ? '?' : c == 85 ? '.' : c == 115 ? ',' : '*';
}

static unsigned code_of(char c) {
  unsigned i;
  for (i = 2; i < sizeof tab - 1; i++)
    if (tab[i] == c && c != '~') return i;
  return 0;
}

/* 3x5 letters for the tapes: rows top first, 3 bits a row, bit 14 the top left */
static const char glyph_ch[] = "/?=.,+";
static const uint16_t glyph[] = {
    0x2BED, 0x6BAE, 0x3923, 0x6B6E, 0x79A7, 0x79A4, 0x396B, 0x5BED, 0x7497, 0x126A, 0x5BAD,
    0x4927, 0x5FED, 0x6B6D, 0x2B6A, 0x6BA4, 0x2B73, 0x6BAD, 0x388E, 0x7492, 0x5B6F, 0x5B6A,
    0x5BFD, 0x5AAD, 0x5A92, 0x72A7, 0x7B6F, 0x2C97, 0x62A7, 0x628E, 0x5BC9, 0x798E, 0x39EF,
    0x7292, 0x7BEF, 0x7BCE, 0x12A4, 0x6282, 0x0E38, 0x0002, 0x0014, 0x05D0, 0x0AA8};

static uint32_t udiv(uint32_t n, uint32_t d) {   /* the M0 has no divide */
  uint32_t q = 0, b = 1;
  while (d <= n && !(d & 0x80000000u)) d <<= 1, b <<= 1;
  while (b) {
    if (n >= d) n -= d, q |= b;
    d >>= 1, b >>= 1;
  }
  return q;
}

static char *put(char *p, const char *s) {
  while (*s) *p++ = *s++;
  return p;
}

static char *dec(char *p, uint32_t v) {
  static const uint32_t pw[] = {100000u, 10000u, 1000u, 100u, 10u, 1u};
  int i, started = 0;
  for (i = 0; i < 6; i++) {
    char d = '0';
    while (v >= pw[i]) v -= pw[i], d++;
    if (d != '0' || started || i == 5) *p++ = d, started = 1;
  }
  return p;
}

/* ---- the tapes ---- */
#define ROWS 29              /* the bitmap: lcd_wf's rows (y 21-49) */
#define TAPE_RX 0            /* first bitmap row of each tape */
#define TAPE_TX 15
#define LOG 44

typedef struct {
  uint32_t edge;             /* when the key last changed (back-dated over the debounce) */
  uint32_t cand_t, seen;     /* the input first differed; the last change committed (ms()) */
  int16_t x, cx;             /* next free column; this character's first */
  uint16_t unit;             /* dot length, ms */
  uint8_t r0, key, cand, state;   /* first row; committed key; differing; 0 idle 1 char 2 gap */
  uint8_t code, adapt, lag, glitch;
  uint8_t nlog, hi;
  uint16_t hist[8];          /* the last marks (adapt) */
  char text[22];             /* the last 21 characters, for the screen */
  char log[LOG + 1];         /* this transmission's text, for its say line */
} tape_t;

typedef struct {
  uint8_t mode, swap, el, phase, dit_m, dah_m, last, first;
  uint32_t until;            /* the current mark or gap ends */
  const char *q;             /* sending a text (the ident): the rest of it */
  unsigned qcode;            /* the current character's elements left (start bit on top) */
} keyer_t;

typedef struct {
  uint8_t bm[ROWS * 16];
  tape_t rx, tx;
  keyer_t k;
  uint32_t st[11];           /* TIM1 and pins as found (sidetone) */
  uint8_t st_ok, idm, dirty, wpm;
} work_t;

static void px(work_t *w, int x, int y) {
  if (x >= 0 && x < 128) w->bm[y * 16 + (x >> 3)] |= (uint8_t)(0x80u >> (x & 7));
}

static void bar(work_t *w, int x, int y, int n, int h) {
  int i, j;
  for (j = 0; j < h; j++)
    for (i = 0; i < n; i++) px(w, x + i, y + j);
}

/* room for n more columns: the tape moves left 32 px at a time */
static void room(work_t *w, tape_t *t, int n) {
  while (t->x + n > 127) {
    int r, b;
    for (r = t->r0; r < t->r0 + 14; r++) {
      uint8_t *p = w->bm + r * 16;
      for (b = 0; b < 16; b++) p[b] = b < 12 ? p[b + 4] : 0;
    }
    t->x -= 32;
    t->cx -= 32;
    w->dirty = 1;
  }
}

static void text_add(work_t *w, tape_t *t, char c) {
  int i;
  for (i = 0; i < 20 && t->text[i]; i++) {}
  if (i == 20 && t->text[20]) {
    for (i = 0; i < 20; i++) t->text[i] = t->text[i + 1];
  } else if (t->text[i]) {
    i++;
  }
  t->text[i] = c;
  t->text[i + 1] = 0;
  if (t->nlog < LOG) t->log[t->nlog++] = c;
  t->log[t->nlog] = 0;
  w->dirty = 1;
}

/* a mark of d ms ended: a dit or a dah, drawn. With adapt, the dot follows the sender: once
   the last 8 marks' shortest and longest are 2:1 apart (dits and dahs both there), the marks
   below halfway are dits and the rest dahs, and the dot is the mean of the dits' mean and a
   third of the dahs'; a dah is a mark of two dots or more */
static void mark(work_t *w, tape_t *t, int32_t d) {
  int dah;
  if (d < 16) return;                           /* too short for Morse at 50 wpm: noise */
  if (t->state != 1) {
    t->state = 1;
    t->code = 1;
    t->cx = t->x;
  }
  if (t->adapt && d < 1000) {
    unsigned i, mn, mx;
    t->hist[t->hi++ & 7] = (uint16_t)d;
    mn = mx = t->hist[0];
    for (i = 1; i < 8; i++) {
      if (t->hist[i] < mn) mn = t->hist[i];
      if (t->hist[i] > mx) mx = t->hist[i];
    }
    if (mx >= 2 * mn) {                         /* the dot: the dits' and the dahs' means */
      unsigned th = (mn + mx) >> 1, sd = 0, nd = 0, sa = 0, na = 0, u;
      for (i = 0; i < 8; i++)
        if (t->hist[i] < th) sd += t->hist[i], nd++;
        else sa += t->hist[i], na += 3;
      u = (udiv(sd, nd) + udiv(sa, na) + 1) >> 1;
      t->unit = (uint16_t)(u < 24 ? 24 : u > 150 ? 150 : u);
    }
  }
  dah = d >= 2 * t->unit;
  room(w, t, 6);
  bar(w, t->x, t->r0 + 2, dah ? 6 : 2, 2);
  t->x += dah ? 8 : 4;
  t->code = t->code < 128 ? (uint8_t)(t->code << 1 | dah) : 0;
  w->dirty = 1;
}

/* the gaps: a character ends at 2 dots of key-up, a word at 5. Returns the character, or 0 */
static char gap(work_t *w, tape_t *t, uint32_t now) {
  uint32_t up = now - t->edge;
  char c = 0;
  if (t->key) return 0;
  if (t->state == 1 && up >= 2u * t->unit) {
    unsigned g, i;
    int col = 0, rr = 0;
    c = decode(t->code);
    i = c >= 'A' ? (unsigned)(c - 'A') : c >= '0' && c <= '9' ? 26u + (unsigned)(c - '0') : 36u;
    if (i == 36)
      for (; glyph_ch[i - 36] && glyph_ch[i - 36] != c; i++) {}
    g = glyph[i];
    for (i = 0; i < 15; i++) {                  /* 5 rows of 3: (row, column) counted */
      if (g & (0x4000u >> i)) px(w, t->cx + col, t->r0 + 6 + rr);
      if (++col == 3) col = 0, rr++;
    }
    if (t->x < t->cx + 5) t->x = (int16_t)(t->cx + 5);
    t->x += 2;
    t->state = 2;
    text_add(w, t, c);
  } else if (t->state == 2 && up >= 5u * t->unit) {
    t->state = 0;
    t->x += 5;
    text_add(w, t, ' ');
  }
  return c;
}

/* the key's state now: debounced, and a mark that ended goes to mark() */
static void key(work_t *w, tape_t *t, uint32_t now, int k) {
  if (k == t->key) {
    t->cand = 0;
    return;
  }
  if (!t->cand) t->cand = 1, t->cand_t = now;
  if (now - t->cand_t < t->glitch) return;
  if (t->key) mark(w, t, (int32_t)(t->cand_t - t->edge) - t->lag);
  t->key = (uint8_t)k;
  t->edge = t->cand_t;
  t->seen = now;
  t->cand = 0;
}

static void tape_init(tape_t *t, int r0, unsigned unit, int adapt, int lag, int glitch) {
  t->r0 = (uint8_t)r0;
  t->x = t->cx = 0;
  t->unit = (uint16_t)unit;
  t->key = t->cand = t->state = 0;
  t->code = 1;
  t->adapt = (uint8_t)adapt;
  t->lag = (uint8_t)lag;
  t->glitch = (uint8_t)glitch;
  t->nlog = 0;
  t->text[0] = t->log[0] = 0;
  t->edge = t->seen = 0;
  t->hi = 0;
  for (r0 = 0; r0 < 8; r0++) t->hist[r0] = (uint16_t)((r0 & 1 ? 3 : 1) * unit);
}

/* ---- the keyer ---- */
enum { EL_DIT = 1, EL_DAH = 2 };
enum { M_IB, M_IA, M_SK };

/* the key's state now, from the paddles (or the text being sent). unit: the dot, ms */
static int keyer(keyer_t *k, uint32_t now, int dit, int dah, uint32_t unit) {
  int next = 0;
  if (!k->q && k->mode == M_SK && !k->phase) {
    k->dit_m = k->dah_m = 0;                    /* no memories for a straight key */
    return dit || dah;
  }
  if (k->phase && k->mode == M_IB) {          /* iambic B: the other paddle, latched */
    if (k->el == EL_DAH && dit) k->dit_m = 1;
    if (k->el == EL_DIT && dah) k->dah_m = 1;
  }
  if (k->phase == 1) {
    if ((int32_t)(now - k->until) < 0) return 1;
    k->phase = 2;
    k->until += unit;
  }
  if (k->phase == 2) {
    if ((int32_t)(now - k->until) < 0) return 0;
    k->phase = 0;
  }
  if (k->q) {                                   /* a text: its next element, or a gap */
    while (k->qcode <= 1) {
      char c = *k->q;
      if (!c) {
        k->q = 0;
        return 0;
      }
      k->q++;
      if (c == ' ') {
        k->phase = 2, k->until = now + 4 * unit;
        return 0;
      }
      k->qcode = code_of(c);
      if (!k->first) {
        k->first = 1;
        continue;
      }
      k->phase = 2, k->until = now + 2 * unit;  /* 3 dots between characters */
      return 0;
    }
    {
      unsigned b = 31;
      while (!(k->qcode >> b)) b--;
      next = (k->qcode >> (b - 1)) & 1 ? EL_DAH : EL_DIT;
      k->qcode = (k->qcode & ((1u << (b - 1)) - 1u)) | (1u << (b - 1));
    }
  } else if (k->mode != M_SK) {
    int wd = dit || k->dit_m, wa = dah || k->dah_m;
    if (wd && wa) next = k->last == EL_DIT ? EL_DAH : EL_DIT;
    else if (wd) next = EL_DIT;
    else if (wa) next = EL_DAH;
  }
  if (!next) return 0;
  /* back to back: from the gap's end, so the spacing stays exact */
  if (now - k->until > 4u) k->until = now;
  k->until += next == EL_DAH ? 3 * unit : unit;
  k->el = k->last = (uint8_t)next;
  k->phase = 1;
  k->dit_m = k->dah_m = 0;
  return 1;
}

/* ---- the sidetone: TIM1 CH4 on PA11 a 1/16-duty square wave at MR_TONE ---- */
static void st_open(const k6_api_t *api, work_t *w) {
  uint32_t *s = w->st;
  w->st_ok = 0;
  if (!MR_SIDETONE || rd(TIM1 + T_CR1) & 1) return;   /* TIM1 busy: no sidetone */
  s[0] = rd(RCC_APB2ENR), s[1] = rd(GPIOA + MODER), s[2] = rd(GPIOA + OSPEEDR);
  s[3] = rd(GPIOA + AFRH), s[4] = rd(GPIOB + ODR) & SPK;
  wr(RCC_APB2ENR, s[0] | (1u << 11));
  s[5] = rd(TIM1 + T_CCMR2), s[6] = rd(TIM1 + T_CCER), s[7] = rd(TIM1 + T_BDTR);
  s[8] = rd(TIM1 + T_PSC), s[9] = rd(TIM1 + T_ARR), s[10] = rd(TIM1 + T_RCR);
  wr(TIM1 + T_DIER, 0);
  wr(TIM1 + T_PSC, 7);
  wr(TIM1 + T_ARR, ST_ARR);
  wr(TIM1 + T_RCR, 0);
  wr(TIM1 + T_CCR4, 0);
  wr(TIM1 + T_CCMR2, (s[5] & ~0xFF00u) | (6u << 12) | (1u << 11));   /* PWM 1, preload */
  wr(TIM1 + T_CCER, 1u << 12);
  wr(TIM1 + T_EGR, 1);
  wr(TIM1 + T_SR, 0);
  wr(GPIOA + AFRH, (s[3] & ~(0xFu << 12)) | (2u << 12));             /* PA11 AF2 */
  wr(GPIOA + OSPEEDR, s[2] | (3u << 22));
  wr(GPIOA + MODER, (s[1] & ~(3u << 22)) | (2u << 22));
  wr(TIM1 + T_CR1, (1u << 7) | 1);
  wr(TIM1 + T_BDTR, s[7] | (1u << 15));
  api->delay_us(30000);                         /* the RC filter settles before the amplifier */
  wr(GPIOB + BSRR, SPK);
  w->st_ok = 1;
}

static void st_key(work_t *w, int on) {
  if (w->st_ok) wr(TIM1 + T_CCR4, on ? ST_ON : 0);
}

static void st_close(const k6_api_t *api, work_t *w) {
  uint32_t *s = w->st;
  if (!w->st_ok) return;
  wr(TIM1 + T_CCR4, 0);
  api->delay_us(20000);
  if (!s[4]) wr(GPIOB + BRR, SPK);
  wr(TIM1 + T_BDTR, s[7] & ~(1u << 15));
  wr(TIM1 + T_CR1, 0);
  wr(TIM1 + T_CCER, s[6]);
  wr(TIM1 + T_CCMR2, s[5]);
  wr(TIM1 + T_PSC, s[8]);
  wr(TIM1 + T_ARR, s[9]);
  wr(TIM1 + T_RCR, s[10]);
  wr(GPIOA + MODER, s[1]);
  wr(GPIOA + OSPEEDR, s[2]);
  wr(GPIOA + AFRH, s[3]);
  wr(RCC_APB2ENR, s[0]);
  w->st_ok = 0;
}

/* ---- the screen ---- */
enum { S_RX, S_TXUP, S_TX, S_ID, S_PR };

static void screen(const k6_api_t *api, work_t *w, int s, int hint) {
  static const char mode[] = "IBIASK";
  static const char st[] = "RX TX.TX ID PR ";
  char l[24], *p = put(l, "CW ");
  p = dec(p, w->wpm);
  p = put(p, "wpm ");
  *p++ = mode[2 * w->k.mode], *p++ = mode[2 * w->k.mode + 1];
  p = dec(put(p, " r"), udiv(1200u, w->rx.unit));
  while (p < l + 18) *p++ = ' ';
  *p++ = st[3 * s], *p++ = st[3 * s + 1], *p++ = st[3 * s + 2];
  *p = 0;
  api->lcd_text(0, l);
  api->lcd_text(1, w->rx.text);
  api->lcd_text(5, hint && !w->tx.text[0] ? "paddles: side keys" : w->tx.text);
  api->lcd_flush();
  w->dirty = 0;
}

static void say_log(const k6_api_t *api, tape_t *t, const char *head, uint32_t wpm, uint32_t ms,
                    int id) {
  char l[64], *p = put(l, head);
  p = put(dec(p, wpm), "wpm ");
  if (ms) p = put(put(dec(put(p, "ms="), ms), " id="), id ? "auto " : "own ");
  while (t->nlog && t->log[t->nlog - 1] == ' ') t->nlog--;
  if (p + t->nlog > l + 60) t->nlog = (uint8_t)(l + 60 - p);
  t->log[t->nlog] = 0;
  p = put(p, t->log);
  *p = 0;
  api->say(l);
  t->nlog = 0;
  t->log[0] = 0;
}

static void tone(const k6_api_t *api, work_t *w, int s, int on) {
  if (s == S_TX || s == S_ID) api->fd_write(0x70, on ? REG70_ON : 0);
  st_key(w, on);
}

static int run(const k6_api_t *api, work_t *w) {
  uint32_t now, t_on = 0, t_act = 0, t_flush = 0, t_key = 0, t_chk = 0, over_ms, unit;
  uint32_t rx_hz = api->rx_hz();
  int s = S_RX, tx_ok, quit = 0, overs = 0, hint = 1, id_auto = 0, relax = 1, leave = 0;
  uint32_t kd = 0;
  uint16_t r47 = api->fd_read(0x47);
  unsigned held = 0, i;
  char l[64];

  for (i = 0; i < sizeof w->bm; i++) w->bm[i] = 0;
  for (i = 0; i < 128; i += 4) px(w, (int)i, 14);        /* the line between the tapes */
  w->wpm = MR_WPM;
  w->k.mode = MR_MODE, w->k.swap = MR_SWAP, w->k.phase = 0, w->k.q = 0, w->k.last = 0;
  w->k.dit_m = w->k.dah_m = 0;
  unit = udiv(1200u, w->wpm);
  tape_init(&w->rx, TAPE_RX, unit, 1, MR_LAG, GLITCH);
  tape_init(&w->tx, TAPE_TX, unit, 0, 0, 5);
  w->idm = 0;
  tx_ok = MR_TX && api->level >= 3 && rx_hz >= TX_LO && rx_hz <= TX_HI;
  over_ms = api->level >= 7 ? MR_OVER_MS : 18000u;

  {
    char *p = put(l, "morse ");
    p = put(dec(p, w->wpm), "wpm ");
    p = put(p, w->k.mode == M_IB ? "IB" : w->k.mode == M_IA ? "IA" : "SK");
    p = put(dec(put(p, " tone="), MR_TONE), " rx=");
    p = dec(p, udiv(rx_hz, 1000u));
    p = put(p, tx_ok ? "000 tx=on" : "000 tx=off");
    *p = 0;
    api->say(l);
  }
  td_on(api);
  if (MR_AF) api->fd_write(0x47, 0x6040 | (1 << 8));    /* Aura's AF_OUT_BASE | RX audio */
  st_open(api, w);
  api->lcd_wf(w->bm, ROWS, 0);
  screen(api, w, tx_ok ? S_RX : S_PR, hint);
  t_flush = api->ms();

  while (!quit) {
    unsigned c;
    int dit, dah, k;
    now = api->ms();
    if (MR_RUN_MS && now >= MR_RUN_MS && s == S_RX) break;
    c = cols();
    dit = !!(c & (w->k.swap ? 8 : 2));
    dah = !!(c & (w->k.swap ? 2 : 8));
    if (!dit && !dah) relax = 1;

    if (s == S_RX) {
      if ((dit || dah) && relax) {               /* a paddle: a new over (or practice) */
        hint = 0;
        w->idm = 0;
        w->tx.nlog = 0;
        w->k.phase = 0, w->k.q = 0;
        w->k.dit_m = (uint8_t)dit, w->k.dah_m = (uint8_t)dah;   /* sent once keyed up */
        s = S_PR;
        if (tx_ok && KM_RSSI_DBM(api->fd_read(0x67)) >= MR_BUSY_DBM) {
          api->say("morse channel busy: practice");
        } else if (tx_ok) {
          screen(api, w, S_TXUP, 0);
          if (api->tx_on(over_ms) == 0) {
            api->fd_write(0x71, WORD(MR_TONE));
            api->fd_write(0x70, 0);
            if (w->st_ok) wr(GPIOB + BSRR, SPK);
            s = S_TX;
            overs++;
            t_chk = api->ms();
          } else {
            api->say("morse tx refused: practice");
            tx_ok = 0;
          }
        }
        if (api->level >= 7) api->led(0);
        now = t_on = t_act = t_flush = api->ms();
        w->dirty = 1;
      } else {
        k = (td_read(api) & TD_ON) != 0;
        if (k != w->rx.key && api->level >= 7 && !w->rx.cand) api->led(k);
        key(w, &w->rx, now, k);
        gap(w, &w->rx, now);
        if (w->rx.nlog && !w->rx.key && now - w->rx.edge > 2000u) {
          say_log(api, &w->rx, "rx ", udiv(1200u, w->rx.unit), 0, 0);
          if (api->level >= 7) api->led(0);
        }
      }
    }

    if (s != S_RX) {                             /* an over (S_TX, S_ID) or practice (S_PR) */
      int quiet, late, was = w->tx.key;
      char ch;
      unit = udiv(1200u, w->wpm);
      w->tx.unit = (uint16_t)unit;
      /* the over's end draws near: the paddles stop, in time for the ident */
      late = s != S_PR && now - t_on > over_ms - 100u * unit - 1500u;
      if (s == S_ID || late) dit = dah = 0, relax = 0;
      k = keyer(&w->k, now, dit, dah, unit);
      /* a straight key's contacts bounce: debounced 5 ms; the keyer's own timing isn't */
      w->tx.glitch = (uint8_t)(w->k.mode == M_SK && !w->k.q ? 5 : 0);
      key(w, &w->tx, now, k);
      if (w->tx.key != was) tone(api, w, s, w->tx.key);
      if ((ch = gap(w, &w->tx, now)) != 0 && !(w->idm & 0x80)) {   /* the ident sent? */
        w->idm = ch == MR_ID[w->idm] ? (uint8_t)(w->idm + 1) : ch == MR_ID[0] ? 1 : 0;
        if (w->idm >= sizeof MR_ID - 1) w->idm = 0x80;
      }
      quiet = !dit && !dah && !w->k.phase && !w->k.q && !k && !w->tx.key && !w->tx.cand &&
              !w->k.dit_m && !w->k.dah_m;
      if (!quiet) {
        t_act = now;
      } else if (s == S_ID || now - t_act >= MR_HANG_MS || leave || late) {
        if (s == S_TX && !(w->idm & 0x80)) {     /* not identified yet: DE AG7EW */
          s = S_ID;
          id_auto = 1;
          w->k.q = " DE " MR_ID;
          w->k.qcode = 0;
          w->k.first = 0;
          w->dirty = 1;
        } else {                                 /* the over (or practice) is done */
          uint32_t t = now;
          if (s != S_PR) {
            api->fd_write(0x70, 0);
            api->fd_write(0x71, 0);
            api->tx_off();
          }
          while (w->tx.state) gap(w, &w->tx, t += 5u * unit);
          if (s != S_PR) say_log(api, &w->tx, "tx ", w->wpm, api->ms() - t_on, id_auto);
          w->tx.nlog = 0;
          id_auto = 0;
          s = S_RX;
          td_on(api);
          if (leave) quit = 1;
          w->dirty = 1;
        }
      }
      if (s != S_PR && s != S_RX && now - t_chk >= 100u) {   /* keyed down under us? */
        t_chk = now;
        if (api->tx_pa(1) < 0) {
          api->say("morse tx cut by the core's limit");
          api->fd_write(0x70, 0);
          api->fd_write(0x71, 0);
          api->tx_off();
          st_key(w, 0);
          s = S_RX;
          tx_ok = 0;                             /* not again this run */
          w->k.phase = 0, w->k.q = 0, w->tx.key = 0;
          td_on(api);
          if (leave) quit = 1;
        }
      }
    }

    /* the keys, every 20 ms while the paddles are up (they share the keypad's columns) */
    if (!dit && !dah && now - t_key >= 20u) {
      unsigned kk = keys(api);
      t_key = now;
      if (kk && (!held || now - kd >= 300u)) {
        if (kk & K_EXIT) {
          if (s == S_RX) quit = 1;
          else leave = 1;                        /* after the ident and key-down */
        }
        if (kk & K_UP && w->wpm < 40) w->wpm++;
        if (kk & K_DOWN && w->wpm > 5) w->wpm--;
        if (kk & K_MENU && !held) w->k.mode = (uint8_t)(w->k.mode == M_SK ? M_IB : w->k.mode + 1);
        if (kk & K_STAR && !held) w->k.swap ^= 1;
        if (s == S_RX && kk & (K_UP | K_DOWN)) w->rx.unit = (uint16_t)udiv(1200u, w->wpm);
        kd = now;
        w->dirty = 1;
      }
      held = kk;
    }

    /* the screen (20-40 ms a frame): never with a key change pending or inside the keyer's
       last 45 ms of a mark or gap; on receive, just after a key change if the next one is 40 ms
       away or more (a mark: a dot and the detector's lag; a gap: a dot less the lag), or when
       it is quiet */
    if (w->dirty && now - t_flush >= 120u && !w->rx.cand && !w->tx.cand) {
      int ok;
      if (s == S_RX)                            /* (a mark: lag + a dot; a gap: a dot - lag) */
        ok = (now - w->rx.seen < 5u && w->rx.unit >= (w->rx.key ? 32u : 52u)) ||
             (!w->rx.key && now - w->rx.seen > 1000u);
      else if (w->k.phase) ok = (int32_t)(w->k.until - now) > 45;
      else ok = w->k.mode != M_SK || now - w->tx.seen < 20u || now - w->tx.seen > 300u;
      if (ok) {
        screen(api, w, s == S_RX && !tx_ok ? S_PR : s, hint);
        t_flush = api->ms();
      }
    }
    if (api->ms() == now) api->delay_us(POLL_US);
  }

  if (s == S_TX || s == S_ID) {                  /* (never: the loop ends only unkeyed) */
    api->fd_write(0x70, 0);
    api->tx_off();
  }
  st_close(api, w);
  td_off(api);
  api->fd_write(0x47, r47);
  if (api->level >= 7) api->led(0);
  {
    char *p = dec(put(l, "morse end overs="), (uint32_t)overs);
    *p = 0;
    api->say(l);
  }
  return overs;
}

__attribute__((noinline)) static int run_on_stack(const k6_api_t *api) {
  work_t w;
  return run(api, &w);
}

int k6_main(const k6_api_t *api) {
  uint32_t n = 0;
  void *s;
  if (api->abi != 1 || api->level < 6) return -1;
  s = api->scratch(&n);
  if (s && n >= sizeof(work_t)) return run(api, (work_t *)s);
  return run_on_stack(api);
}
