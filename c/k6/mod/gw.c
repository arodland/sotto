/* gw: GROUNDWAVE, a turn-based artillery game for the K6 (Jeff's idea, 2026-11-09).

   Two radio stations, each a shack with a mast on a hill, take turns lobbing radio-themed
   projectiles across the hills: the field is the level-6 bitmap (128 x 30, y = 21-50), the
   score and aim on the text lines around it. The rules (terrain, flight, weapons, shields,
   money) are c/k6/mod/gw_game.h; this file is the radio's side: keys, screens, and play over
   the air.

   Two ways to play:
     1 HOT SEAT   one radio, passed between two players
     2 RADIO      two K6s, each running this module, on 440.850 MHz (the pager's frequency must
                  be that: the module refuses radio play anywhere else). One calls (MENU in the
                  lobby), the other answers (MENU). Each turn the radio whose turn it is sends
                  its move (angle, power, item: the flight is worked out on both radios from the
                  same seed, gw_game.h), the other answers with an ACK, and both draw the shot.

   Keys (scanned from the keypad matrix as surf.c does: the API has no key identity):
     4 6, side keys    angle (held: repeats)        2 8, UP DOWN   power
     1 3               angle 10 at a time           7 9            power 10 at a time
     * #               previous / next item held    MENU or 5      fire
     EXIT              EXIT twice leaves the game (from the title screen: the module)
   In the shop: UP DOWN (2 8) pick, MENU (5) buys, EXIT done.

   Over the air (radio mode). Each transmission (an "over") waits for a clear channel (below
   RF_CLEAR_DBM for RF_CLEAR_MS), keys up with tx_on on the pager's frequency (440.850 only),
   sends one 64-byte packet twice through the FD6818's own FSK modem (FFSK 1200/1800 Hz at 1800
   baud: the pager's PHY F, the same registers as the core's ARQ reports), and ends with AG7EW
   in MCW (800 Hz, 25 wpm, on the tone oscillator), the project rule that every transmission
   identifies. About 3.7 s keyed, plus the 1.4-1.9 s tx_on takes. A key pressed during an over
   keys down at once. Packets, de-whitened (PN9, as the pager's):
     0 0xA6   1 type (HELLO JOIN SHOP MOVE ACK BYE)   2-3 game id   4 seq   5 from (station 0/1)
     6 round  7 turn   8-11 the sender's gw_hash before its action (0: don't check)
     12.. HELLO: seed (4), rounds | SHOP: shield, inv[14], money (2) | MOVE: angle, power, item |
          ACK: the seq and type it answers
     46-51 "AG7EW"   62-63 CRC-16/CCITT-FALSE of 0-61, big-endian
   Every packet but an ACK wants one: the sender listens RF_ACK_MS after its over and tries
   RF_TRIES times in all, a random pause between. A repeat (same seq) gets its ACK again and is
   otherwise dropped. A MOVE whose hash differs from the receiver's says DESYNC on line 5 (the
   move is still played).

   Build: python -m modem.k6 mod gw.wav c/k6/mod/gw.c --key-file KEY --phy F --payload XL (the
   SPI images' 11,776-byte block: the EXPLORE and lean images' 5,376 B are too small).
   gw_auto.c builds a hands-free version for the bench (GW_AUTO): the radio plays itself.
   Says (K6 MOD say ...): "gw start", "gw tx T seq=N r=R" (T: H J S M A B, the packet types),
   "gw rx T seq=N", "gw desync", "gw shot P ANG POW ITEM" (P 1 or 2), "gw round N W", "gw end W"
   (W: the winner, 0 or 1, 2 a draw), "gw bye ROUNDS".
   Returns the rounds played, or -3 (an image below level 6).
   SPDX-License-Identifier: MIT */
#ifndef K6_MODULE_LEVEL
#define K6_MODULE_LEVEL 6
#define K6_MODULE_OPT z
#endif
#include "../k6_mod.h"
#include "gw_game.h"

#ifndef GW_ROUNDS
#define GW_ROUNDS 3             /* a game: best of 3 */
#endif
#define FLUSH_MS 250            /* the idle screen's refresh (each flush ~20-40 ms) */
#define NTRAIL 64               /* trail dots kept while a shot flies */

#define RF_HZ 440850000u
#define RF_CLEAR_DBM -90
#define RF_CLEAR_MS 500
#define RF_CLEAR_WAIT_MS 15000
#define RF_OVER_MS 8000         /* tx_on's limit: lead, 2 packets, the ID (about 3.7 s) */
#define RF_LEAD_MS 300
#define RF_ACK_MS 7000
#define RF_TRIES 4
#define RF_WAIT_MS 300000       /* the caller's wait for an answer, and for the other's turn */
#define RF_MAGIC 0xA6
#define RF_CALL "AG7EW"
#define MCW_UNIT_MS 48          /* 25 wpm */
#define MCW_WORD 8260           /* 800 Hz on tone 1 (x 10.32444) */
#define MCW_ON 0xBC00           /* REG_70: tone 1 on, gain 60 */

enum { T_HELLO = 1, T_JOIN, T_SHOP, T_MOVE, T_ACK, T_BYE };

/* FD6818 registers (BK4819 numbering) and values, as the pager's own FSK path (aura-pager.patch) */
#define R_IRQ 0x02
#define R_STATUS 0x0C
#define R_IRQ_EN 0x3F
#define R_FSK_CFG 0x58
#define R_FSK_CTRL 0x59
#define R_FIFO 0x5F
#define R_TONE 0x70
#define R_TONE1 0x71
#define IRQ_TX_DONE 0x8000
#define IRQ_RX_DONE 0x2000
#define IRQ_RX_FULL 0x1000
#define IRQ_RX_SYNC 0x0002
#define CTRL_BASE 0x00F8        /* 16 preamble bytes, 4-byte sync, no scrambler */
#define CTRL_CLR 0xC000
#define CTRL_CLR_RX 0x4000
#define CTRL_RX_EN 0x1000
#define CTRL_TX_EN 0x0800

/* PHY F (FFSK 1200/1800 Hz, 1800 baud), 64-byte packets: register, value */
static const uint16_t PHY_F[] = {
    R_IRQ_EN,   0,
    R_FSK_CTRL, CTRL_BASE,
    R_FSK_CFG,  0x3F01,         /* FFSK 1200/1800 (the pager's CFG_F_1800) */
    0x72,       0x4898,         /* 1800 baud */
    0x5A,       0x076B,         /* the sync word */
    0x5B,       0x3E36,
    0x5C,       0x5625,         /* the chip's CRC off */
    0x5D,       0x3F00,         /* 64 bytes */
    0x5E,       0x0203,         /* RX almost full at 3 words */
    R_TONE,     0x00B4,         /* FSK deviation (GAIN_F) */
};
/* listen for the next packet */
static const uint16_t ARM[] = {
    R_FSK_CTRL, CTRL_BASE | CTRL_CLR_RX,
    R_FSK_CTRL, CTRL_BASE | CTRL_RX_EN,
    R_IRQ,      0,
    R_IRQ_EN,   IRQ_RX_DONE | IRQ_RX_FULL | IRQ_RX_SYNC,
};
/* the modem off (as Aura's exit_fsk_mode), the tone oscillator off */
static const uint16_t QUIET[] = {
    R_FSK_CTRL, CTRL_BASE,
    R_IRQ_EN,   0,
    R_FSK_CFG,  0,
    R_TONE,     0,
    R_TONE1,    0,
};

#define GPIOB 0x48000400u
#define GPIOC 0x48000800u
#define GPIOF 0x48001400u
#define IDR 0x10u
#define BSRR 0x18u
#define BRR 0x28u
#define ROWS_B ((1u << 0) | (1u << 4) | (1u << 9) | (1u << 10) | (1u << 11))

static uint32_t rd(uint32_t a) { return *(volatile uint32_t *)a; }
static void wr(uint32_t a, uint32_t v) { *(volatile uint32_t *)a = v; }

/* Aura's KEY_TABLE (surf.c): phase 0 side keys, then rows PB0, PB4, PB10, PB11, PB9;
   A/B side 1/2, U/D up/down, M menu, X exit, N band, V vm, W a/b */
static const char KEYS[24] = {0,   'A', 0,   'B', '7', '8', '#', '9', '4', '5', '0', '6',
                              '1', '2', '*', '3', 'M', 'U', 'X', 'D', 'V', 'W', 0,   'N'};
static const uint8_t ROWPIN[6] = {0xFF, 0, 4, 10, 11, 9};
/* the aiming keys and what they do to the angle and the power (the first 8 repeat when held) */
static const char AIMK[] = "4A6B2U8D1397";
static const int8_t AIM_DA[12] = {1, 1, -1, -1, 0, 0, 0, 0, 10, -10, 0, 0};
static const int8_t AIM_DP[12] = {0, 0, 0, 0, 1, 1, -1, -1, 0, 0, -10, 10};

/* AG7EW in Morse by code (cwkey.c's table): a start bit, then 0 a dot and 1 a dash */
static const uint8_t ID[5] = {5, 14, 56, 2, 11};
static const char TYPES[] = "?HJSMAB";

/* the stations, 7 x 6, bit 6 leftmost: P1 a solid shack under a ground plane, P2 an open one
   under a dipole; and what's left of one knocked off the air */
static const uint8_t SPRITE[3][6] = {{0x08, 0x1C, 0x08, 0x3E, 0x2A, 0x3E},
                                     {0x22, 0x14, 0x08, 0x3E, 0x22, 0x3E},
                                     {0x00, 0x00, 0x04, 0x18, 0x2C, 0x7E}};

static const char HELP[] = "HOW TO PLAY\n4 6 side  angle\n2 8 UP DN power\n* #       pick item\n"
                           "MENU 5    fire!\nEXIT x2   quit game";

typedef struct {
  const k6_api_t *api;
  gw_t g;
  uint8_t bmp[16 * GW_H];
  uint32_t now, t_key, t_rep, t_flush, t_exit, t_msg, seed, rnd;
  uint8_t key, raw, mode, me, desync, played, cur, nt;
  char ln[44];
  char msg[22];                 /* line 5's message, until t_msg */
  /* radio */
  union {                       /* the packets being taken in and sent, or while a shot flies
                                   (no radio then), its trail dots */
    struct {
      uint8_t rx[64], tx[64];
    };
    struct {
      uint8_t tx_[NTRAIL], ty_[NTRAIL];
    };
  };
  uint8_t inbox[64];
  uint8_t rxn, rx_on, have, seq, peer_seq, acked, await_seq, lobby, land_x, rounds;
  uint16_t gid;
  uint32_t t_sync, t_rd;
#ifdef GW_AUTO
  int8_t bias[2];               /* the bot's power correction for each station */
  uint8_t went;                 /* it has played (or tried to): the title screen quits */
#endif
} gc_t;

/* ---- text ---- */

static char *put(char *p, const char *s) {
  while (*s) *p++ = *s++;
  return p;
}

/* v in decimal, at least w digits */
static char *num(char *p, uint32_t v, int w) {
  char t[10];
  int n = 0;
  do {
    uint32_t q = gw_udiv(v, 10);
    t[n++] = (char)('0' + v - 10 * q);
    v = q;
  } while (v || n < w);
  while (n) *p++ = t[--n];
  return p;
}

static char *nsp(char *p, uint32_t v) {
  *p++ = ' ';
  return num(p, v, 1);
}

static char *pad(gc_t *c, char *p, int w) {
  while (p - c->ln < w) *p++ = ' ';
  return p;
}

static void say(gc_t *c, char *p) {
  *p = 0;
  c->api->say(c->ln);
}

static void text(gc_t *c, int line, char *p) {
  *p = 0;
  c->api->lcd_text(line, c->ln);
}

/* "gw <what> <type letter> seq=<n>" */
static char *say_pkt(gc_t *c, const char *what, int type, int seq) {
  char *p = put(c->ln, what);
  *p++ = TYPES[type < 7 ? type : 0];
  p = put(p, " seq=");
  return num(p, (uint32_t)seq, 1);
}

/* line 5's message for ms */
static void msg(gc_t *c, const char *s, uint32_t ms) {
  *put(c->msg, s) = 0;
  c->t_msg = c->api->ms() + ms;
}

/* ---- keys ---- */

static int kp_scan(gc_t *c) {
  int ph, k = 0;
  for (ph = 0; ph < 6 && !k; ph++) {
    uint32_t col, v;
    wr(GPIOB + BSRR, ROWS_B);
    if (ph) wr(GPIOB + BRR, 1u << ROWPIN[ph]);
    c->api->delay_us(5);
    v = rd(GPIOC + IDR);
    if (!(v & (1u << 13))) col = 0;
    else if (!(v & (1u << 14))) col = 1;
    else if (!(rd(GPIOF + IDR) & (1u << 6))) col = 2;
    else if (!(rd(GPIOB + IDR) & (1u << 14))) col = 3;
    else continue;
    k = KEYS[4 * ph + col];
  }
  wr(GPIOB + BSRR, ROWS_B);
  return k;
}

static int aim_key(int k) {
  int i;
  for (i = 0; AIMK[i]; i++)
    if (AIMK[i] == k) return i;
  return -1;
}

/* a key event: a key once it reads the same twice 10 ms apart; the first 8 aim keys repeat */
static int kp_event(gc_t *c) {
  int k;
  c->now = c->api->ms();
  if (c->now - c->t_key < 10) {
    c->api->delay_us(500);              /* (idle loops wait here) */
    return 0;
  }
  c->t_key = c->now;
  k = kp_scan(c);
  if (k != c->raw) {
    c->raw = (uint8_t)k;
    return 0;
  }
  if (k != c->key) {
    c->key = (uint8_t)k;
    c->t_rep = c->now + 400;
    return k;
  }
  if ((unsigned)aim_key(k) < 8 && (int32_t)(c->now - c->t_rep) >= 0) {
    c->t_rep = c->now + 50;
    return k;
  }
  return 0;
}

/* wait (up to 3 s) for every key to be up */
static void kp_release(gc_t *c) {
  int i, n;
  for (i = 0, n = 0; i < 300 && n < 5; i++) {
    n = kp_scan(c) ? 0 : n + 1;
    c->api->delay_us(10000);
  }
  c->key = c->raw = 0;
}

/* EXIT twice within 2 s: 1 (the first time says so on line 5) */
static int exit_twice(gc_t *c) {
  if (c->now - c->t_exit < 2000) return 1;
  c->t_exit = c->now;
  msg(c, "EXIT again: quit", 2000);
  return 0;
}

static void led(gc_t *c, int on) {
  if (c->api->level >= 7) c->api->led(on);
}

/* ---- the field ---- */

static void px(gc_t *c, int x, int y) {
  if ((unsigned)x < GW_W && (unsigned)y < GW_H) c->bmp[16 * y + (x >> 3)] |= (uint8_t)(0x80 >> (x & 7));
}

static void draw_field(gc_t *c, int aim) {
  gw_t *g = &c->g;
  int x, y, j, i;
  for (i = 0; i < 16 * GW_H; i++) c->bmp[i] = 0;
  for (x = 0; x < GW_W; x++)
    for (y = g->top[x]; y < GW_H; y++)
      if (y <= g->top[x] + 1 || ((x ^ y) & 1)) px(c, x, y);
  for (i = 0; i < c->nt; i++) px(c, c->tx_[i], c->ty_[i]);
  for (j = 0; j < 2; j++) {
    gw_pl_t *p = &g->p[j];
    const uint8_t *sp = SPRITE[p->hp ? j : 2];
    for (y = 0; y < 6; y++)
      for (x = 0; x < 7; x++)
        if (sp[y] & (0x40 >> x)) px(c, p->x - 3 + x, p->y - 6 + y);
    if (p->she) {                         /* the shield: a dotted dome */
      int r = GW_ITEMS[p->sh].r - 3, a;
      for (a = 0; a <= 180; a += 15)
        px(c, p->x + ((r * gw_cos(a)) >> 12), p->y - 3 - ((r * gw_sin(a)) >> 13));
    }
  }
  for (i = 0; i < GW_NS; i++)             /* shots: a 2 x 2 dot, or a caret above the field */
    if (g->s[i].live) {
      x = g->s[i].x >> 16;
      y = g->s[i].y >> GW_YS;
      if (y < 0) y = -1;
      px(c, x, y + 1);
      px(c, x + 1, y);
      px(c, x - (y < 0), y + 1);
      if (y >= 0) px(c, x, y);
    }
  for (i = 0; i < g->nb; i++) {           /* blasts: a disc */
    int r = g->br[i], dx, dy;
    for (dy = -r; dy <= r; dy++)
      for (dx = -r; dx <= r; dx++)
        if (dx * dx + dy * dy <= r * r) px(c, g->bx[i] + dx, g->by[i] + dy);
  }
  if (aim && !g->over) {
    gw_pl_t *p = &g->p[g->mover];
    int k;
    for (k = 3; k <= 8; k++)              /* the aim, at half height as the flight is drawn */
      px(c, p->x + ((k * gw_cos(p->ang)) >> 12), p->y - 6 - ((k * gw_sin(p->ang)) >> 13));
    if (GW_ITEMS[p->wpn].fl & GF_CEIL)
      for (x = 0; x < GW_W; x += 4) px(c, x, GW_CEIL);
  }
}

/* ---- the text lines ---- */

static void hud(gc_t *c) {
  gw_t *g = &c->g;
  gw_pl_t *p = &g->p[g->mover];
  const gw_item_t *it = &GW_ITEMS[p->wpn];
  char *q = c->ln;
  int j, w = g->wind;
  for (j = 0; j < 2; j++) {
    *q++ = g->mover == j && !g->over ? '>' : ' ';
    *q++ = 'P';
    *q++ = (char)('1' + j);
    *q++ = ' ';
    q = num(q, g->p[j].hp, 3);
    q = pad(c, q, j ? 15 : 8);
  }
  q = put(q, " R");
  q = num(q, g->round, 1);
  *q++ = '/';
  text(c, 0, num(q, g->rounds, 1));
  q = put(c->ln, "A");
  q = num(q, p->ang, 3);
  q = put(q, " P");
  q = num(q, p->pow, 3);
  q = put(q, "   W");
  q = put(q, w < 0 ? "<<" : w ? ">>" : " ");
  text(c, 1, num(q, (uint32_t)(w < 0 ? -w : w), 1));
  if ((int32_t)(c->t_msg - c->now) > 0) {
    q = put(c->ln, c->msg);
  } else {
    q = put(c->ln, it->name);
    if (p->wpn) {
      q = put(q, " x");
      q = num(q, p->inv[p->wpn], 1);
    }
    if (p->she) {
      q = pad(c, q, 17);
      *q++ = GW_ITEMS[p->sh].name[0];
      q = num(q, p->she, 3);
    }
  }
  text(c, 5, q);
}

static void flush(gc_t *c, int aim) {
  c->now = c->api->ms();
  draw_field(c, aim);
  hud(c);
  c->api->lcd_wf(c->bmp, GW_H, 0);
  c->api->lcd_flush();
  c->t_flush = c->now;
}

/* a text-only screen: lines 0-5 from one string, '\n' between them */
static void page(gc_t *c, const char *s) {
  int line;
  c->api->lcd_wf(0, 0, 0);
  for (line = 0; line < 6; line++) {
    char *p = c->ln;
    while (*s && *s != '\n') *p++ = *s++;
    if (*s) s++;
    text(c, line, p);
  }
  c->api->lcd_flush();
}

/* ---- over the air ---- */

static void pn9(uint8_t *b) {
  uint32_t s = 0x1FF;
  int i, k;
  for (i = 0; i < 64; i++) {
    b[i] ^= (uint8_t)s;
    for (k = 0; k < 8; k++) s = (s >> 1) | (((s ^ (s >> 5)) & 1) << 8);
  }
}

static uint32_t crc16(const uint8_t *p) {
  uint32_t c = 0xFFFF;
  int k, n = 62;
  while (n--) {
    c ^= (uint32_t)*p++ << 8;
    for (k = 0; k < 8; k++) c = (c & 0x8000) ? (c << 1) ^ 0x1021 : c << 1;
  }
  return c & 0xFFFF;
}

static int rssi(gc_t *c) { return KM_RSSI_DBM(c->api->fd_read(0x67)); }

static void fd(gc_t *c, int reg, int v) { c->api->fd_write((uint8_t)reg, (uint16_t)v); }

static void fd_list(gc_t *c, const uint16_t *t, int n) {
  while (n > 0) {
    fd(c, t[0], t[1]);
    t += 2;
    n -= 2;
  }
}

static void rf_rearm(gc_t *c) {
  fd_list(c, ARM, sizeof ARM / 2);
  c->rx_on = 0;
  c->rxn = 0;
}

static void rf_arm(gc_t *c) {
  fd_list(c, PHY_F, sizeof PHY_F / 2);
  rf_rearm(c);
}

static void rf_take(gc_t *c, int n) {
  if (n > 32 - c->rxn) n = 32 - c->rxn;
  while (n-- > 0) {
    uint32_t w = c->api->fd_read(R_FIFO);
    c->rx[2 * c->rxn] = (uint8_t)w;     /* the low byte came first on air */
    c->rx[2 * c->rxn + 1] = (uint8_t)(w >> 8);
    c->rxn++;
  }
  c->t_rd = c->api->ms();
}

/* Poll the FSK receiver: 1 when c->rx holds a good packet (de-whitened, CRC, magic, this game's
   or, in the lobby, a HELLO). Words come off the 8-word FIFO 3 at a time on its almost-full
   interrupt, the rest at RX-done, and by time if the interrupt stalls (it is an edge: the
   pager reads it the same way). */
static int rf_poll(gc_t *c) {
  const k6_api_t *api = c->api;
  uint32_t f = 0, now = api->ms();
  uint8_t *b = c->rx;
  int ok = 0;
  if (api->fd_read(R_STATUS) & 1) {
    fd(c, R_IRQ, 0);
    f = api->fd_read(R_IRQ);
  } else {
    api->delay_us(1000);                /* (a word takes 8.9 ms; the FIFO holds 8) */
  }
  if (f & IRQ_RX_SYNC) {
    c->rx_on = 1;
    c->rxn = 0;
    c->t_sync = c->t_rd = now;
    led(c, 1);
  }
  if (!c->rx_on) {
    if (f & IRQ_RX_DONE) rf_rearm(c);
    return 0;
  }
  if (f & IRQ_RX_FULL) rf_take(c, 3);
  else if (now - c->t_rd > 36 && (((now - c->t_sync) * 115) >> 10) > (uint32_t)c->rxn + 1)
    rf_take(c, 1);                      /* 4 word times and no interrupt: one by time */
  if (f & IRQ_RX_DONE) rf_take(c, 8);
  if (c->rxn < 32 && !(f & IRQ_RX_DONE) && now - c->t_sync < 700) return 0;
  if (c->rxn >= 32) {
    pn9(b);
    ok = crc16(b) == ((uint32_t)b[62] << 8 | b[63]) && b[0] == RF_MAGIC &&
         (b[1] == T_HELLO ? c->lobby : (uint32_t)(b[2] | b[3] << 8) == c->gid);
  }
  rf_rearm(c);
  led(c, 0);
  return ok;
}

/* wait ms; a key pressed while keyed ends the over (the TX rules): -4 */
static int rf_wait(gc_t *c, uint32_t ms) {
  uint32_t t0 = c->api->ms();
  while (c->api->ms() - t0 < ms) {
    if (kp_scan(c)) return -4;
    c->api->delay_us(4000);
  }
  return 0;
}

static int rf_send_packet(gc_t *c) {
  int i, r;
  fd(c, R_IRQ_EN, IRQ_TX_DONE);
  fd(c, R_FSK_CTRL, CTRL_BASE | CTRL_CLR);
  fd(c, R_FSK_CTRL, CTRL_BASE);
  for (i = 0; i < 32; i++) fd(c, R_FIFO, c->tx[2 * i] | c->tx[2 * i + 1] << 8);
  if ((r = rf_wait(c, 20)) != 0) return r;
  fd(c, R_FSK_CTRL, CTRL_BASE | CTRL_TX_EN);
  for (i = 0; i < 200 && !(c->api->fd_read(R_STATUS) & 1); i++)
    if ((r = rf_wait(c, 5)) != 0) return r;
  fd(c, R_IRQ, 0);
  fd(c, R_FSK_CTRL, CTRL_BASE);
  return rf_wait(c, 60);
}

/* AG7EW in MCW on the tone oscillator */
static int rf_mcw(gc_t *c) {
  int r, i;
  fd(c, R_TONE1, MCW_WORD);
  if ((r = rf_wait(c, 200)) != 0) return r;
  for (i = 0; i < 5; i++) {
    int code = ID[i], b;
    for (b = 7; !(code >> b); b--) {}
    while (b--) {
      fd(c, R_TONE, MCW_ON);
      if ((r = rf_wait(c, ((code >> b) & 1 ? 3 : 1) * MCW_UNIT_MS)) != 0) return r;
      fd(c, R_TONE, 0);
      if ((r = rf_wait(c, (b ? 1 : 3) * MCW_UNIT_MS)) != 0) return r;
    }
  }
  return 0;
}

/* One over: the packet in c->tx (plain; framed and whitened here) twice, then the ID. 0, -2 the
   channel stayed busy, -3 tx_on refused, -4 a key ended it. */
static int rf_over(gc_t *c) {
  const k6_api_t *api = c->api;
  uint32_t t0, clear, k;
  int r = 0, i;
  char *p;
  c->tx[0] = RF_MAGIC;
  c->tx[2] = (uint8_t)c->gid;
  c->tx[3] = (uint8_t)(c->gid >> 8);
  for (i = 0; i < 5; i++) c->tx[46 + i] = (uint8_t)RF_CALL[i];
  k = crc16(c->tx);
  c->tx[62] = (uint8_t)(k >> 8);
  c->tx[63] = (uint8_t)k;
  p = say_pkt(c, "gw tx ", c->tx[1], c->tx[4]);
  pn9(c->tx);
  kp_release(c);
  t0 = clear = api->ms();
  while (!r && api->ms() - clear < RF_CLEAR_MS) {
    if (rssi(c) >= RF_CLEAR_DBM) clear = api->ms();
    if (api->ms() - t0 > RF_CLEAR_WAIT_MS) r = -2;
    if (kp_scan(c)) r = -4;
    api->delay_us(10000);
  }
  if (!r && api->tx_on(RF_OVER_MS) != 0) r = -3;
  if (!r) {
    led(c, 1);
    r = rf_wait(c, RF_LEAD_MS);
    fd_list(c, PHY_F, sizeof PHY_F / 2);
    for (i = 0; i < 2 && !r; i++) r = rf_send_packet(c);
    fd_list(c, QUIET, sizeof QUIET / 2);
    if (!r) r = rf_mcw(c);
    fd_list(c, QUIET, sizeof QUIET / 2);
    api->tx_off();
    led(c, 0);
  }
  p = put(p, " r=");
  if (r) *p++ = '-';
  say(c, num(p, (uint32_t)-r, 1));
  rf_arm(c);
  return r;
}

/* start c->tx as a packet of this type */
static void rf_new(gc_t *c, int type) {
  int i;
  for (i = 0; i < 64; i++) c->tx[i] = 0;
  c->tx[1] = (uint8_t)type;
  c->tx[4] = type == T_ACK ? 0 : ++c->seq;
  c->tx[5] = c->me;
  c->tx[6] = c->g.round;
  c->tx[7] = c->g.turn;
}

static void put32(uint8_t *b, uint32_t v) {
  int i;
  for (i = 0; i < 4; i++, v >>= 8) b[i] = (uint8_t)v;
}

static uint32_t get32(const uint8_t *b) {
  return (uint32_t)b[0] | (uint32_t)b[1] << 8 | (uint32_t)b[2] << 16 | (uint32_t)b[3] << 24;
}

/* Take in what the receiver heard: an ACK for what we wait on sets c->acked; anything else of
   the other station's is answered with an ACK (repeats too) and, if new, kept in c->inbox
   (c->have = 1 until taken). */
static void rf_service(gc_t *c) {
  uint8_t *b = c->rx, seq, type;
  int i;
  if (!rf_poll(c)) return;
  type = b[1];
  seq = b[4];
  say(c, say_pkt(c, "gw rx ", type, seq));
  if (type == T_ACK) {
    if (b[12] == c->await_seq) c->acked = 1;
    return;
  }
  if (b[5] == c->me && type != T_HELLO) return;       /* our own station's: an echo? */
  if (seq != c->peer_seq) {
    for (i = 0; i < 64; i++) c->inbox[i] = b[i];
    c->have = 1;
    c->peer_seq = seq;
  }
  if (type == T_HELLO) c->gid = (uint16_t)(b[2] | b[3] << 8);
  rf_new(c, T_ACK);
  c->tx[12] = seq;
  c->tx[13] = type;
  rf_over(c);
}

/* line 5 says s until cleared (c->t_msg = 0), shown now */
static void status(gc_t *c, const char *s) {
  msg(c, s, 600000);
  if (c->g.round) {
    flush(c, 0);
  } else {
    text(c, 5, put(c->ln, c->msg));
    c->api->lcd_flush();
  }
  c->t_flush = c->api->ms();
}

/* send c->tx (built with rf_new) until it is acknowledged: 1, or 0 after RF_TRIES overs */
static int rf_reliable(gc_t *c) {
  uint8_t keep[64];
  int i, t;
  for (i = 0; i < 64; i++) keep[i] = c->tx[i];
  c->await_seq = keep[4];
  for (t = 0; t < RF_TRIES; t++) {
    uint32_t t0;
    for (i = 0; i < 64; i++) c->tx[i] = keep[i];
    c->acked = 0;
    status(c, t ? "TX again..." : "TX...");
    if (rf_over(c) == -4) break;
    status(c, "waiting for ACK");
    t0 = c->api->ms();
    while (c->api->ms() - t0 < RF_ACK_MS) {
      rf_service(c);
      if (c->acked) {
        c->t_msg = 0;
        return 1;
      }
    }
    c->rnd = c->rnd * 1103515245u + 12345u + (uint32_t)rssi(c);
    c->api->delay_us(((c->rnd >> 16) & 2047) * 1000u);    /* 0-2 s apart */
  }
  status(c, "no ACK: EXIT quits");
  return 0;
}

/* wait for a new packet of this type from the other radio: 1, or 0 (they left, EXIT twice,
   or RF_WAIT_MS) */
static int rf_await(gc_t *c, int type, const char *what) {
  uint32_t t0 = c->api->ms();
  status(c, what);
  for (;;) {
    if (c->have && c->inbox[1] == type) {
      c->have = 0;
      c->t_msg = 0;
      return 1;
    }
    if ((c->have && c->inbox[1] == T_BYE) || c->api->ms() - t0 > RF_WAIT_MS) {
      status(c, "the other left");
      return 0;
    }
    rf_service(c);
    if (kp_event(c) == 'X' && exit_twice(c)) return 0;
    if (!c->rx_on && c->api->ms() - c->t_flush > 1000) status(c, c->msg);
  }
}

/* ---- the game ---- */

#ifdef GW_AUTO
/* the bench bot: power for the distance (the range at 45 degrees is about v^2 / g), plus its
   own correction from where its last shot came down. Never the game's generator: the other
   radio doesn't run the bot. */
static void bot_aim(gc_t *c) {
  gw_t *g = &c->g;
  gw_pl_t *p = &g->p[g->mover];
  int d = g->p[g->mover ^ 1].x - p->x, i;
  c->rnd = c->rnd * 1103515245u + 12345u;
  p->ang = (uint8_t)(d > 0 ? 45 : 135);
  if (d < 0) d = -d;
  p->pow = (uint8_t)gw_clamp((int)gw_isqrt((uint32_t)d * 67u) + c->bias[g->mover] + (int)(c->rnd >> 29) - 4, 20, 100);
  p->wpn = 0;
  for (i = GW_NWPN - 1; i > 0; i--)
    if (p->inv[i] && i != 10) p->wpn = (uint8_t)i;
}
#endif

/* aim: the mover's keys until MENU fires. 1 fire, 0 leave the game */
static int aim(gc_t *c) {
  gw_t *g = &c->g;
  gw_pl_t *p = &g->p[g->mover];
  uint32_t t0 = c->api->ms();
  int k, i;
#ifdef GW_AUTO
  bot_aim(c);
#endif
  (void)t0;
  flush(c, 1);
  for (;;) {
    k = kp_event(c);
#ifdef GW_AUTO
    if (!k && c->api->ms() - t0 > 2500) k = 'M';
#endif
    if (k == 'X' && exit_twice(c)) return 0;
    if ((i = aim_key(k)) >= 0) {
      p->ang = (uint8_t)gw_clamp(p->ang + AIM_DA[i], 0, 180);
      p->pow = (uint8_t)gw_clamp(p->pow + AIM_DP[i], 0, 100);
    }
    if (k == '#' || k == '*') {           /* the next (previous) item held */
      int w = p->wpn;
      for (i = 0; i < GW_NWPN; i++) {
        w = k == '#' ? (w + 1 == GW_NWPN ? 0 : w + 1) : (w ? w - 1 : GW_NWPN - 1);
        if (!w || p->inv[w]) break;
      }
      p->wpn = (uint8_t)w;
    }
    if (k == 'M' || k == '5') return 1;
    if (c->mode) {
      rf_service(c);
      if (c->have && c->inbox[1] == T_BYE) return 0;
    }
    if (k || (c->now - c->t_flush >= FLUSH_MS && !c->rx_on)) flush(c, 1);
  }
}

/* the shot on the screen: the flight, a frame every other step, each blast as a disc */
static void fly(gc_t *c, int a, int pw, int w) {
  gw_t *g = &c->g;
  int k, i;
  say(c, nsp(nsp(nsp(nsp(put(c->ln, "gw shot"), g->mover + 1u), (uint32_t)a), (uint32_t)pw), (uint32_t)w));
  c->t_msg = 0;
  c->nt = 0;
  c->land_x = 0xFF;
  gw_fire(g, a, pw, w);
  for (k = 0;; k++) {
    int live = gw_step(g);
    for (i = 0; i < GW_NS && !(k & 1); i++) {
      gw_shot_t *s = &g->s[i];
      if (s->live && c->nt < NTRAIL && s->y >= 0) {
        c->tx_[c->nt] = (uint8_t)(s->x >> 16);
        c->ty_[c->nt++] = (uint8_t)(s->y >> GW_YS);
      }
    }
    if (g->nb && c->land_x == 0xFF) c->land_x = g->bx[0];
    if (g->nb || !(k & 1) || !live) {
      led(c, g->nb);
      flush(c, 0);
      if (g->nb) c->api->delay_us(150000);
    }
    if (!live) break;
  }
  led(c, 0);
  c->nt = 0;
  g->nb = 0;
  gw_end_turn(g);
}

/* the shop for station j, till EXIT */
static void shop(gc_t *c, int j) {
  gw_pl_t *p = &c->g.p[j];
  int k, i;
#ifdef GW_AUTO
  for (i = 1, k = 0; i < GW_NI && k < 3; i++) {
    c->rnd = c->rnd * 1103515245u + 12345u;
    if (!(c->rnd >> 30) && !gw_buy(&c->g, j, i)) k++;
  }
  return;
#endif
  if (!c->cur) c->cur = 1;
  c->t_msg = 0;
  for (;;) {
    int top = c->cur > 3 ? c->cur - 3 : 1;
    char *q = put(c->ln, "SHOP P");
    *q++ = (char)('1' + j);
    q = put(q, "  $");
    q = num(q, p->money, 1);
    q = put(q, "  R");
    text(c, 0, num(q, (uint32_t)c->g.round + 1, 1));
    for (i = 0; i < 4; i++) {
      int n = top + i;
      q = c->ln;
      *q++ = n == c->cur ? '>' : ' ';
      q = pad(c, put(q, GW_ITEMS[n].name), 14);
      *q++ = '$';
      q = num(q, GW_ITEMS[n].price, 2);
      *q++ = ' ';
      text(c, 1 + i, num(q, n >= GW_NWPN ? p->sh == n : p->inv[n], 1));
    }
    text(c, 5, put(c->ln, (int32_t)(c->t_msg - c->now) > 0 ? c->msg : GW_ITEMS[c->cur].info));
    c->api->lcd_wf(0, 0, 0);
    c->api->lcd_flush();
    do {
      k = kp_event(c);
      if (c->mode) rf_service(c);
    } while (!k);
    if (k == 'U' || k == '2') c->cur = (uint8_t)(c->cur > 1 ? c->cur - 1 : GW_NI - 1);
    if (k == 'D' || k == '8') c->cur = (uint8_t)(c->cur < GW_NI - 1 ? c->cur + 1 : 1);
    if (k == 'M' || k == '5') msg(c, gw_buy(&c->g, j, c->cur) ? "can't buy that" : "bought!", 1500);
    if (k == 'X') return;
  }
}

/* radio: my shop goes to the other radio, theirs comes in. 1, or 0 to leave */
static int swap_shops(gc_t *c) {
  gw_pl_t *p = &c->g.p[c->me];
  uint8_t *b = c->inbox;
  int i;
  rf_new(c, T_SHOP);
  c->tx[12] = p->sh;
  for (i = 0; i < GW_NI; i++) c->tx[13 + i] = p->inv[i];
  c->tx[27] = (uint8_t)p->money;
  c->tx[28] = (uint8_t)(p->money >> 8);
  if (!rf_reliable(c) || !rf_await(c, T_SHOP, "the other is shopping")) return 0;
  p = &c->g.p[c->me ^ 1];
  p->sh = b[12] < GW_NI ? b[12] : 0;
  for (i = 0; i < GW_NI; i++) p->inv[i] = b[13 + i];
  p->money = (uint16_t)(b[27] | b[28] << 8);
  return 1;
}

static int any_key(gc_t *c) {
  int k;
#ifdef GW_AUTO
  c->api->delay_us(2000000);
  return 'M';
#endif
  kp_release(c);
  do {
    k = kp_event(c);
    if (c->mode) rf_service(c);
  } while (!k);
  return k;
}

/* the field with line 1 saying "<s>P<who+1>" (who 2: s alone) and line 5 s5; then a key */
static int banner(gc_t *c, const char *s, int who, const char *s5) {
  char *q;
  flush(c, 0);
  q = put(c->ln, s);
  if (who < 2) {
    *q++ = 'P';
    *q++ = (char)('1' + who);
  }
  text(c, 1, q);
  text(c, 5, put(c->ln, s5));
  c->api->lcd_flush();
  return any_key(c);
}

/* one game: 0 played out, -1 left */
static int game(gc_t *c, uint32_t seed) {
  gw_t *g = &c->g;
  uint8_t *b = c->inbox;
  gw_new_game(g, seed, c->rounds);
  for (;;) {
    int j;
    for (j = 0; j < 2; j++)               /* the shops: each station's own, or swapped over air */
      if (!c->mode || j == c->me) shop(c, j);
    if (c->mode && !swap_shops(c)) return -1;
    gw_new_round(g);
    c->t_msg = 0;
    while (!g->over) {
      gw_pl_t *pl = &g->p[g->mover];
      int me = g->mover, dir = g->p[me ^ 1].x > pl->x ? 1 : -1;
      if (!c->mode || me == c->me) {
        if (!aim(c)) return -1;
        if (c->mode) {
          rf_new(c, T_MOVE);
          put32(c->tx + 8, gw_hash(g));
          c->tx[12] = pl->ang;
          c->tx[13] = pl->pow;
          c->tx[14] = pl->wpn;
          if (!rf_reliable(c)) return -1;
        }
        fly(c, pl->ang, pl->pow, pl->wpn);
      } else {
        if (!rf_await(c, T_MOVE, "the other is aiming")) return -1;
        if (get32(b + 8) && get32(b + 8) != gw_hash(g)) {
          c->desync = 1;
          say(c, put(c->ln, "gw desync"));
        }
        fly(c, b[12], b[13], b[14]);
      }
#ifdef GW_AUTO
      if (c->land_x != 0xFF && (!c->mode || me == c->me))
        c->bias[me] = (int8_t)gw_clamp(c->bias[me] + (((int)g->p[me ^ 1].x - c->land_x) * dir) / 4, -30, 30);
#endif
      (void)dir;
      if (!pl->inv[pl->wpn]) pl->wpn = 0;
      if (c->desync) msg(c, "DESYNC!", 3000);
    }
    c->played++;
    say(c, nsp(nsp(put(c->ln, "gw round"), g->round), g->winner));
    if (g->over == 2) {
      int w = g->p[0].wins > g->p[1].wins ? 0 : g->p[1].wins > g->p[0].wins ? 1 : 2;
      say(c, nsp(put(c->ln, "gw end"), (uint32_t)w));
      return banner(c, w == 2 ? "A DRAW!" : "GAME TO ", w, "MENU again  EXIT quit") == 'X' ? -1 : 0;
    }
    banner(c, g->winner == 2 ? "both off the air!" : "round to ", g->winner, "any key: the shop");
  }
}

/* the title screen's backdrop: a field with both stations */
static void backdrop(gc_t *c) {
  gw_new_game(&c->g, c->seed, 1);
  gw_new_round(&c->g);
  draw_field(c, 0);
  c->api->lcd_wf(c->bmp, GW_H, 0);
}

/* radio: the lobby. 1 a game is set up (c->me, c->gid, the seed in c->seed), 0 back */
static int lobby(gc_t *c) {
  const k6_api_t *api = c->api;
  int k;
  c->have = 0;
  c->peer_seq = 0;
  c->gid = 0;
  c->lobby = 1;
  rf_arm(c);
  backdrop(c);
  c->g.round = 0;
  for (;;) {
    int called = c->have && c->inbox[1] == T_HELLO;
    char *p = num(put(c->ln, "listening "), (uint32_t)-rssi(c), 1);
    text(c, 5, put(p, " -dBm"));
    text(c, 0, put(c->ln, "RADIO 440.850"));
    text(c, 1, put(c->ln, called ? "AG7EW calls! MENU" : "MENU call  EXIT back"));
    api->lcd_flush();
    c->t_flush = api->ms();
    do {
      k = kp_event(c);
#ifdef GW_AUTO
      if (!k && c->now - c->t_flush > 900 && (called || GW_AUTO == 1)) k = 'M';
      if (!k && c->now > 600000u) k = 'X';
#endif
      rf_service(c);
    } while (!k && (c->rx_on || api->ms() - c->t_flush < 1000));
    if (k == 'X') return c->lobby = 0;
    if (k != 'M' && k != '5') continue;
    c->lobby = 0;
    if (called) {                         /* answer the call: we are P2 */
      c->have = 0;
      c->seed = get32(c->inbox + 12);
      c->rounds = c->inbox[16];           /* (the caller's game) */
      c->me = 1;
      rf_new(c, T_JOIN);
      if (rf_reliable(c)) return 1;
    } else {                              /* call: we are P1, and the seed is ours */
      c->me = 0;
      c->seed = (c->seed * 1103515245u + 12345u) ^ (api->ticks() << 16) ^ (uint32_t)rssi(c);
      c->gid = (uint16_t)(c->seed >> 8 | 1);
      rf_new(c, T_HELLO);
      put32(c->tx + 12, c->seed);
      c->tx[16] = GW_ROUNDS;
      if (rf_reliable(c) && rf_await(c, T_JOIN, "waiting for an answer")) return 1;
    }
    c->have = 0;
    c->lobby = 1;
#ifdef GW_AUTO
    return c->lobby = 0;                  /* the bot calls once (4 tries), then quits */
#endif
  }
}

__attribute__((noinline)) static int run(const k6_api_t *api, gc_t *c) {
  uint8_t *z;
  unsigned i;
  int k;
  z = (uint8_t *)c;
  for (i = 0; i < sizeof *c; i++) z[i] = 0;
  c->api = api;
  c->seed = c->rnd = api->ticks() * 2654435761u + 1;
  say(c, put(c->ln, "gw start"));
  kp_release(c);
  for (;;) {
    backdrop(c);
    text(c, 0, put(c->ln, "     GROUNDWAVE"));
    text(c, 1, put(c->ln, "1 hot seat  2 radio"));
    text(c, 5, put(c->ln, "3 help     EXIT quit"));
    api->lcd_flush();
#ifdef GW_AUTO
    k = c->went++ ? 'X' : GW_AUTO ? '2' : '1';
#else
    do k = kp_event(c);
    while (!k);
#endif
    if (k == 'X') break;
    if (k == '3') {
      page(c, HELP);
      any_key(c);
    }
    c->mode = k == '2';
    c->rounds = GW_ROUNDS;
    c->me = 0;
    c->desync = 0;
    if (k == '1') game(c, c->seed);
    if (c->mode && api->rx_hz() != RF_HZ) {
      text(c, 5, put(c->ln, "radio: 440.850 only"));
      api->lcd_flush();
      any_key(c);
    } else if (c->mode && lobby(c) && game(c, c->seed) < 0) {
      rf_new(c, T_BYE);                   /* (once, unacknowledged) */
      rf_over(c);
    }
    c->mode = 0;
    c->seed = c->seed * 1103515245u + api->ticks();
  }
  kp_release(c);
  say(c, nsp(put(c->ln, "gw bye"), c->played));
  return c->played;
}

/* the state (about 1.4 KB) in the scratch RAM past the code if it fits there, else on the stack */
__attribute__((noinline)) static int on_stack(const k6_api_t *api) {
  gc_t st;
  return run(api, &st);
}

int k6_main(const k6_api_t *api) {
  uint32_t room;
  void *m;
  if (api->level < 6) return -3;
  m = api->scratch(&room);
  return room >= sizeof(gc_t) ? run(api, (gc_t *)m) : on_stack(api);
}
