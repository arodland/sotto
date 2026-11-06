/* afsweep: step the FD6818's AF output select (REG_47 bits 11:8) through all 16 values while
   the IC-7100 sends FM, then AM, then USB, with the speaker on, so a listener (or a phone
   recording) can hear what each value does (docs/k6-afsweep.md).

   REG_47 on the 0x4819 is the BK4819 layout, 0x6040 | (AF << 8) (Aura's AF_OUT_BASE, egzumer's
   BK4819_SetAF); the FD6818 guide's 5-bit values are the 0x6818's. Known names (egzumer
   driver/bk4819.h): 0 mute, 1 FM, 2 alarm, 3 beep, 4 BASEBAND1 "RAW", 5 BASEBAND2 "USB",
   6 CTCO, 7 AM, 8 FSKO, 9 "BYP", 10-15 unnamed.

   Each pass sets the chip up as Aura's apply_modulation does for that mode, then waits for the
   IC-7100's carrier (RSSI 15 dB over the floor, up to WAIT_S seconds, twice that for the
   first), skips the sender's 1 s lead tone (1.5 s after the carrier; 0.8 s in USB, where RSSI
   rises with the audio, not the key) and runs 16 steps of
   STEP_MS, AF 0 to 15, each after GAP_MS of mute so the steps are easy to tell apart on a
   recording:
     fm:  REG_3D 0x2AAB (8.46 kHz IF), REG_73 bit 4 clear (AFC on), REG_14 0x0019, REG_49 0x2A38,
          REG_48 0xB3BF (Aura's init volume)
     am:  REG_3D 0x2AAB, AFC off, REG_14 0x0000, REG_49 (50 << 7) | 32, REG_48 0xB3AA (Aura's AM)
     usb: REG_3D 0x0000 (zero IF), AFC off, REG_14 0x0019, REG_49 0x2A38, REG_48 0xB3BF
   Between passes it waits for the carrier to drop (the sender unkeys to change mode).

   Every 10 ms of a step it reads RSSI (REG_67), noise (REG_65), glitch (REG_63) and the AF
   level (REG_6F[5:0]); a step's line gives min and max of each:
     s fm 7 r-61 -57 n12 30 g0 3 a20 31
   The speaker amplifier (PB2, high = on: Aura's set_speaker_switch) is on for the passes.
   Writes REG_47 48 3D 73 14 49 and GPIOB BSRR/BRR bit 2, all put back. No TX.
   Returns the passes done, -2 (a key), -3 (old image), -6 (no carrier for a pass).
   SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 2
#include "../k6_mod.h"

#ifndef STEP_MS
#define STEP_MS 3700
#endif
#ifndef GAP_MS
#define GAP_MS 300
#endif
#ifndef WAIT_S
#define WAIT_S 60
#endif
#define GPIOB_ODR 0x48000414u
#define GPIOB_BSRR 0x48000418u
#define GPIOB_BRR 0x48000428u
#define SPK (1u << 2)

static uint32_t rd(uint32_t a) { return *(volatile uint32_t *)a; }
static void wr(uint32_t a, uint32_t v) { *(volatile uint32_t *)a = v; }

static char *put(char *p, const char *s) {
  while (*s) *p++ = *s++;
  return p;
}

static char *dec(char *p, uint32_t v) {
  static const uint32_t pw[] = {1000000000, 100000000, 10000000, 1000000, 100000,
                                10000,      1000,      100,      10,      1};
  int i, started = 0;
  for (i = 0; i < 10; i++) {
    int d = 0;
    while (v >= pw[i]) v -= pw[i], d++;
    if (d || started || i == 9) *p++ = (char)('0' + d), started = 1;
  }
  return p;
}

static char *sdec(char *p, int v) {
  if (v < 0) *p++ = '-', v = -v;
  return dec(p, (uint32_t)v);
}

static int rssi(const k6_api_t *api) { return KM_RSSI_DBM(api->fd_read(0x67)); }

/* registers saved and restored, in this order */
static const uint8_t regs[6] = {0x47, 0x48, 0x3D, 0x73, 0x14, 0x49};

typedef struct {
  char name[4];
  uint16_t r3d, afc_off, r14, r49, r48, lead_ms;
} mode_t;

static const mode_t modes[3] = {
    {"fm", 0x2AAB, 0, 0x0019, 0x2A38, 0xB3BF, 1500},
    {"am", 0x2AAB, 1, 0x0000, (50 << 7) | 32, 0xB3AA, 1500},
    {"usb", 0x0000, 1, 0x0019, 0x2A38, 0xB3BF, 800},
};

typedef struct {
  int lo[4], hi[4];
} stat_t;

/* one step: AF value v for ms, polling every 10 ms */
static void step(const k6_api_t *api, stat_t *st, uint32_t ms) {
  static const uint8_t r[4] = {0x67, 0x65, 0x63, 0x6F};
  uint32_t t0 = api->ms();
  int i;
  for (i = 0; i < 4; i++) st->lo[i] = 0x7FFF, st->hi[i] = -0x7FFF;
  while (api->ms() - t0 < ms) {
    for (i = 0; i < 4; i++) {
      uint16_t x = api->fd_read(r[i]);
      int v = i == 0 ? KM_RSSI_DBM(x) : i == 1 ? (int)(x & 0x7F) : i == 2 ? (int)(x & 0xFF)
                                                                     : (int)(x & 0x3F);
      if (v < st->lo[i]) st->lo[i] = v;
      if (v > st->hi[i]) st->hi[i] = v;
    }
    api->delay_us(10000);
  }
}

static int wait_level(const k6_api_t *api, int above, int lvl, uint32_t ms) {
  uint32_t t0 = api->ms();
  while ((rssi(api) >= lvl) != above) {
    if (api->key_down()) return -2;
    if (api->ms() - t0 > ms) return -6;
    api->delay_us(5000);
  }
  return 0;
}

int k6_main(const k6_api_t *api) {
  static const char tag[4] = {'r', 'n', 'g', 'a'};
  uint16_t saved[6];
  uint32_t spk;
  int floor, ret = 0, m, v, i;
  char line[64], *p;
  stat_t st;
  if (api->abi != 1 || api->level < 2) return -3;
  for (i = 0; i < 6; i++) saved[i] = api->fd_read(regs[i]);
  spk = rd(GPIOB_ODR) & SPK;
  /* the upload's carrier gone, then the floor */
  if (wait_level(api, 0, -80, 30000) == -2) return -2;
  api->delay_us(300000);
  floor = rssi(api);
  p = put(line, "afsweep start floor=");
  p = sdec(p, floor);
  p = put(p, " r47=");
  p = dec(p, saved[0]);
  p = put(p, " r48=");
  p = dec(p, saved[1]);
  *p = 0;
  api->say(line);
  for (m = 0; m < 3; m++) {
    const mode_t *md = &modes[m];
    if (m) {
      /* the sender unkeys to change mode */
      ret = wait_level(api, 0, floor + 10, 30000);
      if (ret) goto out;
      ret = m;
    }
    api->fd_write(0x47, 0x6040);       /* mute while it changes */
    api->fd_write(0x3D, md->r3d);
    api->fd_write(0x73, (uint16_t)((saved[3] & ~0x10u) | (md->afc_off ? 0x10u : 0)));
    api->fd_write(0x14, md->r14);
    api->fd_write(0x49, md->r49);
    api->fd_write(0x48, md->r48);
    p = put(line, "afsweep ");
    p = put(p, md->name);
    p = put(p, " waiting");
    *p = 0;
    api->say(line);
    ret = wait_level(api, 1, floor + 15, (uint32_t)(m ? WAIT_S : 2 * WAIT_S) * 1000u);
    if (ret) goto out;
    api->delay_us((uint32_t)md->lead_ms * 1000u); /* the sender's 1 s lead tone */
    wr(GPIOB_BSRR, SPK);
    for (v = 0; v < 16; v++) {
      api->fd_write(0x47, 0x6040);
      api->delay_us(GAP_MS * 1000u);
      api->fd_write(0x47, (uint16_t)(0x6040 | (v << 8)));
      step(api, &st, STEP_MS);
      p = put(line, "s ");
      p = put(p, md->name);
      *p++ = ' ';
      p = dec(p, (uint32_t)v);
      for (i = 0; i < 4; i++) {
        *p++ = ' ';
        *p++ = tag[i];
        p = sdec(p, st.lo[i]);
        *p++ = ' ';
        p = sdec(p, st.hi[i]);
      }
      *p = 0;
      api->say(line);
      if (api->key_down()) {
        ret = -2;
        goto out;
      }
    }
    ret = m + 1;
  }
out:
  api->fd_write(0x47, 0x6040);
  if (!spk) wr(GPIOB_BRR, SPK);
  for (i = 5; i >= 0; i--) api->fd_write(regs[i], saved[i]);
  api->say("afsweep done");
  return ret;
}
