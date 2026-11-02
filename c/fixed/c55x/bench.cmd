/* Link map for bench.c on the C5517: 64 KB DARAM, 256 KB SARAM (addresses in bytes).
   The SARAM is given in parts that each stay inside one 64K-word page, so no data object
   can cross a page boundary (the large memory model's pointers wrap at 16 bits there; the
   simulator stops on it). SPDX-License-Identifier: MIT */
MEMORY
{
    MMR:    o = 0x000000  l = 0x0000C0
    DARAM:  o = 0x0000C0  l = 0x00FF40   /* 64 KB dual-access */
    SARAM0: o = 0x010000  l = 0x010000   /* 256 KB single-access: page 0 */
    SARAM1: o = 0x020000  l = 0x020000   /* page 1 */
    SARAM2: o = 0x040000  l = 0x010000   /* page 2 */
    VECS:   o = 0xFFFF00  l = 0x000100
}
SECTIONS
{
    vectors (NOLOAD) > VECS
    .stack    > DARAM, RUN_START(_stk_start), RUN_END(_stk_end)
    .sysstack > DARAM, RUN_START(_sstk_start), RUN_END(_sstk_end)
    .sysmem   > DARAM (HIGH)   /* the heap: encoder and decoder states */
    .cio      > DARAM
    .data     > DARAM
    .bss      > SARAM1 | SARAM0 | SARAM2   /* the benchmark's own buffers */
    .cinit    > SARAM0 | SARAM1 | SARAM2
    .const:_fx_cos512 > DARAM  /* read twice a cycle by nlp_dft.asm's dual MAC */
    .const:.string > DARAM   /* the strings: when they began on a 64K-word page boundary,
                               the simulator stopped in printf with a page-crossing warning */
    .const    >> SARAM0 | SARAM1 | SARAM2  /* split between parts, each table whole */
    .switch   > SARAM0 | SARAM1 | SARAM2
    .text     > SARAM2 | SARAM1 | SARAM0
}
