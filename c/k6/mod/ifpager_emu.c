/* ifpager_emu: the pager's coded 4FSK session (../ifpager.c) as a module, for the emulated M0
   (tests/k6_live_emu.py, tests/test_k6_ifpager.py): the CPU it takes, its stack, and the
   packets it decodes from a modelled burst. Not for the radio, which links ifpager.c into the
   pager image (it needs sp_packet and more than a module's 4 KB). In place of sp_packet: its
   work's time (de-whitening into a copy, the nibble-table CRC, the frames into a store: here a
   copy into scratch RAM after the DMA ring, for the test to read). Prints the session's results. SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 4
#define IFRX_T4_K6 1
#include "../k6_mod.h"
#include "../ifpager.c"
#define EMU_SCR 0x20040000u /* the emulator's scratch RAM: the ring, then a count and the packets */

static const uint16_t nib[16] = {0x0000, 0x1021, 0x2042, 0x3063, 0x4084, 0x50A5, 0x60C6, 0x70E7,
                                 0x8108, 0x9129, 0xA14A, 0xB16B, 0xC18C, 0xD1AD, 0xE1CE, 0xF1EF};

int sp_packet(const uint8_t *payload, int len) {
  volatile uint32_t *np = (volatile uint32_t *)(EMU_SCR + IFP_RING);
  uint8_t *o = (uint8_t *)(EMU_SCR + IFP_RING + 4) + *np * (uint32_t)len, w[IFP_PAYLOAD];
  unsigned c = 0xFFFF;
  int i;
  for (i = 0; i < len; i++) w[i] = (uint8_t)(payload[i] ^ nib[i & 15]); /* as its PN9 table read */
  for (i = 0; i < len - 2; i++) {
    c = ((c << 4) & 0xFFFF) ^ nib[((c >> 12) ^ (w[i] >> 4)) & 15];
    c = ((c << 4) & 0xFFFF) ^ nib[((c >> 12) ^ w[i]) & 15];
  }
  for (i = 0; i < len; i++) o[i] = payload[i];
  (*np)++;
  return (int)(c & 1) + 2; /* (a result that depends on the loop, so it stays) */
}

/* say f with each '%' a signed decimal from v */
static void say_f(const k6_api_t *api, const char *f, const int32_t *v) {
  char line[96], *p = line, t[10];
  while (*f && p < line + 84) {
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
  int32_t v[10];
  int r;
  *(volatile uint32_t *)(scr + IFP_RING) = 0;
  r = ifp_session(api, scr, 8000, 60000, &res);
  v[0] = r, v[1] = (int32_t)res.syncs, v[2] = (int32_t)res.syms, v[3] = (int32_t)res.lost;
  v[4] = res.acq_q, v[5] = res.acq_rej, v[6] = res.afc_hz, v[7] = res.idle_pct, v[8] = res.ks;
  v[9] = (int32_t)res.ms;
  say_f(api, "ifp % syncs=% syms=% L=% q=% j=% f=% i=% k=% ms=%", v);
  return r;
}
