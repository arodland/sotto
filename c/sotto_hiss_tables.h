/* v3.8 hiss-band tables, generated into sotto_hiss_tables.c by tools/export_hiss_c.py from
 * sim/data/hiss_band.npz, hiss_vowel_fine.npz and lr_hiss_b.npz (LibriSpeech-trained, MIT). */
#ifndef SOTTO_HISS_TABLES_H
#define SOTTO_HISS_TABLES_H
extern const double sotto_hiss_guess_w[3 * 8 * 4];   /* [class][feature][band], hiss_band.guess */
extern const double sotto_hiss_cb6[64 * 4];          /* residual codebook of UU frames, dB */
extern const double sotto_hiss_fine_w[16 * 16];      /* [1, 15 bands] -> 16 bands, hiss_band.fine_predict */
extern const double sotto_hiss_fine_m0[16];          /* mean speaker miss, dB */
extern const double sotto_hiss_fine_vt[3 * 16];      /* speaker-shape PCA axes */
extern const double sotto_resample_taps[41];         /* half-band FIR of scipy's resample_poly */
extern const double sotto_lr_hiss_bcb[4 * 4];        /* sotto 1200 hiss: B frames' 2-bit level offsets */
#endif
