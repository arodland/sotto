/* xdmr: DMR detect-and-mute inside EXPLORE's Listen for ID (docs/k6-dmr.md, docs/k6-explore-app.md).

   While xlisten polls the tone detector every 2 ms, xd_step drains the GP0 receive IF's DMA
   ring through c/k6/dmrsync.h in the waits between polls (xl_step_w), so the CW ID listener and
   the DMR detector share the listen. A confirmed DMR sync mutes the speaker (REG_47's AF bits)
   until 720 ms after the last sync of that family, as the dmrdet module does; the CW ID goes on
   being decoded, since the tone detector doesn't hear REG_47. The caller marks the row DMR, and
   on a row marked before it calls xd_known: the speaker then starts shut and opens only after
   XD_CLEAR_MS with no sync of any kind (an FM user on a mixed-mode repeater, or no DMR after
   all). The caller clears the mark after a few long listens with no DMR (ex_dmr_heard).

   The DMA ring (XD_RSZ bytes) lives on xd_step's stack, so the DMA runs only during a step.
   The detector's state (in xd_t, which the caller keeps in RAM that's free during a listen)
   carries on across the gaps between steps: its grid index moves on by the time that went by,
   so two syncs a whole number of 30 ms slots apart still confirm when a step boundary falls
   between them (a repeater with one slot idle sends a sync every 60 ms; a step is 60 ms).

   The receiver's gain: under the pager's AGC a -85 dBm DMR carrier reaches GP0 only 4-5 dB over
   the sigma-delta's own noise; REG_13 0x03FF frozen gives ~10 dB and band-passed |rho| 0.95-0.99
   (dmrcap, 2026-11-08). xd_start freezes it for a carrier no louder than XD_FREEZE_DBM and
   measures how much louder the RSSI then reads (xd_adj): the caller takes that off its level
   reads, so the listen's carrier-up rule and the floor still mean the same.

   CTCSS: the FD6818's sub-audio scan (REG_51 = 0x0300, as Aura's SCAN QT) runs for the whole
   listen; each step reads REG_68 (bit 15 clear: a tone word, tenths of Hz = raw * 100000 /
   206489). Two reads in a row within 0.9 Hz of one standard tone make it the carrier's tone
   (xd_tone), as Aura's SCAN QT decides. This part runs even when the DMR side can't.

   Writes REG_51, REG_33 bit 8, REG_34[3:0] (GP0 code 13), REG_47, REG_7E and REG_13, the MCU's TIM17 and
   DMA channel 1 (as dmrdet), all put back by xd_stop. No division but ds_udiv, no writable
   statics, receive only. SPDX-License-Identifier: MIT */
#ifndef XDMR_H
#define XDMR_H
#include <stdint.h>
#include "k6_mod.h"
#include "xlisten.h"
#include "dmrsync.h"
#ifdef __cplusplus
extern "C" {
#endif

#define XD_RSZ 2048u          /* the DMA ring on the stack: 370 us at 5.5 MS/s, room for a tone
                                 detector read (2-4 FD6818 calls) between drains */
#define XD_FREEZE_DBM (-60)   /* freeze the gain for carriers this loud or weaker */
#define XD_HOLD_MS 150u       /* the speaker stays shut this long into a listen, so a DMR
                                 carrier's first syncs can keep it shut */
#define XD_KEEP_MS 720u       /* DMR held this long after its family's last sync (a voice
                                 superframe has one every 360 ms a slot) */
#define XD_CLEAR_MS 3000u     /* a row already known as DMR (xd_known): the speaker stays shut
                                 until this long has gone by with no DMR sync at all (Jeff
                                 2026-11-08: mute a DMR repeater by default) */

enum { XD_NOW = 1, XD_EVER = 2 };

typedef struct {
  const k6_api_t *api;
  uint32_t t, c17, c14;        /* the 96 MHz clock, unwrapped from TIM17 and Aura's TIM14 */
  uint32_t cps8;               /* the DMA's rate: 96 MHz cycles a transfer, Q8 */
  uint32_t tc;                 /* the clock at the last count the detector had */
  uint32_t start, until, on_ms;/* api->ms(): the listen began; DMR held until; DMR came on */
  uint32_t last;               /* api->ms() at the last sync of any family (start: none yet) */
  uint32_t hits, steps, lost, busy, win;
  uint32_t stuck;              /* waits that api->ms() ended: the 96 MHz clock didn't move */
  uint32_t ahb, apb2;
  uint16_t r33, r34, r47, r7e, r13, r51;
  uint16_t tone, tcand;        /* the confirmed CTCSS tone (0.1 Hz), the last reading's */
  uint16_t treads;             /* steps with a tone reading */
  int16_t adj, lvl;            /* dB the frozen gain adds to RSSI reads; the level at the start */
  uint8_t ok;                  /* 1 running; 0 off (PB1 not an input, REG_33 refused) */
  uint8_t spk, frozen, dmr, ever, af, pend, fam, sign, r2, ndmr, rated;
  uint8_t known;               /* xd_known: shut until XD_CLEAR_MS clear of syncs */
  ds_t d;
} xd_t;

/* After xl_start, on the same channel: set the detector up (spk: the listen plays on the
   speaker, so DMR mutes it; cps8: the DMA rate the last listen measured, xd_rate8, or 0) */
void xd_start(xd_t *s, const k6_api_t *api, int spk, uint32_t cps8);
/* After xd_start: the row is known as DMR (a listen heard it there before), so the speaker
   stays shut until XD_CLEAR_MS go by with no DMR sync, instead of the XD_HOLD_MS hold */
void xd_known(xd_t *s);
/* One listen step: xl_step with the detector running in its waits; xl_step's result */
int xd_step(xd_t *s, xl_t *x);
/* Before xl_stop: everything put back; the DMR transmissions heard */
int xd_stop(xd_t *s);
uint32_t xd_flags(const xd_t *s);   /* XD_NOW | XD_EVER */
int xd_adj(const xd_t *s);          /* dB to take off RSSI reads while it runs */
uint32_t xd_rate8(const xd_t *s);   /* the DMA's measured rate, for the next listen's start */
uint32_t xd_tone(const xd_t *s);    /* the carrier's CTCSS tone in 0.1 Hz, 0 = none yet */
uint32_t xd_state_bytes(void);

#ifdef __cplusplus
}
#endif
#endif
