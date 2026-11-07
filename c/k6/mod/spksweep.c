/* spksweep: the K6's speaker sweep, from RAM (no reflash). It plays steady tones out of the
   MCU's PWM pin at 16 kHz updates, twice the pager's 8 kHz, so they reach 7.5 kHz, for a
   phone recording of the speaker; tools/k6_sweep.py reads back each tone's level (docs/k6-receiver.md,
   "The audio path").

   Schedule: 1 s of silence (the noise floor), then the 14 tones HZ[] for 1 s each, each
   followed by 0.25 s of silence, all at the same level (top / 8 peak around top / 2: about
   speech's level). About 18.5 s, 296,160 updates.

   The audio path is the stock voice prompts' (Aura's hal/pwm_audio.rs): PA11 = TIM1_CH4 (AF2),
   a 48 kHz carrier (ARR 2000 at 96 MHz), then two RC poles, 22k into the speaker amplifier
   (PB2 high = on), summed with the FD6818's AF output. The pager's playback updates CCR4 from
   DMA every 6 carrier periods (RCR 5); this module sets RCR 2 (every 3 periods: 16 kHz) and
   writes CCR4 itself on each update event (polling TIM1_SR UIF; CCR4 is preloaded, so a
   value lands on the update after it is written). An update it reaches with UIF already set
   counts as late: the value before it played twice (an interrupt held the CPU over 62 us).

   The 16 kHz hold costs 20 log10(sinc(f / 16000)) at each tone (-3.4 dB at 7.5 kHz):
   tools/k6_sweep.py adds it back.

   Sets up, and puts back as it found them: TIM1 (clock, CR1 CCMR2 CCER BDTR; DIER 0, as Aura's
   stop), PA11's mode, speed and alternate function, PB2 (the amplifier), and REG_47 (the
   FD6818's AF output, muted while it plays). No TX.
   say lines: "spk sweep rate=16000 tones=14 top=2000", then
   "spk sweep done tones=N late=L" (N < 14 if a key stopped it).
   Returns the tones played (14), -2 (a key stopped it: checked in each silence),
   -4 (TIM1 already running: something else is playing).
   SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 2
#include "../k6_mod.h"

#define RCC_APB2ENR 0x40021018u     /* TIM1EN bit 11 */
#define GPIOA_MODER 0x48000000u
#define GPIOA_OSPEEDR 0x48000008u
#define GPIOA_AFRH 0x48000024u
#define GPIOB_ODR 0x48000414u
#define GPIOB_BSRR 0x48000418u
#define GPIOB_BRR 0x48000428u
#define SPK (1u << 2)
#define TIM1 0x40012C00u
#define TIM_CR1 (TIM1 + 0x00)
#define TIM_DIER (TIM1 + 0x0C)
#define TIM_SR (TIM1 + 0x10)
#define TIM_EGR (TIM1 + 0x14)
#define TIM_CCMR2 (TIM1 + 0x1C)
#define TIM_CCER (TIM1 + 0x20)
#define TIM_PSC (TIM1 + 0x28)
#define TIM_ARR (TIM1 + 0x2C)
#define TIM_RCR (TIM1 + 0x30)
#define TIM_CCR4 (TIM1 + 0x40)
#define TIM_BDTR (TIM1 + 0x44)

#define TOP 2000u                   /* 96 MHz / 2000 = the 48 kHz carrier */
#define MID (TOP / 2)
#define AMP (TOP / 8)
#define RATE 16000u
#define LEAD (RATE)                 /* updates: 1 s */
#define TONE (RATE)
#define GAP (RATE / 4)
#define NT 14

static uint32_t rd(uint32_t a) { return *(volatile uint32_t *)a; }
static void wr(uint32_t a, uint32_t v) { *(volatile uint32_t *)a = v; }

/* 2^32 / 16000 = 268435.456 per Hz (the compiler works these out) */
#define INC(f) ((f) * 268435u + (f) * 456u / 1000u)
static const uint32_t inc[NT] = {INC(250u), INC(400u), INC(630u), INC(1000u), INC(1600u),
                                 INC(2000u), INC(2500u), INC(3150u), INC(4000u), INC(5000u),
                                 INC(5600u), INC(6300u), INC(7000u), INC(7500u)};

/* sin(pi / 2 * i / 64), Q15 */
static const int16_t quarter_sin[65] = {
  0, 804, 1608, 2410, 3212, 4011, 4808, 5602, 6393, 7179, 7962, 8739, 9512, 10278, 11039, 11793,
  12539, 13279, 14010, 14732, 15446, 16151, 16846, 17530, 18204, 18868, 19519, 20159, 20787, 21403,
  22005, 22594, 23170, 23731, 24279, 24811, 25329, 25832, 26319, 26790, 27245, 27683, 28105, 28510,
  28898, 29268, 29621, 29956, 30273, 30571, 30852, 31113, 31356, 31580, 31785, 31971, 32137, 32285,
  32412, 32521, 32609, 32678, 32728, 32757, 32767};

/* sin(2 pi ph / 2^32), Q15: the quarter table, linearly interpolated */
static int32_t sin_q15(uint32_t ph) {
  uint32_t q = ph >> 30, x = (ph >> 14) & 0xFFFF;   /* quadrant; 16 bits within it */
  uint32_t i, f;
  int32_t a, v;
  if (q & 1) x = 0x10000 - x;
  i = x >> 10, f = x & 1023;
  a = quarter_sin[i];
  v = i < 64 ? a + (int32_t)(((quarter_sin[i + 1] - a) * (int32_t)f) >> 10) : a;
  return q & 2 ? -v : v;
}

/* wait for the next update event, then queue v for the one after it; 1 if it was late */
static int out(uint32_t v) {
  int late = (int)(rd(TIM_SR) & 1);
  while (!(rd(TIM_SR) & 1)) {
  }
  wr(TIM_SR, 0xFFFEu);              /* rc_w0: clears UIF only */
  wr(TIM_CCR4, v);
  return late;
}

static char *put(char *p, const char *s) {
  while (*s) *p++ = *s++;
  return p;
}

static char *dec(char *p, uint32_t v) {
  static const uint32_t pw[] = {100000, 10000, 1000, 100, 10, 1};
  int i, started = 0;
  for (i = 0; i < 6; i++) {
    int d = 0;
    while (v >= pw[i]) v -= pw[i], d++;
    if (d || started || i == 5) *p++ = (char)('0' + d), started = 1;
  }
  return p;
}

int k6_main(const k6_api_t *api) {
  char line[64], *p;
  uint32_t apb2, moder, ospeed, afrh, ccmr2, ccer, bdtr, spk, n, i, ph, step_inc;
  uint16_t r47;
  int step, tones = 0, late = 0, ret;

  if (rd(TIM_CR1) & 1) {
    api->say("spk sweep: TIM1 busy");
    return -4;
  }
  apb2 = rd(RCC_APB2ENR), moder = rd(GPIOA_MODER), ospeed = rd(GPIOA_OSPEEDR);
  afrh = rd(GPIOA_AFRH), spk = rd(GPIOB_ODR) & SPK;
  r47 = api->fd_read(0x47);
  api->say("spk sweep rate=16000 tones=14 top=2000");
  api->fd_write(0x47, r47 & ~0x0F00u);   /* AF 0: the chip's audio muted */

  wr(RCC_APB2ENR, apb2 | (1u << 11));
  ccmr2 = rd(TIM_CCMR2), ccer = rd(TIM_CCER), bdtr = rd(TIM_BDTR);
  wr(TIM_CR1, 0);
  wr(TIM_DIER, 0);
  wr(TIM_PSC, 0);
  wr(TIM_ARR, TOP);
  wr(TIM_RCR, 2);                    /* an update every 3 carrier periods: 16 kHz */
  wr(TIM_CCR4, MID);
  wr(TIM_CCMR2, (ccmr2 & ~0xFF00u) | (6u << 12) | (1u << 11));   /* PWM mode 1, preload */
  wr(TIM_CCER, 1u << 12);            /* CC4E, active high */
  wr(TIM_EGR, 1);                    /* load ARR, RCR, PSC */
  wr(TIM_SR, 0);
  wr(GPIOA_AFRH, (afrh & ~(0xFu << 12)) | (2u << 12));          /* PA11 AF2 = TIM1_CH4 */
  wr(GPIOA_OSPEEDR, ospeed | (3u << 22));
  wr(GPIOA_MODER, (moder & ~(3u << 22)) | (2u << 22));
  wr(TIM_CR1, (1u << 7) | 1);        /* ARPE, CEN */
  wr(TIM_BDTR, bdtr | (1u << 15));   /* MOE */
  for (i = 0; i < 480; i++) out(MID);   /* 30 ms, as stock: the RC filter settles first */
  wr(GPIOB_BSRR, SPK);

  ret = NT;
  /* step -1: the lead silence; then tone j = step / 2 when step is even, its gap when odd */
  for (step = -1; step < 2 * NT; step++) {
    int tone = step >= 0 && !(step & 1);
    if (!tone) {                     /* in a silence, where a missed update can't be heard: */
      api->ticks();                  /* keeps the core's ms count (it wraps unseen in 6.5 s) */
      if (api->key_down()) {         /* ~60 us */
        ret = -2;
        break;
      }
    }
    n = step < 0 ? LEAD : tone ? TONE : GAP;
    step_inc = tone ? inc[step >> 1] : 0;
    ph = 0;
    for (i = 0; i < n; i++) {
      late += out(tone ? (uint32_t)((int32_t)MID + ((sin_q15(ph) * (int32_t)AMP) >> 15)) : MID);
      ph += step_inc;
    }
    tones += tone;
  }

  out(MID);
  if (!spk) wr(GPIOB_BRR, SPK);
  for (i = 0; i < 320; i++) out(MID);   /* 20 ms, as stock, before the pin lets go */
  wr(TIM_BDTR, bdtr & ~(1u << 15));
  wr(TIM_CR1, 0);
  wr(TIM_DIER, 0);
  wr(TIM_CCER, ccer);
  wr(TIM_CCMR2, ccmr2);
  wr(GPIOA_MODER, moder);
  wr(GPIOA_OSPEEDR, ospeed);
  wr(GPIOA_AFRH, afrh);
  wr(RCC_APB2ENR, apb2);
  api->fd_write(0x47, r47);

  p = put(line, "spk sweep done tones=");
  p = dec(p, (uint32_t)tones);
  p = put(p, " late=");
  p = dec(p, (uint32_t)late);
  *p = 0;
  api->say(line);
  return ret;
}
