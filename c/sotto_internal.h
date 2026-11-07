/* Shared between sotto.c (v3.7 core) and sotto_hiss.c (v3.8 layer). Not installed.
 * SPDX-License-Identifier: MIT */
#ifndef SOTTO_INTERNAL_H
#define SOTTO_INTERNAL_H
#include "sotto.h"

#define SOTTO_K 20                    /* rate-K envelope points */
#define SOTTO_NFB 7                   /* hiss_band.FB: 1250 .. 3800 Hz */

/* the fields of one 20 ms frame as the decoder core takes them (sotto 1200 builds them) */
typedef struct {
  int code, wi, ei;                   /* voicing code, 7-bit pitch index, 5-bit energy code */
  double c[SOTTO_K - 1];              /* shape: DCT coefficients 1..K-1, dB */
} sotto_fparams_t;

/* one 20 ms frame out of the v3.7 encoder core, with what the v3.8 rules need per 10 ms half */
typedef struct {
  unsigned char bits[SOTTO_BYTES_PER_FRAME];
  double en[2];                       /* mean of the rate-K envelope, dB (hiss_band._onsets) */
  double tilt[2];                     /* mean rk below 1 kHz - mean rk above 2 kHz, dB */
  double snr[2];                      /* harmonic SNR below 1 kHz; NAN when unvoiced */
  double lwo[2];                      /* log of the tracked Wo (pitch jitter) */
  int loud[2];                        /* above the causal loudness reference */
  sotto_fparams_t p;                  /* the frame's fields before shape quantization (c = target) */
} sotto_enc_frame_t;

/* what the v3.8 decoder layer reads back after each core frame */
typedef struct {
  double env[2][SOTTO_NFB];           /* dB envelope at FB of the two halves just synthesised */
  int cls[2];                         /* their class: 0 unvoiced, 1 voiced, 2 mixed */
  unsigned char b2[SOTTO_BYTES_PER_FRAME];   /* the bits the frame just received decodes (concealed) */
  int run;                            /* lost frames in a row, 0 for a good frame */
} sotto_dec_info_t;

void sotto_enc_analyse_block(struct sotto_enc *e, const short x8[80]);
void sotto_enc_flush_core(struct sotto_enc *e);
int sotto_enc_pop_frame(struct sotto_enc *e, sotto_enc_frame_t *f);
const sotto_dec_info_t *sotto_dec_info(const struct sotto_dec *d);
void sotto_dec_frame(struct sotto_dec *d, const sotto_fparams_t *p, int run, short speech[160]);
double sotto_env_db(const double *rk, double hz);   /* np.interp(hz, KFREQ, rk) */

/* sotto 1200 hiss (VARIANT=v3.8): the sotto 1200 decoder calls the hook after every 20 ms
   frame the core takes, with the frame's voicing code, its 4-band hiss correction (dB, NULL
   when flushed), its side-channel bit, whether its superframe arrived (sgood), its
   concealment run and the core's 160 samples out; flushed = 1 for the core's last output */
struct sotto_lr_dec;
typedef void (*lr_hiss_hook_t)(void *ctx, int code, const double *res, int side, int sgood, int run,
                               const short *out, int flushed);
void sotto_lr_dec_set_hook(struct sotto_lr_dec *d, lr_hiss_hook_t hook, void *ctx);
const struct sotto_dec *sotto_lr_dec_core(const struct sotto_lr_dec *d);

/* sotto 600 / 400 hiss (VARIANT=v3.8): the float sotto 600 / 400 decoder (sotto_vlr_dec.c)
   calls the hook after every 20 ms frame the core takes, with the frame's number g, voicing
   code and concealment run, whether its superframe arrived or was rebuilt (avail), whether it
   was rebuilt (reb), and the core's 160 samples out; flushed = 1 (g = -1) for the core's last
   output. sotto_hiss.c reads the superframes' hiss fields itself. */
struct sotto_vlr_dec;
typedef void (*vlr_hiss_hook_t)(void *ctx, int g, int code, int run, int avail, int reb, const short *out,
                                int flushed);
void sotto_vlr_dec_set_hook(struct sotto_vlr_dec *d, vlr_hiss_hook_t hook, void *ctx);
const struct sotto_dec *sotto_vlr_dec_core(const struct sotto_vlr_dec *d);
/* and its encoder: sotto 600 / 400's coder (sotto_vlr.c) fed the frames after v3.8's rules,
   then sotto_vlr_enc_pop */
struct sotto_vlr_enc;
struct sotto_vlr_enc *sotto_vlr_enc_create_frames(int rate);
void sotto_vlr_enc_push(struct sotto_vlr_enc *e, const sotto_fparams_t *p);
void sotto_vlr_enc_end(struct sotto_vlr_enc *e);
/* sotto 1200 hiss's encoder: sotto 1200's coder fed the frames after v3.8's rules */
struct sotto_lr_enc;
struct sotto_lr_enc *sotto_lr_enc_create_frames(void);
void sotto_lr_enc_push(struct sotto_lr_enc *e, const sotto_fparams_t *p);
void sotto_lr_enc_end(struct sotto_lr_enc *e);
int sotto_lr_enc_pop(struct sotto_lr_enc *e, unsigned char out[SOTTO_BYTES_PER_FRAME]);
void sotto_lr_frames(const unsigned char *sf, const unsigned char *next, sotto_fparams_t *a, sotto_fparams_t *b);
void sotto_lr_set_hiss(unsigned char *sf, int wa, int dw);
#endif
