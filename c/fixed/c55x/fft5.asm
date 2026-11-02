; fx_fft5_stage32 for the C55x (sotto_fx_hiss.c fft5x, m = 32): the radix-5 stage of the
; 160-point FFT, the same arithmetic as the C in the same order. For each k2 < 32, with the
; five 32-point FFTs in sr/si (row n1 at sr[32 n1 + k2]):
;   Z[n1] = Y[n1] W^(n1 k2 t)  (W = c - j s from fx_cos320, fx_q15_mac2 products),
;   t1 = Z1 + Z4, t2 = Z2 + Z3, t3 = Z1 - Z4, t4 = Z2 - Z3,
;   X0 = Z0 + t1 + t2, a1 = Z0 + q15(t1 C5_1 + t2 C5_2), a2 = Z0 + q15(t1 C5_2 + t2 C5_1),
;   b1 = q15(t3 S5_1 + t4 S5_2), b2 = q15(t3 S5_2 - t4 S5_1),
;   X1 = a1 - j b1, X4 = a1 + j b1, X2 = a2 - j b2, X3 = a2 + j b2 at re/im[32 q + k2].
; q15(a c + b s) = HI(a) c + HI(b) s + ((LO(a) c + LO(b) s + 16384) >> 15) with a = HI 2^15 + LO,
; LO in 0..32767 (fx_q15_mac2). The output slots hold the intermediate values: t1..t4 as
; (HI, LO) word pairs for the constant multiplies, then a1, a2, b2 as 32-bit values.
; SPDX-License-Identifier: MIT

        .mmregs
        .cpl_on
        .arms_on
        .c54cm_off
        .model call=c55_std
        .model mem=large

C5_1    .set 10126                      ; cos(2 pi / 5) Q15
C5_2    .set -26510                     ; cos(4 pi / 5)
S5_1    .set 31164                      ; sin(2 pi / 5)
S5_2    .set 19261                      ; sin(4 pi / 5)

        .text
        .global _fx_fft5_stage32
        .ref _fx_cos320

; the twiddle W^i for the index in T3: c = fx_cos320[i] -> *SP(#0), s = fx_cos320[(i + 240) mod 320] -> *SP(#1)
TWIDDLE .macro
        MOV T3, T0
        MOV *AR4(T0), T1
        MOV T1, *SP(#0)
        ADD #-80, T0
        BCC ok?, T0 >= #0
        ADD #320, T0
ok?:
        MOV *AR4(T0), T1
        MOV T1, *SP(#1)
        .endm

; Z = Y W for Y = (sr, si) at word offset off from AR2/AR3: zr -> AC0, zi -> AC1
;   zr = q15(yr c + yi s), zi = q15(yi c + (-yr) s) with the halves of -yr (as the C). Uses AC2, AC3, T1, T2.
ZMUL    .macro off
        MOV dbl(*AR2(#off)), AC2        ; yr
        MOV dbl(*AR3(#off)), AC3        ; yi
        MOV AC2, T1
        AND #0x7FFF, T1, T1             ; LO(yr)
        MOV AC3, T2
        AND #0x7FFF, T2, T2             ; LO(yi)
        MPYM *SP(#0), T1, AC0           ; LO(yr) c
        MACM *SP(#1), T2, AC0, AC0      ; + LO(yi) s
        ADD #16384, AC0
        SFTS AC0, #-15
        MPYM *SP(#0), T2, AC1           ; LO(yi) c
        NEG AC2, AC2                    ; -yr
        MOV AC2, T1
        AND #0x7FFF, T1, T1             ; LO(-yr)
        MACM *SP(#1), T1, AC1, AC1      ; + LO(-yr) s
        ADD #16384, AC1
        SFTS AC1, #-15
        SFTS AC2, #-15
        MOV AC2, T1                     ; HI(-yr)
        SFTS AC3, #-15
        MOV AC3, T2                     ; HI(yi)
        MACM *SP(#0), T2, AC1, AC1      ; + HI(yi) c
        MACM *SP(#1), T1, AC1, AC1      ; + HI(-yr) s = zi
        MOV dbl(*AR2(#off)), AC2
        SFTS AC2, #-15
        MOV AC2, T1                     ; HI(yr)
        MACM *SP(#0), T1, AC0, AC0      ; + HI(yr) c
        MACM *SP(#1), T2, AC0, AC0      ; + HI(yi) s = zr
        .endm

; the 32-bit value in acx as (HI, LO) at words off, off + 1 from arn. Uses T1; acx is shifted.
HALVES  .macro acx, arn, off
        MOV acx, T1
        AND #0x7FFF, T1, T1
        MOV T1, *arn(#(off + 1))        ; LO
        SFTS acx, #-15
        MOV acx, *arn(#off)             ; HI
        .endm

; acx = q15(a k1 + b k2) for the halves of a at words offa, offa + 1 and of b at offb, offb + 1
; from arn, the constants k1, k2 in the registers tk1, tk2
Q15K    .macro acx, arn, offa, tk1, offb, tk2
        MPYM *arn(#(offa + 1)), tk1, acx
        MACM *arn(#(offb + 1)), tk2, acx, acx
        ADD #16384, acx
        SFTS acx, #-15
        MACM *arn(#offa), tk1, acx, acx
        MACM *arn(#offb), tk2, acx, acx
        .endm

; void fx_fft5_stage32(int32_t *re /* XAR0 */, int32_t *im /* XAR1 */, const int32_t *sr /* XAR2 */,
;                      const int32_t *si /* XAR3 */, int t /* T0 */)
; slot q of element k2: words 64 q from the k2 element of re and of im
_fx_fft5_stage32:
        PSH T3, T2
        PSHBOTH XAR5
        PSHBOTH XAR6
        AADD #-2, SP
        BSET ST1_M40
        AMOV #_fx_cos320, XAR4
        MOV T0, AR5                     ; the twiddle step t
        MOV #0, AR6                     ; i1 = k2 t
        MOV #31, BRC0
        RPTB $f5_end-1
        MOV dbl(*AR2), AC0              ; X0 = Z0 so far
        MOV AC0, dbl(*AR0)
        MOV dbl(*AR3), AC0
        MOV AC0, dbl(*AR1)
        MOV AR6, T3                     ; i1
        TWIDDLE
        ZMUL 64                         ; Z1
        MOV AC0, dbl(*AR0(#64))         ; kept in slot 1 until Z4 is known
        MOV AC1, dbl(*AR1(#64))
        ADD AR6, T3
        ADD AR6, T3
        ADD AR6, T3                     ; 4 i1
        TWIDDLE
        ZMUL 256                        ; Z4
        ADD dbl(*AR0(#64)), AC0, AC2    ; t1r = z1r + z4r
        SUB AC0, dbl(*AR0(#64)), AC3    ; t3r = z1r - z4r
        ADD dbl(*AR0), AC2, AC0         ; X0r += t1r
        MOV AC0, dbl(*AR0)
        HALVES AC2, AR0, 64             ; t1r -> slot 1
        HALVES AC3, AR0, 192            ; t3r -> slot 3
        ADD dbl(*AR1(#64)), AC1, AC2    ; t1i
        SUB AC1, dbl(*AR1(#64)), AC3    ; t3i
        ADD dbl(*AR1), AC2, AC1
        MOV AC1, dbl(*AR1)
        HALVES AC2, AR1, 64
        HALVES AC3, AR1, 192
        MOV AR6, T3
        ADD AR6, T3                     ; 2 i1
        TWIDDLE
        ZMUL 128                        ; Z2
        MOV AC0, dbl(*AR0(#128))        ; kept in slot 2 until Z3 is known
        MOV AC1, dbl(*AR1(#128))
        ADD AR6, T3                     ; 3 i1
        TWIDDLE
        ZMUL 192                        ; Z3
        ADD dbl(*AR0(#128)), AC0, AC2   ; t2r = z2r + z3r
        SUB AC0, dbl(*AR0(#128)), AC3   ; t4r = z2r - z3r
        ADD dbl(*AR0), AC2, AC0         ; X0r += t2r: done
        MOV AC0, dbl(*AR0)
        HALVES AC2, AR0, 128            ; t2r -> slot 2
        HALVES AC3, AR0, 256            ; t4r -> slot 4
        ADD dbl(*AR1(#128)), AC1, AC2
        SUB AC1, dbl(*AR1(#128)), AC3
        ADD dbl(*AR1), AC2, AC1
        MOV AC1, dbl(*AR1)
        HALVES AC2, AR1, 128
        HALVES AC3, AR1, 256
        MOV #C5_1, T1
        MOV #C5_2, T2
        Q15K AC0, AR0, 64, T1, 128, T2
        ADD dbl(*AR2), AC0              ; a1r = z0r + q15(t1r C5_1 + t2r C5_2)
        Q15K AC1, AR1, 64, T1, 128, T2
        ADD dbl(*AR3), AC1              ; a1i
        Q15K AC2, AR0, 64, T2, 128, T1
        ADD dbl(*AR2), AC2              ; a2r
        Q15K AC3, AR1, 64, T2, 128, T1
        ADD dbl(*AR3), AC3              ; a2i
        MOV AC0, dbl(*AR0(#64))         ; a1 -> slot 1 (t1 no longer needed)
        MOV AC1, dbl(*AR1(#64))
        MOV AC2, dbl(*AR0(#128))        ; a2 -> slot 2
        MOV AC3, dbl(*AR1(#128))
        MOV #S5_1, T1
        MOV #S5_2, T2
        MOV #-S5_1, T3
        Q15K AC0, AR0, 192, T1, 256, T2         ; b1r
        Q15K AC1, AR1, 192, T1, 256, T2         ; b1i
        Q15K AC2, AR0, 192, T2, 256, T3         ; b2r
        Q15K AC3, AR1, 192, T2, 256, T3         ; b2i
        MOV AC2, dbl(*AR0(#192))        ; b2 -> slot 3 (t3, t4 no longer needed)
        MOV AC3, dbl(*AR1(#192))
        ADD dbl(*AR0(#64)), AC1, AC2    ; X1r = a1r + b1i
        SUB AC1, dbl(*AR0(#64)), AC3    ; X4r = a1r - b1i
        MOV AC2, dbl(*AR0(#64))
        MOV AC3, dbl(*AR0(#256))
        SUB AC0, dbl(*AR1(#64)), AC2    ; X1i = a1i - b1r
        ADD dbl(*AR1(#64)), AC0, AC3    ; X4i = a1i + b1r
        MOV AC2, dbl(*AR1(#64))
        MOV AC3, dbl(*AR1(#256))
        MOV dbl(*AR0(#192)), AC0        ; b2r
        MOV dbl(*AR1(#192)), AC1        ; b2i
        ADD dbl(*AR0(#128)), AC1, AC2   ; X2r = a2r + b2i
        SUB AC1, dbl(*AR0(#128)), AC3   ; X3r = a2r - b2i
        MOV AC2, dbl(*AR0(#128))
        MOV AC3, dbl(*AR0(#192))
        SUB AC0, dbl(*AR1(#128)), AC2   ; X2i = a2i - b2r
        ADD dbl(*AR1(#128)), AC0, AC3   ; X3i = a2i + b2r
        MOV AC2, dbl(*AR1(#128))
        MOV AC3, dbl(*AR1(#192))
        AADD #2, AR0                    ; the next k2
        AADD #2, AR1
        AADD #2, AR2
        AADD #2, AR3
        ADD AR5, AR6                    ; i1 += t
$f5_end:
        BCLR ST1_M40
        AADD #2, SP
        POPBOTH XAR6
        POPBOTH XAR5
        POP T3, T2
        RET
