/* sotto 1200 in the fixed-point build: there is no fixed-point encoder (the K6 only
 * receives), so sotto_lr_enc_create returns NULL. Use the float build (../sotto_lr.c).
 * SPDX-License-Identifier: MIT */
#define SOTTO_BUILD
#include <stddef.h>
#include "../sotto.h"

/* no fixed-point sotto 1200 encoder */
struct sotto_lr_enc *sotto_lr_enc_create(void) { return NULL; }
void sotto_lr_enc_destroy(struct sotto_lr_enc *e) { (void)e; }
int sotto_lr_encode(struct sotto_lr_enc *e, const short speech[SOTTO_LR_SAMPLES_PER_FRAME],
                    unsigned char out[SOTTO_BYTES_PER_FRAME]) { (void)e, (void)speech, (void)out; return 0; }
void sotto_lr_encode_part(struct sotto_lr_enc *e, const short *speech, int n80) { (void)e, (void)speech, (void)n80; }
int sotto_lr_enc_flush(struct sotto_lr_enc *e, unsigned char out[SOTTO_BYTES_PER_FRAME]) { (void)e, (void)out; return 0; }
