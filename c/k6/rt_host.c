/* rt_host.c: the repeater table's 64 KB of SPI flash on this machine, for tests/test_k6_rt.py
   (built as a shared library with rt.c). NOR rules: starts erased, a write only clears bits
   and stays in a 256-byte page, an erase sets 4 KB to 0xFF. rth_cut(n) cuts the power at the
   n-th operation from now: that write programs only its first half, that erase leaves its
   first half erased and the rest as it was, and everything after fails, as on a radio that
   lost its battery. SPDX-License-Identifier: MIT */
#include <string.h>
#include "rt.h"

static uint8_t nor[RT_SECTORS * 4096];
static long budget = -1, ops, erases, writes;

static int live(void) {
  if (budget == 0) return 0;
  if (budget > 0) budget--;
  ops++;
  return 1;
}

static int h_read(void *ctx, uint32_t a, void *buf, uint32_t n) {
  (void)ctx;
  a -= RT_BASE;
  if (budget == 0 || a + n > sizeof nor) return -1;
  memcpy(buf, nor + a, n);
  return 0;
}

static int h_write(void *ctx, uint32_t a, const void *buf, uint32_t n) {
  const uint8_t *p = buf;
  uint32_t i, m = n;
  (void)ctx;
  a -= RT_BASE;
  if (a + n > sizeof nor || (a & 255) + n > 256) return -2;
  if (budget == 1) m = n / 2;
  if (!live()) return -1;
  for (i = 0; i < m; i++) nor[a + i] &= p[i];
  writes++;
  return budget == 0 ? -1 : 0;
}

static int h_erase(void *ctx, uint32_t a) {
  uint32_t m = 4096;
  (void)ctx;
  a = (a - RT_BASE) & ~4095u;
  if (a >= sizeof nor) return -2;
  if (budget == 1) m = 2048;
  if (!live()) return -1;
  memset(nor + a, 0xFF, m);
  erases++;
  return budget == 0 ? -1 : 0;
}

static const rt_flash_t host = {h_read, h_write, h_erase, 0};

const rt_flash_t *rth_flash(void) { return &host; }
uint8_t *rth_mem(void) { return nor; }
void rth_reset(int fill) { memset(nor, fill, sizeof nor), budget = -1, ops = erases = writes = 0; }
void rth_cut(long n) { budget = n; }
long rth_ops(void) { return ops; }
long rth_erases(void) { return erases; }
long rth_writes(void) { return writes; }
int rth_sizeof_rec(void) { return (int)sizeof(rt_rec_t); }
int rth_sizeof_t(void) { return (int)sizeof(rt_t); }
