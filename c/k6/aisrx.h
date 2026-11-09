/* aisrx.h: the AIS receiver on the M0, in integers (docs/k6-ais.md; sim/k6if/ais.py's receive is
   the reference). It decodes one burst recorded whole by the polled capture (c/k6/mod/aispoll.h):
   the counts x[0..n) (ones - 128 of 256 reads of PB1), the trigger at x[pre], and c1, the CPU's
   cycles over the n - pre counts from the trigger on (TIM17, 96 MHz), which gives the count rate.

   Per start point s = pre - AR_BACKS[i] (just before the trigger: the burst starts within ~0.5
   ms of it, and near the threshold each start's offset estimate and CIC phase decode different
   bursts):
     counts -> mixed down by the IF (16.927 kHz, a 64-entry sine) -> 2nd-order CIC by 3
     (~27 kHz, ~2.8 samples a bit) -> CORDIC phase (2^16 a turn) and magnitude -> the mean phase
     step over the samples above half the mean magnitude (the carrier's offset from the IF) comes
     off -> at each of 8 clock phases, a bit is the sign of the phase advance over it -> NRZI ->
     HDLC (flags, unstuffing) -> CRC-16/X.25 (residue 0xF0B8).
   Every frame with a good CRC goes into f[], once. A frame's bytes are the message's bytes in
   order (each byte goes out least significant bit first, so collecting the bits LSB first gives
   them back), then the FCS. pos is the start flag's first bit, in counts from x[0], Q16.

   ph is the caller's work array of AR_NPH(n, pre) halfwords. No statics, no division at run time
   but ar_udiv/ar_muldiv, nothing big on the stack. SPDX-License-Identifier: MIT */
#ifndef AISRX_H
#define AISRX_H
#include <stdint.h>

#define AR_DEC 3
#define AR_MAXB 40 /* bytes a frame (a one-slot message is at most 256 bits with its FCS) */
#define AR_MINB 11 /* 72 bits and the FCS */
#define AR_NPH(n, pre) (((n) - (pre) + 256u) / AR_DEC + 2u)
/* 2^32 x 16927.083 Hz / 96 MHz: the IF's phase step a CPU cycle, Q32 */
#define AR_IF_Q32 757305u

typedef struct {
  uint8_t b[AR_MAXB];
  int nbytes; /* message bytes, the FCS after them */
  uint32_t pos;
} ar_frame_t;

static uint32_t ar_udiv(uint32_t n, uint32_t d) {
  uint32_t q = 0, bit = 1;
  if (!d) return 0;
  while (d < n && !(d & 0x80000000u)) d <<= 1, bit <<= 1;
  while (bit) {
    if (n >= d) n -= d, q |= bit;
    d >>= 1, bit >>= 1;
  }
  return q;
}

/* a * b / c in 64 bits by hand (an M0 module has no run-time library), c < 2^31 */
static uint32_t ar_muldiv(uint32_t a, uint32_t b, uint32_t c) {
  uint32_t al = a & 0xFFFF, ah = a >> 16, bl = b & 0xFFFF, bh = b >> 16;
  uint32_t lo = al * bl, m1 = al * bh, m2 = ah * bl, hi = ah * bh, t, r = 0, q = 0;
  int i;
  t = lo + (m1 << 16);
  hi += (m1 >> 16) + (t < lo);
  lo = t, t = lo + (m2 << 16);
  hi += (m2 >> 16) + (t < lo);
  lo = t;
  for (i = 0; i < 64; i++) {
    r = (r << 1) | (hi >> 31);
    hi = (hi << 1) | (lo >> 31);
    lo <<= 1;
    q <<= 1;
    if (r >= c) r -= c, q |= 1;
  }
  return q;
}

static const int8_t AR_SIN[64] = {
    0,   12,  25,  37,  49,  60,  71,  81,  90,  98,  106, 112, 117, 122, 125, 126,
    127, 126, 125, 122, 117, 112, 106, 98,  90,  81,  71,  60,  49,  37,  25,  12,
    0,   -12, -25, -37, -49, -60, -71, -81, -90, -98, -106, -112, -117, -122, -125, -126,
    -127, -126, -125, -122, -117, -112, -106, -98, -90, -81, -71, -60, -49, -37, -25, -12};
/* atan(2^-i) in 2^16 a turn */
static const uint16_t AR_ATN[11] = {8192, 4836, 2555, 1297, 651, 326, 163, 81, 41, 20, 10};

/* counts x[0..n) through the mixer, CIC and CORDIC: the samples' phases into ph (when ph), the
   samples' count back. Returns the sum of the magnitudes / 16, and with thr > 0 also the sum of
   the phase steps (*sum) over the *non samples whose magnitude is over thr */
static uint32_t ar_pass(const int8_t *x, uint32_t n, uint32_t dph, uint16_t *ph, int32_t thr,
                        int32_t *sum, uint32_t *non, uint32_t *ns) {
  uint32_t p = 0, i, k = 0, msum = 0, m = 0;
  int32_t i1 = 0, i2 = 0, q1 = 0, q2 = 0, ci = 0, cq = 0, di = 0, dq = 0;
  uint16_t last = 0;
  *sum = 0, *non = 0;
  for (i = 0; i < n; i++) {
    uint32_t ix = p >> 26;
    int32_t c = x[i];
    p += dph;
    i1 += c * AR_SIN[(ix + 16) & 63], i2 += i1;
    q1 -= c * AR_SIN[ix], q2 += q1;
    if (++k == AR_DEC) {
      int32_t a = i2 - ci, b = q2 - cq, xx = a - di, yy = b - dq, t, j;
      uint32_t ang = 0;
      ci = i2, cq = q2, di = a, dq = b, k = 0;
      if (xx < 0) xx = -xx, yy = -yy, ang = 32768;
      for (j = 0; j < 11; j++) {
        t = xx;
        if (yy > 0) xx += yy >> j, yy -= t >> j, ang += AR_ATN[j];
        else xx -= yy >> j, yy += t >> j, ang -= AR_ATN[j];
      }
      msum += (uint32_t)xx >> 4;
      if (thr > 0 && xx > thr && m) *sum += (int16_t)(uint16_t)(ang - last), (*non)++;
      last = (uint16_t)ang;
      if (ph) ph[m] = last;
      m++;
    }
  }
  *ns = m;
  return msum;
}

typedef struct {
  uint32_t run, in, nb, start;
  uint8_t b[AR_MAXB];
} ar_hd_t;

static int ar_crc_ok(const uint8_t *b, int n) {
  uint32_t r = 0xFFFF;
  int i, j;
  for (i = 0; i < n; i++) {
    r ^= b[i];
    for (j = 0; j < 8; j++) r = (r & 1) ? (r >> 1) ^ 0x8408 : r >> 1;
  }
  return r == 0xF0B8;
}

/* keep a good frame once; returns the frames kept */
static int ar_keep(const ar_hd_t *h, uint32_t pos, ar_frame_t *f, int nf, int maxf) {
  int m = (int)((h->nb - 6) >> 3), i, j;
  for (i = 0; i < nf; i++) {
    if (f[i].nbytes != m - 2) continue;
    for (j = 0; j < m && f[i].b[j] == h->b[j]; j++) {
    }
    if (j == m) return nf;
  }
  if (nf >= maxf) return nf;
  for (j = 0; j < m; j++) f[nf].b[j] = h->b[j];
  f[nf].nbytes = m - 2;
  f[nf].pos = pos;
  return nf + 1;
}

/* one raw bit (1: no change of level) at level index L; a closing flag with a good CRC keeps the
   frame (its opening flag's first bit at h->start). Returns 1 for a good frame */
static int ar_bit(ar_hd_t *h, uint32_t bit, uint32_t L) {
  int good = 0;
  if (bit) {
    h->run++;
    if (h->run == 7) h->in = 0; /* an abort */
    if (h->run >= 6) return 0;
  } else {
    if (h->run == 6) { /* a flag */
      if (h->in && h->nb >= 6 + 8 * AR_MINB && !((h->nb - 6) & 7) &&
          ar_crc_ok(h->b, (int)((h->nb - 6) >> 3)))
        good = 1;
      else
        h->in = 1, h->nb = 0, h->start = L - 7;
      h->run = 0;
      return good;
    }
    if (h->run == 5) { /* a stuffed zero */
      h->run = 0;
      return 0;
    }
    h->run = 0;
  }
  if (!h->in) return 0;
  if (h->nb >= 8 * AR_MAXB) {
    h->in = 0;
    return 0;
  }
  if (!(h->nb & 7)) h->b[h->nb >> 3] = 0;
  h->b[h->nb >> 3] |= (uint8_t)(bit << (h->nb & 7));
  h->nb++;
  return 0;
}

/* the phase (2^16 a turn) at sample t (Q16), between samples */
static uint16_t ar_at(const uint16_t *ph, uint32_t t) {
  uint32_t i = t >> 16, fr = t & 0xFFFF;
  int32_t d = (int16_t)(uint16_t)(ph[i + 1] - ph[i]);
  return (uint16_t)(ph[i] + ((d * (int32_t)fr) >> 16));
}

static const uint16_t AR_BACKS[4] = {32, 64, 128, 256};

/* if_q32: where the burst sits in the counts, 2^32 x Hz / 96 MHz (AR_IF_Q32 when tuned to the
   channel; a station D Hz off the tuned frequency lands at |16927 + D| Hz, folded at the count
   rate, mirrored when below zero, which NRZI doesn't mind: docs/k6-ais.md run 5) */
static int ar_decode_if(const int8_t *x, uint32_t n, uint32_t pre, uint32_t c1, uint32_t if_q32,
                        uint16_t *ph, ar_frame_t *f, int maxf) {
  uint32_t dph, bl, si, ns, non, msum, j, t, mean, corr;
  int32_t sum;
  int nf = 0;
  ar_hd_t h;
  if (n <= pre + 256 || !c1) return 0;
  dph = ar_muldiv(c1, if_q32, n - pre);
  /* samples a bit, Q16: 96 MHz (n - pre) / c1 / 3 / 9600 */
  bl = ar_muldiv(10000u * (n - pre), 65536u, 3u * c1);
  for (si = 0; si < 4; si++) {
    uint32_t s = pre - AR_BACKS[si];
    if (AR_BACKS[si] > pre) continue;
    msum = ar_pass(x + s, n - s, dph, 0, 0, &sum, &non, &ns);
    ar_pass(x + s, n - s, dph, ph, (int32_t)ar_udiv(msum, ns) << 3, &sum, &non, &ns);
    if (non < 16 || ns < 8) continue;
    /* the carrier's offset: the mean phase step, Q8, off every sample */
    mean = ar_muldiv((uint32_t)(sum < 0 ? -sum : sum), 256u, non);
    if (sum < 0) mean = 0u - mean;
    corr = 0;
    for (j = 0; j < ns; j++) ph[j] = (uint16_t)(ph[j] - (corr >> 8)), corr += mean;
    for (j = 0; j < 8; j++) {
      uint32_t L = 0, lev = 2;
      h.run = 0, h.in = 0, h.nb = 0, h.start = 0;
      for (t = (bl >> 1) + ((bl * j) >> 3); t + (bl >> 1) < ((ns - 1) << 16); t += bl, L++) {
        uint32_t v = (int16_t)(uint16_t)(ar_at(ph, t + (bl >> 1)) - ar_at(ph, t - (bl >> 1))) > 0;
        if (lev < 2 && ar_bit(&h, v == lev, L)) {
          /* the opening flag's first bit, in counts from x[0], Q16 */
          uint32_t tb = (bl >> 1) + ((bl * j) >> 3) + h.start * bl - (bl >> 1);
          nf = ar_keep(&h, (s << 16) + tb * AR_DEC, f, nf, maxf);
          h.in = 1, h.nb = 0, h.start = L - 7; /* the closing flag opens the next */
        }
        lev = v;
      }
    }
  }
  return nf;
}

#define ar_decode(x, n, pre, c1, ph, f, maxf) ar_decode_if(x, n, pre, c1, AR_IF_Q32, ph, f, maxf)

/* bits a..a+n-1 of a message (MSB first), n <= 32 */
__attribute__((unused)) static uint32_t ar_uint(const uint8_t *b, uint32_t a, uint32_t n) {
  uint32_t v = 0;
  while (n--) v = (v << 1) | ((b[a >> 3] >> (7 - (a & 7))) & 1), a++;
  return v;
}

#endif
