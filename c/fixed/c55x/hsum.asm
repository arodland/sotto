; fx_hsum for the C55x (sotto_fx_enc.c refine): the harmonic sum
;     E = sum_{k < m} p[(acc + k w) >> 23]
; over a 32-bit power spectrum p, in a 40-bit accumulator; the caller keeps every index
; below 256. Exact: the same integer sum as the C loop.
; The bin addresses go into AR3 and AR4 in turn, each written five instructions before the
; load that uses it, so the pipeline doesn't stall on the address; p must not cross a
; 64K-word page (no object does in the large memory model), so the low 16 bits of the
; address are enough.
; SPDX-License-Identifier: MIT

        .mmregs
        .cpl_on
        .arms_on
        .c54cm_off
        .model call=c55_std
        .model mem=large

        .text
        .global _fx_hsum

; long long fx_hsum(const uint32_t *p /* XAR0 */, uint32_t acc /* AC0 */, uint32_t w /* AC1 */,
;                   int m /* T0 */)
_fx_hsum:
        MOV AC0, AC3                ; acc
        MOV #0, AC0                 ; E
        BCC $hs_ret, T0 <= #0
        BSET ST1_M40                ; 40-bit sums and shifts
        MOV XAR0, XAR3              ; the page of p in both address registers
        MOV XAR0, XAR4
        MOV AR0, T1                 ; the low 16 bits of p's address
        SFTL AC3, #-22, AC2         ; 2 (acc >> 23): the first bin's offset in words
        AND #0xFFFE, AC2, AC2
        ADD T1, AC2
        MOV AC2, AR3
        ADD AC1, AC3
        MOV T0, AR1
        SFTS AR1, #-1               ; pairs of harmonics
        BCC $hs_odd, AR1 == #0
        SUB #1, AR1
        MOV AR1, BRC0
        RPTBLOCAL $hs_end-1
        SFTL AC3, #-22, AC2         ; the next bin -> AR4
        AND #0xFFFE, AC2, AC2
        ADD T1, AC2
        MOV AC2, AR4
        ADD AC1, AC3
        ADD dbl(*AR3), AC0          ; this bin
        SFTL AC3, #-22, AC2         ; the one after -> AR3
        AND #0xFFFE, AC2, AC2
        ADD T1, AC2
        MOV AC2, AR3
        ADD AC1, AC3
        ADD dbl(*AR4), AC0
$hs_end:
$hs_odd:
        MOV T0, AR2                 ; m odd: one more bin, already in AR3
        AND #1, AR2
        BCC $hs_even, AR2 == #0
        ADD dbl(*AR3), AC0
$hs_even:
        BCLR ST1_M40
$hs_ret:
        RET
