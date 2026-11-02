; fx_cb_dots for the C55x (sotto_fx_enc.c, FX_CB_UNPACKED): for the n vectors of 20 words
; at cb, h[v] = 32 e2[v] - sum_k r[k] y_v[k]. Vectors v and v + n/2 go through the dual
; multiply-accumulate together (r is the shared coefficient operand, read through CDP), one
; cycle per pair of products; the two vectors are 10n words apart, so for the 512-vector
; stages they sit in different SARAM blocks and the two reads don't collide.
; Exact: 19 products of |r| < 2^15 by |y| < 2^7 in a 40-bit accumulator, the same integers
; as the C version (the 20th value of each vector is the zero padding, and r[19] = 0).
; No pointer is post-incremented past the end of its array: an array may end on a 64K-word
; page boundary, and the simulator stops when an address register wraps. The codebook
; pointers step over the padding word with 23-bit adds, and e2 and h are indexed by T0.
; SPDX-License-Identifier: MIT

        .mmregs
        .cpl_on
        .arms_on
        .c54cm_off
        .model call=c55_std
        .model mem=large

        .text
        .global _fx_cb_dots

; void fx_cb_dots(const int16_t *r /* XAR0 */, const int16_t *cb /* XAR1 */, int n /* T0 */,
;                 const int32_t *e2 /* XAR2 */, int32_t *h /* XAR3 */)
_fx_cb_dots:
        PSHBOTH XAR5
        PSHBOTH XAR6
        PSHBOTH XAR7
        MOV T0, AC0
        SFTS AC0, #-1               ; AC0 = n / 2
        MOV AC0, T1                 ; pairs left
        SFTS AC0, #2, AC1           ; 4 (n / 2)
        SFTS AC0, #4, AC2           ; 16 (n / 2)
        ADD AC2, AC1                ; 20 (n / 2): the second half's codebook offset
        MOV XAR1, XAR4              ; y0
        MOV XAR1, AC2
        ADD AC1, AC2
        MOV AC2, XAR5               ; y1 = y0 + 20 (n / 2)
        SFTS AC0, #1, AC1           ; 2 (n / 2): the second half's offset in int32 arrays
        MOV XAR2, AC2
        ADD AC1, AC2
        MOV AC2, XAR6               ; e2 of the second half
        MOV XAR3, AC2
        ADD AC1, AC2
        MOV AC2, XAR7               ; h of the second half
        AMOV #1, XAR1               ; the 23-bit step over a padding word
        MOV #0, T0                  ; 2 v: the index into e2 and h
$cb_loop:
        MOV XAR0, XCDP              ; r
        MPY *AR4+, *CDP+, AC0 :: MPY *AR5+, *CDP+, AC1
        RPT #17
        MAC *AR4+, *CDP+, AC0 :: MAC *AR5+, *CDP+, AC1
        AADD XAR1, XAR4             ; past the padding word, in 23 bits
        AADD XAR1, XAR5
        MOV dbl(*AR2(T0)), AC2
        SFTS AC2, #5
        SUB AC0, AC2
        MOV AC2, dbl(*AR3(T0))
        MOV dbl(*AR6(T0)), AC3
        SFTS AC3, #5
        SUB AC1, AC3
        MOV AC3, dbl(*AR7(T0))
        ADD #2, T0
        SUB #1, T1
        BCC $cb_loop, T1 != #0
        POPBOTH XAR7
        POPBOTH XAR6
        POPBOTH XAR5
        RET
