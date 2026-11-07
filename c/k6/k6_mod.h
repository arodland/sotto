/* k6_mod: run a small native module from the K6's RAM, sent over the air.

   A module arrives as whitened L or XL packets with magic 0xAB
   (docs/k6-modules.md). De-whitened, each packet is

     0xAB, transfer id, chunk number, chunk count, data (len - 6 bytes), CRC-16 (big-endian)

   Magic 0xAE instead of 0xAB marks the same packet "reply wanted" (ARQ, images from 2026-11-06
   on): the K6 then answers over the air, on 440.850 MHz only, with 0xAF report packets
   (km_report_status / km_report_text below): which chunks are still missing, 2 s after the
   sender's last packet, and once the module has run, its result and its say lines.

   and the chunks, in order, make the module image:

     0  'K' '6' 'M' 1      magic and ABI major 1
     4  code length        u16, little-endian
     6  entry              u16, byte offset of the entry function in the code (even)
     8  serial             u32, must be newer than the last module this boot accepted
     12 API level, flags   u8 (the k6_api_t level it needs, at most KM_LEVEL), u8 (flags: 0),
                           u8 telemetry parts (0-12: with a reply-wanted upload, the K6 sends
                           this many 0xAF text packets of the module's say lines after it runs),
                           1 reserved byte
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
#define KM_MAGIC_ARQ 0xAE           /* a module chunk, reply wanted */
#define KM_MAGIC_REPORT 0xAF        /* the K6's answer (L, 64 bytes) */
#define KM_TLM_MAX 12               /* telemetry parts at most */
#define KM_TLM_PART 57              /* text bytes a part */
#define KM_HDR 16
#define KM_TAG 8
#define KM_ABI 1
#define KM_LEVEL 5

typedef struct k6_api {
  uint16_t abi, level;              /* KM_ABI, KM_LEVEL */
  void (*say)(const char *s);       /* a serial report line, "K6 MOD say <s>" (up to 60 chars) */
  uint32_t (*ticks)(void);          /* uptime in 100 us ticks, 16 bits: wraps every 6.5536 s */
  /* level 2: the receiver. Receive only: fd_write refuses the registers that switch the
     chip's state, power, PA and band GPIOs (0x30 0x33 0x36 0x37) and returns -1; tune is the
     way to move. After the module returns, the radio goes back to its own frequency and
     the pager's receive set-up. A module that uses these declares K6_MODULE_LEVEL 2. */
  uint16_t (*fd_read)(uint8_t reg);             /* FD6818 register */
  int (*fd_write)(uint8_t reg, uint16_t value); /* 0, or -1 if refused */
  void (*delay_us)(uint32_t us);                /* busy wait */
  void (*tune)(uint32_t hz, int wide);          /* receive on hz (band switch, PLL, filter, RX on) */
  uint32_t (*rx_hz)(void);                      /* the frequency the pager listens on */
  uint32_t (*ms)(void);                         /* ms since the module started, 32 bits (it sums
                                                   100 us ticks in ms and delay_us: a module that
                                                   calls neither for 6.5 s loses time) */
  int (*key_down)(void);                        /* 1 while any key is held: a long module's way out */
  void *(*scratch)(uint32_t *bytes);            /* RAM free while the module runs (the rest of the
                                                   module store, word aligned), its size in *bytes:
                                                   a module's tables and buffers, since it has no
                                                   statics and only about 700 B of stack */
  /* level 3: a guarded transmitter (Jeff OK'd a module TX API, 2026-11-06). tx_on keys up on
     the pager's own frequency (rx_hz) only, inside Aura's TX band lock (144-148, 420-450 MHz),
     FM, low power, no CTCSS, mic ADC off, for at most max_ms (clamped to 120 s on images after
     2026-11-07, for a 110.6 s WSPR frame; 20 s before): every other
     call (fd_write, delay_us, ms, ticks, key_down, tx_pa) checks the time and keys down when
     it is up, and the core keys down when the module returns. The module makes the modulation
     (REG_3B, the crystal value; the tone oscillator REG_70/71) and sends its own CW ID.
     REG_30/33/36/37 stay refused. A module that uses these declares K6_MODULE_LEVEL 3. */
  int (*tx_on)(uint32_t max_ms);                /* 0 on the air, -1 refused, -2 already on */
  void (*tx_off)(void);
  int (*tx_pa)(int on);                         /* PA on/off while keyed (CW keying, PLL locked);
                                                   -1 if not keyed */
  /* level 4 (2026-11-06): a cheap key check and the radio's calibration record. key_poll drives
     every keypad row at once and reads the four columns: about 5 us, against key_down's ~60 us
     (Aura's six-row scan with a 5 us wait a row), so a streaming module can check it in its hot
     loop. cal_read / cal_write: the calibration record in SPI flash (KM_CAL_ADDR, one 4 KB
     sector of its own, after sotto's codebooks), up to KM_CAL_BYTES; its format is
     c/k6/mod/k6_cal.h's. cal_write erases the sector and writes n bytes: once a module run,
     never while keyed. A module that uses these declares K6_MODULE_LEVEL 4. */
  int (*key_poll)(void);                        /* 1 while any key is held */
  int (*cal_read)(void *buf, uint32_t n);       /* n (at most KM_CAL_BYTES), or -1 */
  int (*cal_write)(const void *buf, uint32_t n);/* 0, or -1 (twice this run, keyed, n too big) */
  /* level 5 (2026-11-07, Jeff approved): the SPI flash, for the repeater table (c/k6/rt.h).
     fl_read reads anywhere in 0x0F0000-0x1FFFFF (the table, sotto's codebooks, the calibration
     record); fl_prog programs up to 256 bytes inside one page and fl_erase erases one 4 KB
     sector, both inside the table's 64 KB (KM_FL_LO .. KM_FL_HI) only. The firmware refuses,
     with -1: anything outside those ranges, a program that crosses a page, any write while
     keyed, and more than KM_FL_ERASES erases in one module run (or one EXPLORE session). The
     three calls are rt_flash_t's (c/k6/rt.h) without its ctx. A module that uses these declares
     K6_MODULE_LEVEL 5. */
  int (*fl_read)(uint32_t addr, void *buf, uint32_t n);        /* 0, or -1 */
  int (*fl_prog)(uint32_t addr, const void *buf, uint32_t n);  /* 0, or -1 */
  int (*fl_erase)(uint32_t addr);                              /* 0, or -1 */
} k6_api_t;

#define KM_FL_LO 0x0F0000u          /* the repeater table: fl_prog and fl_erase */
#define KM_FL_HI 0x100000u
#define KM_FL_READ_HI 0x200000u     /* fl_read: KM_FL_LO up to here */
#define KM_FL_ERASES 32u

#define KM_CAL_ADDR 0x10F000u
#define KM_CAL_BYTES 64u

/* REG 0x67 bits 8:1 = RSSI in dB above -174 dBm (as Aura's get_rssi) */
#define KM_RSSI_DBM(reg67) ((int)(((reg67) & 0x1FF) >> 1) - 174)

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
  uint8_t reply;                       /* 1: this transfer's chunks came as 0xAE (reply wanted) */
  uint8_t tlm;                         /* telemetry parts the module's header asks for */
  uint8_t missing;                     /* chunks of this transfer not yet held */
  uint8_t pad;
} km_status_t;

/* The K6's report packet (KM_MAGIC_REPORT), L: 64 bytes, whitened with PN9 like every packet,
   CRC-16/CCITT-FALSE of bytes 0..61 in 62..63 (big-endian). De-whitened:
     0 0xAF   1 kind (KM_REP_*)   2 report seq (counts every report this boot)
     3 part   4 parts             (status = part 0; text parts 1..parts-1)
   kind STATUS:
     5 xfer  6 have  7 count  8 r (the last km_packet result, int8)  9 state (KM_ST_*)
     10..13 serial (u32 LE, of the module once it checked)
     14..45 missing chunks: bit i of byte i/8 set = chunk i not held (resend it)
     46..51 the station's call, ASCII, NUL padded
     52..55 the module's return value (int32 LE)  56..59 ms it ran  60..61 stack_free (u16 LE)
   kind TEXT: 5..61 text (KM_TLM_PART bytes, NUL padded): the module's say lines, '\n' after each */
enum { KM_REP_STATUS = 1, KM_REP_TEXT = 2 };
enum { KM_ST_PARTWAY = 0, KM_ST_RAN = 1, KM_ST_REFUSED = 2 };

/* payload: the packet as received (whitened), len 64 or 128. arena: the message store. */
int km_packet(const uint8_t *payload, int len, void *arena, unsigned arena_bytes);
/* After KM_READY: run the module once and return what it returned (-100 if none is ready,
   or on a machine that can't run Thumb code). */
int km_run(void *arena, const k6_api_t *api);
void km_reset(void);                  /* drop a transfer partway */
const km_status_t *km_status(void);
/* The report packets, whitened and ready for the chip's FIFO (64 bytes). status: from km_status
   and the chunks held; call: up to 6 characters. text: n bytes of tlm at part (1..parts-1). */
void km_report_status(uint8_t out[64], uint8_t seq, uint8_t parts, uint8_t state, const char *call,
                      int32_t ret, uint32_t ms, uint16_t stack_free);
void km_report_text(uint8_t out[64], uint8_t seq, uint8_t part, uint8_t parts, const char *text,
                    unsigned n);
/* SipHash-2-4 (Aumasson and Bernstein), for the tag and the tests */
uint64_t km_siphash(const uint8_t key[16], const uint8_t *m, unsigned n);

#ifdef __cplusplus
}
#endif
#endif
