/* midi: a small music player for the K6's speaker, from RAM (no reflash). Receive side only: it
   never keys the transmitter. Songs come from MIDI files (or the text scores in tools/midi_songs/),
   which tools/k6_midi.py turns into a compact table at the end of this module's code; CI's
   mod-midi.bin carries the public-domain tunes in midi_songs.h, and `k6_midi.py patch` puts
   other songs in its place without a compiler (docs/k6-midi.md).

   Sound: four voices mixed at 16 kHz, each a 256-entry wavetable (harmonics 1-4, built in the
   scratch RAM at the start) with a plucked envelope: a 4 ms attack to the note's velocity, a
   quick fall to half, a slow fade while the note is held, and a short release when it ends.
   The audio path is spksweep's (Aura's voice prompts): PA11 = TIM1_CH4, 48 kHz PWM carrier,
   an update every 3 periods, the module writing CCR4 on each update event; PB2 is the speaker
   amplifier. REG_47 (the FD6818's AF output) is muted while it plays.

   Keys: a press skips to the next song, a hold of a second stops. Between songs the screen
   (level 6) shows the title; it never draws while a song plays, since a flush holds the CPU for
   20-40 ms. The green LED (level 7) blinks on each note.

   The song table (all little-endian, read a byte at a time):
     0  "K6MIDI" 1 0   magic, format 1, reserved
     8  u16 bytes      the whole table
     10 u8 songs, u8 volume (a voice at full velocity swings volume / 8192 x 32767 PWM counts)
     12 u16 offset     of each song, from the table's start
   a song: u16 unit (samples at 16 kHz per time unit), its title (NUL-terminated, up to 21
   characters), then events:
     0x00-0x7F  note on, this MIDI note number, then its length in units (a MIDI-style
                variable-length number, up to 3 bytes)
     0x80-0xBF  wait (b & 0x3F) + 1 units
     0xC0       wait a variable-length number of units
     0xC1 v     velocity of the notes that follow (1-127)
     0xFF       end (any other byte ends the song too)

   say lines: "midi songs=N bytes=B vol=V", "midi play I/N <title>" for each song, then
   "midi done songs=N notes=K late=L".
   Returns the songs played to the end, -2 (a held key stopped it), -3 (scratch too small),
   -4 (TIM1 already running: something else is playing), -5 (no song table).
   SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 2
#define K6_MODULE_OPT z
#include "../k6_mod.h"
#include "midi_songs.h"             /* const uint8_t midi_table[] in section .k6tail: last in the code */

#define RCC_APB2ENR 0x40021018u     /* TIM1EN bit 11 */
#define GPIOA_MODER 0x48000000u
#define GPIOA_OSPEEDR 0x48000008u
#define GPIOA_AFRH 0x48000024u
#define GPIOB_ODR 0x48000414u
#define GPIOB_BSRR 0x48000418u
#define GPIOB_BRR 0x48000428u
#define SPK (1u << 2)
#define TIM1 0x40012C00u
#define TIM_CR1 (TIM1 + 0x00)
#define TIM_DIER (TIM1 + 0x0C)
#define TIM_SR (TIM1 + 0x10)
#define TIM_EGR (TIM1 + 0x14)
#define TIM_CCMR2 (TIM1 + 0x1C)
#define TIM_CCER (TIM1 + 0x20)
#define TIM_PSC (TIM1 + 0x28)
#define TIM_ARR (TIM1 + 0x2C)
#define TIM_RCR (TIM1 + 0x30)
#define TIM_CCR4 (TIM1 + 0x40)
#define TIM_BDTR (TIM1 + 0x44)

#define TOP 2000u                   /* 96 MHz / 2000 = the 48 kHz carrier */
#define MID (TOP / 2)
#define LO 16                       /* the output is clipped to LO .. TOP - LO */
#define NV 4                        /* voices */
#define BLK 16                      /* samples a block: 1 ms; events and envelopes run once a block */
#define GAP_BLKS 600                /* silence between songs, ms */
#define HOLD_MS 1000                /* a key held this long stops the player */
#define PRESS_MS 20                 /* and one held this long is a press */
#define LED_MS 60

static uint32_t rd(uint32_t a) { return *(volatile uint32_t *)a; }
static void wr(uint32_t a, uint32_t v) { *(volatile uint32_t *)a = v; }

typedef struct {
  uint32_t ph, inc;                 /* phase (top 8 bits index the wavetable), per sample */
  int32_t left;                     /* samples of the note still to play */
  int32_t env, peak;                /* envelope, velocity << 16 at the top */
  int32_t g;                        /* env >> 12, this block's gain */
  uint32_t stage;                   /* 0 free, 1 attack, 2 held, 3 release */
} voice_t;

/* the phase step of MIDI notes 120-131 at 16 kHz (2^32 f / 16000); lower octaves shift right */
static const uint32_t top_inc[12] = {2247346494u, 2380980670u, 2522561148u, 2672560440u,
                                     2831479154u, 2999847666u, 3178227890u, 3367215155u,
                                     3567440188u, 3779571220u, 4004316221u, 4242425254u};

/* sin(pi / 2 * i / 64), Q15 (spksweep's) */
static const int16_t quarter_sin[65] = {
  0, 804, 1608, 2410, 3212, 4011, 4808, 5602, 6393, 7179, 7962, 8739, 9512, 10278, 11039, 11793,
  12539, 13279, 14010, 14732, 15446, 16151, 16846, 17530, 18204, 18868, 19519, 20159, 20787, 21403,
  22005, 22594, 23170, 23731, 24279, 24811, 25329, 25832, 26319, 26790, 27245, 27683, 28105, 28510,
  28898, 29268, 29621, 29956, 30273, 30571, 30852, 31113, 31356, 31580, 31785, 31971, 32137, 32285,
  32412, 32521, 32609, 32678, 32728, 32757, 32767};

/* the harmonics' amplitudes, Q15: 1st to 4th */
static const int16_t harm[4] = {19000, 7500, 4000, 2000};

/* sin(2 pi ph / 2^32), Q15: the quarter table, linearly interpolated */
static int32_t sin_q15(uint32_t ph) {
  uint32_t q = ph >> 30, x = (ph >> 14) & 0xFFFF;
  uint32_t i, f;
  int32_t a, v;
  if (q & 1) x = 0x10000 - x;
  i = x >> 10, f = x & 1023;
  a = quarter_sin[i];
  v = i < 64 ? a + (int32_t)(((quarter_sin[i + 1] - a) * (int32_t)f) >> 10) : a;
  return q & 2 ? -v : v;
}

/* wait for the next update event, then queue v for the one after it; 1 if it was late */
static int out(uint32_t v) {
  int late = (int)(rd(TIM_SR) & 1);
  while (!(rd(TIM_SR) & 1)) {
  }
  wr(TIM_SR, 0xFFFEu);              /* rc_w0: clears UIF only */
  wr(TIM_CCR4, v);
  return late;
}

static uint32_t rd16(const uint8_t *p) { return (uint32_t)p[0] | (uint32_t)p[1] << 8; }

/* a variable-length number (7 bits a byte, high bit = more), up to 3 bytes */
static uint32_t vlq(const uint8_t **pp) {
  const uint8_t *p = *pp;
  uint32_t v = 0;
  int i;
  for (i = 0; i < 3; i++) {
    uint8_t b = *p++;
    v = v << 7 | (b & 0x7Fu);
    if (!(b & 0x80)) break;
  }
  *pp = p;
  return v;
}

static char *put(char *p, const char *s) {
  while (*s) *p++ = *s++;
  return p;
}

static char *dec(char *p, uint32_t v) {
  static const uint32_t pw[] = {100000, 10000, 1000, 100, 10, 1};
  int i, started = 0;
  for (i = 0; i < 6; i++) {
    int d = 0;
    while (v >= pw[i]) v -= pw[i], d++;
    if (d || started || i == 5) *p++ = (char)('0' + d), started = 1;
  }
  return p;
}

/* the voice for a new note: a free one, else the quietest (it keeps its phase: no click) */
static voice_t *take(voice_t *v) {
  voice_t *best = v;
  int i;
  for (i = 0; i < NV; i++) {
    if (!v[i].stage) return &v[i];
    if (v[i].env < best->env) best = &v[i];
  }
  return best;
}

static void note_on(voice_t *v, uint32_t n, uint32_t len, int32_t vel) {
  uint32_t o = (n * 2731u) >> 15;   /* n / 12 for n < 128, with no divide (the M0 has none) */
  n -= o * 12;
  v->inc = top_inc[n] >> (10 - o);
  v->left = (int32_t)len;
  v->peak = vel << 16;
  v->stage = 1;
}

/* one block's envelopes, then each voice's gain for the block */
static void envelopes(voice_t *v) {
  int i;
  for (i = 0; i < NV; i++, v++) {
    if (v->stage == 1) {
      v->env += v->peak >> 2;
      if (v->env >= v->peak) v->env = v->peak, v->stage = 2;
    } else if (v->stage == 2) {
      v->env -= v->env > (v->peak >> 1) ? v->env >> 5 : v->env >> 10;
    } else if (v->stage == 3) {
      v->env -= v->env >> 3;
      if (v->env < 4096) v->env = 0, v->stage = 0;
    }
    if (v->stage == 1 || v->stage == 2) {
      v->left -= BLK;
      if (v->left <= 0) v->stage = 3;
    }
    v->g = v->env >> 12;
  }
}

typedef struct {
  const k6_api_t *api;
  int level;
  uint32_t vol, late, notes, blocks, held, armed, led;
  int key;                          /* 1 a press (next song), 2 a hold (stop) */
} run_t;

/* the keys, once a block: a press on release, a hold after HOLD_MS */
static void keys(run_t *r) {
  int k;
  if (r->level >= 4) k = r->api->key_poll();
  else if (!(r->blocks & 7)) k = r->api->key_down();   /* ~60 us: one late sample in eight blocks */
  else return;
  if (!k) {
    if (r->armed && r->held >= PRESS_MS && !r->key) r->key = 1;
    r->held = 0, r->armed = 1;
  } else if (r->armed) {
    r->held += r->level >= 4 ? 1 : 8;
    if (r->held >= HOLD_MS) r->key = 2;
  }
}

/* play one song; 0 at its end, 1 skipped, 2 stopped */
static int play(run_t *r, const uint8_t *song, const int16_t *wt) {
  voice_t v[NV];
  const uint8_t *p = song + 2;
  uint32_t unit = rd16(song), i, k;
  int32_t wait = 0, vel = 100, acc;
  int end = 0;

  for (i = 0; i < NV; i++) v[i].ph = 0, v[i].inc = 0, v[i].left = 0, v[i].env = 0, v[i].peak = 0,
                            v[i].g = 0, v[i].stage = 0;
  while (*p) p++;                   /* the title */
  p++;
  for (;;) {
    keys(r);
    if (r->key) return r->key;
    while (!end && wait <= 0) {
      uint8_t b = *p++;
      if (b < 0x80) {
        note_on(take(v), b, vlq(&p) * unit, vel);
        r->notes++;
        if (r->level >= 7) r->api->led(1), r->led = LED_MS;
      } else if (b < 0xC0) {
        wait += (int32_t)(((b & 0x3Fu) + 1) * unit);
      } else if (b == 0xC0) {
        wait += (int32_t)(vlq(&p) * unit);
      } else if (b == 0xC1) {
        vel = *p++ & 0x7F;
      } else {
        end = 1;
      }
    }
    envelopes(v);
    for (k = 0; k < BLK; k++) {
      uint32_t o;
      acc = 0;
      for (i = 0; i < NV; i++) {
        v[i].ph += v[i].inc;
        acc += wt[v[i].ph >> 24] * v[i].g;
      }
      acc = ((acc >> 11) * (int32_t)r->vol) >> 13;
      o = acc < LO - (int32_t)MID ? LO : acc > (int32_t)MID - LO ? TOP - LO : (uint32_t)((int32_t)MID + acc);
      r->late += (uint32_t)out(o);
    }
    wait -= BLK;
    r->blocks++;
    if (r->led && !--r->led && r->level >= 7) r->api->led(0);
    if (!(r->blocks & 1023)) r->api->ms();   /* the core sums its run time only in ms() and delay_us() */
    if (end && !v[0].stage && !v[1].stage && !v[2].stage && !v[3].stage) return 0;
  }
}

/* silence (the carrier at half) for n blocks, still watching the keys */
static int quiet(run_t *r, uint32_t n) {
  uint32_t k;
  while (n--) {
    keys(r);
    if (r->key) return r->key;
    for (k = 0; k < BLK; k++) out(MID);
    r->blocks++;
    if (!(r->blocks & 1023)) r->api->ms();
  }
  return 0;
}

static void screen(run_t *r, const char *title, uint32_t i, uint32_t n) {
  char ln[24], *p;
  if (r->level < 6) return;
  r->api->lcd_text(0, " MIDI player");
  r->api->lcd_text(1, "");
  r->api->lcd_text(2, title);
  p = put(ln, "song ");
  p = dec(p, i);
  p = put(p, " of ");
  p = dec(p, n);
  *p = 0;
  r->api->lcd_text(3, ln);
  r->api->lcd_text(4, "");
  r->api->lcd_text(5, "key next, hold stop");
  r->api->lcd_flush();
}

int k6_main(const k6_api_t *api) {
  const uint8_t *t = midi_table;
  char line[64], title[22], *p;
  uint32_t apb2, moder, ospeed, afrh, ccmr2, ccer, bdtr, spk, i, j, n, bytes, sbytes, played = 0;
  int16_t *wt;
  uint16_t r47;
  int ret, how = 0;
  run_t r;

  __asm__("" : "+r"(t));            /* the compiler mustn't read the built-in table at build time:
                                       k6_midi.py patch swaps it for another */
  if (t[0] != 'K' || t[1] != '6' || t[2] != 'M' || t[3] != 'I' || t[4] != 'D' || t[5] != 'I' ||
      t[6] != 1 || !t[10]) {
    api->say("midi: no song table");
    return -5;
  }
  n = t[10], bytes = rd16(t + 8);
  wt = (int16_t *)api->scratch(&sbytes);
  if (sbytes < 512) {
    api->say("midi: scratch too small");
    return -3;
  }
  if (rd(TIM_CR1) & 1) {
    api->say("midi: TIM1 busy");
    return -4;
  }
  r.api = api, r.level = api->level, r.vol = t[11], r.late = 0, r.notes = 0, r.blocks = 0;
  r.held = 0, r.armed = 0, r.led = 0, r.key = 0;
  p = put(line, "midi songs=");
  p = dec(p, n);
  p = put(p, " bytes=");
  p = dec(p, bytes);
  p = put(p, " vol=");
  p = dec(p, r.vol);
  *p = 0;
  api->say(line);

  for (i = 0; i < 256; i++) {       /* the wavetable: harmonics 1-4 */
    int32_t s = 0;
    for (j = 0; j < 4; j++) s += (sin_q15((i * (j + 1)) << 24) * harm[j]) >> 15;
    wt[i] = (int16_t)s;
  }

  apb2 = rd(RCC_APB2ENR), moder = rd(GPIOA_MODER), ospeed = rd(GPIOA_OSPEEDR);
  afrh = rd(GPIOA_AFRH), spk = rd(GPIOB_ODR) & SPK;
  r47 = api->fd_read(0x47);
  api->fd_write(0x47, r47 & ~0x0F00u);   /* AF 0: the chip's audio muted */

  wr(RCC_APB2ENR, apb2 | (1u << 11));
  ccmr2 = rd(TIM_CCMR2), ccer = rd(TIM_CCER), bdtr = rd(TIM_BDTR);
  wr(TIM_CR1, 0);
  wr(TIM_DIER, 0);
  wr(TIM_PSC, 0);
  wr(TIM_ARR, TOP);
  wr(TIM_RCR, 2);                    /* an update every 3 carrier periods: 16 kHz */
  wr(TIM_CCR4, MID);
  wr(TIM_CCMR2, (ccmr2 & ~0xFF00u) | (6u << 12) | (1u << 11));   /* PWM mode 1, preload */
  wr(TIM_CCER, 1u << 12);            /* CC4E, active high */
  wr(TIM_EGR, 1);                    /* load ARR, RCR, PSC */
  wr(TIM_SR, 0);
  wr(GPIOA_AFRH, (afrh & ~(0xFu << 12)) | (2u << 12));          /* PA11 AF2 = TIM1_CH4 */
  wr(GPIOA_OSPEEDR, ospeed | (3u << 22));
  wr(GPIOA_MODER, (moder & ~(3u << 22)) | (2u << 22));
  wr(TIM_CR1, (1u << 7) | 1);        /* ARPE, CEN */
  wr(TIM_BDTR, bdtr | (1u << 15));   /* MOE */
  for (i = 0; i < 480; i++) out(MID);   /* 30 ms, as stock: the RC filter settles first */
  wr(GPIOB_BSRR, SPK);

  for (i = 0; i < n && how != 2; i++) {
    const uint8_t *s = t + rd16(t + 12 + 2 * i);
    const uint8_t *q = s + 2;
    for (j = 0; j < 21 && q[j]; j++) title[j] = (char)q[j];
    title[j] = 0;
    screen(&r, title, i + 1, n);
    p = put(line, "midi play ");
    p = dec(p, i + 1);
    *p++ = '/';
    p = dec(p, n);
    *p++ = ' ';
    p = put(p, title);
    *p = 0;
    api->say(line);
    r.key = 0;
    how = play(&r, s, wt);
    if (r.led && r.level >= 7) api->led(0), r.led = 0;
    if (!how) played++;
    if (how != 2) {
      r.key = 0;
      if (quiet(&r, GAP_BLKS) == 2) how = 2;
    }
  }
  ret = how == 2 ? -2 : (int)played;

  out(MID);
  if (!spk) wr(GPIOB_BRR, SPK);
  for (i = 0; i < 320; i++) out(MID);   /* 20 ms, as stock, before the pin lets go */
  wr(TIM_BDTR, bdtr & ~(1u << 15));
  wr(TIM_CR1, 0);
  wr(TIM_DIER, 0);
  wr(TIM_CCER, ccer);
  wr(TIM_CCMR2, ccmr2);
  wr(GPIOA_MODER, moder);
  wr(GPIOA_OSPEEDR, ospeed);
  wr(GPIOA_AFRH, afrh);
  wr(RCC_APB2ENR, apb2);
  api->fd_write(0x47, r47);

  p = put(line, "midi done songs=");
  p = dec(p, played);
  p = put(p, " notes=");
  p = dec(p, r.notes);
  p = put(p, " late=");
  p = dec(p, r.late);
  *p = 0;
  api->say(line);
  return ret;
}
