/* The K6's 2 MB SPI NOR flash on this machine, for the host builds of the pager with the
 * codebook in flash (sp_cb.h): starts erased (0xFF); a write can only clear bits; an erase
 * sets a 4 KB sector back to 0xFF. flash_host_load puts a file's bytes at an address, and
 * flash_host_save writes a range out, for the tests. Counts what the pager did.
 * SPDX-License-Identifier: MIT */
#include <stdio.h>
#include <string.h>
#include "sp_cb.h"
#include "flash_host.h"

#define FLASH_BYTES (2u << 20)
static uint8_t nor[FLASH_BYTES];
static int init;
unsigned flash_host_erases, flash_host_writes, flash_host_reads;

static void start(void) {
  if (!init) memset(nor, 0xFF, sizeof nor), init = 1;
}

void sp_flash_read(uint32_t addr, void *buf, unsigned n) {
  start();
  if (addr + n > FLASH_BYTES) { memset(buf, 0xFF, n); return; }
  memcpy(buf, nor + addr, n);
  flash_host_reads++;
}

void sp_flash_erase4k(uint32_t addr) {
  start();
  addr &= ~4095u;
  if (addr < FLASH_BYTES) memset(nor + addr, 0xFF, 4096);
  flash_host_erases++;
}

void sp_flash_write(uint32_t addr, const void *buf, unsigned n) {
  const uint8_t *p = buf;
  unsigned i;
  start();
  if ((addr & 255) + n > 256) fprintf(stderr, "flash_host: write crosses a page at %06x\n", (unsigned)addr);
  for (i = 0; i < n && addr + i < FLASH_BYTES; i++) nor[addr + i] &= p[i];
  flash_host_writes++;
}

int flash_host_load(const char *path, uint32_t addr) {
  FILE *f = fopen(path, "rb");
  size_t n;
  start();
  if (!f || addr >= FLASH_BYTES) return -1;
  n = fread(nor + addr, 1, FLASH_BYTES - addr, f);
  fclose(f);
  return (int)n;
}

int flash_host_save(const char *path, uint32_t addr, uint32_t n) {
  FILE *f = fopen(path, "wb");
  start();
  if (!f || addr + n > FLASH_BYTES) return -1;
  fwrite(nor + addr, 1, n, f);
  fclose(f);
  return 0;
}
