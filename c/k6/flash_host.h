/* flash_host.c: the K6's SPI flash on this machine. SPDX-License-Identifier: MIT */
#ifndef FLASH_HOST_H
#define FLASH_HOST_H
#include <stdint.h>
extern unsigned flash_host_erases, flash_host_writes, flash_host_reads;
int flash_host_load(const char *path, uint32_t addr);
int flash_host_save(const char *path, uint32_t addr, uint32_t n);
#endif
