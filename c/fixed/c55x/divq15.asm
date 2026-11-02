; fx_div_q15_2 for the C55x (sotto_fx_enc.c div_q15_2): q = n 2^15 / d truncated toward
; zero, for two numerators n1, n2 over one d > 0, with d rounded down to 16 bits, as the C:
;   sd = max(bit length of d - 16, 0), dd = d >> sd, sh = 15 - sd
;   num = |n| 2^sh (a right shift for sh < 0), or 2^32 - 1 when that would reach 2^32
;   q = min(num / dd, 2^31 - 1), negated for n < 0
; The 32-bit by 16-bit division is two rounds of 16 conditional subtractions (SUBC), the
; high half of num first; SXMD is cleared for them so that dd is read unsigned.
; SPDX-License-Identifier: MIT

        .mmregs
        .cpl_on
        .arms_on
        .c54cm_off
        .model call=c55_std
        .model mem=large

        .text
        .global _fx_div_q15_2

; one numerator: n in acn (40 bits), T1 = sh, dd at *SP(#0), q to *arp (int32).
; Uses AC2, AC3, T0, T2, T3.
DIVQ    .macro acn, arp
        MOV #0, AC3
        BCC store?, acn == #0       ; q = 0 for n = 0 (EXP gives 0, not 31, for a zero accumulator)
        MOV #0, T2
        BCC pos?, acn >= #0
        NEG acn
        MOV #1, T2                  ; n < 0
pos?:
        EXP acn, T0                 ; 30 - position of the top bit of |n|
        MOV T1, T3
        ADD #-1, T3
        SUB T0, T3                  ; sh - 1 - T0 > 0 <=> bit length of |n| + sh > 32
        MOV #0x7FFF << #16, AC3
        OR #0xFFFF, AC3
        ADD AC3, AC3
        ADD #1, AC3                 ; 2^32 - 1
        BCC sat?, T3 > #0
        SFTL acn, T1, AC3           ; num = |n| 2^sh
sat?:
        SFTL AC3, #-16, AC2         ; the high half of num
        BCLR ST1_SXMD
        RPT #15
        SUBC *SP(#0), AC2, AC2      ; AC2 = (hi mod dd) << 16 | hi / dd
        BSET ST1_SXMD
        MOV AC2, T3                 ; the high half of q
        SFTL AC2, #-16
        SFTL AC2, #16               ; (hi mod dd) << 16
        SFTL AC3, #24
        SFTL AC3, #-24              ; the low half of num
        ADD AC3, AC2
        BCLR ST1_SXMD
        RPT #15
        SUBC *SP(#0), AC2, AC2
        BSET ST1_SXMD
        SFTL AC2, #24
        SFTL AC2, #-24              ; the low half of q
        MOV T3, AC3
        SFTL AC3, #24
        SFTL AC3, #-8               ; the high half of q, unsigned, in bits 31..16
        ADD AC2, AC3                ; q
        MOV #0x7FFF << #16, AC2
        OR #0xFFFF, AC2
        MIN AC2, AC3                ; q = min(q, 2^31 - 1)
        BCC store?, T2 == #0
        NEG AC3
store?:
        MOV AC3, dbl(*arp)
        .endm

; void fx_div_q15_2(fx_acc n1 /* AC0 */, fx_acc n2 /* AC1 */, fx_acc d /* AC2 */,
;                   int32_t *q1 /* XAR0 */, int32_t *q2 /* XAR1 */)
_fx_div_q15_2:
        PSH T3, T2
        AADD #-1, SP
        BSET ST1_M40
        EXP AC2, T0                 ; bit length of d = 31 - T0
        MOV T0, T1
        MOV T0, T3
        ADD #-15, T3
        BCC $dq_sh, T3 <= #0
        MOV #15, T1                 ; sh = min(T0, 15) = 15 - sd
$dq_sh:
        MOV T1, T3
        ADD #-15, T3                ; -sd
        SFTL AC2, T3                ; dd = d >> sd, in [2^15, 2^16) or d itself
        MOV AC2, *SP(#0)
        DIVQ AC0, AR0
        DIVQ AC1, AR1
        BCLR ST1_M40
        AADD #1, SP
        POP T3, T2
        RET
