; fx_mp_sums for the C55x (sotto_fx_dec.c min_phase): the four dot products of one row of
; the cepstrum or phase table with the high and low halves of the coefficients,
;     out[0] = sum_{j < n1} h[j] row[j],  out[1] = sum_{j < n1} l[j] row[j]
;     out[2] = sum_{n1 <= j < n1 + n2} h[j] row[j],  out[3] = the same with l
; as one dual multiply-accumulate per table value (row[j] is the shared operand, read
; through CDP), in the 40-bit accumulators: the same integers as the C loops.
; SPDX-License-Identifier: MIT

        .mmregs
        .cpl_on
        .arms_on
        .c54cm_off
        .model call=c55_std
        .model mem=large

        .text
        .global _fx_mp_sums

; store the 40-bit accumulator as a long long at *AR3 (high double word first), AR3 += 4
STORE40 .macro acx
        SFTS acx, #-32, AC3
        MOV AC3, dbl(*AR3)
        MOV acx, dbl(*AR3(short(#2)))
        AADD #4, AR3
        .endm

; void fx_mp_sums(const int16_t *h /* XAR0 */, const int16_t *l /* XAR1 */,
;                 const int16_t *row /* XAR2 */, int n1 /* T0 */, int n2 /* T1 */,
;                 fx_acc out[4] /* XAR3 */)
_fx_mp_sums:
        BSET ST1_M40
        MOV XAR2, XCDP              ; the row
        MOV #0, AC0
        MOV #0, AC1
        SUB #1, T0
        MOV T0, CSR
        RPT CSR
        MAC *AR0+, *CDP+, AC0 :: MAC *AR1+, *CDP+, AC1
        STORE40 AC0
        STORE40 AC1
        MOV #0, AC0
        MOV #0, AC1
        SUB #1, T1
        MOV T1, CSR
        RPT CSR
        MAC *AR0+, *CDP+, AC0 :: MAC *AR1+, *CDP+, AC1
        STORE40 AC0
        STORE40 AC1
        BCLR ST1_M40
        RET
