/* ifpager_host: ifrx (ifrx.c) and the coded 4FSK decoder (ifpager.c) over a whole capture of
   GP0 counts, for tests/test_k6_ifpager.py. SPDX-License-Identifier: MIT */
#define IFP_HOST 1
#include "ifpager.c"

/* counts c[0..n) from `start` -> the packets decoded (128 bytes each, in pkts, at most maxp);
   info out: symbols, syncs, the syncs' wrong symbols (summed), ifrx's state, lead fit,
   rejected acquisitions, AFC Hz. r23: 1 for rate 2/3. Returns the packets. */
int ifp_host_run(const int8_t *c, int n, unsigned ks, int start, uint8_t *pkts, int maxp, int32_t *info,
                 int r23) {
  ifrx_t rx;
  ifp_t p;
  int i, np = 0;
  int32_t miss = 0;
  uint32_t syncs = 0;
  ifrx_init(&rx, IFRX_4FSK, ks * 16);
  ifp_init(&p);
  p.r23 = r23 & 1;
  for (i = start; i < n; i++) {
    if (ifrx_push(&rx, c[i])) {
      int v;
      ifp_sym(&p, rx.sym, rx.met);
      while ((v = ifp_work(&p)) >= 0) {
        if (v == 1) {
          int k;
          for (k = 0; k < IFP_PAYLOAD && np < maxp; k++) pkts[np * IFP_PAYLOAD + k] = p.pkt[k];
          np++;
          p.have = 0;
        }
      }
      if (p.syncs != syncs) syncs = p.syncs, miss += p.miss;
    }
  }
  info[0] = (int32_t)p.syms, info[1] = (int32_t)p.syncs, info[2] = miss, info[3] = rx.state;
  info[4] = rx.acq_q, info[5] = rx.acq_rej, info[6] = ifrx_afc_hz(&rx);
  return np;
}

/* the decoder alone on given decisions (tone a symbol, 4 metrics each) -> the packets */
int ifp_host_syms(const uint8_t *sym, const uint32_t *met, int n, uint8_t *pkts, int maxp, int32_t *info,
                  int r23) {
  ifp_t p;
  int i, np = 0, v;
  ifp_init(&p);
  p.r23 = r23 & 1;
  for (i = 0; i < n; i++) {
    ifp_sym(&p, sym[i], met + 4 * i);
    while ((v = ifp_work(&p)) >= 0) {
      if (v == 1) {
        int k;
        for (k = 0; k < IFP_PAYLOAD && np < maxp; k++) pkts[np * IFP_PAYLOAD + k] = p.pkt[k];
        np++;
        p.have = 0;
      }
    }
  }
  info[0] = (int32_t)p.syncs;
  return np;
}
