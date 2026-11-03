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
#define GROUP 8                   /* packets per interleave group (both profiles) */
#define FPG_L (FPP_L * GROUP)     /* 64 frames per group */
#define FPG_S (2 * GROUP)         /* 16 */

static uint8_t pn9[SP_PAYLOAD_L];
static uint8_t last_pkt[SP_PAYLOAD_L];

static void pn9_init(void) {
  unsigned s = 0x1FF, i, b;
  for (i = 0; i < SP_PAYLOAD_L; i++) {
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
  int s_last_group;               /* profile S: group flagged last, -1 until seen */
  uint8_t *frames;                /* max_frames x 6 */
  uint8_t *good;                  /* bit per frame */
  uint8_t *seen;                  /* bit per packet (max_frames / 2 packets: profile S) */
} store_t;

static store_t S;
static struct sotto_dec *dec;
static int play_pos, play_end;
static int32_t emph1, emph2;

static int bit(const uint8_t *m, int i) { return (m[i >> 3] >> (i & 7)) & 1; }
static void set_bit(uint8_t *m, int i) { m[i >> 3] |= (uint8_t)(1u << (i & 7)); }

int sp_init(void *mem, unsigned bytes) {
  /* per frame: 6 bytes, 1 good bit, 1/2 seen bit (packets hold at least 2 frames) */
  unsigned n = (bytes * 16u) / (6u * 16u + 2u + 1u);
  pn9_init();
  memset(&S, 0, sizeof S);
  n &= ~15u;                      /* whole profile S groups; the bitmaps then round evenly */
  if (n > 65535u) n = 65520u;
  if (n == 0) return 0;
  S.max_frames = (int)n;
  S.frames = (uint8_t *)mem;
  S.good = S.frames + 6u * n;
  S.seen = S.good + n / 8u;
  S.s_last_group = -1;
  return (int)n;
}

static void clear_message(uint8_t msg_id, uint8_t profile) {
  uint16_t gp = S.st.good_packets, bp = S.st.bad_packets;
  memset(S.good, 0, (unsigned)S.max_frames / 8u);
  memset(S.seen, 0, (unsigned)S.max_frames / 16u);
  memset(&S.st, 0, sizeof S.st);
  S.st.good_packets = gp, S.st.bad_packets = bp;
  S.st.have = 1, S.st.msg_id = msg_id, S.st.profile = profile;
  S.s_last_group = -1;
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

static int packet_l(const uint8_t *p) {
  int seq = p[2] << 8 | p[3], total = p[4] << 8 | p[5], s, was_new = 0;
  unsigned before;
  if (!S.st.have || S.st.profile != 'L' || p[1] != S.st.msg_id || total != S.st.total) {
    clear_message(p[1], 'L');
    S.st.total = (uint16_t)total;
    S.st.stored = (uint16_t)(total < S.max_frames ? total : S.max_frames);
    S.st.packets_total = (uint16_t)((total + FPG_L - 1) / FPG_L * GROUP);
    was_new = 1;
  }
  before = S.st.frames_got;
  for (s = 0; s < FPP_L; s++) {
    long f = (long)(seq / GROUP) * FPG_L + s * GROUP + seq % GROUP;
    if (f < S.st.stored) put_frame((int)f, p + 6 + 6 * s);
  }
  return finish_packet(seq, was_new, before);
}

static int packet_s(const uint8_t *p) {
  int id = p[0] >> 4, last = (p[0] >> 3) & 1, seq = (p[0] & 7) << 8 | p[1], g = seq / GROUP, k = seq % GROUP;
  int was_new = 0, j;
  unsigned before;
  if (!S.st.have || S.st.profile != 'S' || id != S.st.msg_id) {
    clear_message((uint8_t)id, 'S');
    S.st.stored = (uint16_t)S.max_frames;        /* until the last group is known */
    was_new = 1;
  }
  if (last && S.s_last_group < 0) {
    long total = (long)(g + 1) * FPG_S;
    S.s_last_group = g;
    S.st.total = (uint16_t)(total < 65535 ? total : 65535);
    S.st.stored = (uint16_t)(total < S.max_frames ? total : S.max_frames);
    S.st.packets_total = (uint16_t)((g + 1) * GROUP);
    /* frames_got may already count frames past the end: recount */
    S.st.frames_got = 0;
    for (j = 0; j < S.max_frames; j++)
      if (bit(S.good, j)) {
        if (j < S.st.stored) S.st.frames_got++;
      }
  }
  before = S.st.frames_got;
  if ((long)g * FPG_S + k < S.st.stored) put_frame(g * FPG_S + k, p + 2);
  if ((long)g * FPG_S + 8 + k < S.st.stored) put_frame(g * FPG_S + 8 + k, p + 8);
  return finish_packet(seq, was_new, before);
}

int sp_packet(const uint8_t *payload, int len) {
  int i, r;
  if (len != SP_PAYLOAD_L && len != SP_PAYLOAD_S) return SP_BAD_LENGTH;
  if (!pn9[0]) pn9_init();
  for (i = 0; i < len; i++) last_pkt[i] = payload[i] ^ pn9[i];
  for (; i < SP_PAYLOAD_L; i++) last_pkt[i] = 0;
  if (len == SP_PAYLOAD_L && last_pkt[0] != SP_MAGIC) { S.st.bad_packets++; return SP_BAD_MAGIC; }
  if (crc16(last_pkt, len - 2) != ((unsigned)last_pkt[len - 2] << 8 | last_pkt[len - 1])) {
    S.st.bad_packets++;
    return SP_BAD_CRC;
  }
  S.st.good_packets++;
  if (!S.max_frames) return SP_DUP;
  r = len == SP_PAYLOAD_L ? packet_l(last_pkt) : packet_s(last_pkt);
  return r;
}

const uint8_t *sp_last_packet(void) { return last_pkt; }

void sp_status(sp_status_t *st) { *st = S.st; }

/* ---- playback ---------------------------------------------------------------------------- */
void sp_play_start(void) {
  if (dec) sotto_dec_destroy(dec);
  dec = sotto_dec_create();
  play_pos = 0;
  play_end = S.st.have ? S.st.stored : 0;
  if (S.st.profile == 'S') {
    /* the sender pads the last group with zero frames: drop them, and frames past the
       last one that arrived if the last group never did */
    static const uint8_t zero[6];
    while (play_end > 0 && (!bit(S.good, play_end - 1) || !memcmp(S.frames + 6 * (play_end - 1), zero, 6))) play_end--;
  }
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
  static int16_t pcm[SP_FRAME_SAMPLES];
  if (!sp_play_frame(pcm)) return 0;
  sp_pcm_to_pwm(pcm, out, SP_FRAME_SAMPLES, top, gain_q8, emphasis);
  return 1;
}

void sp_test_packet(uint8_t out[SP_PAYLOAD_L]) {
  uint8_t p[SP_PAYLOAD_L];
  int s, i;
  unsigned c;
  if (!pn9[0]) pn9_init();
  memset(p, 0, sizeof p);
  p[0] = SP_MAGIC, p[1] = 0x42, p[5] = 70;          /* seq 0, total 70 frames */
  for (s = 0; s < FPP_L; s++) {                      /* packet 0: frames 0, 8, ..., 56 */
    int f = s * GROUP;
    uint8_t *q = p + 6 + 6 * s;
    q[0] = (uint8_t)f, q[1] = (uint8_t)(f >> 8), q[2] = 0x11, q[3] = 0x22, q[4] = 0x33, q[5] = 0x44;
  }
  c = crc16(p, 62);
  p[62] = (uint8_t)(c >> 8), p[63] = (uint8_t)c;
  for (i = 0; i < SP_PAYLOAD_L; i++) out[i] = p[i] ^ pn9[i];
}

unsigned sp_static_bytes(void) {
  return (unsigned)(sizeof dec_mem + sizeof pn9 + sizeof last_pkt + sizeof S + sizeof dec + 4 * sizeof(int)
                    + 2 * sizeof(int32_t) + 2 * SP_FRAME_SAMPLES
#ifdef FX_OA_PACKED
                    + 2048u                       /* fx_cs512's table (fx_math.c) */
#endif
                    );
}
