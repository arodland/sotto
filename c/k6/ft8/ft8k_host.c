/* ft8k_host: run the K6 FT8 receiver (ft8k.c) on the host, for the simulation and the tests.

     ft8k_host iq FILE NB          complex int16 pairs at the decimated rate (about 200 Hz);
                                   a row every 16 samples
     ft8k_host wf FILE NB          waterfall rows as a K6 running ft8wf sends them (NB / 2
                                   bytes a row, two 4-bit bins a byte, low nibble first)
     ft8k_host counts FILE NB R2 FS FMIX [INV]
                                   int8 GP0 counts at FS Hz (the K6's fs / L), mixed down from
                                   FMIX Hz with the phase the K6 would use, a row every 80 ms

   Prints one line a decode: "row bin snr score iters text". Options after the arguments:
   thr=N qs=N hann=0|1 fosr=0|1 tosr=0|1, r1=N sh1=N (counts: the first CIC), gap=MS (counts: zeros for MS after every fourth
   row call, as gp0rx.h fills the counts a K6 report line every 160 ms loses), and wfout=FILE (iq, counts): write each row as ft8wf
   would send it, decoding through fk_rowq + fk_putrow as the host does from a K6. MIT licence. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "ft8k.h"

static void report(const fk_t *k) {
  int i;
  for (i = 0; i < k->ndec; i++) {
    char t[64];
    fk_text(t, k->dec[i].msg);
    printf("%u %d %d %d %u %s\n", (unsigned)k->dec[i].row, k->dec[i].bin, k->dec[i].snr,
           k->dec[i].score, k->dec[i].iters, t);
  }
}

static void dump(const fk_t *k) {
  int j, nb = k->nb, sl = (k->slot + FK_ROWS - 1) % FK_ROWS, nf = nb - 7, bj = 0;
  if (!getenv("FK_DUMP")) return;
  fprintf(stderr, "%5u ", (unsigned)k->nrow);
  for (j = 0; j < nb; j++) {
    int b = k->wf[sl * (nb / 2) + j / 2];
    fprintf(stderr, "%x", j & 1 ? b >> 4 : b & 15);
  }
  for (j = 0; j < nf; j++)
    if (k->sc[2 * nf + j] > k->sc[2 * nf + bj]) bj = j;
  fprintf(stderr, " flo=%d best=%d@%d\n", (int)(k->flo >> 4), k->sc[2 * nf + bj], bj);
}

static FILE *wfo;
static double gap; /* counts: seconds of zeros after every 4th row call (a K6 report line) */

static void row(fk_t *k) {
  uint8_t r[FK_NBMAX / 2];
  if (!wfo) {
    if (fk_row(k)) report(k);
  } else if (fk_rowq(k, r)) {
    fwrite(r, 1, (size_t)(k->nb / 2), wfo);
    if (fk_putrow(k, r)) report(k);
  }
  dump(k);
}

static void opts(fk_t *k, int argc, char **argv, int a) {
  for (; a < argc; a++) {
    if (!strncmp(argv[a], "thr=", 4)) k->thr = (int16_t)atoi(argv[a] + 4);
    if (!strncmp(argv[a], "qs=", 3)) k->qs = (uint8_t)atoi(argv[a] + 3);
    if (!strncmp(argv[a], "hann=", 5)) k->hann = (uint8_t)atoi(argv[a] + 5);
    if (!strncmp(argv[a], "fosr=", 5)) k->fosr = (uint8_t)atoi(argv[a] + 5);
    if (!strncmp(argv[a], "tosr=", 5)) k->tosr = (uint8_t)atoi(argv[a] + 5);
    if (!strncmp(argv[a], "r1=", 3)) k->r1 = (uint8_t)atoi(argv[a] + 3);
    if (!strncmp(argv[a], "sh1=", 4)) k->sh1 = (uint8_t)atoi(argv[a] + 4);
    if (!strncmp(argv[a], "gap=", 4)) gap = atof(argv[a] + 4) / 1000;
    if (!strncmp(argv[a], "wfout=", 6) && !(wfo = fopen(argv[a] + 6, "wb")))
      perror(argv[a] + 6), exit(1);
  }
}

int main(int argc, char **argv) {
  static uint8_t wf[FK_WF_BYTES(FK_NBMAX)];
  static int16_t sc[FK_SC_WORDS(FK_NBMAX)];
  static uint8_t work[FK_WORK + 4];
  fk_t k;
  FILE *f;
  int nb, a;
  if (argc < 4) {
    fprintf(stderr, "usage: ft8k_host iq FILE NB | wf FILE NB | counts FILE NB R2 FS FMIX [INV]\n");
    return 2;
  }
  nb = atoi(argv[3]);
  f = fopen(argv[2], "rb");
  if (!f) return perror(argv[2]), 1;
  if (!strcmp(argv[1], "wf")) {
    uint8_t row[FK_NBMAX / 2];
    fk_init(&k, nb, 0, wf, sc, work);
    for (a = 4; a < argc; a++)
      if (!strncmp(argv[a], "thr=", 4)) k.thr = (int16_t)atoi(argv[a] + 4);
    while (fread(row, 1, (size_t)(nb / 2), f) == (size_t)(nb / 2))
      if (fk_putrow(&k, row)) report(&k);
  } else if (!strcmp(argv[1], "iq")) {
    int16_t s[2];
    long n = 0;
    fk_init(&k, nb, 0, wf, sc, work);
    opts(&k, argc, argv, 4);
    while (fread(s, 2, 2, f) == 2) {
      fk_sample(&k, s[0], s[1]);
      if (++n % (k.tosr ? 8 : 16) == 0) row(&k);
    }
  } else {
    int r2 = atoi(argv[4]), inv = argc > 7 && !strncmp(argv[7], "1", 1);
    double fs = atof(argv[5]), fmix = atof(argv[6]), t = 0, next = 0, dt;
    double ph = 0, dph = fmix / fs * 4294967296.0, skip = 0;
    int8_t x;
    long nr = 0;
    fk_init(&k, nb, r2, wf, sc, work);
    opts(&k, argc, argv, 7);
    dt = k.tosr ? 0.04 : 0.08;
    next = dt;
    while (fread(&x, 1, 1, f) == 1) {
      uint32_t p = (uint32_t)(long long)ph;
      fk_in(&k, t >= skip ? x : 0, inv ? 0u - p : p); /* a gap: zeros, keeping the clock */
      ph += dph;
      if (ph >= 4294967296.0) ph -= 4294967296.0;
      t += 1 / fs;
      if (t >= next) {
        next += dt;
        row(&k);
        if (gap > 0 && ++nr % 4 == 0) skip = t + gap;
      }
    }
  }
  fclose(f);
  if (wfo) fclose(wfo);
  return 0;
}
