/* sp_vlr: sotto 600 / 400's shape codebooks in the K6's SPI flash (see sp_vlr.h).
   SPDX-License-Identifier: MIT */
#include <string.h>
#include "sp_vlr.h"
#include "sp_cb.h"
#ifdef SP_VLR
#include "../sotto_vlr_tables.h"
#if !defined(VLR_CB_SPI) || defined(SP_VLR_INSTALL)
#define HAVE_TABLE 1
#endif
#if defined(VLR_CB_SPI) && !defined(SP_VLR_INSTALL) && !defined(SP_VLR_CRC)
#error "a VLR_CB_SPI build without the tables needs -DSP_VLR_CRC=0x... (tools/k6_build.sh sets it)"
#endif
#define VEC VLR_NC

static int ready = -1;
static uint32_t reads;

static void put16(uint8_t *p, unsigned v) { p[0] = (uint8_t)v, p[1] = (uint8_t)(v >> 8); }
static void put32(uint8_t *p, uint32_t v) { put16(p, v & 0xFFFF), put16(p + 2, v >> 16); }
#ifdef VLR_CB_SPI
static uint32_t get32(const uint8_t *p) {
  return p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}
#endif

#ifdef HAVE_TABLE
/* Worked out on every call (about 30 ms on the K6; only the install image and the host tools
   have the table), not kept in a static: aura-pager-vlri's boot (Aura's main and place_app,
   6.7 KB of stack with 6.8 KB free) reaches the top of .bss, where this file's statics sit, and
   a cached CRC read back 0 there (`K6 VLR r=-5 crc=00000000`, 2026-11-07). */
static uint32_t table_crc(void) { return sp_cb_crc32(0, (const uint8_t *)vlr_cb_all, vlr_cb_bytes); }
#endif

uint32_t sp_vlr_expected(void) {
#ifdef HAVE_TABLE
  return table_crc();
#else
  return (uint32_t)(SP_VLR_CRC);
#endif
}

static void make_header(uint8_t h[SP_VLR_HDR], uint32_t crc) {
  memset(h, 0, SP_VLR_HDR);
  h[0] = 'K', h[1] = '6', h[2] = 'V', h[3] = 'L';
  put16(h + 4, SP_VLR_VERSION);
  put16(h + 6, VEC);
  put32(h + 8, vlr_cb_bytes);
  put16(h + 12, (unsigned)vlr_mode600.rate);
  put16(h + 14, (unsigned)vlr_mode400.rate);
  put32(h + 24, crc);
  put32(h + 28, sp_cb_crc32(0, h, 28));
}

int sp_vlr_header(uint8_t hdr[SP_VLR_HDR]) {
#ifdef HAVE_TABLE
  make_header(hdr, table_crc());
  return 1;
#else
  memset(hdr, 0, SP_VLR_HDR);
  return 0;
#endif
}

uint32_t sp_vlr_table_bytes(void) { return vlr_cb_bytes; }

unsigned sp_vlr_table(uint32_t off, uint8_t *out, unsigned n) {
#ifdef HAVE_TABLE
  if (off > vlr_cb_bytes) return 0;
  if (n > vlr_cb_bytes - off) n = (unsigned)(vlr_cb_bytes - off);
  memcpy(out, (const uint8_t *)vlr_cb_all + off, n);
  return n;
#else
  (void)off, (void)out, (void)n;
  return 0;
#endif
}

int sp_vlr_ready(void) { return ready > 0; }
uint32_t sp_vlr_reads(void) { return reads; }

int sp_vlr_check(void) {
#ifdef VLR_CB_SPI
  uint8_t h[SP_VLR_HDR], want[SP_VLR_HDR], b[64];
  uint32_t off, len = vlr_cb_bytes, crc = 0;
  sp_flash_read(SP_VLR_ADDR, h, SP_VLR_HDR);
  make_header(want, sp_vlr_expected());
  if (memcmp(h, "K6VL", 4)) return ready = SP_CB_NONE;
  if (get32(h + 28) != sp_cb_crc32(0, h, 28) || memcmp(h, want, 24)) return ready = SP_CB_BAD_HDR;
  if (get32(h + 24) != get32(want + 24)) return ready = SP_CB_WRONG;
  for (off = 0; off < len; off += sizeof b) {
    unsigned n = len - off < sizeof b ? (unsigned)(len - off) : (unsigned)sizeof b;
    sp_flash_read(SP_VLR_ADDR + SP_VLR_HDR + off, b, n);
    crc = sp_cb_crc32(crc, b, n);
  }
  return ready = crc == get32(want + 24) ? SP_CB_OK : SP_CB_BAD_CRC;
#else
  return ready = SP_CB_OK;
#endif
}

#if defined(VLR_CB_SPI) && defined(SP_VLR_INSTALL)
/* the header and the table, in pieces that never cross a 256-byte page */
static void install(void) {
  uint8_t b[SP_VLR_HDR];
  uint32_t len = vlr_cb_bytes, end = SP_VLR_ADDR + SP_VLR_HDR + len, a, off;
  for (a = SP_VLR_ADDR; a < end; a += 4096) sp_flash_erase4k(a);
  make_header(b, table_crc());
  sp_flash_write(SP_VLR_ADDR, b, SP_VLR_HDR);
  for (off = 0; off < len;) {
    uint32_t at = SP_VLR_ADDR + SP_VLR_HDR + off;
    unsigned n = 256 - (unsigned)(at & 255);
    if (n > len - off) n = (unsigned)(len - off);
    sp_flash_write(at, (const uint8_t *)vlr_cb_all + off, n);
    off += n;
  }
}
#endif

int sp_vlr_setup(void) {
  int r = sp_vlr_check();
#if defined(VLR_CB_SPI) && defined(SP_VLR_INSTALL)
  if (r < 0) {
    install();
    r = sp_vlr_check() > 0 ? SP_CB_INSTALLED : SP_CB_VERIFY;
    ready = r;
  }
#endif
  return r;
}

#ifdef VLR_CB_SPI
const int8_t *vlr_cb_fetch(const vlr_mode_t *m, int j, int i, int idx, int8_t buf[VLR_NC]) {
  sp_flash_read(SP_VLR_ADDR + SP_VLR_HDR + m->cboff[j][i] + (uint32_t)idx * VEC, buf, VEC);
  reads++;
  return buf;
}
#endif

#else   /* a build that doesn't play sotto 600 / 400 */
int sp_vlr_setup(void) { return 0; }
int sp_vlr_check(void) { return 0; }
int sp_vlr_ready(void) { return 0; }
uint32_t sp_vlr_expected(void) { return 0; }
uint32_t sp_vlr_reads(void) { return 0; }
int sp_vlr_header(uint8_t hdr[SP_VLR_HDR]) { memset(hdr, 0, SP_VLR_HDR); return 0; }
uint32_t sp_vlr_table_bytes(void) { return 0; }
unsigned sp_vlr_table(uint32_t off, uint8_t *out, unsigned n) { (void)off, (void)out, (void)n; return 0; }
#endif
