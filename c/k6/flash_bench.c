/* The K6's SPI flash for c/k6/bench_k6.py's emulator runs of a codebook-in-flash build
 * (tools/k6_build.sh K6_CB=...): reads come from a copy of the codebook image in the bench's
 * flash (sp_cb_bench_image, made from sotto-cb.bin), as if it sat at SP_CB_ADDR, and in a
 * sotto 600 / 400 build (VLR_CB_SPI) from sp_vlr_bench_image (sotto-vlr.bin) at SP_VLR_ADDR;
 * writes and erases do nothing. The SPI transfer itself isn't modelled. SPDX-License-Identifier: MIT */
#include "sp_cb.h"
#include "sp_vlr.h"

extern const unsigned char sp_cb_bench_image[SP_CB_HDR + 30720];
#ifdef VLR_CB_SPI
extern const unsigned char sp_vlr_bench_image[SP_VLR_HDR + 21280];
#endif

void sp_flash_read(uint32_t addr, void *buf, unsigned n) {
  unsigned char *b = buf;
  unsigned i;
  for (i = 0; i < n; i++, addr++) {
    b[i] = addr >= SP_CB_ADDR && addr - SP_CB_ADDR < sizeof sp_cb_bench_image ? sp_cb_bench_image[addr - SP_CB_ADDR] : 0xFF;
#ifdef VLR_CB_SPI
    if (addr >= SP_VLR_ADDR && addr - SP_VLR_ADDR < sizeof sp_vlr_bench_image) b[i] = sp_vlr_bench_image[addr - SP_VLR_ADDR];
#endif
  }
}

void sp_flash_erase4k(uint32_t addr) { (void)addr; }
void sp_flash_write(uint32_t addr, const void *buf, unsigned n) { (void)addr, (void)buf, (void)n; }
