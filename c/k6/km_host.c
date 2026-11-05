/* km_host: the K6 module loader (k6_mod.c) on this machine, for tests/test_k6_mod.py.
   stdin: one whitened packet per line in hex; argv[1]: arena bytes (default 4096).
   stdout: "r=<result> have=<n>/<count> active=<0|1>" per packet, then "code_len=<n> serial=<n>"
   and the arena's first 64 bytes in hex. Built with the test key 00 01 .. 0f.
   SPDX-License-Identifier: MIT */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "k6_mod.h"

static uint32_t arena[65536 / 4];

int main(int argc, char **argv) {
  unsigned bytes = argc > 1 ? (unsigned)atoi(argv[1]) : 4096u, i, n;
  char line[1024];
  uint8_t p[256];
  const km_status_t *st = km_status();
  memset(arena, 0xEE, sizeof arena);
  while (fgets(line, sizeof line, stdin)) {
    for (n = 0; n < sizeof p && sscanf(line + 2 * n, "%2hhx", &p[n]) == 1; n++) {}
    int r = km_packet(p, (int)n, arena, bytes);
    printf("r=%d have=%u/%u active=%u\n", r, st->have, st->count, st->active);
  }
  printf("code_len=%u serial=%u\n", st->code_len, (unsigned)st->serial);
  for (i = 0; i < 64; i++) printf("%02x", ((uint8_t *)arena)[i]);
  printf("\n");
  return 0;
}
