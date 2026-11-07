/* pager_host [-x] [-p] [-b BYTES] PACKETS.bin OUT.raw: the K6 pager's receive and play path on
 * this machine, for tests. PACKETS.bin holds whitened packets back to back as the radio's
 * FIFO would hand them over (64 bytes each, 128 with -x for profile XL), in arrival order; lost packets are
 * simply absent. Writes the played message as 8 kHz 16-bit raw (the decoder's first 20 ms of
 * delay included, as on the radio) and prints one line of status:
 *   results=<sp_packet codes> total= stored= got= packets= of= complete= bad= ... missing= rebuilt=
 * -b sets the store size (default 6144 bytes). -S turns stream mode off (store then play; the
 * sotto 1200 build streams by default). -s HALVES simulates live streaming: playback starts as
 * soon as sp_stream_ready says so and plays HALVES x 20 ms after each packet (32 is real time
 * for XL at 1800 baud: 640 ms on air per packet), and the line gains ring= waits= (frames
 * not in yet when their turn came), overruns= and start= (packets in when playback began); only decoded frames are written, so the
 * output matches a store-then-play of the same packets. -p writes the PWM compare values the radio would
 * send to TIM1 instead (uint16, top 2000, automatic gain, pre-emphasis on), via sp_play_pwm. pager_host -t prints the
 * calibration test packet (sp_test_packet) in hex. The builds with the codebook in SPI flash
 * (pager_host_cbs, pager_host_lr_cbs, pager_host_cbi; sp_cb.h) take -F IMAGE (a codebook image
 * put at SP_CB_ADDR in the emulated flash before sp_cb_setup) and -W FILE (the image region
 * written out after it), and print cb= (sp_cb_setup's result) mode= before max_frames and
 * cb_reads= (vectors fetched) after pcm=. -B N -F IMAGE prints the sum of N random reads
 * (sp_cb_bench), as the radio's `K6 CB bench n=N ... sum=` line should. The sotto 600 / 400 builds
 * (pager_host_vlr*, -DSP_VLR) also print vlr= (sp_vlr_setup's result) after cb=, and with their
 * tables in SPI flash take -V IMAGE (put at SP_VLR_ADDR) and -X FILE (that region written out
 * after the setup), and print vlr_reads= after cb_reads=. pager_host -e [-x] PACKETS.bin prints, for each packet,
 * sp_test_errors: errors seq swap invert and the eight block counts. pager_host -w OUT.u16 writes
 * the speaker sweep (sp_sweep_pwm, top 2000, SP_SWEEP_RATE values a second) as uint16.
 * SPDX-License-Identifier: MIT */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "sotto_pager.h"
#include "sp_cb.h"
#include "sp_vlr.h"
#ifdef FX_CB_SPI
#include "flash_host.h"
#endif

int main(int argc, char **argv) {
  int len = SP_PAYLOAD_L, n, r, c, pwm = 0, ber = 0, halves = 0, playing = 0, waits = 0, ov = 0, ringf, npk = 0, start = 0;
  unsigned bytes = 6144;
  static uint32_t mem[65536];
  uint8_t pkt[SP_PAYLOAD_MAX];
  int16_t pcm[SP_FRAME_SAMPLES];
  sp_status_t st;
  FILE *in, *out;
  const char *cb_load = 0, *cb_save = 0, *vlr_load = 0, *vlr_save = 0;
  unsigned cb_bench = 0;
  if (argc == 3 && !strcmp(argv[1], "-w")) {          /* the speaker sweep's PWM values */
    uint16_t seg[160];
    int more = 1, tail = 0;
    if (!(out = fopen(argv[2], "wb"))) { perror("pager_host"); return 1; }
    sp_sweep_start();
    while (more || !tail) {
      more = sp_sweep_pwm(seg, 160, 2000);
      if (!more) tail = 1;
      fwrite(seg, 2, 160, out);
    }
    fclose(out);
    return 0;
  }
  if (argc == 2 && !strcmp(argv[1], "-t")) {          /* the calibration test packet, hex */
    int i;
    sp_test_packet(pkt);
    for (i = 0; i < SP_PAYLOAD_L; i++) printf("%02x%c", pkt[i], i == SP_PAYLOAD_L - 1 ? '\n' : ' ');
    return 0;
  }
  while (argc > 1 && argv[1][0] == '-' && argv[1][1]) {
    if (!strcmp(argv[1], "-e")) ber = 1, argv++, argc--;
    else if (!strcmp(argv[1], "-x")) len = SP_PAYLOAD_XL, argv++, argc--;
    else if (!strcmp(argv[1], "-p")) pwm = 1, argv++, argc--;
    else if (!strcmp(argv[1], "-S")) sp_set_stream(0), argv++, argc--;
    else if (!strcmp(argv[1], "-s") && argc > 2) halves = atoi(argv[2]), argv += 2, argc -= 2;
    else if (!strcmp(argv[1], "-b") && argc > 2) bytes = (unsigned)atoi(argv[2]), argv += 2, argc -= 2;
    else if (!strcmp(argv[1], "-F") && argc > 2) cb_load = argv[2], argv += 2, argc -= 2;
    else if (!strcmp(argv[1], "-W") && argc > 2) cb_save = argv[2], argv += 2, argc -= 2;
    else if (!strcmp(argv[1], "-V") && argc > 2) vlr_load = argv[2], argv += 2, argc -= 2;
    else if (!strcmp(argv[1], "-X") && argc > 2) vlr_save = argv[2], argv += 2, argc -= 2;
    else if (!strcmp(argv[1], "-B") && argc > 2) cb_bench = (unsigned)atoi(argv[2]), argv += 2, argc -= 2;
    else break;
  }
#ifdef FX_CB_SPI
  if (cb_bench) {      /* what the radio's `K6 CB bench n=N ... sum=` should say */
    int r;
    if (cb_load && flash_host_load(cb_load, SP_CB_ADDR) < 0) { perror("pager_host -F"); return 1; }
    r = sp_cb_setup();
    printf("cb=%d n=%u sum=%08x\n", r, cb_bench, (unsigned)sp_cb_bench(cb_bench));
    return 0;
  }
#endif
  if (argc == 3 && !strcmp(argv[1], "-c")) {        /* control records found in each packet */
    uint8_t rec[6];
    if (!(in = fopen(argv[2], "rb"))) { perror("pager_host"); return 1; }
    while (fread(pkt, 1, (size_t)len, in) == (size_t)len) {
      int how = sp_control(pkt, len, rec), i;
      printf("%d", how);
      for (i = 0; how && i < 6; i++) printf(" %02x", rec[i]);
      printf("\n");
    }
    return 0;
  }
  if (ber && argc == 2) {                             /* bit errors against the test message */
    sp_biterr_t e;
    if (!(in = fopen(argv[1], "rb"))) { perror("pager_host"); return 1; }
    while (fread(pkt, 1, (size_t)len, in) == (size_t)len) {
      int i;
      sp_test_errors(pkt, len, &e);
      printf("%d %d %d %d", e.errors, e.seq, e.swap, e.invert);
      for (i = 0; i < 8; i++) printf(" %d", e.block[i]);
      printf("\n");
    }
    return 0;
  }
  if (argc != 3 || bytes > sizeof mem) {
    fprintf(stderr, "usage: %s [-x] [-p] [-b BYTES] PACKETS.bin OUT.raw | [-x] -c PACKETS.bin\n", argv[0]);
    return 1;
  }
  in = fopen(argv[1], "rb"), out = fopen(argv[2], "wb");
  if (!in || !out) { perror("pager_host"); return 1; }
#ifdef FX_CB_SPI
  if (cb_load && flash_host_load(cb_load, SP_CB_ADDR) < 0) { perror("pager_host -F"); return 1; }
  printf("cb=%d mode=%d ", sp_cb_setup(), sp_cb_mode());
  if (cb_save) flash_host_save(cb_save, SP_CB_ADDR, SP_CB_HDR + sp_cb_table_bytes());
#else
  (void)cb_load, (void)cb_save;
  sp_cb_setup();
#endif
#ifdef VLR_CB_SPI
  if (vlr_load && flash_host_load(vlr_load, SP_VLR_ADDR) < 0) { perror("pager_host -V"); return 1; }
#endif
#ifdef SP_VLR
  printf("vlr=%d ", sp_vlr_setup());
#else
  sp_vlr_setup();
#endif
#ifdef VLR_CB_SPI
  if (vlr_save) flash_host_save(vlr_save, SP_VLR_ADDR, SP_VLR_HDR + sp_vlr_table_bytes());
#else
  (void)vlr_load, (void)vlr_save;
#endif
  n = sp_init(mem, bytes);
  printf("max_frames=%d static=%u results=", n, sp_static_bytes());
  while (fread(pkt, 1, (size_t)len, in) == (size_t)len) {
    c = sp_check(pkt, len);
    r = sp_packet(pkt, len);
    if (c != (r >= 0)) { fprintf(stderr, "sp_check %d disagrees with sp_packet %d\n", c, r); return 2; }
    printf("%d,", r);
    npk++;
    if (halves && !playing && sp_stream_ready()) sp_play_start(), playing = 1, start = npk;
    for (c = 0; playing == 1 && c < halves; c++) {
      int q = sp_play_frame(pcm);
      if (q == SP_PLAY_WAIT) waits++;
      else if (q) fwrite(pcm, sizeof pcm[0], SP_FRAME_SAMPLES, out);
      else playing = 2;
    }
  }
  sp_stream_end();                  /* the sender stopped */
  sp_status(&st);
  printf(" total=%u stored=%u got=%u packets=%u of=%u complete=%d good=%u bad=%u id=%u auto_gain=%d ", st.total,
         st.stored, st.frames_got, st.packets_got, st.packets_total, st.complete, st.good_packets, st.bad_packets,
         st.msg_id, sp_auto_gain_q8());
  {
    uint16_t miss[8];
    int i, nm = sp_missing(miss, 8);
    printf("missing=%d", nm);
    for (i = 0; i < nm && i < 8; i++) printf("%c%u", i ? ',' : ':', miss[i]);
    printf(" rebuilt=%d\n", sp_rebuilt());
  }
  if (halves) {
    if (!playing) sp_play_start(), start = npk;
    while (playing != 2) {
      int q = sp_play_frame(pcm);
      if (q == SP_PLAY_WAIT) { fprintf(stderr, "pager_host: wait after the end\n"); return 3; }
      if (q) fwrite(pcm, sizeof pcm[0], SP_FRAME_SAMPLES, out);
      else playing = 2;
    }
    ringf = sp_stream_info(&ov);
    printf("ring=%d waits=%d overruns=%d start=%d\n", ringf, waits, ov, start);
  } else if (sp_play_start(), pwm) {
    uint16_t v[SP_FRAME_SAMPLES];
    int g = sp_auto_gain_q8();
    while (sp_play_pwm(v, 2000, g, 1)) fwrite(v, sizeof v[0], SP_FRAME_SAMPLES, out);
  } else {
    while (sp_play_frame(pcm)) fwrite(pcm, sizeof pcm[0], SP_FRAME_SAMPLES, out);
  }
  {
    unsigned ns;
    uint32_t h = sp_pcm_hash(&ns);
    printf("pcm=%08x samples=%u", (unsigned)h, ns);   /* the radio's PLAY line prints the same */
#ifdef FX_CB_SPI
    printf(" cb_reads=%u", (unsigned)sp_cb_reads());
#endif
#ifdef VLR_CB_SPI
    printf(" vlr_reads=%u", (unsigned)sp_vlr_reads());
#endif
    printf("\n");
  }
  fclose(out);
  return 0;
}
