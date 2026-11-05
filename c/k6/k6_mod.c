/* k6_mod: run a small native module from the K6's RAM, sent over the air (see k6_mod.h).
   SPDX-License-Identifier: MIT */
#include "k6_mod.h"
#include "sotto_pager.h"
#include <stddef.h>

/* The key: -DKM_KEY=0x12,0x34,... (16 bytes, tools/k6_build.sh), or else a placeholder that
   `python -m modem.k6 keyimage` overwrites in a built .bin (so a CI image can be keyed without
   a toolchain). While the placeholder is there, every module is refused. Read through a
   volatile pointer so the compiler can't fold it into the code, where keyimage couldn't
   find it. */
#define KM_PLACEHOLDER 's', 'o', 't', 't', 'o', '-', 'K', '6', 'M', 'O', 'D', '-', 'k', 'e', 'y', '!'
#define X(c) (uint8_t)((c) ^ 0xFF)  /* stored inverted, so the placeholder is in the image once */
static const uint8_t placeholder_inv[16] = {X('s'), X('o'), X('t'), X('t'), X('o'), X('-'), X('K'), X('6'),
                                            X('M'), X('O'), X('D'), X('-'), X('k'), X('e'), X('y'), X('!')};
#ifdef KM_KEY
static const uint8_t key_store[16] = {KM_KEY};
#else
__attribute__((used)) static const uint8_t key_store[16] = {KM_PLACEHOLDER};
#endif

static km_status_t K;
static uint8_t seen[32];        /* a bit per chunk: up to 255 */
static uint8_t ready;

static uint64_t le64(const uint8_t *p) {
  uint64_t v = 0;
  int i;
  for (i = 7; i >= 0; i--) v = v << 8 | p[i];
  return v;
}

#define ROTL(x, b) (((x) << (b)) | ((x) >> (64 - (b))))
static void rounds(uint64_t *v, int n) {
  while (n--) {
    v[0] += v[1], v[1] = ROTL(v[1], 13), v[1] ^= v[0], v[0] = ROTL(v[0], 32);
    v[2] += v[3], v[3] = ROTL(v[3], 16), v[3] ^= v[2];
    v[0] += v[3], v[3] = ROTL(v[3], 21), v[3] ^= v[0];
    v[2] += v[1], v[1] = ROTL(v[1], 17), v[1] ^= v[2], v[2] = ROTL(v[2], 32);
  }
}

uint64_t km_siphash(const uint8_t k[16], const uint8_t *m, unsigned n) {
  uint64_t k0 = le64(k), k1 = le64(k + 8), b = (uint64_t)n << 56, mi;
  uint64_t v[4];
  unsigned i, j;
  v[0] = k0 ^ 0x736f6d6570736575ULL, v[1] = k1 ^ 0x646f72616e646f6dULL;
  v[2] = k0 ^ 0x6c7967656e657261ULL, v[3] = k1 ^ 0x7465646279746573ULL;
  for (i = 0; i + 8 <= n; i += 8) {
    mi = le64(m + i);
    v[3] ^= mi, rounds(v, 2), v[0] ^= mi;
  }
  for (j = 0; i + j < n; j++) b |= (uint64_t)m[i + j] << (8 * j);
  v[3] ^= b, rounds(v, 2), v[0] ^= b;
  v[2] ^= 0xff, rounds(v, 4);
  return v[0] ^ v[1] ^ v[2] ^ v[3];
}

/* the whole image is in: is it a module of ours? */
static int check(const uint8_t *a, unsigned have_bytes, unsigned dl) {
  unsigned code = a[4] | a[5] << 8, entry = a[6] | a[7] << 8, total = KM_HDR + code + KM_TAG;
  uint32_t serial = (uint32_t)a[8] | (uint32_t)a[9] << 8 | (uint32_t)a[10] << 16 | (uint32_t)a[11] << 24;
  if (a[0] != 'K' || a[1] != '6' || a[2] != 'M' || a[3] != KM_ABI || a[12] > KM_LEVEL ||
      total > have_bytes || total + dl <= have_bytes || entry >= code || (entry & 1))
    return KM_BAD_HDR;
  {
    const volatile uint8_t *kp = key_store;
    uint8_t key[16];
    int i, same = 1;
    for (i = 0; i < 16; i++) key[i] = kp[i], same &= key[i] == (uint8_t)~placeholder_inv[i];
    if (same) return KM_NO_KEY;
    if (km_siphash(key, a, KM_HDR + code) != le64(a + KM_HDR + code)) return KM_BAD_TAG;
  }
  if (serial <= K.serial) return KM_OLD;
  K.serial = serial;
  K.code_len = (uint16_t)code;
  ready = 1;
  return KM_READY;
}

int km_packet(const uint8_t *payload, int len, void *arena, unsigned arena_bytes) {
  uint8_t d[SP_PAYLOAD_MAX], *a = (uint8_t *)arena;
  const uint8_t *w;
  unsigned dl = (unsigned)len - 6u, off, n, i, idx, cnt;
  if (len != SP_PAYLOAD_L && len != SP_PAYLOAD_XL) return KM_NOT_MINE;
  w = sp_pn9();
  if ((payload[0] ^ w[0]) != KM_MAGIC) return KM_NOT_MINE;
  for (i = 0; i < (unsigned)len; i++) d[i] = payload[i] ^ w[i];
  K.chunk = d[2];
  if (sp_crc16(d, len - 2) != ((unsigned)d[len - 2] << 8 | d[len - 1])) return K.last = KM_BAD_CRC;
  idx = d[2], cnt = d[3], off = idx * dl;
  if (!cnt || idx >= cnt || (cnt - 1u) * dl >= arena_bytes) return K.last = KM_BAD_CHUNK;
  if (!K.active && d[1] == K.xfer && cnt == K.count) return K.last = KM_DUP;   /* done already */
  if (!K.active || d[1] != K.xfer || cnt != K.count) {
    /* a new transfer: the pager's store is ours now, so leave it empty and harmless */
    sp_init(arena, 0);
    for (i = 0; i < sizeof seen; i++) seen[i] = 0;
    K.xfer = d[1], K.count = (uint8_t)cnt, K.have = 0, K.active = 1, ready = 0;
  }
  if (seen[idx >> 3] >> (idx & 7) & 1) return K.last = KM_DUP;
  seen[idx >> 3] |= (uint8_t)(1u << (idx & 7));
  n = arena_bytes - off < dl ? arena_bytes - off : dl;
  for (i = 0; i < n; i++) a[off + i] = d[4 + i];
  if (++K.have < cnt) return K.last = KM_CHUNK;
  K.active = 0;
  n = (cnt - 1u) * dl + n;      /* bytes of image held */
  return K.last = (int8_t)check(a, n, dl);
}

int km_run(void *arena, const k6_api_t *api) {
  const uint8_t *a = (const uint8_t *)arena;
  if (!ready) return -100;
  ready = 0;
#if defined(__thumb__)
  {
    typedef int (*entry_t)(const k6_api_t *);
    entry_t f = (entry_t)((uintptr_t)(a + KM_HDR + (a[6] | a[7] << 8)) | 1u);
    __asm volatile("dsb\n\tisb" ::: "memory");
    return f(api);
  }
#else
  (void)a, (void)api;
  return -100;
#endif
}

void km_reset(void) { K.active = 0, ready = 0; }

const km_status_t *km_status(void) { return &K; }
