; fx_oa_pair for the C55x (sotto_fx_dec.c synth, the overlap-add sums): for samples n and
; n + 1 of the inverse DFT of the nb harmonics on 512-point bins,
;     a(n) = sum_j re[j] cos512[(bin[j] n) mod 512] - im[j] cos512[(bin[j] n - 128) mod 512]
; in the 40-bit accumulators, the same integers as the C. The cosine and sine indices walk
; fx_cos512 as a circular buffer of 512 words (AR4 and AR5, buffer start BSA45, size BK47),
; so the step from n to n + 1 (+ bin[j]) wraps for free; the index arithmetic is one
; multiply per harmonic for the two samples.
; SPDX-License-Identifier: MIT

        .mmregs
        .cpl_on
        .arms_on
        .c54cm_off
        .model call=c55_std
        .model mem=large

        .text
        .global _fx_oa_pair
        .ref _fx_cos512

; fx_acc fx_oa_pair(const int16_t *bin /* XAR0 */, const int16_t *re /* XAR1 */,
;                   const int16_t *im /* XAR2 */, int nb /* T0 */, int n /* T1 */,
;                   fx_acc *a1 /* XAR3 */): returns a(n), stores a(n + 1) at *a1
_fx_oa_pair:
        PSH T3, T2
        PSHBOTH XAR5
        BSET ST1_M40
        AMOV #_fx_cos512, XAR4
        MOV AR4, mmap(BSA45)        ; the circular buffer: fx_cos512, 512 words
        MOV #512, mmap(BK47)
        MOV XAR4, XAR5              ; its page
        BSET ST2_AR4LC
        BSET ST2_AR5LC
        MOV T1, HI(AC2)             ; n
        MOV #0, AC0                 ; a(n)
        MOV #0, AC1                 ; a(n + 1)
        SUB #1, T0
        MOV T0, BRC0
        RPTB $oa_end-1
        MOV *AR0+, T0               ; b = bin[j]
        MPY T0, AC2, AC3            ; b n
        AND #511, AC3
        MOV AC3, AR4                ; x = (b n) mod 512
        ADD #384, AC3
        AND #511, AC3
        MOV AC3, AR5                ; (x - 128) mod 512
        MOV *AR1+, T1               ; re[j]
        MOV *AR2+, T2               ; im[j]
        MACM *(AR4+T0), T1, AC0, AC0   ; re cos[x], then x + b (wrapped) for sample n + 1
        MACM *AR4, T1, AC1, AC1
        MASM *(AR5+T0), T2, AC0, AC0   ; - im sin
        MASM *AR5, T2, AC1, AC1
$oa_end:
        BCLR ST2_AR4LC
        BCLR ST2_AR5LC
        SFTS AC1, #-32, AC3         ; a(n + 1) as a long long: high double word first
        MOV AC3, dbl(*AR3)
        MOV AC1, dbl(*AR3(short(#2)))
        BCLR ST1_M40
        POPBOTH XAR5
        POP T3, T2
        RET
