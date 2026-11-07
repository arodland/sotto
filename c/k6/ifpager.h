/* ifpager: the K6 pager's coded 4FSK receiver (PHY G, docs/k6-if-fec.md, modem/k6g.py).

   The pager's XL packets (128 bytes, whitened, CRC: sotto_pager.h) come as 4FSK at 1200 baud
   with a rate-1/2 K=7 code. The IF matched filter (ifrx.c) decides each symbol and gives each
   tone's metric; this turns them into soft bits, finds each packet's sync word, undoes the
   interleave and runs a soft Viterbi decoder, one or two trellis steps a symbol (a few percent
   of the K6's M0), with a sliding traceback in 640 bytes.

     ifp_init / ifp_sym: the decoder alone (for tests on a PC and inside the session)
     ifp_session: a receive session on the K6: GP0 code 13's stream on PB1 through DMA channel
       1 into ifrx and ifp_sym, every packet into sp_packet, until the burst ends

   No heap, no writable statics, no division in the per-symbol path. SPDX-License-Identifier: MIT */
#ifndef IFPAGER_H
#define IFPAGER_H
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif

#define IFP_PAYLOAD 128                 /* an XL packet */
#define IFP_STEPS (IFP_PAYLOAD * 8 + 8)  /* trellis steps a packet: its bits and 8 zeros */
#define IFP_BLOCK 86                    /* coded bits an interleave block (43 symbols) */
#define IFP_HALF (IFP_BLOCK / 2)
#define IFP_SYNC_LEN 24
#define IFP_SYNC 0x907E6Au              /* 1 = tone 3, 0 = tone 0, the first symbol in bit 23 */
#ifndef IFP_SYNC_MISS
#define IFP_SYNC_MISS 6                 /* symbols of the 24 that may be wrong (the lead is 10 or more from it) */
#endif
#define IFP_TB_DEPTH 64                 /* traceback depth */
#define IFP_TB_OUT 16                   /* bits a traceback decides */
#define IFP_TB_RING (IFP_TB_DEPTH + IFP_TB_OUT)
#define IFP_LLR_ONE 32                  /* a symbol as strong as the running mean */

enum { IFP_HUNT = 0, IFP_DATA = 1 };

typedef struct {
  int32_t state;
  uint32_t hist;          /* the last 24 decisions: 1 = tone 3, as the sync word */
  uint32_t bad;           /* ... and 1 where the decision was an inner tone */
  int32_t nsym;           /* data symbols of the packet coming in */
  int32_t sk, sb;         /* its next symbol's place in its block, and the block's buffer */
  uint32_t ready;         /* trellis steps whose blocks are in (all packets) */
  uint32_t step;          /* trellis steps run (all packets) */
  int32_t ls;             /* ... of the packet being decoded */
  int32_t bk, bb;         /* the next step's place in its block, and the block's buffer */
  int32_t out;            /* bits decided (traced back) of that packet */
  int32_t ri;             /* the next step's entry in dec */
  int32_t have;           /* 1: pkt holds a decoded packet (the caller clears it) */
  uint32_t mean_q4;       /* running mean of the winning metric, Q4 */
  uint32_t recip;         /* (LLR_ONE << 20) / mean, every 16 symbols */
  uint32_t syms;          /* symbols seen */
  uint32_t syncs;         /* sync words found */
  uint32_t last_sync;     /* syms at the last one */
  int32_t miss;           /* the last sync's wrong symbols */
  int16_t pm[2][64];      /* path metrics, this step's and the next's */
  int32_t cur;            /* which of pm is this step's */
  uint32_t dec[IFP_TB_RING][2]; /* each step's 64 decisions */
  int8_t llr[2][IFP_BLOCK];     /* two interleave blocks: the one filling, the one decoding */
  uint8_t pkt[IFP_PAYLOAD];     /* the packet's bytes as decided */
} ifp_t;

void ifp_init(ifp_t *p);
/* one symbol (tone 0..3, the four tones' metrics): its soft bits into the block filling, or the
   sync hunt (returns 1 on a sync word found). Cheap: the decoding is ifp_work's */
int ifp_sym(ifp_t *p, int tone, const uint32_t met[4]);
/* one trellis step, when one is ready (about 30 us on the K6: the session runs it whenever the
   receiver is idle). Returns 1 when that step finished a packet: p->pkt holds it (whitened, as
   the chip's FIFO would give it: sp_packet takes it), and the next packet waits until the caller
   clears p->have; 0 after any other step; -1 with nothing to do */
int ifp_work(ifp_t *p);

#ifndef IFP_HOST
#include "k6_mod.h"
/* results of a session */
#define IFP_RES_MAX 64
typedef struct {
  int32_t ret;            /* packets passed to sp_packet, or -2 a key, -4 PB1 busy, -5 REG_33
                             refused, -6 no lock, -7 TIM17 or DMA busy */
  uint32_t ms;            /* the session's length */
  uint32_t syncs, syms, lost; /* syncs found, symbols decided, counts lost (ring overruns) */
  int32_t acq_q, acq_rej, afc_hz, idle_pct, ks; /* lead fit, rejected acquisitions, AFC, CPU idle, kS/s */
  int32_t nres;
  int8_t res[IFP_RES_MAX];/* each packet's sp_packet result */
  int8_t miss[IFP_RES_MAX];/* and its sync's wrong symbols */
  int32_t ram;            /* bytes of the receiver's hot loops run from RAM (0: from flash) */
  int32_t gap_us, gap_i;  /* the longest the loop was away (a burst and what came before it), and at
                             which sample of the symbol it came back */
  int32_t lag;            /* the most trellis steps the decoder fell behind (over IFP_HALF: blocks lost) */
} ifp_result_t;
/* A receive session (store, then play: the caller plays once it returns). ring: at least
   IFP_RING bytes for the DMA, 4-byte aligned (the pager's PWM ring, 1280 B, is free while nothing
   plays).
   Waits wait_ms for the lead, then runs until no sync word for two packets' time, a key, or
   max_ms. Uses the module API's level 2 calls (fd_read/fd_write, ms, key_poll). Puts back
   REG_33, REG_34, TIM17 and DMA channel 1. */
#define IFP_RING 1216u                 /* 19 counts of 64 samples: 206 us at 5.9 MS/s */
int ifp_session(const k6_api_t *api, uint8_t *ring, uint32_t wait_ms, uint32_t max_ms, ifp_result_t *res);
#endif

#ifdef __cplusplus
}
#endif
#endif
