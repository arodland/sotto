/* cwkey: the K6 sends its own CW ID once, as true CW: the carrier keyed with the PA while
   the PLL stays locked (module API level 3, tx_on / tx_pa / tx_off).

   Jeff asked for it on 2026-11-06 ("can you do it once from the HT?"). It keys up only when
   the pager listens on CW_HZ (440.850 MHz, the bench frequency), sends CW_TEXT ("AG7EW") at
   CW_WPM, and keys down. The core's own guards stay in force: the pager's frequency only, the
   TX band lock, low power, at most 20 s, key-down when the module returns.

   With the PA off between elements the carrier is gone, so a receiver hears it as CW in CW or
   SSB mode; in FM it only hears the squelch open and close with the keying.

   It says
     K6 MOD say cw key AG7EW 20wpm rx=440850000
     K6 MOD say cw sent units=52 ms=3420
   and returns the units sent, or a negative error:
     -1  the image has no level-3 transmitter      -2  not on CW_HZ
     -3  tx_on refused                             -4  a key was pressed (keyed down at once)
     -5  tx_pa failed partway (the core keyed down: time up)
     -6  a character with no Morse code in CW_TEXT

   Build: python -m modem.k6 mod out.wav c/k6/mod/cwkey.c --api-level 3 --key-file KEY
   SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 3
#include "../k6_mod.h"

#ifndef CW_TEXT
#define CW_TEXT "AG7EW"
#endif
#ifndef CW_WPM
#define CW_WPM 20
#endif
#ifndef CW_HZ
#define CW_HZ 440850000u
#endif
#ifndef CW_LEAD_MS
#define CW_LEAD_MS 300       /* key-up with the PA off before the first element: PLL settles */
#endif
#define UNIT_US (1200000u / CW_WPM)

/* Morse by code, as cwid.c: start at 1, then 0 for a dot and 1 for a dash (E = 2, A = 5) */
static const char tab[] = "~~ETIANMSURWDKGOHVF~L~PJBXCYZQ~~54~3~~~2~~+~~~~16=/~~~~~7~~~8~90";

static int code_of(char c) {
  int i;
  if (c >= 'a' && c <= 'z') c -= 32;
  for (i = 2; i < (int)sizeof tab - 1; i++)
    if (tab[i] == c && c != '~') return i;
  return 0;
}

static char *put(char *p, const char *s) {
  while (*s) *p++ = *s++;
  return p;
}

static char *dec(char *p, uint32_t v) {     /* no divide on the M0: subtract powers of ten */
  static const uint32_t pw[] = {1000000000u, 100000000u, 10000000u, 1000000u, 100000u,
                                10000u, 1000u, 100u, 10u, 1u};
  int i, started = 0;
  for (i = 0; i < 10; i++) {
    char d = '0';
    while (v >= pw[i]) { v -= pw[i]; d++; }
    if (d != '0' || started || i == 9) { *p++ = d; started = 1; }
  }
  return p;
}

/* hold the key (PA on or off) for n units; 0, or a negative error */
static int hold(const k6_api_t *api, int on, int n) {
  if (api->tx_pa(on) < 0) return -5;
  while (n--) {
    if (api->key_down()) return -4;
    api->delay_us(UNIT_US);
  }
  return 0;
}

int k6_main(const k6_api_t *api) {
  const char *s;
  char line[64], *p;
  int units = 0, need = 0, r = 0;
  uint32_t t0;
  if (api->level < 3) return -1;
  for (s = CW_TEXT; *s; s++) {          /* check the text and count its units */
    int c = code_of(*s), b;
    if (*s == ' ') { need += 4; continue; }
    if (!c) return -6;
    for (b = 31; !(c >> b); b--) {}
    while (b--) need += ((c >> b) & 1 ? 3 : 1) + (b ? 1 : 3);
  }
  p = put(line, "cw key " CW_TEXT " ");
  p = dec(p, CW_WPM);
  p = put(p, "wpm rx=");
  p = dec(p, api->rx_hz());
  *p = 0;
  api->say(line);
  if (api->rx_hz() != CW_HZ) return -2;
  /* the core's limit: the lead-in, the text and a second's margin */
  if (api->tx_on(CW_LEAD_MS + (uint32_t)need * (UNIT_US / 1000u) + 1000u) != 0) return -3;
  t0 = api->ms();
  if (api->tx_pa(0) < 0) r = -5;
  if (!r) api->delay_us(CW_LEAD_MS * 1000u);
  for (s = CW_TEXT; *s && !r; s++) {
    int c, b;
    if (*s == ' ') {                     /* a word gap: 7 units, 3 already after the last char */
      r = hold(api, 0, 4);
      units += 4;
      continue;
    }
    c = code_of(*s);
    for (b = 31; !(c >> b); b--) {}      /* the start bit */
    while (b--) {
      int n = (c >> b) & 1 ? 3 : 1;
      if ((r = hold(api, 1, n)) != 0) break;
      if ((r = hold(api, 0, b ? 1 : 3)) != 0) break;
      units += n + (b ? 1 : 3);
    }
  }
  api->tx_pa(0);
  api->tx_off();
  if (r) return r;
  p = put(line, "cw sent units=");
  p = dec(p, (uint32_t)units);
  p = put(p, " ms=");
  p = dec(p, api->ms() - t0);
  *p = 0;
  api->say(line);
  return units;
}
