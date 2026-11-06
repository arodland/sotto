/* ft8k: a small fixed-point FT8 receiver for the Baofeng K6's Cortex-M0 (docs/k6-ft8-wspr.md).

   It runs on what the K6 module rules allow: no divide instruction, no libraries, no writable
   statics, no 64-bit arithmetic, every buffer passed in by the caller. One instance watches
   one narrow window (nb bins of about 6.25 Hz, 24 = 150 Hz) and decodes as it goes, with no
   UTC clock: it keeps the last 162 half-symbol rows and looks for a Costas pattern ending at
   every new row, so a transmission can start whenever it likes.

     samples -> mixer (by the caller's phase) -> CIC 16 -> CIC r2 -> complex ~200 Hz
             -> every 80 ms (the caller says when): 32-point DFT, log2 power, 4-bit waterfall
             -> Costas sync score for the start 156 rows back -> local maxima over threshold
             -> 174 max-log LLRs -> layered normalised min-sum LDPC -> CRC-14 -> 77 bits

   Front ends: fk_in takes the K6's GP0 counts (about 86 kHz) with the mixer phase for each,
   fk_sample takes complex samples already at the decimated rate (the host's WAV path).

   The LDPC tables are ft8_lib's (MIT, ft8k_tables.h); the receiver is ours. MIT licence. */
#ifndef FT8K_H
#define FT8K_H
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif

#define FK_ROWS 162                 /* waterfall rows kept: 79 symbols at 2 a symbol + 4 */
#define FK_NBMAX 32                 /* bins (one DFT is 32) */
#define FK_MAXDEC 4                 /* decodes one row can report */
#define FK_WORK (174 * 2 + 83 * 7 + 3) /* LDPC work: L[174] int16, R[83][7] int8 */
#define FK_WF_BYTES(nb) (FK_ROWS * (nb) / 2)
#define FK_SC_WORDS(nb) (3 * ((nb) - 7))

typedef struct {
  uint8_t msg[10];                  /* 77 bits, MSB first (ft8_lib's payload layout) */
  int16_t bin;                      /* base tone's bin in the window, 0 .. nb-8 */
  int16_t score;                    /* sync score, 1/16 of a waterfall step a neighbour */
  int16_t snr;                      /* rough SNR in 2500 Hz, dB */
  uint16_t iters;                   /* LDPC iterations taken */
  uint32_t row;                     /* the row of symbol 0, counted from fk_init */
} fk_dec_t;

typedef struct {
  /* set up by fk_init */
  uint8_t nb, r2, qs;               /* bins, second decimation, waterfall step (log2 Q4 >> qs) */
  uint8_t r1, sh1;                  /* fk_in: the first CIC's decimation (16) and its output shift
                                       (16); a caller with counts of 128 samples sets 8 and 14 */
  int16_t thr;                      /* sync threshold, as fk_dec_t.score */
  uint8_t *wf;                      /* FK_ROWS rows of nb 4-bit values */
  int16_t *sc;                      /* sync scores for the last 3 starts */
  void *work;                       /* FK_WORK bytes */
  /* front end */
  int32_t i1[6], c1[6], i2[6], c2[6]; /* the CICs' integrators and combs (I 0..2, Q 3..5) */
  uint8_t n1, n2, sp, nsamp;        /* decimation counters, sample ring position and fill */
  int16_t si[32], sq[32];           /* the last 32 samples at the decimated rate */
  /* waterfall */
  uint16_t slot;                    /* the next row's slot */
  uint32_t nrow;                    /* rows made */
  int32_t flo;                      /* noise floor (log2 power Q4, x 16) */
  uint32_t nq;                      /* rows quantised */
  uint8_t hann, fosr, tosr, half;   /* Hann window; half-bin max; half-row max (fk_row every
                                       40 ms); which half */
  int16_t uh[FK_NBMAX];             /* tosr: the last half-row's DFT */
  /* results of the last fk_row */
  uint8_t ndec;
  fk_dec_t dec[FK_MAXDEC];
  uint32_t last_row[4];             /* recent decodes, so one signal is reported once */
  uint8_t last_msg[4][10];
  uint8_t nlast;
} fk_t;

/* nb: bins (even, 8 .. 32), r2: the second CIC's decimation (fk_in only; 0 for fk_sample) */
void fk_init(fk_t *k, int nb, int r2, uint8_t *wf, int16_t *sc, void *work);
/* one GP0 count x (2 * ones - L, |x| <= 64) and the mixer phase at it (2^32 a turn) */
void fk_in(fk_t *k, int x, uint32_t phase);
/* one complex sample at the decimated rate (|i|, |q| < 8192) */
void fk_sample(fk_t *k, int i, int q);
/* one waterfall row from the last 32 samples; then sync and decode. Returns the decodes
   (k->dec[0 .. k->ndec-1]). Call it every 80 ms of real time. */
int fk_row(fk_t *k);
/* the two halves of fk_row, for a K6 that sends its waterfall to a host (ft8wf): fk_rowq makes
   one row's nb 4-bit values (two a byte, low nibble first) into row and returns 1, or 0 on a
   half call; fk_putrow takes a row into the waterfall, syncs and decodes as fk_row does */
int fk_rowq(fk_t *k, uint8_t *row);
int fk_putrow(fk_t *k, const uint8_t *row);
/* text for a decode: standard messages ("CQ AG7EW CN87"), else 20 hex digits; returns the end */
char *fk_text(char *p, const uint8_t msg[10]);

/* pieces, for the tests */
int fk_crc_ok(const uint8_t bits[91]);
int fk_ldpc(int16_t llr[174], uint8_t bits[174], void *work, int iters); /* llr may be work */
uint32_t fk_udiv(uint32_t n, uint32_t d);

#ifdef __cplusplus
}
#endif
#endif
