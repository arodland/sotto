/* ctcssprobe: does the FD6818's sub-audio scan find a carrier's CTCSS tone? (EXPLORE's tone read)

   Aura's SCAN QT mode (src/drivers/fd6818.rs, ported from the stock firmware's
   Rfic_ScanQT_Enable / Rfic_GetCtsDcsData) sets REG_51 = 0x0300 (the wide sub-audio filter,
   sub-audio TX off) and polls REG_69 (bit 15 clear: a DCS word in 69[11:0] << 12 | 6A[11:0])
   and REG_68 (bit 15 clear: a CTCSS word in [12:0], tenths of Hz = raw * 100000 / 206489).
   Nobody here has seen it on Jeff's chip, so this module listens on the pager's own channel
   for PROBE_S seconds (any key stops it), polls every POLL_MS and says each change:

     t 1234 68=1fff 69=8000 rssi=-60       (ms, the registers)
     ctcss 1000 x12                        (a run of one CTCSS reading, in 0.1 Hz, ended)

   The IC-7100 sends PSK31 'AG7EW TEST' with its own CTCSS on, one tone after another.
   Writes REG_51 only, put back. Receive only. SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 2
#define K6_MODULE_OPT z
#include "../k6_mod.h"

#ifndef PROBE_S
#define PROBE_S 90
#endif
#ifndef POLL_MS
#define POLL_MS 50
#endif
#define SCAN51 0x0300u

static char *put(char *p, const char *s) {
  while (*s) *p++ = *s++;
  return p;
}

static uint32_t udiv(uint32_t n, uint32_t d) {
  uint32_t q = 0, bit = 1;
  if (!d) return 0;
  while (d < n && !(d & 0x80000000u)) d <<= 1, bit <<= 1;
  while (bit) {
    if (n >= d) n -= d, q |= bit;
    d >>= 1, bit >>= 1;
  }
  return q;
}

static char *dec(char *p, uint32_t v) {
  char t[10];
  int n = 0;
  do {
    uint32_t q = udiv(v, 10);
    t[n++] = (char)('0' + v - 10 * q);
    v = q;
  } while (v);
  while (n) *p++ = t[--n];
  return p;
}

static char *hex4(char *p, uint32_t v) {
  int i;
  for (i = 12; i >= 0; i -= 4) *p++ = "0123456789abcdef"[(v >> i) & 15];
  return p;
}

static void say(const k6_api_t *api, char *line, char *p) {
  *p = 0;
  api->say(line);
}

/* a run of one CTCSS reading ended */
static void run_end(const k6_api_t *api, uint32_t tenths, uint32_t n) {
  char line[32], *p;
  if (!n || !tenths) return;
  p = dec(put(line, "ctcss "), tenths);
  p = dec(put(p, " x"), n);
  say(api, line, p);
}

int k6_main(const k6_api_t *api) {
  char line[48], *p;
  uint32_t r51, t, last68 = 0xFFFF, last69 = 0xFFFF, run = 0, run_n = 0, polls = 0, hits = 0;
  if (api->level < 2) return -3;
  r51 = api->fd_read(0x51);
  api->fd_write(0x51, SCAN51);
  p = dec(put(line, "ctcss start r51="), r51);
  say(api, line, p);
  for (t = api->ms(); api->ms() - t < PROBE_S * 1000u && !api->key_down();) {
    uint32_t r69 = api->fd_read(0x69), r68 = api->fd_read(0x68), r6a = 0, tenths = 0;
    int rssi = KM_RSSI_DBM(api->fd_read(0x67));
    polls++;
    if (!(r69 & 0x8000)) r6a = api->fd_read(0x6A);
    if (!(r68 & 0x8000)) tenths = udiv((r68 & 0x1FFF) * 100000u, 206489u), hits++;
    if (tenths != run) run_end(api, run, run_n), run = tenths, run_n = 0;
    run_n++;
    if (r68 != last68 || r69 != last69) {
      last68 = r68, last69 = r69;
      p = dec(put(line, "t "), api->ms() - t);
      p = hex4(put(p, " 68="), r68);
      p = hex4(put(p, " 69="), r69);
      if (r6a) p = hex4(put(p, " 6a="), r6a);
      p = put(p, " rssi=");
      if (rssi < 0) *p++ = '-', rssi = -rssi;
      p = dec(p, (uint32_t)rssi);
      say(api, line, p);
    }
    api->delay_us(POLL_MS * 1000u);
  }
  run_end(api, run, run_n);
  api->fd_write(0x51, (uint16_t)r51);
  p = dec(put(line, "ctcss end polls="), polls);
  p = dec(put(p, " hits="), hits);
  say(api, line, p);
  return (int)hits;
}
