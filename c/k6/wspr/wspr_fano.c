/* wspr_fano.c: Fano sequential decoding of WSPR's K = 32, rate-1/2 convolutional code (after Phil
   Karn's description of the algorithm), for sim/k6digi/wsprref.py and later the K6.

   q0[i] / q1[i]: the integer Fano metric of coded bit i (0..161, in coder order) being 0 / 1.
   Decodes 81 bits (50 message bits, then 31 zero tail bits) into bits[]; returns the cycles
   used, or -1 if it gave up after maxcycles. No division, no floating point, no statics.
   SPDX-License-Identifier: MIT */
#include <stdint.h>

#define WSPR_P1 0xF2D05351u
#define WSPR_P2 0xE4613C47u
#define WSPR_NB 81
#define WSPR_TAIL (WSPR_NB - 31)

static int par32(uint32_t x) {
  x ^= x >> 16;
  x ^= x >> 8;
  x ^= x >> 4;
  x ^= x >> 2;
  x ^= x >> 1;
  return (int)(x & 1);
}

typedef struct {
  uint32_t st;      /* coder state with this node's bit in bit 0 */
  int32_t gam;      /* path metric into this node */
  int32_t tm[2];    /* branch metrics, best first */
  int8_t i;         /* which branch is being tried */
} wspr_node_t;

static int32_t branch(const int16_t *q0, const int16_t *q1, int k, uint32_t st) {
  int a = par32(st & WSPR_P1), b = par32(st & WSPR_P2);
  return (a ? q1[2 * k] : q0[2 * k]) + (b ? q1[2 * k + 1] : q0[2 * k + 1]);
}

static void metrics(wspr_node_t *n, const int16_t *q0, const int16_t *q1, int k) {
  int32_t m0 = branch(q0, q1, k, n->st), m1;
  if (k >= WSPR_TAIL) {
    n->tm[0] = m0;
    n->tm[1] = -(1 << 28);
  } else {
    m1 = branch(q0, q1, k, n->st | 1);
    if (m0 >= m1) {
      n->tm[0] = m0, n->tm[1] = m1;
    } else {
      n->tm[0] = m1, n->tm[1] = m0;
      n->st |= 1;
    }
  }
  n->i = 0;
}

long wspr_fano(const int16_t *q0, const int16_t *q1, uint8_t *bits, int32_t delta, long maxcycles) {
  wspr_node_t nodes[WSPR_NB + 1], *np = nodes;
  int32_t t = 0, ng;
  long c;
  int k;
  nodes[0].st = 0;
  nodes[0].gam = 0;
  metrics(np, q0, q1, 0);
  for (c = 0; c < maxcycles; c++) {
    ng = np->gam + np->tm[np->i];
    if (ng >= t) {
      if (np->gam < t + delta)                 /* first visit: tighten the threshold */
        while (ng >= t + delta) t += delta;
      np[1].gam = ng;
      np[1].st = np->st << 1;
      if (++np == nodes + WSPR_NB) {
        for (k = 0; k < WSPR_NB; k++) bits[k] = (uint8_t)(nodes[k].st & 1);
        return c;
      }
      metrics(np, q0, q1, (int)(np - nodes));
      continue;
    }
    for (;;) {                                  /* the threshold is violated: look back */
      if (np == nodes || np[-1].gam < t) {
        t -= delta;
        if (np->i != 0) {
          np->i = 0;
          np->st ^= 1;
        }
        break;
      }
      --np;
      if (np - nodes < WSPR_TAIL && np->i != 1) {
        np->i = 1;
        np->st ^= 1;
        break;
      }
    }
  }
  return -1;
}
