/* Very-low-rate sotto (sotto 600, sotto 400) mode descriptions and tables, written by
 * tools/export_vlr_c.py into sotto_vlr_tables.c. See sim/sotto_vlr.py and docs/sotto-vlr.md.
 * SPDX-License-Identifier: MIT */
#ifndef SOTTO_VLR_TABLES_H
#define SOTTO_VLR_TABLES_H
#include <stdint.h>

#define VLR_MAX_N 6                   /* frames per superframe */
#define VLR_MAX_ANCHORS 3
#define VLR_MAX_STAGES 2
#define VLR_NC 19                     /* shape: DCT coefficients 1..K-1 */

/* Field order (MSB first): voicing pattern bv, pitch centre 7, slope bs, energy mean 5, energy
   residual br, then each anchor's stage indices. */
typedef struct {
  int rate, n, bv, bs, br, na;
  const unsigned char *vpat;          /* [2^bv][n] v3.2 voicing codes */
  const int16_t *slope16;             /* [2^bs] pitch index steps per frame, x16 */
  const int16_t *eres16;              /* [2^br][n] energy code offsets from the mean, x16 */
  unsigned char aoff[VLR_MAX_ANCHORS];          /* anchor frame offsets in the superframe */
  unsigned char ns[VLR_MAX_ANCHORS];            /* stages per anchor */
  unsigned char sbits[VLR_MAX_ANCHORS][VLR_MAX_STAGES];
  const int8_t *cb[VLR_MAX_ANCHORS][VLR_MAX_STAGES];   /* [2^bits][VLR_NC] */
  int32_t step[VLR_MAX_ANCHORS][VLR_MAX_STAGES];       /* dB per codebook unit, Q16 */
  uint32_t cboff[VLR_MAX_ANCHORS][VLR_MAX_STAGES];     /* where cb[j][i] starts in vlr_cb_all */
} vlr_mode_t;

extern const vlr_mode_t vlr_mode600, vlr_mode400;
extern const uint32_t vlr_cb_bytes;            /* both modes' shape codebooks, int8 */
extern const int8_t vlr_cb_all[];              /* them (not in a -DVLR_CB_SPI tables build) */

/* Codebook vector idx of anchor j's stage i (VLR_NC int8 values). Built with -DVLR_CB_SPI (the
   K6), the tables leave the codebooks out (cb[][] is NULL) and the vector comes from
   vlr_cb_fetch (c/k6/sp_vlr.c), which reads it from the SPI flash into buf. */
#ifdef VLR_CB_SPI
const int8_t *vlr_cb_fetch(const vlr_mode_t *m, int j, int i, int idx, int8_t buf[VLR_NC]);
#define VLR_CB_VEC(m, j, i, idx, buf) vlr_cb_fetch(m, j, i, idx, buf)
#else
#define VLR_CB_VEC(m, j, i, idx, buf) ((void)(buf), (m)->cb[j][i] + (idx) * VLR_NC)
#endif

#endif
