/* sotto voice pager: packet reassembly in RAM and playback. See sotto_pager.h.
 * SPDX-License-Identifier: MIT */
#include <stddef.h>
#include <string.h>
#include "sotto_pager.h"
#include "../sotto.h"
#include "../fixed/fx.h"            /* FX_OA_PACKED: whether fx_cs512's 2 KB table is in RAM */

/* ---- the decoder's one allocation: sotto_fx_dec.c is built with -Dcalloc=sp_calloc
   -Dfree=sp_free, so its state lives here instead of on a heap ------------------------- */
#define SP_DEC_MAX 864               /* sotto_fx_dec_bytes(): 836 on the M0 and x86 */
static uint32_t dec_mem[SP_DEC_MAX / 4];
static int dec_taken;
void *sp_calloc(size_t n, size_t size);
void sp_free(void *p);
void *sp_calloc(size_t n, size_t size) {
  if (dec_taken || n * size > sizeof dec_mem) return 0;
  dec_taken = 1;
  memset(dec_mem, 0, sizeof dec_mem);
  return dec_mem;
}
void sp_free(void *p) { if (p == (void *)dec_mem) dec_taken = 0; }

/* ---- packet format v1 ------------------------------------------------------------------- */
#define FPP_L 8                   /* frames per packet, profile L */
#define FPP_XL 20                 /* profile XL: 6 + 20 x 6 + 2 = 128 bytes, nothing reserved */
#define GROUP 8                   /* packets per interleave group (both profiles) */
#define HDR 6                     /* magic, msg_id, seq (2), total (2) */
#define TEST_FRAMES 70            /* the spec's test message (section 10) */

static uint8_t pn9[SP_PAYLOAD_MAX];
static uint8_t last_pkt[SP_PAYLOAD_MAX];

static int fpp_of(int len) { return len == SP_PAYLOAD_XL ? FPP_XL : FPP_L; }

static void pn9_init(void) {
  unsigned s = 0x1FF, i, b;
  for (i = 0; i < SP_PAYLOAD_MAX; i++) {
    pn9[i] = (uint8_t)s;
    for (b = 0; b < 8; b++) s = (s >> 1) | (((s ^ (s >> 5)) & 1u) << 8);
  }
}

static unsigned crc16(const uint8_t *p, int n) {
  unsigned c = 0xFFFF;
  int i, b;
  for (i = 0; i < n; i++) {
    c ^= (unsigned)p[i] << 8;
    for (b = 0; b < 8; b++) c = (c & 0x8000) ? ((c << 1) ^ 0x1021) & 0xFFFF : (c << 1) & 0xFFFF;
  }
  return c;
}

/* ---- the store -------------------------------------------------------------------------- */
typedef struct {
  sp_status_t st;
  int max_frames;
  uint8_t *frames;                /* max_frames x 6 */
  uint8_t *good;                  /* bit per frame */
  uint8_t *seen;                  /* bit per packet (up to max_frames / 2 packets) */
} store_t;

static store_t S;
static struct sotto_dec *dec;
static int play_pos, play_end;
static int32_t emph1, emph2;

static int bit(const uint8_t *m, int i) { return (m[i >> 3] >> (i & 7)) & 1; }
static void set_bit(uint8_t *m, int i) { m[i >> 3] |= (uint8_t)(1u << (i & 7)); }

int sp_init(void *mem, unsigned bytes) {
  /* per frame: 6 bytes, 1 good bit, 1/2 seen bit (room for 2-frame packets, as the retired
     profile S had; the store size stays as it was) */
  unsigned n = (bytes * 16u) / (6u * 16u + 2u + 1u);
  pn9_init();
  memset(&S, 0, sizeof S);
  memset(mem, 0, bytes);          /* the good/seen bitmaps start clear (the K6 keeps mem in NOLOAD RAM) */
  n &= ~15u;                      /* the bitmaps round evenly */
  if (n > 65535u) n = 65520u;
  if (n == 0) return 0;
  S.max_frames = (int)n;
  S.frames = (uint8_t *)mem;
  S.good = S.frames + 6u * n;
  S.seen = S.good + n / 8u;
  return (int)n;
}

static void clear_message(uint8_t msg_id, uint8_t profile) {
  uint16_t gp = S.st.good_packets, bp = S.st.bad_packets;
  memset(S.good, 0, (unsigned)S.max_frames / 8u);
  memset(S.seen, 0, (unsigned)S.max_frames / 16u);
  memset(&S.st, 0, sizeof S.st);
  S.st.good_packets = gp, S.st.bad_packets = bp;
  S.st.have = 1, S.st.msg_id = msg_id, S.st.profile = profile;
}

static void put_frame(int f, const uint8_t *src) {
  if (f >= S.st.stored || bit(S.good, f)) return;
  memcpy(S.frames + 6 * f, src, 6);
  set_bit(S.good, f);
  S.st.frames_got++;
}

static int finish_packet(int seq, int was_new, unsigned before) {
  if (seq < S.max_frames / 2 && !bit(S.seen, seq)) set_bit(S.seen, seq), S.st.packets_got++;
  if (S.st.stored && S.st.frames_got == S.st.stored) {
    int done = !S.st.complete;
    S.st.complete = 1;
    if (done && !was_new) return SP_COMPLETE;
  }
  if (was_new) return SP_NEW;
  return S.st.frames_got != before ? SP_ADDED : SP_DUP;
}

/* profile L or XL (fpp frames per packet), checked and de-whitened */
static int packet(const uint8_t *p, int fpp, uint8_t profile) {
  int seq = p[2] << 8 | p[3], total = p[4] << 8 | p[5], s, was_new = 0, fpg = fpp * GROUP;
  unsigned before;
  if (!S.st.have || S.st.profile != profile || p[1] != S.st.msg_id || total != S.st.total) {
    clear_message(p[1], profile);
    S.st.total = (uint16_t)total;
    S.st.stored = (uint16_t)(total < S.max_frames ? total : S.max_frames);
    S.st.packets_total = (uint16_t)((total + fpg - 1) / fpg * GROUP);
    was_new = 1;
  }
  before = S.st.frames_got;
  for (s = 0; s < fpp; s++) {
    long f = (long)(seq / GROUP) * fpg + s * GROUP + seq % GROUP;
    if (f < S.st.stored) put_frame((int)f, p + HDR + 6 * s);
  }
  return finish_packet(seq, was_new, before);
}

int sp_packet(const uint8_t *payload, int len) {
  int i, r;
  if (len != SP_PAYLOAD_L && len != SP_PAYLOAD_XL) return SP_BAD_LENGTH;
  if (!pn9[0]) pn9_init();
  for (i = 0; i < len; i++) last_pkt[i] = payload[i] ^ pn9[i];
  for (; i < SP_PAYLOAD_MAX; i++) last_pkt[i] = 0;
  if (last_pkt[0] != SP_MAGIC) { S.st.bad_packets++; return SP_BAD_MAGIC; }
  if (crc16(last_pkt, len - 2) != ((unsigned)last_pkt[len - 2] << 8 | last_pkt[len - 1])) {
    S.st.bad_packets++;
    return SP_BAD_CRC;
  }
  S.st.good_packets++;
  if (!S.max_frames) return SP_DUP;
  r = packet(last_pkt, fpp_of(len), len == SP_PAYLOAD_XL ? 'X' : 'L');
  return r;
}

int sp_check(const uint8_t *payload, int len) {
  int i;
  unsigned c = 0xFFFF;
  if (len != SP_PAYLOAD_L && len != SP_PAYLOAD_XL) return 0;
  if (!pn9[0]) pn9_init();
  if ((payload[0] ^ pn9[0]) != SP_MAGIC) return 0;
  for (i = 0; i < len - 2; i++) {     /* the CRC as crc16(), de-whitening on the way: no copy */
    int b;
    c ^= (unsigned)(payload[i] ^ pn9[i]) << 8;
    for (b = 0; b < 8; b++) c = (c & 0x8000) ? ((c << 1) ^ 0x1021) & 0xFFFF : (c << 1) & 0xFFFF;
  }
  return c == ((unsigned)(payload[len - 2] ^ pn9[len - 2]) << 8 | (payload[len - 1] ^ pn9[len - 1]));
}

int sp_control(const uint8_t *payload, int len, uint8_t rec[6]) {
  int how, o, i;
  uint8_t r[SP_CTL_REC];
  if (len != SP_PAYLOAD_L && len != SP_PAYLOAD_XL) return 0;
  if (!pn9[0]) pn9_init();
  for (how = 0; how < 4; how++)
    for (o = 0; o + SP_CTL_REC <= len; o += SP_CTL_REC) {
      for (i = 0; i < SP_CTL_REC; i++)
        r[i] = (uint8_t)(payload[(o + i) ^ (how & 1)] ^ (how & 2 ? 0xFF : 0) ^ pn9[o + i]);
      if (r[0] == SP_CTL_MAGIC && crc16(r, 6) == ((unsigned)r[6] << 8 | r[7])) {
        for (i = 0; i < 6; i++) rec[i] = r[i];
        return 1 + how;
      }
    }
  return 0;
}

const uint8_t *sp_last_packet(void) { return last_pkt; }

void sp_status(sp_status_t *st) { *st = S.st; }

int sp_missing(uint16_t *out, int max) {
  int n = 0;
  for (unsigned q = 0; q < S.st.packets_total && q < (unsigned)S.max_frames / 2u; q++)
    if (!bit(S.seen, q)) {
      if (n < max) out[n] = (uint16_t)q;
      n++;
    }
  return n;
}

/* ---- playback ---------------------------------------------------------------------------- */
void sp_play_start(void) {
  if (dec) sotto_dec_destroy(dec);
  dec = sotto_dec_create();
  play_pos = 0;
  play_end = S.st.have ? S.st.stored : 0;
  emph1 = emph2 = 0;
}

int sp_play_frame(int16_t pcm[SP_FRAME_SAMPLES]) {
  if (!dec) return 0;
  if (play_pos < play_end) {
    sotto_decode(dec, S.frames + 6 * play_pos, bit(S.good, play_pos), pcm);
    play_pos++;
    return 1;
  }
  if (play_pos == play_end) {     /* the decoder's last 20 ms */
    sotto_dec_flush(dec, pcm);
    play_pos++;
    return 1;
  }
  return 0;
}

int sp_play_pos(void) { return play_pos < play_end ? play_pos : play_end; }

void sp_pcm_to_pwm(const int16_t *pcm, uint16_t *out, int n, unsigned top, int gain_q8, int emphasis) {
  int i;
  int32_t half = (int32_t)(top / 2);
  for (i = 0; i < n; i++) {
    int32_t x = pcm[i], y = x, t;
    if (emphasis) {
      y = (33 * x - 21 * emph1 + 4 * emph2) >> 4;
      emph2 = emph1, emph1 = x;
    }
    t = (y * gain_q8) >> 8;
    if (t > 32767) t = 32767;
    if (t < -32767) t = -32767;
    t = half + ((t * half) >> 15);
    out[i] = (uint16_t)(t < 0 ? 0 : t > (int32_t)top ? (int32_t)top : t);
  }
}

/* 256 x 10^(g / 20) with g = -13 dBFS - (the energy code's level), within -6..+18 dB; code c
   stands for 10 + 70 c / 31 - 87.3 dBFS (sotto.h, "Level") */
static const uint16_t auto_gain[32] = {
  2033, 2033, 2033, 2033, 2033, 2033, 2033, 2033, 2033, 2033, 2033, 2033, 2033, 2033, 2033, 2033,
  2033, 2033, 2033, 2033, 2033, 1788, 1378, 1063, 820, 632, 487, 376, 290, 223, 172, 133};

int sp_auto_gain_q8(void) {
  int f, top = -1;
  for (f = 0; f < S.st.stored; f++)
    if (bit(S.good, f)) {
      const uint8_t *q = S.frames + 6 * f;
      int e = ((q[1] << 8 | q[2]) >> 10) & 31;      /* bits 9..13: voicing 2 | Wo 7 | energy 5 */
      if (e > top) top = e;
    }
  return top < 0 ? 256 : auto_gain[top];
}

int sp_play_pwm(uint16_t out[SP_FRAME_SAMPLES], unsigned top, int gain_q8, int emphasis) {
  int16_t *pcm = (int16_t *)out;   /* decoded in place: the conversion reads each sample before
                                      writing it (320 bytes less RAM on the K6) */
  if (!sp_play_frame(pcm)) return 0;
  sp_pcm_to_pwm(pcm, out, SP_FRAME_SAMPLES, top, gain_q8, emphasis);
  return 1;
}

/* packet seq of the spec's 70-frame test message (section 10: frame f is f as 16 bits little
   endian, then 11 22 33 44; msg_id 0x42) in profile L or XL (len bytes), whitened */
static void test_packet(uint8_t *out, int len, int seq) {
  uint8_t p[SP_PAYLOAD_MAX];
  int slots = fpp_of(len), s, i;
  unsigned c;
  if (!pn9[0]) pn9_init();
  memset(p, 0, sizeof p);
  p[0] = SP_MAGIC, p[1] = 0x42, p[2] = (uint8_t)(seq >> 8), p[3] = (uint8_t)seq, p[5] = TEST_FRAMES;
  for (s = 0; s < slots; s++) {
    int f = seq / GROUP * slots * GROUP + s * GROUP + seq % GROUP;
    uint8_t *q = p + HDR + 6 * s;
    if (f < TEST_FRAMES) q[0] = (uint8_t)f, q[1] = (uint8_t)(f >> 8), q[2] = 0x11, q[3] = 0x22, q[4] = 0x33, q[5] = 0x44;
  }
  c = crc16(p, len - 2);
  p[len - 2] = (uint8_t)(c >> 8), p[len - 1] = (uint8_t)c;
  for (i = 0; i < len; i++) out[i] = p[i] ^ pn9[i];
}

void sp_test_packet(uint8_t out[SP_PAYLOAD_L]) { test_packet(out, SP_PAYLOAD_L, 0); }

static int ones(unsigned v) {
  static const uint8_t n4[16] = {0, 1, 1, 2, 1, 2, 2, 3, 1, 2, 2, 3, 2, 3, 3, 4};
  return n4[v & 15] + n4[v >> 4 & 15];
}

int sp_test_errors(const uint8_t *payload, int len, sp_biterr_t *e) {
  uint8_t t[SP_PAYLOAD_MAX];
  int npk, fpg, seq, how, i, n, best_seq = 0;
  memset(e, 0, sizeof *e);
  e->errors = -1;
  if (len != SP_PAYLOAD_L && len != SP_PAYLOAD_XL) return -1;
  fpg = fpp_of(len) * GROUP;
  npk = (TEST_FRAMES + fpg - 1) / fpg * GROUP;        /* 16 in L, 8 in XL */
  for (seq = 0; seq < npk; seq++) {
    test_packet(t, len, seq);
    for (how = 0; how < 4; how++) {                  /* bit 0: bytes swapped, bit 1: inverted */
      unsigned x = how & 2 ? 0xFF : 0;
      for (i = n = 0; i < len; i++) n += ones(payload[i ^ (how & 1)] ^ t[i] ^ x);
      if (e->errors < 0 || n < e->errors)
        e->errors = (int16_t)n, e->seq = (int16_t)seq, e->swap = how & 1, e->invert = how >> 1, best_seq = seq;
    }
  }
  test_packet(t, len, best_seq);                      /* again, instead of a second buffer */
  for (i = 0; i < len; i++)
    e->block[i * 8 / len] += (uint8_t)ones(payload[i ^ e->swap] ^ t[i] ^ (e->invert ? 0xFF : 0));
  return e->errors;
}

unsigned sp_static_bytes(void) {
  return (unsigned)(sizeof dec_mem + sizeof pn9 + sizeof last_pkt + sizeof S + sizeof dec + 4 * sizeof(int)
                    + 2 * sizeof(int32_t) + 2 * SP_FRAME_SAMPLES
#ifdef FX_OA_PACKED
                    + 2048u                       /* fx_cs512's table (fx_math.c) */
#endif
                    );
}
