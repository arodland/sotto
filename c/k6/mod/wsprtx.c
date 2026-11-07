/* wsprtx: the K6 as a WSPR beacon. It sends WSPR frames itself, as audio 4-FSK on FM from the
   FD6818's tone oscillator, each followed by an MCW ID (module API level 3).

   Jeff, 2026-11-07: "finish out the WSPR beacon and try for receive". The 162 tones come from
   wsprtx_msg.h, which `tools/k6_ft8.py wsprhdr` writes with sim/k6digi/wsprref.py's encoder
   (default "AG7EW CN87 10"). Per transmission the module:

   - waits until the channel has been clear (below WSPR_CLEAR_DBM) for WSPR_CLEAR_MS: the first
     time, that's the upload's sender unkeying; later, it skips a slot rather than key over
     someone;
   - calls tx_on for the frame, the ID and a margin: 300 + 110,592 + 400 + 3,120 + 1,000 ms,
     about 115.4 s. That needs an image whose module TX cap is 120 s (after 2026-11-07); an
     older image's 20 s cap keys it down partway (-5);
   - turns tone 1 on (REG_70 = enable | WSPR_GAIN << 8) and sets its frequency word REG_71 once a
     symbol: tone k (0-3) at WSPR_AUDIO + 1.4648 k Hz for 8192 / 12000 s (682.67 ms) each, phase
     continuous, 162 symbols. Symbol k starts at t0 + k x 2048 / 3 ms, kept in thirds of a ms;
   - sends AG7EW in MCW (the 800 Hz tone keyed with REG_70) at 20 wpm, and keys down.

   On FM the receiver hears the tones as audio: their frequencies come from the FD6818's crystal
   divided down to 1.5 kHz, so the carrier's drift at 440 MHz (5-20 Hz over a frame, run 5)
   doesn't reach them. A WSPR decoder on the receiver's audio sees a drift-free signal.

   With WSPR_REPEAT > 1 it is a beacon: a transmission every WSPR_EVERY two-minute slots
   (default 5: every 10 minutes, 20% of the time), timed by the module clock (the M0's crystal),
   until the count is done or a key is held. WSPR frames start 1 s after an even UTC minute;
   the K6 has no UTC clock, so the host times the first one by when it unkeys after the upload
   (key-up comes about 2.7-3.2 s after that: the 1 s clear wait and tx_on's 1.4-1.9 s) and the
   later ones follow on the 2-minute grid.

   It says
     K6 MOD say wspr AG7EW CN87 10 f=1500 g=60 n=1 rx=440850000
     K6 MOD say wspr sent 1 sym=162 ms=110592 id=52
     K6 MOD say wspr next 480 s                              (a beacon, between frames)
     K6 MOD say wspr busy, slot skipped                     (a beacon: the channel wasn't clear)
   and returns the frames sent, or a negative error:
     -1  the image has no level-3 transmitter      -2  not on WSPR_HZ
     -3  tx_on refused                             -4  a key was pressed (keyed down at once)
     -5  the core keyed down partway (time up)      -7  the channel stayed busy (WSPR_WAIT_MS)

   Build: python -m modem.k6 mod out.wav c/k6/mod/wsprtx.c --api-level 3 --key-file KEY
   (or CI's mod-wsprtx.bin; mod-wsprbcn.bin is the beacon build). SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 3
#include "../k6_mod.h"
#include "wsprtx_msg.h"   /* WSPR_TEXT, WSPR_TONES[162] */

#ifndef WSPR_HZ
#define WSPR_HZ 440850000u
#endif
#ifndef WSPR_AUDIO
#define WSPR_AUDIO 1500     /* Hz, tone 0 */
#endif
#ifndef WSPR_GAIN
#define WSPR_GAIN 60        /* REG_70 tone-1 gain, 0-127 (ft8tx's: -28 dBFS on the IC-7100) */
#endif
#ifndef WSPR_REPEAT
#define WSPR_REPEAT 1       /* frames to send */
#endif
#ifndef WSPR_EVERY
#define WSPR_EVERY 5        /* two-minute slots from one frame's start to the next */
#endif
#ifndef WSPR_LEAD_MS
#define WSPR_LEAD_MS 300
#endif
#ifndef WSPR_CLEAR_DBM
#define WSPR_CLEAR_DBM -90
#endif
#ifndef WSPR_CLEAR_MS
#define WSPR_CLEAR_MS 1000
#endif
#ifndef WSPR_WAIT_MS
#define WSPR_WAIT_MS 30000
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

#define NSYM 162u
#define SYM3 2048u                                   /* a symbol in thirds of a ms: 682.67 ms */
#define FRAME_MS 110592u                             /* 162 x 8192 / 12 */
#define WORD(hz) ((uint16_t)((uint32_t)(hz) * 1032444u / 100000u))
#define TONE_Q16 991148u                             /* 12000 / 8192 x 10.32444 x 65536 */
#define REG70_ON ((uint16_t)(0x8000u | ((WSPR_GAIN & 0x7Fu) << 8)))
#define UNIT_MS (1200u / ID_WPM)
#define ID_UNITS 52                                  /* AG7EW: A 8, G 12, 7 16, E 4, W 12 */
#define TX_MS (WSPR_LEAD_MS + FRAME_MS + 400u + ID_UNITS * UNIT_MS + 1000u)
#define SLOT_MS (120000u * WSPR_EVERY)

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

/* wait until ms() reaches t; 0, or -4 on a key (checked at the start and every 0.5 s: about
   60 us each) */
static int until(const k6_api_t *api, uint32_t t) {
  uint32_t kt = api->ms();
  if (api->key_down()) return -4;
  while ((int32_t)(api->ms() - t) < 0) {
    api->delay_us(500);
    if (api->ms() - kt >= 500u) {
      kt = api->ms();
      if (api->key_down()) return -4;
    }
  }
  return 0;
}

/* still keyed? tx_pa(1) only re-enables the PA that tx_on enabled; -1 once the core keyed down */
static int keyed(const k6_api_t *api) {
  return api->tx_pa(1) < 0 ? -5 : 0;
}

/* the channel clear for WSPR_CLEAR_MS: 0, -4 on a key, -7 if it is busy after wait_ms */
static int clear(const k6_api_t *api, uint32_t wait_ms) {
  uint32_t t0 = api->ms(), c = t0;
  while (api->ms() - c < WSPR_CLEAR_MS) {
    if (KM_RSSI_DBM(api->fd_read(0x67)) >= WSPR_CLEAR_DBM) {
      c = api->ms();
      if (c - t0 > wait_ms) return -7;
    }
    if (api->key_down()) return -4;
    api->delay_us(10000);
  }
  return 0;
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

/* one frame and its ID, keyed; the symbols sent and *id the ID's units, or a negative error */
static int frame(const k6_api_t *api, int *id) {
  uint32_t t0, k, j;
  int r;
  if (api->tx_on(TX_MS) != 0) return -3;
  api->fd_write(0x70, 0);
  api->fd_write(0x71, (uint16_t)(WORD(WSPR_AUDIO) + ((WSPR_TONES[0] * TONE_Q16) >> 16)));
  t0 = api->ms() + WSPR_LEAD_MS;
  r = until(api, t0);
  if (!r) api->fd_write(0x70, REG70_ON);
  for (k = 0; !r && k < NSYM; k++) {
    uint32_t t3;
    if ((r = keyed(api)) != 0) break;
    api->fd_write(0x71, (uint16_t)(WORD(WSPR_AUDIO) + ((WSPR_TONES[k] * TONE_Q16) >> 16)));
    /* the next symbol starts at t0 + (k + 1) 2048 / 3 ms: wait for 3 ms() to pass 3 t0 + ... */
    t3 = 3u * t0 + (k + 1) * SYM3;
    for (j = 0; !r && (int32_t)(3u * api->ms() - t3) < 0; j++) {
      if (!(j & 127) && api->key_down()) r = -4;    /* about every 70 ms */
      api->delay_us(500);
    }
  }
  api->fd_write(0x70, 0);
  if (!r) r = until(api, api->ms() + 400);    /* a gap, then the ID */
  if (!r) {
    *id = mcw(api);
    if (*id < 0) r = *id;
  }
  api->fd_write(0x70, 0);
  api->fd_write(0x71, 0);
  api->tx_off();
  return r ? r : (int)k;
}

int k6_main(const k6_api_t *api) {
  char line[64], *p;
  uint32_t next = 0;
  int sent = 0, n, r, id = 0, sym;
  if (api->level < 3) return -1;
  p = put(line, "wspr " WSPR_TEXT " f=");
  p = dec(p, WSPR_AUDIO);
  p = put(p, " g=");
  p = dec(p, WSPR_GAIN);
  p = put(p, " n=");
  p = dec(p, WSPR_REPEAT);
  p = put(p, " rx=");
  p = dec(p, api->rx_hz());
  *p = 0;
  api->say(line);
  if (api->rx_hz() != WSPR_HZ) return -2;
  if ((r = clear(api, WSPR_WAIT_MS)) != 0) return r;     /* the upload's sender has unkeyed */
  for (n = 0; n < WSPR_REPEAT; n++) {
    if (!n) {
      next = api->ms();                         /* the first key-up sets the 2-minute grid */
    } else {                                    /* a beacon: the next slot on the grid */
      p = put(line, "wspr next ");
      p = dec(p, ((next - api->ms() + 500u) * 4194u) >> 22);   /* / 1000, no divide */
      p = put(p, " s");
      *p = 0;
      api->say(line);
      if ((r = until(api, next - WSPR_CLEAR_MS)) != 0) return r;
      r = clear(api, 200u);                     /* 1 s clear brings it to the slot's key-up */
      if (r == -4) return r;
      if (r) {
        api->say("wspr busy, slot skipped");
        next += SLOT_MS;
        continue;
      }
    }
    next += SLOT_MS;
    sym = frame(api, &id);
    if (sym < 0) return sym;
    sent++;
    p = put(line, "wspr sent ");
    p = dec(p, (uint32_t)sent);
    p = put(p, " sym=");
    p = dec(p, (uint32_t)sym);
    p = put(p, " id=");
    p = dec(p, (uint32_t)id);
    *p = 0;
    api->say(line);
  }
  return sent;
}
