@ __aeabi_uldivmod / __aeabi_ldivmod: quotient in r0:r1, remainder in r2:r3 (shim.c does the work)
@ SPDX-License-Identifier: MIT
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
  ldrd r2, r3, [sp, #8]
  add sp, sp, #16
  pop {r4, pc}
.endm
divmod __aeabi_uldivmod, udivmod64
divmod __aeabi_ldivmod, sdivmod64
