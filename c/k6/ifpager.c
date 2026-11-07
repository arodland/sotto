/* ifpager: the K6 pager's coded 4FSK receiver (ifpager.h). SPDX-License-Identifier: MIT */
#ifndef IFRX_ONLY
#define IFRX_ONLY 1 /* IFRX_4FSK */
#endif
#include "ifpager.h"
/* the acquisition backs off less from its best offset than the modules' default (1/2): at the
   image's DMA rate (5.9 MS/s, K6-trained templates) a half left a quarter of the starting
   offsets locked a quarter symbol early; 2/3 took 243 of 252 packets on the model at 47 dB-Hz
   over two rates, three offsets and 14 starts, against 150 */
#ifndef IFRX_EDGE_NUM
#define IFRX_EDGE_NUM 2
#define IFRX_EDGE_DEN 3
#endif
#ifndef IFP_HOST
#define IFRX_RAMFN 1 /* the receiver's hot loops run from a copy in RAM (ifp_session) */
#endif
#include "ifrx.c"

/* the code's outputs (bit 1: polynomial 133, bit 0: 171) for new state 2j with the older
   predecessor bit 0: the register is 2j. With the other predecessor, or for state 2j + 1, both
   flip (both polynomials have their first and last taps): one branch metric a butterfly */
static const uint8_t ifp_out[32] = {0, 2, 0, 2, 3, 1, 3, 1, 3, 1, 3, 1, 0, 2, 0, 2,
                                    1, 3, 1, 3, 2, 0, 2, 0, 2, 0, 2, 0, 1, 3, 1, 3};

void ifp_init(ifp_t *p) {
  ifrx_zero(p, sizeof(*p) / 4);
  p->bad = 0xFFFFFFu;
  p->ls = IFP_STEPS; /* no packet being decoded */
}

/* the decided bits of steps [from, to) into the packet, tracing back from state s after step
   end - 1 (ring entry r) through the ring of decisions (end - from <= IFP_TB_RING) */
IFRX_NOINLINE static void traceback(ifp_t *p, uint32_t s, int32_t r, int32_t end, int32_t from, int32_t to) {
  int32_t k = end;
  if (to > IFP_PAYLOAD * 8) to = IFP_PAYLOAD * 8; /* the tail's zeros */
  while (--k >= from) {
    uint32_t c;
    if (k < to && (s & 1))
      p->pkt[k >> 3] |= (uint8_t)(0x80u >> (k & 7));
    c = (p->dec[r][s >> 5] >> (s & 31)) & 1;
    s = (s >> 1) | (c << 5);
    r = r ? r - 1 : IFP_TB_RING - 1;
  }
}

/* one trellis step, on coded bits 2 bk and 2 bk + 1 of the block being decoded */
IFRX_NOINLINE static void step(ifp_t *p) {
  const int8_t *l = p->llr[p->bb] + 2 * p->bk;
  int16_t *a = p->pm[p->cur], *b = p->pm[p->cur ^ 1];
  int32_t l0 = l[0], l1 = l[1], bm[4], base = a[0];
  uint32_t d[2] = {0, 0};
  int j;
  bm[0] = l0 + l1, bm[1] = l0 - l1, bm[2] = l1 - l0, bm[3] = -l0 - l1;
  for (j = 0; j < 32; j++) {
    int32_t m = bm[ifp_out[j]], x = a[j] - base, y = a[j + 32] - base, u, v;
    uint32_t bit = 1u << ((2 * j) & 31), *w = &d[j >> 4];
    u = x + m, v = y - m;
    if (v > u) b[2 * j] = (int16_t)v, *w |= bit;
    else b[2 * j] = (int16_t)u;
    u = x - m, v = y + m;
    if (v > u) b[2 * j + 1] = (int16_t)v, *w |= bit << 1;
    else b[2 * j + 1] = (int16_t)u;
  }
  p->cur ^= 1;
  p->dec[p->ri][0] = d[0], p->dec[p->ri][1] = d[1];
  if (++p->bk == IFP_HALF) p->bk = 0, p->bb ^= 1;
  p->step++;
  p->ls++;
  if (p->ls == IFP_STEPS) {
    traceback(p, 0, p->ri, p->ls, p->out, p->ls); /* the tail brought it to state 0 */
    p->out = IFP_STEPS;
    p->have = 1;
  } else if (!(p->ls & (IFP_TB_OUT - 1)) && p->ls >= IFP_TB_RING) {
    uint32_t s = 0, k;
    for (k = 1; k < 64; k++)
      if (b[k] > b[s]) s = k;
    traceback(p, s, p->ri, p->ls, p->out, p->out + IFP_TB_OUT);
    p->out += IFP_TB_OUT;
  }
  p->ri = p->ri == IFP_TB_RING - 1 ? 0 : p->ri + 1;
}

int ifp_work(ifp_t *p) {
  int k;
  if (p->have || p->step >= p->ready) return -1;
  if (p->ls == IFP_STEPS) { /* the next packet's first step: start it afresh */
    p->ls = 0, p->out = 0, p->ri = 0;
    ifrx_zero(p->pkt, IFP_PAYLOAD / 4);
    ifrx_zero(p->pm[0], sizeof(p->pm) / 4);
    p->cur = 0;
    for (k = 1; k < 64; k++) p->pm[0][k] = -8000;
  }
  step(p);
  return p->have;
}

/* x's set bits among the low 24 */
static uint32_t ones24(uint32_t x) {
  x &= 0xFFFFFFu;
  x = x - ((x >> 1) & 0x555555u);
  x = (x & 0x333333u) + ((x >> 2) & 0x333333u);
  x = (x + (x >> 4)) & 0x0F0F0Fu;
  return (x + (x >> 8) + (x >> 16)) & 0x3F;
}

static int32_t clamp_llr(int32_t d, uint32_t mean) {
  int32_t v = ifrx_sdiv(clampi(d, 1 << 24) * IFP_LLR_ONE, mean | 1);
  return clampi(v, 127);
}

int ifp_sym(ifp_t *p, int tone, const uint32_t met[4]) {
  uint32_t mx = met[0], k;
  for (k = 1; k < 4; k++)
    if (met[k] > mx) mx = met[k];
  if (!p->syms) p->mean_q4 = mx << 4;
  else p->mean_q4 = (uint32_t)((int32_t)p->mean_q4 + (((int32_t)(mx << 4) - (int32_t)p->mean_q4) >> 6));
  p->syms++;
  /* every decision into the hunt's window, so a sync word a symbol or two from where the
     packet's count put it is still found */
  p->hist = (p->hist << 1) | (tone == 3);
  p->bad = (p->bad << 1) | (tone == 1 || tone == 2);
  if (p->state == IFP_DATA) {
    /* Gray: tones 0 1 2 3 are 00 01 11 10; LLR > 0 for a 0 bit */
    uint32_t m01 = met[0] > met[1] ? met[0] : met[1], m23 = met[2] > met[3] ? met[2] : met[3];
    uint32_t m03 = met[0] > met[3] ? met[0] : met[3], m12 = met[1] > met[2] ? met[1] : met[2];
    uint32_t mean = p->mean_q4 >> 4;
    int8_t *l = p->llr[p->sb] + p->sk;
    l[0] = (int8_t)clamp_llr((int32_t)m01 - (int32_t)m23, mean);
    l[IFP_HALF] = (int8_t)clamp_llr((int32_t)m03 - (int32_t)m12, mean);
    if (++p->sk == IFP_HALF) p->sk = 0, p->sb ^= 1, p->ready += IFP_HALF; /* a block in */
    if (++p->nsym == IFP_STEPS) p->state = IFP_HUNT;
    return 0;
  }
  k = ones24((p->hist ^ IFP_SYNC) | p->bad);
  if (k > IFP_SYNC_MISS) return 0;
  p->state = IFP_DATA;
  p->nsym = 0;
  p->miss = (int32_t)k;
  p->syncs++;
  p->last_sync = p->syms;
  return 1;
}

#ifndef IFP_HOST
#include "sotto_pager.h"

#define GPIOB 0x48000400u
#define RCC_AHBENR 0x40021014u
#define RCC_APB2ENR 0x40021018u
#define TIM17 0x40014800u
#define TIM17_CNT (TIM17 + 0x24)
#define DMA 0x40020000u
#define DMA_CCR1 (DMA + 0x08)
#define DMA_CNDTR1 (DMA + 0x0C)
#define DMA_CPAR1 (DMA + 0x10)
#define DMA_CMAR1 (DMA + 0x14)
#define TIM17EN (1u << 18)
#define RSZ IFP_RING
#define L 64u
#ifndef KS0
#define KS0 5880       /* kS/s to start on (measured as it runs): the image's code polls the ring
                          from flash, where channel 1 runs at 5880-5915 (on air, 2026-11-07) */
#endif
#define BLK_US 131072u /* TIM17 microseconds a measurement of the rate */
#define STALL_US 120u  /* well under a lap of the ring */
/* the burst is over after this many symbols with no sync word: two packets' time (and the
   rest of a 2 s lead before the first) */
#define END_SYMS (2 * (IFP_STEPS + IFP_SYNC_LEN))
#define FIRST_SYMS (2400 + END_SYMS)

static uint32_t rd(uint32_t a) { return *(volatile uint32_t *)a; }
static void wr(uint32_t a, uint32_t v) { *(volatile uint32_t *)a = v; }
static uint32_t tim(void) { return rd(TIM17_CNT) & 0xFFFF; }

static void stop(void) {
  wr(TIM17 + 0x00, 0);
  wr(TIM17 + 0x0C, 0);
  wr(DMA_CCR1, 0);
  wr(DMA + 0x04, 0xF);
}

static void start(uint8_t *ring) {
  stop();
  wr(DMA_CPAR1, GPIOB + 0x10);
  wr(DMA_CMAR1, (uint32_t)ring);
  wr(DMA_CNDTR1, RSZ);
  wr(TIM17 + 0x28, 95); /* PSC: 1 MHz, a lap of 65 ms */
  wr(TIM17 + 0x2C, 0xFFFF);
  wr(TIM17 + 0x14, 1);
  wr(TIM17 + 0x10, 0);
  wr(TIM17 + 0x0C, 1u << 8);
  wr(TIM17 + 0x00, 1);
  wr(DMA_CCR1, (3u << 12) | (1u << 7) | (1u << 5) | 1u); /* PL very high, MINC, CIRC, EN */
}

/* L samples at ring offset r: the ones on PB1 (bit 1 of each byte), as a count. In the RAM
   block after ifrx.c's sample, corr2 and corr4, which ifp_ram_end closes */
IFRX_RAM static int count(const uint8_t *ring, uint32_t r) {
  const uint32_t *q = (const uint32_t *)(ring + r);
  uint32_t acc = 0, i;
  for (i = 0; i < L / 4; i += 4)
    acc += (q[i] & 0x02020202u) + (q[i + 1] & 0x02020202u) + (q[i + 2] & 0x02020202u) +
           (q[i + 3] & 0x02020202u);
  acc >>= 1;
  acc = (acc & 0x00FF00FFu) + ((acc >> 8) & 0x00FF00FFu);
  return 2 * (int)((acc & 0xFFFF) + (acc >> 16)) - (int)L;
}

IFRX_RAM static void ifp_ram_end(void) { __asm__ volatile(""); }

/* the RAM block (sample .. ifp_ram_end) into code[], and rx's hot loops and *cnt pointed at the
   copies: returns its size, or 0 (the originals stay: the block isn't as laid out, or too big).
   Thumb: a function's address has bit 0 set; the copy keeps each word's place in a word, as
   its literal loads (pc-relative, word aligned) need; the calls inside it (bl) are relative */
#define IFP_RAM_MAX 416u
typedef int (*count_fn)(const uint8_t *, uint32_t);
IFRX_NOINLINE static uint32_t ram_block(ifrx_t *rx, uint32_t *code, count_fn *cnt) {
  uint32_t f[4], lo = 0xFFFFFFFFu, e = (uint32_t)ifp_ram_end & ~1u, i, d;
  uint16_t *dst;
  const uint16_t *src;
  f[0] = (uint32_t)sample, f[1] = (uint32_t)corr2, f[2] = (uint32_t)corr4, f[3] = (uint32_t)count;
  for (i = 0; i < 4; i++) {
    f[i] &= ~1u;
    if (f[i] >= e) return 0;
    if (f[i] < lo) lo = f[i];
  }
  if (e - lo + 4 > IFP_RAM_MAX) return 0;
  dst = (uint16_t *)((uint8_t *)code + (lo & 2));
  src = (const uint16_t *)lo;
  for (i = 0; i < (e - lo) / 2; i++) dst[i] = src[i];
  d = (uint32_t)dst - lo;
  rx->samp = (void (*)(const struct ifrx_s *, uint32_t, uint32_t, int16_t *))((f[0] + d) | 1u);
  rx->c4 = (void (*)(const int16_t (*)[2], const uint8_t *, int32_t *, int))((f[2] + d) | 1u);
  *cnt = (count_fn)((f[3] + d) | 1u);
  return e - lo;
}

/* a decoded packet into the store (sp_packet: de-whitening, CRC, the frames) */
IFRX_NOINLINE static void hand(ifp_t *p, ifp_result_t *res) {
  int v = sp_packet(p->pkt, IFP_PAYLOAD);
  if (res->nres < IFP_RES_MAX) res->res[res->nres] = (int8_t)v, res->miss[res->nres] = (int8_t)p->miss;
  res->nres++;
  p->have = 0;
}

IFRX_NOINLINE static int run(const k6_api_t *api, uint8_t *rb, uint32_t wait_ms, uint32_t max_ms,
                            ifp_result_t *res) {
  ifrx_t rx;
  ifp_t p;
  uint32_t lastw = 0, produced = 0, consumed = 0, c0, k = 0, kq = 0, idle = 0, tot = 0;
  uint32_t ks = KS0 * 16, bp = 0, bt = 0, t0 = api->ms(), lock_sym = 0;
  int r = 0, locked = 0;
  uint32_t code[IFP_RAM_MAX / 4], cpos = 0, pmet[4];
  int pend = 0, psym = 0;
  count_fn cnt = count;
  ifrx_init(&rx, IFRX_4FSK, ks);
#ifndef IFP_NO_RAM
  res->ram = (int32_t)ram_block(&rx, code, &cnt);
#else
  (void)code, (void)ram_block;
#endif
  ifp_init(&p);
  start(rb);
  c0 = tim();
  for (;;) {
    uint32_t w = RSZ - (rd(DMA_CNDTR1) & 0xFFFF), dw = w >= lastw ? w - lastw : w + RSZ - lastw,
             cn = tim(), d, a, nc = 0;
    d = (cn - c0) & 0xFFFF;
    c0 = cn;
    tot += d;
    if (d > (uint32_t)res->gap_us && locked) res->gap_us = (int32_t)d, res->gap_i = rx.i;
    lastw = w;
    if (d > STALL_US) /* away near a lap of the ring or more: the laps it didn't see, by the clock */
      dw += ifrx_udiv(ifrx_udiv(d * (ks >> 4), 1000u) + RSZ / 2 - dw, RSZ) * RSZ;
    produced += dw;
    a = produced - consumed;
    if (a > RSZ - L) { /* the ring overran: skip what's gone */
      nc = a / L;
      consumed += nc * L;
      cpos += nc * L;
      cpos -= ifrx_udiv(cpos, RSZ) * RSZ;
      res->lost += nc;
      ifrx_skip(&rx, nc);
      continue;
    }
    if (a < L) {
      /* caught up: the last symbol into the decoder, a trellis step (~30 us in RAM, ~40 from
         flash), or a decoded packet into the store (its CRC, ~130 us), but not just before the
         receiver's bursts (half way through a symbol and at its end, ~120-150 us), which would
         then overrun the ring: a step anywhere but the sample before them (the decoder must
         keep within one interleave block of the symbols), the store's work three or more
         samples before them */
      int i = rx.i;
      if (pend) {
        pend = 0;
        ifp_sym(&p, psym, pmet);
        if ((int32_t)(p.ready - p.step) > res->lag) res->lag = (int32_t)(p.ready - p.step);
        continue;
      }
      if (p.have) {
        if ((i >= 0 && i <= IFRX_TL / 2 - 4) || (i >= IFRX_TL / 2 + 1 && i <= IFRX_TL - 4)) {
          hand(&p, res);
          r++;
          continue;
        }
      } else if (i != IFRX_TL / 2 - 1 && i < IFRX_TL - 1 && ifp_work(&p) >= 0)
        continue;
      idle += d;
      if (++kq & 0xFFFF) continue;
    } else {
      if (ifrx_push(&rx, cnt(rb, cpos))) {
        /* the symbol's soft bits later, when caught up (not on top of the burst that
           decided it) */
        if (pend) ifp_sym(&p, psym, pmet);
        if (!locked) locked = 1, lock_sym = p.syms;
        rx.gap = 0;
        psym = rx.sym, pmet[0] = rx.met[0], pmet[1] = rx.met[1], pmet[2] = rx.met[2], pmet[3] = rx.met[3];
        pend = 1;
      }
      consumed += L;
      cpos += L;
      if (cpos == RSZ) cpos = 0;
      /* the checks every 256 counts, a third into a symbol (away from the receiver's bursts) */
      if (++k < 256 || (rx.i != IFRX_TL / 3 && k < 1024)) continue;
    }
    k = 0;
    if (tot - bt >= BLK_US) {
      ks = ifrx_udiv((produced - bp) * 2000u, (tot - bt) >> 3); /* 1/16 kS/s */
#ifndef IFP_NO_RATE
      ifrx_rate(&rx, ks);
#endif
      bp = produced, bt = tot;
      continue;
    }
    if (api->key_poll()) {
      r = -2;
      break;
    }
    if (api->ms() - t0 > max_ms) break;
    if (!locked) {
      if (api->ms() - t0 > wait_ms) {
        r = -6;
        break;
      }
      continue;
    }
    if (p.syncs ? p.syms - p.last_sync > END_SYMS : p.syms - lock_sym > FIRST_SYMS) break;
  }
  stop();
  /* the burst is over (or the time): the packets whose blocks are all in */
  if (pend) ifp_sym(&p, psym, pmet);
  if (r >= 0)
    for (;;) {
      if (p.have) hand(&p, res), r++;
      else if (ifp_work(&p) < 0) break;
    }
  res->syncs = p.syncs;
  res->syms = p.syms;
  res->acq_q = rx.acq_q;
  res->acq_rej = rx.acq_rej;
  res->afc_hz = ifrx_afc_hz(&rx);
  res->idle_pct = (int32_t)ifrx_udiv(idle >> 8, (tot >> 16) | 1) * 100 >> 8;
  res->ks = (int32_t)(ks >> 4);
  return r;
}

int ifp_session(const k6_api_t *api, uint8_t *ring, uint32_t wait_ms, uint32_t max_ms, ifp_result_t *res) {
  uint32_t ahb, apb2, t0 = api->ms();
  uint16_t r33, r34;
  ifrx_zero(res, sizeof(*res) / 4);
  res->ret = -4;
  if (rd(GPIOB) & (3u << 2)) return res->ret; /* PB1 not an input */
  apb2 = rd(RCC_APB2ENR);
  ahb = rd(RCC_AHBENR);
  wr(RCC_AHBENR, ahb | 1u); /* the DMA's registers read junk with its clock off */
  res->ret = -7;
  if ((apb2 & TIM17EN) || (rd(DMA_CCR1) & 1)) goto out_ahb;
  r33 = api->fd_read(0x33);
  r34 = api->fd_read(0x34);
  res->ret = -5;
  if ((r33 & 0x0100) && api->fd_write(0x33, (uint16_t)(r33 & ~0x0100)) != 0) goto out_ahb;
  api->fd_write(0x34, (uint16_t)((r34 & 0xFFF0) | 13));
  wr(RCC_APB2ENR, apb2 | TIM17EN);
  res->ret = run(api, ring, wait_ms, max_ms, res);
  wr(RCC_APB2ENR, apb2);
  api->fd_write(0x34, r34);
  api->fd_write(0x33, r33);
out_ahb:
  wr(RCC_AHBENR, ahb);
  res->ms = api->ms() - t0;
  return res->ret;
}
#endif
