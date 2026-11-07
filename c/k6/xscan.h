/* xscan: the K6 explorer's fast two-stage sweep for the core image, a block of bins at a time
   (Phase 3 of the explorer design; docs/k6-explore-app.md).

   The same sweep as the scan module (c/k6/mod/scan.c, docs/k6-scan.md: coarse read, a floor per
   bin, confirm, refine, a hot list revisited every hot_ms, spurs), cut into steps so the main
   loop keeps the keys, the screen and the power switch: xs_step runs at most XS_BLOCK bins (about
   80 ms at 1.5 ms quick reads) and returns. Each over that ends goes to the candidate list
   (ex_over), each spur to ex_spur, and both go out as the module's report lines (H, X, N, S, F),
   so tools/k6_explore.py replay and tools/k6_scan.py decode read a serial log of the app as they
   read the module's.

   The radio is reached through a k6_api_t (c/k6/k6_mod.h), the module table: say, fd_read, tune,
   delay_us, ms and key_poll. The core passes its own table, so this file runs unchanged on the
   host (tests/test_k6_xscan.py) with a simulated receiver.

   Memory is the caller's: xs_t (the state and the hot list) and a byte a bin for the floors plus
   one waterfall row, xs_mem_bytes(). No division, no library calls, no writable statics.
   SPDX-License-Identifier: MIT */
#ifndef XSCAN_H
#define XSCAN_H
#include <stdint.h>
#include "k6_mod.h"
#include "explore.h"
#ifdef __cplusplus
extern "C" {
#endif

#define XS_NSEG 4
#define XS_HOT 10
#define XS_BLOCK 32
#define XS_DESENSE_DB 15   /* a floor read this far under the last is desense (held once) */
#define XS_COLS 128        /* waterfall row: max-pooled to this many columns a segment at most */

typedef struct {
  uint32_t lo_hz, step_hz;
  uint16_t nbins, snap_hz;   /* snap: the channel raster a refined hit snaps to (0: none) */
} xs_seg_t;

typedef struct {
  uint8_t lvl, lo, hi;       /* this over: its level, quick reads' lowest and highest (dBm + 174) */
  uint8_t olo, ohi;          /* every over's level: lowest, highest */
  uint8_t miss, spur, pad;
  uint16_t bin, overs;       /* the coarse bin (all segments counted), for its quick floor */
  uint32_t hz;               /* centre (0: a free entry) */
  uint32_t last, up;         /* ms last seen up; ms this over started (0: down) */
  uint32_t on;               /* ms up, overs that ended */
} xs_hot_t;                  /* 28 */

/* phases */
enum { XS_FLOORS = 0, XS_SWEEP = 1 };
/* xs_step results */
enum { XS_RUN = 0, XS_ROW = 1, XS_KEY = 2 };

typedef struct {
  /* set up by the caller before xs_init (xs_defaults fills the usual values) */
  uint8_t nseg, wide, noise_db, hit_db, refine, say_rows, quiet;
  uint8_t refloor_s;         /* read a segment's floor again this often (0: only at the start) */
  uint16_t quick_us, confirm_us, hot_ms, spur_s;
  xs_seg_t seg[XS_NSEG];
  /* the rest is the sweep's */
  const k6_api_t *api;
  ex_t *ex;                  /* overs and spurs go here (0: none) */
  uint8_t *fl;               /* quick floor a bin, dBm + 174, all segments */
  uint8_t *row;              /* the waterfall row of the segment being swept: dB over the floor */
  uint32_t sweeps, bins, rev, t0;
  uint8_t sfl[XS_NSEG], qfl[XS_NSEG];
  uint32_t fl_t[XS_NSEG];    /* ms each segment's floor was last read */
  /* where the sweep is */
  uint8_t phase, g, cols, last_g;   /* last_g: the segment whose row is complete (XS_ROW) */
  uint16_t b, base;          /* the bin in segment g, segment g's first bin in fl */
  uint32_t hz, conf, hits, col, acc, sweep_t0;
  int16_t sf;
  uint16_t last_ms;          /* the last finished segment sweep's time */
  xs_hot_t hot[XS_HOT];
  char line[64];
  uint8_t held, pad2[3];      /* segments whose last floor read was held as desense (a bit each) */
} xs_t;

/* the usual settings: 1.5 ms quick reads, 12 ms full, wide, noise 6 dB, hits 10 dB, refine on */
void xs_defaults(xs_t *s);
/* bytes of floors and row for s's segments */
uint32_t xs_mem_bytes(const xs_t *s);
/* Check the segments and take the memory (mem, at least xs_mem_bytes). The floors are read on
   the first xs_step. 0, or -11 (a bad segment), -8 (too little memory). */
int xs_init(xs_t *s, const k6_api_t *api, ex_t *ex, uint8_t *mem, uint32_t bytes);
/* One step: the floors (the first call), or up to XS_BLOCK bins and the hot list if it's due.
   XS_ROW when a segment's sweep ended this step (s->row holds its row, s->cols columns, segment
   s->last_g), XS_KEY when a key was down (nothing more done), else XS_RUN. */
int xs_step(xs_t *s);
/* Back from a gap (a Listen or Try that had the receiver for a while): the segment being swept
   reads its floor again and is swept from its start, so its bins aren't judged against a floor
   from before the gap (bench 3: 2 m's floor rose 8 dB during a 2 minute listen, and the rest of
   the segment came back as a wall of new hits). The hot list stays. */
void xs_resume(xs_t *s);
/* end every over that's up (their H lines and ex_over calls), as at the end of a run */
void xs_close(xs_t *s);
/* the hot list's summary lines (T) and the Z line, as the module's end of run */
void xs_summary(xs_t *s);
/* sizeof(xs_t), for a caller that can't see the struct (the Rust side places it in the store) */
uint32_t xs_state_bytes(void);
typedef struct {
  uint32_t sweeps, bins, ms;   /* full passes over every segment, bins swept, ms since the floors */
  uint16_t last_ms;            /* the last segment sweep's time */
  uint8_t g, phase;            /* the segment being swept, XS_FLOORS or XS_SWEEP */
  int16_t floor_dbm[XS_NSEG];  /* each segment's settled floor */
  uint8_t hot, up;             /* hot entries (not spurs), and how many are up now */
  uint8_t pad[2];
} xs_info_t;
void xs_info(const xs_t *s, xs_info_t *out);
/* the hot entries that are up now worth a listen (not spurs, skirts, or weak), loudest first:
   their centres into out, at most max; how many */
unsigned xs_up(const xs_t *s, uint32_t *out, unsigned max);
/* reads the level at hz now (a tune and a full read), for the details screen */
int xs_level(xs_t *s, uint32_t hz);

#ifdef __cplusplus
}
#endif
#endif
