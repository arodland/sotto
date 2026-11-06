/* sp_vlr: sotto 600 / 400's shape codebooks in the K6's SPI flash, read a vector at a time.

   The very-low-rate decoder (c/fixed/sotto_fx_vlr.c) needs 21,280 bytes of int8 shape
   codebooks (6,688 for sotto 600, 14,592 for sotto 400): vlr_cb_all in c/sotto_vlr_tables.c.
   It reads one 19-byte vector per anchor stage: 6 per 80 ms (600), 4 per 120 ms (400). Built
   with -DVLR_CB_SPI the tables leave the codebooks out and vlr_cb_fetch (here) reads each
   vector from the image at SP_VLR_ADDR, so the output is bit-exact with the table build.
   (The small tables, voicing patterns, slopes and energy residuals, 0.7 KB, stay in the MCU.)

   The image (little-endian), SP_VLR_HDR bytes of header and then vlr_cb_all:
     0   'K' '6' 'V' 'L'
     4   version (1), u16
     6   bytes per vector (19), u16
     8   table bytes (21,280), u32
     12  the rates, u16 each (600, 400); the rest zero
     24  CRC-32 of the table bytes
     28  CRC-32 of header bytes 0 .. 27
   It sits at 0x108000, after v3.7's codebook image (sp_cb.h, 0x100000 to 0x107820): the module
   library's data slots 2 and 3. The two images are independent: an image that plays sotto 600 /
   400 (-DSP_VLR) checks both, and plays each codec whose tables check.

   sp_vlr_setup, once at power-on after sp_cb_setup: checks the image against the tables this
   build expects (SP_VLR_CRC, or the linked tables' own CRC), and with -DSP_VLR_INSTALL, which
   links the tables, writes a missing or wrong image first, once. Its results are sp_cb.h's
   (1 good, 2 installed, -1 none, -2 header, -3 other tables, -4 CRC, -5 verify), or 0 in a
   build that doesn't play sotto 600 / 400 (every build has these functions, so the radio's
   code is the same for all). Without VLR_CB_SPI (tables linked, read in place) it returns 1
   and touches no flash.
   SPDX-License-Identifier: MIT */
#ifndef SP_VLR_H
#define SP_VLR_H
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif

#define SP_VLR_ADDR 0x108000u
#define SP_VLR_HDR 32
#define SP_VLR_VERSION 1

int sp_vlr_setup(void);
int sp_vlr_check(void);
int sp_vlr_ready(void);             /* 1 when sotto 600 / 400 can play */
uint32_t sp_vlr_expected(void);     /* the table CRC this build wants (0: no sotto 600 / 400) */
uint32_t sp_vlr_reads(void);        /* vectors fetched since power-on */
/* the header and the table bytes (builds with the tables linked; 0 otherwise) */
int sp_vlr_header(uint8_t hdr[SP_VLR_HDR]);
uint32_t sp_vlr_table_bytes(void);
unsigned sp_vlr_table(uint32_t off, uint8_t *out, unsigned n);

#ifdef __cplusplus
}
#endif
#endif
