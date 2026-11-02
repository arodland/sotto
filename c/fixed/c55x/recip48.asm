; fx_recip48 for the C55x (sotto_fx_enc.c recip48): q = round(2^48 / x) for 2^20 <= x < 2^28,
; the same steps as the C version (fx_recip48_c) with 16 x 16-bit products only:
;   n = bit length of x, xn = x >> (n - 16)                                (16 bits)
;   y = floor((2^32 - 1) / (xn + 1)) from a table seed, one Newton step and the exact
;       remainder (at most one correction)                                 (17 bits)
;   r = -y x mod 2^32, q = y 2^(32-n) + ((r >> (n - 13)) y) >> (n - 3)
;   r = -q x mod 2^32, q corrected until 0 <= r < x, then rounded.
; The products mod 2^32 come from the 15-bit halves of each operand in the 40-bit
; accumulator, whose wrap keeps the low 32 bits right.
; SPDX-License-Identifier: MIT

        .mmregs
        .cpl_on
        .arms_on
        .c54cm_off
        .model call=c55_std
        .model mem=large

        .text
        .global _fx_recip48
        .ref _fx_rcp_tab

; uint32_t fx_recip48(uint32_t x /* AC0 */), result in AC0
; stack: 0 xl (xn & 0x7FFF)  1 x & 0x7FFF  2 x >> 15  3 n   (x = *2 2^15 + *1)
_fx_recip48:
        PSH T3, T2
        AADD #-4, SP
        BSET ST1_M40
        MOV AC0, T1
        AND #0x7FFF, T1, T1
        MOV T1, *SP(#1)             ; x & 0x7FFF
        SFTS AC0, #-15, AC1
        MOV AC1, *SP(#2)            ; x >> 15, < 2^13
        EXP AC0, T0                 ; 30 - (position of the top bit): n = 31 - T0
        MOV #31, T1
        SUB T0, T1                  ; T1 = n
        MOV T1, *SP(#3)
        MOV T1, T2
        ADD #-16, T2                ; n - 16
        NEG T2
        SFTL AC0, T2, AC1           ; AC1 = xn = x >> (n - 16), in [2^15, 2^16)
        MOV AC1, T1
        AND #0x7FFF, T1, T1         ; xl = xn - 2^15
        MOV T1, *SP(#0)
        SFTL AC1, #-7, AC2
        AND #0xFF, AC2
        MOV AC2, AR2
        AMOV #_fx_rcp_tab, XAR3
        AADD AR2, AR3               ; *AR3 = y0, about 2^30 / (xn + 1)
        MPYM *AR3, T1, AC2          ; xl y0
        ADD *AR3 << #15, AC2, AC2   ; + 2^15 y0
        ADD *AR3, AC2, AC2          ; + y0 = (xn + 1) y0
        MOV #0x4000 << #16, AC3     ; 2^30
        SUB AC2, AC3                ; e = 2^30 - (xn + 1) y0, |e| < 2^22
        SFTS AC3, #-7
        MOV AC3, T2                 ; eh = e >> 7
        MPYM *AR3, T2, AC2          ; y0 eh
        SFTS AC2, #-21
        ADD *AR3 << #2, AC2, AC2    ; y = 4 y0 + (y0 eh >> 21)
        ; r = 2^32 - 1 - y (xn + 1), y (xn + 1) = y xl + y 2^15 + y, with y = yh 2^15 + yl
        MOV AC2, T3
        AND #0x7FFF, T3, T3         ; yl
        SFTS AC2, #-15, AC3
        MOV AC3, T0                 ; yh
        MPYM *SP(#0), T0, AC3       ; yh xl
        SFTL AC3, #15
        MACM *SP(#0), T3, AC3, AC3  ; + yl xl = y xl
        SFTL AC2, #15, AC0
        ADD AC0, AC3                ; + y 2^15
        ADD AC2, AC3                ; + y
        MOV #0x7FFF << #16, AC0
        OR #0xFFFF, AC0
        ADD AC0, AC0
        ADD #1, AC0                 ; 2^32 - 1 (positive, 40 bits)
        SUB AC3, AC0                ; r
        ADD #1, AC1                 ; d = xn + 1
$rc_dn:
        BCC $rc_up, AC0 >= #0
        SUB #1, AC2
        ADD AC1, AC0
        B $rc_dn
$rc_up:
        CMP AC0 < AC1, TC1
        BCC $rc_y, TC1
        ADD #1, AC2
        SUB AC1, AC0
        B $rc_up
$rc_y:                              ; AC2 = y = floor((2^32 - 1) / (xn + 1))
        MOV AC2, T3
        AND #0x7FFF, T3, T3         ; yl
        SFTS AC2, #-15, AC3
        MOV AC3, T0                 ; yh
        ; y x mod 2^40 = ((yh xh 2^15 + yh xl + yl xh) 2^15 + yl xl)
        MPYM *SP(#2), T0, AC3
        SFTL AC3, #15
        MACM *SP(#1), T0, AC3, AC3
        MACM *SP(#2), T3, AC3, AC3
        SFTL AC3, #15
        MACM *SP(#1), T3, AC3, AC3
        NEG AC3
        SFTL AC3, #8
        SFTL AC3, #-8               ; r = -y x mod 2^32, < 2^(n + 2)
        MOV *SP(#3), T2
        NEG T2
        ADD #13, T2                 ; 13 - n
        SFTL AC3, T2                ; r >> (n - 13), < 2^15
        MOV AC3, T2
        MOV T0, HI(AC0)
        MPY T2, AC0, AC0            ; yh (r >> (n - 13))
        SFTL AC0, #15
        MOV T3, HI(AC1)
        MAC AC1, T2, AC0            ; + yl (r >> (n - 13)) = y (r >> (n - 13)), < 2^32
        MOV *SP(#3), T2
        NEG T2
        ADD #3, T2                  ; 3 - n
        SFTL AC0, T2                ; >> (n - 3)
        MOV *SP(#3), T2
        NEG T2
        ADD #32, T2                 ; 32 - n
        SFTL AC2, T2, AC1           ; y 2^(32 - n)
        ADD AC0, AC1                ; AC1 = q
        ; r = -q x mod 2^32, q = qh 2^15 + ql
        MOV AC1, T3
        AND #0x7FFF, T3, T3         ; ql
        SFTS AC1, #-15, AC3
        MOV AC3, T0                 ; qh
        MPYM *SP(#2), T0, AC3
        SFTL AC3, #15
        MACM *SP(#1), T0, AC3, AC3
        MACM *SP(#2), T3, AC3, AC3
        SFTL AC3, #15
        MACM *SP(#1), T3, AC3, AC3
        NEG AC3
        SFTL AC3, #8
        SFTL AC3, #-8               ; r as an unsigned 32-bit value
        MOV #0x4000 << #16, AC2
        ADD AC2, AC2                ; 2^31
        MOV *SP(#2) << #15, AC0
        ADD *SP(#1), AC0, AC0       ; x
$rq_dn:                             ; while ((int32_t) r < 0) q--, r += x
        CMP AC3 < AC2, TC1
        BCC $rq_up, TC1
        SUB #1, AC1
        ADD AC0, AC3
        SFTL AC3, #8
        SFTL AC3, #-8
        B $rq_dn
$rq_up:                             ; while (r >= x) q++, r -= x
        CMP AC3 < AC0, TC1
        BCC $rq_rnd, TC1
        ADD #1, AC1
        SUB AC0, AC3
        B $rq_up
$rq_rnd:                            ; q + (r + (x >> 1) >= x)
        SFTL AC0, #-1, AC2
        ADD AC2, AC3
        CMP AC3 < AC0, TC1
        BCC $rq_end, TC1
        ADD #1, AC1
$rq_end:
        MOV AC1, AC0
        BCLR ST1_M40
        AADD #4, SP
        POP T3, T2
        RET

