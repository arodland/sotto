/* sp_cb: the decoder's shape codebook in the K6's SPI flash (see sp_cb.h).
   SPDX-License-Identifier: MIT */
#include <string.h>
#include "sp_cb.h"
#include "../fixed/fx.h"
#include "../fixed/fx_tables.h"

#define VEC_BYTES (FX_CBW * 2)
#if !defined(FX_CB_SPI) || defined(SP_CB_INSTALL)
#define HAVE_TABLE 1
#endif
#if defined(FX_CB_SPI) && !defined(SP_CB_INSTALL) && !defined(SP_CB_CRC)
#error "an FX_CB_SPI build without the table needs -DSP_CB_CRC=0x... (tools/k6_build.sh sets it)"
#endif

static int ready = -1;              /* the last check's result; > 0: the decoder may read */
static uint32_t reads;

uint32_t sp_cb_crc32(uint32_t crc, const uint8_t *p, unsigned n) {
  int k;
  crc = ~crc;
  while (n--) {
    crc ^= *p++;
    for (k = 0; k < 8; k++) crc = (crc >> 1) ^ (0xEDB88320u & (0u - (crc & 1)));
  }
  return ~crc;
}

static void put16(uint8_t *p, unsigned v) { p[0] = (uint8_t)v, p[1] = (uint8_t)(v >> 8); }
static void put32(uint8_t *p, uint32_t v) { put16(p, v & 0xFFFF), put16(p + 2, v >> 16); }
#ifdef FX_CB_SPI
static uint32_t get32(const uint8_t *p) {
  return p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}
#endif

static uint32_t table_bytes(void) {
  uint32_t n = 0;
  int s;
  for (s = 0; s < fx_cb_stages; s++) n += (uint32_t)VEC_BYTES << fx_cb_bits[s];
  return n;
}

#ifdef FX_CB_SPI
/* the first vector of stage s, counted over all stages */
static uint32_t stage_base(int s) {
  uint32_t v = 0;
  int i;
  for (i = 0; i < s; i++) v += 1u << fx_cb_bits[i];
  return v;
}
#endif

int sp_cb_mode(void) {
#if !defined(FX_CB_SPI)
  return SP_CB_MODE_TABLE;
#elif defined(SP_CB_INSTALL)
  return SP_CB_MODE_INSTALL;
#else
  return SP_CB_MODE_SPI;
#endif
}

int sp_cb_ready(void) {
#ifdef FX_CB_SPI
  return ready > 0;
#else
  return 1;
#endif
}

uint32_t sp_cb_reads(void) { return reads; }

#ifdef HAVE_TABLE
/* n bytes of the table from byte off, little-endian as the image holds them */
static void table_read(uint32_t off, uint8_t *out, unsigned n) {
  while (n--) {
    uint32_t w = off >> 1, s = 0, per;
    for (;;) {
      per = (uint32_t)FX_CBW << fx_cb_bits[s];
      if (w < per) break;
      w -= per, s++;
    }
    *out++ = (uint8_t)((uint16_t)fx_cb[s][w] >> (8 * (off & 1)));
    off++;
  }
}

static uint32_t table_crc(void) {
  static uint32_t crc;
  static int have;
  if (!have) {
    uint8_t b[64];
    uint32_t off, len = table_bytes();
    for (off = 0, crc = 0; off < len; off += sizeof b) {
      unsigned n = len - off < sizeof b ? (unsigned)(len - off) : (unsigned)sizeof b;
      table_read(off, b, n);
      crc = sp_cb_crc32(crc, b, n);
    }
    have = 1;
  }
  return crc;
}
#endif

uint32_t sp_cb_expected(void) {
#ifdef HAVE_TABLE
  return table_crc();
#else
  return (uint32_t)(SP_CB_CRC);
#endif
}

static void make_header(uint8_t h[SP_CB_HDR], uint32_t crc) {
  int s;
  memset(h, 0, SP_CB_HDR);
  h[0] = 'K', h[1] = '6', h[2] = 'C', h[3] = 'B';
  put16(h + 4, SP_CB_VERSION);
  put16(h + 6, VEC_BYTES);
  put32(h + 8, table_bytes());
  h[12] = (uint8_t)fx_cb_stages;
  for (s = 0; s < fx_cb_stages && s < 8; s++) h[13 + s] = (uint8_t)fx_cb_bits[s];
  put32(h + 24, crc);
  put32(h + 28, sp_cb_crc32(0, h, 28));
}

int sp_cb_header(uint8_t hdr[SP_CB_HDR]) {
#ifdef HAVE_TABLE
  make_header(hdr, table_crc());
  return 1;
#else
  memset(hdr, 0, SP_CB_HDR);
  return 0;
#endif
}

uint32_t sp_cb_table_bytes(void) { return table_bytes(); }

unsigned sp_cb_table(uint32_t off, uint8_t *out, unsigned n) {
#ifdef HAVE_TABLE
  table_read(off, out, n);
  return n;
#else
  (void)off, (void)out, (void)n;
  return 0;
#endif
}

int sp_cb_check(void) {
#ifdef FX_CB_SPI
  uint8_t h[SP_CB_HDR], want[SP_CB_HDR], b[64];
  uint32_t off, len = table_bytes(), crc = 0;
  sp_flash_read(SP_CB_ADDR, h, SP_CB_HDR);
  make_header(want, sp_cb_expected());
  if (memcmp(h, "K6CB", 4)) return ready = SP_CB_NONE;
  if (get32(h + 28) != sp_cb_crc32(0, h, 28) || memcmp(h, want, 24)) return ready = SP_CB_BAD_HDR;
  if (get32(h + 24) != get32(want + 24)) return ready = SP_CB_WRONG;
  for (off = 0; off < len; off += sizeof b) {
    unsigned n = len - off < sizeof b ? (unsigned)(len - off) : (unsigned)sizeof b;
    sp_flash_read(SP_CB_ADDR + SP_CB_HDR + off, b, n);
    crc = sp_cb_crc32(crc, b, n);
  }
  return ready = crc == get32(want + 24) ? SP_CB_OK : SP_CB_BAD_CRC;
#else
  return ready = SP_CB_OK;
#endif
}

#if defined(FX_CB_SPI) && defined(SP_CB_INSTALL)
/* write the header and the table, in pieces that never cross a 256-byte page */
static void install(void) {
  uint8_t b[64];
  uint32_t len = table_bytes(), end = SP_CB_ADDR + SP_CB_HDR + len, a, off;
  for (a = SP_CB_ADDR; a < end; a += 4096) sp_flash_erase4k(a);
  make_header(b, table_crc());
  sp_flash_write(SP_CB_ADDR, b, SP_CB_HDR);
  for (off = 0; off < len;) {
    uint32_t at = SP_CB_ADDR + SP_CB_HDR + off;
    unsigned n = (unsigned)sizeof b, room = 256 - (unsigned)(at & 255);
    if (n > room) n = room;
    if (n > len - off) n = (unsigned)(len - off);
    table_read(off, b, n);
    sp_flash_write(at, b, n);
    off += n;
  }
}
#endif

int sp_cb_setup(void) {
  int r = sp_cb_check();
#if defined(FX_CB_SPI) && defined(SP_CB_INSTALL)
  if (r < 0) {
    install();
    r = sp_cb_check() > 0 ? SP_CB_INSTALLED : SP_CB_VERIFY;
    ready = r;
  }
#endif
  return r;
}

#ifdef FX_CB_SPI
const int16_t *fx_cb_fetch(int s, int i, int16_t *buf) {
  uint8_t b[VEC_BYTES];
  int k;
  sp_flash_read(SP_CB_ADDR + SP_CB_HDR + (stage_base(s) + (uint32_t)i) * VEC_BYTES, b, VEC_BYTES);
  for (k = 0; k < FX_CBW; k++) buf[k] = (int16_t)(b[2 * k] | b[2 * k + 1] << 8);
  reads++;
  return buf;
}
#endif

uint32_t sp_cb_bench(unsigned n) {
#ifdef FX_CB_SPI
  int16_t v[FX_CBW];
  uint32_t x = 12345, sum = 0;
  unsigned j;
  int k, s = 0;
  if (ready <= 0) return 0;
  for (j = 0; j < n; j++, s = s + 1 < fx_cb_stages ? s + 1 : 0) {
    const int16_t *y;
    x = x * 1103515245u + 12345u;
    y = fx_cb_fetch(s, (int)((x >> 8) & ((1u << fx_cb_bits[s]) - 1)), v);
    for (k = 0; k < FX_CBW; k++) sum = sum * 31 + (uint16_t)y[k];
  }
  return sum;
#else
  (void)n;
  return 0;
#endif
}
