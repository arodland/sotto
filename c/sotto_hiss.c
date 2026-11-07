/* sotto v3.8: the 16 kHz layer over the v3.7 core (sim/hiss_band.py SottoV38).
 *
 * Encoder: 16 kHz in, decimated to 8 kHz for the core; the v3.8 rules (onset recoding, whisper,
 * voicing smoothing, burst and sibilant rules) run over a ring of the core's frames with
 * SOTTO_RULE_LAG frames of look-ahead; all-unvoiced frames then get a 6-bit 4-8 kHz index in
 * their pitch field and one bit of the speaker-shape side channel. The index and the side
 * channel are measured against the encoder's own decoder (a second core decoder, as the
 * Python's _low16 and _side_pc do).
 *
 * Decoder: the core's 8 kHz output, upsampled, plus a 4-8 kHz noise band shaped per 10 ms
 * STFT frame: consonant hiss from the index of unvoiced frames, a vowel-following layer from
 * the decoded 0-4 kHz spectrum and the speaker shape, blended per bin. The look-ahead the
 * Python's framing implies (HB_LAG, the predictor on decoded audio, 40 ms of flicker
 * bridging) makes the 16 kHz output SOTTO_DEC16_DELAY_FRAMES frames late.
 *
 * Each function names the Python it mirrors. SPDX-License-Identifier: MIT */
#define SOTTO_BUILD
#include <limits.h>
#include <math.h>
#include <stdlib.h>
#include <string.h>

#include "codec2_fft.h"
#include "sotto.h"
#include "sotto_hiss_tables.h"
#include "sotto_internal.h"

#define FS16 16000
#define N16 SOTTO_SAMPLES_PER_FRAME_16K     /* 320 */
#define HN 320                              /* STFT window, 20 ms at 16 kHz */
#define HH 160                              /* hop */
#define NBIN 161
#define NTAPS 41
#define K SOTTO_K
#define NFB SOTTO_NFB
#define HB_LAG 2
#define WIN_FRAMES 100
#define MSG_BITS 16
#define UV_GAIN 3.0
#define MIXED_DB (-3.0)
#define BURST_DB 12.0
#define SIB_DB (-10.0)                      /* sib_db = -10, sib_voiced = True (the "z fix") */
#define ONSET_LOUD_DB 10.0
#define ONSET_LOWDOM 3.0
#define WHISPER_MIN 10
#define WHISPER_SNR 6.0
#define WHISPER_JIT 0.025
#define VOICED_TRIM (-5.0)
#define SMOOTH_MS 30.0
#define BRIDGE 4                            /* blend: flickers of <= 4 frames bridged */
#define PC_STEP 4.0
#define PCT_WARM 50
#define PCT_Q 60.0
#define SIDE_MIN_FRAMES 20
#ifndef SOTTO_RULE_LAG
#define SOTTO_RULE_LAG 10                   /* frames of look-ahead for the encoder rules */
#endif
#define FRING 128                           /* frames of history the rules see */
/* STFT frames of features kept: the sotto 1200 hiss encoder reaches 2 * SOTTO_RULE_LAG + 29
   back (49 at the default lag) */
#define SRING (2 * SOTTO_RULE_LAG + 29 <= 64 ? 64 : 128)
#define PCT_MAX 12000                       /* frames the running percentile remembers (120 s) */
#define ALIGN_CALLS 2                       /* core decoder calls before its output lines up with the input */

static const double PI_D = 3.14159265358979323846;
static const double pc_min[3] = {-52.0, -32.0, -32.0};
static const double hbc[4] = {4500.0, 5500, 6500, 7500};
static double fhc[16];                      /* 4125 .. 7875 */
static double win[HN];                      /* sqrt-Hann */
static double ramp[NBIN];                   /* clip((FR - 3900) / 200, 0, 1) */
static double fr_hz[NBIN];
static int tables_ready;

static void init_tables(void) {
  if (tables_ready) return;
  for (int i = 0; i < HN; i++) win[i] = sqrt(0.5 - 0.5 * cos(2 * PI_D * i / HN));
  for (int k = 0; k < NBIN; k++) {
    fr_hz[k] = k * (double)FS16 / HN;
    double r = (fr_hz[k] - 3900) / 200;
    ramp[k] = r < 0 ? 0 : r > 1 ? 1 : r;
  }
  for (int b = 0; b < 16; b++) fhc[b] = 4000 + 250 * b + 125.0;
  tables_ready = 1;
}

/* np.interp, clamped */
static double interp(double x, const double *xp, const double *fp, int n) {
  if (x <= xp[0]) return fp[0];
  if (x >= xp[n - 1]) return fp[n - 1];
  int j = 0;
  while (xp[j + 1] < x) j++;
  if (x == xp[j + 1]) return fp[j + 1];
  return fp[j] + (fp[j + 1] - fp[j]) * (x - xp[j]) / (xp[j + 1] - xp[j]);
}

/* ------------------------------------------------------------------ STFT and bands */
/* power spectrum of 320 samples through the sqrt-Hann window (hiss_band.stft) */
static void stft_power(codec2_fftr_cfg fft, const double *x, double *P) {
  float in[HN];
  COMP out[NBIN];
  for (int i = 0; i < HN; i++) in[i] = (float)(x[i] * win[i]);
  codec2_fftr(fft, in, out);
  for (int k = 0; k < NBIN; k++) P[k] = (double)out[k].real * out[k].real + (double)out[k].imag * out[k].imag;
}

/* mean power of the bins with a_hz <= FR < b_hz, in dB (hiss_band.bands_db) */
static double band_db(const double *P, int a_hz, int b_hz) {
  int k0 = (a_hz + 49) / 50, k1 = (b_hz + 49) / 50;
  double s = 0;
  for (int k = k0; k < k1; k++) s += P[k];
  return 10 * log10(s / (k1 - k0) + 1e-12);
}

static double total_db(const double *P) {
  double s = 0;
  for (int k = 0; k < NBIN; k++) s += P[k];
  return 10 * log10(s + 1e-12);
}

/* hiss_band.fine_predict: 16 bands at 4-8 kHz (dB re 3.0-3.5 kHz) from the 0-4 kHz bands,
   and the reference level */
static void fine_predict(const double *P, double *pr, double *ref) {
  double x[16];
  *ref = band_db(P, 3000, 3500);
  x[0] = 1.0;
  for (int b = 0; b < 14; b++) x[1 + b] = band_db(P, 250 + 250 * b, 500 + 250 * b) - *ref;
  x[15] = band_db(P, 3750, 3950) - *ref;
  for (int o = 0; o < 16; o++) {
    double s = 0;
    for (int i = 0; i < 16; i++) s += x[i] * sotto_hiss_fine_w[i * 16 + o];
    pr[o] = s;
  }
}

/* hiss_band.guess for one frame: the four 4-8 kHz levels from the FB envelope, by class */
static void guess(int cls, const double *E, double *p) {
  double ref = E[6], feat[8];
  feat[0] = 1.0;
  for (int i = 0; i < 6; i++) feat[1 + i] = E[i] - ref;
  feat[7] = ref;
  for (int b = 0; b < 4; b++) {
    double s = 0;
    for (int i = 0; i < 8; i++) s += feat[i] * sotto_hiss_guess_w[(cls * 8 + i) * 4 + b];
    p[b] = s + ref;
  }
}

/* ------------------------------------------------------------------ side channel */
static void crc4(const int *b, int n, int *out) {
  int r = 0;
  for (int i = 0; i < n; i++) {
    int fb = ((r >> 3) & 1) ^ b[i];
    r = ((r << 1) & 0xF) ^ (fb ? 0x3 : 0);
  }
  for (int i = 0; i < 4; i++) out[i] = (r >> (3 - i)) & 1;
}

/* hiss_band.pack_pc: 3 speaker-shape coefficients -> 12 bits + CRC-4 */
static void pack_pc(const double *pc, int *b) {
  for (int c = 0; c < 3; c++) {
    double q = rint((pc[c] - pc_min[c]) / PC_STEP);
    int qi = q < 0 ? 0 : q > 15 ? 15 : (int)q;
    for (int i = 0; i < 4; i++) b[4 * c + i] = (qi >> (3 - i)) & 1;
  }
  crc4(b, 12, b + 12);
}

/* hiss_band.unpack_pc: 1 and the coefficients when the CRC holds */
static int unpack_pc(const int *b, double *pc) {
  int c[4];
  crc4(b, 12, c);
  for (int i = 0; i < 4; i++)
    if (c[i] != b[12 + i]) return 0;
  for (int i = 0; i < 3; i++) {
    int q = (b[4 * i] << 3) | (b[4 * i + 1] << 2) | (b[4 * i + 2] << 1) | b[4 * i + 3];
    pc[i] = pc_min[i] + PC_STEP * q;
  }
  return 1;
}

static int frame_code(const unsigned char *bits) { return bits[0] >> 6; }
static int frame_bit(const unsigned char *bits, int pos) { return (bits[pos >> 3] >> (7 - (pos & 7))) & 1; }
static void set_bit(unsigned char *bits, int pos, int v) {
  if (v) bits[pos >> 3] |= 0x80 >> (pos & 7);
  else bits[pos >> 3] &= ~(0x80 >> (pos & 7));
}
static int index6(const unsigned char *bits) { return (bits[0] & 0x3F); }   /* bits 2..7 */

static unsigned rng_next(unsigned *s) {   /* xorshift32 */
  unsigned x = *s;
  x ^= x << 13;
  x ^= x >> 17;
  x ^= x << 5;
  return *s = x;
}

/* ------------------------------------------------------------------ resamplers */
/* scipy.signal.resample_poly(x, 1, 2): y[n] = sum_j h[j] x[2n + 20 - j] */
static double down2(const double *x16_at_2n) {          /* pointer to x16[2n - 20] */
  double s = 0;
  for (int j = 0; j < NTAPS; j++) s += sotto_resample_taps[j] * x16_at_2n[NTAPS - 1 - j];
  return s;
}

/* resample_poly(x, 2, 1): y[2n + p] = 2 sum_k h[2k + p] x[n + 10 - k] */
static void up2(const double *x8_at_n, double *y) {      /* pointer to x8[n - 10] */
  double e = 0, o = 0;
  for (int k = 0; k <= 20; k++) e += sotto_resample_taps[2 * k] * x8_at_n[20 - k];
  for (int k = 0; k <= 19; k++) o += sotto_resample_taps[2 * k + 1] * x8_at_n[20 - k];
  y[0] = 2 * e;
  y[1] = 2 * o;
}

/* ================================================================== decoder */
#define DY8 4096                            /* 8 kHz samples kept (power of 2) */
#define DY16 8192
#define MRING 64                            /* model frames (10 ms) */
#define GRING 32                            /* codec frames */
#define TRING 32                            /* STFT frames between stage A and B */
#define ORING 8192                          /* output samples (OLA) */

typedef struct {
  int cls, mask, want;
  double p[4], vt[16], fade;
} tframe_t;

struct sotto_dec16 {
  struct sotto_dec *core;
  codec2_fftr_cfg fwd, inv;
  long n;                                   /* frames received */
  long nend;                                /* frames in all, once the stream ended, else -1 */
  double y8[DY8];                           /* aligned 8 kHz output, y8[j] at j & (DY8-1) */
  long y8n;                                 /* samples so far */
  double y16[DY16];
  long y16n;
  double env[MRING][NFB];                   /* per model frame k */
  int mcls[MRING];
  long kmax;                                /* model frames known: 0 .. kmax */
  int code[GRING], run[GRING];              /* per codec frame */
  double res[GRING][4];                     /* its coded 4-band hiss correction (unvoiced frames) */
  double pc[GRING][3];
  /* side channel reader (hiss_band._read_pc) */
  double cur[3];
  int got[MSG_BITS], ngot, ok;
  tframe_t tf[TRING];
  long ta, tb;                              /* stage A done for t < ta, stage B for t < tb */
  long last_true;                           /* last STFT frame with want = 1, -1 if none */
  double lv[16], wv;
  double out[ORING];                        /* y16 + hiss, out[j] at j & (ORING-1) */
  long outn;                                /* samples handed out */
  unsigned rng;
  /* sotto 1200 hiss (sotto_lr_dec16): the sotto 1200 decoder feeds the frames, and the
     20 ms blocks out queue up for its 40 ms calls */
  struct sotto_lr_dec *lr;
  short q[4 * N16];
  int qn;
};

struct sotto_dec16 *sotto_dec16_create(void) {
  struct sotto_dec16 *d = calloc(1, sizeof *d);
  if (!d) return NULL;
  init_tables();
  d->core = sotto_dec_create();
  d->fwd = codec2_fftr_alloc(HN, 0, NULL, NULL);
  d->inv = codec2_fftr_alloc(HN, 1, NULL, NULL);
  d->nend = -1;
  d->kmax = -1;
  d->last_true = -1;
  d->ok = 1;
  d->rng = 0x9E3779B9u;
  return d;
}

void sotto_dec16_destroy(struct sotto_dec16 *d) {
  if (!d) return;
  sotto_dec_destroy(d->core);
  codec2_fftr_free(d->fwd);
  codec2_fftr_free(d->inv);
  free(d);
}

/* y8 -> y16 for every 8 kHz sample that has its 10 samples of look-ahead */
static void dec_advance_y16(struct sotto_dec16 *d) {
  while (d->y16n / 2 + 10 < d->y8n) {
    long n = d->y16n / 2;
    double x[21];
    for (int i = 0; i < 21; i++) {
      long j = n - 10 + i;
      x[i] = j < 0 ? 0 : d->y8[j & (DY8 - 1)];
    }
    up2(x, &d->y16[d->y16n & (DY16 - 1)]);
    /* up2 wrote two consecutive slots; fix the wrap-around if the ring boundary split them */
    if (((d->y16n + 1) & (DY16 - 1)) == 0) {
      double tmp[2];
      up2(x, tmp);
      d->y16[(d->y16n + 1) & (DY16 - 1)] = tmp[1];
    }
    d->y16n += 2;
  }
}

static long dec_frame_of(const struct sotto_dec16 *d, long t) {
  long f = (t + HB_LAG) / 2;
  if (d->nend >= 0 && f > d->nend - 1) f = d->nend - 1;
  return f;
}

/* stage A for STFT frame t (hiss_band.decode_audio16 before the vowel layer): needs model
   frame t + 2, codec frames up to f + 1 and y16 up to 160 t + 320 */
static void dec_stage_a(struct sotto_dec16 *d, long t) {
  tframe_t *s = &d->tf[t % TRING];
  long k = t + HB_LAG, f = dec_frame_of(d, t);
  if (k > d->kmax) k = d->kmax;
  const double *E = d->env[k % MRING];
  int v = d->mcls[k % MRING];
  int code = d->code[f % GRING];
  long src = -1;
  long nlast = d->nend >= 0 ? d->nend - 1 : d->n - 1;
  if (code == 0) src = f;
  else if (v == 0) {
    if (f - 1 >= 0 && d->code[(f - 1) % GRING] == 0) src = f - 1;
    else if (f + 1 <= nlast && d->code[(f + 1) % GRING] == 0) src = f + 1;
  }
  guess(v, E, s->p);
  s->mask = 0;
  if (src >= 0) {
    for (int b = 0; b < 4; b++) s->p[b] += d->res[src % GRING][b];
    s->mask = 1;
  } else if (v == 2) {
    for (int b = 0; b < 4; b++) s->p[b] += MIXED_DB;
    s->mask = 3;
  }
  int run = d->run[f % GRING];
  s->fade = run == 0 ? 0.0 : run > 4 ? -80.0 : -6.0 * (run - 2 > 0 ? run - 2 : 0);
  for (int b = 0; b < 4; b++) s->p[b] += s->fade + (s->mask == 1 ? UV_GAIN : 0.0);
  s->cls = v;
  s->want = (v == 1 || v == 2) && s->mask != 1;
  /* vowel layer level: the predictor on the decoded 0-4 kHz plus the speaker shape */
  double x[HN], P[NBIN], pr[16], ref;
  for (int i = 0; i < HN; i++) x[i] = d->y16[(HH * t + i) & (DY16 - 1)];
  stft_power(d->fwd, x, P);
  fine_predict(P, pr, &ref);
  const double *pc = d->pc[f % GRING];
  for (int b = 0; b < 16; b++) {
    double corr = sotto_hiss_fine_m0[b];
    for (int c = 0; c < 3; c++) corr += pc[c] * sotto_hiss_fine_vt[c * 16 + b];
    s->vt[b] = pr[b] + ref + corr;
  }
}

/* hiss_band.decode_audio16, blend: want with flickers bridged (needs stage A up to t + 4) */
static int dec_want_bridged(struct sotto_dec16 *d, long t) {
  tframe_t *s = &d->tf[t % TRING];
  if (s->want) return 1;
  long g0 = d->last_true + 1;
  if (d->last_true < 0) return 0;            /* a gap from the start is not bridged */
  for (long j = t + 1; j <= g0 + BRIDGE; j++) {
    if (j >= d->ta) return 0;                /* reaches the end (or beyond what is known) */
    if (d->tf[j % TRING].want) return 1;
  }
  return 0;
}

/* stage B: the two hiss layers of frame t, blended, into the output overlap-add */
static void dec_stage_b(struct sotto_dec16 *d, long t) {
  tframe_t *s = &d->tf[t % TRING];
  int want = dec_want_bridged(d, t);
  if (s->want) d->last_true = t;
  double a = exp(-10.0 / SMOOTH_MS);
  if (t == 0)
    memcpy(d->lv, s->vt, sizeof d->lv);
  else
    for (int b = 0; b < 16; b++) d->lv[b] = a * d->lv[b] + (1 - a) * s->vt[b];
  d->wv = a * d->wv + (1 - a) * want;
  double lvo[16];
  for (int b = 0; b < 16; b++) lvo[b] = d->lv[b] + s->fade + VOICED_TRIM;
  COMP X[NBIN];
  for (int k = 0; k < NBIN; k++) {
    double m1 = sqrt(pow(10, interp(fr_hz[k], hbc, s->p, 4) / 10)) * ramp[k] * s->mask;
    double m2 = sqrt(pow(10, interp(fr_hz[k], fhc, lvo, 16) / 10)) * ramp[k] * d->wv;
    double m = sqrt(fmax(m1 * m1, m2 * m2));
    double ph = 2 * PI_D * (rng_next(&d->rng) / 4294967296.0);
    X[k].real = (float)(m * cos(ph));
    X[k].imag = (float)(m * sin(ph));
  }
  float y[HN];
  codec2_fftri(d->inv, X, y);
  for (int i = 0; i < HN; i++) {
    long j = HH * t + i;
    d->out[j & (ORING - 1)] += y[i] / HN * win[i];
  }
}

/* run stages A and B as far as the inputs allow */
static void dec_run(struct sotto_dec16 *d) {
  dec_advance_y16(d);
  long nlast = d->nend >= 0 ? d->nend - 1 : d->n - 1;    /* last codec frame received */
  long tmax = d->nend >= 0 ? 2 * d->nend - 2 : LONG_MAX;  /* nfr - 1 (LONG_MAX: long is 32 bits on wasm32) */
  for (;;) {
    long t = d->ta, k = t + HB_LAG, f = dec_frame_of(d, t);
    if (t > tmax) break;
    if (HH * t + HN > d->y16n) break;
    if (d->nend < 0 && (k > d->kmax || f + 1 > nlast)) break;
    if (d->nend >= 0 && k > d->kmax) k = d->kmax;
    if (k > d->kmax) break;
    /* the output slots this frame will add to must be clear: the OLA ring is zeroed as it is read */
    dec_stage_a(d, t);
    d->ta++;
  }
  for (;;) {
    long t = d->tb;
    if (t >= d->ta) break;
    if (d->nend < 0 && t + BRIDGE >= d->ta) break;
    dec_stage_b(d, t);
    d->tb++;
  }
}

/* y16 + hiss for samples [outn, outn + 320) */
static void dec_output(struct sotto_dec16 *d, short *speech) {
  for (int i = 0; i < N16; i++) {
    long j = d->outn + i;
    double v = 0;
    if (j >= 0) {
      v = d->out[j & (ORING - 1)] + (j < d->y16n ? d->y16[j & (DY16 - 1)] : 0.0);
      d->out[j & (ORING - 1)] = 0;
    }
    v *= 32768.0;
    speech[i] = v > 32767 ? 32767 : v < -32768 ? -32768 : (short)lrint(v);
  }
  d->outn += N16;
}

/* one 20 ms frame through the core: its 160 samples y8 (frame n - 1 synthesised), the core's
   envelope and classes of those halves, frame n's voicing code, hiss correction and
   concealment run, and its side-channel bit (read when the frame is unvoiced and arrived:
   hiss_band._read_pc). Then the hiss stages, and one 20 ms block out once past the delay. */
static void dec_push(struct sotto_dec16 *d, const short *y8, const sotto_dec_info_t *info, int code,
                     const double *res, int run, int side, int sgood, short *speech) {
  long n = d->n;
  if (n >= ALIGN_CALLS)
    for (int i = 0; i < SOTTO_SAMPLES_PER_FRAME; i++) d->y8[d->y8n++ & (DY8 - 1)] = y8[i] / 32768.0;
  if (n >= 1) {                              /* frame n - 1 was synthesised */
    for (int h = 0; h < 2; h++) {
      long k = 2 * (n - 1) + h;
      memcpy(d->env[k % MRING], info->env[h], sizeof info->env[h]);
      d->mcls[k % MRING] = info->cls[h];
      d->kmax = k;
    }
  }
  d->code[n % GRING] = code;
  memcpy(d->res[n % GRING], res, sizeof d->res[0]);
  d->run[n % GRING] = run;
  if (n % WIN_FRAMES == 0) d->ngot = 0, d->ok = 1;
  memcpy(d->pc[n % GRING], d->cur, sizeof d->cur);
  if (d->ok && d->ngot < MSG_BITS) {
    if (!sgood) d->ok = 0;
    else if (code == 0) {
      d->got[d->ngot++] = side;
      if (d->ngot == MSG_BITS && unpack_pc(d->got, d->cur)) memcpy(d->pc[n % GRING], d->cur, sizeof d->cur);
    }
  }
  d->n++;
  dec_run(d);
  if (d->n > SOTTO_DEC16_DELAY_FRAMES) dec_output(d, speech);
  else memset(speech, 0, N16 * sizeof(short));
}

/* the core's last output (frame n - 1): the stream has ended */
static void dec_end(struct sotto_dec16 *d, const short *y8, const sotto_dec_info_t *info) {
  if (d->n >= ALIGN_CALLS)
    for (int i = 0; i < SOTTO_SAMPLES_PER_FRAME; i++) d->y8[d->y8n++ & (DY8 - 1)] = y8[i] / 32768.0;
  for (int h = 0; h < 2; h++) {
    long k = 2 * (d->n - 1) + h;
    memcpy(d->env[k % MRING], info->env[h], sizeof info->env[h]);
    d->mcls[k % MRING] = info->cls[h];
    d->kmax = k;
  }
  d->nend = d->n;
  while (d->y8n < 160 * d->nend + 10) d->y8[d->y8n++ & (DY8 - 1)] = 0;   /* zero padding, as the Python */
  dec_run(d);
}

/* one received frame: core decode, bookkeeping, hiss stages, one 20 ms block out */
void sotto_decode16(struct sotto_dec16 *d, const unsigned char in[SOTTO_BYTES_PER_FRAME], int good,
                    short speech[SOTTO_SAMPLES_PER_FRAME_16K]) {
  short y8[SOTTO_SAMPLES_PER_FRAME];
  sotto_decode(d->core, in, good, y8);
  const sotto_dec_info_t *info = sotto_dec_info(d->core);
  double res[4];
  for (int b = 0; b < 4; b++) res[b] = sotto_hiss_cb6[index6(info->b2) * 4 + b];
  dec_push(d, y8, info, frame_code(info->b2), res, info->run, frame_bit(in, 8), good, speech);
}

/* the end of the stream: returns 1 and 320 samples while output remains */
int sotto_dec16_flush(struct sotto_dec16 *d, short speech[SOTTO_SAMPLES_PER_FRAME_16K]) {
  if (d->n == 0) return 0;
  if (d->nend < 0) {
    short y8[SOTTO_SAMPLES_PER_FRAME];
    sotto_dec_flush(d->core, y8);
    dec_end(d, y8, sotto_dec_info(d->core));
  }
  if (d->outn >= 320 * d->nend) return 0;
  dec_output(d, speech);
  return 1;
}

/* ================================================================== sotto 1200 hiss decoder */
struct sotto_lr_dec16 {
  struct sotto_dec16 h;
};

static void lr_hook(void *ctx, int code, const double *res, int side, int sgood, int run, const short *out,
                    int flushed) {
  struct sotto_dec16 *d = ctx;
  const sotto_dec_info_t *info = sotto_dec_info(sotto_lr_dec_core(d->lr));
  if (flushed) {
    dec_end(d, out, info);
    return;
  }
  short blk[N16];
  long n = d->n;
  dec_push(d, out, info, code, res, run, side, sgood, blk);
  if (n + 1 > SOTTO_DEC16_DELAY_FRAMES) memcpy(d->q + N16 * d->qn++, blk, sizeof blk);
}

struct sotto_lr_dec16 *sotto_lr_dec16_create(void) {
  struct sotto_lr_dec16 *L = calloc(1, sizeof *L);
  if (!L) return NULL;
  struct sotto_dec16 *d = &L->h;
  init_tables();
  d->lr = sotto_lr_dec_create();
  d->fwd = codec2_fftr_alloc(HN, 0, NULL, NULL);
  d->inv = codec2_fftr_alloc(HN, 1, NULL, NULL);
  if (!d->lr || !d->fwd || !d->inv) {
    sotto_lr_dec16_destroy(L);
    return NULL;
  }
  sotto_lr_dec_set_hook(d->lr, lr_hook, d);
  d->nend = -1;
  d->kmax = -1;
  d->last_true = -1;
  d->ok = 1;
  d->rng = 0x9E3779B9u;
  return L;
}

void sotto_lr_dec16_destroy(struct sotto_lr_dec16 *L) {
  if (!L) return;
  sotto_lr_dec_destroy(L->h.lr);
  codec2_fftr_free(L->h.fwd);
  codec2_fftr_free(L->h.inv);
  free(L);
}

/* 640 samples from the queue (two 20 ms blocks), or silence while it fills */
static int lr_pop(struct sotto_dec16 *d, short *speech) {
  if (d->qn < 2) {
    memset(speech, 0, 2 * N16 * sizeof(short));
    return 0;
  }
  memcpy(speech, d->q, 2 * N16 * sizeof(short));
  memmove(d->q, d->q + 2 * N16, (size_t)(d->qn - 2) * N16 * sizeof(short));
  d->qn -= 2;
  return 1;
}

void sotto_lr_decode16(struct sotto_lr_dec16 *L, const unsigned char in[SOTTO_BYTES_PER_FRAME], int good,
                       short speech[SOTTO_LR_SAMPLES_PER_FRAME_16K]) {
  struct sotto_dec16 *d = &L->h;
  short y[SOTTO_SAMPLES_PER_FRAME];
  sotto_lr_decode_half(d->lr, in, good, 0, y);
  sotto_lr_decode_half(d->lr, NULL, 0, 1, y);
  lr_pop(d, speech);
}

int sotto_lr_dec16_flush(struct sotto_lr_dec16 *L, short speech[SOTTO_LR_SAMPLES_PER_FRAME_16K]) {
  struct sotto_dec16 *d = &L->h;
  if (d->n == 0) return 0;
  short y[SOTTO_SAMPLES_PER_FRAME];
  while (d->nend < 0 && d->qn < 2) {           /* the sotto 1200 decoder's last frames */
    if (!sotto_lr_dec_flush_half(d->lr, 0, y)) break;
    sotto_lr_dec_flush_half(d->lr, 1, y);
  }
  while (d->nend >= 0 && d->qn < 2 && d->outn < 320 * d->nend) dec_output(d, d->q + N16 * d->qn++);
  if (!d->qn) return 0;
  if (d->qn == 1) memset(d->q + N16, 0, N16 * sizeof(short)), d->qn = 2;
  return lr_pop(d, speech);
}

/* ================================================================== encoder */
#define EX16 8192                           /* 16 kHz input samples kept */
#define EX8 2048
#define OUTQ 64

typedef struct {                            /* features of one STFT frame of the input */
  double top, low, one, b2530, tot, hb[4], Y[16];
  int vow;
} sframe_t;

typedef struct {                            /* one core frame in the rule ring */
  sotto_enc_frame_t raw;
  int code;                                 /* after the rules */
  unsigned char bits[SOTTO_BYTES_PER_FRAME];
} rframe_t;

struct sotto_enc16 {
  struct sotto_enc *core;
  struct sotto_dec *ldec;                   /* the encoder's own decoder (hiss_band._low16) */
  codec2_fftr_cfg fwd;
  long ncall;                               /* 320-sample calls so far */
  long nend;                                /* frames in all once the input ended, else -1 */
  double x16[EX16];                         /* input, short units */
  long x16n;
  long nblk;                                /* 10 ms blocks analysed */
  sframe_t sf[SRING];
  long nst;                                 /* input STFT frames computed */
  double pct[PCT_MAX];                      /* sorted frame levels for the running percentile */
  int npct;
  rframe_t rf[FRING];
  long nraw;                                /* core frames in the ring: 0 .. nraw - 1 */
  long nfinal;                              /* frames with final voicing: 0 .. nfinal - 1 */
  long nfed;                                /* frames fed to the local decoder */
  long nemit;                               /* frames finished and queued */
  /* local decoder streams */
  double ly8[EX8];
  long ly8n;
  double ly16[EX16];
  long ly16n;
  double lenv[MRING][NFB];
  int lcls[MRING];
  long lkmax;
  long lnst;                                /* local STFT frames done (pr) */
  double pr[SRING][16];
  double acc[16];
  int cnt;
  long tacc;                                /* STFT frames accumulated: 0 .. tacc - 1 */
  int msg[MSG_BITS], msgpos;
  unsigned char outq[OUTQ][SOTTO_BYTES_PER_FRAME];
  int qh, qn;
};

static void enc_init(struct sotto_enc16 *e) {
  init_tables();
  e->core = sotto_enc_create();
  e->ldec = sotto_dec_create();
  e->fwd = codec2_fftr_alloc(HN, 0, NULL, NULL);
  e->nend = -1;
  e->lkmax = -1;
  e->msgpos = MSG_BITS;
}

struct sotto_enc16 *sotto_enc16_create(void) {
  struct sotto_enc16 *e = calloc(1, sizeof *e);
  if (!e) return NULL;
  enc_init(e);
  return e;
}

void sotto_enc16_destroy(struct sotto_enc16 *e) {
  if (!e) return;
  sotto_enc_destroy(e->core);
  sotto_dec_destroy(e->ldec);
  codec2_fftr_free(e->fwd);
  free(e);
}

/* np.percentile(v, 60) of the levels so far, then insert the new one */
static int enc_above_pct(struct sotto_enc16 *e, double v) {
  int above = 0;
  if (e->npct) {
    int n = e->npct;
    /* the percentile includes the current frame (v[:t+1]) */
    double pos = PCT_Q / 100 * n;             /* over n + 1 values: 0.6 * ((n+1) - 1) */
    int i = (int)floor(pos);
    double frac = pos - i;
    /* value at sorted rank r among the n stored plus v */
    double a[2];
    for (int r = 0; r < 2; r++) {
      int rank = i + r;
      if (rank > n) rank = n;
      /* rank-th smallest of the merged set */
      int below = 0;                        /* stored values < v */
      while (below < n && e->pct[below] < v) below++;
      a[r] = rank < below ? e->pct[rank] : rank == below ? v : e->pct[rank - 1];
    }
    double q = i + 1 <= n ? a[0] + frac * (a[1] - a[0]) : a[0];
    above = v > q;
  } else {
    above = 0;                               /* v > percentile of {v} is false */
  }
  if (e->npct < PCT_MAX) {
    int i = e->npct;
    while (i > 0 && e->pct[i - 1] > v) e->pct[i] = e->pct[i - 1], i--;
    e->pct[i] = v;
    e->npct++;
  }
  return above;
}

/* features of input STFT frame t (needs x16 up to 160 t + 320) */
static void enc_input_stft(struct sotto_enc16 *e, long t) {
  sframe_t *s = &e->sf[t % SRING];
  double x[HN], P[NBIN];
  for (int i = 0; i < HN; i++) x[i] = e->x16[(HH * t + i) & (EX16 - 1)] / 32767.0;
  stft_power(e->fwd, x, P);
  s->top = band_db(P, 4000, 8000);
  s->low = band_db(P, 0, 3900);
  s->one = band_db(P, 0, 1000);
  s->b2530 = band_db(P, 2500, 3000);
  s->tot = total_db(P);
  for (int b = 0; b < 4; b++) s->hb[b] = band_db(P, 4000 + 1000 * b, 5000 + 1000 * b);
  double ref = band_db(P, 3000, 3500);
  for (int b = 0; b < 16; b++) s->Y[b] = band_db(P, 4000 + 250 * b, 4250 + 250 * b) - ref;
  int above = enc_above_pct(e, s->tot);
  s->vow = (s->one - s->b2530 > 10) && t >= PCT_WARM && above;
}

/* ---- the rules over the ring (hiss_band.encode_low) ---- */
static long enc_frame_of(const struct sotto_enc16 *e, long t) {
  long f = (t + HB_LAG) / 2;
  if (e->nend >= 0 && f > e->nend - 1) f = e->nend - 1;
  return f;
}

static double median(double *v, int n) {
  for (int i = 1; i < n; i++) {             /* insertion sort, n is small */
    double x = v[i];
    int j = i;
    while (j > 0 && v[j - 1] > x) v[j] = v[j - 1], j--;
    v[j] = x;
  }
  return n % 2 ? v[n / 2] : 0.5 * (v[n / 2 - 1] + v[n / 2]);
}

static void enc_rules(struct sotto_enc16 *e) {
  long g0 = e->nraw > FRING ? e->nraw - FRING : 0, g1 = e->nraw;   /* frames [g0, g1) */
  if (g1 == g0) return;
  int n = (int)(g1 - g0);
  static int code[FRING], c0[FRING];
  rframe_t *rf = e->rf;
#define RF(g) (&rf[(g) % FRING])
  for (int i = 0; i < n; i++) {
    rframe_t *r = RF(g0 + i);
    memcpy(r->bits, r->raw.bits, SOTTO_BYTES_PER_FRAME);
    code[i] = c0[i] = frame_code(r->raw.bits);
  }
  /* _onsets: a loud, low-heavy unvoiced frame next to a voiced or mixed one -> MM with its pitch */
  for (int i = 0; i < n; i++) {
    const sotto_enc_frame_t *f = &RF(g0 + i)->raw;
    if (c0[i] || !f->loud[0] || f->tilt[0] <= ONSET_LOWDOM) continue;
    static const int dd[6] = {1, -1, 2, -2, 3, -3};
    for (int j = 0; j < 6; j++) {
      int h = i + dd[j];
      if (h >= 0 && h < n && (c0[h] == 1 || c0[h] == 2)) {
        unsigned char *b = RF(g0 + i)->bits;
        const unsigned char *nb = RF(g0 + h)->raw.bits;
        code[i] = 2;
        for (int p = 2; p < 9; p++) set_bit(b, p, frame_bit(nb, p));
        break;
      }
    }
  }
  /* _whisper (lag = SOTTO_RULE_LAG): runs of >= 10 mixed/edge frames with a weak, wandering pitch */
  for (int i = 0; i < n;) {
    if (code[i] != 2 && code[i] != 3) {
      i++;
      continue;
    }
    int h = i;
    while (h < n && (code[h] == 2 || code[h] == 3)) h++;
    if (h - i >= WHISPER_MIN) {
      int hj = h < i + SOTTO_RULE_LAG + 1 ? h : i + SOTTO_RULE_LAG + 1;
      double sv[2 * (SOTTO_RULE_LAG + 1)], jv[2 * (SOTTO_RULE_LAG + 1)];
      int ns = 0, nj = 0;
      for (int g = i; g < hj; g++) {
        const sotto_enc_frame_t *f = &RF(g0 + g)->raw;
        for (int half = 0; half < 2; half++) {
          if (!isnan(f->snr[half])) sv[ns++] = f->snr[half];
          double prev = half ? f->lwo[0] : (g0 + g > 0 ? RF(g0 + g - 1)->raw.lwo[1] : f->lwo[0]);
          jv[nj++] = g0 + g == 0 && half == 0 ? 0.0 : fabs(f->lwo[half] - prev);
        }
      }
      if (ns && median(sv, ns) < WHISPER_SNR && median(jv, nj) > WHISPER_JIT)
        for (int g = i; g < h; g++) code[g] = 0;
    }
    i = h;
  }
  /* _vsmooth: edge blips inside voiced runs -> M; V/M flutter -> M */
  for (int i = 0; i < n;) {
    if (code[i] != 3) {
      i++;
      continue;
    }
    int h = i;
    while (h < n && code[h] == 3) h++;
    if (i > 0 && h < n && h - i <= 2 && (code[i - 1] == 1 || code[i - 1] == 2) && (code[h] == 1 || code[h] == 2))
      for (int g = i; g < h; g++) code[g] = 2;
    i = h;
  }
  memcpy(c0, code, n * sizeof(int));
  for (int i = 0; i < n; i++) {
    if (c0[i] != 1 && c0[i] != 2) continue;
    int w[7], nw = 0, sw = 0;
    for (int j = i - 3 < 0 ? 0 : i - 3; j <= i + 3 && j < n; j++)
      if (c0[j] > 0) w[nw++] = c0[j];
    for (int j = 1; j < nw; j++) sw += w[j] != w[j - 1] && w[j] < 3 && w[j - 1] < 3;
    if (sw >= 3) code[i] = 2;
  }
  /* burst and sibilant rules on the input STFT frames of the recent frames */
  long t0 = e->nst > SRING ? e->nst - SRING : 0;
  for (long t = t0 < 1 ? 1 : t0; t < e->nst; t++) {
    long g = enc_frame_of(e, t);
    if (g < g0 || g >= g1) continue;
    const sframe_t *s = &e->sf[t % SRING], *q = &e->sf[(t - 1) % SRING];
    if (s->top - q->top > BURST_DB && s->top > s->low - 15) code[g - g0] = 0;
  }
  for (long t = t0; t < e->nst; t++) {
    long g = enc_frame_of(e, t);
    if (g < g0 || g >= g1) continue;
    const sframe_t *s = &e->sf[t % SRING];
    if (s->top - s->one > SIB_DB && code[g - g0] != 0) code[g - g0] = 0;
  }
  for (int i = 0; i < n; i++) {
    rframe_t *r = RF(g0 + i);
    r->code = code[i];
    set_bit(r->bits, 0, code[i] >> 1);
    set_bit(r->bits, 1, code[i] & 1);
  }
#undef RF
}

/* feed frame g (final voicing) to the local decoder: its output is frame g - 1 */
static void enc_feed_local(struct sotto_enc16 *e, long g) {
  short y8[SOTTO_SAMPLES_PER_FRAME];
  sotto_decode(e->ldec, e->rf[g % FRING].bits, 1, y8);
  const sotto_dec_info_t *info = sotto_dec_info(e->ldec);
  if (g >= ALIGN_CALLS)
    for (int i = 0; i < SOTTO_SAMPLES_PER_FRAME; i++) e->ly8[e->ly8n++ & (EX8 - 1)] = y8[i] / 32768.0;
  if (g >= 1)
    for (int h = 0; h < 2; h++) {
      long k = 2 * (g - 1) + h;
      memcpy(e->lenv[k % MRING], info->env[h], sizeof info->env[h]);
      e->lcls[k % MRING] = info->cls[h];
      e->lkmax = k;
    }
}

/* the local decoder's 16 kHz output and its STFT predictor, as far as possible */
static void enc_advance_local(struct sotto_enc16 *e) {
  while (e->ly16n / 2 + 10 < e->ly8n) {
    long n = e->ly16n / 2;
    double x[21], y[2];
    for (int i = 0; i < 21; i++) {
      long j = n - 10 + i;
      x[i] = j < 0 ? 0 : e->ly8[j & (EX8 - 1)];
    }
    up2(x, y);
    e->ly16[e->ly16n & (EX16 - 1)] = y[0];
    e->ly16[(e->ly16n + 1) & (EX16 - 1)] = y[1];
    e->ly16n += 2;
  }
  while (HH * e->lnst + HN <= e->ly16n) {
    double x[HN], P[NBIN], ref;
    for (int i = 0; i < HN; i++) x[i] = e->ly16[(HH * e->lnst + i) & (EX16 - 1)];
    stft_power(e->fwd, x, P);
    fine_predict(P, e->pr[e->lnst % SRING], &ref);
    e->lnst++;
  }
}

/* hiss_band._side_pc: add the vowel frames up to STFT frame tmax to the speaker-shape mean */
static void enc_accumulate(struct sotto_enc16 *e, long tmax) {
  while (e->tacc <= tmax && e->tacc < e->lnst && e->tacc < e->nst) {
    long t = e->tacc++;
    const sframe_t *s = &e->sf[t % SRING];
    if (!s->vow) continue;
    for (int b = 0; b < 16; b++) e->acc[b] += s->Y[b] - e->pr[t % SRING][b];
    e->cnt++;
  }
}

/* hiss_band._side_pc for frame g (frames in order) of voicing code: its side-channel bit,
   or -1 when it carries none */
static int enc_side(struct sotto_enc16 *e, long g, int code) {
  enc_accumulate(e, 2 * g - 3);
  if (g % WIN_FRAMES == 0) {                 /* a new window: its message from the frames before it */
    double pc[3] = {0, 0, 0};
    if (e->cnt >= SIDE_MIN_FRAMES)
      for (int c = 0; c < 3; c++) {
        double s = 0;
        for (int b = 0; b < 16; b++) s += sotto_hiss_fine_vt[c * 16 + b] * (e->acc[b] / e->cnt - sotto_hiss_fine_m0[b]);
        pc[c] = s;
      }
    pack_pc(pc, e->msg);
    e->msgpos = 0;
  }
  return code == 0 && e->msgpos < MSG_BITS ? e->msg[e->msgpos++] : -1;
}

/* hiss_band.uu_residuals for frame g: the mean (input - guess) over its STFT frames, up to
   STFT frame tlast; returns how many */
static int enc_residual(const struct sotto_enc16 *e, long g, long tlast, double *res) {
  int nt = 0;
  for (int b = 0; b < 4; b++) res[b] = 0;
  for (long t = 2 * g - 2; t <= tlast; t++) {
    if (t < 0 || t >= e->nst) continue;
    long k = t + HB_LAG;
    if (k > e->lkmax) k = e->lkmax;
    double p[4];
    guess(0, e->lenv[k % MRING], p);
    for (int b = 0; b < 4; b++) res[b] += e->sf[t % SRING].hb[b] - p[b];
    nt++;
  }
  for (int b = 0; nt && b < 4; b++) res[b] /= nt;
  return nt;
}

/* the nearest of n 4-band codewords */
static int nearest4(const double *cb, int n, const double *base, const double *x) {
  int best = 0;
  double bd = 1e300;
  for (int i = 0; i < n; i++) {
    double d = 0;
    for (int b = 0; b < 4; b++) {
      double v = base[b] + cb[i * 4 + b] - x[b];
      d += v * v;
    }
    if (d < bd) bd = d, best = i;
  }
  return best;
}

static const double zero4[4] = {0, 0, 0, 0};

/* finish frame g: the 6-bit index of an all-unvoiced frame and its side-channel bit */
static void enc_finish(struct sotto_enc16 *e, long g) {
  rframe_t *r = &e->rf[g % FRING];
  int side = enc_side(e, g, r->code);
  if (r->code == 0) {
    double res[4];
    int best = enc_residual(e, g, 2 * g - 1, res) ? nearest4(sotto_hiss_cb6, 64, zero4, res) : 0;
    for (int i = 0; i < 6; i++) set_bit(r->bits, 2 + i, (best >> (5 - i)) & 1);
    if (side >= 0) set_bit(r->bits, 8, side);
  }
  memcpy(e->outq[(e->qh + e->qn++) % OUTQ], r->bits, SOTTO_BYTES_PER_FRAME);
}

/* pull core frames, apply the rules, feed the local decoder, finish what can be finished */
static void enc_run(struct sotto_enc16 *e) {
  sotto_enc_frame_t f;
  while (sotto_enc_pop_frame(e->core, &f)) {
    rframe_t *r = &e->rf[e->nraw % FRING];
    r->raw = f;
    e->nraw++;
  }
  enc_rules(e);
  long nfinal = e->nend >= 0 ? e->nraw : (e->nraw - SOTTO_RULE_LAG > 0 ? e->nraw - SOTTO_RULE_LAG : 0);
  while (e->nfed < nfinal) enc_feed_local(e, e->nfed++);
  if (e->nend >= 0 && e->nfed == e->nend && e->ly8n < 160 * e->nend + 10) {
    short y8[SOTTO_SAMPLES_PER_FRAME];
    sotto_dec_flush(e->ldec, y8);
    if (e->nend >= ALIGN_CALLS)
      for (int i = 0; i < SOTTO_SAMPLES_PER_FRAME; i++) e->ly8[e->ly8n++ & (EX8 - 1)] = y8[i] / 32768.0;
    const sotto_dec_info_t *info = sotto_dec_info(e->ldec);
    for (int h = 0; h < 2; h++) {
      long k = 2 * (e->nend - 1) + h;
      memcpy(e->lenv[k % MRING], info->env[h], sizeof info->env[h]);
      e->lcls[k % MRING] = info->cls[h];
      e->lkmax = k;
    }
    while (e->ly8n < 160 * e->nend + 10) e->ly8[e->ly8n++ & (EX8 - 1)] = 0;
  }
  enc_advance_local(e);
  e->nfinal = nfinal;
  /* a frame is finished one frame after its voicing: its index needs its own decoded
     envelope (synthesised when the next frame was fed), the side channel the local
     output up to the window boundary */
  long nfinish = e->nend >= 0 ? e->nend : (nfinal - 1 > 0 ? nfinal - 1 : 0);
  while (e->nemit < nfinish && e->qn < OUTQ) enc_finish(e, e->nemit++);
}

static int enc_pop(struct sotto_enc16 *e, unsigned char *out) {
  if (!e->qn) return 0;
  memcpy(out, e->outq[e->qh], SOTTO_BYTES_PER_FRAME);
  e->qh = (e->qh + 1) % OUTQ;
  e->qn--;
  return 1;
}

/* decimate and analyse every 10 ms block whose 20 input samples of look-ahead are in */
static void enc_analyse(struct sotto_enc16 *e) {
  for (;;) {
    long n0 = 80 * e->nblk;
    if (2 * (n0 + 79) + 20 >= e->x16n) break;
    short x8[80];
    for (int i = 0; i < 80; i++) {
      double x[NTAPS];
      long m = 2 * (n0 + i) - 20;
      for (int j = 0; j < NTAPS; j++) x[j] = m + j < 0 ? 0 : e->x16[(m + j) & (EX16 - 1)];
      double v = rint(down2(x));
      x8[i] = v > 32767 ? 32767 : v < -32768 ? -32768 : (short)v;
    }
    sotto_enc_analyse_block(e->core, x8);
    e->nblk++;
  }
  while (HH * e->nst + HN <= e->x16n) enc_input_stft(e, e->nst++);
}

int sotto_encode16(struct sotto_enc16 *e, const short speech[SOTTO_SAMPLES_PER_FRAME_16K],
                   unsigned char out[SOTTO_BYTES_PER_FRAME]) {
  for (int i = 0; i < N16; i++) e->x16[e->x16n++ & (EX16 - 1)] = speech[i];
  e->ncall++;
  enc_analyse(e);
  enc_run(e);
  return enc_pop(e, out);
}

/* the input has ended: analyse its tail, flush the core, take its last frames */
static void enc_end_input(struct sotto_enc16 *e) {
  long n16 = e->x16n;
  for (int i = 0; i < 40; i++) e->x16[e->x16n++ & (EX16 - 1)] = 0;   /* the resampler's tail */
  for (;;) {                                 /* analyse the remaining blocks of the input itself */
    long n0 = 80 * e->nblk;
    if (n0 + 80 > n16 / 2) break;
    short x8[80];
    for (int i = 0; i < 80; i++) {
      double x[NTAPS];
      long m = 2 * (n0 + i) - 20;
      for (int j = 0; j < NTAPS; j++) x[j] = m + j < 0 || m + j >= n16 ? 0 : e->x16[(m + j) & (EX16 - 1)];
      double v = rint(down2(x));
      x8[i] = v > 32767 ? 32767 : v < -32768 ? -32768 : (short)v;
    }
    sotto_enc_analyse_block(e->core, x8);
    e->nblk++;
  }
  e->x16n = n16;
  sotto_enc_flush_core(e->core);
  sotto_enc_frame_t f;
  while (sotto_enc_pop_frame(e->core, &f)) e->rf[e->nraw++ % FRING].raw = f;
  e->nend = e->nraw;
}

int sotto_enc16_flush(struct sotto_enc16 *e, unsigned char out[SOTTO_BYTES_PER_FRAME]) {
  if (e->nend < 0) {
    enc_end_input(e);
    enc_run(e);
  }
  return enc_pop(e, out);
}

/* ================================================================== sotto 1200 hiss encoder */
/* Sotto1200Hiss.encode: v3.8's front end (core, input features, voicing rules) feeds its
   final 20 ms frames to sotto 1200's coder; the superframes go through a local decoder (the
   v3.7 core on the frames sotto 1200's decoder rebuilds), whose envelope gives the guess and
   whose output the side channel's predictor, as in v3.8. Superframe s is finished once A(s + 1)
   can be quantised: A's 6-bit index, B's 2-bit level over the mean of the unvoiced anchors
   A(s) and A(s + 1), and a side-channel bit in every unvoiced frame. */
#define LSQ 16                              /* superframes coded but not finished */

struct sotto_lr_enc16 {
  struct sotto_enc16 h;                     /* front end, local streams, side channel, output queue */
  struct sotto_lr_enc *lr;                  /* sotto 1200's coder */
  sotto_fparams_t fp[FRING];                /* frames after the rules */
  long npushed;                             /* frames given to the coder */
  unsigned char sf[LSQ][SOTTO_BYTES_PER_FRAME];   /* plain sotto 1200 superframes */
  long nsf, nloc, ndone;                    /* superframes coded, decoded locally, finished */
  long nsf_end;                             /* superframes in all once the input ended, else -1 */
  int lflushed;                             /* local decoder flushed */
  double qa[LSQ][4];                        /* A(s)'s quantised hiss when unvoiced */
  int ia[LSQ];
};

struct sotto_lr_enc16 *sotto_lr_enc16_create(void) {
  struct sotto_lr_enc16 *L = calloc(1, sizeof *L);
  if (!L) return NULL;
  enc_init(&L->h);
  L->lr = sotto_lr_enc_create_frames();
  if (!L->h.core || !L->h.ldec || !L->h.fwd || !L->lr) {
    sotto_lr_enc16_destroy(L);
    return NULL;
  }
  L->nsf_end = -1;
  return L;
}

void sotto_lr_enc16_destroy(struct sotto_lr_enc16 *L) {
  if (!L) return;
  sotto_enc_destroy(L->h.core);
  sotto_dec_destroy(L->h.ldec);
  codec2_fftr_free(L->h.fwd);
  sotto_lr_enc_destroy(L->lr);
  free(L);
}

static void lr_take(struct sotto_lr_enc16 *L) {
  unsigned char b[SOTTO_BYTES_PER_FRAME];
  while (sotto_lr_enc_pop(L->lr, b)) memcpy(L->sf[L->nsf++ % LSQ], b, sizeof b);
}

/* one 20 ms frame into the local decoder (its output is the frame before) */
static void lr_feed_local(struct sotto_enc16 *e, const sotto_fparams_t *p) {
  long g = e->nfed++;
  short y8[SOTTO_SAMPLES_PER_FRAME];
  sotto_dec_frame(e->ldec, p, 0, y8);
  const sotto_dec_info_t *info = sotto_dec_info(e->ldec);
  if (g >= ALIGN_CALLS)
    for (int i = 0; i < SOTTO_SAMPLES_PER_FRAME; i++) e->ly8[e->ly8n++ & (EX8 - 1)] = y8[i] / 32768.0;
  if (g >= 1)
    for (int h = 0; h < 2; h++) {
      long k = 2 * (g - 1) + h;
      memcpy(e->lenv[k % MRING], info->env[h], sizeof info->env[h]);
      e->lcls[k % MRING] = info->cls[h];
      e->lkmax = k;
    }
}

/* A(s)'s 6-bit index and quantised hiss (unvoiced A frames) */
static void lr_a_index(struct sotto_lr_enc16 *L, long s) {
  long g = 2 * s;
  L->ia[s % LSQ] = -1;
  if (L->fp[g % FRING].code) return;
  double res[4];
  int i = enc_residual(&L->h, g, 2 * g - 1, res) ? nearest4(sotto_hiss_cb6, 64, zero4, res) : 0;
  L->ia[s % LSQ] = i;
  memcpy(L->qa[s % LSQ], sotto_hiss_cb6 + 4 * i, sizeof L->qa[0]);
}

/* everything superframe s needs is in: A(s + 1)'s envelope and the local output up to the
   side channel's frames */
static int lr_ready(const struct sotto_lr_enc16 *L, long s) {
  const struct sotto_enc16 *e = &L->h;
  if (L->lflushed) return 1;
  long g = 2 * s;
  return e->lkmax >= 2 * (g + 2) + 1 && e->lnst > 2 * (g + 1) - 3 && e->nst > 2 * (g + 2) - 1;
}

static void lr_finish(struct sotto_lr_enc16 *L, long s) {
  struct sotto_enc16 *e = &L->h;
  long g = 2 * s;
  int last = L->nsf_end >= 0 && s == L->nsf_end - 1;
  if (s == 0) lr_a_index(L, 0);
  if (!last) lr_a_index(L, s + 1);
  int ca = L->fp[g % FRING].code, cb = L->fp[(g + 1) % FRING].code;
  int sa = enc_side(e, g, ca), sb = enc_side(e, g + 1, cb), wa = -1, dw = -1;
  if (ca == 0) wa = (L->ia[s % LSQ] << 1) | (sa > 0);
  if (cb == 0) {                            /* Sotto1200Hiss._b_base + the nearest level */
    double res[4], base[4];
    int ha = ca == 0, hn = !last && L->ia[(s + 1) % LSQ] >= 0;
    const double *qa = L->qa[s % LSQ], *qn = L->qa[(s + 1) % LSQ];
    for (int b = 0; b < 4; b++) base[b] = ha && hn ? (qa[b] + qn[b]) / 2 : ha ? qa[b] : hn ? qn[b] : 0.0;
    /* the last frame also takes the STFT frames past its own (hiss_band.frame_map's clip) */
    long tlast = last && L->h.nend >= 0 ? e->nst - 1 : 2 * (g + 1) - 1;
    int k = enc_residual(e, g + 1, tlast, res) ? nearest4(sotto_lr_hiss_bcb, 4, base, res) : 0;
    dw = (k << 1) | (sb > 0);
  }
  unsigned char *b = e->outq[(e->qh + e->qn++) % OUTQ];
  memcpy(b, L->sf[s % LSQ], SOTTO_BYTES_PER_FRAME);
  sotto_lr_set_hiss(b, wa, dw);
}

static void lr_run(struct sotto_lr_enc16 *L) {
  struct sotto_enc16 *e = &L->h;
  sotto_enc_frame_t f;
  while (sotto_enc_pop_frame(e->core, &f)) e->rf[e->nraw++ % FRING].raw = f;
  enc_rules(e);
  long nfinal = e->nend >= 0 ? e->nraw : (e->nraw - SOTTO_RULE_LAG > 0 ? e->nraw - SOTTO_RULE_LAG : 0);
  for (; L->npushed < nfinal; L->npushed++) {  /* Sotto1200Hiss._v38_rules -> p["code"], p["wi"] */
    const rframe_t *r = &e->rf[L->npushed % FRING];
    sotto_fparams_t *p = &L->fp[L->npushed % FRING];
    *p = r->raw.p;
    p->code = r->code;
    p->wi = 0;
    for (int i = 2; i < 9; i++) p->wi = (p->wi << 1) | frame_bit(r->bits, i);
    sotto_lr_enc_push(L->lr, p);
    lr_take(L);
  }
  if (e->nend >= 0 && L->nsf_end < 0) {
    sotto_lr_enc_end(L->lr);
    lr_take(L);
    L->nsf_end = L->nsf;
    if (e->nend % 2) L->fp[e->nend % FRING] = L->fp[(e->nend - 1) % FRING];   /* odd count: B = A */
  }
  /* superframe s through the local decoder once s + 1 is coded (B(s) is halfway to A(s + 1)) */
  while (L->nloc < L->nsf - 1 || (L->nsf_end >= 0 && L->nloc < L->nsf_end)) {
    long s = L->nloc++;
    sotto_fparams_t a, b;
    sotto_lr_frames(L->sf[s % LSQ], s + 1 < L->nsf ? L->sf[(s + 1) % LSQ] : NULL, &a, &b);
    lr_feed_local(e, &a);
    lr_feed_local(e, &b);
  }
  if (L->nsf_end >= 0 && L->nloc == L->nsf_end && !L->lflushed) {
    long nf = 2 * L->nsf_end;
    short y8[SOTTO_SAMPLES_PER_FRAME];
    sotto_dec_flush(e->ldec, y8);
    if (nf >= ALIGN_CALLS)
      for (int i = 0; i < SOTTO_SAMPLES_PER_FRAME; i++) e->ly8[e->ly8n++ & (EX8 - 1)] = y8[i] / 32768.0;
    const sotto_dec_info_t *info = sotto_dec_info(e->ldec);
    for (int h = 0; h < 2; h++) {
      long k = 2 * (nf - 1) + h;
      memcpy(e->lenv[k % MRING], info->env[h], sizeof info->env[h]);
      e->lcls[k % MRING] = info->cls[h];
      e->lkmax = k;
    }
    while (e->ly8n < 160 * nf + 10) e->ly8[e->ly8n++ & (EX8 - 1)] = 0;
    L->lflushed = 1;
  }
  enc_advance_local(e);
  while (L->ndone < L->nloc && e->qn < OUTQ && lr_ready(L, L->ndone)) lr_finish(L, L->ndone++);
}

int sotto_lr_encode16(struct sotto_lr_enc16 *L, const short speech[SOTTO_LR_SAMPLES_PER_FRAME_16K],
                      unsigned char out[SOTTO_BYTES_PER_FRAME]) {
  struct sotto_enc16 *e = &L->h;
  for (int i = 0; i < 2 * N16; i++) e->x16[e->x16n++ & (EX16 - 1)] = speech[i];
  e->ncall += 2;
  enc_analyse(e);
  lr_run(L);
  return enc_pop(e, out);
}

int sotto_lr_enc16_flush(struct sotto_lr_enc16 *L, unsigned char out[SOTTO_BYTES_PER_FRAME]) {
  if (L->h.nend < 0) enc_end_input(&L->h);
  lr_run(L);
  return enc_pop(&L->h, out);
}
