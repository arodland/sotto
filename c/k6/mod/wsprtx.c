/* wsprtx: the K6 as a WSPR beacon. It sends WSPR frames itself, as audio 4-FSK on FM from the
   FD6818's tone oscillator, each followed by an MCW ID (module API level 3).

   Jeff, 2026-11-07: "finish out the WSPR beacon and try for receive". The 162 tones come from
   wsprtx_msg.h, which `tools/k6_ft8.py wsprhdr` writes with sim/k6digi/wsprref.py's encoder
   (default "AG7EW CN87 10"). Per transmission the module:

   - waits until the channel has been clear (below WSPR_CLEAR_DBM) for WSPR_CLEAR_MS: the first
     time, that's the upload's sender unkeying; later, it skips a slot rather than key over
     someone;
   - calls tx_on for the frame, the ID and a margin: 300 + 110,592 + 400 + 3,120 + 3,000 ms,
     about 117.4 s (the margin covers a lead over 300 ms when tx_on is quick). That needs an
     image whose module TX cap is 120 s (after 2026-11-07); an older image's 20 s cap keys it
     down partway (-5);
   - turns tone 1 on (REG_70 = enable | WSPR_GAIN << 8) and sets its frequency word REG_71 once a
     symbol: tone k (0-3) at WSPR_AUDIO + 1.4648 k Hz for 8192 / 12000 s (682.67 ms) each, phase
     continuous, 162 symbols. Symbol k starts at t0 + k x 2048 / 3 ms, kept in thirds of a ms;
   - sends AG7EW in MCW (the 800 Hz tone keyed with REG_70) at 20 wpm, and keys down.

   On FM the receiver hears the tones as audio: their frequencies come from the FD6818's crystal
   divided down to 1.5 kHz, so the carrier's drift at 440 MHz (5-20 Hz over a frame, run 5)
   doesn't reach them. A WSPR decoder on the receiver's audio sees a drift-free signal.

   With WSPR_REPEAT > 1 it is a beacon: a transmission every WSPR_EVERY two-minute slots
   (default 5: every 10 minutes, 20% of the time), timed by the module clock (the M0's crystal),
   until the count is done or a key is held. WSPR frames start 1 s after an even UTC minute,
   and the K6 has no UTC clock (only uptime: Aura's TIM14 from the 16 MHz crystal, gone at
   power-off). Without a sync mark (WSPR_SYNC_MS 0, this build) the first frame's tones start
   3.3 s after the upload's sender unkeys. With one (wsprbcn: 10 s), the module waits up to
   WSPR_SYNC_WAIT_MS for a carrier of at least 0.5 s and starts the first frame's tones
   WSPR_SYNC_MS after it drops; `tools/k6_wspr.py sync` keys the IC-7100 so that it drops 10 s
   before a slot by the PC's NTP clock (the drop is seen to about 10 ms). Every frame starts on
   that 2-minute grid, the key-up 3.3 s before its tones (the clear wait, tx_on's 1.4-1.9 s, the
   lead), and the crystal holds the grid to a few ms an hour.

   It says
     K6 MOD say wspr AG7EW CN87 10 f=1500 g=60 n=1 rx=440850000
     K6 MOD say wspr sent 1 sym=162 ms=110592 id=52
     K6 MOD say wspr sync: waiting for the mark             (WSPR_SYNC_MS; then "wspr no sync
                                                            mark: starting now" if none came)
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
#ifndef WSPR_SYNC_MS
#define WSPR_SYNC_MS 0      /* 0: no sync mark. Else the first frame's tones start this long
                               after a sync carrier drops (tools/k6_wspr.py sync) */
#endif
#ifndef WSPR_SYNC_WAIT_MS
#define WSPR_SYNC_WAIT_MS 150000  /* covers the next slot the host can mark */
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
#define TX_MS (WSPR_LEAD_MS + FRAME_MS + 400u + ID_UNITS * UNIT_MS + 3000u)
#define SLOT_MS (120000u * WSPR_EVERY)
#define KEYUP_MS 3300u    /* from a slot's channel check to its tones: the clear wait, tx_on's
                             1.4-1.9 s, the lead */
#if WSPR_SYNC_MS && WSPR_SYNC_MS < 4000
#error "WSPR_SYNC_MS leaves no time for the channel check and key-up"
#endif

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

/* ---- the screen (module API level 6, on images that have it; older ones skip it) ----
   WSPR BEACON  440.850      (inverted)
   AG7EW CN87 10 dBm
   next slot in 7:58  /  TX [=====     ]  /  ID AG7EW  /  waiting for sync
   sent 1 of 6        /  PC: k6_wspr.py sync
   on the UTC grid    /  else start in 2:24
   hold a key to stop */
enum { UI_SYNC, UI_WAIT, UI_TX, UI_ID };
typedef struct {
  uint32_t until;   /* UI_SYNC, UI_WAIT: the countdown's end (ms()) */
  uint16_t k;       /* UI_TX: symbols sent */
  uint8_t phase, sent, synced;
} ui_t;

static char *mss(char *p, uint32_t s) {     /* "7:58", no divide */
  uint32_t m = 0, t = 0;
  while (s >= 60) {                         /* (the asm keeps clang from making a divide) */
    s -= 60, m++;
    __asm__("" : "+r"(s));
  }
  while (s >= 10) {
    s -= 10, t++;
    __asm__("" : "+r"(s));
  }
  p = dec(p, m);
  *p++ = ':', *p++ = (char)('0' + t), *p++ = (char)('0' + s);
  *p = 0;
  return p;
}

static void ui(const k6_api_t *api, const ui_t *u) {
  char l[24], *p;
  uint32_t left;
  if (api->level < 6) return;
  left = (int32_t)(u->until - api->ms()) > 0 ? ((u->until - api->ms() + 500u) * 4194u) >> 22 : 0;
  api->lcd_text(0, WSPR_REPEAT > 1 ? "WSPR BEACON  440.850" : "WSPR TX      440.850");
  api->lcd_text(1, WSPR_TEXT " dBm");
  api->lcd_bar(2, u->k, u->phase == UI_TX ? NSYM : 0);
  if (u->phase == UI_SYNC) {
    api->lcd_text(2, "waiting for sync");
    api->lcd_text(3, "PC: k6_wspr.py sync");
    mss(put(l, "else start in "), left);
    api->lcd_text(4, l);
  } else {
    if (u->phase == UI_WAIT) mss(put(l, "next slot in "), left);
    else put(l, u->phase == UI_TX ? "TX" : "ID " ID_TEXT)[0] = 0;
    api->lcd_text(2, l);
    p = dec(put(l, "sent "), u->sent);
    p = dec(put(p, " of "), WSPR_REPEAT);
    *p = 0;
    api->lcd_text(3, l);
    api->lcd_text(4, u->synced ? "on the UTC grid" : WSPR_SYNC_MS ? "no sync mark" : "");
  }
  api->lcd_text(5, "hold a key to stop");
  api->lcd_flush();
}

/* until() with the screen's countdown redrawn every second */
static int wait_ui(const k6_api_t *api, uint32_t t, const ui_t *u) {
  int r;
  while ((int32_t)(t - api->ms()) > 1000) {
    ui(api, u);
    if ((r = until(api, api->ms() + 1000u)) != 0) return r;
  }
  return until(api, t);
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

#if WSPR_SYNC_MS
/* the sync mark: a carrier held for at least half a second, then dropped. *td = the first ms()
   it read clear (polled every 5 ms); 0, -4 on a key, -7 if none came in WSPR_SYNC_WAIT_MS */
static int sync(const k6_api_t *api, uint32_t *td, ui_t *u) {
  uint32_t t0 = api->ms(), up = 0, now, shown = t0 - 1000u;
  u->until = t0 + WSPR_SYNC_WAIT_MS;
  while ((now = api->ms()) - t0 < WSPR_SYNC_WAIT_MS) {
    if (api->key_down()) return -4;
    if (!up && now - shown >= 1000u) {          /* not while a mark is up: its drop is timed */
      shown = now;
      ui(api, u);
    }
    if (KM_RSSI_DBM(api->fd_read(0x67)) >= WSPR_CLEAR_DBM) {
      if (!up) up = now | 1;
    } else if (up) {
      if (now - up >= 500u) {
        *td = now;
        return 0;
      }
      up = 0;
    }
    api->delay_us(5000);
  }
  return -7;
}
#endif

/* one frame, its tones from t0 (or the lead after tx_on if that is later), and its ID, keyed;
   the symbols sent and *id the ID's units, or a negative error */
static int frame(const k6_api_t *api, int *id, uint32_t t0, ui_t *u) {
  uint32_t k = 0, j, t3;
  int r = 0;
  if (api->tx_on(TX_MS) != 0) return -3;
  api->fd_write(0x70, 0);
  api->fd_write(0x71, (uint16_t)(WORD(WSPR_AUDIO) + ((WSPR_TONES[0] * TONE_Q16) >> 16)));
  if ((int32_t)(t0 - api->ms()) < WSPR_LEAD_MS) t0 = api->ms() + WSPR_LEAD_MS;
  /* the lead (from tx_on's return to the slot: 0.3-0.9 s), then symbol k from t0 + k 2048 / 3 ms,
     each wait checking the key and the core's limit about every 70 ms */
  for (t3 = 3u * t0; !r && k <= NSYM; t3 += SYM3) {
    for (j = 0; !r && (int32_t)(3u * api->ms() - t3) < 0; j++) {
      if (!(j & 127) && ((r = keyed(api)) != 0 || api->key_down())) r = r ? r : -4;
      api->delay_us(500);
    }
    if (r || k == NSYM) break;
    if (!k) api->fd_write(0x70, REG70_ON);
    api->fd_write(0x71, (uint16_t)(WORD(WSPR_AUDIO) + ((WSPR_TONES[k] * TONE_Q16) >> 16)));
    k++;
    u->k = (uint16_t)k;
    if (!(k & 3)) ui(api, u);                   /* the bar every 4 symbols (2.7 s) */
  }
  api->fd_write(0x70, 0);
  if (!r) r = until(api, api->ms() + 400);    /* a gap, then the ID */
  if (!r) {
    u->phase = UI_ID;
    ui(api, u);
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
  ui_t u = {0, 0, UI_WAIT, 0, 0};
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
  next = api->ms() + KEYUP_MS;                  /* no sync: the first frame at once */
#if WSPR_SYNC_MS
  api->say("wspr sync: waiting for the mark");
  {
    uint32_t td;
    u.phase = UI_SYNC;
    r = sync(api, &td, &u);
    u.phase = UI_WAIT;
    u.synced = !r;
    if (r == -4) return r;
    next = r ? api->ms() + KEYUP_MS : td + WSPR_SYNC_MS; /* dropped WSPR_SYNC_MS before a slot */
    if (r) api->say("wspr no sync mark: starting now");
  }
#endif
  for (n = 0; n < WSPR_REPEAT; n++) {
    if (n || WSPR_SYNC_MS != 0) {               /* say how long until the slot */
      p = put(line, "wspr next ");
      p = dec(p, ((next - api->ms() + 500u) * 4194u) >> 22);   /* / 1000, no divide */
      p = put(p, " s");
      *p = 0;
      api->say(line);
    }
    u.until = next;
    u.k = 0;
    if ((r = wait_ui(api, next - KEYUP_MS, &u)) != 0) return r;
    r = clear(api, 200u);                       /* 1 s clear, then key-up for the slot's tones */
    if (r == -4) return r;
    if (r) {
      api->say("wspr busy, slot skipped");
      next += SLOT_MS;
      continue;
    }
    u.phase = UI_TX;
    ui(api, &u);
    sym = frame(api, &id, next, &u);
    u.phase = UI_WAIT;
    next += SLOT_MS;
    if (sym < 0) return sym;
    u.sent = (uint8_t)++sent;
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
