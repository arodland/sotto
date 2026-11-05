/* k6_mod: run a small native module from the K6's RAM, sent over the air.

   A module arrives as whitened L or XL packets with magic 0xAB
   (docs/k6-modules.md). De-whitened, each packet is

     0xAB, transfer id, chunk number, chunk count, data (len - 6 bytes), CRC-16 (big-endian)

   and the chunks, in order, make the module image:

     0  'K' '6' 'M' 1      magic and ABI major 1
     4  code length        u16, little-endian
     6  entry              u16, byte offset of the entry function in the code (even)
     8  serial             u32, must be newer than the last module this boot accepted
     12 API level, flags   u8, u8 (flags: 0), then 2 reserved bytes
     16 code               position-independent Thumb (clang -fropi), no data or bss
     16 + code length      SipHash-2-4 tag (8 bytes, little-endian) of bytes 0 .. 15 + code length
                           under the 16-byte key the library was built with (KM_KEY)

   The chunks land straight in the pager's message store, which the radio then lacks a message
   for: the first chunk of a new transfer empties the store (sp_init with 0 bytes), and the
   caller re-initialises it with sp_init once the transfer is over. Without KM_KEY (the CI
   build) every module is refused.

   The module runs on the caller's stack, once, as int entry(const k6_api_t *api): it reaches
   the radio only through the table. The table is append-only within ABI major 1.
   SPDX-License-Identifier: MIT */
#ifndef K6_MOD_H
#define K6_MOD_H
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif

#define KM_MAGIC 0xAB
#define KM_HDR 16
#define KM_TAG 8
#define KM_ABI 1
#define KM_LEVEL 1

typedef struct k6_api {
  uint16_t abi, level;              /* KM_ABI, KM_LEVEL */
  void (*say)(const char *s);       /* a serial report line, "K6 MOD say <s>" (up to 60 chars) */
  uint32_t (*ticks)(void);          /* uptime in 100 us ticks, 16 bits: wraps every 6.5536 s */
} k6_api_t;

/* km_packet results */
enum {
  KM_NOT_MINE = 0,                  /* not a module packet: hand it to the pager */
  KM_CHUNK = 1,                     /* a new chunk stored */
  KM_DUP = 2,                       /* a chunk already held */
  KM_READY = 3,                     /* the last chunk: the module checks; km_run it */
  KM_BAD_CRC = -1,
  KM_BAD_CHUNK = -2,                /* chunk number or count out of range for the store */
  KM_BAD_HDR = -3,                  /* magic, ABI, API level, entry or length wrong */
  KM_BAD_TAG = -4,                  /* the SipHash tag doesn't check: not ours */
  KM_NO_KEY = -5,                   /* this build has no key: modules are off */
  KM_OLD = -6                       /* serial not newer than the last one run (a replay) */
};

typedef struct {
  uint8_t xfer, have, count, active;   /* transfer id, chunks held / expected, 1 while partway */
  int8_t last;                         /* the last km_packet result */
  uint8_t chunk;                       /* the last chunk number seen */
  uint16_t code_len;                   /* from the header, once the module checks */
  uint32_t serial;                     /* of the last module accepted */
} km_status_t;

/* payload: the packet as received (whitened), len 64 or 128. arena: the message store. */
int km_packet(const uint8_t *payload, int len, void *arena, unsigned arena_bytes);
/* After KM_READY: run the module once and return what it returned (-100 if none is ready,
   or on a machine that can't run Thumb code). */
int km_run(void *arena, const k6_api_t *api);
void km_reset(void);                  /* drop a transfer partway */
const km_status_t *km_status(void);
/* SipHash-2-4 (Aumasson and Bernstein), for the tag and the tests */
uint64_t km_siphash(const uint8_t key[16], const uint8_t *m, unsigned n);

#ifdef __cplusplus
}
#endif
#endif
