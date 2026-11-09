/* aisrx_host: the K6's AIS receiver (c/k6/aisrx.h) on the host, for tests/test_k6_ais.py.
     ar_host(x, n, pre, c1, out, maxf)   decode one capture: out gets maxf records of
                                         4 + 4 + AR_MAXB bytes (nbytes, pos Q16, bytes);
                                         returns the frames
     ar_host_if(x, n, pre, c1, if_q32, out, maxf)   the same with the burst at if_q32 (aisrx.h)
   SPDX-License-Identifier: MIT */
#include <stdint.h>
#include <string.h>
#include "aisrx.h"

int ar_host_if(const int8_t *x, int n, int pre, unsigned c1, unsigned if_q32, uint8_t *out,
               int maxf) {
  static uint16_t ph[4096];
  ar_frame_t f[8];
  int k, i;
  if (maxf > 8) maxf = 8;
  if (AR_NPH((uint32_t)n, (uint32_t)pre) > 4096) return -1;
  k = ar_decode_if(x, (uint32_t)n, (uint32_t)pre, c1, if_q32, ph, f, maxf);
  for (i = 0; i < k; i++) {
    uint8_t *o = out + i * (8 + AR_MAXB);
    int32_t nb = f[i].nbytes;
    memcpy(o, &nb, 4);
    memcpy(o + 4, &f[i].pos, 4);
    memcpy(o + 8, f[i].b, AR_MAXB);
  }
  return k;
}

int ar_host(const int8_t *x, int n, int pre, unsigned c1, uint8_t *out, int maxf) {
  return ar_host_if(x, n, pre, c1, AR_IF_Q32, out, maxf);
}
