/* surf: band surfing on the K6 (Jeff's idea, 2026-11-08). Receive only.

   The screen is a bargraph of signal strength (Y) across frequency (X): 63 bins, 2 pixels each,
   drawn as the level-6 waterfall bitmap (30 rows of 128 pixels, y = 21-50). The listening
   frequency is always the middle bin (x = 62-63), a blinking bar (it flips to inverse about 2.5
   times a second) with a caret above it, and the scale moves with it. The scale is a lens (Jeff,
   2026-11-09): the 8 bins each side of the middle are 2.5 kHz apart (+-20 kHz, a quarter of the
   screen), and further out each bin's step grows by the same amount, so the last of the 31 each
   side reaches the span (1 / 3: +-100 kHz to +-5 MHz). Ticks under the caret's rows mark +-20 kHz,
   +-100 kHz and +-1 MHz. A small tick over a bar marks a peak, a dot in the top row a repeater
   the explorer saved (its table, c/k6/rt.h, read once at the start). Bins outside the band stay
   empty. The title line says the band and its channel step, line 1 the listening frequency and
   its level in dBm (a settled read), line 5 the scale's edges, or with the bar on a saved
   repeater its call, offset and TX tone ("N0CALL +5.0 103.5").

   Keys (the module scans the keypad itself: the module API has key_down but no key identity;
   the matrix is Aura's src/device/keypad.rs at edf3b98, rows PB0/PB4/PB10/PB11/PB9, columns
   PC13/PC14/PF6/PB14, side keys read with no row driven):
     UP / DOWN       the listening frequency one channel up / down (held: repeats); the scale
                     moves with it, each bin keeping the level of the old bin nearest it
     SIDE1 / SIDE2   (the orange side keys) jump to the next peak above / below that a settled
                     read confirms as a signal (SQ_DB over the settled floor), on the channel
                     raster; after the next sweep the loudest bin of the lens, if it is another
                     channel, moves it once more (an outer bin is coarse). With none left on
                     screen, the scale's edge becomes the middle and it looks again after the
                     next sweep, on up the band until it finds one or reaches the band's edge
     BAND or #       next band: 2m, 1.25m, 70cm, GMRS, marine + NOAA WX
     1 / 3           zoom in / out: the span +-100 kHz, 250 kHz, 500 kHz, 1 MHz, 2.5 MHz, 5 MHz
     *               monitor: audio open on the bar all the time, no sweeping
     0               hold: no refresh sweeps while a signal plays (default: one every 5 s)
     MENU            say the bar's frequency and level on the serial port
     EXIT            leave (the module waits for the key's release, so the pager doesn't see it)

   One receiver both sweeps and listens, so the module listens first: while the bar's channel
   is quiet it sweeps (audio muted), and every CHK_MS it goes back to the bar for a settled
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
   SURF_AUTO_MS (each peak it finds says "sel" with the settled read that confirmed it and the
   settled floor), it wraps to the band's bottom at
   the top, and it leaves after SURF_AUTO_S; keys still work. Before it ends it says its last
   bargraph ("bar 441.2000 1000k lens": the middle and the span, then two lines of a letter a
   bin, tools/k6_screen.py draws them).

   Lines (K6 MOD say ...): "surf 70cm 440.850 25k", "sel 441.2000 -71 -127" (MENU, or the
   bench build's jumps: the bar, its settled dBm, the settled floor), "end sw=N".
   Writes REG_47 and GPIOB bit 2 (speaker) and the keypad rows' GPIOB BSRR/BRR bits, all put back.
   Reads the SPI flash (level 5's fl_read), never writes it. No TX. The bitmap (480 B), the state
   (~750 B) and a move's copy of the old scale (~320 B) live on the stack, about 1.6 KB of the
   3 KB a module has at its call; the store holds only the code.
   Returns the sweeps done, or -3 (an image below level 6).
   SPDX-License-Identifier: MIT */
#ifndef K6_MODULE_LEVEL
#define K6_MODULE_LEVEL 6
#define K6_MODULE_OPT z
#endif
#include "../k6_mod.h"

#define NB 64                   /* bins in the arrays */
#define NV 63                   /* bins on screen: the middle one and 31 each side */
#define C 31                    /* the middle bin, the listening frequency (x = 62-63) */
#define KI 8                    /* the lens: 8 bins each side of the middle 2.5 kHz apart */
#define U_HZ 2500u              /* the lens's step, and the unit of every bin's offset */
#define ROWS 30                 /* bitmap rows (KM_WF_ROWS) */
#define BAR0 6                  /* first bar row: caret rows 1-3, peak ticks row 4 */
#define BARH (ROWS - BAR0)
#define SETTLE_US 3000          /* a sweep read after its tune (Aura's spectrum, wide) */
#define CHECK_US 12000          /* a settled read */
#define CHK_MS 70               /* sweeping: a check of the bar's channel this often */
#define SUB_HZ 20000u           /* a wide bin: one quick read this far apart across it */
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
  uint8_t ch, si;               /* channel step (x 2.5 kHz), default span (SPANS index) */
  char name[6];
} band_t;                       /* 16 */

static const band_t BANDS[] = {
    {144000000u, 148000000u, 4, 2, "2m"},   {222000000u, 225000000u, 4, 2, "1.25m"},
    {430000000u, 450000000u, 10, 3, "70cm"}, {462550000u, 467725000u, 5, 4, "GMRS"},
    {156000000u, 162550000u, 10, 3, "MAR/WX"}};
#define NBANDS 5
/* the span: the last bin's offset from the middle, x 2.5 kHz (100 kHz to 5 MHz) */
static const uint16_t SPANS[] = {40, 100, 200, 400, 1000, 2000};
#define NSPANS 6
/* ruler ticks, kHz from the middle */
static const uint16_t TICKS[] = {20, 100, 1000};

/* Aura's KEY_TABLE: phase 0 (no row driven) side keys, then rows PB0, PB4, PB10, PB11, PB9;
   A/B = side 1/2, U/D up/down, M menu, X exit, N band, V vm, W a/b */
static const char KEYS[24] = {0,   'A', 0,   'B', '7', '8', '#', '9', '4', '5', '0', '6',
                              '1', '2', '*', '3', 'M', 'U', 'X', 'D', 'V', 'W', 0,   'N'};
static const uint8_t ROWPIN[6] = {0xFF, 0, 4, 10, 11, 9};

typedef struct {
  const k6_api_t *api;
  const band_t *bd;             /* the band */
  uint8_t *bmp;
  uint32_t ch, cen, now;         /* channel step, the middle (listening) frequency */
  uint32_t t_flush, t_open, t_full, t_sfl, t_msg, t_key, t_rep, t_auto, t_chk;
  int next, pend, sj, sweeps;
  int sfl, sel_dbm;
  uint8_t si, qfl, parked, tuned, check, mon, hold, audio, blink, key, raw, full, refine, stale;
  uint16_t off[C + 1];          /* bin C+-k's offset from the middle, x 2.5 kHz */
  uint32_t hz[NB];              /* each bin's frequency, 0 outside the band */
  uint32_t pk[NB];              /* where in its range a bin's loudest read was */
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

/* a step in kHz: "12.5k", "25k" */
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

/* the level at hz in dBm, us after its tune */
static int read_hz(sf_t *s, uint32_t hz, uint32_t us) {
  s->api->tune(hz, WIDE);
  s->api->delay_us(us);
  return dbm(s);
}

static int read_at(sf_t *s, int i, uint32_t us) { return read_hz(s, s->hz[i], us); }

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

/* ---- the scale ---- */

/* the lens's offsets for the span: 0..KI one step each, then step 1 + c*j for the j-th bin past
   the lens, c such that bin C lands on the span: off = KI + j + (span - C) * j(j+1) / (n(n+1)) */
static void lens(sf_t *s) {
  uint32_t u = SPANS[s->si] - C, n = C - KI, j;
  for (j = 0; j <= KI; j++) s->off[j] = (uint16_t)j;
  for (j = 1; j <= n; j++) s->off[KI + j] = (uint16_t)(KI + j + udiv(u * j * (j + 1), n * (n + 1)));
}

/* bin i's distance from the middle, in bins */
static int dist(int i) { return i < C ? C - i : i - C; }

/* the step around bin i (Hz) */
static uint32_t sp(sf_t *s, int i) {
  int a = dist(i);
  return U_HZ * (a < C ? s->off[a + 1] - s->off[a] : s->off[C] - s->off[C - 1]);
}

/* the frequencies bin i stands for: halfway to its neighbours */
static void cov(sf_t *s, int i, uint32_t *lo, uint32_t *hi) {
  int a = dist(i);
  uint32_t in = a ? U_HZ * (s->off[a] - s->off[a - 1]) / 2 : U_HZ / 2, out = sp(s, i) / 2;
  *lo = s->hz[i] - (i < C ? out : in);
  *hi = s->hz[i] + (i < C ? in : out);
}

/* the bin nearest f, if f is within half its step of it; -1 if none */
static int bin_of(sf_t *s, uint32_t f) {
  int i, b = -1;
  uint32_t d, bd = ~0u;
  for (i = 0; i < NV; i++)
    if (s->hz[i]) {
      d = s->hz[i] > f ? s->hz[i] - f : f - s->hz[i];
      if (d < bd) bd = d, b = i;
    }
  return b >= 0 && 2 * bd <= sp(s, b) ? b : -1;
}

/* settled floor: the second lowest of 8 bins' settled reads */
static void measure_sfl(sf_t *s) {
  int j, a = 999, b = 999;
  for (j = 3; j < NV; j += 8) {
    int d;
    if (!s->hz[j]) continue;
    d = read_at(s, j, CHECK_US);
    if (d < a) b = a, a = d;
    else if (d < b) b = d;
  }
  if (a == 999) a = read_at(s, C, CHECK_US);   /* (a band edge: the middle is always in it) */
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

/* the middle on hz (in the band, on its channel raster) and the scale around it; each bin keeps
   the loudest old bin whose loudest read falls in its range */
static void recentre(sf_t *s, uint32_t hz) {
  const band_t *b = s->bd;
  uint32_t old[NV], lo, hi;
  uint8_t lv[NV];
  int i, j;
  if (hz < b->lo) hz = b->lo;
  if (hz > b->hi) hz = b->hi;
  s->cen = b->lo + udiv(hz - b->lo + s->ch / 2, s->ch) * s->ch;
  if (s->cen > b->hi) s->cen -= s->ch;
  for (i = 0; i < NV; i++) {
    uint32_t o = U_HZ * s->off[dist(i)], f = i < C ? s->cen - o : s->cen + o;
    old[i] = s->pk[i];
    lv[i] = s->lvl[i];
    s->hz[i] = s->pk[i] = f >= b->lo && f <= b->hi ? f : 0;
    s->lvl[i] = 0;
  }
  s->stale = 1;
  for (i = 0; i < NV; i++) {
    if (!s->hz[i]) continue;
    cov(s, i, &lo, &hi);
    for (j = 0; j < NV; j++)
      if (lv[j] > s->lvl[i] && old[j] >= lo && old[j] < hi) s->lvl[i] = lv[j], s->pk[i] = old[j];
  }
}

/* a fresh scale on hz, at the band's channel step and the span SPANS[si] */
static void window(sf_t *s, uint32_t hz) {
  int i;
  s->ch = s->bd->ch * U_HZ;
  lens(s);
  for (i = 0; i < NB; i++) s->lvl[i] = 0;
  recentre(s, hz);
  s->t_chk = s->api->ms();
  s->next = 0;
  s->pend = 0;
  s->full = 0;
  s->refine = 0;
  measure_sfl(s);
  moved(s);
}

static int is_peak(sf_t *s, int i) {
  int v = s->lvl[i];
  if (v < s->qfl + PEAK_DB) return 0;      /* (an unknown bin, 0, never is) */
  if (i > 0 && s->lvl[i - 1] > v) return 0;
  if (i + 1 < NV && s->lvl[i + 1] >= v) return 0;
  return 1;
}

/* "sel <bar MHz> <its settled dBm> <the settled floor>" */
static void say_sel(sf_t *s) {
  char *p = put(s->ln, "sel ");
  p = mhz(p, s->cen, 4);
  *p++ = ' ';
  p = sint(p, s->sel_dbm);
  *p++ = ' ';
  say_end(s, sint(p, s->sfl));
}

/* bin j's channel: the middle frequency its loudest read would make */
static uint32_t chan(sf_t *s, int j) {
  uint32_t lo = s->bd->lo;
  return lo + udiv(s->pk[j] - lo + s->ch / 2, s->ch) * s->ch;
}

/* the first peak from bin j on, going d (+1 up, -1 down) that a settled read confirms (SQ_DB over
   the settled floor; bench run 1 on 2026-11-08 stopped on -103 to -122 dBm bumps without it)
   on a channel other than the middle's: the middle moves there; 1 if found */
static int seek(sf_t *s, int d, int j) {
  for (; j >= 0 && j < NV; j += d)
    if (is_peak(s, j) && chan(s, j) != s->cen) {
      int v = read_hz(s, chan(s, j), CHECK_US);
      s->tuned = 0;
      if (v < s->sfl + SQ_DB) continue;   /* a noise bump in the quick reads: not a station */
      recentre(s, s->pk[j]);
      s->sel_dbm = v;
      s->refine = 1;
      moved(s);
#ifdef SURF_AUTO_S
      say_sel(s);                         /* the bench: the read that confirmed it */
#endif
      return 1;
    }
  return 0;
}

/* no peak left on screen going d: the scale's last bin that way becomes the middle, and it looks
   again after the next sweep; at the band's edge, give up (the bench build wraps) */
static void hunt(sf_t *s, int d) {
  int e = d > 0 ? NV - 1 : 0;
  while (e != C && !s->hz[e]) e -= d;
  if (e != C && chan(s, e) != s->cen) {
    recentre(s, s->hz[e]);
    s->pend = d;
    s->sj = C + d;
    moved(s);
  } else {
    s->t_msg = s->now + MSG_MS;  /* line 5 says "no peak" till then */
#ifdef SURF_AUTO_S
    window(s, s->bd->lo);
#endif
  }
}

static void jump(sf_t *s, int d) {
  if (!s->full) {               /* the scale not swept yet: look after its first sweep */
    s->pend = d;
    s->sj = C + d;
  } else if (!seek(s, d, C + d)) {
    hunt(s, d);
  }
}

static void step_bar(sf_t *s, int d) {
  uint32_t f = d > 0 ? s->cen + s->ch : s->cen - s->ch;
  if (f < s->bd->lo || f > s->bd->hi) return;
  recentre(s, f);
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
    s->si = s->bd->si;
    window(s, (s->bd->lo + s->bd->hi) >> 1);
    break;
  case '1':
  case '3':
    if (k == '1' ? s->si == 0 : s->si == NSPANS - 1) break;
    s->si = (uint8_t)(s->si + (k == '1' ? -1 : 1));
    lens(s);
    recentre(s, s->cen);
    s->pend = 0;
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

/* bin i's quick level: the loudest of reads SUB_HZ apart across its range (the wide filter's
   width: a coarse bin far out would miss a carrier between two bins), and where it was */
static void sweep_bin(sf_t *s, int i) {
  uint32_t lo, hi, f, at = s->hz[i];
  int r, o = s->lvl[i], v;
  cov(s, i, &lo, &hi);
  if (hi - lo <= SUB_HZ) {
    r = read_at(s, i, SETTLE_US);
  } else {
    for (r = -999, f = lo + SUB_HZ / 2; f < hi; f += SUB_HZ)
      if (f >= s->bd->lo && f <= s->bd->hi && (v = read_hz(s, f, SETTLE_US)) > r) r = v, at = f;
    if (r == -999) r = read_at(s, i, SETTLE_US);
  }
  if (r + 174 >= o || !o) s->pk[i] = at;
  r += 174;
  if (r < 1) r = 1;
  s->lvl[i] = (uint8_t)(r >= o || !o ? r : (o - r > DECAY_DB ? o - DECAY_DB : r));
}

/* the quartile of the scale's known bars */
static void floor_q(sf_t *s) {
  uint8_t t[NB];
  int n = 0, i, j, k;
  for (i = 0; i < NV; i++)
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
  s->stale = 0;
  floor_q(s);
  if (s->refine) {
    /* after a jump: the loudest bin of the lens, if that is another channel, is the station
       (the bin the jump came from may have been a coarse one far out) */
    int i, b = C;
    s->refine = 0;
    for (i = C - KI; i <= C + KI; i++)
      if (s->hz[i] && s->lvl[i] > s->lvl[b]) b = i;
    if (b != C && s->lvl[b] > s->lvl[C] + 1 && chan(s, b) != s->cen) {
      recentre(s, s->pk[b]);
      moved(s);
    }
  }
  if (s->pend) {
    int d = s->pend, j = s->sj;
    s->pend = 0;
    if (j < 0) j = 0;
    if (j >= NV) j = NV - 1;
    if (!seek(s, d, j)) hunt(s, d);
  }
}

/* a settled read of the bar's channel: opens the audio on a signal */
static void check_bar(sf_t *s) {
  int d, i;
  s->check = 0;
  s->t_chk = s->api->ms();
  d = s->sel_dbm = read_at(s, C, CHECK_US);
  s->tuned = 1;
  if (d < s->sfl) s->sfl = d;
  if (d >= s->sfl + SQ_DB) {
    if (s->stale) {
      /* the scale just moved: sweep it once before the audio opens (no sweeps while it plays) */
      for (i = 0; i < NV; i++)
        if (s->hz[i]) sweep_bin(s, i);
      s->next = 0;
      sweep_done(s);
      if (s->check) return;             /* that moved the middle again: check there */
      s->tuned = 0;
    }
    s->parked = 1;
    s->t_open = s->t_full = s->api->ms();
    audio(s, 1);
  }
}

static void sweep_step(sf_t *s) {
  if (s->hz[s->next]) {
    sweep_bin(s, s->next);
    s->tuned = 0;
  }
  if (++s->next >= NV) {
    s->next = 0;
    sweep_done(s);
    if (s->now - s->t_sfl >= SFL_MS) measure_sfl(s);
  }
  if (s->api->ms() - s->t_chk >= CHK_MS) s->check = 1;
}

static void park_step(sf_t *s) {
  int d;
  if (!s->tuned) {
    d = read_at(s, C, CHECK_US);
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
  if (!s->hold && (s->stale || s->now - s->t_full >= REFRESH_MS)) {   /* (stale: a zoom) */
    int i;
    s->api->fd_write(0x47, AF_MUTE);  /* the speaker amp stays on: no pop */
    for (i = 0; i < NV; i++)
      if (s->hz[i]) sweep_bin(s, i);
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
  for (i = 0; i < RT_SLOTS && s->nk < NK; i++) {
    s->api->fl_read(RT_BASE + 64u * (uint32_t)i, r, 8);   /* (in range: it can't refuse) */
    if ((uint8_t)r[0] == 0xFC && r[1]) {
      s->khz[s->nk] = r[1];
      s->kslot[s->nk++] = (uint16_t)i;
    }
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
  for (i = 0; i < NV; i++) {
    if (!s->hz[i]) continue;
    for (r = ROWS - bar_h(s, i); r < ROWS; r++) pair(b, i, r);
    if (is_peak(s, i)) pair(b, i, BAR0 - 2);
  }
  for (i = 0, k = -1; i < s->nk; i++) {   /* known repeaters: a mark in row 0 */
    int j = bin_of(s, s->khz[i]);
    if (j < 0) continue;
    pair(b, j, 0);
    if (j == C) k = s->kslot[i];
  }
  for (i = 0; i < (int)sizeof TICKS / 2; i++) {   /* the ruler: a 2 px tick each side */
    uint32_t o = 1000u * TICKS[i];
    int j, e;
    for (e = -1; e <= 1; e += 2) {
      j = bin_of(s, e < 0 ? s->cen - o : s->cen + o);
      if (j >= 0 && j != 0 && j != NV - 1) px(b, 2 * j + (e > 0), 2), px(b, 2 * j + (e > 0), 3);
    }
  }
  x = 2 * C;
  for (r = 0; r < 3; r++)
    for (i = x - 2 + r; i <= x + 3 - r; i++) px(b, i, r + 1);
  if (s->blink)
    for (r = BAR0; r < ROWS; r++) b[16 * r + (x >> 3)] ^= (uint8_t)(0xC0 >> (x & 7));

  p = put(s->ln, "SURF ");
  p = put(p, s->bd->name);
  *p++ = ' ';
  p = stepn(p, s->ch);
  if (s->mon) p = put(p, " MON");
  else if (s->hold) p = put(p, " HOLD");
  *p = 0;
  api->lcd_text(0, s->ln);
  p = mhz(s->ln, s->cen, 4);
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
    for (i = 0; !s->hz[i]; i++) {}
    for (r = NV - 1; !s->hz[r]; r--) {}
    p = mhz(s->ln, s->hz[i], 3);
    e = s->ln + 14;
    while (p < e) *p++ = ' ';
    p = mhz(p, s->hz[r], 3);
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
    s->si = s->bd->si;
    s->now = api->ms();
    rt_load(s);
    window(s, hz);
    p = put(s->ln, "surf ");
    p = put(p, s->bd->name);
    *p++ = ' ';
    p = mhz(p, hz, 3);
    *p++ = ' ';
    say_end(s, stepn(p, s->ch));
  }
  s->t_flush = api->ms() - FLUSH_MS;
  for (;;) {
    s->now = api->ms();
    k = kp_event(s);
#ifdef SURF_AUTO_S
    if (!k && !s->pend && !s->check && s->now - s->t_auto >= SURF_AUTO_MS) {
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
  /* the last bargraph, for the bench: "bar <middle MHz> <span> lens", then the bins, 32 a line,
     a letter a bin: its height a (1 px) to x (24 px), '-' outside the band */
  p = put(s->ln, "bar ");
  p = mhz(p, s->cen, 4);
  *p++ = ' ';
  p = stepn(p, U_HZ * SPANS[s->si]);
  say_end(s, put(p, " lens"));
  for (p = s->ln, k = 0; k < NV;) {
    *p++ = s->hz[k] ? (char)('a' - 1 + bar_h(s, k)) : '-';
    if (!(++k & 31) || k == NV) say_end(s, p), p = s->ln;
  }
#endif
  p = put(s->ln, "end sw=");
  say_end(s, fix(p, (uint32_t)s->sweeps, 0));
  return s->sweeps;
}
