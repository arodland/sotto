/* sp_cb: the decoder's shape codebook in the K6's SPI flash, read a vector at a time.

   The fixed-point decoder looks up one 20-byte vector per codebook stage and frame (v3.7: 4
   per 20 ms; sotto 1200: 3 per 40 ms), by index. Built with -DFX_CB_SPI, it calls
   fx_cb_fetch (c/fixed/fx_tables.h) instead of reading fx_cb, the linker drops the
   30,720-byte table from the MCU's flash, and this file reads each vector from the codebook
   image in SPI flash at SP_CB_ADDR. The bytes are the table's, so the decoder's output is
   bit-exact with the table build.

   The image (little-endian), SP_CB_HDR bytes of header and then the table:
     0   'K' '6' 'C' 'B'
     4   version (1), u16
     6   bytes per vector (FX_CBW 16-bit words: 20), u16
     8   table bytes (30,720), u32
     12  stages (4), then the bits of each stage (9 9 8 8), up to 8 stages; the rest zero
     24  CRC-32 of the table bytes
     28  CRC-32 of header bytes 0 .. 27
   then stage 0's vectors, stage 1's, ... each vector FX_CBW words, as fx_cb holds them.

   The firmware provides the flash access (sp_flash_*). It calls sp_cb_setup once at power-on:
   the image is checked against the codebook this build expects (SP_CB_CRC, or the table's
   own CRC when the table is linked), and with -DSP_CB_INSTALL, which links the table, a
   missing or wrong image is written first, once. Until sp_cb_ready, the pager plays nothing:
   sp_play_start leaves the message unplayed rather than decode from a bad codebook.
   Without FX_CB_SPI (the table build) everything here reports ready and touches no flash.
   SPDX-License-Identifier: MIT */
#ifndef SP_CB_H
#define SP_CB_H
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif

#define SP_CB_ADDR 0x100000u        /* the module library's first data slots (2 x 16 KB) */
#define SP_CB_HDR 32
#define SP_CB_VERSION 1

/* sp_cb_mode */
enum { SP_CB_MODE_TABLE = 0, SP_CB_MODE_SPI = 1, SP_CB_MODE_INSTALL = 2 };

/* sp_cb_setup / sp_cb_check results */
enum {
  SP_CB_OK = 1,                     /* the image checks: the decoder reads it */
  SP_CB_INSTALLED = 2,              /* it didn't, this build wrote it, and now it does */
  SP_CB_NONE = -1,                  /* no image (no 'K6CB'): erased flash, or never installed */
  SP_CB_BAD_HDR = -2,               /* header CRC, version or sizes wrong */
  SP_CB_WRONG = -3,                 /* a good image of another codebook */
  SP_CB_BAD_CRC = -4,               /* the table bytes don't match their CRC */
  SP_CB_VERIFY = -5                 /* written, but it doesn't read back right */
};

int sp_cb_mode(void);
int sp_cb_check(void);              /* check the image in flash; sets sp_cb_ready */
int sp_cb_setup(void);              /* sp_cb_check, and with SP_CB_INSTALL write it if needed */
int sp_cb_ready(void);
uint32_t sp_cb_expected(void);      /* the table CRC this build wants */
uint32_t sp_cb_reads(void);         /* vectors fetched since power-on */
/* n fetches at pseudo-random indices of every stage, for timing the flash reads; returns a
   checksum of what was read (0 when not ready) */
uint32_t sp_cb_bench(unsigned n);
/* the header for the table linked into this build (table and install builds; 0 otherwise),
   and the table itself as bytes (len = table bytes): what an install writes */
int sp_cb_header(uint8_t hdr[SP_CB_HDR]);
uint32_t sp_cb_table_bytes(void);
unsigned sp_cb_table(uint32_t off, uint8_t *out, unsigned n);    /* n, or 0 without the table */
uint32_t sp_cb_crc32(uint32_t crc, const uint8_t *p, unsigned n);  /* start with 0 */

/* provided by the firmware (and by c/k6/flash_host.c on this machine) */
void sp_flash_read(uint32_t addr, void *buf, unsigned n);
void sp_flash_erase4k(uint32_t addr);                     /* the 4 KB sector at addr */
void sp_flash_write(uint32_t addr, const void *buf, unsigned n);   /* erased bytes only */

#ifdef __cplusplus
}
#endif
#endif
