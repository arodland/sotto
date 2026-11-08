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

   A spur scores -100: it ranks last and is the first to go when the list is full. So does a
   skirt: a candidate whose every over ended within 2 s of an over EX_SKIRT_DB louder within
   EX_SKIRT_HZ (the K6 hears a -22 dBm carrier 12.5-62.5 kHz either side at -58 to -79, on air
   2026-11-07).
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
#define EX_F_KNOWN 0x20          /* in the saved repeater table (ex_know): listed after the new ones */
/* 0x40 is the app's (explore.rs EX_F_LOOKED) */
#define EX_F_DMR 0x80            /* a listen heard DMR syncs there (xdmr.c) */
/* 0x40 is the app's (EXPLORE: the table's call has been looked up) */
#define EX_SKIRT_HZ 100000       /* a carrier this close and EX_SKIRT_DB louder, ending with it: a skirt */
#define EX_SKIRT_DB 25
#define EX_STALE_MS 600000u      /* a one-over '?' this long without another leaves the screen list */

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
  uint8_t skirts;                /* overs that were a louder neighbour's skirt */
  uint8_t flags;
  int8_t score;
  char grade;                    /* '?' 'R' 'r' 's' */
  char call[10];                 /* from ex_cw, NUL padded */
  uint8_t listens;               /* times Listen for ID went to it on its own (ex_pick_id) */
  uint16_t heard_s;              /* seconds those listens lasted (ex_heard) */
  uint8_t clean;                 /* long automatic listens with no DMR since it was marked
                                    EX_F_DMR (ex_dmr_heard); in the struct's tail padding */
} ex_cand_t;

typedef struct {
  ex_cand_t *c;
  uint16_t n, cap;
  uint32_t tol_hz;               /* overs within this of a candidate are its own (default 5 kHz) */
  uint32_t *known;               /* the saved table's frequencies (ex_know), known_cap of them */
  uint16_t known_n, known_cap;
  uint32_t now;                  /* the latest over's end (ex_over), for EX_STALE_MS */
} ex_t;

/* band plan rows: an output range, its usual offset, and whether it is a simplex range */
typedef struct {
  uint32_t lo_hz, hi_hz;
  int32_t off_hz;                /* 0 for a simplex row */
  uint8_t simplex, pad;
  uint16_t raster_khz;           /* the usual channel raster from lo_hz (0: any) */
} ex_plan_t;

void ex_init(ex_t *e, ex_cand_t *store, unsigned cap);
/* the scan's H line: an over at hz from start_ms for dur_ms at level dBm. The candidate, or 0
   (the list full of better ones) */
ex_cand_t *ex_over(ex_t *e, uint32_t hz, uint32_t start_ms, uint32_t dur_ms, int level);
void ex_spur(ex_t *e, uint32_t hz, int lvl);              /* the scan's X line */
/* within 2.5 kHz of a harmonic of the K6's own 24 MHz MCU clock (144.000 the loudest) */
int ex_own_clock(uint32_t hz);
void ex_input(ex_t *e, uint32_t hz, int32_t off_hz);       /* a user heard at hz + off_hz */
void ex_tone(ex_t *e, uint32_t hz, uint16_t tone);
void ex_cw(ex_t *e, uint32_t hz, const char *call);
ex_cand_t *ex_find(ex_t *e, uint32_t hz);
/* Known repeaters (Jeff 2026-11-07: new ones above a rule, known ones below): ex_known_store
   hands over room for cap frequencies (ex_init leaves none), ex_know adds one (Save, a CW write,
   each saved entry at the start) and marks its candidate, now and when it first appears */
void ex_known_store(ex_t *e, uint32_t *buf, unsigned cap);
void ex_know(ex_t *e, uint32_t hz);
/* the entry was deleted from the table (key 3 twice): new again */
void ex_unknow(ex_t *e, uint32_t hz);
/* rescore one (after any event the caller adds itself) */
void ex_grade(ex_cand_t *c);
/* the i-th best (0 first) by score, then most overs, then most recent; 0 past the end. New
   candidates come before known ones (EX_F_KNOWN), and in each group the ignored ones last. */
ex_cand_t *ex_rank(ex_t *e, unsigned i);
/* the same, leaving out skirts (every over a louder neighbour's: Jeff 2026-11-07, "lump close
   together signals together") and stale one-over '?'s (EX_STALE_MS), for the list on the
   screen; and how many that leaves */
ex_cand_t *ex_view(ex_t *e, unsigned i);
unsigned ex_view_n(const ex_t *e);
/* on its band plan row's channel raster (within 1 kHz), or on a row without one, or off the plan */
int ex_on_raster(uint32_t hz);
/* Which of the frequencies up now (up_hz, n of them: the scan's hot entries that are keyed) the
   automatic Listen for ID should go to, by Jeff's rule (2026-11-07): only candidates still
   without a callsign that could be repeaters (not a spur, a skirt, a simplex frequency, an
   ignored or an 's' one); of those the one listened to fewest times, then the first in up_hz
   (xs_up gives them loudest first: Jeff 2026-11-07, strongest first). Its listens count goes
   up, so it moves to the back: the next loudest goes when it's up again, and two repeaters that
   ID at the same time take turns and neither starves. 0: none to listen to. */
ex_cand_t *ex_pick_id(ex_t *e, const uint32_t *up_hz, unsigned n);
/* The automatic listen's cap, by Jeff's rule (2026-11-07: "reduces the time spent listening for
   CW the longer a given station has actually been listened to"): max_s halved for each
   EX_HEARD_STEP_S already spent on it, min_s at least; ex_heard adds a listen's seconds. */
#define EX_HEARD_STEP_S 60
unsigned ex_listen_cap(const ex_cand_t *c, unsigned max_s, unsigned min_s);
void ex_heard(ex_t *e, uint32_t hz, unsigned s);
/* DMR by default (Jeff 2026-11-08: "mute by default until listening to it for a longer duration
   reveals that it's not actually a dmr repeater"): a listen that heard DMR marks the row
   EX_F_DMR, and the next listens there start with the speaker shut (xd_known). An automatic
   listen of at least EX_CLEAN_S with no DMR counts towards clearing the mark; EX_CLEAN_N of
   them in a row clear it, and any DMR starts the count again. s: the listen's seconds (0 for a
   listen by hand, which never counts: it may sit on a dead carrier). Returns 1 when this
   listen cleared the mark (the caller clears the saved entry's RT_F_DMR too), else 0. */
#define EX_CLEAN_S 10
#define EX_CLEAN_N 3
int ex_dmr_heard(ex_t *e, uint32_t hz, int dmr, unsigned s);
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
