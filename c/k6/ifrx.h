/* ifrx: the K6's IF matched-filter receiver for audio-tone FSK (FFSK and 4FSK at 1200 baud, and
   built with IFRX_ONLY = IFRX_4H, 4FSK at 1200 to 1600 baud on a half-baud tone grid),
   working on GP0 code 13's counts (2 * ones - 64 per 64 DMA samples of PB1, ~85.8 kHz).
   docs/k6-if-modem.md has the study; sim/k6if/mcu.py is this receiver in float.

     counts -> a 24-tap complex filter (a low-pass turned up to the IF), worked out only at the
               counts it samples: TL = 18 a symbol, spaced pc / 18 counts, locked to the symbols
            -> turned down by the IF and the AFC's offset (a 64-entry sine table)
            -> |segment . template*| for each tone and audio start phase; max over phases
               (one template stands for two phases: the other is its conjugate)
            -> early-late timing on the winning template, decision-directed AFC from its two
               halves, the symbol and each tone's metric

   Acquisition needs the sender's lead (tones 0 and M-1 in turn, about 1.5 s): ifrx sweeps its
   symbol clock through IFRX_HYP offsets, pc / IFRX_HYP apart, IFRX_HSYMS symbols each, scoring
   each by how well the lead's pattern fits (the expected tone's metric less the other's, for
   the parity that makes it positive). The fit is a plateau about a third of a symbol wide
   (4FSK) whose late side the timing loop would hold, so the lead's timing is taken at the
   plateau's leading edge (back from the best while the fit is over half of it). The matched
   filter also has side peaks a quarter symbol out, so it takes a sweep whose best clears
   IFRX_ACQ_Q and whose edge agrees (to one step) with the
   sweep before. For the next IFRX_LEAD_SYMS symbols it steers timing and AFC on the lead's
   known tones, faster at first, then on its own decisions. If its decisions miss the lead too
   often in any window of those, the acquisition was wrong: it sweeps again.

   No division in the per-count path, no library calls, no writable statics (it builds into a
   K6 RAM module). SPDX-License-Identifier: MIT */
#ifndef IFRX_H
#define IFRX_H
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif

#ifndef IFRX_BAUD
#define IFRX_BAUD 1200      /* symbols a second: 1200, or (IFRX_ONLY == IFRX_4H) 1350, 1440, 1500 or 1600 */
#endif
#if IFRX_BAUD == 1200
#define IFRX_TL 18          /* samples a symbol (template length), about 4 counts apart */
#else
#define IFRX_TL 12
#endif
#define IFRX_NT 24          /* filter taps */
#define IFRX_RING 128       /* counts kept */
#define IFRX_MAXS 8         /* kept templates: tones x start phases / 2 */
#define IFRX_HYP (IFRX_TL * 2 / 3) /* symbol clock offsets a sweep tries */
#define IFRX_HSYMS 32       /* symbols on each */
#define IFRX_ACQ_Q 48       /* the least lead fit a sweep's best takes (share of 256; a bare carrier makes <= 36) */
#define IFRX_LEAD_SYMS 256  /* symbols steered on the known lead after acquisition */
#define IFRX_WIN 32         /* the lead is checked in windows of this many symbols */
#define IFRX_WIN_BAD 8      /* more misses than this in the first: no lead after all */

#ifndef IFRX_STATS
#define IFRX_STATS 1 /* 0: leave acq_off, lead_err, slips and terr unkept (a module that doesn't print them) */
#endif

#define IFRX_FFSK 0
#define IFRX_4FSK 1
#define IFRX_4H 2 /* four tones on a half-baud grid, baud/2 .. 2 baud (only as IFRX_ONLY) */
enum { IFRX_ACQ = 0, IFRX_LEAD = 1, IFRX_TRACK = 2 };

typedef struct ifrx_s {
  /* scalars first: an M0 load reaches 124 bytes into a struct in one instruction */
  const uint8_t *tm;        /* templates [M][P/2][TL], 4-bit re and im (each also stands for its
                               conjugate) */
  uint32_t n;               /* counts pushed so far */
  uint32_t tau, tau_f;      /* this symbol's start: count, Q16 fraction */
  uint32_t th_tau;          /* the IF's phase at tau */
  int32_t i;                /* next sample (-1 .. TL) */
  uint32_t ni;              /* its count */
  int32_t state;
  uint32_t pc_q16;          /* counts a symbol, Q16 */
  uint32_t step_q16;        /* counts a sample, Q16 (pc / TL) */
  uint32_t dphi_if;         /* the IF's turn a count, 2^32 a turn */
  int32_t dphi_f;           /* the AFC's offset a count, same units */
  uint32_t q;               /* symbols, passed over ones too */
  uint32_t nsym;            /* symbols decided since the acquisition */
  int32_t sym;              /* the symbol (tone index; bits: Gray, sym ^ (sym >> 1)) */
  int32_t M, hsh, k;        /* tones, log2(P/2), bits a symbol */
  int32_t gain_sh, afc_sh;
  uint32_t pc_q8;
  int32_t dphi_max;
  uint32_t k_afc;
  int32_t parity;           /* the lead's tone at even q: 0 or M-1 */
  int32_t win_err, win_n;
  /* acquisition */
  int32_t h, hk, prev;      /* the offset being tried, its symbols so far, the last sweep's best */
  int32_t tot;              /* its lead fit so far */
  uint32_t norm;
  uint32_t bfit, bnorm;     /* this sweep's best so far: its fit, and the metrics' sum */
  int32_t bh;               /* its offset */
  /* results */
  uint32_t slips;           /* symbols passed over after a skip */
  int32_t gap;              /* 1 if symbols were passed over since the caller cleared it */
  int32_t terr;             /* the last timing correction, Q16 counts */
  int32_t acq_off;          /* the acquisition's offset (of IFRX_HYP) */
  int32_t acq_q;            /* and how well the lead fitted (of 256) */
  int32_t acq_tries;        /* sweeps */
  int32_t lead_err;         /* decisions that missed the lead's symbol */
  int32_t acq_rej;          /* acquisitions the lead check threw out */
  uint32_t met[4];          /* each tone's metric (|corr|), valid when ifrx_push returns 1 */
  int32_t fit[IFRX_HYP];    /* this sweep's lead fit at each offset, signed by parity */
  int16_t w[IFRX_TL + 2][2];
  int32_t A[IFRX_MAXS][4], A1[IFRX_MAXS][4]; /* each kept template's four sums; at half way */
  int8_t ring[2 * IFRX_RING]; /* the counts, twice over */
#ifdef IFRX_RAMFN
  /* the hot loops, which the caller may point at copies in RAM (code in the K6's flash runs at
     two wait states: about 1.3x the cycles); ifrx_init points them at the originals */
  void (*samp)(const struct ifrx_s *s, uint32_t m, uint32_t th, int16_t out[2]);
  void (*c4)(const int16_t (*w)[2], const uint8_t *t, int32_t a[4], int n);
  const uint32_t *g;        /* the filter's taps, and the sine's quarter: the block reaches */
  const int16_t *qs;        /* its tables only through these, so a copy needs no relocation */
#endif
} ifrx_t;

/* mode IFRX_FFSK or IFRX_4FSK; ks16 = the DMA's rate, 1/16 kS/s (counts = ks16 * 62.5 / 64 a
   second) */
void ifrx_init(ifrx_t *s, int mode, uint32_t ks16);
/* the DMA's rate as measured again (it moves with the CPU's load): the symbol clock's and the
   IF's steps a count */
void ifrx_rate(ifrx_t *s, uint32_t ks16);
/* one count; returns 1 when a symbol has been decided (s->sym, s->met), else 0 */
int ifrx_push(ifrx_t *s, int c);
/* nskip counts were lost (the ring overran) */
void ifrx_skip(ifrx_t *s, uint32_t nskip);
/* the AFC's offset, Hz (approximately: counts at the nominal 85781/s) */
int ifrx_afc_hz(const ifrx_t *s);

#ifdef __cplusplus
}
#endif
#endif
