/* pager_host [-s] [-p] [-b BYTES] PACKETS.bin OUT.raw: the K6 pager's receive and play path on
 * this machine, for tests. PACKETS.bin holds whitened packets back to back as the radio's
 * FIFO would hand them over (64 bytes each, 16 with -s), in arrival order; lost packets are
 * simply absent. Writes the played message as 8 kHz 16-bit raw (the decoder's first 20 ms of
 * delay included, as on the radio) and prints one line of status:
 *   results=<sp_packet codes> total= stored= got= packets= of= complete= bad=
 * -b sets the store size (default 6144 bytes). -p writes the PWM compare values the radio would
 * send to TIM1 instead (uint16, top 2000, automatic gain, pre-emphasis on), via sp_play_pwm. pager_host -t prints the
 * calibration test packet (sp_test_packet) in hex. pager_host -e [-s] PACKETS.bin prints, for each packet,
 * sp_test_errors: errors seq swap invert and the eight block counts.
 * SPDX-License-Identifier: MIT */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "sotto_pager.h"

int main(int argc, char **argv) {
  int len = SP_PAYLOAD_L, n, r, c, pwm = 0, ber = 0;
  unsigned bytes = 6144;
  static uint32_t mem[65536];
  uint8_t pkt[SP_PAYLOAD_L];
  int16_t pcm[SP_FRAME_SAMPLES];
  sp_status_t st;
  FILE *in, *out;
  if (argc == 2 && !strcmp(argv[1], "-t")) {          /* the calibration test packet, hex */
    int i;
    sp_test_packet(pkt);
    for (i = 0; i < SP_PAYLOAD_L; i++) printf("%02x%c", pkt[i], i == SP_PAYLOAD_L - 1 ? '\n' : ' ');
    return 0;
  }
  while (argc > 1 && argv[1][0] == '-' && argv[1][1]) {
    if (!strcmp(argv[1], "-e")) ber = 1, argv++, argc--;
    else if (!strcmp(argv[1], "-s")) len = SP_PAYLOAD_S, argv++, argc--;
    else if (!strcmp(argv[1], "-p")) pwm = 1, argv++, argc--;
    else if (!strcmp(argv[1], "-b") && argc > 2) bytes = (unsigned)atoi(argv[2]), argv += 2, argc -= 2;
    else break;
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
    fprintf(stderr, "usage: %s [-s] [-p] [-b BYTES] PACKETS.bin OUT.raw\n", argv[0]);
    return 1;
  }
  in = fopen(argv[1], "rb"), out = fopen(argv[2], "wb");
  if (!in || !out) { perror("pager_host"); return 1; }
  n = sp_init(mem, bytes);
  printf("max_frames=%d static=%u results=", n, sp_static_bytes());
  while (fread(pkt, 1, (size_t)len, in) == (size_t)len) {
    c = sp_check(pkt, len);
    r = sp_packet(pkt, len);
    if (c != (r >= 0)) { fprintf(stderr, "sp_check %d disagrees with sp_packet %d\n", c, r); return 2; }
    printf("%d,", r);
  }
  sp_status(&st);
  printf(" total=%u stored=%u got=%u packets=%u of=%u complete=%d good=%u bad=%u id=%u auto_gain=%d\n", st.total,
         st.stored, st.frames_got, st.packets_got, st.packets_total, st.complete, st.good_packets, st.bad_packets,
         st.msg_id, sp_auto_gain_q8());
  sp_play_start();
  if (pwm) {
    uint16_t v[SP_FRAME_SAMPLES];
    int g = sp_auto_gain_q8();
    while (sp_play_pwm(v, 2000, g, 1)) fwrite(v, sizeof v[0], SP_FRAME_SAMPLES, out);
  } else {
    while (sp_play_frame(pcm)) fwrite(pcm, sizeof pcm[0], SP_FRAME_SAMPLES, out);
  }
  fclose(out);
  return 0;
}
