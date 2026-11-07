/* ifpager_emu: the pager's coded 4FSK session (../ifpager.c) as a module, for the emulated M0
   (tests/k6_live_emu.py, tests/test_k6_ifpager.py): the CPU it takes, its stack, and the
   packets it decodes from a modelled burst. Not for the radio, which links ifpager.c into the
   pager image (it needs sp_packet and more than a module's 4 KB). In place of sp_packet: its
   work's time (de-whitening into a copy, the nibble-table CRC, the frames into a store: here a
   copy into scratch RAM after the DMA ring, for the test to read). Prints the session's results. SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 4
#define SP_PKT_YIELD 1 /* as the image builds it: sp_packet yields to the receiver */
#include "../k6_mod.h"
#include "../ifpager.c"
#define EMU_SCR 0x20040000u /* the emulator's scratch RAM: the ring, then a count and the packets */

/* sp_packet's stand-in does its CRC this many times: on the K6 (2026-11-07, image fe25ec8) a
   good packet kept the session away 407 us, against ~70 us for one pass here */
#define EMU_SP_REPS 6

/* the yield and its context in scratch RAM, after the rate word (a module has no writable statics) */
#define PK_YIELD_AT ((void *volatile *)(EMU_SCR + IFP_RING + 4 + 128 * 64 + 4))
void sp_set_packet_yield(void (*f)(void *), void *ctx) { PK_YIELD_AT[0] = (void *)f, PK_YIELD_AT[1] = ctx; }

static const uint16_t nib[16] = {0x0000, 0x1021, 0x2042, 0x3063, 0x4084, 0x50A5, 0x60C6, 0x70E7,
                                 0x8108, 0x9129, 0xA14A, 0xB16B, 0xC18C, 0xD1AD, 0xE1CE, 0xF1EF};

int sp_packet(const uint8_t *payload, int len) {
  volatile uint32_t *np = (volatile uint32_t *)(EMU_SCR + IFP_RING);
  /* de-whitened into scratch RAM, as sp_packet into its static last_pkt (not on the stack) */
  uint8_t *o = (uint8_t *)(EMU_SCR + IFP_RING + 4) + *np * (uint32_t)len, *w = (uint8_t *)(PK_YIELD_AT + 2);
  unsigned c = 0xFFFF;
  int i, k;
  for (i = 0; i < len; i++) w[i] = (uint8_t)(payload[i] ^ nib[i & 15]); /* as its PN9 table read */
  for (k = 0; k < EMU_SP_REPS; k++)
    for (i = 0; i < len - 2; i++) {
      c = ((c << 4) & 0xFFFF) ^ nib[((c >> 12) ^ (w[i] >> 4)) & 15];
      c = ((c << 4) & 0xFFFF) ^ nib[((c >> 12) ^ w[i]) & 15];
      if ((i & 15) == 15 && PK_YIELD_AT[0]) /* as sotto_pager.c's crc16 */
        ((void (*)(void *))PK_YIELD_AT[0])(PK_YIELD_AT[1]);
    }
  for (i = 0; i < len; i++) o[i] = payload[i];
  (*np)++;
  return (int)(c & 1) + 2; /* (a result that depends on the loop, so it stays) */
}

/* say f with each '%' a signed decimal from v */
static void say_f(const k6_api_t *api, const char *f, const int32_t *v) {
  char line[120], *p = line, t[10];
  while (*f && p < line + 108) {
    if (*f != '%') {
      *p++ = *f++;
    } else {
      int32_t x = *v++;
      uint32_t u, n = 0;
      f++;
      if (x < 0) *p++ = '-', x = -x;
      u = (uint32_t)x;
      do {
        uint32_t q = ifrx_udiv(u, 10);
        t[n++] = (char)('0' + u - q * 10);
        u = q;
      } while (u);
      while (n) *p++ = t[--n];
    }
  }
  *p = 0;
  api->say(line);
}

int k6_main(const k6_api_t *api) {
  uint32_t n;
  uint8_t *scr = (uint8_t *)api->scratch(&n);
  ifp_result_t res;
  int32_t v[15];
  int r;
  *(volatile uint32_t *)(scr + IFP_RING) = 0;
  /* the test puts the flags (IFP_R23, IFP_NODECODE, IFP_R34) after the packets' store */
  r = ifp_session(api, scr, 8000, 60000, (int)*(volatile uint32_t *)(scr + IFP_RING + 4 + 128 * 64), &res);
  v[0] = r, v[1] = (int32_t)res.syncs, v[2] = (int32_t)res.syms, v[3] = (int32_t)res.lost;
  v[4] = res.acq_q, v[5] = res.acq_rej, v[6] = res.afc_hz, v[7] = res.idle_pct, v[8] = res.ks;
  v[9] = (int32_t)res.ms, v[10] = res.ram, v[11] = res.lag, v[12] = res.gap_us, v[13] = res.gap_i;
  v[14] = res.lead_ser;
  say_f(api, "ifp % syncs=% syms=% L=% q=% j=% f=% i=% k=% ms=% ram=% lag=% gap=% at=% ld=%", v);
  return r;
}
