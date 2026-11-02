; The spectrum bin loops of fx.h for the C55x (C versions in fx_math.c): the largest
; magnitude, the 16-bit copy, and the per-bin powers from the bins rounded to 19 bits:
; a = round(re / 2^t) = ((re >> (t - 1)) + 1) >> 1 (so t = 0 leaves re as it is), and
; a^2 = ah^2 2^30 + ah al 2^16 + al^2 for a = ah 2^15 + al (al in 0..32767), exact in the
; 40-bit accumulator, with the same integers as the C. No pointer is post-incremented
; past the end of an array (the last bin is read without an increment).
; SPDX-License-Identifier: MIT

        .mmregs
        .cpl_on
        .arms_on
        .c54cm_off
        .model call=c55_std
        .model mem=large

        .text
        .global _fx_bins_max
        .global _fx_bins_to16
        .global _fx_bins_pow_max
        .global _fx_bins_pow

; uint32_t fx_bins_max(const int32_t *re /* XAR0 */, const int32_t *im /* XAR1 */, int n /* T0 */)
_fx_bins_max:
        MOV #0, AC0
        SUB #2, T0
        BCC $bm_last, T0 < #0
        MOV T0, BRC0
        RPTBLOCAL $bm_end-1
        MOV dbl(*AR0+), AC1
        ABS AC1, AC1
        MAX AC1, AC0
        MOV dbl(*AR1+), AC1
        ABS AC1, AC1
        MAX AC1, AC0
$bm_end:
$bm_last:
        MOV dbl(*AR0), AC1
        ABS AC1, AC1
        MAX AC1, AC0
        MOV dbl(*AR1), AC1
        ABS AC1, AC1
        MAX AC1, AC0
        RET

; void fx_bins_to16(const int32_t *re /* XAR0 */, const int32_t *im /* XAR1 */,
;                   int16_t *sw /* XAR2 */, int n /* T0 */, int s /* T1 */)
; (x + 2^(s-1)) >> s for s > 0 (the C's rounding shift, equal for every x that fits), x << -s otherwise
_fx_bins_to16:
        MOV #0, AC2                 ; the rounding term
        BCC $bt_sh, T1 <= #0
        MOV #1, AC2
        SUB #1, T1
        SFTL AC2, T1
        ADD #1, T1
$bt_sh:
        NEG T1                      ; SFTS by -s
        SUB #2, T0
        BCC $bt_last, T0 < #0
        MOV T0, BRC0
        RPTBLOCAL $bt_end-1
        MOV dbl(*AR0+), AC0
        ADD AC2, AC0
        SFTS AC0, T1
        MOV AC0, *AR2+
        MOV dbl(*AR1+), AC1
        ADD AC2, AC1
        SFTS AC1, T1
        MOV AC1, *AR2+
$bt_end:
$bt_last:
        MOV dbl(*AR0), AC0
        ADD AC2, AC0
        SFTS AC0, T1
        MOV AC0, *AR2+
        MOV dbl(*AR1), AC1
        ADD AC2, AC1
        SFTS AC1, T1
        MOV AC1, *AR2
        RET

; one bin's power into AC2: the bins at *AR0, *AR1 (post-incremented when the macro's
; argument is +), T1 = 1 - t. Uses AC0, AC1, T2, T3.
POW     .macro inc
        MOV dbl(*AR0:inc:), AC0
        SFTS AC0, T1
        ADD #1, AC0
        SFTS AC0, #-1               ; a = round(re / 2^t)
        MOV AC0, T2
        AND #0x7FFF, T2, T2         ; al
        SFTS AC0, #-15
        MOV AC0, T3                 ; ah
        MOV T3, HI(AC1)
        MPY T3, AC1, AC2            ; ah^2
        SFTL AC2, #14
        MAC AC1, T2, AC2            ; + ah al
        SFTL AC2, #16
        MOV T2, HI(AC1)
        MAC AC1, T2, AC2            ; + al^2 = a^2
        MOV dbl(*AR1:inc:), AC0
        SFTS AC0, T1
        ADD #1, AC0
        SFTS AC0, #-1               ; c = round(im / 2^t)
        MOV AC0, T2
        AND #0x7FFF, T2, T2
        SFTS AC0, #-15
        MOV AC0, T3
        MOV T3, HI(AC1)
        MPY T3, AC1, AC0
        SFTL AC0, #14
        MAC AC1, T2, AC0
        SFTL AC0, #16
        MOV T2, HI(AC1)
        MAC AC1, T2, AC0            ; c^2
        ADD AC0, AC2                ; a^2 + c^2
        .endm

; fx_acc fx_bins_pow_max(const int32_t *re /* XAR0 */, const int32_t *im /* XAR1 */,
;                        int n /* T0 */, int t /* T1 */): the largest power, in AC0
_fx_bins_pow_max:
        PSH T3, T2
        BSET ST1_M40
        NEG T1
        ADD #1, T1                  ; 1 - t
        MOV #0, AC3
        SUB #2, T0
        BCC $pm_last, T0 < #0
        MOV T0, BRC0
        RPTBLOCAL $pm_end-1
        POW +
        MAX AC2, AC3
$pm_end:
$pm_last:
        POW
        MAX AC2, AC3
        MOV AC3, AC0
        BCLR ST1_M40
        POP T3, T2
        RET

; void fx_bins_pow(const int32_t *re /* XAR0 */, const int32_t *im /* XAR1 */, uint32_t *p /* XAR2 */,
;                  int n /* T0 */, int t /* T1 */, int32_t sp /* AC0 */, uint32_t rnd /* AC1 */)
_fx_bins_pow:
        PSH T3, T2
        PSHBOTH XAR5
        BSET ST1_M40
        MOV AC0, AR5                ; sp
        NEG AR5                     ; SFTS by -sp
        MOV AC1, AC3                ; rnd
        NEG T1
        ADD #1, T1                  ; 1 - t
        SUB #2, T0
        BCC $bp_last, T0 < #0
        MOV T0, BRC0
        MOV AR5, T0                 ; -sp
        RPTBLOCAL $bp_end-1
        POW +
        ADD AC3, AC2
        SFTS AC2, T0
        MOV AC2, dbl(*AR2+)
$bp_end:
$bp_last:
        MOV AR5, T0
        POW
        ADD AC3, AC2
        SFTS AC2, T0
        MOV AC2, dbl(*AR2)
        BCLR ST1_M40
        POPBOTH XAR5
        POP T3, T2
        RET
