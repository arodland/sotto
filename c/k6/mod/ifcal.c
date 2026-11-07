/* ifcal: the one-time frequency calibration. Measures where a bare carrier on the pager's
   frequency lands on the GP0 receive IF, with the chip's AFC off, and writes the offset to the
   radio's calibration record in SPI flash (k6_cal.h, module API level 4), so every IF-tap
   module can read it instead of the sender aiming its audio by hand (ft8wf's --f0 1624 at
   440.850 MHz, 2026-11-06).

   It is gp0dma's capture and demodulator (DMA flat out, L 64; gp0dma.c) run one ~105 ms block
   at a time on the carrier: IFCAL_BLOCKS blocks, each block's mean IF, then their mean in
   0.1 Hz. Steps: the upload's carrier drops; AFC off (REG_73 bit 4) and RX on again so it holds
   (gp0rx.h); up to 90 s for a carrier 15 dB over the floor; IFCAL_SETTLE_MS; the blocks while the
   carrier lasts. The sender keys the IC-7100 in FM with no modulation for about 8 s.

   Lines:
     ifcal start floor=-118 rx=440850000 old=-14550        (old: the record's off_chz, or "none")
     ifcal waiting for a carrier
     ifcal if=16781.6 n=32 spread=2.0 rssi=-41
     ifcal off=-145.5 Hz ppb=-330 wrote                    (or "dry run": built with IFCAL_DRY)
   Returns the offset in 0.1 Hz (-1455 for -145.5 Hz), or gp0dma's errors (-2 key, -3 image
   below level 4, -4 PB1, -5 REG_33, -6 no carrier, -7 TIM17 / DMA busy, -8 scratch), -9 too
   few blocks or a spread over IFCAL_SPREAD_DHZ (the carrier wandered or dropped: nothing
   written), -10 the write refused, -11 the sector is neither blank nor a K6CL record (something
   else uses it: nothing measured or written). Writes what gp0dma writes and REG_73, all put
   back, and one 4 KB SPI flash sector (KM_CAL_ADDR). Receive only. SPDX-License-Identifier: MIT */
#define K6_MODULE_LEVEL 4
#define K6_MODULE_OPT z /* the DMA ring needs 516 B of the store past the code */
#define GP0_SWEEP 1
#define GP0_NO_MAIN 1
#define TAG "ifcal"
#include "gp0dma.c"
#include "k6_cal.h"

#ifndef IFCAL_BLOCKS
#define IFCAL_BLOCKS 32
#endif
#ifndef IFCAL_SETTLE_MS
#define IFCAL_SETTLE_MS 800 /* the sender's carrier up and steady, the RX restart behind us */
#endif
#ifndef IFCAL_SPREAD_DHZ
#define IFCAL_SPREAD_DHZ 300 /* 30 Hz: a steady carrier reads within a few Hz block to block */
#endif

/* v in tenths, signed: -1455 -> "-145.5" */
static char *dtenth(char *p, int32_t v) {
  uint32_t a = (uint32_t)(v < 0 ? -v : v), q = udiv(a, 10);
  if (v < 0) *p++ = '-';
  p = dec(p, q);
  *p++ = '.';
  *p++ = (char)('0' + a - 10 * q);
  return p;
}

int k6_main(const k6_api_t *api) {
  saved_t sv;
  uint8_t *ring;
  k6_cal_t c;
  blk_t blk;
  uint32_t sum = 0, lo = 0xFFFFFFFFu, hi = 0, n = 0, i;
  uint16_t v[IFCAL_BLOCKS];
  uint16_t r73;
  int32_t ifd, off, ppb;
  int ret, have, rssi0;
  char line[64], *p;
  if (api->level < 4) return -3;
  have = k6cal_get(api, &c) == 0;
  /* the sector is the record's alone: blank, or a record of ours (even a bad one). Anything
     else is someone else's data, and cal_write would erase it */
  if (!have) {
    const uint32_t *w = (const uint32_t *)&c;
    uint32_t a = w[0];
    if (a != K6CAL_MAGIC) {
      for (i = 1; i < sizeof c / 4; i++) a &= w[i];
      if (a != 0xFFFFFFFFu) {
        return -11;
      }
    }
  }
  ret = setup(api, &sv, &ring);
  if (ret) return ret;
  r73 = api->fd_read(0x73);
  api->fd_write(0x73, (uint16_t)(r73 | 0x0010)); /* AFC off, then RXON so it takes hold */
  api->tune(api->rx_hz(), api->fd_read(0x43) == 0x3028);
  p = put(line, TAG " start floor=");
  p = sdec(p, sv.floor);
  p = kv(p, " rx=", api->rx_hz());
  p = put(p, " old=");
  p = have ? sdec(p, c.off_chz) : put(p, "none");
  say0(api, line, p);
  ret = carrier(api, sv.floor);
  if (ret) goto out;
  api->delay_us(IFCAL_SETTLE_MS * 1000u);
  rssi0 = rssi(api);
  blk.ifm = 17000;
  for (i = 0; i < IFCAL_BLOCKS && rssi(api) >= sv.floor + 15; i++) {
    uint32_t lost = run(ring, sv.rsz, 64, 1, &blk, (int)blk.ifm);
    if (lost || blk.ifm < 15000 || blk.ifm > 19000) continue;
    v[n] = (uint16_t)blk.ifm;
    sum += blk.ifm, n++;
    if (blk.ifm < lo) lo = blk.ifm;
    if (blk.ifm > hi) hi = blk.ifm;
    if (api->key_poll()) {
      ret = -2;
      goto out;
    }
  }
  /* every block, in order: the first in Hz, then each as a step from the one before */
  for (i = 0; i < n;) {
    p = put(line, TAG " b");
    p = dec(p, i);
    *p++ = ' ';
    p = i ? sdec(p, (int)v[i] - (int)v[i - 1]) : dec(p, v[0]);
    for (i++; i < n && p - line < 54; i++) {
      *p++ = ' ';
      p = sdec(p, (int)v[i] - (int)v[i - 1]);
    }
    say0(api, line, p);
  }
  if (n < IFCAL_BLOCKS / 2 || (hi - lo) * 10 > IFCAL_SPREAD_DHZ) {
    p = kv(line, TAG " refused n=", n);
    p = kv(p, " spread=", n ? hi - lo : 0);
    say0(api, line, p);
    ret = -9;
    goto out;
  }
  ifd = (int32_t)udiv(sum * 10 + n / 2, n); /* 0.1 Hz */
  off = ifd * 10 - K6CAL_NOMINAL_CHZ;       /* 0.01 Hz */
  {
    /* ppb = off_chz x 1e7 / hz = off_chz x 1e4 / (hz / 1000): |off| < 2 kHz keeps it in range */
    uint32_t a = (uint32_t)(off < 0 ? -off : off), khz = udiv(api->rx_hz(), 1000);
    ppb = (int32_t)udiv(a * 10000u + khz / 2, khz);
    if (off < 0) ppb = -ppb;
  }
  p = put(line, TAG " if=");
  p = dtenth(p, ifd);
  p = kv(p, " n=", n);
  p = put(p, " spread=");
  p = dtenth(p, (int32_t)(hi - lo) * 10);
  p = put(p, " rssi=");
  p = sdec(p, rssi0);
  say0(api, line, p);
  {
    uint8_t *z = (uint8_t *)&c;
    for (i = 0; i < sizeof c; i++) z[i] = 0;
  }
  c.magic[0] = 'K', c.magic[1] = '6', c.magic[2] = 'C', c.magic[3] = 'L';
  c.version = 1, c.flags = 1;
  c.off_chz = off, c.ppb = ppb, c.hz = api->rx_hz();
  c.blocks = (uint16_t)n, c.spread_dhz = (uint16_t)((hi - lo) * 10), c.rssi = (int16_t)rssi0;
  c.check = k6cal_sum(&c);
  ret = (off < 0 ? -(int)udiv((uint32_t)-off + 5, 10) : (int)udiv((uint32_t)off + 5, 10));
  p = put(line, TAG " off=");
  p = dtenth(p, ret);
  p = put(p, " Hz ppb=");
  p = sdec(p, ppb);
#ifdef IFCAL_DRY
  p = put(p, " dry run");
#else
  if (api->cal_write(&c, sizeof c) != 0) {
    p = put(p, " write refused");
    ret = -10;
  } else {
    p = put(p, " wrote");
  }
#endif
  say0(api, line, p);
out:
  api->fd_write(0x73, r73);
  teardown(api, &sv);
  return ret;
}
