/* Run-time ABI helpers for the bare-metal Cortex-M0 bench (cm0/run.py): 32-bit division and
 * 64-bit multiply, shifts and division, which ARMv6-M has no instructions for. Written with
 * 32-bit operations only, as libgcc's Thumb-1 versions are, so their cycles are of the same
 * order (a 32-bit division is a shift-and-subtract loop here, ~60-150 cycles).
 * SPDX-License-Identifier: MIT */
#include <stdint.h>
typedef union { uint64_t u; int64_t s; struct { uint32_t lo, hi; } w; } u64;

uint64_t __aeabi_llsl(uint64_t x, int n) {
  u64 a, r; a.u = x;
  if (n >= 32) { r.w.hi = a.w.lo << (n - 32); r.w.lo = 0; }
  else if (n == 0) return x;
  else { r.w.hi = (a.w.hi << n) | (a.w.lo >> (32 - n)); r.w.lo = a.w.lo << n; }
  return r.u;
}
uint64_t __aeabi_llsr(uint64_t x, int n) {
  u64 a, r; a.u = x;
  if (n >= 32) { r.w.lo = a.w.hi >> (n - 32); r.w.hi = 0; }
  else if (n == 0) return x;
  else { r.w.lo = (a.w.lo >> n) | (a.w.hi << (32 - n)); r.w.hi = a.w.hi >> n; }
  return r.u;
}
int64_t __aeabi_lasr(int64_t x, int n) {
  u64 a, r; a.s = x;
  if (n >= 32) { r.w.lo = (uint32_t)((int32_t)a.w.hi >> (n - 32)); r.w.hi = (uint32_t)((int32_t)a.w.hi >> 31); }
  else if (n == 0) return x;
  else { r.w.lo = (a.w.lo >> n) | (a.w.hi << (32 - n)); r.w.hi = (uint32_t)((int32_t)a.w.hi >> n); }
  return r.s;
}
/* 64 x 64 -> 64: the low 32 x 32 -> 64 from four 16 x 16 products, plus the two cross terms */
uint64_t __aeabi_lmul(uint64_t x, uint64_t y) {
  u64 a, b, r; uint32_t al, ah, bl, bh, ll, lh, hl, mid, lo;
  a.u = x; b.u = y;
  al = a.w.lo & 0xffff; ah = a.w.lo >> 16; bl = b.w.lo & 0xffff; bh = b.w.lo >> 16;
  ll = al * bl; lh = al * bh; hl = ah * bl;
  mid = (ll >> 16) + (lh & 0xffff) + (hl & 0xffff);
  lo = (ll & 0xffff) | (mid << 16);
  r.w.hi = ah * bh + (lh >> 16) + (hl >> 16) + (mid >> 16) + a.w.lo * b.w.hi + a.w.hi * b.w.lo;
  r.w.lo = lo;
  return r.u;
}
static uint32_t udiv32(uint32_t n, uint32_t d, uint32_t *rem) {
  uint32_t q = 0, bit = 1;
  if (d == 0) { *rem = n; return 0xffffffffu; }
  while (d < n && !(d & 0x80000000u)) { d <<= 1; bit <<= 1; }
  while (bit) {
    if (n >= d) { n -= d; q |= bit; }
    d >>= 1; bit >>= 1;
  }
  *rem = n;
  return q;
}
uint32_t __aeabi_uidiv(uint32_t n, uint32_t d) { uint32_t r; return udiv32(n, d, &r); }
int32_t __aeabi_idiv(int32_t n, int32_t d) {
  uint32_t r, q = udiv32(n < 0 ? -(uint32_t)n : (uint32_t)n, d < 0 ? -(uint32_t)d : (uint32_t)d, &r);
  return (n < 0) != (d < 0) ? -(int32_t)q : (int32_t)q;
}
/* quotient in r0, remainder in r1: returned as one 64-bit value */
uint64_t __aeabi_uidivmod(uint32_t n, uint32_t d) { u64 x; x.w.lo = udiv32(n, d, &x.w.hi); return x.u; }
uint64_t __aeabi_idivmod(int32_t n, int32_t d) {
  u64 x; uint32_t r, q = udiv32(n < 0 ? -(uint32_t)n : (uint32_t)n, d < 0 ? -(uint32_t)d : (uint32_t)d, &r);
  x.w.lo = (n < 0) != (d < 0) ? -q : q;
  x.w.hi = n < 0 ? -r : r;
  return x.u;
}
/* count leading zeros (ARMv6-M has no CLZ): binary search, ~20 cycles */
int __clzsi2(uint32_t x) {
  int n = 0;
  if (x == 0) return 32;
  if (!(x & 0xffff0000u)) { n += 16; x <<= 16; }
  if (!(x & 0xff000000u)) { n += 8; x <<= 8; }
  if (!(x & 0xf0000000u)) { n += 4; x <<= 4; }
  if (!(x & 0xc0000000u)) { n += 2; x <<= 2; }
  if (!(x & 0x80000000u)) n += 1;
  return n;
}
