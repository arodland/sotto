/* gp0sweep: gp0dma's capture and demodulator at 12 MS/s, run block after block (50 ms each)
   while a carrier lasts, to measure the received deviation of a stepped tone sweep against the
   chip's own AF level (REG_6F[5:0], dB): the deviation is what reached the K6's antenna, the AF
   level what is left after the chip's audio filters. Lines:
     sweep start floor=-103 blk=4688
     sweep waiting for a carrier
     s 12 tone=1000 rms=2980 dev=4150 af=33 if=16990       (block 12)
   rms: the rms of each IF cycle's frequency about the last block's mean (Hz; x sqrt 2 = the
   peak deviation of a sine, plus noise); dev: half the swing of a 4-cycle average; tone: Hz.
   Returns the blocks, or gp0dma's errors.
   SPDX-License-Identifier: MIT */
#define GP0_SWEEP 1
#include "gp0dma.c"
