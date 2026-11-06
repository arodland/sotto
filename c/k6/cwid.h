/* cwid: decode a repeater's Morse ID from a keyed/unkeyed signal sampled every few ms.

   The K6's MCU can't hear the receive audio, so the input is one of the FD6818's own
   detectors, polled: the audio level in dB (REG_6F, or REG_64 through cw_db) through the
   cw_lvl tracker, or the tone detector's key state. cw_feed debounces the key, keeps the
   mark and space lengths of one transmission and, once the key has been up CW_END_MS,
   decodes them (in 4 ms units, a byte each): the dot length comes from the marks themselves (dots and dashes, 1:3), with
   the detector's lag taken out, so any speed from about 8 to 40 wpm works without a setting.
   cw_call then picks a callsign out of the text.

   No division or library calls (it builds into a K6 RAM module: the M0 has no divide
   instruction and a module has no run-time library), no writable statics.
   SPDX-License-Identifier: MIT */
#ifndef CWID_H
#define CWID_H
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif

#ifndef CW_NDUR
#define CW_NDUR 96          /* marks and spaces kept per transmission (AG7EW SOTTO TEST: 61) */
#endif
#define CW_QSHIFT 2         /* lengths are kept in units of CW_Q = 4 ms (a byte: up to 1020 ms) */
#define CW_Q (1 << CW_QSHIFT)
#define CW_TEXT 40          /* decoded text, NUL-terminated */
#define CW_GLITCH_MS 8      /* a key change must hold this long (edges are back-dated) */
#define CW_END_MS 1200      /* key up this long ends a transmission */
#define CW_MINMARK_MS 16    /* a shorter mark is noise: the decoder folds it into the gap */
#define CW_MAXMARK_MS 900   /* a longer mark is not Morse (a carrier tone, a voice ID) */

/* cw_feed results */
enum { CW_NONE = 0, CW_MSG = 1 };
/* cw_t.why for a CW_MSG */
enum { CW_END = 0, CW_FULL = 1, CW_LONG = 2 };

typedef struct {
  uint32_t edge_t;          /* time of the last committed key change */
  uint32_t cand_t;          /* when the input first differed from the committed state */
  uint8_t key, cand;        /* committed state, 1 while the input differs */
  uint8_t active;           /* 1 inside a transmission */
  uint8_t why;              /* CW_END, CW_FULL, CW_LONG (for the last CW_MSG) */
  uint16_t n;               /* durations held: d[0] a mark, d[1] a space, ... */
  uint16_t nd;              /* lengths of the last decoded transmission (d[] still holds them) */
  uint16_t unit;            /* dot length of the last decode, ms */
  int16_t lag;              /* how much longer the detector makes a mark than it was, ms */
  uint8_t wpm;              /* 1200 / unit */
  uint8_t tlen;
  char text[CW_TEXT];
  uint8_t d[CW_NDUR];       /* in CW_Q ms */
} cw_t;

#ifndef CW_LVL_LOOK
#define CW_LVL_LOOK 16      /* polls of look-ahead (a power of 2): 32 ms at 2 ms */
#endif
/* the level tracker for a dB input: a key-up and a key-down level, keyed above the middle
   once they're CW_MIN_CONTRAST dB and CW_NOISE_X x the noise + CW_NOISE_DB apart */
typedef struct {
  int32_t lo, hi;           /* key-up and key-down levels, Q8 dB */
  int32_t dev;              /* the samples' mean distance from their level, Q8 dB */
  int32_t sm;               /* the input, smoothed, Q8 dB */
  int16_t ring[CW_LVL_LOOK];/* the last inputs, Q4 dB: cw_lvl answers for the oldest */
  uint8_t ri, fill;
  uint8_t on, init;
} cw_lvl_t;

#define CW_MIN_CONTRAST 6   /* dB between key down and key up before anything keys */
#ifndef CW_LVL_TAU
#define CW_LVL_TAU 5        /* the levels follow their samples with tau 2^CW_LVL_TAU ms */
#endif
#ifndef CW_NOISE_X
#define CW_NOISE_X 3        /* ... and CW_NOISE_X x the noise + CW_NOISE_DB */
#endif
#ifndef CW_NOISE_DB
#define CW_NOISE_DB 2
#endif

void cw_init(cw_t *c);
/* one sample at t ms (any start, wrapping 32-bit): returns CW_MSG when a transmission ended
   and c->text holds it (c->d[0 .. nd-1] its lengths in CW_Q ms, until the next sample). */
int cw_feed(cw_t *c, uint32_t t, int keyed);
/* force the end of a transmission now (e.g. the listen is over): CW_MSG if there was one */
int cw_flush(cw_t *c, uint32_t t);

void cw_lvl_init(cw_lvl_t *l);
/* a level in dB, dt ms since the last one: 1 while keyed, CW_LVL_LOOK polls late (a fixed
   delay, so the lengths don't change) */
int cw_lvl(cw_lvl_t *l, int db, uint32_t dt);
int cw_lvl_contrast(const cw_lvl_t *l);   /* dB, peak minus floor */

/* 20 log10(v) in whole dB (within 0.7 dB), for a linear amplitude register; 0 for v <= 1 */
int cw_db(uint32_t v);

/* call flags (the repeater table's byte 50 keeps its own layout; these are what was heard) */
enum { CW_CALL_P = 1, CW_CALL_M = 2, CW_CALL_R = 4 };
/* The first callsign in text, as heard ("WA7ABC/R"), into call (up to 10 characters + NUL).
   Returns its length (0: none) and sets *flags (portable /P, mobile /M, repeater /R, /RPT or a
   separate R or RPT after the call). */
int cw_call(const char *text, char call[11], uint8_t *flags);

/* helpers for report lines: unsigned decimal, returns the end; and a division by shifts */
char *cw_utoa(char *p, uint32_t v);
uint32_t cw_div(uint32_t n, uint32_t d);

#ifdef __cplusplus
}
#endif
#endif
