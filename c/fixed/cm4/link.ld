/* Bench memory map: everything in one flash and one RAM region (sizes are not the chip's;
   run.py reports the real use). SPDX-License-Identifier: MIT */
MEMORY { FLASH (rx) : ORIGIN = 0x08000000, LENGTH = 1M
         RAM (rwx)  : ORIGIN = 0x20000000, LENGTH = 1M }
SECTIONS {
  .text : { *(.text*) } > FLASH
  .rodata : { *(.rodata*) } > FLASH
  .data : { *(.data*) } > RAM
  .bss (NOLOAD) : { *(.bss*) *(COMMON) } > RAM
  /DISCARD/ : { *(.ARM.exidx*) *(.comment) }
}
