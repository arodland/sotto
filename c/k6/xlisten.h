/* xlisten: "Listen for ID" for the core image: the CW ID listener (c/k6/mod/cwlisten.c,
   docs/k6-cwid.md) cut into steps for the main loop.

   xl_start programs the FD6818's tone detector (16 tones, 600-1350 Hz) on the channel the
   caller tuned, xl_step polls REG_0B every 2 ms for about XL_STEP_MS and feeds cw_feed, and
   when two transmissions in a row give the same callsign it returns XL_OK with the call, its
   flags, the tone and the speed. Report lines go out as cwlisten's (CWID ..., CWID ok ...),
   so tools/k6_explore.py replay reads them.

   No flash writes here: the caller decides what to do with the call (ex_cw, rt_cw_check).
   No division, no library calls, no writable statics. SPDX-License-Identifier: MIT */
#ifndef XLISTEN_H
#define XLISTEN_H
#include <stdint.h>
#include "k6_mod.h"
#include "cwid.h"
#ifdef __cplusplus
extern "C" {
#endif

#define XL_STEP_MS 60      /* one xl_step polls this long */
#define XL_POLL_MS 2

enum { XL_RUN = 0, XL_OK = 1, XL_TIME = 2, XL_MSG = 3 };

typedef struct {
  const k6_api_t *api;
  cw_t cw;
  uint32_t start, max_ms, rx_hz;
  uint16_t hist[16];        /* key-down polls on each tone, this transmission */
  char last[11];            /* the callsign the last transmission with one gave */
  uint8_t seen, fl;         /* how many in a row gave it, its CW_CALL_* flags */
  uint8_t tone, q;          /* this transmission's tone index, % of key-down polls on it */
  uint8_t keyed;            /* 1 while the detector says a tone is on (for the screen) */
  uint16_t msgs, junk;
  char call[11];            /* after XL_OK: the confirmed call */
  char text[CW_TEXT];       /* the last transmission's text */
} xl_t;

/* Listen on rx_hz (already tuned) for up to max_s seconds. */
void xl_start(xl_t *x, const k6_api_t *api, uint32_t rx_hz, uint32_t max_s);
/* Poll for about XL_STEP_MS. XL_OK: x->call is confirmed (twice alike); XL_MSG: a transmission
   ended (x->text); XL_TIME: max_s is up; else XL_RUN. */
int xl_step(xl_t *x);
/* xl_step with the waits between polls handed to wait(c, us) instead of delay_us: it must
   return after about us (xdmr.c drains the GP0 DMA ring there) */
typedef void (*xl_wait_fn)(void *c, uint32_t us);
int xl_step_w(xl_t *x, xl_wait_fn wait, void *c);
/* the end of a listen: the detector off (the caller puts its own receive set-up back) */
void xl_stop(xl_t *x, int why);
int xl_tone_hz(const xl_t *x);
uint32_t xl_state_bytes(void);   /* sizeof(xl_t) */
/* what the screen shows, for a caller that can't see xl_t */
typedef struct {
  uint32_t ms;              /* listening this long */
  uint16_t msgs, junk;
  uint8_t keyed, seen, fl, wpm;
  uint16_t tone_hz;
  char call[11];            /* the confirmed call (after XL_OK), else the last one heard */
  char text[21];            /* the last transmission's text, cut to the screen */
} xl_info_t;
void xl_info(const xl_t *x, xl_info_t *out);

#ifdef __cplusplus
}
#endif
#endif
