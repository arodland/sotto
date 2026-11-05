/* sotto 1200 constants: must match sim/sotto_lr.py (tests/test_lr.py checks them).
 * SPDX-License-Identifier: MIT */
#ifndef SOTTO_LR_TABLES_H
#define SOTTO_LR_TABLES_H
#define LR_FORMAT "sotto 1200 v1 (v3.7 core, 48 bits / 40 ms)"
#define LR_NA 2               /* A's shape: stages 1-2 of the 9+9+8+8 codebook (18 bits) */
#define LR_BST 2              /* B's shape residual: one codeword of stage 3 (8 bits) ... */
#define LR_BG 1.5             /* ... times 1.5 */
#define LR_WB_BITS 3
#define LR_EB_BITS 3
#define LR_JOINT 1.0
#define LR_W_ZERO 4           /* B's pitch code when B is unvoiced (step 0) */
#define LR_E_ZERO 4           /* B's energy code with step 0 */
#ifndef LR_INTERP
#define LR_INTERP 1           /* rebuild a lost superframe between two good ones from both (0: hold) */
#endif
static const signed char lr_w_tab[8] = {-12, -5, -2, -1, 0, 2, 5, 12};    /* Sotto1200.W_TAB */
static const unsigned char lr_w_abs[8] = {10, 23, 30, 36, 42, 53, 75, 103}; /* W_ABS: no voiced anchor */
static const signed char lr_e_tab[8] = {-6, -3, -2, -1, 0, 1, 2, 4};      /* E_TAB */
#endif
