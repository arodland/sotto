/* Link map for bench.c -DBENCH16 (the v3.8 16 kHz layer) on the C5517. The v3.8 states
   (about 100 KB with two core encoders/decoders) don't fit the 64 KB of DARAM, so the heap
   gets a whole 64K-word page of SARAM (bytes 0x20000-0x3FFFF), where no object can cross a
   page boundary. Addresses in bytes. SPDX-License-Identifier: MIT */
MEMORY
{
    MMR:    o = 0x000000  l = 0x0000C0
    DARAM:  o = 0x0000C0  l = 0x00FF40   /* 64 KB dual-access */
    SARAM0: o = 0x010000  l = 0x010000   /* 256 KB single-access, in three parts */
    SARAMP: o = 0x020000  l = 0x020000   /* one 64K-word page: the heap */
    SARAM2: o = 0x040000  l = 0x010000
    VECS:   o = 0xFFFF00  l = 0x000100
}
SECTIONS
{
    vectors (NOLOAD) > VECS
    .stack    > DARAM, RUN_START(_stk_start), RUN_END(_stk_end)
    .sysstack > DARAM, RUN_START(_sstk_start), RUN_END(_sstk_end)
    .cio      > DARAM
    .data     > DARAM
    .bss      > DARAM | SARAM0 | SARAM2
    .sysmem   > SARAMP
    .cinit    > SARAM0 | SARAM2
    .const:_fx_cos512 > DARAM  /* see bench.cmd */
    .const:.string > DARAM   /* see bench.cmd */
    .const    >> SARAM0 | SARAM2 | DARAM   /* split: the unpacked codebook makes it larger than one range */
    .switch   > SARAM0 | SARAM2
    .text     > SARAM2 | SARAM0
}
