; fx_nlp_dft for the C55x (sotto_fx_enc.c nlp_bin): for k0 <= k < k1,
;     r = sum_{n < 64} x[n] cos512[(k n) & 511],  q = sum_{n < 64} x[n] cos512[((k n) - 128) & 511],
;     rr[k] = (r + 2^14) >> 15,  qq[k] = (q + 2^14) >> 15.
; The two table pointers (AR4 for cos, AR5 for sin) run in circular mode over the 512-word
; table and step by k after each product, so every value pair is one dual multiply-
; accumulate with x[n] as the shared operand (CDP). Exact: the same table entries, products
; and 40-bit sums as the C loop. The circular-mode registers are restored on exit.
; SPDX-License-Identifier: MIT

        .mmregs
        .cpl_on
        .arms_on
        .c54cm_off
        .model call=c55_std
        .model mem=large

        .text
        .global _fx_nlp_dft

; void fx_nlp_dft(const int16_t *x /* XAR0 */, const int16_t *cos512 /* XAR1 */,
;                 int32_t *rr /* XAR2 */, int32_t *qq /* XAR3 */, int k0 /* T0 */, int k1 /* T1 */)
; rr and qq are indexed by k (entries k0..k1-1 written).
_fx_nlp_dft:
        PSHBOTH XAR5                ; save-on-entry
        PSH mmap(ST2_55)
        PSH mmap(BK47)
        PSH mmap(BSA45)
        BSET ST1_M40
        MOV T0, AC0                 ; rr += 2 k0, qq += 2 k0 (int32 entries)
        SFTS AC0, #1
        MOV XAR2, AC1
        ADD AC0, AC1
        MOV AC1, XAR2
        MOV XAR3, AC1
        ADD AC0, AC1
        MOV AC1, XAR3
        MOV #512, mmap(BK47)        ; circular buffers of 512 words for AR4..AR7
        MOV AR1, mmap(BSA45)        ; starting at the table (AR4/AR5 hold offsets)
        MOV XAR1, XAR4              ; the table's page in AR4H, AR5H
        MOV XAR1, XAR5
        BSET AR4LC
        BSET AR5LC
$dft_loop:
        MOV #0, AR4                 ; cos: offset k n, from n = 0
        MOV #384, AR5               ; sin: k n - 128
        MOV XAR0, XCDP
        MPY *(AR4+T0), *CDP+, AC0 :: MPY *(AR5+T0), *CDP+, AC1
        RPT #62
        MAC *(AR4+T0), *CDP+, AC0 :: MAC *(AR5+T0), *CDP+, AC1
        ADD #16384, AC0
        SFTS AC0, #-15
        MOV AC0, dbl(*AR2+)
        ADD #16384, AC1
        SFTS AC1, #-15
        MOV AC1, dbl(*AR3+)
        ADD #1, T0
        CMP T0 < T1, TC1
        BCC $dft_loop, TC1
        BCLR ST1_M40
        POP mmap(BSA45)
        POP mmap(BK47)
        POP mmap(ST2_55)
        POPBOTH XAR5
        RET
