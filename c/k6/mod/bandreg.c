/* bandreg: which FD6818 registers follow the tuned frequency, and does the PLL lock? (docs/k6-hf.md)
   Receive only, and it writes no register.

   The FD6818 programming guide (chip 0x6818) picks the LO divider by hand: REG_1AH holds a band
   code, from Band_740M_1120M (0x1f80, divide by 2) down to Band_16M_23M (0xdf21, divide by 96), and
   REG_0CH bit 9 reads 1 while the PLL is locked. Jeff's chip (0x4819) uses BK4819 numbering and
   seems to choose the band itself. The bench found the floor going dead below about 16.5 MHz, which
   is where divide by 96 should run out (a VCO near 1.5-1.6 GHz). If the chip shows its band choice
   in some register, the band below (divide by 128, guide code 0x7f40 by the pattern) may be one
   write away.

   For each frequency in the block it tunes, waits settle_us, reads every register from 0x00 to 0x7F
   (except 0x5F, the FSK FIFO, which a read would pop) twice, 2 ms apart, and prints the ones that
   differ from the first frequency's and held still between the two reads. REG_0C comes first,
   always:
     f 440.850 0c=0200                         the first frequency: REG_0C only
     d 16.000 0c=0000 1a=df21 3e=...           a frequency: REG_0C, then each stable difference
   Lines over 60 characters carry on with "+ ". REG_0C bit 9 = PLL lock, if the guide's REG_0CH maps
   straight across (REG_0C is one of the addresses the regmap found the same in both chips).
   Returns the number of frequencies with REG_0C bit 9 set, or -2 a key, -3 an image below level 2,
   -8 scratch under 768 bytes, -11 a bad parameter block. SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 2
#define K6_MODULE_OPT z
#include "../k6_mod.h"

#define NF 24
typedef struct {
  char magic[8];      /*  0 "BANDREG1" */
  uint32_t settle_us; /*  8 */
  uint8_t nf, wide;   /* 12 */
  uint16_t pad;       /* 14 */
  uint32_t hz[NF];    /* 16 */
} par_t;              /* 112 bytes */

static const volatile par_t PAR = {
    {'B', 'A', 'N', 'D', 'R', 'E', 'G', '1'}, 30000u, 6, 1, 0,
    {440850000u, 145000000u, 28000000u, 18000000u, 16000000u, 14000000u}};

#define CLEAR_DBM (-100)

static char *put(char *p, const char *s) {
  while (*s) *p++ = *s++;
  return p;
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

static int key(const k6_api_t *api) { return api->level >= 4 ? api->key_poll() : api->key_down(); }

static void dump(const k6_api_t *api, uint16_t *r) {
  int i;
  for (i = 0; i < 128; i++) r[i] = i == 0x5F ? 0 : api->fd_read((uint8_t)i);
}

int k6_main(const k6_api_t *api) {
  par_t q;
  uint8_t *d = (uint8_t *)&q;
  const volatile uint8_t *src = (const volatile uint8_t *)&PAR;
  uint16_t *base, *a, *b;
  uint32_t i, k, room = 0;
  int locked = 0;
  char line[64], *p;
  if (api->level < 2) return -3;
  for (i = 0; i < sizeof q; i++) d[i] = src[i];
  if (!q.nf || q.nf > NF || q.settle_us < 1000) return -11;
  base = (uint16_t *)api->scratch(&room);
  if (room < 3 * 256) return -8;
  a = base + 128, b = base + 256;
  /* the upload's own carrier on the pager frequency drops first */
  for (k = 0; k < 300 && KM_RSSI_DBM(api->fd_read(0x67)) > CLEAR_DBM; k++) {
    if (key(api)) return -2;
    api->delay_us(100000);
  }
  api->delay_us(200000);
  for (k = 0; k < q.nf; k++) {
    if (key(api)) return -2;
    api->tune(q.hz[k], q.wide);
    api->delay_us(q.settle_us);
    dump(api, a);
    api->delay_us(2000);
    dump(api, b);
    if (b[0x0C] & 0x0200) locked++;
    p = put(line, k ? "d " : "f ");
    p = mhz(p, q.hz[k]);
    p = put(p, " 0c=");
    p = hex(p, b[0x0C], 4);
    if (!k) {
      for (i = 0; i < 128; i++) base[i] = b[i];
    } else {
      for (i = 0; i < 128; i++) {
        if (i == 0x0C || a[i] != b[i] || b[i] == base[i]) continue;
        if (p - line > 50) say0(api, line, p), p = put(line, "+");
        *p++ = ' ';
        p = hex(p, i, 2);
        *p++ = '=';
        p = hex(p, b[i], 4);
      }
    }
    say0(api, line, p);
  }
  return locked;
}
