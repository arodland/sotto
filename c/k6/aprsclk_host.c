/* aprsclk_host: the APRS clock module's receiver (c/k6/mod/aprsclk.c, everything after the DMA)
   on the host, for tests/test_k6_aprsclk.py and for recordings of GP0 counts.

     aprs_reset(cps8)              a fresh receiver at the DMA rate cps8 (96 MHz cycles a
                                   transfer, Q8)
     aprs_counts(c, n, ms0)        counts (2 * ones - 64 per 64 DMA samples) through the whole
                                   chain, the module's clock at ms0 for the first
     aprs_frame(f, n, ms, nbits)   one frame (with its FCS) as if the HDLC had just closed it at
                                   module ms `ms`, nbits bits after its opening flag
     aprs_result(r)                votes, interval ms, UTC ms of day at module ms 0 (-1 unset),
                                   frames good, frames bad, timestamps, day, month, year
     aprs_lines()                  the module's lines since the reset, '\n' after each

   The trace (aprs_tr: each audio sample's discriminator and the mark and space levels) is for
   looking at the demodulator. SPDX-License-Identifier: MIT */
#include <stdint.h>
#include <string.h>
#define APRS_HOST
int32_t aprs_tr[1 << 20][3];
int aprs_ntr;
#define APRS_TRACE(s, d, m, sp)                                                              \
  do {                                                                                       \
    if (aprs_ntr < (1 << 20)) {                                                              \
      int32_t *t_ = aprs_tr[aprs_ntr++];                                                     \
      t_[0] = d, t_[1] = m, t_[2] = sp;                                                      \
    }                                                                                        \
  } while (0)
#include "mod/aprsclk.c"

static st_t S;
static k6_api_t A;
static char out[1 << 16];
static int olen;
static uint32_t h_ms;

static void h_say(const char *s) {
  int n = (int)strlen(s);
  if (olen + n + 2 < (int)sizeof out) memcpy(out + olen, s, n), olen += n, out[olen++] = '\n';
  out[olen] = 0;
}
static uint32_t h_msf(void) { return h_ms; }

void aprs_reset(uint32_t cps8) {
  memset(&S, 0, sizeof S);
  memset(&A, 0, sizeof A);
  A.abi = 1, A.level = 2, A.say = h_say, A.ms = h_msf;
  olen = 0, out[0] = 0, aprs_ntr = 0;
  S.cps8 = cps8;
  steps(&S);
  S.n = (int32_t)udiv(64000000u + 600u * cps8, 1200u * cps8);
  if (S.n > NW - 1) S.n = NW - 1;
}

void aprs_counts(const int8_t *c, int n, uint32_t ms0) {
  int i;
  for (i = 0; i < n; i++) {
    /* 64 transfers of cps8 / 256 cycles at 96 MHz a count */
    h_ms = ms0 + (uint32_t)((uint64_t)i * 64u * S.cps8 / (256u * 96000u));
    S.now_ms = h_ms;
    count(&A, &S, c[i]);
  }
}

void aprs_frame(const uint8_t *f, int n, uint32_t ms, uint32_t nbits) {
  if (n > MAXF) return;
  memcpy(S.fr, f, (size_t)n);
  S.len = n, S.nbits = nbits, S.now_ms = h_ms = ms;
  if (crc_ok(S.fr, S.len)) S.ok++, frame(&A, &S);
  else S.bad++;
}

void aprs_result(int32_t r[9]) {
  r[0] = S.votes, r[1] = S.wid, r[2] = S.votes >= 2 ? (int32_t)utc(&S, 0) : -1;
  r[3] = (int32_t)S.ok, r[4] = (int32_t)S.bad, r[5] = (int32_t)S.nts;
  r[6] = S.day, r[7] = S.mon, r[8] = S.yr;
}

const char *aprs_lines(void) { return out; }
