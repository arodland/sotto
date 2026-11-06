/* settle: Phase 0 of the K6 explorer (docs/k6-modules.md, the explorer design). How long after
   a retune does the FD6818's RSSI read true? Every sweep speed in the design rests on that.

   With a steady carrier on the pager's frequency F (the IC-7100, keyed after the module is
   sent), for each way of arriving at F, at wide and at narrow:
     10k   from F - 10 kHz       (a raster step)
     1M    from F - 1 MHz        (a long hop in the band)
     band  from the other band   (146.520 MHz, or 446.000 MHz if F is VHF)
     off   from F to F + 1 MHz   (the carrier going away: how fast RSSI falls)
   it parks on the start frequency for 5 ms, tunes, waits t, reads RSSI (REG 0x67), for t
   from 0 to 20 ms, 4 times each. The reference is the reading after 30 ms. The settle time is
   the first t from which every later reading is within 2 dB of it.

   If it starts on a carrier (above -100 dBm: the sender still repeating the module's chunks),
   it first waits for that to drop. Then it waits up to 60 s for the test carrier: RSSI 15 dB
   above that floor. Any key stops it. Output, on the serial report (K6 MOD say ...):
     settle 10k W ref -61 dBm 700 us        one per case
     curve 10k W 0:-118 100:-118 ... 20000:-61   (dBm per t in us, mean of the 4)
     probe on|off 63=.. 64=.. 65=.. 6f=.. 4f=..   noise/glitch/AF registers, carrier on and off
   It returns the worst settle time of the "on" cases in us, or -1 (no carrier), -2 (a key).
   Needs API level 2 (an image from 2026-11-06 on). Build and send it like hello.c:
     python -m modem.k6 mod settle.wav c/k6/mod/settle.c --key-file KEY --phy F --payload XL
   No libraries, no division, no writable statics. SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 2
#include "../k6_mod.h"

#define NT 13
#define REPS 4
#define PARK_US 5000
#define REF_US 30000
#define TOL_DB 2
#define NOISE_MAX_DBM (-100)

static char *put(char *p, const char *s) {
  while (*s) *p++ = *s++;
  return p;
}

static char *hex(char *p, uint32_t v, int digits) {
  const char *x = "0123456789abcdef";
  while (digits--) *p++ = x[(v >> (4 * digits)) & 15];
  return p;
}

/* unsigned decimal without a divide: subtract powers of ten */
static char *dec(char *p, uint32_t v) {
  static const uint32_t pw[] = {100000, 10000, 1000, 100, 10, 1};
  int i, started = 0;
  for (i = 0; i < 6; i++) {
    int d = 0;
    while (v >= pw[i]) v -= pw[i], d++;
    if (d || started || i == 5) *p++ = (char)('0' + d), started = 1;
  }
  return p;
}

/* hz as MHz with three decimals (kHz), by subtraction too (a plain loop per digit, clang
   turns into a divide) */
static char *mhz(char *p, uint32_t hz) {
  static const uint32_t pw[] = {1000000000, 100000000, 10000000, 1000000, 100000, 10000, 1000};
  int i, started = 0;
  for (i = 0; i < 7; i++) {
    int d = 0;
    while (hz >= pw[i]) hz -= pw[i], d++;
    if (d || started || i >= 3) *p++ = (char)('0' + d), started = 1;
    if (i == 3) *p++ = '.';
  }
  return p;
}

static char *sdec(char *p, int v) {
  if (v < 0) *p++ = '-', v = -v;
  return dec(p, (uint32_t)v);
}

static int rssi(const k6_api_t *api) { return KM_RSSI_DBM(api->fd_read(0x67)); }

/* mean of n readings 1 ms apart */
static int level(const k6_api_t *api, int n) {
  int s = 0, i;
  for (i = 0; i < n; i++) s += rssi(api), api->delay_us(1000);
  return n == 8 ? s >> 3 : n == 4 ? s >> 2 : s;
}

static void probe(const k6_api_t *api, const char *what) {
  static const uint8_t regs[] = {0x63, 0x64, 0x65, 0x6f, 0x4f, 0x0c};
  char line[64], *p = put(line, "probe ");
  unsigned i;
  p = put(p, what);
  for (i = 0; i < sizeof regs; i++) {
    *p++ = ' ';
    p = hex(p, regs[i], 2);
    *p++ = '=';
    p = hex(p, api->fd_read(regs[i]), 4);
  }
  *p = 0;
  api->say(line);
}

/* one case: returns the settle time in us, or -2 if a key was pressed */
static int one(const k6_api_t *api, const char *name, uint32_t from, uint32_t to, int wide) {
  static const uint32_t ts[NT] = {0, 100, 200, 400, 700, 1000, 1500, 2000, 3000, 5000, 8000, 12000, 20000};
  int mean[NT], lo[NT], hi[NT], ref, k, j, settle;
  char line[64], *p;
  for (k = 0; k < NT; k++) {
    int s = 0;
    lo[k] = 1000, hi[k] = -1000;
    for (j = 0; j < REPS; j++) {
      int r;
      if (api->key_down()) return -2;
      api->tune(from, wide);
      api->delay_us(PARK_US);
      api->tune(to, wide);
      api->delay_us(ts[k]);
      r = rssi(api);
      s += r;
      if (r < lo[k]) lo[k] = r;
      if (r > hi[k]) hi[k] = r;
    }
    mean[k] = s >> 2;
  }
  api->tune(from, wide);
  api->delay_us(PARK_US);
  api->tune(to, wide);
  api->delay_us(REF_US);
  ref = level(api, 4);
  /* the first t from which every reading (all REPS of it and of every later t) is within TOL */
  settle = -1;
  for (k = NT - 1; k >= 0; k--) {
    if (lo[k] < ref - TOL_DB || hi[k] > ref + TOL_DB) break;
    settle = (int)ts[k];
  }
  p = put(line, "settle ");
  p = put(p, name);
  p = put(p, wide ? " W ref " : " N ref ");
  p = sdec(p, ref);
  p = put(p, " dBm ");
  if (settle < 0) p = put(p, ">20000"); else p = dec(p, (uint32_t)settle);
  p = put(p, " us");
  *p = 0;
  api->say(line);
  /* the curve, as many lines as it takes at 60 characters */
  p = put(line, "curve ");
  p = put(p, name);
  p = put(p, wide ? " W" : " N");
  for (k = 0; k < NT; k++) {
    if (p - line > 46) {
      *p = 0;
      api->say(line);
      p = put(line, "  ");
    }
    *p++ = ' ';
    p = dec(p, ts[k]);
    *p++ = ':';
    p = sdec(p, mean[k]);
  }
  *p = 0;
  api->say(line);
  return settle < 0 ? 20001 : settle;
}

int k6_main(const k6_api_t *api) {
  uint32_t f, other, t0;
  int base, w, worst = 0, r;
  char line[64], *p;
  if (api->level < 2) return -3;
  f = api->rx_hz();
  other = f >= 300000000u ? 146520000u : 446000000u;
  api->tune(f, 1);
  api->delay_us(REF_US);
  base = level(api, 8);
  /* the sender's own carrier may still be up (it repeats the module's chunks after the last one
     we needed): a start above -100 dBm is a carrier, not the noise floor. Wait for it to go. */
  if (base > NOISE_MAX_DBM) {
    api->say("settle: waiting for the sender's carrier to drop");
    t0 = api->ms();
    while (rssi(api) > base - 15) {
      if (api->key_down()) return -2;
      if (api->ms() - t0 > 60000) {
        api->say("settle: the sender's carrier stayed up 60 s");
        return -1;
      }
      api->delay_us(10000);
    }
    api->delay_us(200000);
    base = level(api, 8);
  }
  p = put(line, "settle: waiting for a carrier on ");
  p = mhz(p, f);
  p = put(p, " MHz, now ");
  p = sdec(p, base);
  p = put(p, " dBm");
  *p = 0;
  api->say(line);
  t0 = api->ms();
  while (rssi(api) < base + 15) {
    if (api->key_down()) return -2;
    if (api->ms() - t0 > 60000) {
      api->say("settle: no carrier in 60 s");
      return -1;
    }
    api->delay_us(10000);
  }
  api->delay_us(200000);
  p = put(line, "settle: carrier at ");
  p = sdec(p, level(api, 8));
  p = put(p, " dBm after ");
  p = dec(p, api->ms() - t0);
  p = put(p, " ms");
  *p = 0;
  api->say(line);
  probe(api, "on ");
  for (w = 1; w >= 0; w--) {
    if ((r = one(api, "10k", f - 10000, f, w)) < 0) return r;
    if (r > worst) worst = r;
    if ((r = one(api, "1M", f - 1000000, f, w)) < 0) return r;
    if (r > worst) worst = r;
    if ((r = one(api, "band", other, f, w)) < 0) return r;
    if (r > worst) worst = r;
    if ((r = one(api, "off", f, f + 1000000, w)) < 0) return r;
  }
  api->tune(f + 1000000, 1);
  api->delay_us(REF_US);
  probe(api, "off");
  p = put(line, "settle: done in ");
  p = dec(p, api->ms() - t0);
  p = put(p, " ms, worst on-case ");
  p = dec(p, (uint32_t)worst);
  p = put(p, " us");
  *p = 0;
  api->say(line);
  return worst;
}
