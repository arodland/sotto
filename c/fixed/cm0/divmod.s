@ __aeabi_uldivmod / __aeabi_ldivmod for ARMv6-M: quotient in r0:r1, remainder in r2:r3
@ (cm4/shim.c's udivmod64 / sdivmod64 do the work). SPDX-License-Identifier: MIT
.syntax unified
.thumb
.macro divmod name, impl
.global \name
.thumb_func
\name:
  push {r4, lr}
  sub sp, sp, #16
  add r4, sp, #8
  str r4, [sp]
  bl \impl
  ldr r2, [sp, #8]
  ldr r3, [sp, #12]
  add sp, sp, #16
  pop {r4, pc}
.endm
divmod __aeabi_uldivmod, udivmod64
divmod __aeabi_ldivmod, sdivmod64
