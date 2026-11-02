; fx_cb_resid for the C55x (sotto_fx_enc.c msvq_search): the residual of one search path in
; codebook steps, r[k] = clamp(((c[k] - recon[k]) istep) >> 24, +-32767), and the sum of its
; squares. The 32 x 32-bit product is four 16 x 16 ones: x = xh 2^15 + xl, istep = ih 2^15 + il
; (xl, il in 0..32767), x istep = ((xh ih) 2^15 + xh il + xl ih) 2^15 + xl il, exact in the
; 40-bit accumulator whenever the product fits it (the C checks that), the same integers as
; the C; the compiler's 32 x 32 multiply is a library call of about 90 cycles.
; SPDX-License-Identifier: MIT

        .mmregs
        .cpl_on
        .arms_on
        .c54cm_off
        .model call=c55_std
        .model mem=large

        .text
        .global _fx_cb_resid

; fx_acc fx_cb_resid(const int32_t *c /* XAR0 */, const int32_t *recon /* XAR1 */,
;                    int16_t *r /* XAR2 */, int n /* T0 */, int32_t istep /* AC0 */): returns sum r^2
; stack: 0 ih  1 il
_fx_cb_resid:
        PSH T2
        AADD #-2, SP
        BSET ST1_M40
        MOV AC0, T1
        AND #0x7FFF, T1, T1
        MOV T1, *SP(#1)             ; il
        SFTS AC0, #-15
        MOV AC0, *SP(#0)            ; ih
        MOV #0, AC0                 ; the sum of squares
        MOV #32767, AC3
        SUB #1, T0
        MOV T0, BRC0
        RPTB $cr_end-1
        MOV dbl(*AR0+), AC1
        SUB dbl(*AR1+), AC1, AC1    ; x = c[k] - recon[k]
        MOV AC1, T1
        AND #0x7FFF, T1, T1         ; xl
        SFTS AC1, #-15
        MOV AC1, T2                 ; xh
        MPYM *SP(#0), T2, AC2       ; xh ih
        SFTL AC2, #15
        MACM *SP(#1), T2, AC2, AC2  ; + xh il
        MACM *SP(#0), T1, AC2, AC2  ; + xl ih
        SFTL AC2, #15
        MACM *SP(#1), T1, AC2, AC2  ; + xl il = x istep
        SFTS AC2, #-24              ; floor(x istep / 2^24)
        MIN AC3, AC2                ; <= 32767
        NEG AC3, AC1
        MAX AC1, AC2                ; >= -32767
        MOV AC2, *AR2+              ; r[k]
        MOV AC2, T1
        MOV T1, HI(AC1)
        MAC AC1, T1, AC0            ; + r[k]^2
$cr_end:
        BCLR ST1_M40
        AADD #2, SP
        POP T2
        RET
