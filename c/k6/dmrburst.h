/* dmrburst.h: DMR burst metadata on the K6 (docs/k6-dmr.md, "Decoding"). The integer version of
   sim/k6if/dmrdec.py's codes, for a RAM module and the host check (c/k6/dmrburst_host.c,
   tests/test_k6_dmrdec.py): header-only, no statics, no division, no 64-bit, no tables bigger
   than a few words. No voice: AMBE+2 is out.

   Bits are one a byte (0/1), in air order, so the slicer can write them straight in and the
   soft combiner can keep a signed sum per bit instead:
     slot type  Golay (20,8): 8 data bits (colour code, data type), the (23,12) generator
                0xC75 shortened, then an overall parity bit (checked on 440.925: e=0)
     TACT       Hamming (7,4) at CACH bits 0, 4, 8, 12, 14, 18, 22
     full LC    BPTC (196,96): bit i of the info is matrix bit (i * 181) % 196, 13 rows of 15
                after one reserved bit, rows Hamming (15,11,3) and columns (13,9,3); then
                RS (12,9) over GF(2^8) 0x11D, the parity masked per data type (voice LC
                header 0x96, terminator 0x99), CSBKs and data headers CRC-CCITT instead
     EMB        QR (16,7,6): the (17,9) QR code's generator 0x139, overall parity, shortened
     embedded LC  the 4 fragments of voice bursts B-E, 128 bits sent column by column from an
                8 x 16 matrix: rows Hamming (16,11,4), row 7 column parity, 72 LC bits and a
                5-bit checksum (the bytes' sum mod 31)

   SPDX-License-Identifier: MIT */
#ifndef DMRBURST_H
#define DMRBURST_H
#include <stdint.h>

#define DB_NOINLINE __attribute__((noinline))
#ifdef __clang__
#define DB_COLD __attribute__((noinline, minsize)) /* once a burst: size over speed */
#else
#define DB_COLD DB_NOINLINE
#endif

static int db_par(uint32_t v) {
  v ^= v >> 16, v ^= v >> 8, v ^= v >> 4, v ^= v >> 2, v ^= v >> 1;
  return (int)(v & 1);
}

static int db_pop(uint32_t v) {
  int n = 0;
  while (v) v &= v - 1, n++;
  return n;
}

/* bits (one a byte) -> an integer, the first bit the most significant */
static uint32_t db_word(const uint8_t *b, int n) {
  uint32_t v = 0;
  while (n--) v = (v << 1) | *b++;
  return v;
}

/* nearest codeword by trying all 2^k: the k data bits from w's top; *err the distance */
static uint32_t db_slot_cw(uint32_t d) {
  uint32_t r = d << 11, w;
  int i;
  for (i = 18; i > 10; i--)
    if (r & (1u << i)) r ^= 0xC75u << (i - 11);
  w = (d << 11) | (r & 0x7FF);
  return (w << 1) | (uint32_t)db_par(w);
}

static uint32_t db_emb_cw(uint32_t d) {
  uint32_t r = d << 8, w;
  int i;
  for (i = 14; i > 7; i--)
    if (r & (1u << i)) r ^= 0x139u << (i - 8);
  w = (d << 8) | (r & 0xFF);
  return (d << 9) | ((r & 0xFF) << 1) | (uint32_t)db_par(w);
}

DB_NOINLINE static uint32_t db_nearest(uint32_t w, int k, int *err, uint32_t (*cw)(uint32_t)) {
  uint32_t d, best = 0;
  int e = 99;
  for (d = 0; d < (1u << k); d++) {
    int x = db_pop(cw(d) ^ w);
    if (x < e) e = x, best = d;
  }
  *err = e;
  return best;
}

/* the slot type's 20 bits (burst bits 98-107 and 156-165) -> cc << 4 | data type; a clean
   codeword (most bursts) without the search */
static uint32_t db_slot(const uint8_t *b20, int *err) {
  uint32_t w = db_word(b20, 20);
  if (db_slot_cw(w >> 12) == w) return *err = 0, w >> 12;
  return db_nearest(w, 8, err, db_slot_cw);
}

/* the EMB's 16 bits (the burst's middle 8 + its last 8) -> cc << 3 | pi << 2 | lcss */
static uint32_t db_emb(const uint8_t *mid48, int *err) {
  uint32_t w = (db_word(mid48, 8) << 8) | db_word(mid48 + 40, 8);
  if (db_emb_cw(w >> 9) == w) return *err = 0, w >> 9;
  return db_nearest(w, 7, err, db_emb_cw);
}

/* TACT from the 24 CACH bits: at << 3 | tc << 2 | lcss, with 0x10 set if Hamming (7,4)
   corrected a bit (two errors come out wrong, so a caller checks those against what it knows) */
static int db_tact(const uint8_t *cach) {
  /* the syndrome (h2 h1 h0 failing) -> the bit to flip, as an index in d0 d1 d2 d3 h2 h1 h0 */
  static const uint8_t FIX[8] = {7, 6, 5, 3, 4, 0, 2, 1};
  int d0 = cach[0], d1 = cach[4], d2 = cach[8], d3 = cach[12];
  int s = (((d0 ^ d1 ^ d2) ^ cach[14]) << 2) | (((d1 ^ d2 ^ d3) ^ cach[18]) << 1) | ((d0 ^ d1 ^ d3) ^ cach[22]);
  int t = (d0 << 3) | (d1 << 2) | (d2 << 1) | d3;
  if (s) {
    if (FIX[s] < 4) t ^= 8 >> FIX[s];
    t |= 0x10;
  }
  return t;
}

/* Hamming codes as parity masks on a word whose bit (n - 1 - i) is code bit i; the last
   parity bit of each check is its own */
#define DB_M(n, a, b, c, d, e, f, g, h) \
  ((1u << ((n)-1 - (a))) | (1u << ((n)-1 - (b))) | (1u << ((n)-1 - (c))) | (1u << ((n)-1 - (d))) | \
   (1u << ((n)-1 - (e))) | (1u << ((n)-1 - (f))) | (1u << ((n)-1 - (g))) | (1u << ((n)-1 - (h))))
static const uint16_t DB_H16[5] = {
    DB_M(16, 0, 1, 2, 3, 5, 7, 8, 11), DB_M(16, 1, 2, 3, 4, 6, 8, 9, 12),
    DB_M(16, 2, 3, 4, 5, 7, 9, 10, 13), DB_M(16, 0, 1, 2, 4, 6, 7, 10, 14),
    DB_M(16, 0, 2, 5, 6, 8, 9, 10, 15)};
static const uint16_t DB_H13[4] = {
    DB_M(13, 0, 1, 3, 5, 6, 9, 9, 9), DB_M(13, 0, 1, 2, 4, 6, 7, 10, 10),
    DB_M(13, 0, 1, 2, 3, 5, 7, 8, 11), DB_M(13, 0, 2, 4, 5, 8, 12, 12, 12)};

static uint32_t db_syn(uint32_t w, const uint16_t *m, int nm) {
  uint32_t s = 0;
  int i;
  for (i = 0; i < nm; i++) s = (s << 1) | (uint32_t)db_par(w & m[i]);
  return s;
}

/* correct one bit of an n-bit word: 0 clean, 1 fixed, -1 not correctable. Bit i's syndrome
   is bit i of each mask */
DB_NOINLINE static int db_fix(uint32_t *w, int n, const uint16_t *m, int nm) {
  uint32_t s = db_syn(*w, m, nm);
  int i, k;
  if (!s) return 0;
  for (i = 0; i < n; i++) {
    uint32_t si = 0;
    for (k = 0; k < nm; k++) si = (si << 1) | ((m[k] >> i) & 1u);
    if (si == s) {
      *w ^= 1u << i;
      return 1;
    }
  }
  return -1;
}

/* the 15-bit row parity masks are the 16-bit ones without the last bit, shifted down */
static void db_h15(uint16_t *m) {
  int i;
  for (i = 0; i < 4; i++) m[i] = (uint16_t)(DB_H16[i] >> 1);
}

/* BPTC (196,96): 196 info bits -> 12 bytes; returns rows bad << 4 | columns bad after up to
   3 passes of column then row correction (0: clean) */
DB_COLD static int db_bptc196(const uint8_t *info, uint8_t *out12) {
  uint16_t row[13], h15[4];
  uint32_t i, r, c, pass, kr = 0, kc = 0;
  int bad_r = 0, bad_c = 0, fixed = 1;
  db_h15(h15);
  for (r = 0; r < 13; r++) row[r] = 0;
  /* matrix bit k (after the reserved bit 0) = info[(k + 1) * 181 % 196], walked by adding 181
     (row kr, column kc) */
  for (i = 1, c = 181; i < 196; i++) {
    row[kr] |= (uint16_t)(info[c] << (14 - kc));
    if (++kc == 15) kc = 0, kr++;
    c += 181;
    if (c >= 196) c -= 196;
  }
  for (pass = 0; pass < 3 && fixed; pass++) {                /* until a pass changes nothing */
    fixed = bad_r = bad_c = 0;
    for (c = 0; c < 15; c++) {
      uint32_t col = 0;
      int f;
      for (r = 0; r < 13; r++) col = (col << 1) | ((row[r] >> (14 - c)) & 1u);
      if ((f = db_fix(&col, 13, DB_H13, 4)) > 0) {
        fixed++;
        for (r = 0; r < 13; r++)
          row[r] = (uint16_t)((row[r] & ~(1u << (14 - c))) | (((col >> (12 - r)) & 1u) << (14 - c)));
      }
      bad_c += f < 0;
    }
    for (r = 0; r < 9; r++) {
      uint32_t w = row[r];
      int f = db_fix(&w, 15, h15, 4);
      fixed += f > 0, bad_r += f < 0;
      row[r] = (uint16_t)w;
    }
  }
  if (fixed) {                                              /* the last pass fixed: count again */
    bad_r = bad_c = 0;
    for (r = 0; r < 9; r++) bad_r += db_syn(row[r], h15, 4) != 0;
    for (c = 0; c < 15; c++) {
      uint32_t col = 0;
      for (r = 0; r < 13; r++) col = (col << 1) | ((row[r] >> (14 - c)) & 1u);
      bad_c += db_syn(col, DB_H13, 4) != 0;
    }
  }
  /* data: row 0 columns 3-10, rows 1-8 columns 0-10 */
  {
    uint32_t acc = 0, n = 0, o = 0;
    for (r = 0; r < 9; r++)
      for (c = r ? 0 : 3; c < 11; c++) {
        acc = (acc << 1) | ((row[r] >> (14 - c)) & 1u);
        if (++n == 8) out12[o++] = (uint8_t)acc, acc = 0, n = 0;
      }
  }
  return (bad_r << 4) | bad_c;
}

/* GF(2^8) 0x11D multiply by alpha^j (j 1-3), shift and reduce */
static uint32_t db_xa(uint32_t v, int j) {
  while (j--) {
    v <<= 1;
    if (v & 0x100) v ^= 0x11D;
  }
  return v;
}

/* RS (12,9) check with the data type's mask on the parity: 1 if all 3 syndromes are 0 */
DB_NOINLINE static int db_rs_ok(const uint8_t *b12, uint32_t mask) {
  int j, i;
  for (j = 1; j <= 3; j++) {
    uint32_t s = 0;
    for (i = 0; i < 12; i++) s = db_xa(s, j) ^ (b12[i] ^ (i >= 9 ? mask : 0));
    if (s) return 0;
  }
  return 1;
}

/* CRC-CCITT (poly 0x1021) of 10 bytes with init and final XOR; CSBK mask 0xA5A5, data
   header 0xCCCC are XORed in by the caller */
static uint32_t db_crc16(const uint8_t *b, int n, uint32_t init, uint32_t xorout) {
  uint32_t c = init;
  int i;
  while (n--) {
    c ^= (uint32_t)*b++ << 8;
    for (i = 0; i < 8; i++) c = (c & 0x8000) ? ((c << 1) ^ 0x1021) : (c << 1);
  }
  return (c ^ xorout) & 0xFFFF;
}

/* embedded LC: the 4 fragments joined (128 bits, as sent) -> 9 bytes; 1 if every row's
   Hamming (after one-bit fixes), the column parity (one miss allowed: row 7 itself) and the
   checksum agree. One row Hamming can't fix (two errors) is fixed by the columns whose parity
   fails, if that makes it a codeword */
DB_COLD static int db_emb_lc(const uint8_t *f128, uint8_t *out9) {
  uint16_t row[8];
  uint32_t a, r, c, colp = 0, sum = 0, crc = 0, acc = 0, n = 0, o = 0;
  int ok = 1, bad = -1, nbad = 0;
  for (r = 0; r < 8; r++) row[r] = 0;
  for (a = 0; a < 128; a++) row[a & 7] |= (uint16_t)(f128[a] << (15 - (a >> 3)));
  for (r = 0; r < 7; r++) {
    uint32_t w = row[r];
    if (db_fix(&w, 16, DB_H16, 5) < 0) bad = (int)r, nbad++;
    row[r] = (uint16_t)w;
  }
  for (r = 0; r < 8; r++) colp ^= row[r];
  if (nbad == 1 && !db_syn(row[bad] ^ colp, DB_H16, 5)) row[bad] ^= (uint16_t)colp, colp = 0, nbad = 0;
  if (nbad || db_pop(colp) > 1) ok = 0;
  for (r = 0; r < 7; r++)
    for (c = 0; c < (r < 2 ? 11u : 10u); c++) {
      acc = (acc << 1) | ((row[r] >> (15 - c)) & 1u);
      if (++n == 8) sum += out9[o++] = (uint8_t)acc, acc = 0, n = 0;
    }
  for (r = 2; r < 7; r++) crc = (crc << 1) | ((row[r] >> 5) & 1u);
  sum = (sum & 31) + (sum >> 5), sum = (sum & 31) + (sum >> 5); /* mod 31 (32 = 1) */
  if (sum >= 31) sum -= 31;
  return ok && crc == sum;
}

/* 4FSK soft symbol (the sync's outer level = 3 << 8, Q8) -> its two bits as soft values for
   the combiner (positive = 1), clamped to int8 */
static int db_clamp8(int v) { return v > 127 ? 127 : v < -127 ? -127 : v; }
static void db_soft(int u8, int8_t *two) {
  int a = u8 < 0 ? -u8 : u8;
  two[0] = (int8_t)db_clamp8(-u8 >> 4);
  two[1] = (int8_t)db_clamp8((a - 512) >> 4);
}

/* hard decision of a soft symbol: +3 = 01, +1 = 00, -1 = 10, -3 = 11 */
static void db_hard(int u8, uint8_t *two) {
  two[0] = u8 < 0;
  two[1] = (u8 < 0 ? -u8 : u8) > 512;
}

#endif
