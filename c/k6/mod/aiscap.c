/* aiscap: record AIS bursts as the K6 hears them, for the host's receiver (sim/k6if/ais.py,
   docs/k6-ais.md). Receive only.

   The K6 sits on AIS channel A (161.975 MHz) or B (162.025 MHz), switching every SWITCH_S, with
   the FD6818's low IF on PB1 (GP0 code 13). The MCU polls PB1 itself, 256 reads a count in
   straight-line Thumb as gp0fm.c does (~22.9 M samples/s, a count every ~11.2 us, ~89 kHz), not
   by DMA: the DMA's ~5.5 MS/s sampling of the 6.5 MHz 1-bit stream folds its noise into the IF
   band (docs/k6-fd6818-audio.md run 6: 166 Hz rms of FM noise on a carrier against 60 at 22.9
   MS/s), and the first bench run (2026-11-09 02:48Z, DMA) heard 739 bursts at about 5 dB too
   little for the receiver. On the model, polling moves the threshold about 6 dB.

   The counts (ones - 128, int8) run round a ring of NCAP in the scratch. Each also goes through
   a band-pass around the IF (11-23 kHz, a Q14 biquad) into a leaky energy (1/64 a count, ~0.7
   ms). Each channel's turn starts with NCAL counts on the empty channel: their time gives the
   count rate and the quietest of their 1024-count blocks the floor. A burst is the energy over
   TRIG_X times the floor; the ring keeps going for NCAP - PRE counts more and stops, so it holds
   PRE counts from before the trigger and the whole 27 ms burst after. The RSSI isn't read while
   listening: an FD6818 read stalls the polling ~50 us, a hole in the burst. The keys are read
   about every 0.37 s (60 us), the turn's clock every 46 ms (a counter read).

   The gain is frozen (REG_7E bit 15, index 3) at REG_13 = GAIN13 for the run (0: the pager's
   AGC); 0x03FF is the chip's maximum, and at 162 MHz the receiver's noise still sits under the
   1-bit path's.

   Lines (the c and y lines in gp0cap's format, so sim/k6if/dmr.py's parse_caps reads them; ks is
   the count rate x 64 / 1000, so fc = ks * 1000 / 64 there as for the DMA modules; a line is at
   most 60 characters):
     acap start a=161975000 b=162025000 g13=03ff ncap=3600
     f B hz=89421 fl=1234                         a turn: count rate, floor (energy units)
     c 1 1 7 ks=5723 r=14 t=1234 lost=0 ch=B      capture 7: r = the burst's peak energy, dB
                                                  over the floor
     e 7 pre=1024 c0=1101234 c1=2770321 ms=61234  the trigger's count in it; CPU cycles of the
                                                  1024 counts before it and the NCAP - PRE after
                                                  (TIM17); module ms at the end
     y 7 0000 <48 base64 chars>                   its counts (int8)
     b B r=9 ms=..                                a burst not dumped (under DUMP_DB, or past MAXCAP)
     m B n=54 caps=12 ms=60105                    the turn's bursts and captures
     acap done n=739 caps=60
   The dumps are too long for telemetry: the cable is needed. Writes REG_34[3:0], REG_33 bit 8,
   REG_7E and REG_13, the MCU's TIM17 and its clock enable (a cycle counter), all put back; the
   firmware retunes after. Everything big is in the scratch,
   so the stack stays small. Returns the captures, or -2 (a key held at the start), -3 (old
   image), -4 (PB1 not an input), -5 (REG_33 refused), -7 (TIM17 in use), -8 (scratch too small).

   Built with AISCAP_EMU, a count is one read of 0x480004F0 instead (tests/test_k6_ais.py: the
   emulator can't poll 23 M reads a second, so it hands over each count and its time).
   SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 2
#define K6_MODULE_OPT z
#include "../k6_mod.h"

#ifndef CH_A_HZ
#define CH_A_HZ 161975000u
#endif
#ifndef CH_B_HZ
#define CH_B_HZ 162025000u
#endif
#ifndef FIRST_B
#define FIRST_B 1 /* start on channel B */
#endif
#ifndef SWITCH_S
#define SWITCH_S 60u
#endif
#ifndef RUN_S
#define RUN_S 600u
#endif
#ifndef DUMP_DB
#define DUMP_DB 11 /* dump a burst only this far over the floor (run 2: frames only at 12+) */
#endif
#ifndef MAXCAP
#define MAXCAP 60u
#endif
#include "aispoll.h"

static const char b64[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

/* the ring from its oldest count on, in gp0cap's y lines */
NOINLINE static void dump(const k6_api_t *api, const int8_t *buf, uint32_t head, uint32_t k) {
  char line[64], *p;
  uint32_t off, i, j, m, v;
  for (off = 0; off < NCAP; off += 36) {
    p = put(line, "y ");
    p = dec(p, k);
    *p++ = ' ';
    p = hex4(p, off);
    *p++ = ' ';
    j = head + off;
    if (j >= NCAP) j -= NCAP;
    for (i = off; i < off + 36 && i < NCAP; i += 3) {
      v = 0;
      for (m = 0; m < 3; m++) {
        v <<= 8;
        if (i + m < NCAP) v |= (uint8_t)buf[j];
        if (++j == NCAP) j = 0;
      }
      *p++ = b64[(v >> 18) & 63];
      *p++ = b64[(v >> 12) & 63];
      *p++ = b64[(v >> 6) & 63];
      *p++ = b64[v & 63];
    }
    *p = 0;
    api->say(line);
  }
}

NOINLINE static void cap_lines(const k6_api_t *api, const cap_t *c, int b, uint32_t k,
                               uint32_t hz, int d) {
  char line[64], *p;
  p = put(line, "c 1 ");
  p = dec(p, GAIN13 ? 1u : 0u);
  *p++ = ' ';
  p = dec(p, k);
  p = put(p, " ks=");
  p = dec(p, udiv(hz * 8u + 62, 125)); /* hz * 64 / 1000, rounded */
  p = put(p, " r=");
  p = dec(p, (uint32_t)d);
  p = put(p, " t=");
  p = dec(p, api->ticks() & 0xFFFF);
  p = put(p, b ? " lost=0 ch=B" : " lost=0 ch=A");
  *p = 0;
  api->say(line);
  p = put(line, "e ");
  p = dec(p, k);
  p = put(p, " pre=");
  p = dec(p, PRE);
  p = put(p, " c0=");
  p = dec(p, c->c0);
  p = put(p, " c1=");
  p = dec(p, c->c1);
  p = put(p, " ms=");
  p = dec(p, c->end_ms);
  *p = 0;
  api->say(line);
}

int k6_main(const k6_api_t *api) {
  int8_t *buf;
  bp_t bp;
  cap_t c;
  ap_ck_t ck = {0, 0};
  uint32_t room, k = 0, nb = 0, turn_n, turn_c, until, hz, apb2;
  uint16_t r33, r34, r7e, r13;
  int32_t fl;
  int b = FIRST_B, quit = 0, d;
  char line[64], *p;
  if (api->abi != 1 || api->level < 2) return -3;
  if (api->key_down()) return -2;
  buf = (int8_t *)api->scratch(&room);
  if (room < NCAP) return -8;
  if (rd(GPIOB) & (3u << 2)) return -4;
  apb2 = rd(RCC_APB2ENR);
  if (apb2 & TIM17EN) return -7;
  r33 = api->fd_read(0x33);
  r34 = api->fd_read(0x34);
  r7e = api->fd_read(0x7E);
  r13 = api->fd_read(0x13);
  if ((r33 & 0x0100) && api->fd_write(0x33, (uint16_t)(r33 & ~0x0100)) != 0) return -5;
  wr(RCC_APB2ENR, apb2 | TIM17EN);
  wr(TIM17 + 0x00, 0);
  wr(TIM17 + 0x28, 0);      /* PSC: 96 MHz */
  wr(TIM17 + 0x2C, 0xFFFF); /* ARR */
  wr(TIM17 + 0x00, 1);      /* CEN */
  p = put(line, "acap start a=");
  p = dec(p, CH_A_HZ);
  p = put(p, " b=");
  p = dec(p, CH_B_HZ);
  p = put(p, " g13=");
  p = hex4(p, GAIN13);
  p = put(p, " ncap=");
  p = dec(p, NCAP);
  *p = 0;
  api->say(line);
  bp.x1 = bp.x2 = bp.y1 = bp.y2 = bp.e = 0;
  while (!quit && api->ms() < RUN_S * 1000u) {
    api->tune(b ? CH_B_HZ : CH_A_HZ, WIDE);
    api->fd_write(0x34, (uint16_t)((r34 & 0xFFF0) | 13));
    if (GAIN13) {
      api->fd_write(0x7E, (uint16_t)((r7e & 0x0FFF) | 0x8000 | (3u << 12)));
      api->fd_write(0x13, GAIN13);
    }
    api->delay_us(30000);
    hz = calibrate(&bp, &fl);
    p = put(line, b ? "f B hz=" : "f A hz=");
    p = dec(p, hz);
    p = put(p, " fl=");
    p = dec(p, (uint32_t)fl);
    *p = 0;
    api->say(line);
    turn_n = 0, turn_c = 0;
    until = api->ms() + SWITCH_S * 1000u;
    if (until > RUN_S * 1000u) until = RUN_S * 1000u;
    for (;;) {
      bp.e = fl; /* the energy at the floor again after a dump or a turn */
      listen(api, &ck, buf, &bp, fl * TRIG_X, until, &c);
      if (c.got < 0) quit = 1;
      if (c.got <= 0) break;
      turn_n++, nb++;
      d = db_over(c.emax, fl);
      if (k < MAXCAP && d >= DUMP_DB) {
        cap_lines(api, &c, b, k, hz, d);
        dump(api, buf, c.head, k);
        k++, turn_c++;
      } else {
        p = put(line, b ? "b B r=" : "b A r=");
        p = dec(p, (uint32_t)d);
        p = put(p, " ms=");
        p = dec(p, c.end_ms);
        *p = 0;
        api->say(line);
      }
      if (api->ms() >= until) break;
    }
    p = put(line, b ? "m B n=" : "m A n=");
    p = dec(p, turn_n);
    p = put(p, " caps=");
    p = dec(p, turn_c);
    p = put(p, " ms=");
    p = dec(p, api->ms());
    *p = 0;
    api->say(line);
    b = !b;
  }
  wr(TIM17 + 0x00, 0);
  wr(RCC_APB2ENR, apb2);
  api->fd_write(0x13, r13);
  api->fd_write(0x7E, r7e);
  api->fd_write(0x34, r34);
  api->fd_write(0x33, r33);
  p = put(line, "acap done n=");
  p = dec(p, nb);
  p = put(p, " caps=");
  p = dec(p, k);
  *p = 0;
  api->say(line);
  return (int)k;
}
