/* sotto: 2400 bit/s speech codec, 48 bits per 20 ms frame.
 *
 * C port of sotto v3.5 (sim/vocoder_lab.py SottoV35) with the real-time pitch tracker
 * (fixed-lag Viterbi, 80 ms look-ahead). Analysis and synthesis reuse Codec 2's sinusoidal
 * model (libcodec2, LGPL-2.1, linked); this code is MIT.
 *
 * Frame: voicing 2 | Wo 7 | energy 5 | shape 34, MSB first, packed into 6 bytes. The shape
 * bits are vector-quantizer indices; their split depends on the codebook the library was
 * built with (12+11+11 by default), and sotto_format() names it. Both ends must match.
 *
 * Input level: see "Level" below. Speech at an active level near -26 dBFS (the usual
 * telephone and broadcast level) uses the energy field well; above -7 dBFS per 10 ms it
 * saturates, below -77 dBFS it decodes as silence.
 *
 * Encoder: feed 160 samples (20 ms, 8 kHz, 16-bit) per call. Output starts after the
 * tracker's look-ahead: the first frame comes out on the 6th call. At the end, call
 * sotto_enc_flush until it returns 0 to get the remaining frames.
 *
 * Decoder: one call per received frame, with good = 0 when the frame failed its CRC (the
 * decoder then conceals it). Each call returns 160 samples, 20 ms behind: the first call
 * returns silence. sotto_dec_flush returns the last 20 ms.
 */
#ifndef SOTTO_H
#define SOTTO_H

#ifdef __cplusplus
extern "C" {
#endif

#define SOTTO_SAMPLES_PER_FRAME 160
#define SOTTO_SAMPLES_PER_FRAME_16K 320     /* v3.8: 16 kHz in and out */
#define SOTTO_BITS_PER_FRAME 48
#define SOTTO_BYTES_PER_FRAME 6
#define SOTTO_SAMPLE_RATE 8000

#define SOTTO_VERSION_MAJOR 0
#define SOTTO_VERSION_MINOR 1
#define SOTTO_VERSION_PATCH 0

/* Symbols of the shared library. Define SOTTO_STATIC when linking libsotto.a on Windows. */
#if defined(_WIN32) && !defined(SOTTO_STATIC)
#  ifdef SOTTO_BUILD
#    define SOTTO_API __declspec(dllexport)
#  else
#    define SOTTO_API __declspec(dllimport)
#  endif
#elif defined(__GNUC__) && defined(SOTTO_BUILD) && !defined(__TI_COMPILER_VERSION__)
#  define SOTTO_API __attribute__((visibility("default")))
#else
#  define SOTTO_API
#endif

/* ---- Level -------------------------------------------------------------------------------
 * Samples are signed 16-bit, full scale +-32767. Levels below are dBFS of RMS,
 * 20*log10(rms / 32768), so a full-scale sine is -3 dBFS.
 *
 * The 5-bit energy field codes each 20 ms frame's energy (measured over a 10 ms analysis
 * window) in 2.26 dB steps. Field value in dB = frame RMS dBFS + SOTTO_ENERGY_OFFSET_DB.
 *   - frames quieter than SOTTO_LEVEL_SILENT_DBFS are sent as code 0 and decode as silence;
 *   - frames louder than SOTTO_LEVEL_MAX_DBFS saturate at code 31: they decode at that level,
 *     so the loudest syllables are compressed rather than distorted;
 *   - SOTTO_LEVEL_TARGET_DBFS is the recommended active speech level (ITU-T P.56). The test
 *     voices sit at -22 to -27 dBFS, with their loudest 10 ms at -11 to -15 dBFS, leaving
 *     3 to 8 dB before saturation. Short sample peaks up to full scale are fine: the field
 *     codes RMS, not peaks.
 * The decoder reproduces the coded level, so its output sits at the input level. */
#define SOTTO_ENERGY_OFFSET_DB 87.3
#define SOTTO_ENERGY_MIN_DB 10.0           /* code 0 (silence) .. code 31 */
#define SOTTO_ENERGY_MAX_DB 80.0
#define SOTTO_LEVEL_SILENT_DBFS (-76.2)    /* below this: code 0 */
#define SOTTO_LEVEL_MAX_DBFS (-7.3)        /* above this: code 31 */
#define SOTTO_LEVEL_TARGET_DBFS (-26.0)

struct sotto_enc;
struct sotto_dec;

/* "sotto v3.5, shape 12-11-11": the bitstream format this build speaks. */
SOTTO_API const char *sotto_format(void);

SOTTO_API struct sotto_enc *sotto_enc_create(void);
SOTTO_API void sotto_enc_destroy(struct sotto_enc *e);
/* Returns the number of frames written to out (0 or 1). */
SOTTO_API int sotto_encode(struct sotto_enc *e, const short speech[SOTTO_SAMPLES_PER_FRAME],
                 unsigned char out[SOTTO_BYTES_PER_FRAME]);
/* Feeds a last 10 ms (80 samples) before the flush, for inputs that end mid-frame. */
SOTTO_API void sotto_encode_half(struct sotto_enc *e, const short speech[SOTTO_SAMPLES_PER_FRAME / 2]);
/* Ends the stream. Returns 1 and writes a frame while frames remain, then 0. */
SOTTO_API int sotto_enc_flush(struct sotto_enc *e, unsigned char out[SOTTO_BYTES_PER_FRAME]);

SOTTO_API struct sotto_dec *sotto_dec_create(void);
SOTTO_API void sotto_dec_destroy(struct sotto_dec *d);
SOTTO_API void sotto_decode(struct sotto_dec *d, const unsigned char in[SOTTO_BYTES_PER_FRAME], int good,
                  short speech[SOTTO_SAMPLES_PER_FRAME]);
SOTTO_API void sotto_dec_flush(struct sotto_dec *d, short speech[SOTTO_SAMPLES_PER_FRAME]);

/* ---- v3.8: 16 kHz, with the 4-8 kHz hiss band (builds with VARIANT=v3.8, the default) ----
 * Same 48-bit frame; a v3.7 decoder plays a v3.8 stream without the high band. The 8 kHz
 * functions above still work in a v3.8 build: sotto_encode is the plain v3.7 encoder, and
 * sotto_decode decodes v3.7 or v3.8 streams at 8 kHz.
 *
 * Encoder: feed 320 samples (20 ms at 16 kHz) per call. Frames come out 5 + SOTTO_RULE_LAG + 1
 * calls after the speech went in (the core's 90 ms plus 10 ms for the resampler, then
 * SOTTO_RULE_LAG frames of look-ahead for the voicing rules and one more for the index: the
 * first frame on the 17th call with the default lag of 10); call sotto_enc16_flush until it
 * returns 0 at the end.
 * Decoder: one call per received frame; each returns 320 samples SOTTO_DEC16_DELAY_FRAMES
 * frames behind the encoder's input (the first calls return silence). Call sotto_dec16_flush
 * until it returns 0 to drain the last frames. */
#ifdef SOTTO_V39
#define SOTTO_DEC16_DELAY_FRAMES 6      /* v3.9: one more 10 ms STFT frame for the non-s cap */
#else
#define SOTTO_DEC16_DELAY_FRAMES 5
#endif

struct sotto_enc16;
struct sotto_dec16;
SOTTO_API struct sotto_enc16 *sotto_enc16_create(void);
SOTTO_API void sotto_enc16_destroy(struct sotto_enc16 *e);
SOTTO_API int sotto_encode16(struct sotto_enc16 *e, const short speech[SOTTO_SAMPLES_PER_FRAME_16K],
                             unsigned char out[SOTTO_BYTES_PER_FRAME]);
SOTTO_API int sotto_enc16_flush(struct sotto_enc16 *e, unsigned char out[SOTTO_BYTES_PER_FRAME]);
SOTTO_API struct sotto_dec16 *sotto_dec16_create(void);
SOTTO_API void sotto_dec16_destroy(struct sotto_dec16 *d);
SOTTO_API void sotto_decode16(struct sotto_dec16 *d, const unsigned char in[SOTTO_BYTES_PER_FRAME], int good,
                              short speech[SOTTO_SAMPLES_PER_FRAME_16K]);
SOTTO_API int sotto_dec16_flush(struct sotto_dec16 *d, short speech[SOTTO_SAMPLES_PER_FRAME_16K]);
/* the library's own SOTTO_DEC16_DELAY_FRAMES (5 in v3.8, 6 in v3.9), for programs that
   include this header without the library's VARIANT flags */
SOTTO_API int sotto_dec16_delay_frames(void);

#ifdef __cplusplus
}
#endif
#endif
