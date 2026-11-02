; The butterfly loops of fx_fft (fx_math.c) for the C55x: the same radix-2 butterfly as the C
; bfly, in the same order. b *= W (W = c - j s), then a, b = (a + b) / 2, (a - b) / 2, rounded:
;   x = round(b W / 2^15), b = bh 2^15 + bl (bl in 0..32767):
;   xr = rh c + ih s + round((rl c + il s) / 2^15), xi = ih c - rh s + round((il c - rl s) / 2^15)
; All products are 16 x 16 bits, the sums stay inside 32 bits (|input| <= 2^23), the same
; integers as the C. The twiddle operands come from the stack (one twiddle, strided
; butterflies) or from fx_cos512 (twiddles inside a group).
; SPDX-License-Identifier: MIT

        .mmregs
        .cpl_on
        .arms_on
        .c54cm_off
        .model call=c55_std
        .model mem=large

        .text
        .global _fx_fft_bfly1
        .global _fx_fft_bfly_grp
        .ref _fx_cos512

; one butterfly: a at *AR0 (real), *AR1 (imaginary), b at *AR2, *AR3. c, s: the twiddle
; operands (Smem), clast, slast: the forms of their last read (with any post-increment),
; sa0, sa1, sb0, sb1: the store forms (with the pointer post-increments). Uses AC0-AC3, T1, T2.
BF      .macro c, s, clast, slast, sa0, sa1, sb0, sb1
        MOV dbl(*AR2), AC2              ; br
        MOV dbl(*AR3), AC3              ; bi
        MOV AC2, T1
        AND #0x7FFF, T1, T1             ; rl
        MOV AC3, T2
        AND #0x7FFF, T2, T2             ; il
        MPYM c, T1, AC0                 ; rl c
        MACM s, T2, AC0, AC0            ; + il s
        MPYM c, T2, AC1                 ; il c
        MASM s, T1, AC1, AC1            ; - rl s
        ADD #16384, AC0
        SFTS AC0, #-15                  ; round((rl c + il s) / 2^15)
        ADD #16384, AC1
        SFTS AC1, #-15                  ; round((il c - rl s) / 2^15)
        SFTS AC2, #-15
        MOV AC2, T1                     ; rh
        SFTS AC3, #-15
        MOV AC3, T2                     ; ih
        MACM c, T1, AC0, AC0            ; + rh c
        MACM s, T2, AC0, AC0            ; + ih s = xr
        MACM clast, T2, AC1, AC1        ; + ih c
        MASM slast, T1, AC1, AC1        ; - rh s = xi
        ADD dbl(*AR0), AC0, AC2         ; a0 + xr
        SUB AC0, dbl(*AR0), AC3         ; a0 - xr
        ADD #1, AC2
        SFTS AC2, #-1
        MOV AC2, sa0                    ; a = (a0 + xr + 1) >> 1
        ADD #1, AC3
        SFTS AC3, #-1
        MOV AC3, sb0                    ; b = (a0 - xr + 1) >> 1
        ADD dbl(*AR1), AC1, AC2
        SUB AC1, dbl(*AR1), AC3
        ADD #1, AC2
        SFTS AC2, #-1
        MOV AC2, sa1
        ADD #1, AC3
        SFTS AC3, #-1
        MOV AC3, sb1
        .endm

; void fx_fft_bfly1(int32_t *ar /* XAR0 */, int32_t *ai /* XAR1 */, int32_t *br /* XAR2 */,
;                   int32_t *bi /* XAR3 */, int cnt /* T0 */, int stride /* T1, in values */,
;                   int32_t cs /* AC0: c << 16 | (s & 0xFFFF) */)
; cnt butterflies with the one twiddle, the pointers advancing by stride values each
_fx_fft_bfly1:
        PSH T2
        AADD #-2, SP
        BSET ST1_M40
        MOV HI(AC0), *SP(#0)            ; c
        MOV AC0, *SP(#1)                ; s
        SUB #1, T0
        MOV T0, BRC0
        MOV T1, T0
        ADD T1, T0                      ; the stride in words
        RPTB $b1_end-1
        BF *SP(#0), *SP(#1), *SP(#0), *SP(#1), dbl(*(AR0+T0)), dbl(*(AR1+T0)), dbl(*(AR2+T0)), dbl(*(AR3+T0))
$b1_end:
        BCLR ST1_M40
        AADD #2, SP
        POP T2
        RET

; void fx_fft_bfly_grp(int32_t *ar /* XAR0 */, int32_t *ai /* XAR1 */, int32_t *br /* XAR2 */,
;                      int32_t *bi /* XAR3 */, int q /* T0 */, int step /* T1 */)
; 2 q consecutive butterflies with the twiddles W^(k step), k = 0 .. 2q - 1: c = fx_cos512[k step],
; s = fx_cos512[(k step + 384) mod 512], and k step < 128 for k < q, so the sine pointer
; starts at 384 and restarts at 0 after q butterflies
_fx_fft_bfly_grp:
        PSH T3, T2
        PSHBOTH XAR5
        BSET ST1_M40
        AMOV #_fx_cos512, XAR4
        AMOV #(_fx_cos512 + 384), XAR5
        SUB #1, T0
        MOV T0, T3                      ; q - 1
        MOV T0, BRC0
        MOV T1, T0                      ; the twiddle step
        RPTB $bg1_end-1
        BF *AR4, *AR5, *(AR4+T0), *(AR5+T0), dbl(*AR0+), dbl(*AR1+), dbl(*AR2+), dbl(*AR3+)
$bg1_end:
        AMOV #_fx_cos512, XAR5
        MOV T3, BRC0
        RPTB $bg2_end-1
        BF *AR4, *AR5, *(AR4+T0), *(AR5+T0), dbl(*AR0+), dbl(*AR1+), dbl(*AR2+), dbl(*AR3+)
$bg2_end:
        BCLR ST1_M40
        POPBOTH XAR5
        POP T3, T2
        RET
