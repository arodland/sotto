/* surf: band surfing on the K6 (Jeff's idea, 2026-11-08). Receive only.

   The screen is a bargraph of signal strength (Y) across frequency (X): 64 bins, 2 pixels each,
   drawn as the level-6 waterfall bitmap (30 rows of 128 pixels, y = 21-50). The listening
   frequency is a blinking bar (it flips to inverse about 2.5 times a second) with a caret above
   it; a small tick over a bar marks a peak, a dot in the top row a repeater the explorer saved
   (its table, c/k6/rt.h, read once at the start). The title line says the band and the bin step,
   line 1 the listening frequency and its level in dBm (a settled read), line 5 the window's
   edges, or with the bar on a saved repeater its call, offset and TX tone ("N0CALL +5.0 103.5").

   Keys (the module scans the keypad itself: the module API has key_down but no key identity;
   the matrix is Aura's src/device/keypad.rs at edf3b98, rows PB0/PB4/PB10/PB11/PB9, columns
   PC13/PC14/PF6/PB14, side keys read with no row driven):
     UP / DOWN       the listening bar one bin up / down (held: repeats); past an edge the
                     window pans a quarter span
     SIDE1 / SIDE2   (the orange side keys) jump to the next peak above / below that a settled
                     read confirms as a signal (SQ_DB over the settled floor); with none left
                     in the window, it pans half a span that way and looks again after the next
                     sweep, on up the band until it finds one or reaches the band's edge
     BAND or #       next band: 2m, 1.25m, 70cm, GMRS, marine + NOAA WX
     1 / 3           zoom in / out (bin step 5, 10, 12.5, 25, 50, 100 kHz) around the bar
     *               monitor: audio open on the bar all the time, no sweeping
     0               hold: no refresh sweeps while a signal plays (default: one every 5 s)
     MENU            say the bar's frequency and level on the serial port
     EXIT            leave (the module waits for the key's release, so the pager doesn't see it)

   One receiver both sweeps and listens, so the module listens first: while the bar's channel
   is quiet it sweeps (audio muted), and every BLOCK bins it goes back to the bar for a settled
   read; a signal there SQ_DB over the settled floor opens the audio (REG_47 FM, speaker amp on
   PB2, as dmrdet) and parks on it. It closes HANG_MS after the signal drops, and then sweeps
   again. While parked, a refresh sweep every REFRESH_MS (about 0.3 s of muted audio) keeps the
   bars current, unless hold (0) is on.

   Levels: sweep reads are quick (SETTLE_US after the tune, as Aura's own spectrum: about 59 %
   of the way in dB from the -173 dBm restart, Phase 0), so the bars are relative: the floor is
   the quartile of the window's bins, each pixel of height about 1.1 quick dB. Bars fall at
   most DECAY_DB a sweep and rise at once. The squelch and line 1 use settled reads (CHECK_US):
   the settled floor is the second lowest of 8 bins' settled reads, measured when the window
   changes and every 30 s of sweeping, and a quieter read of the bar lowers it. The receiver's
   AGC is left alone (dmrdet's frozen gain shifts RSSI by up to 15 dB).

   surfauto.c builds the same module for unattended bench runs: SIDE1 presses itself every
   SURF_AUTO_MS (saying the bar's frequency and level first), it wraps to the band's bottom at
   the top, and it leaves after SURF_AUTO_S; keys still work. Before it ends it says its last
   bargraph ("bar 441.2000 25k" and two lines of a letter a bin, tools/k6_screen.py draws them).

   Lines (K6 MOD say ...): "surf 70cm 440.850 25k", "sel 441.2000 -71", "end sw=N".
   Writes REG_47 and GPIOB bit 2 (speaker) and the keypad rows' GPIOB BSRR/BRR bits, all put back.
   Reads the SPI flash (level 5's fl_read), never writes it. No TX. The bitmap (480 B) and the state (~400 B) live on the stack, about 0.8 KB of the
   3.5 KB a module has at its call; the store holds only the code.
   Returns the sweeps done, or -3 (an image below level 6).
   SPDX-License-Identifier: MIT */
#ifndef K6_MODULE_LEVEL
#define K6_MODULE_LEVEL 6
#define K6_MODULE_OPT z
#endif
#include "../k6_mod.h"

#define NB 64                   /* bins on screen */
#define ROWS 30                 /* bitmap rows (KM_WF_ROWS) */
#define BAR0 6                  /* first bar row: caret rows 1-3, peak ticks row 4 */
#define BARH (ROWS - BAR0)
#define SETTLE_US 3000          /* a sweep read after its tune (Aura's spectrum, wide) */
#define CHECK_US 12000          /* a settled read */
#define BLOCK 16                /* bins between checks of the bar's channel */
#define SQ_DB 10                /* squelch: settled dB over the settled floor */
#define HYST_DB 3
#define HANG_MS 1500
#define PARK_US 20000           /* a parked read every ~20 ms */
#define REFRESH_MS 5000
#define SFL_MS 30000            /* settled floor re-measured this often while sweeping */
#define FLUSH_MS 200            /* the screen (each flush ~20-40 ms) */
#define PEAK_DB 6               /* a peak: quick dB over the floor */
#define DECAY_DB 3
#define MSG_MS 1500
#define WIDE 1
#define NK 24                   /* known repeaters (the explorer's table) kept at a time */
#define RT_BASE 0x0F0000u       /* c/k6/rt.h: 1024 slots of 64 B */
#define RT_SLOTS 1024

#define GPIOB 0x48000400u
#define GPIOC 0x48000800u
#define GPIOF 0x48001400u
#define IDR 0x10u
#define ODR 0x14u
#define BSRR 0x18u
#define BRR 0x28u
#define SPK (1u << 2)
#define ROWS_B ((1u << 0) | (1u << 4) | (1u << 9) | (1u << 10) | (1u << 11))
#define AF_FM 0x6140u
#define AF_MUTE 0x6040u

static uint32_t rd(uint32_t a) { return *(volatile uint32_t *)a; }
static void wr(uint32_t a, uint32_t v) { *(volatile uint32_t *)a = v; }

typedef struct {
  uint32_t lo, hi;
  uint8_t zi;                   /* default step (STEPS index) */
  char name[7];
} band_t;                       /* 16 */

static const band_t BANDS[] = {
    {144000000u, 148000000u, 1, "2m"},   {222000000u, 225000000u, 1, "1.25m"},
    {430000000u, 450000000u, 3, "70cm"}, {462550000u, 467725000u, 2, "GMRS"},
    {156000000u, 162550000u, 3, "MAR/WX"}};
#define NBANDS 5
static const uint32_t STEPS[] = {5000, 10000, 12500, 25000, 50000, 100000};
#define NSTEPS 6

/* Aura's KEY_TABLE: phase 0 (no row driven) side keys, then rows PB0, PB4, PB10, PB11, PB9;
   A/B = side 1/2, U/D up/down, M menu, X exit, N band, V vm, W a/b */
static const char KEYS[24] = {0,   'A', 0,   'B', '7', '8', '#', '9', '4', '5', '0', '6',
                              '1', '2', '*', '3', 'M', 'U', 'X', 'D', 'V', 'W', 0,   'N'};
static const uint8_t ROWPIN[6] = {0xFF, 0, 4, 10, 11, 9};

typedef struct {
  const k6_api_t *api;
  const band_t *bd;             /* the band */
  uint8_t *bmp;
  uint32_t step, now;
  uint32_t t_flush, t_open, t_full, t_sfl, t_msg, t_key, t_rep, t_auto;
  int pos, nbb, nv, sel, next, pend, sj, sweeps;
  int sfl, sel_dbm;
  uint8_t zi, qfl, parked, tuned, check, mon, hold, audio, blink, key, raw, full;
  uint32_t r47, odr;
  uint8_t lvl[NB];              /* bars: quick reads, dBm + 174, 0 unknown */
  char ln[40];
  uint32_t khz[NK];             /* the repeater table's valid entries: rx_hz, and their slots */
  uint16_t kslot[NK];
  int nk;
} sf_t;

static uint32_t udiv(uint32_t n, uint32_t d) {
  uint32_t q = 0, bit = 1;
  while (d < n && !(d & 0x80000000u)) d <<= 1, bit <<= 1;
  while (bit) {
    if (n >= d) n -= d, q |= bit;
    d >>= 1, bit >>= 1;
  }
  return q;
}

static char *put(char *p, const char *s) {
  while (*s) *p++ = *s++;
  return p;
}

/* v in decimal, a '.' before its last dot digits */
static char *fix(char *p, uint32_t v, int dot) {
  char t[11];
  int n = 0;
  do {
    uint32_t q = udiv(v, 10);
    t[n++] = (char)('0' + v - 10 * q);
    v = q;
  } while (v || n <= dot);
  while (n) {
    if (dot && n == dot) *p++ = '.';
    *p++ = t[--n];
  }
  return p;
}

static char *sint(char *p, int v) {
  if (v < 0) *p++ = '-', v = -v;
  return fix(p, (uint32_t)v, 0);
}

static char *mhz(char *p, uint32_t hz, int dec) { return fix(p, udiv(hz, dec == 4 ? 100 : 1000), dec); }

/* the bin step in kHz: "12.5k", "25k" */
static char *stepn(char *p, uint32_t step) {
  p = fix(p, udiv(step, 100), 1);
  if (p[-1] == '0') p -= 2;
  *p++ = 'k';
  return p;
}

static void say_end(sf_t *s, char *p) {
  *p = 0;
  s->api->say(s->ln);
}

static int dbm(sf_t *s) { return KM_RSSI_DBM(s->api->fd_read(0x67)); }
static uint32_t hz_of(sf_t *s, int i) {
  return s->bd->lo + (uint32_t)(s->pos + i) * s->step;
}

/* bin i's level in dBm, us after its tune */
static int read_at(sf_t *s, int i, uint32_t us) {
  s->api->tune(hz_of(s, i), WIDE);
  s->api->delay_us(us);
  return dbm(s);
}

static void audio(sf_t *s, int on) {
  if (on == s->audio) return;
  s->audio = (uint8_t)on;
  s->api->fd_write(0x47, on ? AF_FM : AF_MUTE);
  wr(GPIOB + (on ? BSRR : BRR), SPK);
}

/* ---- the keypad ---- */

static int kp_scan(sf_t *s) {
  int ph, k = 0;
  for (ph = 0; ph < 6 && !k; ph++) {
    uint32_t c, col;
    wr(GPIOB + BSRR, ROWS_B);
    if (ph) wr(GPIOB + BRR, 1u << ROWPIN[ph]);
    s->api->delay_us(5);
    c = rd(GPIOC + IDR);
    if (!(c & (1u << 13))) col = 0;
    else if (!(c & (1u << 14))) col = 1;
    else if (!(rd(GPIOF + IDR) & (1u << 6))) col = 2;
    else if (!(rd(GPIOB + IDR) & (1u << 14))) col = 3;
    else continue;
    k = KEYS[4 * ph + col];
  }
  wr(GPIOB + BSRR, ROWS_B);
  return k;
}

/* a key event: the key once it reads the same twice 10 ms apart; UP and DOWN repeat */
static int kp_event(sf_t *s) {
  int k;
  if (s->now - s->t_key < 10) return 0;
  s->t_key = s->now;
  k = kp_scan(s);
  if (k != s->raw) {
    s->raw = (uint8_t)k;
    return 0;
  }
  if (k != s->key) {
    s->key = (uint8_t)k;
    s->t_rep = s->now + 400;
    return k;
  }
  if ((k == 'U' || k == 'D') && (int32_t)(s->now - s->t_rep) >= 0) {
    s->t_rep = s->now + 120;
    return k;
  }
  return 0;
}

/* ---- the window ---- */

/* settled floor: the second lowest of 8 bins' settled reads */
static void measure_sfl(sf_t *s) {
  int j, a = 999, b = 999;
  for (j = 3; j < s->nv; j += 8) {
    int d = read_at(s, j, CHECK_US);
    if (d < a) b = a, a = d;
    else if (d < b) b = d;
  }
  s->sfl = b < 999 ? b : a;
  s->t_sfl = s->api->ms();
  s->tuned = 0;
}

/* after the bar moves: listen there afresh (monitor: just retune) */
static void moved(sf_t *s) {
  s->tuned = 0;
  if (!s->mon) {
    audio(s, 0);
    s->parked = 0;
    s->check = 1;
  }
}

static void limits(sf_t *s) {
  int n = s->nbb - s->pos;
  s->nv = n < NB ? n : NB;
  if (s->sel >= s->nv) s->sel = s->nv - 1;
  if (s->sel < 0) s->sel = 0;
}

/* the window and bar centred (as far as the band allows) on hz, at step STEPS[zi] */
static void window(sf_t *s, uint32_t hz) {
  const band_t *b = s->bd;
  int c, i, mx;
  s->step = STEPS[s->zi];
  s->nbb = (int)udiv(b->hi - b->lo, s->step) + 1;
  if (hz < b->lo) hz = b->lo;
  c = (int)udiv(hz - b->lo + s->step / 2, s->step);
  if (c >= s->nbb) c = s->nbb - 1;
  mx = s->nbb > NB ? s->nbb - NB : 0;
  s->pos = c - NB / 2;
  if (s->pos > mx) s->pos = mx;
  if (s->pos < 0) s->pos = 0;
  s->sel = c - s->pos;
  for (i = 0; i < NB; i++) s->lvl[i] = 0;
  s->next = 0;
  s->pend = 0;
  s->full = 0;
  limits(s);
  measure_sfl(s);
  moved(s);
}

/* pan by k bins (clamped to the band); returns the bins it moved */
static int pan(sf_t *s, int k) {
  int mx = s->nbb > NB ? s->nbb - NB : 0, np = s->pos + k, a, i;
  if (np > mx) np = mx;
  if (np < 0) np = 0;
  a = np - s->pos;
  if (!a) return 0;
  for (i = 0; i < NB; i++) {
    int j = a > 0 ? i : NB - 1 - i, f = j + a;
    s->lvl[j] = f >= 0 && f < NB ? s->lvl[f] : 0;
  }
  s->pos = np;
  s->sel -= a;
  s->next = 0;
  limits(s);
  return a;
}

static int is_peak(sf_t *s, int i) {
  int v = s->lvl[i];
  if (v < s->qfl + PEAK_DB) return 0;      /* (an unknown bin, 0, never is) */
  if (i > 0 && s->lvl[i - 1] > v) return 0;
  if (i + 1 < s->nv && s->lvl[i + 1] >= v) return 0;
  return 1;
}

static void say_sel(sf_t *s) {
  char *p = put(s->ln, "sel ");
  p = mhz(p, hz_of(s, s->sel), 4);
  *p++ = ' ';
  say_end(s, sint(p, s->sel_dbm));
}

/* the first peak from bin j on, going d (+1 up, -1 down) that a settled read confirms (SQ_DB over
   the settled floor; bench run 1 on 2026-11-08 stopped on -103 to -122 dBm bumps without it):
   the bar moves there; 1 if found */
static int seek(sf_t *s, int d, int j) {
  for (; j >= 0 && j < s->nv; j += d)
    if (is_peak(s, j)) {
      int v = read_at(s, j, CHECK_US);
      s->tuned = 0;
      if (v < s->sfl + SQ_DB) continue;   /* a noise bump in the quick reads: not a station */
      s->sel = j;
      s->sel_dbm = v;
      moved(s);
      return 1;
    }
  return 0;
}

/* no peak left in the window going d: pan half a span and look again after the next sweep,
   from the first bin not yet looked at; at the band's edge, give up (the bench build wraps) */
static void hunt(sf_t *s, int d) {
  int o = d > 0 ? s->nv - 1 : 0, a = pan(s, d * NB / 2);
  if (a) {
    s->pend = d;
    s->sj = o - a + d;
    moved(s);
  } else {
    s->t_msg = s->now + MSG_MS;  /* line 5 says "no peak" till then */
#ifdef SURF_AUTO_S
    window(s, s->bd->lo);
#endif
  }
}

static void jump(sf_t *s, int d) {
  if (!s->full) {               /* the window not swept yet: look after its first sweep */
    s->pend = d;
    s->sj = s->sel + d;
  } else if (!seek(s, d, s->sel + d)) {
    hunt(s, d);
  }
}

static void step_bar(sf_t *s, int d) {
  int n = s->sel + d;
  if (n < 0 || n >= s->nv) {
    if (!pan(s, d * NB / 4)) return;
    n = s->sel + d;
    if (n < 0 || n >= s->nv) return;
  }
  s->sel = n;
  moved(s);
}

static void on_key(sf_t *s, int k) {
  switch (k) {
  case 'U': step_bar(s, 1); break;
  case 'D': step_bar(s, -1); break;
  case 'A': jump(s, 1); break;
  case 'B': jump(s, -1); break;
  case 'N':
  case '#':
    s->bd = s->bd + 1 == BANDS + NBANDS ? BANDS : s->bd + 1;
    s->zi = s->bd->zi;
    window(s, (s->bd->lo + s->bd->hi) >> 1);
    break;
  case '1':
  case '3':
    if (k == '1' ? s->zi == 0 : s->zi == NSTEPS - 1) break;
    s->zi = (uint8_t)(s->zi + (k == '1' ? -1 : 1));
    window(s, hz_of(s, s->sel));
    break;
  case '*':
    s->mon ^= 1;
    s->tuned = 0;
    s->parked = s->mon;
    if (s->mon) audio(s, 1);
    else moved(s);
    break;
  case '0':
    s->hold ^= 1;
    break;
  case 'M': say_sel(s); break;
  }
}

/* ---- sweeping ---- */

static void sweep_bin(sf_t *s, int i) {
  int r = read_at(s, i, SETTLE_US) + 174, o = s->lvl[i];
  if (r < 1) r = 1;
  s->lvl[i] = (uint8_t)(r >= o || !o ? r : (o - r > DECAY_DB ? o - DECAY_DB : r));
}

/* the quartile of the window's known bars */
static void floor_q(sf_t *s) {
  uint8_t t[NB];
  int n = 0, i, j, k;
  for (i = 0; i < s->nv; i++)
    if (s->lvl[i]) t[n++] = s->lvl[i];
  if (!n) return;
  k = n >> 2;
  for (i = 0; i <= k; i++)
    for (j = i + 1; j < n; j++)
      if (t[j] < t[i]) {
        uint8_t x = t[i];
        t[i] = t[j], t[j] = x;
      }
  s->qfl = t[k];
}

static void sweep_done(sf_t *s) {
  s->sweeps++;
  s->full = 1;
  floor_q(s);
  if (s->pend) {
    int d = s->pend, j = s->sj;
    s->pend = 0;
    if (j < 0) j = 0;
    if (j >= s->nv) j = s->nv - 1;
    if (!seek(s, d, j)) hunt(s, d);
  }
}

/* a settled read of the bar's channel: opens the audio on a signal */
static void check_bar(sf_t *s) {
  int d;
  s->check = 0;
  d = s->sel_dbm = read_at(s, s->sel, CHECK_US);
  s->tuned = 1;
  if (d < s->sfl) s->sfl = d;
  if (d >= s->sfl + SQ_DB) {
    s->parked = 1;
    s->t_open = s->t_full = s->api->ms();
    audio(s, 1);
  }
}

static void sweep_step(sf_t *s) {
  sweep_bin(s, s->next);
  s->tuned = 0;
  if (++s->next >= s->nv) {
    s->next = 0;
    sweep_done(s);
    if (s->now - s->t_sfl >= SFL_MS) measure_sfl(s);
  }
  if (!(s->next & (BLOCK - 1))) s->check = 1;
}

static void park_step(sf_t *s) {
  int d;
  if (!s->tuned) {
    d = read_at(s, s->sel, CHECK_US);
    s->tuned = 1;
  } else {
    s->api->delay_us(PARK_US);
    d = dbm(s);
  }
  s->sel_dbm = d;
  if (d >= s->sfl + SQ_DB - HYST_DB) s->t_open = s->now;
  if (!s->mon && s->now - s->t_open > HANG_MS) {
    audio(s, 0);
    s->parked = 0;
    return;
  }
  if (!s->hold && s->now - s->t_full >= REFRESH_MS) {
    int i;
    s->api->fd_write(0x47, AF_MUTE);  /* the speaker amp stays on: no pop */
    for (i = 0; i < s->nv; i++) sweep_bin(s, i);
    sweep_done(s);
    s->tuned = 0;
    s->t_full = s->api->ms();
    if (s->audio) s->api->fd_write(0x47, AF_FM);
  }
}

/* ---- the screen ---- */

static void px(uint8_t *b, int x, int r) {
  if ((unsigned)x < 128) b[16 * r + (x >> 3)] |= (uint8_t)(0x80 >> (x & 7));
}

/* bin i's two pixels in row r */
static void pair(uint8_t *b, int i, int r) { b[16 * r + (i >> 2)] |= (uint8_t)(0xC0 >> (2 * (i & 3))); }

/* the explorer's repeater table (c/k6/rt.h, level 5's fl_read): every valid record's rx_hz and
   slot, read once at the start (1024 reads of 8 bytes, ~0.1 s) */
static void rt_load(sf_t *s) {
  uint32_t r[2];
  int i;
  for (i = 0; i < RT_SLOTS && s->nk < NK; i++)
    if (!s->api->fl_read(RT_BASE + 64u * (uint32_t)i, r, 8) && (uint8_t)r[0] == 0xFC && r[1]) {
      s->khz[s->nk] = r[1];
      s->kslot[s->nk++] = (uint16_t)i;
    }
}

/* line 5 on a known repeater: its call, offset (MHz) and TX tone, from its record */
static char *rt_info(sf_t *s, int slot) {
  uint8_t r[44];               /* record bytes 8..51 */
  char *p = s->ln;
  int32_t off;
  int i, tone;
  s->api->fl_read(RT_BASE + 64u * (uint32_t)slot + 8, r, 44);
  for (i = 32; i < 42 && r[i] > ' ' && r[i] < 0x7F; i++) *p++ = (char)r[i];
  off = (int32_t)(r[0] | r[1] << 8 | r[2] << 16 | (uint32_t)r[3] << 24);
  if (off) {
    *p++ = ' ';
    *p++ = off < 0 ? '-' : '+';
    p = fix(p, udiv((uint32_t)(off < 0 ? -off : off) + 50000, 100000), 1);
  }
  tone = r[6] | r[7] << 8;
  if (tone && !(tone & 0x8000)) {
    *p++ = ' ';
    p = fix(p, (uint32_t)tone, 1);
  }
  return p;
}

/* bin i's bar height in pixels, 1..BARH: the quick dB over the window's floor, 7/8 px each */
static int bar_h(sf_t *s, int i) {
  int v = s->lvl[i], h = s->qfl && v > s->qfl ? ((v - s->qfl) * 7) >> 3 : 0;
  if (h < 1) h = 1;
  if (h > BARH) h = BARH;
  return h;
}

static void draw(sf_t *s) {
  const k6_api_t *api = s->api;
  uint8_t *b = s->bmp;
  int i, r, x, k;
  char *p;
  s->t_flush = s->now;
  s->blink ^= 1;
  for (i = 0; i < 16 * ROWS; i++) b[i] = 0;
  for (i = 0; i < s->nv; i++) {
    for (r = ROWS - bar_h(s, i); r < ROWS; r++) pair(b, i, r);
    if (is_peak(s, i)) pair(b, i, BAR0 - 2);
  }
  for (i = 0, k = -1; i < s->nk; i++) {   /* known repeaters: a mark in row 0 */
    uint32_t lo = hz_of(s, 0), f = s->khz[i] + (s->step >> 1);
    int j;
    if (f < lo) continue;
    j = (int)udiv(f - lo, s->step);
    if (j >= s->nv) continue;
    pair(b, j, 0);
    if (j == s->sel) k = s->kslot[i];
  }
  x = 2 * s->sel;
  for (r = 0; r < 3; r++)
    for (i = x - 2 + r; i <= x + 3 - r; i++) px(b, i, r + 1);
  if (s->blink)
    for (r = BAR0; r < ROWS; r++) b[16 * r + (x >> 3)] ^= (uint8_t)(0xC0 >> (x & 7));

  p = put(s->ln, "SURF ");
  p = put(p, s->bd->name);
  *p++ = ' ';
  p = stepn(p, s->step);
  if (s->mon) p = put(p, " MON");
  else if (s->hold) p = put(p, " HOLD");
  *p = 0;
  api->lcd_text(0, s->ln);
  p = mhz(s->ln, hz_of(s, s->sel), 4);
  *p++ = ' ';
  p = sint(p, s->sel_dbm);
  p = put(p, "dBm");
  if (s->audio) p = put(p, " RX");
  *p = 0;
  api->lcd_text(1, s->ln);
  if (k >= 0) {
    p = rt_info(s, k);
  } else {
    char *e;
    p = mhz(s->ln, hz_of(s, 0), 3);
    e = s->ln + 14;
    while (p < e) *p++ = ' ';
    p = mhz(p, hz_of(s, s->nv - 1), 3);
  }
  *p = 0;
  api->lcd_text(5, (int32_t)(s->t_msg - s->now) > 0 ? "no peak" : s->ln);
  api->lcd_wf(b, ROWS, 0);
  api->lcd_flush();
}

int k6_main(const k6_api_t *api) {
  sf_t st, *s = &st;
  uint8_t bmp[16 * ROWS];
  uint32_t n;
  uint8_t *z = (uint8_t *)s;
  unsigned i;
  int k;
  char *p;
  if (api->level < 6) return -3;
  for (i = 0; i < sizeof st; i++) z[i] = 0;
  s->api = api;
  s->bmp = bmp;
  s->r47 = api->fd_read(0x47);
  s->odr = rd(GPIOB + ODR) & SPK;
  s->audio = 1;
  audio(s, 0);
  {
    uint32_t hz = api->rx_hz();
    s->bd = &BANDS[2];
    for (i = 0; i < NBANDS; i++)
      if (hz >= BANDS[i].lo && hz <= BANDS[i].hi) s->bd = &BANDS[i];
    s->zi = s->bd->zi;
    s->now = api->ms();
    rt_load(s);
    window(s, hz);
    p = put(s->ln, "surf ");
    p = put(p, s->bd->name);
    *p++ = ' ';
    p = mhz(p, hz, 3);
    *p++ = ' ';
    say_end(s, stepn(p, s->step));
  }
  s->t_flush = api->ms() - FLUSH_MS;
  for (;;) {
    s->now = api->ms();
    k = kp_event(s);
#ifdef SURF_AUTO_S
    if (!k && !s->pend && !s->check && s->now - s->t_auto >= SURF_AUTO_MS) {
      say_sel(s);
      k = 'A';
      s->t_auto = s->now;
    }
    if (s->now >= SURF_AUTO_S * 1000u) break;
#endif
    if (k == 'X') break;
    if (k) on_key(s, k);
    if (s->check) check_bar(s);
    else if (s->parked) park_step(s);
    else sweep_step(s);
    s->now = api->ms();
    if (s->now - s->t_flush >= FLUSH_MS) draw(s);
  }
  /* put back the audio path; wait (up to 3 s) for the keys' release */
  api->fd_write(0x47, (uint16_t)s->r47);
  wr(GPIOB + (s->odr ? BSRR : BRR), SPK);
  for (i = 0, n = 0; i < 300 && n < 5; i++) {
    n = kp_scan(s) ? 0 : n + 1;
    api->delay_us(10000);
  }
#ifdef SURF_AUTO_S
  /* the last bargraph, for the bench: "bar <first bin MHz> <step>", then the bins, 32 a line, a
     letter a bin: its height a (1 px) to x (24 px) */
  p = put(s->ln, "bar ");
  p = mhz(p, hz_of(s, 0), 4);
  *p++ = ' ';
  say_end(s, stepn(p, s->step));
  for (p = s->ln, k = 0; k < s->nv;) {
    *p++ = (char)('a' - 1 + bar_h(s, k));
    if (!(++k & 31) || k == s->nv) say_end(s, p), p = s->ln;
  }
#endif
  p = put(s->ln, "end sw=");
  say_end(s, fix(p, (uint32_t)s->sweeps, 0));
  return s->sweeps;
}
