/* explore: the K6 explorer's candidate list (Phase 2 of the explorer design, "The explorer: from
   activity to a list of repeaters to try"; docs/k6-explorer-next.md).

   The scan (c/k6/mod/scan.c) reports each transmission it follows: centre, start, length,
   level. The explorer keeps one candidate per frequency, adds up the cues that tell a repeater
   from a simplex station, guesses the offset and grades each one:

     cue                                   points  how
     same level on every over              +3      3+ overs within EX_SAME_DB (a repeater's
                                                    output always comes from one site)
     levels that wander                    -2      3+ overs spread over EX_WANDER_DB
     input heard at the offset             +4      ex_input: the scan hopped to rx + offset while
                                                    the output was up and heard the user there
     on a repeater output sub-band         +2      the band plan (ex_plan)
     a simplex range or calling frequency  -3      the band plan
     a tone on the output                  +1      ex_tone
     a CW ID                               +2      ex_cw (a callsign that ends /R or RPT: +1 more)
     a short regular over                  +1      3+ overs of 2-15 s, 5-20 min apart (like an
                                                    ID every 10 minutes)

   A spur scores -100: it ranks last and is the first to go when the list is full.
   Grades: 's' a spur, a simplex frequency or under 0 points; '?' fewer than EX_MIN_OVERS overs
   with no input heard and no call; 'R' 6 points or more; otherwise 'r'. The offset comes from ex_input when there is one, else
   from the band plan row the output falls in.

   RAM only (the caller gives the array): nothing here writes flash. ex_to_rt fills a repeater
   table record (c/k6/rt.h) for Save. No division, no library calls, no writable statics.
   SPDX-License-Identifier: MIT */
#ifndef EXPLORE_H
#define EXPLORE_H
#include <stdint.h>
#include "rt.h"
#ifdef __cplusplus
extern "C" {
#endif

#define EX_SAME_DB 4
#define EX_WANDER_DB 10
#define EX_MIN_OVERS 2

/* candidate flags */
#define EX_F_IGNORED 0x01
#define EX_F_TRIED 0x02
#define EX_F_OFF_HEARD 0x04      /* the offset comes from ex_input, not the plan */
#define EX_F_SIMPLEX 0x08        /* in a simplex range */
#define EX_F_SPUR 0x10

typedef struct {
  uint32_t hz;
  uint32_t first_ms, last_ms;    /* first over's start, last over's end */
  uint32_t on_ms;                /* total time up */
  int32_t off_hz;                /* offset guess, 0 = simplex or none */
  uint16_t overs;
  uint16_t tone;                 /* CTCSS 0.1 Hz or DCS | RT_TONE_DCS, from ex_tone */
  int16_t lo, hi;                /* lowest and highest over level, dBm */
  uint32_t prev_start;           /* last over's start, for the ID-interval cue */
  uint8_t ids;                   /* short overs at an ID-like interval */
  uint8_t flags;
  int8_t score;
  char grade;                    /* '?' 'R' 'r' 's' */
  char call[10];                 /* from ex_cw, NUL padded */
} ex_cand_t;

typedef struct {
  ex_cand_t *c;
  uint16_t n, cap;
  uint32_t tol_hz;               /* overs within this of a candidate are its own (default 5 kHz) */
} ex_t;

/* band plan rows: an output range, its usual offset, and whether it is a simplex range */
typedef struct {
  uint32_t lo_hz, hi_hz;
  int32_t off_hz;                /* 0 for a simplex row */
  uint8_t simplex;
} ex_plan_t;

void ex_init(ex_t *e, ex_cand_t *store, unsigned cap);
/* the scan's H line: an over at hz from start_ms for dur_ms at level dBm. The candidate, or 0
   (the list full of better ones) */
ex_cand_t *ex_over(ex_t *e, uint32_t hz, uint32_t start_ms, uint32_t dur_ms, int level);
void ex_spur(ex_t *e, uint32_t hz);                       /* the scan's X line */
void ex_input(ex_t *e, uint32_t hz, int32_t off_hz);       /* a user heard at hz + off_hz */
void ex_tone(ex_t *e, uint32_t hz, uint16_t tone);
void ex_cw(ex_t *e, uint32_t hz, const char *call);
ex_cand_t *ex_find(ex_t *e, uint32_t hz);
/* rescore one (after any event the caller adds itself) */
void ex_grade(ex_cand_t *c);
/* the i-th best (0 first) by score, then most overs, then most recent; 0 past the end. Ignored
   candidates come last. */
ex_cand_t *ex_rank(ex_t *e, unsigned i);
/* a repeater table record from a candidate, for Save (id 0: rt_put makes it a new entry) */
void ex_to_rt(const ex_cand_t *c, rt_rec_t *r);

/* the US band plan rows (ARRL 2 m and 1.25 m, WWARA 70 cm): from memory, to be checked against
   the current plans before they go in a release (the design doc says the same) */
extern const ex_plan_t ex_plan_us[];
extern const unsigned ex_plan_us_n;

#ifdef __cplusplus
}
#endif
#endif
