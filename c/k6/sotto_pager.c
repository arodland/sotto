/* sotto voice pager: packet reassembly in RAM and playback. See sotto_pager.h.
 * SPDX-License-Identifier: MIT */
#include <stddef.h>
#include <string.h>
#include "sotto_pager.h"
#include "sp_cb.h"
#include "sp_vlr.h"
#include "../sotto.h"
#ifdef SP_VLR
#include "../sotto_vlr.h"
#ifndef SP_LR
#error "SP_VLR builds on SP_LR: the image plays sotto 1200, 600 and 400"
#endif
#endif
#include "../fixed/fx.h"            /* FX_OA_PACKED: whether fx_cs512's 2 KB table is in RAM */

/* ---- the decoder's allocations: sotto_fx_dec.c (and sotto_fx_lr.c) are built with
   -Dcalloc=sp_calloc -Dfree=sp_free, so their state lives here instead of on a heap: one
   block for v3.7 (the core), two for sotto 1200 (its own state, then the core). A bump pool,
   emptied when every block is freed ---------------------------------------------------- */
#define SP_DEC_CORE 864              /* sotto_fx_dec_bytes(): 836 on the M0 and x86 */
#if defined(SP_VLR)
#define SP_DEC_MAX (SP_DEC_CORE + 640)  /* + struct sotto_vlr_dec (sotto_vlr_dec_bytes(): 580 B on
                                           the M0), or struct sotto_lr_dec: one at a time */
#elif defined(SP_LR)
#define SP_DEC_MAX (SP_DEC_CORE + 320)  /* + struct sotto_lr_dec (300 B on the M0) */
#else
#define SP_DEC_MAX SP_DEC_CORE
#endif
#ifdef FX_YIELD_HOOK
void (*volatile fx_yield)(void);
void sp_set_yield(void (*f)(void)) { fx_yield = f; }
void sp_yield_probe(void) {}         /* a do-nothing yield, for c/k6/bench_k6.py's gap count */
#endif
static uint32_t dec_mem[SP_DEC_MAX / 4];
static unsigned dec_used, dec_blocks;
void *sp_calloc(size_t n, size_t size);
void sp_free(void *p);
void *sp_calloc(size_t n, size_t size) {
  size_t b = (n * size + 3u) & ~(size_t)3u;
  void *p;
  if (b > sizeof dec_mem - dec_used) return 0;
  p = (uint8_t *)dec_mem + dec_used;
  memset(p, 0, b);
  dec_used += (unsigned)b, dec_blocks++;
  return p;
}
void sp_free(void *p) {
  if (p && dec_blocks && --dec_blocks == 0) dec_used = 0;
}

/* ---- packet format v1 ------------------------------------------------------------------- */
#define FPP_L 8                   /* frames per packet, profile L */
#define FPP_XL 20                 /* profile XL: 6 + 20 x 6 + 2 = 128 bytes, nothing reserved */
#define GROUP 8                   /* packets per interleave group: the default, and the test message's */
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
  uint8_t magic;                  /* the message's codec */
  int max_frames;
  uint8_t *frames;                /* max_frames x 6 */
  uint8_t *good;                  /* bit per frame */
  uint8_t *seen;                  /* bit per packet (up to max_frames / 2 packets) */
} store_t;

static store_t S;
#ifdef SP_LR
static struct sotto_lr_dec *dec;
static int play_half;               /* sotto 1200: which 20 ms half of the slot comes next */
static int play_skip;               /* halves still to drop: the decoder's delay, silent */
#ifdef SP_VLR
static struct sotto_vlr_dec *vdec;  /* a sotto 600 / 400 message's decoder (dec is then 0) */
static int play_k, play_n;          /* the next 20 ms of the slot, of play_n */
#endif
#else
static struct sotto_dec *dec;
#endif
static int play_pos, play_end;
/* stream mode (sp_set_stream): the store is a ring of whole interleave groups, played while
   later groups arrive */
#ifdef SP_LR
#define STREAM_DEFAULT 1
#else
#define STREAM_DEFAULT 0
#endif
static int stream_on = STREAM_DEFAULT;
static int ring;                    /* frames in the ring while a message streams, else 0 */
static int fpg_now;                 /* frames per interleave group of the message */
static int gp_now = GROUP;          /* packets per interleave group of the message: 8, 4, 2 or 1 */
static int hi_group, hi_count;      /* the newest group seen, and how many of its packets */
static int avail;                   /* frames below this are final: playable */
static int ended, overruns, playing;
static uint32_t pcm_hash, pcm_n;    /* FNV-1a of the samples played since sp_play_start */

static void hash_pcm(const int16_t *pcm) {
  uint32_t h = pcm_hash;
  int i;
  for (i = 0; i < SP_FRAME_SAMPLES; i++) {
    unsigned v = (uint16_t)pcm[i];
    h = (h ^ (v & 0xFF)) * 16777619u;
    h = (h ^ (v >> 8)) * 16777619u;
  }
  pcm_hash = h, pcm_n += SP_FRAME_SAMPLES;
}

uint32_t sp_pcm_hash(unsigned *samples) {
  if (samples) *samples = pcm_n;
  return pcm_hash;
}
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

static int voice_magic(int m) {
  return m == SP_MAGIC || m == SP_MAGIC_LR || m == SP_MAGIC_600 || m == SP_MAGIC_400;
}

static void clear_message(uint8_t msg_id, uint8_t profile, uint8_t magic) {
  uint16_t gp = S.st.good_packets, bp = S.st.bad_packets;
  ring = fpg_now = hi_count = avail = ended = overruns = playing = 0;
  hi_group = -1;
  memset(S.good, 0, (unsigned)S.max_frames / 8u);
  memset(S.seen, 0, (unsigned)S.max_frames / 16u);
  memset(&S.st, 0, sizeof S.st);
  S.st.good_packets = gp, S.st.bad_packets = bp;
  S.st.have = 1, S.st.msg_id = msg_id, S.st.profile = profile;
  S.magic = magic;
}

static int pos_of(long f) { return ring ? (int)(f % ring) : (int)f; }

static void put_frame(int f, const uint8_t *src) {
  int r = pos_of(f);
  if (f >= S.st.stored || bit(S.good, r)) return;
  memcpy(S.frames + 6 * r, src, 6);
  set_bit(S.good, r);
  S.st.frames_got++;
}

/* stream mode: the frames playback may take, once group g's packets are all in or a later
   group's have started (packets come in seq order), or the sender has stopped (ended) */
static void update_avail(void) {
  long a = (long)hi_group * fpg_now + (hi_count >= gp_now ? fpg_now : 0);
  if (ended || S.st.complete) a = S.st.total == 0xFFFF ? (long)(hi_group + 1) * fpg_now : S.st.stored;
  if (a > S.st.stored) a = S.st.stored;
  if (a > avail) avail = (int)a;
}

static int finish_packet(int seq, int was_new, unsigned before) {
  if (seq < S.max_frames / 2 && !bit(S.seen, seq)) {
    set_bit(S.seen, seq), S.st.packets_got++;
    if (ring && seq / gp_now == hi_group) hi_count++;
  }
  if (S.st.stored && S.st.frames_got == S.st.stored) {
    int done = !S.st.complete;
    S.st.complete = 1;
    if (ring) update_avail();
    if (done && !was_new) return SP_COMPLETE;
  }
  if (ring) update_avail();
  if (was_new) return SP_NEW;
  return S.st.frames_got != before ? SP_ADDED : SP_DUP;
}

/* profile L or XL (fpp frames per packet), checked and de-whitened */
static int packet(const uint8_t *p, int fpp, uint8_t profile) {
  /* seq's top 2 bits: the interleave depth, 8 >> code packets per group (0: 8, as before) */
  int dep = GROUP >> (p[2] >> 6), seq = (p[2] & 0x3F) << 8 | p[3], total = p[4] << 8 | p[5];
  int s, was_new = 0, fpg = fpp * dep;
  unsigned before;
  if (!S.st.have || S.st.profile != profile || p[0] != S.magic || p[1] != S.st.msg_id || total != S.st.total ||
      dep != gp_now) {
    clear_message(p[1], profile, p[0]);
    S.st.total = (uint16_t)total;
    S.st.stored = (uint16_t)(total < S.max_frames ? total : S.max_frames);
    S.st.packets_total = (uint16_t)((total + fpg - 1) / fpg * dep);
    fpg_now = fpg, gp_now = dep;
    if (stream_on && S.max_frames >= fpg)   /* the whole message, through a ring of groups */
      ring = S.max_frames / fpg * fpg, S.st.stored = (uint16_t)total;
    was_new = 1;
  }
  if (ring) {
    int g = seq / dep, k, i, groups = ring / fpg;
    if (g <= hi_group - groups) return SP_DUP;   /* its place in the ring went to a later group */
    if (g > hi_group) {
      for (k = hi_group + 1 > g - groups + 1 ? hi_group + 1 : g - groups + 1; k <= g; k++) {
        /* group k reuses group k - groups's place: an overrun if that wasn't played yet */
        if (playing && (long)(k - groups + 1) * fpg > play_pos) overruns++;
        for (i = 0; i < fpg; i++) {           /* XL at depth 1 has 20-frame groups: by bit */
          int b = pos_of((long)k * fpg + i);
          S.good[b >> 3] &= (uint8_t)~(1u << (b & 7));
        }
      }
      hi_group = g, hi_count = 0;
    }
  }
  before = S.st.frames_got;
  for (s = 0; s < fpp; s++) {
    long f = (long)(seq / dep) * fpg + s * dep + seq % dep;
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
  if (!voice_magic(last_pkt[0])) { S.st.bad_packets++; return SP_BAD_MAGIC; }
  if (crc16(last_pkt, len - 2) != ((unsigned)last_pkt[len - 2] << 8 | last_pkt[len - 1])) {
    S.st.bad_packets++;
    return SP_BAD_CRC;
  }
  S.st.good_packets++;
  if (!S.max_frames) return SP_DUP;
  r = packet(last_pkt, fpp_of(len), (len == SP_PAYLOAD_XL ? 'X' : 'L') | (last_pkt[0] != SP_MAGIC ? 0x20 : 0));
  return r;
}

int sp_check(const uint8_t *payload, int len) {
  int i;
  unsigned c = 0xFFFF;
  if (len != SP_PAYLOAD_L && len != SP_PAYLOAD_XL) return 0;
  if (!pn9[0]) pn9_init();
  if (!voice_magic(payload[0] ^ pn9[0])) return 0;
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

const uint8_t *sp_pn9(void) {
  if (!pn9[0]) pn9_init();
  return pn9;
}

unsigned sp_crc16(const uint8_t *p, int n) { return crc16(p, n); }

void sp_status(sp_status_t *st) { *st = S.st; }
int sp_magic(void) { return S.st.have ? S.magic : 0; }

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
#if defined(SP_VLR)
#define MSG_VLR (S.magic == SP_MAGIC_600 || S.magic == SP_MAGIC_400)
#define MSG_PLAYS (S.magic == SP_MAGIC_LR || MSG_VLR)
#elif defined(SP_LR)
#define MSG_PLAYS (S.magic == SP_MAGIC_LR)
#else
#define MSG_PLAYS (S.magic == SP_MAGIC)
#endif
/* the tables a message needs: sotto 600 / 400 their own (sp_vlr.h), the rest the codebook */
#ifdef SP_VLR
#define TABLES_READY (MSG_VLR ? sp_vlr_ready() : sp_cb_ready())
#else
#define TABLES_READY sp_cb_ready()
#endif

void sp_play_start(void) {
#ifdef SP_LR
  if (dec) sotto_lr_dec_destroy(dec);
  dec = 0;
#ifdef SP_VLR
  if (vdec) sotto_vlr_dec_destroy(vdec);
  vdec = 0;
  play_k = 0;
  if (S.st.have && MSG_VLR) {
    vdec = sotto_vlr_dec_create(S.magic == SP_MAGIC_600 ? 600 : 400);
    play_n = vdec ? sotto_vlr_samples(vdec) / SP_FRAME_SAMPLES : 0;
    play_skip = play_n * SOTTO_VLR_DEC_DELAY;
  } else
#endif
  {
    dec = sotto_lr_dec_create();
    play_skip = 2 * SOTTO_LR_DEC_DELAY;
  }
  play_half = 0;
#else
  if (dec) sotto_dec_destroy(dec);
  dec = sotto_dec_create();
#endif
  play_pos = 0;
  /* -1: the other codec's message, or no tables to decode with (sp_cb.h, sp_vlr.h) */
  play_end = S.st.have && MSG_PLAYS && TABLES_READY ? S.st.stored : -1;
  playing = 1;
  emph1 = emph2 = 0;
  pcm_hash = 2166136261u, pcm_n = 0;
}

#ifdef SP_LR
/* sotto 1200: each 6-byte slot is 40 ms, played as two 20 ms halves (no 320-sample buffer);
   the decoder runs SOTTO_LR_DEC_DELAY slots behind, and its flush plays the last ones */
static int lr_half(int16_t pcm[SP_FRAME_SAMPLES]) {
  int half = play_half, r;
  if (play_pos < play_end) {
    if (ring && play_pos >= avail) return SP_PLAY_WAIT;
    r = pos_of(play_pos);
    sotto_lr_decode_half(dec, S.frames + 6 * r, bit(S.good, r), half, pcm);
  } else if (!sotto_lr_dec_flush_half(dec, half, pcm))
    return 0;
  play_half = !half;
  if (half) play_pos++;
  return 1;
}

#ifdef SP_VLR
/* sotto 600 / 400: each slot is play_n x 20 ms; the decoder runs SOTTO_VLR_DEC_DELAY slots
   behind, and its flush plays the last ones */
static int vlr_frame(int16_t pcm[SP_FRAME_SAMPLES]) {
  int r;
  if (play_pos < play_end) {
    if (ring && play_pos >= avail && play_k == 0) return SP_PLAY_WAIT;
    r = pos_of(play_pos);
    sotto_vlr_decode_frame(vdec, S.frames + 6 * r, bit(S.good, r), play_k, pcm);
    if (++play_k == play_n) play_k = 0, play_pos++;
    return 1;
  }
  return sotto_vlr_dec_flush_frame(vdec, pcm);
}
#endif

int sp_play_frame(int16_t pcm[SP_FRAME_SAMPLES]) {
#ifdef SP_VLR
  if (vdec && play_end >= 0)
    for (;;) {
      int r = vlr_frame(pcm);
      if (r != 1) return r;
      if (!play_skip) return hash_pcm(pcm), 1;
      play_skip--;                /* the first 2 slots out are the delay's silence */
    }
#endif
  if (!dec || play_end < 0) return 0;
  for (;;) {
    int r = lr_half(pcm);
    if (r != 1) return r;
    if (!play_skip) return hash_pcm(pcm), 1;
    play_skip--;                  /* the first 80 ms out are the delay's silence */
  }
}
#else
int sp_play_frame(int16_t pcm[SP_FRAME_SAMPLES]) {
  if (!dec || play_end < 0) return 0;
  if (play_pos < play_end) {
    int r = pos_of(play_pos);
    if (ring && play_pos >= avail) return SP_PLAY_WAIT;
    sotto_decode(dec, S.frames + 6 * r, bit(S.good, r), pcm);
    play_pos++;
    hash_pcm(pcm);
    return 1;
  }
  if (play_pos == play_end) {     /* the decoder's last 20 ms */
    sotto_dec_flush(dec, pcm);
    play_pos++;
    hash_pcm(pcm);
    return 1;
  }
  return 0;
}
#endif

void sp_set_stream(int on) { stream_on = on != 0; }
int sp_stream(void) { return stream_on; }
int sp_stream_ready(void) { return ring && S.st.have && MSG_PLAYS && avail > 0; }
void sp_stream_end(void) {
  if (ring) ended = 1, update_avail();
}
int sp_stream_info(int *overrun_count) {
  if (overrun_count) *overrun_count = overruns;
  return ring;
}

int sp_play_pos(void) { return play_end < 0 ? 0 : play_pos < play_end ? play_pos : play_end; }

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
  for (f = 0; f < (ring && ring < S.st.stored ? ring : S.st.stored); f++)
    if (bit(S.good, f)) {
      const uint8_t *q = S.frames + 6 * f;
      int e = ((q[1] << 8 | q[2]) >> 10) & 31;      /* bits 9..13: voicing 2 | Wo 7 | energy 5 */
      if (S.magic == SP_MAGIC_600 || S.magic == SP_MAGIC_400)  /* bits 13..17: the energy mean */
        e = (int)(((uint32_t)q[1] << 16 | (uint32_t)q[2] << 8 | q[3]) >> 14) & 31;
      if (e > top) top = e;
    }
  return top < 0 ? 256 : auto_gain[top];
}

int sp_play_pwm(uint16_t out[SP_FRAME_SAMPLES], unsigned top, int gain_q8, int emphasis) {
  int16_t *pcm = (int16_t *)out;   /* decoded in place: the conversion reads each sample before
                                      writing it (320 bytes less RAM on the K6) */
  int r = sp_play_frame(pcm), i;
  if (r == SP_PLAY_WAIT)
    for (i = 0; i < SP_FRAME_SAMPLES; i++) out[i] = (uint16_t)(top / 2);
  if (r != 1) return r;
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
  return (unsigned)(sizeof dec_mem + sizeof pn9 + sizeof last_pkt + sizeof S + sizeof dec + 14 * sizeof(int)
#ifdef SP_VLR
                    + sizeof vdec + 2 * sizeof(int)
#endif
                    + 2 * sizeof(int32_t) + 2 * SP_FRAME_SAMPLES
#ifdef FX_OA_PACKED
                    + 2048u                       /* fx_cs512's table (fx_math.c) */
#endif
                    );
}
