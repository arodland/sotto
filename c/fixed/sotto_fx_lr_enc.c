/* sotto 1200 and sotto 600 / 400 in the fixed-point build: there is no fixed-point encoder
 * for them (the K6 only receives), so sotto_lr_enc_create and sotto_vlr_enc_create return
 * NULL. Use the float build (../sotto_lr.c, ../sotto_vlr.c). Nor is there a fixed-point
 * sotto 1200 hiss or sotto 600 / 400 hiss (16 kHz) encoder or decoder yet: their create
 * functions return NULL too (the float ones are in ../sotto_hiss.c).
 * SPDX-License-Identifier: MIT */
#define SOTTO_BUILD
#include <stddef.h>
#include "../sotto.h"
#include "../sotto_vlr.h"

/* no fixed-point sotto 1200 encoder */
struct sotto_lr_enc *sotto_lr_enc_create(void) { return NULL; }
void sotto_lr_enc_destroy(struct sotto_lr_enc *e) { (void)e; }
int sotto_lr_encode(struct sotto_lr_enc *e, const short speech[SOTTO_LR_SAMPLES_PER_FRAME],
                    unsigned char out[SOTTO_BYTES_PER_FRAME]) { (void)e, (void)speech, (void)out; return 0; }
void sotto_lr_encode_part(struct sotto_lr_enc *e, const short *speech, int n80) { (void)e, (void)speech, (void)n80; }
int sotto_lr_enc_flush(struct sotto_lr_enc *e, unsigned char out[SOTTO_BYTES_PER_FRAME]) { (void)e, (void)out; return 0; }

/* no fixed-point sotto 600 / 400 encoder */
struct sotto_vlr_enc *sotto_vlr_enc_create(int rate) { (void)rate; return NULL; }
void sotto_vlr_enc_destroy(struct sotto_vlr_enc *e) { (void)e; }
void sotto_vlr_encode(struct sotto_vlr_enc *e, const short *speech, int n80) { (void)e, (void)speech, (void)n80; }
int sotto_vlr_enc_pop(struct sotto_vlr_enc *e, unsigned char out[SOTTO_BYTES_PER_FRAME]) { (void)e, (void)out; return 0; }
int sotto_vlr_enc_flush(struct sotto_vlr_enc *e, unsigned char out[SOTTO_BYTES_PER_FRAME]) { (void)e, (void)out; return 0; }

#ifdef SOTTO_V38
/* no fixed-point sotto 1200 hiss */
struct sotto_lr_enc16 *sotto_lr_enc16_create(void) { return NULL; }
void sotto_lr_enc16_destroy(struct sotto_lr_enc16 *e) { (void)e; }
int sotto_lr_encode16(struct sotto_lr_enc16 *e, const short speech[SOTTO_LR_SAMPLES_PER_FRAME_16K],
                      unsigned char out[SOTTO_BYTES_PER_FRAME]) { (void)e, (void)speech, (void)out; return 0; }
int sotto_lr_enc16_flush(struct sotto_lr_enc16 *e, unsigned char out[SOTTO_BYTES_PER_FRAME]) { (void)e, (void)out; return 0; }
struct sotto_lr_dec16 *sotto_lr_dec16_create(void) { return NULL; }
void sotto_lr_dec16_destroy(struct sotto_lr_dec16 *d) { (void)d; }
void sotto_lr_decode16(struct sotto_lr_dec16 *d, const unsigned char in[SOTTO_BYTES_PER_FRAME], int good,
                       short speech[SOTTO_LR_SAMPLES_PER_FRAME_16K]) { (void)d, (void)in, (void)good, (void)speech; }
int sotto_lr_dec16_flush(struct sotto_lr_dec16 *d, short speech[SOTTO_LR_SAMPLES_PER_FRAME_16K]) { (void)d, (void)speech; return 0; }

/* no fixed-point sotto 600 / 400 hiss */
struct sotto_vlr_enc16 *sotto_vlr_enc16_create(int rate) { (void)rate; return NULL; }
void sotto_vlr_enc16_destroy(struct sotto_vlr_enc16 *e) { (void)e; }
void sotto_vlr_encode16(struct sotto_vlr_enc16 *e, const short *speech, int n160) { (void)e, (void)speech, (void)n160; }
int sotto_vlr_enc16_pop(struct sotto_vlr_enc16 *e, unsigned char out[SOTTO_BYTES_PER_FRAME]) { (void)e, (void)out; return 0; }
int sotto_vlr_enc16_flush(struct sotto_vlr_enc16 *e, unsigned char out[SOTTO_BYTES_PER_FRAME]) { (void)e, (void)out; return 0; }
struct sotto_vlr_dec16 *sotto_vlr_dec16_create(int rate) { (void)rate; return NULL; }
void sotto_vlr_dec16_destroy(struct sotto_vlr_dec16 *d) { (void)d; }
int sotto_vlr_samples16(const struct sotto_vlr_dec16 *d) { (void)d; return 0; }
void sotto_vlr_decode16(struct sotto_vlr_dec16 *d, const unsigned char in[SOTTO_BYTES_PER_FRAME], int good,
                        short *speech) { (void)d, (void)in, (void)good, (void)speech; }
int sotto_vlr_dec16_flush(struct sotto_vlr_dec16 *d, short *speech) { (void)d, (void)speech; return 0; }
#endif
