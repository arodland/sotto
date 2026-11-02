; The loops of the encoder's harmonic fit (sotto_fx_enc.c harm_fit, C versions in fx_math.c)
; for the C55x. sw points at the first fitted bin of the interleaved 16-bit spectrum
; (sw[0], sw[1] = real, imaginary of that bin), w at the window response for it, nb is the
; number of fitted bins. The sums use the 40-bit accumulator (M40 set), and no pointer is
; post-incremented past the end of an array: the last element is read without an increment.
; SPDX-License-Identifier: MIT

        .mmregs
        .cpl_on
        .arms_on
        .c54cm_off
        .model call=c55_std
        .model mem=large

        .text
        .global _fx_hfit_sums
        .global _fx_hfit_err
        .global _fx_hfit_pow

; store the 40-bit accumulator as a long long at *AR2 (high double word first), AR2 += 4
STORE40 .macro acx
        SFTS acx, #-32, AC3
        MOV AC3, dbl(*AR2)
        MOV acx, dbl(*AR2(short(#2)))
        AADD #4, AR2
        .endm

; void fx_hfit_sums(const int16_t *sw /* XAR0 */, const int16_t *w /* XAR1 */, int nb /* T0 */,
;                   fx_acc out[3] /* XAR2 */): out = { sum sw[2i] w[i], sum sw[2i+1] w[i], sum w[i]^2 }
_fx_hfit_sums:
        BSET ST1_M40
        MOV XAR1, XCDP              ; w, the coefficient shared by the two multiplies
        AMAR *AR0(#1), XAR3         ; the imaginary parts
        MOV T0, T1                  ; nb
        MOV #2, T0                  ; the bin stride
        MOV #0, AC0
        MOV #0, AC1
        SUB #2, T1
        BCC $hs_last, T1 < #0
        MOV T1, CSR
        RPT CSR
        MAC *(AR0+T0), *CDP+, AC0 :: MAC *(AR3+T0), *CDP+, AC1
$hs_last:
        MAC *AR0, *CDP, AC0 :: MAC *AR3, *CDP, AC1
        MOV #0, AC2
        BCC $hs_dlast, T1 < #0
        MOV T1, CSR
        RPT CSR
        SQAM *AR1+, AC2, AC2
$hs_dlast:
        SQAM *AR1, AC2, AC2
        STORE40 AC0
        STORE40 AC1
        STORE40 AC2
        BCLR ST1_M40
        RET

; one bin's squared fit error added to AC3: the bin at *AR0 (post-incremented when the
; macro's argument is +), its window value at *AR1, the Q15 amplitude halves at *SP(#0..3).
; Uses AC0, AC1, AC2, T0, T2, T3.
;   e = sw - rshr(a w, 15), a w = (ah w) 2^15 + al w with a = ah 2^15 + al (al in 0..32767)
;   e^2 = eh^2 2^30 + eh el 2^16 + el^2 for e = eh 2^15 + el, as fx_sq32 in fx.h
ERR     .macro inc
        MOV *AR1:inc:, T0           ; w
        MPYM *SP(#0), T0, AC0       ; ah w
        SFTL AC0, #15
        MACM *SP(#1), T0, AC0, AC0  ; + al w
        ADD #0x4000, AC0
        SFTS AC0, #-15              ; rshr(a w, 15)
        MOV *AR0+, AC1              ; sw[2i]
        SUB AC0, AC1                ; e (real part)
        MOV AC1, T2
        AND #0x7FFF, T2, T2         ; el
        SFTS AC1, #-15
        MOV AC1, T3                 ; eh
        MOV T3, HI(AC1)
        MPY T3, AC1, AC2            ; eh^2
        SFTL AC2, #14
        MAC AC1, T2, AC2            ; + eh el
        SFTL AC2, #16
        MOV T2, HI(AC1)
        MAC AC1, T2, AC2            ; + el^2 = e^2
        ADD AC2, AC3
        MPYM *SP(#2), T0, AC0       ; the same for the imaginary part
        SFTL AC0, #15
        MACM *SP(#3), T0, AC0, AC0
        ADD #0x4000, AC0
        SFTS AC0, #-15
        MOV *AR0:inc:, AC1          ; sw[2i + 1]
        SUB AC0, AC1
        MOV AC1, T2
        AND #0x7FFF, T2, T2
        SFTS AC1, #-15
        MOV AC1, T3
        MOV T3, HI(AC1)
        MPY T3, AC1, AC2
        SFTL AC2, #14
        MAC AC1, T2, AC2
        SFTL AC2, #16
        MOV T2, HI(AC1)
        MAC AC1, T2, AC2
        ADD AC2, AC3
        .endm

; fx_acc fx_hfit_err(const int16_t *sw /* XAR0 */, const int16_t *w /* XAR1 */, int nb /* T0 */,
;                    int32_t ar /* AC0 */, int32_t ai /* AC1 */): the residual power, in AC0
; stack: 0 ah  1 al  2 ih  3 il  (the halves of ar and ai; ah, ih are 16-bit truncations as FX_HI)
_fx_hfit_err:
        PSH T3, T2
        AADD #-4, SP
        BSET ST1_M40
        MOV AC0, T2
        AND #0x7FFF, T2, T2
        MOV T2, *SP(#1)             ; al
        SFTS AC0, #-15
        MOV AC0, *SP(#0)            ; ah
        MOV AC1, T2
        AND #0x7FFF, T2, T2
        MOV T2, *SP(#3)             ; il
        SFTS AC1, #-15
        MOV AC1, *SP(#2)            ; ih
        MOV #0, AC3
        SUB #2, T0
        BCC $he_last, T0 < #0
        MOV T0, BRC0
        RPTB $he_end-1
        ERR +
$he_end:
$he_last:
        ERR
        MOV AC3, AC0
        BCLR ST1_M40
        AADD #4, SP
        POP T3, T2
        RET

; fx_acc fx_hfit_pow(const int16_t *sw /* XAR0 */, int n /* T0 */): sum of the squares of n
; values (2 nb for nb bins), in AC0
_fx_hfit_pow:
        BSET ST1_M40
        MOV #0, AC0
        SUB #2, T0
        BCC $hp_last, T0 < #0
        MOV T0, CSR
        RPT CSR
        SQAM *AR0+, AC0, AC0
$hp_last:
        SQAM *AR0, AC0, AC0
        BCLR ST1_M40
        RET
