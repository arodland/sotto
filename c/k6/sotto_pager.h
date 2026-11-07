/* sotto voice pager: receive a sotto message as FSK packets, keep it in RAM, play it.
 *
 * The receive side of the K6 voice pager (docs/k6-receiver.md): the radio's FSK modem hands
 * over 64-byte packets in the K6 voice-pager packet format, version 1 (profile L), or 128-byte
 * packets (profile XL); this code de-whitens them, checks them, puts each sotto frame in its
 * place in a RAM store with a good flag, and plays the message through the fixed-point
 * decoder. Nothing is written to flash. The radio code (Aura, Rust) owns the FSK registers,
 * the screen and the PWM output; it calls only the functions below.
 *
 * The packet format: k6-fsk-packet-spec.md (project files, ht-survey/k6fsk/), reference
 * k6fsk_ref.py. In short, profile L: PN9-whitened 64 bytes = magic 0xA7, msg_id, seq (2),
 * total frames (2), 8 frames of 6 bytes, 8 reserved, CRC-16/CCITT-FALSE (2); packet k of a
 * group g of 8 carries frames g*64 + s*8 + k, s = 0..7. Profile XL: the same 6-byte header,
 * 20 frames, no reserved bytes, CRC (128 bytes); packet k of group g carries frames
 * g*160 + s*8 + k, s = 0..19. The top 2 bits of seq give the interleave depth D (0: 8, 1: 4,
 * 2: 2, 3: 1 packets per group; seq is the low 13 bits), and with it packet k of group g carries
 * frames g*F*D + s*D + k (F frames per packet). Seq bit 13 marks a short last group (senders
 * from 2026-11-07): its rem < F*D frames take m = ceil(rem / F) packets instead of D, packet k
 * carrying frames g*F*D + s*m + k; m comes from total. (Profile S, 16 bytes, is no longer
 * taken: the radio reads 64-byte packets fine.) Pause frames (sotto 2400 and 1200, senders from
 * 2026-11-07): a good slot with voicing 0, Wo 127, energy 0 and marker 0xA5304B in bits 24-47
 * plays as N slots (bits 14-23) of the last real slot again, with its good flag.
 * Optional parity packets (magic | 0x10, one after each group) rebuild a group's one lost
 * packet exactly: sp_rebuilt.
 *
 * No heap, no floating point, no 64-bit division: everything lives in the caller's buffer
 * (sp_init) and in this library's static decoder state (about 0.9 KB plus a 2 KB cos/sin
 * table). Not reentrant; call from one thread (Aura's main loop).
 * SPDX-License-Identifier: MIT */
#ifndef SOTTO_PAGER_H
#define SOTTO_PAGER_H
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define SP_PAYLOAD_L 64           /* profile L packet */
#define SP_PAYLOAD_XL 128         /* profile XL packet */
#define SP_PAYLOAD_MAX SP_PAYLOAD_XL
#define SP_MAGIC 0xA7               /* v3.7 voice packets */
#define SP_MAGIC_LR 0xA9            /* sotto 1200 voice packets: each 6-byte slot is 40 ms */
#define SP_MAGIC_600 0xAC           /* sotto 600: each slot is an 80 ms superframe */
#define SP_MAGIC_400 0xAD           /* sotto 400: each slot is a 120 ms superframe */
/* The codec this build plays: v3.7, or sotto 1200 with -DSP_LR, and with -DSP_LR -DSP_VLR also
   sotto 600 and 400 (docs/sotto-vlr.md; their tables in SPI flash, sp_vlr.h). Every build
   takes every kind of voice packet (CRC, sync, store, bit-error tests); a message of a codec
   the build doesn't play stays unplayed. */
#ifdef SP_LR
#define SP_PLAY_MAGIC SP_MAGIC_LR
#else
#define SP_PLAY_MAGIC SP_MAGIC
#endif
#define SP_FRAME_BYTES 6
#define SP_FRAME_SAMPLES 160      /* 20 ms at 8 kHz */
#define SP_GAP_MS 3000            /* play after this long with no packet for the message */

/* sp_packet results */
enum {
  SP_BAD_LENGTH = -3,
  SP_BAD_MAGIC = -2,             /* de-whitened byte 0 is not 0xA7, 0xA9, 0xAC or 0xAD (or one | 0x10) */
  SP_BAD_CRC = -1,
  SP_DUP = 0,                    /* good packet, nothing new in it */
  SP_ADDED = 1,                  /* good packet with new frames */
  SP_NEW = 2,                    /* first packet of a new message (the old one is dropped) */
  SP_COMPLETE = 3                /* this packet brought the message's last missing frame */
};

/* Message store in the caller's buffer (4-byte aligned). Returns the longest message it
   holds, in frames (6.2 bytes per frame; 6 KB holds 976 frames, 19.5 s),
   or 0 if the buffer is too small. Clears any message. */
int sp_init(void *mem, unsigned bytes);

/* One received packet, as the chip's FIFO gave it (whitened), SP_PAYLOAD_L or SP_PAYLOAD_XL
   bytes. Returns one of the codes above. The checked, de-whitened packet is kept for
   sp_last_packet. */
int sp_packet(const uint8_t *payload, int len);

/* 1 if the packet's magic byte and CRC are right, else 0. It changes nothing:
   the radio uses it to try the other byte order or polarity before the first good packet. */
int sp_check(const uint8_t *payload, int len);

/* Per-packet diagnostics: the de-whitened bytes of the last packet passed to sp_packet,
   good or bad (the screen shows the magic byte and the CRC result). */
const uint8_t *sp_last_packet(void);

typedef struct {
  int16_t have;                  /* 1 while a message is in the store */
  uint8_t msg_id;
  uint8_t profile;               /* 'L' or 'X' (XL); 'l' or 'x' for sotto 1200, 600 or 400 (sp_magic) */
  uint16_t total;                /* frames in the message */
  uint16_t stored;               /* frames that fit the store (= total unless it was truncated) */
  uint16_t frames_got;           /* frames received, of stored */
  uint16_t packets_got;          /* distinct packets received */
  uint16_t packets_total;        /* packets in one pass (0 while unknown) */
  uint16_t good_packets;         /* packets accepted since sp_init, all messages */
  uint16_t bad_packets;          /* CRC or magic failures since sp_init */
  int16_t complete;              /* every stored frame arrived */
} sp_status_t;
void sp_status(sp_status_t *st);
int sp_magic(void);              /* the stored message's magic byte (its codec), 0 for none */
/* Packets of the stored message rebuilt from parity packets (spec 14): after each interleave
   group the sender may add one packet whose byte 0 is the codec's magic | 0x10 (0xB7, 0xB9,
   0xBC, 0xBD) and whose bytes 6 .. len-3 are the XOR of the group's data packets' bytes there.
   sp_packet takes it like a voice packet; when exactly one of the group's packets is missing
   it rebuilds that one (its frames become good, its seq counts as received, sp_missing no
   longer lists it) and returns SP_ADDED or SP_COMPLETE, else SP_DUP. Builds with -DSP_NO_PARITY
   (tools/k6_build.sh sets it for the images that link the 30 KB codebook into the MCU's flash,
   which have no room) take parity packets as SP_DUP and rebuild nothing. */
int sp_rebuilt(void);
/* The seqs (packets of one pass) not received yet, up to max of them into out; returns how many
   are missing in all (0 while the total is unknown). */
int sp_missing(uint16_t *out, int max);

/* Playback. sp_play_start resets the decoder and starts at frame 0 (it can be called again
   to replay). sp_play_frame decodes the next 20 ms into pcm (160 samples, 8 kHz) and returns
   1, or returns 0 once the message and the decoder's 20 ms delay are done. Frames that never
   arrived are concealed. With -DSP_LR (sotto 1200) each stored slot is 40 ms, played as two
   calls of 20 ms (sotto_lr_decode_half), the decoder's 80 ms of delay dropped at the start; a
   message of the other codec plays nothing (returns 0). With -DSP_VLR a sotto 600 / 400 slot
   is 4 / 6 calls (sotto_vlr_decode_frame), the decoder's 2 superframes of delay dropped. Takes about 55-75 MHz-milliseconds per call on the K6's M0 at
   96 MHz (docs/k6-receiver.md), so it must not run inside an interrupt. */
void sp_play_start(void);
int sp_play_frame(int16_t pcm[SP_FRAME_SAMPLES]);
int sp_play_pos(void);           /* frames played so far */
/* FNV-1a (32-bit, offset 2166136261, prime 16777619) over the int16 samples played since
   sp_play_start, low byte first, before the PWM conversion; samples counts them. The serial
   PLAY report prints it, so a PC decoding the same frames and good flags (c/k6/pager_host,
   bit-exact with the radio) can check the playback without anyone listening. */
uint32_t sp_pcm_hash(unsigned *samples);

/* Stream mode (on by default in the sotto 1200 build, off in v3.7's, whose packets are slower
   on air than real time): a message's frames go through the store as a ring of whole
   interleave groups (4 XL groups, 25.6 s of sotto 1200, in the K6's 4 KB), so a message can
   be any length and play while the rest arrives. sp_stream_ready says when the first group
   is final: all D of its packets are in (D = 8 unless the header says 4, 2 or 1), or a later group's have started (packets come in seq
   order), or sp_stream_end was called (the sender stopped: the radio heard nothing for a
   while). Then sp_play_start and sp_play_frame as usual, interleaved with sp_packet; a frame
   whose group isn't final yet makes sp_play_frame return SP_PLAY_WAIT (sp_play_pwm: silence)
   without decoding, so the decoded samples and pcm hash match a store-then-play of the same
   packets. A group that arrives while the group whose place it takes is still unplayed counts
   an overrun (the sender ran too far ahead; the newer frames win). A total of 0xFFFF is an
   open-ended stream: sp_stream_end stops playback after the last group heard. sp_set_stream
   applies from the next new message; sp_stream_info returns the ring size in frames (0 when
   the message doesn't stream). Replaying (sp_play_start again) a message longer than the
   ring plays the overwritten groups wrong. */
#define SP_PLAY_WAIT 2
/* Builds with -DFX_YIELD_HOOK (the K6's): f is called from inside each decode, after at most
   about 0.2 ms of work between calls (c/k6/bench_k6.py --yield-gap measures it), so the radio
   can empty its RF chip's 8-word FIFO while a frame decodes; 0 for none. f must not touch the
   store or call this library. */
void sp_set_yield(void (*f)(void));
void sp_yield_probe(void);
void sp_set_stream(int on);
int sp_stream(void);
int sp_stream_ready(void);
void sp_stream_end(void);
int sp_stream_info(int *overruns);

/* PCM to PWM compare values for the K6's TIM1 CH4 path (PA11 -> two RC poles -> speaker
   amplifier), in place of the stock voice prompts' (s + 2048) >> 1. out[i] is in
   0..top with top / 2 for silence (top = TIM1 ARR, 2000 for the stock 48 kHz carrier).
   gain_q8: 256 maps full scale to +-top (speech at the usual -26 dBFS then swings about
   +-5 % of the range); louder clips. emphasis: 1 runs the samples through
   (33 x[n] - 21 x[n-1] + 4 x[n-2]) / 16 first, which undoes the RC filter's -10 dB at
   3 kHz (computed from the schematic, so +-0.5 dB to 3.5 kHz on paper; not measured). The
   filter's history carries over between calls; sp_play_start clears it. */
void sp_pcm_to_pwm(const int16_t *pcm, uint16_t *out, int n, unsigned top, int gain_q8, int emphasis);

/* A gain_q8 for sp_pcm_to_pwm that puts the message's loudest frame (from the frames' 5-bit
   energy fields) at about -13 dBFS, between -6 and +18 dB: senders' levels vary by 15 dB or
   more, and the PWM path has no other volume control. 256 for an empty store. */
int sp_auto_gain_q8(void);

/* sp_play_frame then sp_pcm_to_pwm in one call, with the PCM in this library's buffer:
   what the radio's playback loop calls for each 20 ms. Returns as sp_play_frame. */
int sp_play_pwm(uint16_t out[SP_FRAME_SAMPLES], unsigned top, int gain_q8, int emphasis);

/* Speaker sweep (control action "sweep"): PWM compare values for a list of steady tones, to
   measure the K6's audio chain (PWM pin, RC filter, amplifier, speaker) with a microphone.
   The radio plays them with TIM1 updating at SP_SWEEP_RATE (RCR 2: 16 kHz, twice the pager's
   8 kHz), so the tones reach 7.5 kHz. Schedule: SP_SWEEP_LEAD_MS of silence (the noise
   floor), then each of the SP_SWEEP_N tones sp_sweep_hz[] for SP_SWEEP_TONE_MS followed by
   SP_SWEEP_GAP_MS of silence; all at the same level, top / 8 peak (about 7 dB above speech at
   the pager's usual gain), with no pre-emphasis. The PWM's sample-and-hold takes
   20 log10(sinc(f / SP_SWEEP_RATE)) off each tone (-3.4 dB at 7.5 kHz): the analysis adds it
   back. sp_sweep_start resets it; sp_sweep_pwm fills n values and returns 1, or 0 once the
   schedule has ended (out is then silence). */
#define SP_SWEEP_RATE 16000
#define SP_SWEEP_N 14
#define SP_SWEEP_LEAD_MS 1000
#define SP_SWEEP_TONE_MS 1000
#define SP_SWEEP_GAP_MS 250
extern const uint16_t sp_sweep_hz[SP_SWEEP_N];
void sp_sweep_start(void);
int sp_sweep_pwm(uint16_t *out, int n, unsigned top);

/* Profile L test packet 0 of the spec's section 10 (70-frame test message, msg_id 0x42),
   whitened, for the calibration transmit key. */
void sp_test_packet(uint8_t out[SP_PAYLOAD_L]);

/* Bit errors of a received packet (as sp_packet would take it) against the spec's 70-frame
   test message, for the first on-air tests: the closest of the message's packets (16 in L,
   8 in XL; the calibration key and `modem.k6 test` send packet 0), read as given, with each
   16-bit word's two bytes swapped, with every bit inverted, and both. Errors rising towards
   the end of the packet (block) point at a baud-rate mismatch; a burst at one place, at a
   lost or extra bit. Returns errors, or -1 for a wrong len. About 1 ms on the K6. */
typedef struct {
  int16_t errors;                /* bit errors against the closest reading (of 512, or 1024 in XL) */
  int16_t seq;                   /* which test packet that was */
  uint8_t swap, invert;          /* 1 if that reading swapped the bytes / inverted the bits */
  uint8_t block[8];              /* errors in each eighth of the packet (8 bytes in L, 16 in XL) */
} sp_biterr_t;
int sp_test_errors(const uint8_t *payload, int len, sp_biterr_t *e);

/* Control packet (spec section 13): an L or XL payload, whitened like a voice packet, that
   holds 8-byte records repeated end to end, each 0xA8, seq, mask, values, var|vol<<4, action,
   CRC-16 (big-endian) of the 6 bytes before it. The radio applies the first record whose CRC
   checks. Copies early in the packet survive a link that slips later in the packet (D/1200).
   Reads the payload as given, with each 16-bit word's bytes swapped, inverted, and both.
   Returns 0 if no record checks, else 1 + (swapped | inverted << 1) with the record in rec. */
#define SP_CTL_MAGIC 0xA8
#define SP_CTL_REC 8
enum { SP_CTL_PHY = 1, SP_CTL_FRAMES = 2, SP_CTL_BAUD = 4, SP_CTL_AUTO = 8, SP_CTL_EMPH = 16,
       SP_CTL_VAR = 32, SP_CTL_VOL = 64, SP_CTL_IDLE_KEEP = 128 };
/* mask bits; the value is the same bit of byte 3 (phy 1 = F, frames 1 = XL, baud 1 = 2400,
   idle keep 1 = no re-arm on an idle RX-done with no sync) */
enum { SP_CTL_NONE = 0, SP_CTL_PLAY = 1, SP_CTL_CLEAR = 2, SP_CTL_REPORT = 3,  /* byte 5 */
       SP_CTL_REG = 4,      /* write register byte 2 = bytes 3-4 (high first); 0 drops all */
       SP_CTL_REGS = 5 };   /* print the FSK registers */
int sp_control(const uint8_t *payload, int len, uint8_t rec[6]);

/* The PN9 whitening bytes (SP_PAYLOAD_MAX of them) and the packets' CRC-16/CCITT-FALSE, for
   the module loader (k6_mod.c), which shares the voice packets' framing. */
const uint8_t *sp_pn9(void);
unsigned sp_crc16(const uint8_t *p, int n);

/* Bytes of static RAM this library uses (decoder state, cos/sin table and the rest). */
unsigned sp_static_bytes(void);

#ifdef __cplusplus
}
#endif
#endif
