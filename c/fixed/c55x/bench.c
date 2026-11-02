/* Cycle and memory benchmark of the fixed-point sotto on the TI C55x (C5517).
 *
 * Reads 16-bit little-endian 8 kHz audio from the host file in.raw (CIO), encodes it,
 * writes the bits to out.bit, decodes them, writes the audio to out.raw, and prints the
 * cycles of every sotto_encode / sotto_decode call, as counted by clock() (the debugger's
 * profile clock on a simulator, which bench.js enables). Built by tools/c55x_build.sh.
 * With -DBENCH16 (a v3.8 build): in.raw is 16 kHz and goes through the v3.8 layer
 * (sotto_encode16 / sotto_decode16, 320 samples a call); out.raw is 16 kHz.
 * SPDX-License-Identifier: MIT */
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include "../../sotto.h"
#include "../fx.h"

#define MAXF 1500                       /* 30 s */
#ifdef BENCH16
#define NS SOTTO_SAMPLES_PER_FRAME_16K
#else
#define NS SOTTO_SAMPLES_PER_FRAME
#endif
static short pcm[NS];
static unsigned char bits[MAXF][SOTTO_BYTES_PER_FRAME];
static unsigned long cyc_enc[MAXF], cyc_dec[MAXF];

/* stack high-water marks: fill the unused part of both stacks (they grow down) with a
   pattern at start, and count the words that changed at the end */
extern int stk_start[], stk_end[], sstk_start[], sstk_end[];   /* from bench.cmd */
/* RUN_START / RUN_END give byte addresses; a data pointer holds a word address */
#define WORDS(p) ((int *)((unsigned long)(p) >> 1))
static void fill(int *a, int *b) { while (a < b) *a++ = (int)0xA5C3; }
static void paint(void) {
  int here;
  fill(WORDS(stk_start), &here - 64);
  fill(WORDS(sstk_start), WORDS(sstk_start) + (WORDS(sstk_end) - WORDS(sstk_start)) / 2);   /* the lower half: the upper may be in use */
}
static unsigned used(int *a, int *b) {
  int *p = WORDS(a);
  while (p < WORDS(b) && *p == (int)0xA5C3) p++;
  return (unsigned)(WORDS(b) - p);
}

#ifdef FX_PROFILE
/* inclusive cycles per block id, see FX_PROF in fx.h */
#define NPROF 28
static const char *const prof_name[NPROF] = {"enc nlp", "enc spectrum", "enc hs_refine", "enc c2_voicing",
  "enc frame_costs", "enc decide", "enc  msvq_search", "enc  decide spectrum", "enc  refine",
  "enc  ln_fit_error", "dec unpack", "dec min_phase", "dec overlap-add sums", "enc  odd_even_db", "enc   recip48 in refine", "enc   fft (both spectra)",
  /* the v3.8 16 kHz layer (sotto_fx_hiss.c); the core ids above count inside these too */
  "enc16 decimate + core analysis", "enc16  core analysis", "enc16 input STFT + bands", "enc16 rules",
  "enc16 local decoder (core)", "enc16 local 2x upsampler + STFT", "enc16 finish (index, side bit)", "enc16 core decide",
  "dec16 core decoder", "dec16 2x upsampler", "dec16 stage A (STFT, predictor)", "dec16 stage B (hiss synthesis)"};
static unsigned long prof_tot[NPROF], prof_t0[NPROF], prof_self;   /* the cost of a clock() pair */
void fx_prof(int id, int start) {
  unsigned long t = (unsigned long)clock();
  if (start) prof_t0[id] = t;
  else prof_tot[id] += t - prof_t0[id] - prof_self;
}
static void prof_report(int frames) {
  int i;
  for (i = 0; i < NPROF; i++)
    if (prof_tot[i]) printf("  %-34s %8.2f MHz\n", prof_name[i], prof_tot[i] / (double)frames * 50 / 1e6);
}
#endif

static void stats(const char *name, const unsigned long *c, int n) {
  unsigned long mx = 0, mn = 0xFFFFFFFFul;
  double sum = 0;
  int i;
  for (i = 0; i < n; i++) {
    if (c[i] > mx) mx = c[i];
    if (c[i] < mn) mn = c[i];
    sum += c[i];
  }
  /* a frame is 20 ms, so cycles per frame x 50 = cycles per second */
  printf("%s: %d frames, cycles/frame mean %.0f min %lu max %lu -> %.2f MHz mean, %.2f MHz peak\n",
         name, n, n ? sum / n : 0.0, mn, mx, n ? sum / n * 50 / 1e6 : 0.0, mx * 50 / 1e6);
}

#ifdef BENCH16
#define ENC struct sotto_enc16
#define DEC struct sotto_dec16
#define enc_create sotto_enc16_create
#define dec_create sotto_dec16_create
#define encode sotto_encode16
#define enc_flush sotto_enc16_flush
#define decode sotto_decode16
#define enc_bytes sotto_fx_enc16_bytes
#define dec_bytes sotto_fx_dec16_bytes
#else
#define ENC struct sotto_enc
#define DEC struct sotto_dec
#define enc_create sotto_enc_create
#define dec_create sotto_dec_create
#define encode sotto_encode
#define enc_flush sotto_enc_flush
#define decode sotto_decode
#define enc_bytes sotto_fx_enc_bytes
#define dec_bytes sotto_fx_dec_bytes
#endif

int main(void) {
  FILE *fi, *fb, *fo;
  ENC *e;
  DEC *d;
  int nf = 0, nb = 0, i;
  clock_t t0;
  printf("state: encoder %u words, decoder %u words\n", enc_bytes(), dec_bytes());
  fi = fopen("in.raw", "rb"), fb = fopen("out.bit", "wb");
  e = enc_create();
  d = dec_create();
  if (!fi || !fb || !e || !d) {
    printf("setup failed:%s%s%s%s\n", fi ? "" : " in.raw", fb ? "" : " out.bit", e ? "" : " encoder (heap)", d ? "" : " decoder (heap)");
    return 1;
  }
  paint();
#ifdef FX_PROFILE
  { clock_t a = clock(); prof_self = (unsigned long)(clock() - a); printf("clock() pair: %lu cycles\n", prof_self); }
#endif
#ifdef FFT_BENCH
  {   /* the 512-point FFT alone, on a static buffer */
    static int32_t fre[512], fim[512];
    clock_t a;
    for (i = 0; i < 512; i++) fre[i] = (int32_t)((i * 7919L) % 65536 - 32768) * 64, fim[i] = 0;
    a = clock();
    fx_fft(fre, fim, 512);
    printf("fx_fft 512: %lu cycles\n", (unsigned long)(clock() - a));
  }
#endif
  while (nf < MAXF) {
    unsigned char raw[2 * NS];
    if (fread(raw, 1, sizeof raw, fi) != sizeof raw) break;
    for (i = 0; i < NS; i++)
      pcm[i] = (short)((raw[2 * i] & 0xFF) | ((raw[2 * i + 1] & 0xFF) << 8));
    t0 = clock();
    i = encode(e, pcm, bits[nb]);
    cyc_enc[nf] = (unsigned long)(clock() - t0);
    nb += i;
    nf++;
  }
  while (nb < MAXF && enc_flush(e, bits[nb])) nb++;
  for (i = 0; i < nb; i++) {
    int k;
    for (k = 0; k < SOTTO_BYTES_PER_FRAME; k++) fputc(bits[i][k] & 0xFF, fb);
  }
  fclose(fb);
  fclose(fi);
  fo = fopen("out.raw", "wb");
  for (i = 0; i < nb; i++) {
    int k;
    t0 = clock();
    decode(d, bits[i], 1, pcm);
    cyc_dec[i] = (unsigned long)(clock() - t0);
    for (k = 0; k < NS; k++) {
      fputc(pcm[k] & 0xFF, fo);
      fputc((pcm[k] >> 8) & 0xFF, fo);
    }
  }
  fclose(fo);
  stats("encode", cyc_enc, nf);
  stats("decode", cyc_dec, nb);
#ifdef FX_PROFILE
  prof_report(nf);
#endif
  printf("stack used: %u of %u words, system stack %u of %u words\n", used(stk_start, stk_end),
         (unsigned)(WORDS(stk_end) - WORDS(stk_start)), used(sstk_start, sstk_end), (unsigned)(WORDS(sstk_end) - WORDS(sstk_start)));
  printf("done\n");
  return 0;
}
