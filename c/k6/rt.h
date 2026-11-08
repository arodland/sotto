/* rt: the K6 explorer's repeater table in SPI flash (the explorer design, "Repeater table in
   SPI flash"; docs/k6-repeater-table.md).

   One 64-byte record per saved repeater, in 0x0F0000-0x0FFFFF: 16 sectors of 4 KB, 64 slots
   each. The table is a log: an edit appends a new version of the entry (same id, next seq)
   and marks the old one replaced, so editing never erases a sector. One sector is always kept
   erased: when every other slot is used, the live records of the sector with the most dead
   ones are copied into it and that sector is erased, becoming the new spare. NOR flash only
   clears bits without an erase, so a record's state word only ever loses bits, one 2-byte write
   a step. Its low byte: FF free, FE being written, FC valid, F8 replaced, F0 deleted. Its high
   byte: FF as written; a compaction's copy of a record from sector V is E0|V while the copy
   runs, C0|V once every record is copied (then V is erased), 80|V after that. A cut power leaves at worst a slot that never reached valid (dead), the
   last edit's old version still valid (rt_mount keeps the newer by wseq and marks the other),
   or a half-done compaction (rt_mount finishes or undoes it), never a lost entry.

   The record (little-endian, as the M0 and the host are):
     0  state         u16   see RT_ST_*
     2  seq           u16   the entry's version (the newer of two wins, wrap-safe)
     4  rx_hz         u32
     8  tx_off        i32   Hz, 0 = simplex
     12 rx_tone       u16   CTCSS in 0.1 Hz, or a DCS code | RT_TONE_DCS; 0 = none
     14 tx_tone       u16
     16 flags         u16   RT_F_*
     18 best_rssi     u8    dBm + 200
     19 score         u8    the explorer's grade ('R' 'r' 's' '?') or 0
     20 last_heard    u32   boot count << 16 | minutes since boot (the K6 has no clock)
     24 name          16 characters, ASCII, NUL padded
     40 call          10 characters, A-Z 0-9 /, as heard, NUL padded
     50 call_flags    u8    RT_C_*
     51 cw            u8    the last CW ID decode: tone index (tone 400 + 60 i Hz; 600 + 50 i
                            in records written before 2026-11-08) in bits 3:0,
                            wpm / 3 in bits 7:4 (0 = never decoded)
     52 group         u8    bits 3:0 scan group: 0 Always Scan, 1-15 groups A-O
     53 spare         u8    0
     54 id            u16   the entry (1..65534): every version of an entry carries it
     56 wseq          u32   write sequence: every record written gets the next (newest wins)
     60 crc           u32   CRC-32 (IEEE) of bytes 2-59: the state word changes after the write

   No division, no library calls and no writable statics, so it builds into the core or a RAM
   module; the flash is reached through rt_flash_t (the core's SPI driver, or flash_host.c).
   Writes happen only when the caller asks: Save, Delete, or a confirmed CW ID (rt_cw_apply).
   SPDX-License-Identifier: MIT */
#ifndef RT_H
#define RT_H
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif

#define RT_BASE 0x0F0000u
#define RT_SECTORS 16
#define RT_SLOTS_PER_SECTOR 64
#define RT_SLOTS (RT_SECTORS * RT_SLOTS_PER_SECTOR)
#define RT_REC 64

/* the state word's low byte */
enum { RT_ST_FREE = 0xFF, RT_ST_WRITING = 0xFE, RT_ST_VALID = 0xFC, RT_ST_REPLACED = 0xF8,
       RT_ST_DELETED = 0xF0 };
#define RT_ST_COPY 0xE000u        /* high byte E0|V: being copied from sector V */
#define RT_ST_SETTLED 0xC000u     /* high byte C0|V: the copy is complete, V about to be erased */
#define RT_ST_DONE 0x8000u        /* high byte 80|V: V erased */

/* flags */
#define RT_F_WIDE 0x0001         /* 25 kHz channel (else 12.5) */
#define RT_F_OFF_SURE 0x0002     /* offset confirmed (opened it, or heard the input) */
#define RT_F_TONE_SURE 0x0004    /* the TX tone opened the repeater */
#define RT_F_NAME_USER 0x0008    /* the name was typed */
#define RT_F_CALL_USER 0x0010    /* the call was typed */
#define RT_F_DMR 0x0020          /* the explorer heard DMR syncs on the output (mixed-mode: its ID may be analog) */
#define RT_F_TONE_USER 0x0040    /* the tone was typed */
#define RT_TONE_DCS 0x8000

/* call_flags */
#define RT_C_PORTABLE 0x01
#define RT_C_MOBILE 0x02
#define RT_C_REPEATER 0x04
#define RT_C_FROM_CW 0x08        /* source: a CW ID decode (else the user) */
#define RT_C_TWICE 0x10          /* decoded twice alike */

typedef struct {
  uint16_t state, seq;
  uint32_t rx_hz;
  int32_t tx_off;
  uint16_t rx_tone, tx_tone, flags;
  uint8_t best_rssi, score;
  uint32_t last_heard;
  char name[16];
  char call[10];
  uint8_t call_flags, cw, group, spare0;
  uint16_t id;
  uint32_t wseq;
  uint32_t crc;
} rt_rec_t;

typedef struct {
  /* each returns 0, or <0 on a failure; write programs n bytes inside one 256-byte page (it
     can only clear bits), erase sets one 4 KB sector to 0xFF */
  int (*read)(void *ctx, uint32_t addr, void *buf, uint32_t n);
  int (*write)(void *ctx, uint32_t addr, const void *buf, uint32_t n);
  int (*erase)(void *ctx, uint32_t addr);
  void *ctx;
} rt_flash_t;

typedef struct {
  const rt_flash_t *fl;
  uint32_t base;
  uint16_t live, dead, free;   /* slots */
  uint16_t max_id;
  uint32_t wseq;               /* the newest record's */
  uint8_t spare;               /* the sector kept erased */
  uint8_t gcs;                 /* compactions since mount */
  uint8_t repairs;             /* what rt_mount mended: old versions marked, compactions finished */
} rt_t;

/* results */
enum { RT_OK = 0, RT_ERR_FLASH = -1, RT_ERR_FULL = -2, RT_ERR_NONE = -3, RT_ERR_ARG = -4,
       RT_ERR_NOT_BLANK = -5 };

/* Scan the table (also the first time: a region that reads all 0xFF is an empty table; a region
   holding anything else that isn't a record is refused with RT_ERR_NOT_BLANK unless force, so
   a table never lands on someone else's data). Repairs what a cut power left. Returns the live
   entries, or < 0. */
int rt_mount(rt_t *t, const rt_flash_t *fl, uint32_t base, int force);
/* the newest live version of entry id, or of the entry nearest rx_hz within tol (id 0): its slot,
   or RT_ERR_NONE */
int rt_find(rt_t *t, uint16_t id, uint32_t rx_hz, uint32_t tol, rt_rec_t *out);
/* live entries in slot order: rt_next(t, -1, &r) first, then rt_next(t, slot, &r) until < 0 */
int rt_next(rt_t *t, int slot, rt_rec_t *out);
/* Save r: id 0 = a new entry (its id is set), else a new version of entry id. Fills seq, state
   and crc. Returns the slot, or < 0. */
int rt_put(rt_t *t, rt_rec_t *r);
int rt_delete(rt_t *t, uint16_t id);

/* The CW ID listener's result for the entry on rx_hz (Jeff, 2026-11-07: "auto if blank, ask if
   changed"). rt_cw_check decides without writing; rt_cw_apply writes when the check allows it,
   or when confirmed (the user's key press after an RT_CW_ASK). */
enum {
  RT_CW_NO_ENTRY = 0,       /* no saved entry there: the explorer keeps the call in RAM */
  RT_CW_SAME = 1,           /* the stored call already: nothing to write */
  RT_CW_FILL = 2,           /* the entry had no call: written (rt_cw_apply) */
  RT_CW_ASK = 3,            /* a different call is stored (typed or decoded): show both, wait */
  RT_CW_CHANGED = 4         /* rt_cw_apply with confirmed: written */
};
int rt_cw_check(rt_t *t, uint32_t rx_hz, uint32_t tol, const char *call, rt_rec_t *out);
int rt_cw_apply(rt_t *t, uint32_t rx_hz, uint32_t tol, const char *call, uint8_t call_flags, int tone_hz,
                int wpm, int confirmed, rt_rec_t *out);
uint8_t rt_cw_byte(int tone_hz, int wpm);   /* byte 51 */

/* What a listen heard on the entry at rx_hz besides the call: its output's CTCSS tone (0.1 Hz;
   0 = none heard) and whether it carried DMR (dmr > 0 sets RT_F_DMR; dmr < 0 clears it: long
   listens heard none, ex_dmr_heard). A blank tone is filled (rx_tone and, if it is
   blank too, tx_tone: most repeaters send the access tone back); a
   different stored tone is left as it is (typed or heard before: the caller shows both). One
   new version at most, none if nothing changes. Returns RT_TONE_*, or < 0 (flash). */
enum { RT_TONE_NO_ENTRY = 0, RT_TONE_SAME = 1, RT_TONE_FILL = 2, RT_TONE_DIFFERS = 3 };
int rt_heard_apply(rt_t *t, uint32_t rx_hz, uint32_t tol, uint16_t tone, int dmr, rt_rec_t *out);

uint32_t rt_crc(const rt_rec_t *r);         /* of bytes 2-59 */

#ifdef __cplusplus
}
#endif
#endif
