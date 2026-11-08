/* dmrburst_host.c: c/k6/dmrburst.h as a host shared library, for tests/test_k6_dmrdec.py
   (cc -O2 -shared -fPIC -o dmrburst.so dmrburst_host.c). SPDX-License-Identifier: MIT */
#include "dmrburst.h"

int h_slot(const uint8_t *b20, int *err) { return (int)db_slot(b20, err); }
int h_emb(const uint8_t *mid48, int *err) { return (int)db_emb(mid48, err); }
int h_tact(const uint8_t *cach) { return db_tact(cach); }
int h_bptc196(const uint8_t *info, uint8_t *out12) { return db_bptc196(info, out12); }
int h_rs_ok(const uint8_t *b12, int mask) { return db_rs_ok(b12, (uint32_t)mask); }
int h_crc16(const uint8_t *b, int n, int init, int xorout) {
  return (int)db_crc16(b, n, (uint32_t)init, (uint32_t)xorout);
}
int h_emb_lc(const uint8_t *f128, uint8_t *out9) { return db_emb_lc(f128, out9); }
void h_soft(int u8, int8_t *two) { db_soft(u8, two); }
void h_hard(int u8, uint8_t *two) { db_hard(u8, two); }
