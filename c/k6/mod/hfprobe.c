/* hfprobe: can the K6 hear below 18 MHz? (docs/k6-hf.md). Receive only.

   The FD6818 (BK4819 numbering) is specified from 18 MHz, but nothing in Aura's tune path stops a
   lower frequency word. Where the synthesizer really stops is shown by signals whose frequency is
   known exactly: the radio's own clock harmonics (12, 13, 16 MHz ...) and WWV / CHU. A probe that
   is really tuned sees its birdie or carrier at its own frequency and not 30 kHz away.

   For each configuration (up to NCFG) and each probe frequency (up to NPROBE), the module tunes
   the probe, waits full_us, takes the mean of 4 reads 1 ms apart (sig), and does the same ref_hz
   above it (ref, the floor beside it). A configuration is two knobs:
     path  the RX front-end path, MCU pins PA13 (UHF LNA path) and PA14 (VHF path), set after each
           tune: 0 Aura's choice (VHF below 300 MHz), 1 UHF, 2 VHF, 3 neither, 4 both
     reg   one FD6818 register written for the configuration (0 = none) and put back after it,
           e.g. REG_3E (0xA037, the band selection threshold: VCO max / 96 / 640 by egzumer's
           note) or REG_1F (0x5454, PLL/VCO bias). REG_30/33/36/37 stay refused by the core.
   Lines (serial: every probe; telemetry: the S rows, which come last):
     p 2 13.000 -101 -118          configuration, probe MHz, sig, ref
     r 2 3e a037>b000 ok            a configuration's register write (or "refused")
     S2 <2 chars a probe>           sig and ref per probe, '0' + (dBm + 160) / 2 as rxmap's rows
     H n=8 c=6 ms=12345
   Writes nothing else; the core puts the pager's frequency and set-up back afterwards. Returns
   the probes (over all configurations) 10 dB or more over their ref, or -2 a key, -3 an image
   below level 2, -11 a bad parameter block. SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 2
#define K6_MODULE_OPT z
#include "../k6_mod.h"

#define NPROBE 16
#define NCFG 8
#define GPIOA_BSRR 0x48000018u
#define GPIOA_BRR 0x48000028u
#define PA13 (1u << 13)
#define PA14 (1u << 14)

typedef struct {
  uint8_t path, reg;
  uint16_t val;
} cfg_t;

typedef struct {
  char magic[8];          /*  0 "HFPROBE1" */
  uint32_t ref_hz;        /*  8 the floor's offset above each probe */
  uint16_t full_us;       /* 12 */
  uint8_t nprobe, ncfg;   /* 14 */
  uint8_t wide, pad[3];   /* 16 */
  uint32_t probe_hz[NPROBE]; /* 20 */
  cfg_t cfg[NCFG];        /* 84 */
} par_t;                  /* 116 bytes */

static const volatile par_t PAR = {
    {'H', 'F', 'P', 'R', 'O', 'B', 'E', '1'},
    30000u, 30000, 4, 2, 1, {0, 0, 0},
    {12000000u, 13000000u, 15000000u, 26000000u},
    {{0, 0, 0}, {3, 0, 0}}};

#define CLEAR_DBM (-100)

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

static char *hex(char *p, uint32_t v, int digits) {
  int i;
  for (i = (digits - 1) * 4; i >= 0; i -= 4) *p++ = "0123456789abcdef"[(v >> i) & 15];
  return p;
}

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

static void say0(const k6_api_t *api, char *line, char *p) {
  *p = 0;
  api->say(line);
}

static int rssi(const k6_api_t *api) { return KM_RSSI_DBM(api->fd_read(0x67)); }

static int key(const k6_api_t *api) { return api->level >= 4 ? api->key_poll() : api->key_down(); }

/* the front-end path after a tune (path 0 leaves Aura's) */
static void path(uint8_t p) {
  if (p == 1) wr(GPIOA_BRR, PA14), wr(GPIOA_BSRR, PA13);
  else if (p == 2) wr(GPIOA_BRR, PA13), wr(GPIOA_BSRR, PA14);
  else if (p == 3) wr(GPIOA_BRR, PA13 | PA14);
  else if (p == 4) wr(GPIOA_BSRR, PA13 | PA14);
}

static int full(const k6_api_t *api, const par_t *q, uint32_t hz, uint8_t p) {
  int s = 0, i;
  api->tune(hz, q->wide);
  path(p);
  api->delay_us(q->full_us);
  for (i = 0; i < 4; i++) {
    if (i) api->delay_us(1000);
    s += rssi(api);
  }
  return s >> 2;
}

static char ch(int dbm) {
  int x = dbm + 160;
  x = x < 0 ? 0 : x > 126 ? 63 : x >> 1;
  return (char)('0' + x);
}

/* wait (up to 30 s) for the sender's own carrier on the pager frequency to drop */
static int clear(const k6_api_t *api, const par_t *q) {
  uint32_t t0 = api->ms();
  int r = full(api, q, api->rx_hz(), 0);
  if (r <= CLEAR_DBM) return 0;
  while (rssi(api) > CLEAR_DBM && rssi(api) > r - 15) {
    if (key(api)) return -2;
    if (api->ms() - t0 > 30000) break;
    api->delay_us(10000);
  }
  api->delay_us(200000);
  return 0;
}

int k6_main(const k6_api_t *api) {
  par_t q;
  uint8_t *d = (uint8_t *)&q, sig[NCFG][NPROBE], ref[NCFG][NPROBE];
  const volatile uint8_t *src = (const volatile uint8_t *)&PAR;
  uint32_t i, c, t0;
  int hits = 0, r;
  char line[64], *p;
  if (api->level < 2) return -3;
  for (i = 0; i < sizeof q; i++) d[i] = src[i];
  if (!q.nprobe || q.nprobe > NPROBE || !q.ncfg || q.ncfg > NCFG || q.full_us < 1000) return -11;
  for (c = 0; c < q.ncfg; c++)
    if (q.cfg[c].path > 4) return -11;
  r = clear(api, &q);
  if (r) return r;
  t0 = api->ms();
  for (c = 0; c < q.ncfg; c++) {
    const cfg_t *g = &q.cfg[c];
    uint16_t old = 0;
    int wrote = 0;
    if (g->reg) {
      old = api->fd_read(g->reg);
      wrote = api->fd_write(g->reg, g->val) == 0;
      p = put(line, "r ");
      p = dec(p, c);
      *p++ = ' ';
      p = hex(p, g->reg, 2);
      *p++ = ' ';
      p = hex(p, old, 4);
      *p++ = '>';
      p = hex(p, g->val, 4);
      say0(api, line, put(p, wrote ? " ok" : " refused"));
    }
    for (i = 0; i < q.nprobe; i++) {
      int s, f;
      if (key(api)) {
        if (wrote) api->fd_write(g->reg, old);
        path(0);
        return -2;
      }
      s = full(api, &q, q.probe_hz[i], g->path);
      f = full(api, &q, q.probe_hz[i] + q.ref_hz, g->path);
      sig[c][i] = (uint8_t)(s + 174), ref[c][i] = (uint8_t)(f + 174);
      if (s >= f + 10) hits++;
      p = put(line, "p ");
      p = dec(p, c);
      *p++ = ' ';
      p = mhz(p, q.probe_hz[i]);
      *p++ = ' ';
      p = sdec(p, s);
      *p++ = ' ';
      say0(api, line, sdec(p, f));
    }
    if (wrote) api->fd_write(g->reg, old);
  }
  path(0);
  for (c = 0; c < q.ncfg; c++) {
    p = put(line, "S");
    p = dec(p, c);
    *p++ = ' ';
    for (i = 0; i < q.nprobe; i++) *p++ = ch((int)sig[c][i] - 174), *p++ = ch((int)ref[c][i] - 174);
    say0(api, line, p);
  }
  p = put(line, "H n=");
  p = dec(p, q.nprobe);
  p = put(p, " c=");
  p = dec(p, q.ncfg);
  p = put(p, " ms=");
  say0(api, line, dec(p, api->ms() - t0));
  return hits;
}
