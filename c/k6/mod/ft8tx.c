/* ft8tx: the K6 sends one FT8 frame itself, as audio FSK on FM from the FD6818's tone
   oscillator, then identifies in MCW (module API level 3).

   The FT8 thread's transmit half (Jeff, 2026-11-06: "develop ft8, WSPR plugins for this
   firmware"). The 79 tones come from ft8tx_msg.h, which `tools/k6_ft8.py txhdr TEXT` writes
   from the same encoder as the receiver's tests (default "CQ AG7EW CN87"). The module:

   - keys up only when the pager listens on FT8_HZ (440.850 MHz, the bench frequency), after the
     channel has been clear for FT8_CLEAR_MS (the upload's sender has unkeyed);
   - calls tx_on for the frame, the ID and a margin (about 17 s, under the core's 20 s cap):
     FM, low power, no CTCSS, mic ADC off;
   - turns tone 1 on (REG_70 = enable | FT8_GAIN << 8) and steps its frequency word REG_71
     every 5 ms: tone k of 79 at FT8_AUDIO + 6.25 k Hz for 160 ms each, with the GFSK (BT 2)
     transition of WSJT-X's waveform over the 30 ms either side of each symbol boundary;
   - sends AG7EW in MCW (the 800 Hz tone keyed with REG_70) at 20 wpm, and keys down.

   REG_71 is f x 10.32444 (Aura's tone_reg_word), about 0.097 Hz a step: one FT8 tone step is
   64.53 words. Symbol timing is the module clock (api->ms, Aura's 100 us TIM14 from the 16 MHz
   crystal and PLL), so the frame is 12.64 s long to the crystal's accuracy.

   The open question this answers on air: tx_on leaves REG_30 at 0xC1FA, Aura's TX state with
   the mic ADC off. Aura's own tone and SSTV transmitters use 0xC3FA (bit 9, the AF DAC, too).
   If the tone doesn't reach the carrier in 0xC1FA, the IC-7100 hears a silent carrier and
   tx_on needs a tone option (a firmware change). The MCW ID at the end tests the same path.

   It says
     K6 MOD say ft8tx CQ AG7EW CN87 f=1500 g=60 rx=440850000
     K6 MOD say ft8tx sent sym=79 ms=12640 id=52
   and returns the symbols sent, or a negative error:
     -1  the image has no level-3 transmitter      -2  not on FT8_HZ
     -3  tx_on refused                             -4  a key was pressed (keyed down at once)
     -5  the core keyed down partway (time up)      -7  the channel stayed busy (FT8_WAIT_MS)

   Build: python -m modem.k6 mod out.wav c/k6/mod/ft8tx.c --api-level 3 --key-file KEY
   (or CI's mod-ft8tx.bin). SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 3
#include "../k6_mod.h"
#include "ft8tx_msg.h"   /* FT8_TEXT, FT8_TONES[79] */

#ifndef FT8_HZ
#define FT8_HZ 440850000u
#endif
#ifndef FT8_AUDIO
#define FT8_AUDIO 1500      /* Hz, tone 0 */
#endif
#ifndef FT8_GAIN
#define FT8_GAIN 60         /* REG_70 tone-1 gain, 0-127 (Aura's tone and SSTV use 60) */
#endif
#ifndef FT8_LEAD_MS
#define FT8_LEAD_MS 300     /* carrier, tone off, before the frame: the PLL settles */
#endif
#ifndef FT8_CLEAR_DBM
#define FT8_CLEAR_DBM -90
#endif
#ifndef FT8_CLEAR_MS
#define FT8_CLEAR_MS 1000
#endif
#ifndef FT8_WAIT_MS
#define FT8_WAIT_MS 30000
#endif
#ifndef ID_TEXT
#define ID_TEXT "AG7EW"
#endif
#ifndef ID_WPM
#define ID_WPM 20
#endif
#ifndef ID_HZ
#define ID_HZ 800
#endif

#define STEP_MS 5u                                   /* REG_71 update interval */
#define SYM_STEPS 32u                                /* 160 ms / 5 ms */
#define NSYM 79u
#define WORD(hz) ((uint16_t)((uint32_t)(hz) * 1032444u / 100000u))
#define TONE_Q16 4228947u                            /* 6.25 x 10.32444 x 65536: words per tone */
#define REG70_ON ((uint16_t)(0x8000u | ((FT8_GAIN & 0x7Fu) << 8)))
#define UNIT_MS (1200u / ID_WPM)
#define ID_UNITS 52                                  /* AG7EW: A 8, G 12, 7 16, E 4, W 12 */
#define TX_MS (FT8_LEAD_MS + NSYM * 160u + 400u + ID_UNITS * UNIT_MS + 1000u)

/* the GFSK (BT 2) step response, 256 x 0.5 (1 + erf(pi sqrt(2/ln 2) BT x)), at the middle of each
   5 ms step after a symbol boundary (x = 2.5 ms, 7.5 ms, ...); 256 from the seventh on */
static const uint8_t ramp[6] = {152, 195, 225, 243, 252, 255};

/* Morse by code, as cwkey.c: start at 1, then 0 for a dot and 1 for a dash */
static const char tab[] = "~~ETIANMSURWDKGOHVF~L~PJBXCYZQ~~54~3~~~2~~+~~~~16=/~~~~~7~~~8~90";

static int code_of(char c) {
  int i;
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

/* wait until ms() reaches t; 0, or -4 on a key (checked once a wait: about 60 us each) */
static int until(const k6_api_t *api, uint32_t t) {
  if (api->key_down()) return -4;
  while ((int32_t)(api->ms() - t) < 0) api->delay_us(250);
  return 0;
}

/* still keyed? tx_pa(1) only re-enables the PA that tx_on enabled; -1 once the core keyed down */
static int keyed(const k6_api_t *api) {
  return api->tx_pa(1) < 0 ? -5 : 0;
}

/* the tone frequency at step j (0-31) of symbol k, in 1/256 tone steps */
static uint32_t freq_q8(uint32_t k, uint32_t j) {
  int32_t f = (int32_t)FT8_TONES[k] << 8;
  if (j < 6 && k > 0) f += ((int32_t)FT8_TONES[k - 1] - FT8_TONES[k]) * (256 - ramp[j]);
  if (j >= SYM_STEPS - 6 && k + 1 < NSYM)
    f += ((int32_t)FT8_TONES[k + 1] - FT8_TONES[k]) * (256 - ramp[SYM_STEPS - 1 - j]);
  return (uint32_t)f;
}

/* AG7EW in MCW: the tone keyed with REG_70; the units sent, or a negative error */
static int mcw(const k6_api_t *api) {
  const char *s;
  int units = 0, r = 0;
  uint32_t t = api->ms();
  api->fd_write(0x71, WORD(ID_HZ));
  for (s = ID_TEXT; *s && !r; s++) {
    int c = code_of(*s), b;
    if (*s == ' ') { t += 4 * UNIT_MS; units += 4; continue; }
    for (b = 31; !(c >> b); b--) {}
    while (b-- && !r) {
      int n = (c >> b) & 1 ? 3 : 1, g = b ? 1 : 3;
      if ((r = keyed(api)) != 0) break;
      api->fd_write(0x70, REG70_ON);
      t += (uint32_t)n * UNIT_MS;
      if ((r = until(api, t)) != 0) break;
      api->fd_write(0x70, 0);
      t += (uint32_t)g * UNIT_MS;
      r = until(api, t);
      units += n + g;
    }
  }
  api->fd_write(0x70, 0);
  return r ? r : units;
}

int k6_main(const k6_api_t *api) {
  char line[64], *p;
  uint32_t t0, t, k = 0, j;
  int r = 0, id = 0;
  if (api->level < 3) return -1;
  p = put(line, "ft8tx " FT8_TEXT " f=");
  p = dec(p, FT8_AUDIO);
  p = put(p, " g=");
  p = dec(p, FT8_GAIN);
  p = put(p, " rx=");
  p = dec(p, api->rx_hz());
  *p = 0;
  api->say(line);
  if (api->rx_hz() != FT8_HZ) return -2;
  {                                           /* the upload's sender must have unkeyed */
    uint32_t clear = api->ms();
    while (api->ms() - clear < FT8_CLEAR_MS) {
      if (KM_RSSI_DBM(api->fd_read(0x67)) >= FT8_CLEAR_DBM) clear = api->ms();
      if (api->ms() > FT8_WAIT_MS) return -7;
      if (api->key_down()) return -4;
      api->delay_us(10000);
    }
  }
  if (api->tx_on(TX_MS) != 0) return -3;
  api->fd_write(0x70, 0);
  api->fd_write(0x71, WORD(FT8_AUDIO));
  t0 = api->ms() + FT8_LEAD_MS;
  r = until(api, t0);
  if (!r) api->fd_write(0x70, REG70_ON);
  for (t = t0; !r && k < NSYM; k++) {
    if ((r = keyed(api)) != 0) break;
    for (j = 0; j < SYM_STEPS && !r; j++) {
      api->fd_write(0x71, (uint16_t)(WORD(FT8_AUDIO) + ((freq_q8(k, j) * (TONE_Q16 >> 8)) >> 16)));
      t += STEP_MS;
      r = until(api, t);
    }
  }
  api->fd_write(0x70, 0);
  if (!r) r = until(api, t + 400);            /* a gap, then the ID */
  if (!r) {
    id = mcw(api);
    if (id < 0) r = id;
  }
  api->fd_write(0x70, 0);
  api->fd_write(0x71, 0);
  api->tx_off();
  if (r) return r;
  p = put(line, "ft8tx sent sym=");
  p = dec(p, k);
  p = put(p, " ms=");
  p = dec(p, t - t0);
  p = put(p, " id=");
  p = dec(p, (uint32_t)id);
  *p = 0;
  api->say(line);
  return (int)k;
}
