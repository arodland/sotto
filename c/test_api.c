/* API test against the shared library: a voiced, rising tone at the target level goes
 * through encoder and decoder, checking frame counts, delay and output level.
 * SPDX-License-Identifier: MIT */
#include <math.h>
#include <stdio.h>
#include <string.h>
#include "sotto.h"
#include "sotto_vlr.h"

#define SECONDS 2
#define PI 3.14159265358979323846
#define N (SOTTO_SAMPLE_RATE * SECONDS / SOTTO_SAMPLES_PER_FRAME)

static double rms_dbfs(const short *x, int n) {
  double s = 0;
  for (int i = 0; i < n; i++) s += (double)x[i] * x[i];
  return 10 * log10(s / n / (32768.0 * 32768.0) + 1e-20);
}

int main(void) {
  static short in[N * SOTTO_SAMPLES_PER_FRAME], out[(N + 1) * SOTTO_SAMPLES_PER_FRAME];
  static unsigned char bits[N + 8][SOTTO_BYTES_PER_FRAME];
  /* buzz at 120-160 Hz: harmonics up to 3.5 kHz with falling amplitude */
  double ph = 0, amp = 0;
  for (int k = 1; k * 120 < 3500; k++) amp += 1.0 / (k * k);
  amp = 32768 * pow(10, SOTTO_LEVEL_TARGET_DBFS / 20) / sqrt(amp / 2);
  for (int i = 0; i < N * SOTTO_SAMPLES_PER_FRAME; i++) {
    double f0 = 120 + 40.0 * i / (N * SOTTO_SAMPLES_PER_FRAME), v = 0;
    ph += 2 * PI * f0 / SOTTO_SAMPLE_RATE;
    for (int k = 1; k * f0 < 3500; k++) v += sin(k * ph) / k;
    in[i] = (short)(amp * v);
  }
  printf("format: %s\n", sotto_format());
  struct sotto_enc *e = sotto_enc_create();
  int nf = 0;
  for (int f = 0; f < N; f++) nf += sotto_encode(e, in + f * SOTTO_SAMPLES_PER_FRAME, bits[nf]);
  int early = nf;
  while (sotto_enc_flush(e, bits[nf])) nf++;
  sotto_enc_destroy(e);
  struct sotto_dec *d = sotto_dec_create();
  for (int f = 0; f < nf; f++) sotto_decode(d, bits[f], 1, out + f * SOTTO_SAMPLES_PER_FRAME);
  sotto_dec_flush(d, out + nf * SOTTO_SAMPLES_PER_FRAME);
  sotto_dec_destroy(d);
  /* skip the decoder's 20 ms delay and the first and last 200 ms */
  int skip = SOTTO_SAMPLES_PER_FRAME + 1600, len = N * SOTTO_SAMPLES_PER_FRAME - 3200;
  double li = rms_dbfs(in + 1600, len), lo = rms_dbfs(out + skip, len);
  printf("frames %d (%d before flush), input %.1f dBFS, output %.1f dBFS\n", nf, early, li, lo);
  int ok = nf == N && early == N - 5 && fabs(lo - li) < 3.0;
#ifdef SOTTO_V38
  {   /* the 16 kHz path: the same tone with a hiss burst, upsampled by sample repetition
         (its images above 4 kHz are what the high band has to code) */
    static short in16[N * SOTTO_SAMPLES_PER_FRAME_16K], out16[(N + 24) * SOTTO_SAMPLES_PER_FRAME_16K];
    unsigned s = 12345;
    for (int i = 0; i < N * SOTTO_SAMPLES_PER_FRAME_16K; i++) {
      int burst = i >= N * SOTTO_SAMPLES_PER_FRAME_16K / 2 && i < N * SOTTO_SAMPLES_PER_FRAME_16K * 3 / 4;
      s = s * 1103515245u + 12345u;
      in16[i] = burst ? (short)(((int)(s >> 16) - 32768) / 8) : in[i / 2];
    }
    struct sotto_enc16 *e16 = sotto_enc16_create();
    int nf16 = 0, early16;
    for (int f = 0; f < N; f++) nf16 += sotto_encode16(e16, in16 + f * SOTTO_SAMPLES_PER_FRAME_16K, bits[nf16]);
    early16 = nf16;
    while (sotto_enc16_flush(e16, bits[nf16])) nf16++;
    sotto_enc16_destroy(e16);
    struct sotto_dec16 *d16 = sotto_dec16_create();
    int no = 0;
    for (int f = 0; f < nf16; f++) sotto_decode16(d16, bits[f], 1, out16 + (no++) * SOTTO_SAMPLES_PER_FRAME_16K);
    while (sotto_dec16_flush(d16, out16 + no * SOTTO_SAMPLES_PER_FRAME_16K)) no++;
    sotto_dec16_destroy(d16);
    /* output starts SOTTO_DEC16_DELAY_FRAMES frames late; compare the tone's level and the
       hiss burst's level (the burst is white, so its 4-8 kHz half must come back) */
    int d0 = SOTTO_DEC16_DELAY_FRAMES * SOTTO_SAMPLES_PER_FRAME_16K;
    int q = N * SOTTO_SAMPLES_PER_FRAME_16K / 4;
    double ti = rms_dbfs(in16 + 3200, q - 6400), to = rms_dbfs(out16 + d0 + 3200, q - 6400);
    double bi = rms_dbfs(in16 + 2 * q + 1600, q - 3200), bo = rms_dbfs(out16 + d0 + 2 * q + 1600, q - 3200);
    printf("16 kHz: frames %d (%d before flush), %d blocks out; tone %.1f -> %.1f dBFS, hiss burst %.1f -> %.1f dBFS\n",
           nf16, early16, no, ti, to, bi, bo);
    int ok16 = nf16 == N && no == N + SOTTO_DEC16_DELAY_FRAMES && fabs(to - ti) < 3.0 && fabs(bo - bi) < 4.0;
    ok = ok && ok16;
    /* sotto 1200 hiss: the same 16 kHz input, 40 ms superframes */
    int ns = N / 2, nh = 0, earlyh;
    struct sotto_lr_enc16 *eh = sotto_lr_enc16_create();
    for (int f = 0; f < ns; f++) nh += sotto_lr_encode16(eh, in16 + f * SOTTO_LR_SAMPLES_PER_FRAME_16K, bits[nh]);
    earlyh = nh;
    while (sotto_lr_enc16_flush(eh, bits[nh])) nh++;
    sotto_lr_enc16_destroy(eh);
    struct sotto_lr_dec16 *dh = sotto_lr_dec16_create();
    no = 0;
    for (int f = 0; f < nh; f++) sotto_lr_decode16(dh, bits[f], 1, out16 + (no++) * SOTTO_LR_SAMPLES_PER_FRAME_16K);
    while (sotto_lr_dec16_flush(dh, out16 + no * SOTTO_LR_SAMPLES_PER_FRAME_16K)) no++;
    sotto_lr_dec16_destroy(dh);
    d0 = SOTTO_LR_DEC16_DELAY * SOTTO_LR_SAMPLES_PER_FRAME_16K;
    to = rms_dbfs(out16 + d0 + 3200, q - 6400), bo = rms_dbfs(out16 + d0 + 2 * q + 1600, q - 3200);
    printf("sotto 1200 hiss: superframes %d (%d before flush), %d blocks out; tone %.1f -> %.1f dBFS, hiss burst %.1f -> %.1f dBFS\n",
           nh, earlyh, no, ti, to, bi, bo);
    ok = ok && nh == ns && earlyh == ns - 11 && no == ns + SOTTO_LR_DEC16_DELAY && fabs(to - ti) < 3.0 && fabs(bo - bi) < 4.0;
    /* sotto 600 / 400 hiss */
    for (int r = 600; r >= 400; r -= 200) {
      int n = r == 600 ? 4 : 6, want = (N + n - 1) / n, nv = 0, earlyv;
      struct sotto_vlr_enc16 *ev = sotto_vlr_enc16_create(r);
      for (int f = 0; f < 2 * N; f++) {
        sotto_vlr_encode16(ev, in16 + f * SOTTO_SAMPLES_PER_FRAME_16K / 2, 1);
        while (sotto_vlr_enc16_pop(ev, bits[nv])) nv++;
      }
      earlyv = nv;
      while (sotto_vlr_enc16_flush(ev, bits[nv])) nv++;
      sotto_vlr_enc16_destroy(ev);
      struct sotto_vlr_dec16 *dv = sotto_vlr_dec16_create(r);
      int ss = sotto_vlr_samples16(dv);
      no = 0;
      for (int f = 0; f < nv; f++) sotto_vlr_decode16(dv, bits[f], 1, out16 + (no++) * ss);
      while (sotto_vlr_dec16_flush(dv, out16 + no * ss)) no++;
      sotto_vlr_dec16_destroy(dv);
      d0 = SOTTO_VLR_DEC16_DELAY(r) * ss;
      to = rms_dbfs(out16 + d0 + 3200, q - 6400), bo = rms_dbfs(out16 + d0 + 2 * q + 1600, q - 3200);
      printf("sotto %d hiss: superframes %d (%d before flush), %d out; tone %.1f -> %.1f dBFS, hiss burst %.1f -> %.1f dBFS\n",
             r, nv, earlyv, no, ti, to, bi, bo);
      ok = ok && ss == n * SOTTO_SAMPLES_PER_FRAME_16K && nv == want && earlyv >= want - (r == 600 ? 10 : 9) &&
           no == nv + SOTTO_VLR_DEC16_DELAY(r) && fabs(to - ti) < 3.0 && fabs(bo - bi) < 4.0 && !sotto_vlr_enc16_create(1200);
    }
  }
#endif
  /* sotto 600 / 400 encoder: one superframe per 4 / 6 frames, most of them before the flush */
  for (int r = 600; r >= 400; r -= 200) {
    struct sotto_vlr_enc *ev = sotto_vlr_enc_create(r);
    int ns = 0, ns_early, want = (N + (r == 600 ? 3 : 5)) / (r == 600 ? 4 : 6);
    for (int f = 0; f < 2 * N; f++) {
      sotto_vlr_encode(ev, in + f * SOTTO_SAMPLES_PER_FRAME / 2, 1);
      while (sotto_vlr_enc_pop(ev, bits[ns])) ns++;
    }
    ns_early = ns;
    while (sotto_vlr_enc_flush(ev, bits[ns])) ns++;
    sotto_vlr_enc_destroy(ev);
    printf("sotto %d: superframes %d (%d before flush)\n", r, ns, ns_early);
    ok = ok && ns == want && ns_early >= want - 3 && !sotto_vlr_enc_create(1200);
  }
  printf(ok ? "PASS\n" : "FAIL\n");
  return !ok;
}
