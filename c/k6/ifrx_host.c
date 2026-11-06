/* ifrx_host: ifrx (ifrx.c) driven over a whole capture, for tests/test_k6_ifrx.py.
   SPDX-License-Identifier: MIT */
/* each sweep's best offset, its lead fit and the AFC then (Hz), for the tests */
int ifrx_trace[512][3], ifrx_ntrace;
#define IFRX_TRACE(s, h)                                                                 \
  do {                                                                                   \
    if (ifrx_ntrace < 512) {                                                             \
      int *t_ = ifrx_trace[ifrx_ntrace++];                                               \
      t_[0] = (int)(h), t_[1] = (s)->acq_q, t_[2] = ifrx_afc_hz(s);                      \
    }                                                                                    \
  } while (0)
#include "ifrx.c"

/* counts c[0..n) from count `start`, with `pause` counts skipped at each `every` (0: none;
   the DMA ring overrunning while the radio prints a line). Each symbol's tone index, metrics
   (M a symbol), AFC (Hz), gap flag and next symbol start (counts, Q8; taus may be 0) go out;
   info: acquisition offset, lead fit, sweeps, rejects, slips, state, lead misses. info[8] in:
   0x100 turns the AFC off. Returns the symbols. */
int ifrx_run(const int8_t *c, int n, int mode, unsigned ks, int start, int pause, int every,
             uint8_t *sym, uint32_t *met, int *afc, uint8_t *gap, int maxsym, int32_t *info,
             uint32_t *taus) {
  ifrx_t s;
  int i, ns = 0, j;
  ifrx_init(&s, mode, ks);
  ifrx_ntrace = 0;
  if (info[8] == 0x100) s.afc_sh = 0;
  for (i = start; i < n && ns < maxsym; i++) {
    if (ifrx_push(&s, c[i])) {
      sym[ns] = (uint8_t)s.sym;
      for (j = 0; j < s.M; j++) met[ns * s.M + j] = s.met[j];
      afc[ns] = ifrx_afc_hz(&s);
      gap[ns] = (uint8_t)s.gap;
      s.gap = 0;
      if (taus) taus[ns] = ((s.tau + (uint32_t)start) << 8) + (s.tau_f >> 8);
      ns++;
    }
    if (every && pause && (i - start) % every == every - 1) {
      ifrx_skip(&s, (uint32_t)pause);
      i += pause;
    }
  }
  info[0] = s.acq_off, info[1] = s.acq_q, info[2] = s.acq_tries, info[3] = s.acq_rej;
  info[4] = (int32_t)s.slips, info[5] = s.state, info[6] = s.lead_err;
  return ns;
}
