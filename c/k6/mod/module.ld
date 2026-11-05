/* K6 RAM module: one position-independent block, code then constants. MIT */
ENTRY(k6_main)
SECTIONS
{
  . = MODULE_BASE;
  .text : { *(.text.k6_main) *(.text .text.*) *(.rodata .rodata.*) }
  .data : { *(.data .data.* .bss .bss.* COMMON) }
  /DISCARD/ : { *(.ARM.exidx* .ARM.extab* .comment .ARM.attributes) }
}
ASSERT(SIZEOF(.data) == 0, "a K6 module can't have writable statics (no RWPI): use the stack")
