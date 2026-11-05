/* sotto v3.5 encoder and decoder, floating point. See sotto.h.
 *
 * Each function names the Python it mirrors (sim/sinusoid.py, sim/vocoder_lab.py), so the
 * two can be checked against each other with tools/check_c_port.py.
 *
 * SPDX-License-Identifier: MIT
 */
#define SOTTO_BUILD
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "codec2_fft.h"
#include "defines.h"
#include "newamp1.h"
#include "nlp.h"
#include "phase.h"
#include "postfilter.h"
#include "sine.h"
#include "sotto.h"
#include "sotto_codebook.h"
#include "sotto_internal.h"

#define FS 8000
#define NSAMP 80          /* 10 ms analysis frame */
#define MPITCH 320
#define NFFT FFT_ENC      /* 512 */
#define K 20              /* rate-K envelope points */
#define NC (K - 1)        /* DCT coefficients sent (1..K-1) */

/* pitch and voicing tracker (sinusoid.track_pitch_voicing with sotto v3.3's options) */
#define LAG 8             /* fixed-lag decision: 80 ms */
#define NCAND 10          /* candidates: 3 neighbours x {1, 2, 1/2}, and 1/3 of frame t's own (v3.7) */
#define NST (NCAND + 1)   /* state 0 = unvoiced */
#define RING 16           /* frames kept: > LAG + 2 */
#define LAMBDA 3.0
#define BIAS 0.5
#define FMIN 70.0
#define UV_COST (-0.5)
#define PRIOR 1.0
#define SWITCH 1.5
#define JUMP 4.0
#define SUB 2.0
#define SUB_DB 10.0

/* unvoice_tails and voicing_classes */
#define TAIL_SNR 6.0
#define TAIL_DROP 20.0
#define TAIL_HOLD 30
#define MIXED_SNR 8.0
#define MIXED 2
#define MIX_HZ 1000.0

/* frame fields */
#define E_LO 10.0
#define E_HI 80.0
#define PF_GAIN 1.35

/* concealment (SottoV35) */
#define C_HOLD 2
#define C_SLOPE 6.0
#define C_MIX_AFTER 99
#define C_MAX_LOST 4

#define MBEST 8

/* v3.6: energy field from the rate-K envelope (vocoder_lab.SottoV36). Build with
   make VARIANT=v3.6, which also picks the 9+9+8+8 codebook in 8-bit values. */
#ifdef SOTTO_V36
#define SOTTO_ENERGY_RK 1
#else
#define SOTTO_ENERGY_RK 0
#endif
/* v3.7 (make VARIANT=v3.7, the default): v3.6 plus a pitch candidate at a third of Codec 2's
   estimate (vocoder_lab.SottoV37). Encoder only; the frame and decoder are v3.6's. */
#ifdef SOTTO_PITCH_THIRD
#define N_MU 4
#else
#define N_MU 3
#endif
/* v3.8 (make VARIANT=v3.8): the v3.7 frame with a 4-8 kHz hiss band (sotto_hiss.c). In the
   core: unvoiced halves are synthesised 50 Hz apart whatever the pitch field says (it carries
   a hiss index in all-unvoiced frames), upward-tilted ones 3 dB louder, and a voiced second
   half between two unvoiced frames keeps its own pitch (hiss_band.SottoV38._model_hook,
   vocoder_lab.interp_wo guard). */
#ifdef SOTTO_V38
#define V38 1
#else
#define V38 0
#endif
#define UV_GAIN_DB 3.0

static const double PI_D = 3.14159265358979323846;
#define WO_MIN (2 * PI_D / 160)
#define WO_HI (2 * PI_D / 16)

/* ------------------------------------------------------------------ shared tables */
static double kfreq[K];   /* Hz, vocoder_lab.KFREQ */
static double dct[K][K];  /* orthonormal DCT-II, vocoder_lab.DCT */
static double pre[K];     /* postfilter pre-emphasis */
static int klo[K], khi[K];  /* KFREQ < 1000 Hz, > 2000 Hz (hiss_band.KLO, KHI) */
static int tables_ready;
static const double fb_hz[SOTTO_NFB] = {1250, 1750, 2250, 2750, 3250, 3600, 3800};   /* hiss_band.FB */

static double mel(double f) { return floor(2595 * log10(1 + f / 700) + 0.5); }

static void init_tables(void) {
  if (tables_ready) return;
  double a = mel(200), b = mel(3700);
  for (int k = 0; k < K; k++) {
    double m = (k == K - 1) ? b : a + k * (b - a) / (K - 1);
    kfreq[k] = 700 * (pow(10, m / 2595) - 1);
    pre[k] = 20 * log10(kfreq[k] / 300.0);
    klo[k] = kfreq[k] < 1000;
    khi[k] = kfreq[k] > 2000;
    for (int n = 0; n < K; n++)
      dct[k][n] = sqrt(2.0 / K) * cos(PI_D * (n + 0.5) * k / K) / (k == 0 ? sqrt(2.0) : 1.0);
  }
  tables_ready = 1;
}

/* np.interp: piecewise linear, clamped to the end values */
static double interp(double x, const double *xp, const double *fp, int n) {
  if (x <= xp[0]) return fp[0];
  if (x >= xp[n - 1]) return fp[n - 1];
  int j = 0;
  while (xp[j + 1] < x) j++;
  if (x == xp[j + 1]) return fp[j + 1];
  return fp[j] + (fp[j + 1] - fp[j]) * (x - xp[j]) / (xp[j + 1] - xp[j]);
}

/* vocoder_lab.postfilter_rk */
static void postfilter_rk(double *v, double gain) {
  double e0 = 0, e1 = 0;
  for (int k = 0; k < K; k++) {
    v[k] += pre[k];
    e0 += pow(10, v[k] / 10);
  }
  for (int k = 0; k < K; k++) {
    v[k] *= gain;
    e1 += pow(10, v[k] / 10);
  }
  double d = 10 * (log10(e1) - log10(e0));
  for (int k = 0; k < K; k++) v[k] -= d + pre[k];
}

static double wo_hz(double wo) { return wo * FS / (2 * PI_D); }

double sotto_env_db(const double *rk, double hz) { return interp(hz, kfreq, rk, K); }

/* mean rk below 1 kHz minus mean rk above 2 kHz (hiss_band: rk[KLO].mean() - rk[KHI].mean()) */
static double rk_tilt(const double *rk) {
  double lo = 0, hi = 0;
  int nlo = 0, nhi = 0;
  for (int k = 0; k < K; k++) {
    if (klo[k]) lo += rk[k], nlo++;
    if (khi[k]) hi += rk[k], nhi++;
  }
  return lo / nlo - hi / nhi;
}

/* sinusoid.n_harm: Codec 2's harmonic count, in float like Codec 2 */
static int n_harm(float wo) {
  int L = (int)floorf((float)M_PI / wo);
  return (wo * (float)L >= (float)(0.95 * M_PI)) ? L - 1 : L;
}

const char *sotto_format(void) {
  static char buf[64];
  if (!buf[0]) {
    int n = snprintf(buf, sizeof buf, "sotto %s, shape ", V38 ? "v3.8" : SOTTO_ENERGY_RK ? "v3.6" : "v3.5");
    for (int s = 0; s < sotto_cb_stages && n < (int)sizeof buf; s++)
      n += snprintf(buf + n, sizeof buf - n, s ? "-%d" : "%d", sotto_cb_bits[s]);
  }
  return buf;
}

/* ------------------------------------------------------------------ encoder */
typedef struct {
  float wo0;            /* Codec 2's Wo (nlp + two-stage refinement) */
  int v0;               /* Codec 2's voicing */
  COMP Sw[NFFT];
  int nc;
  double cand[NCAND];
  double cost[NST];
  double D[NST];        /* best path cost ending in each state */
  unsigned char back[NST];
} frame_t;

struct sotto_enc {
  C2CONST c2;
  codec2_fft_cfg fft;
  void *nlp;
  float w[MPITCH], W[NFFT], Sn[MPITCH], prev_f0;
  COMP Sw[NFFT];
  frame_t ring[RING];
  long n_an;            /* frames analysed */
  long n_dec;           /* frames decided */
  double ehist[TAIL_HOLD + 1];
  /* even half of the frame being built */
  float wo_e;
  double en_e;
  int idx_e[SOTTO_CB_MAX_STAGES], cls_e;
  double c_e[NC];                  /* its shape target (DCT coefficients 1..K-1) */
  sotto_enc_frame_t cur;           /* the frame being built: per-half features for v3.8 */
  double peak;                     /* causal loudness reference (hiss_band._onsets) */
  sotto_enc_frame_t q[8];
  int q_head, q_n, flushed;
};

#define R(e, t) (&(e)->ring[(t) % RING])

/* sinusoid.refine: Codec 2's two-stage harmonic-sum refinement around wo */
static double refine(const COMP *Sw, double wo, double wo_max) {
  static const double span[2] = {5, 1}, step[2] = {1.0, 0.25};
  for (int s = 0; s < 2; s++) {
    int L = (int)(PI_D / wo);
    double p0 = 2 * PI_D / wo, best = -1, wbest = wo;
    int np = (int)ceil((2 * span[s] + 1e-9) / step[s]);
    for (int i = 0; i < np; i++) {
      double w = 2 * PI_D / (p0 - span[s] + i * step[s]), e = 0;
      for (int m = 1; m <= L; m++) {
        int b = (int)(m * w * NFFT / (2 * PI_D) + 0.5);
        if (b < NFFT / 2) e += (double)Sw[b].real * Sw[b].real + (double)Sw[b].imag * Sw[b].imag;
      }
      if (e > best) best = e, wbest = w;
    }
    wo = wbest;
  }
  return wo < WO_MIN ? WO_MIN : wo > wo_max ? wo_max : wo;
}

/* Least-squares fit of one windowed sinusoid to Sw[al..bl): returns the residual energy
   and adds the band energy to *sig (the MBE fit of est_voicing_mbe). */
static double harm_fit(const COMP *Sw, const float *W, double wo, int l, double *sig, int f32) {
  int al, bl, off;
  if (f32) {            /* harmonic_snr is called with a float32 Wo: numpy then works in float32 */
    float w = (float)wo, tp = (float)(2 * PI_D);
    al = (int)ceilf((float)(l - 0.5) * w * (float)NFFT / tp);
    bl = (int)ceilf((float)(l + 0.5) * w * (float)NFFT / tp);
    off = (int)(256.0f - (float)l * w * (float)NFFT / tp + 0.5f);
  } else {
    double k = wo * NFFT / (2 * PI_D);
    al = (int)ceil((l - 0.5) * k), bl = (int)ceil((l + 0.5) * k);
    off = (int)(NFFT / 2 - l * k + 0.5);
  }
  double num_r = 0, num_i = 0, den = 0, s = 0, err = 0;
  for (int i = al; i < bl; i++) {
    double wm = W[off + i];
    num_r += Sw[i].real * wm;
    num_i += Sw[i].imag * wm;
    den += wm * wm;
  }
  if (den < 1e-12) den = 1e-12;
  double ar = num_r / den, ai = num_i / den;
  for (int i = al; i < bl; i++) {
    double wm = W[off + i], er = Sw[i].real - ar * wm, ei = Sw[i].imag - ai * wm;
    err += er * er + ei * ei;
    s += (double)Sw[i].real * Sw[i].real + (double)Sw[i].imag * Sw[i].imag;
  }
  if (sig) *sig = s;
  return err;
}

/* sinusoid.fit_error */
static double fit_error(const COMP *Sw, const float *W, double wo) {
  double k = wo * NFFT / (2 * PI_D);
  int L = (int)(2500.0 / wo_hz(wo));
  if (L < 1) L = 1;
  int hi = (int)ceil((L + 0.5) * k);
  double err = 1e-9, e = 1e-9;
  for (int l = 1; l <= L; l++) err += harm_fit(Sw, W, wo, l, NULL, 0);
  for (int i = 1; i < hi; i++) e += (double)Sw[i].real * Sw[i].real + (double)Sw[i].imag * Sw[i].imag;
  return err / e;
}

/* sinusoid.odd_even_db */
static double odd_even_db(const COMP *Sw, double w) {
  int h = (int)(w * NFFT / (2 * PI_D) / 4);
  if (h < 1) h = 1;
  int L = (int)(1500.0 / wo_hz(w));
  double odd = 0, even = 0;
  for (int j = 3; j <= L; j++) {
    int c = (int)(j * w * NFFT / (2 * PI_D));
    double p = 0;
    if (c < NFFT / 2) {
      int lo = c - h > 1 ? c - h : 1;
      for (int i = lo; i <= c + h; i++) {
        double q = (double)Sw[i].real * Sw[i].real + (double)Sw[i].imag * Sw[i].imag;
        if (q > p) p = q;
      }
    }
    if (j % 2) odd += p; else even += p;
  }
  return 10 * log10((odd + 1e-9) / (even + 1e-9));
}

/* mean per-harmonic SNR (dB) of the harmonics below 1 kHz (sinusoid.harmonic_snr) */
static double low_snr(const COMP *Sw, const float *W, float wo) {
  int L = n_harm(wo), n = 0;
  double sum = 0;
  for (int l = 1; l <= L; l++) {
    double f = l * (double)wo * FS / (2 * PI_D);
    if (f >= 1000) break;
    double sig, err = harm_fit(Sw, W, wo, l, &sig, 1) + 1e-12;
    sum += 10 * log10(sig / err + 1e-12);
    n++;
  }
  return n ? sum / n : 1e9;
}

/* sinusoid.amplitudes: harmonic magnitudes at a double-precision Wo */
static void amplitudes(const COMP *Sw, double wo, MODEL *m) {
  double r = 2 * PI_D / NFFT;
  m->Wo = (float)wo;
  m->L = n_harm((float)wo);
  for (int h = 1; h <= m->L; h++) {
    int am = (int)((h - 0.5) * wo / r + 0.5), bm = (int)((h + 0.5) * wo / r + 0.5);
    double p = 0;
    for (int i = am; i < bm; i++) p += (double)Sw[i].real * Sw[i].real + (double)Sw[i].imag * Sw[i].imag;
    m->A[h] = (float)sqrt(p);
  }
}

/* candidates and costs of frame t (needs frames t-1..t+1 analysed when they exist) */
static void frame_costs(struct sotto_enc *e, long t, int has_next) {
  frame_t *f = R(e, t);
  static const double mu[4] = {1.0, 2.0, 0.5, 1.0 / 3};
  static const int dd[3] = {0, -1, 1};
  f->nc = 0;
  for (int i = 0; i < 3; i++) {
    long u = t + dd[i];
    if (u < 0 || (dd[i] == 1 && !has_next)) continue;
    for (int j = 0; j < (dd[i] == 0 ? N_MU : 3); j++) {
      if (j == 3 && wo_hz(R(e, u)->wo0 * mu[j]) < FMIN) continue;
      double w0 = R(e, u)->wo0 * mu[j];
      w0 = w0 < WO_MIN ? WO_MIN : w0 > WO_HI ? WO_HI : w0;
      double w = refine(f->Sw, w0, WO_HI);
      if (wo_hz(w) >= FMIN || (dd[i] == 0 && j == 0)) {
        int dup = 0;
        for (int c = 0; c < f->nc; c++)
          if (fabs(log(w / f->cand[c])) <= 0.01) dup = 1;
        if (!dup) f->cand[f->nc++] = w;
      }
    }
  }
  f->cost[0] = UV_COST + PRIOR * (f->v0 > 0);
  for (int c = 0; c < f->nc; c++) {
    double w = f->cand[c];
    double cst = log(fit_error(f->Sw, e->W, w)) - BIAS * log(w) + PRIOR * (f->v0 == 0);
    double oe = odd_even_db(f->Sw, w);
    if (-oe - SUB_DB > 0) cst += SUB * (-oe - SUB_DB) / 10;
    f->cost[1 + c] = cst;
  }
}

/* one Viterbi step into frame t */
static void viterbi_step(struct sotto_enc *e, long t) {
  frame_t *f = R(e, t);
  int n = f->nc + 1;
  if (t == 0) {
    for (int j = 0; j < n; j++) f->D[j] = f->cost[j], f->back[j] = 0;
    return;
  }
  frame_t *p = R(e, t - 1);
  int np = p->nc + 1;
  for (int j = 0; j < n; j++) {
    double best = 0;
    int bi = 0;
    for (int i = 0; i < np; i++) {
      double tr;
      if (i == 0 && j == 0) tr = 0;
      else if (i == 0 || j == 0) tr = SWITCH;
      else {
        double lj = fabs(log(p->cand[i - 1] / f->cand[j - 1])), x = lj - log(2.0) / 3;
        tr = LAMBDA * lj + JUMP * (x > 0 ? x : 0);
      }
      double tot = p->D[i] + tr;
      if (i == 0 || tot < best) best = tot, bi = i;
    }
    f->D[j] = best + f->cost[j];
    f->back[j] = (unsigned char)bi;
  }
}

static void put_bits(unsigned char *out, int *pos, int v, int nb) {
  for (int i = nb - 1; i >= 0; i--, (*pos)++)
    if ((v >> i) & 1) out[*pos >> 3] |= 0x80 >> (*pos & 7);
}

static int get_bits(const unsigned char *in, int *pos, int nb) {
  int v = 0;
  for (int i = 0; i < nb; i++, (*pos)++) v = (v << 1) | ((in[*pos >> 3] >> (7 - (*pos & 7))) & 1);
  return v;
}

/* ShapeMSVQ._search: M-best multistage search over DCT coefficients 1..K-1 */
static void msvq_search(const double *c, int *idx) {
  int np = 1, path[MBEST][SOTTO_CB_MAX_STAGES];
  double recon[MBEST][NC];
  memset(recon, 0, sizeof recon);
  for (int s = 0; s < sotto_cb_stages; s++) {
    int n = 1 << sotto_cb_bits[s], nk = 0;
    const float *cb = sotto_cb[s];
    double kd[MBEST];
    int kp[MBEST], kc[MBEST];
    for (int p = 0; p < np; p++) {
      double res[NC];
      for (int k = 0; k < NC; k++) res[k] = c[k] - recon[p][k];
      for (int v = 0; v < n; v++) {
        const float *y = cb + v * NC;
        double d = 0;
        for (int k = 0; k < NC; k++) d += (res[k] - y[k]) * (res[k] - y[k]);
        if (nk < MBEST || d < kd[nk - 1]) {          /* insert into the sorted keep list */
          int i = nk < MBEST ? nk++ : MBEST - 1;
          while (i > 0 && kd[i - 1] > d) kd[i] = kd[i - 1], kp[i] = kp[i - 1], kc[i] = kc[i - 1], i--;
          kd[i] = d, kp[i] = p, kc[i] = v;
        }
      }
    }
    int npath[MBEST][SOTTO_CB_MAX_STAGES];
    double nrec[MBEST][NC];
    for (int i = 0; i < nk; i++) {
      memcpy(npath[i], path[kp[i]], sizeof path[0]);
      npath[i][s] = kc[i];
      for (int k = 0; k < NC; k++) nrec[i][k] = recon[kp[i]][k] + cb[kc[i] * NC + k];
    }
    memcpy(path, npath, sizeof path);
    memcpy(recon, nrec, sizeof recon);
    np = nk;
  }
  int best = 0;
  double bd = 0;
  for (int p = 0; p < np; p++) {
    double d = 0;
    for (int k = 0; k < NC; k++) d += (c[k] - recon[p][k]) * (c[k] - recon[p][k]);
    if (p == 0 || d < bd) bd = d, best = p;
  }
  memcpy(idx, path[best], sotto_cb_stages * sizeof(int));
}

/* vocoder_lab.to_rate_k for one frame */
static void to_rate_k(const MODEL *m, double *rk) {
  double f[MAX_AMP], a[MAX_AMP], amax = -1e300;
  for (int h = 1; h <= m->L; h++) {
    f[h - 1] = h * (double)m->Wo * FS / (2 * PI_D);
    a[h - 1] = 20 * log10f(m->A[h] + 1e-16f);
    if (a[h - 1] > amax) amax = a[h - 1];
  }
  float fl = (float)amax - 50.0f;
  for (int h = 0; h < m->L; h++)
    if (a[h] < fl) a[h] = fl;
  for (int k = 0; k < K; k++) rk[k] = interp(kfreq[k], f, a, m->L);
}

static int q_index(double x, double lo, double hi, int bits) {
  int n = 1 << bits;
  double i = rint((x - lo) / (hi - lo) * (n - 1));
  return i < 0 ? 0 : i > n - 1 ? n - 1 : (int)i;
}

static void emit(struct sotto_enc *e, int cls);

/* frame t is decided: voiced at candidate k-1, or unvoiced (k = 0) */
static void decide(struct sotto_enc *e, long t, int k) {
  frame_t *f = R(e, t);
  double wd = k ? f->cand[k - 1] : f->wo0;
  float wo = (float)wd;
  MODEL m;
  memset(&m, 0, sizeof m);
  amplitudes(f->Sw, wd, &m);
  float s = 0;            /* float32, as vocoder_lab.frame_energy_db */
  for (int h = 1; h <= m.L; h++) s += m.A[h] * m.A[h];
  double en = 10.0f * log10f(s + 1e-9f);

  /* unvoice_tails, then voicing_classes */
  e->ehist[t % (TAIL_HOLD + 1)] = en;
  int cls = k > 0;
  if (cls) {
    double emax = -1e300;
    for (long u = t - TAIL_HOLD < 0 ? 0 : t - TAIL_HOLD; u <= t; u++)
      if (e->ehist[u % (TAIL_HOLD + 1)] > emax) emax = e->ehist[u % (TAIL_HOLD + 1)];
    double snr = low_snr(f->Sw, e->W, wo);
    if (en < emax - TAIL_DROP && snr < TAIL_SNR) cls = 0;
    else if (snr < MIXED_SNR) cls = MIXED;
  }

  {   /* per-half features of the v3.8 encoder rules (hiss_band._onsets, _whisper) */
    double rk[K], mean = 0;
    int h = (int)(t % 2);
    to_rate_k(&m, rk);
    for (int i = 0; i < K; i++) mean += rk[i];
    mean /= K;
    e->peak = fmax(mean, e->peak - 0.05);
    e->cur.en[h] = mean;
    e->cur.loud[h] = mean > e->peak - 6.0 - 10.0;
    e->cur.tilt[h] = rk_tilt(rk);
    e->cur.snr[h] = cls ? low_snr(f->Sw, e->W, wo) : NAN;
    e->cur.lwo[h] = log(wd);
  }

  if (t % 2 == 0) {
    double rk[K], mean = 0, c[NC];
    to_rate_k(&m, rk);
    for (int i = 0; i < K; i++) mean += rk[i];
    mean /= K;
    for (int j = 1; j < K; j++) {
      double v = 0;
      for (int i = 0; i < K; i++) v += dct[j][i] * (rk[i] - mean);
      c[j - 1] = v;
    }
    msvq_search(c, e->idx_e);
    memcpy(e->c_e, c, sizeof e->c_e);
    e->wo_e = wo;
    if (SOTTO_ENERGY_RK) {   /* energy of from_rate_k(m, rk), as SottoV36 codes it */
      float s2 = 0;
      for (int h = 1; h <= m.L; h++) {
        float a = (float)pow(10, interp(h * (double)m.Wo * FS / (2 * PI_D), kfreq, rk, K) / 20);
        s2 += a * a;
      }
      en = 10.0f * log10f(s2 + 1e-9f);
    }
    e->en_e = en;
    e->cls_e = cls;
  } else {
    emit(e, cls);
  }
}

/* pack the frame: the even half's parameters and both halves' voicing classes */
static void emit(struct sotto_enc *e, int cls) {
  {
    int a = e->cls_e, b = cls, code = a == b ? a : (a == 0 || b == 0) ? 3 : 1;
    sotto_enc_frame_t *fr = &e->q[(e->q_head + e->q_n++) % 8];
    unsigned char *out = fr->bits;
    int pos = 0;
    *fr = e->cur;
    fr->p.code = code;
    fr->p.wi = q_index(logf(e->wo_e), log(WO_MIN), log(WO_HI), 7);
    fr->p.ei = q_index(e->en_e, E_LO, E_HI, 5);
    memcpy(fr->p.c, e->c_e, sizeof fr->p.c);
    memset(out, 0, SOTTO_BYTES_PER_FRAME);
    put_bits(out, &pos, code, 2);
    put_bits(out, &pos, fr->p.wi, 7);
    put_bits(out, &pos, fr->p.ei, 5);
    for (int s2 = 0; s2 < sotto_cb_stages; s2++) put_bits(out, &pos, e->idx_e[s2], sotto_cb_bits[s2]);
  }
}

/* decide frame t from the best path ending at frame last */
static void decide_from(struct sotto_enc *e, long t, long last) {
  frame_t *f = R(e, last);
  int k = 0;
  for (int j = 1; j <= f->nc; j++)
    if (f->D[j] < f->D[k]) k = j;
  for (long u = last; u > t; u--) k = R(e, u)->back[k];
  decide(e, t, k);
}

struct sotto_enc *sotto_enc_create(void) {
  struct sotto_enc *e = calloc(1, sizeof *e);
  if (!e) return NULL;
  init_tables();
  e->c2 = c2const_create(FS, N_S);
  e->fft = codec2_fft_alloc(NFFT, 0, NULL, NULL);
  e->nlp = nlp_create(&e->c2);
  make_analysis_window(&e->c2, e->fft, e->w, e->W);
  for (int i = 0; i < MPITCH; i++) e->Sn[i] = 1.0f;
  e->prev_f0 = 1 / P_MAX_S;
  e->peak = -1e9;
  return e;
}

void sotto_enc_destroy(struct sotto_enc *e) {
  if (!e) return;
  nlp_destroy(e->nlp);
  codec2_fft_free(e->fft);
  free(e);
}

static void analyse_10ms(struct sotto_enc *e, const short *x) {
  MODEL m;
  float pitch;
  memmove(e->Sn, e->Sn + NSAMP, (MPITCH - NSAMP) * sizeof(float));
  for (int i = 0; i < NSAMP; i++) e->Sn[MPITCH - NSAMP + i] = x[i];
  nlp(e->nlp, e->Sn, NSAMP, &pitch, e->Sw, e->W, &e->prev_f0);
  m.Wo = TWO_PI / pitch;
  dft_speech(&e->c2, e->fft, e->Sw, e->Sn, e->w);
  two_stage_pitch_refinement(&e->c2, &m, e->Sw);
  estimate_amplitudes(&m, e->Sw, e->W, 1);
  est_voicing_mbe(&e->c2, &m, e->Sw, e->W);
  long a = e->n_an++;
  frame_t *f = R(e, a);
  f->wo0 = m.Wo;
  f->v0 = m.voiced;
  memcpy(f->Sw, e->Sw, sizeof f->Sw);
  if (a >= 1) {
    frame_costs(e, a - 1, 1);
    viterbi_step(e, a - 1);
    if (a - 1 - LAG >= 0) decide_from(e, e->n_dec++, a - 1);
  }
}

int sotto_enc_pop_frame(struct sotto_enc *e, sotto_enc_frame_t *f) {
  if (!e->q_n) return 0;
  *f = e->q[e->q_head];
  e->q_head = (e->q_head + 1) % 8;
  e->q_n--;
  return 1;
}

static int pop(struct sotto_enc *e, unsigned char *out) {
  sotto_enc_frame_t f;
  if (!sotto_enc_pop_frame(e, &f)) return 0;
  memcpy(out, f.bits, SOTTO_BYTES_PER_FRAME);
  return 1;
}

void sotto_enc_analyse_block(struct sotto_enc *e, const short x8[NSAMP]) { analyse_10ms(e, x8); }

int sotto_encode(struct sotto_enc *e, const short speech[SOTTO_SAMPLES_PER_FRAME],
                 unsigned char out[SOTTO_BYTES_PER_FRAME]) {
  analyse_10ms(e, speech);
  analyse_10ms(e, speech + NSAMP);
  return pop(e, out);
}

void sotto_encode_half(struct sotto_enc *e, const short speech[SOTTO_SAMPLES_PER_FRAME / 2]) {
  analyse_10ms(e, speech);
}

void sotto_enc_flush_core(struct sotto_enc *e) {
  if (!e->flushed && e->n_an > 0) {
    long n = e->n_an;
    frame_costs(e, n - 1, 0);
    viterbi_step(e, n - 1);
    while (e->n_dec < n) decide_from(e, e->n_dec++, n - 1);
    if (n % 2) emit(e, e->cls_e);    /* odd frame count: the last half repeated */
    e->flushed = 1;
  }
}

int sotto_enc_flush(struct sotto_enc *e, unsigned char out[SOTTO_BYTES_PER_FRAME]) {
  sotto_enc_flush_core(e);
  return pop(e, out);
}

/* ------------------------------------------------------------------ decoder */
typedef struct {       /* one decoded 20 ms frame, before synthesis */
  double wo, en, rk[K];
  int v[2];            /* voicing of the two halves: 0, 1 or MIXED */
  float g[2];          /* concealment gain of the two halves */
} dframe_t;

struct sotto_dec {
  C2CONST c2;
  codec2_fft_cfg phase_fwd, phase_inv;
  codec2_fftr_cfg fftr_inv;
  float Pn[2 * NSAMP], Sn_[2 * NSAMP], ex_phase[MAX_AMP + 1], bg_est;
  unsigned char last[SOTTO_BYTES_PER_FRAME];
  int have_last, run, prev_v;
  long n;              /* frames received */
  dframe_t prev;       /* frame n-1 */
  sotto_dec_info_t info;
};

const sotto_dec_info_t *sotto_dec_info(const struct sotto_dec *d) { return &d->info; }

struct sotto_dec *sotto_dec_create(void) {
  struct sotto_dec *d = calloc(1, sizeof *d);
  if (!d) return NULL;
  init_tables();
  d->c2 = c2const_create(FS, N_S);
  d->phase_fwd = codec2_fft_alloc(NEWAMP1_PHASE_NFFT, 0, NULL, NULL);
  d->phase_inv = codec2_fft_alloc(NEWAMP1_PHASE_NFFT, 1, NULL, NULL);
  d->fftr_inv = codec2_fftr_alloc(FFT_DEC, 1, NULL, NULL);
  make_synthesis_window(&d->c2, d->Pn);
  return d;
}

void sotto_dec_destroy(struct sotto_dec *d) {
  if (!d) return;
  codec2_fft_free(d->phase_fwd);
  codec2_fft_free(d->phase_inv);
  codec2_fftr_free(d->fftr_inv);
  free(d);
}

/* the frame fields of a 48-bit frame: shape stages summed into DCT coefficients 1..K-1 */
static void bits_to_params(const unsigned char *b, sotto_fparams_t *p) {
  int pos = 0;
  p->code = get_bits(b, &pos, 2);
  p->wi = get_bits(b, &pos, 7);
  p->ei = get_bits(b, &pos, 5);
  for (int k = 0; k < NC; k++) p->c[k] = 0;
  for (int s = 0; s < sotto_cb_stages; s++) {
    const float *y = sotto_cb[s] + get_bits(b, &pos, sotto_cb_bits[s]) * NC;
    for (int k = 0; k < NC; k++) p->c[k] += y[k];
  }
}

/* SottoV0.decode for one frame, after SottoV34's bit replay */
static void params_to_frame(struct sotto_dec *d, const sotto_fparams_t *p, dframe_t *f) {
  int code = p->code, wi = p->wi, ei = p->ei;
  if (code < 3) f->v[0] = f->v[1] = code;
  else if (d->prev_v == 0) f->v[0] = 0, f->v[1] = MIXED;
  else f->v[0] = MIXED, f->v[1] = 0;
  d->prev_v = f->v[1];
  f->wo = exp(log(WO_MIN) + wi * (log(WO_HI) - log(WO_MIN)) / 127);
  f->en = ei == 0 ? -60.0 : E_LO + ei * (E_HI - E_LO) / 31;
  double c[K] = {0};
  for (int k = 0; k < NC; k++) c[k + 1] = p->c[k];
  for (int n = 0; n < K; n++) {
    double v = 0;
    for (int k = 0; k < K; k++) v += c[k] * dct[k][n];
    f->rk[n] = v;
  }
  postfilter_rk(f->rk, PF_GAIN);
}

/* model for one 10 ms half (set_wo, from_rate_k, set_energy, mixed cutoff, concealment
   gain), then Codec 2's phase0 synthesis and postfilter as c2sim runs them */
static void synth(struct sotto_dec *d, double wo, int v, const double *rk, double en, float g,
                  short *out, int half) {
  MODEL m;
  COMP H[MAX_AMP + 1];
  memset(&m, 0, sizeof m);
  if (!v && (V38 || wo < WO_MIN)) wo = WO_MIN;    /* v3.8: unvoiced noise harmonics 50 Hz apart */
  m.Wo = (float)wo;
  m.L = (int)floor(PI_D / wo);
  m.voiced = v > 0;
  float s = 0;
  for (int h = 1; h <= m.L; h++) {
    double f = h * (double)m.Wo * FS / (2 * PI_D);
    m.A[h] = (float)pow(10, interp(f, kfreq, rk, K) / 20);
    s += m.A[h] * m.A[h];
  }
  double gain = sqrt(pow(10, en / 10) / ((double)s + 1e-12));
  if (V38 && !v && rk_tilt(rk) < 0) gain *= pow(10, UV_GAIN_DB / 20);   /* a real fricative */
  for (int h = 1; h <= m.L; h++) m.A[h] = (float)(m.A[h] * gain) * g;
  {   /* for the hiss band: envelope at FB (hiss_band.model_env) and class */
    double f[MAX_AMP], a[MAX_AMP];
    for (int h = 1; h <= m.L; h++) {
      f[h - 1] = h * (double)m.Wo * FS / (2 * PI_D);
      a[h - 1] = 20 * log10(m.A[h] + 1e-9);
    }
    for (int j = 0; j < SOTTO_NFB; j++) d->info.env[half][j] = interp(fb_hz[j], f, a, m.L);
    d->info.cls[half] = v == MIXED ? 2 : v;
  }
  if (v == MIXED) {
    int c = (int)(MIX_HZ / (m.Wo * FS / (float)(2 * PI_D))) + 1;
    m.voiced = c > 2 ? c : 2;
  }
  determine_phase(&d->c2, H, &m, NEWAMP1_PHASE_NFFT, d->phase_fwd, d->phase_inv);
  phase_synth_zero_order(NSAMP, &m, d->ex_phase, H);
  postfilter(&m, &d->bg_est);
  synthesise(NSAMP, d->fftr_inv, d->Sn_, &m, d->Pn, 1);
  for (int i = 0; i < NSAMP; i++) {
    float x = d->Sn_[i];
    out[i] = x > 32767.0f ? 32767 : x < -32767.0f ? -32767 : (short)x;
  }
}

/* synthesise frame p (both halves); n is the next frame, or NULL at the end */
static void synth_pair(struct sotto_dec *d, const dframe_t *p, const dframe_t *n, short *out) {
  synth(d, p->wo, p->v[0], p->rk, p->en, p->g[0], out, 0);
  /* odd half: interpolated with the next frame (decimate_interp, interp_wo) */
  double rk[K], en, wo;
  const dframe_t *q = n ? n : p;
  int va = p->v[0], vi = p->v[1], vb = n ? n->v[0] : p->v[1];
  for (int k = 0; k < K; k++) rk[k] = 0.5 * (p->rk[k] + q->rk[k]);
  en = 0.5 * (p->en + q->en);
  if (vi && va && vb) wo = 0.5 * (p->wo + q->wo);
  else if (vi) wo = (va || (V38 && !vb)) ? p->wo : q->wo;   /* interp_wo, guard in v3.8 */
  else wo = WO_MIN * 2;
  synth(d, wo, vi, rk, en, p->g[1], out + NSAMP, 1);
}

static float conceal_gain(int run, int half) {
  if (run == 0) return 1.0f;
  if (run > C_MAX_LOST) return 1e-4f;
  double x = run - C_HOLD + half / 2.0;
  return (float)pow(10, -C_SLOPE * (x > 0 ? x : 0) / 20);
}

/* one 20 ms frame's fields in; frame n-1's 160 samples out. run = frames lost in a row
   (0 for a good frame), for the concealment gains. */
void sotto_dec_frame(struct sotto_dec *d, const sotto_fparams_t *p, int run,
                     short speech[SOTTO_SAMPLES_PER_FRAME]) {
  dframe_t f;
  params_to_frame(d, p, &f);
  for (int h = 0; h < 2; h++) {
    f.g[h] = conceal_gain(run, h);
    if (run >= C_MIX_AFTER && run <= C_MAX_LOST && f.v[h] == 1) f.v[h] = MIXED;
  }
  if (d->n == 0) memset(speech, 0, SOTTO_SAMPLES_PER_FRAME * sizeof(short));
  else synth_pair(d, &d->prev, &f, speech);
  d->prev = f;
  d->n++;
}

void sotto_decode(struct sotto_dec *d, const unsigned char in[SOTTO_BYTES_PER_FRAME], int good,
                  short speech[SOTTO_SAMPLES_PER_FRAME]) {
  const unsigned char *b = in;
  if (good) {
    memcpy(d->last, in, SOTTO_BYTES_PER_FRAME);
    d->have_last = 1;
    d->run = 0;
  } else {
    d->run++;
    if (d->have_last) b = d->last;
  }
  sotto_fparams_t p;
  memcpy(d->info.b2, b, SOTTO_BYTES_PER_FRAME);
  d->info.run = d->run;
  bits_to_params(b, &p);
  sotto_dec_frame(d, &p, d->run, speech);
}

void sotto_dec_flush(struct sotto_dec *d, short speech[SOTTO_SAMPLES_PER_FRAME]) {
  if (d->n == 0) {
    memset(speech, 0, SOTTO_SAMPLES_PER_FRAME * sizeof(short));
    return;
  }
  synth_pair(d, &d->prev, NULL, speech);
  d->n = 0;
}
