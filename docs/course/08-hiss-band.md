---
title: "8. The hiss band: s, sh and whispers"
---

# 8. The hiss band: s, sh and whispers

Previous: [How we measure "better"](07-measuring.md) · [Course home](README.md) · Next: [Glossary](glossary.md)

## Big idea

Chapters 1 to 7 describe a vocoder that only knows sounds below 4,000 Hz. That's fine for vowels, but the hiss of an "s" lives mostly *above* 4,000 Hz, so sotto's "s", "sh" and "f" sounded dull and thin. **sotto v3.8** adds a **hiss band** from 4,000 to 8,000 Hz without adding a single bit to the 48-bit frame. It finds bits that were being wasted, spends them on the hiss, and guesses the rest.

This chapter explains the ideas behind the hiss band, and then the mistakes and surprises the team ran into while building it. The second half matters as much as the first: it's what doing this kind of work is really like.

## Why fricatives need high frequencies

Remember from chapter 1: with 8,000 samples per second you can only capture sounds up to 4,000 Hz, half the sample rate. That limit is called the **Nyquist frequency** (say *NY-kwist*). Anything higher can't be stored at all.

For a vowel, that's no problem. Its harmonics and formants that matter for understanding sit below 4,000 Hz. But **fricatives** (chapter 2) are different. The narrow gap that makes an "s" is tiny, and tiny gaps make high-pitched noise. In studio recordings, most of the energy of an "s" is between 4,000 and 8,000 Hz. Cut that off and an "s" starts to sound like an "f" or a "th", which is why people on the phone say "S as in Sam, F as in Frank". The hissing fricatives "s", "sh", "z" and "zh" have their own name: **sibilants** (say *SIB-uh-lunts*).

![The same phrase three ways: the original, sotto v3.7 and sotto v3.8](figures/hiss.svg)

These are spectrograms like the ones in chapter 2, but now going up to 8,000 Hz (the orange dashed line is 4,000 Hz). The top one is 1.8 seconds of a LibriSpeech reader recorded at 16,000 samples per second. Look at the dark block at 8.7 seconds: that's a fricative, and nearly all of it is above the line. In the middle, sotto v3.7 keeps only the part below 4,000 Hz, so that hiss is almost gone. At the bottom, sotto v3.8 puts it back. It also adds a faint layer above 4,000 Hz under the vowels, which you'll meet later in this chapter.

| Clip | What it is |
|---|---|
| <audio controls preload="none" src="audio/hiss_original.flac"></audio><br>[hiss_original.flac](audio/hiss_original.flac) | The original, 16,000 samples per second |
| <audio controls preload="none" src="audio/hiss_v37.flac"></audio><br>[hiss_v37.flac](audio/hiss_v37.flac) | sotto v3.7: nothing above 4,000 Hz |
| <audio controls preload="none" src="audio/hiss_v38.flac"></audio><br>[hiss_v38.flac](audio/hiss_v38.flac) | sotto v3.8: the same 2,400 bits per second, with the hiss band |

(Headphones help a lot here. Small laptop speakers often can't play much above 4,000 Hz either.)

So v3.8 takes in 16,000 samples per second and plays out 16,000. The 0 to 4,000 Hz part still goes through the vocoder of chapters 2 to 6 (now version 3.7, plus a few new voicing rules you'll meet below). The 4,000 to 8,000 Hz part is new, and it's always *noise*: the decoder never tries to rebuild harmonics up there, only hiss shaped to the right loudness.

## First, a quieter problem: fricatives were 3 dB too quiet

Before adding anything above 4,000 Hz, the team measured what was wrong *below* it. On frames where a fricative was loud, sotto's output had about the right shape but was about **3 dB too quiet**. Two things caused it, and both are nice pieces of math.

**Overlap-add loses power.** The decoder makes each 10 ms of sound separately and cross-fades neighbours with a triangle-shaped window: one fades out (weight *w*) while the next fades in (weight 1 − *w*). For buzz that's fine, because the two neighbours line up. But noise has random phases, so the two halves don't add like waves; their *powers* add. The power left at each moment is *w*² + (1 − *w*)². Averaged over the fade, that's 2/3, and 10 × log₁₀(2/3) = **−1.76 dB**. (Codec 2 has the same loss.)

**Averaging logs isn't averaging powers.** The rate-K envelope (chapter 3) samples a noisy spectrum and averages it in decibels. The average of the logs is always lower than the log of the average, so the envelope sits about 1.3 dB too low on noise.

1.76 + 1.3 ≈ 3 dB. The fix is to turn unvoiced frames up by 3 dB, but only frames whose envelope tilts upward toward high frequencies (real fricatives). Boosting every unvoiced frame also boosted pauses and breaths, and made a scratch at the creaky start of the vowel in "act" louder.

Here's a surprise: in the first listening round, the 3 dB fix *by itself* tied with no fix twice. Measured, yes; heard, no. The hiss band was what listeners noticed.

## Finding spare bits

Every bit of the 48-bit frame already has a job (chapter 4). So where can hiss information go? The team found bits that were working but not *useful*.

In an **all-unvoiced frame** (voicing code U U, both halves hiss), the 7 pitch bits only set how far apart the decoder's noise harmonics are. Noise doesn't have a pitch, so what if the decoder ignored those bits and always used the same spacing?

- At a fixed **100 Hz** spacing, PESQ dropped a little and it sounded rough. Here's why: 100 Hz harmonics with random phases add up to a pattern that *repeats every 10 ms*, a faint buzz under every "s". That was the old Codec 2 unvoiced sound.
- At a fixed **50 Hz** spacing, the pattern repeats every 20 ms, which is the length of a whole frame, so inside a frame it never repeats. It sounds like real noise, and PESQ was the same as before.

So with 50 Hz spacing, the 7 pitch bits of a U U frame are free. U U frames are about 24% of all frames in read speech (pauses aren't silent: they're breath and room noise). That's

> 7 bits × 24% × 50 frames per second ≈ **84 bits per second**

of free space, sitting exactly in the frames that are fricatives.

Mixed and edge frames still need their pitch (they have a buzz in them), so voiced fricatives like "z" and "v" get no spare bits. Keep that in mind: it comes back as a bug later.

## The hiss index

How should a few bits describe the 4,000 to 8,000 Hz band? sotto splits it into four 1,000 Hz-wide bands (4 to 5, 5 to 6, 6 to 7, and 7 to 8 kHz) and describes the loudness of each.

**Step 1: a guess.** The decoder already has the 0 to 4,000 Hz envelope. Its top end gives a clue about what's above, so a small formula (trained on LibriSpeech) predicts the four band levels from it. This costs no bits at all. On its own it was poor for fricatives: off by 11 to 14 dB, which is about as bad as always guessing the same number.

**Step 2: the correction.** The encoder runs the same guess, sees how wrong it is, and sends a **hiss index** that fixes it. The index picks one of 64 patterns of four corrections (a small vector quantizer like the one in chapter 4, trained on LibriSpeech). That's 6 bits, and it brings the error down to about 4 to 6 dB. (The first version used all 7 bits. The 7th bit got a different job, below.)

This "guess, then send the correction" trick is very common in compression. The guess is free, so the bits only have to carry what the guess got wrong.

The decoder then makes noise at 16,000 samples per second, shapes it to the four band levels in 20 ms pieces, and adds it on top of the normal 0 to 4,000 Hz output.

### Bursts and mixed frames

The first listening round with the hiss band was a clear win: it beat v3.7 in every pair a listener answered. But the notes said "t" and "p" needed more hiss. A **stop consonant** ("t", "p", "k") is a short pop of air, a **burst**, and it usually runs straight into the vowel. So its frame was coded mixed, and mixed frames got no index.

Two fixes:

- **The burst rule** (encoder): a frame where 4,000 to 8,000 Hz jumps by more than 12 dB in 10 ms is sent as U U, so it gets an index. That turns about 6 frames per 12 seconds into U U.
- **A guess for mixed frames** (decoder): mixed frames get the free guess, turned down 3 dB to be safe.

In the next round, the version with both won 6 to 1 against the hiss band without them.

## The vowel layer

Bright voices (and bright microphones) have some energy above 4,000 Hz in their *vowels* too. Leave it out and the hiss of a consonant pops on and off against a vowel that's dull. Adding it turned out to be the hardest part, and listeners' words steered every step:

1. **A loudness offset per speaker:** clearer, but "scratchy". Measured, it put 5,000 to 7,000 Hz 4 to 10 dB too loud.
2. **The 0 to 4,000 Hz spectrum folded upward** (mirrored about 4,000 Hz, so the pitch pulses carry on up): "terrible", like "a tambourine full of BBs".
3. **One smooth, quiet layer**, faded in and out over about 30 ms: better on both test voices.
4. A listener's idea: a whispered vowel keeps its **resonances**, so the high band should be "a high, quiet whistle within the vowel", not flat noise. Recordings of two people's vowels, voiced and whispered, showed the idea was right: each vowel has one resonance somewhere between about 4,300 and 7,300 Hz, and it *moves with the vowel* (one voice: about 5,100 Hz in front vowels like "ee", about 6,000 Hz in back vowels like "oo"). They also showed the overall loudness up there depends mostly on the microphone: the same person was 10 to 15 dB brighter on a phone than on a desk microphone.

So the final vowel layer predicts **sixteen 250 Hz-wide bands** from 4,000 to 8,000 Hz out of the shape of the decoded 0 to 4,000 Hz spectrum. The prediction is a **ridge regression**, a weighted sum fitted to many LibriSpeech vowels, with a penalty that keeps the weights small so it doesn't over-fit. It's then turned down 5 dB. That follows each vowel's resonance as it moves. What it can't know is the speaker and microphone, and that's what the next part sends.

## The speaker side channel

How each voice and microphone differs from the average is fairly simple. When the team measured how far the vowel prediction missed for many speakers and ran **PCA** (principal component analysis, said as letters: a way of finding the few patterns that explain most of the differences in a big table of numbers), three patterns explained 92% of the speaker-to-speaker differences. So a speaker's high band can be described with just **3 numbers**. With them, the vowel layer's error dropped from 11.9 dB to 8.4 dB.

Three numbers × 4 bits each = 12 bits, plus a 4-bit CRC (chapter 6) to catch errors = a **16-bit message**. Where can 16 bits go? This is the 7th pitch bit of every U U frame. Each U U frame carries one bit of the message, so the message travels on a slow, separate path through the stream: a **side channel**.

- Time is cut into **2-second windows** of 100 frames. Both ends count frames, so both know where a window starts.
- The encoder measures the 3 numbers from the loud vowels it has heard so far and sends them one bit per U U frame. It doesn't need to look ahead.
- When the 16th bit arrives and the CRC checks out, the decoder switches to the new speaker shape. If a frame is lost before then, that message is dropped and the old shape stays.
- Until the first message arrives, the decoder uses the average speaker.

On the listening page, v3.8 had two rows: **"shape measured"**, where the 3 numbers came straight from the original recording (the best the side channel could ever do), and **"over the air"**, where they came through the side channel as a real radio would get them. If the side channel works, the two should sound the same.

They didn't, at first. A listener heard "shape measured" clearly beat "over the air" on the word "poems", 1 second into a clip that started at 4 seconds. The cause: the encoder waited 1 second before measuring, the message carried only what it had measured in *earlier* windows, and the decoder applied it only at the *next* window. The first real shape arrived at 6 seconds, after the word. Two changes, both needing no new bits: start measuring after 0.5 s, and apply a message the moment its 16th bit arrives. Now the shape lands at about 3 seconds. One voice still waits 8.6 seconds: this speaker pauses so little that his windows have only 9 to 11 U U frames, fewer than the 16 bits a message needs. The next idea is shorter 8-bit messages, one number at a time.

## Whispers, and keeping it smooth

Whispering is all hiss, so it should be easy. It wasn't, and each fix came from something a listener heard:

- **A low warble under whispered vowels.** v3.7 coded 93% of one whisper as *mixed*: the pitch tracker found a "pitch" in pure noise and played wandering harmonics. The **whisper rule** sends a run of at least 10 mixed frames as U U if the harmonics are weak (fit less than 6 dB above the noise) and the "pitch" jumps around. Whispers are now 85 to 93% U U; normal speech hardly changes.
- **Hiss popping on and off in a whispered "ah".** The frames flickered between U U (consonant hiss) and mixed (vowel layer). Now the decoder uses **one blended layer**, the louder of the two at each frequency, and ignores flickers of 40 ms or less inside a vowel. A version that let the hiss fade out over 20 ms sounded like "a hard plate reverb" (an old echo effect made with a big metal sheet), so the fade was removed.
- **A stutter in the "zh" of "division".** The voicing flipped between voiced, mixed and edge every 20 ms. With hiss switching on and off at the same time, the stutter got easier to hear. **Voicing smoothing** turns short blips inside a voiced run into mixed frames.
- **A weak "s" that started inside a vowel.** The **sibilant rule** sends a mixed frame as U U when its 4,000 to 8,000 Hz band is louder than its 0 to 1,000 Hz band, so the "s" gets an index.

## What we learned along the way

The ideas above are the tidy version. Here is what actually happened, because the mistakes taught as much as the successes.

### 1. The pitch-field leak (a real bug)

Remember chapter 4's "sending half the frames": the decoder fills in the missing 10 ms between frames by interpolation. For pitch, there's a special case. If a 10 ms piece is voiced but the half before it is unvoiced, it has no earlier pitch to blend with, so it takes the pitch of the **next** frame.

In v3.7 that was harmless. In v3.8, if the next frame is U U, its pitch field doesn't hold a pitch any more: it holds the hiss index and a side-channel bit. So just before some fricatives, 10 ms of buzz was played at a *random* pitch. In one clip the number of harmonics jumped from 39 to 78 for one subframe. It happened only a few times per clip, so nobody heard it as a bug, but it moved PESQ by up to 0.1 per clip.

The fix is one line: in that case, keep the frame's own pitch. **Lesson: when you give an old field a new meaning, find every piece of code that reads it.** The encoder changed what the 7 bits meant, but an interpolation step deep inside the decoder still read them as pitch.

### 2. The "z" of "prism"

A listener said "act as a prism" still sounded like "act as a prith-um". The first diagnosis looked at a "z" at 8.1 seconds, found it was coded mixed, and proposed a fix. Then the listener pointed out that the clip being studied said "rainbow *is* a division", not "prism": the team had analyzed the wrong word.

The real "z" of "prism" (4.76 to 4.82 seconds) was coded **voiced**. The speaker's voice buzz there was weak, 5 dB under the vowel, and the 6,000 to 8,000 Hz hiss was almost as loud as that buzz. A voiced frame has no hiss index, and the vowel layer predicted a dark vowel, so the hiss came out 30 to 50 dB too quiet. A "z" without its hiss sounds like "th".

The **z fix**: any frame whose 4,000 to 8,000 Hz band comes within 10 dB of its 0 to 1,000 Hz band is sent as U U with a hiss index, *whether it's voiced or not*. That throws away 20 ms of buzz under the "z", but which consonant you hear depends far more on the hiss than on the buzz. (Try the whisper test in Try it question 6.) PESQ went down a little (2.868 to 2.844); the listener said "all of the clips with the z fix sound great". Ears won.

### 3. Get the time index first

The "t" of "sunlight strikes" sounded like "sunlike". The team looked for the problem at 1.1 seconds, where the "t" of "strikes" is, and found real-looking evidence there: the burst was 10 to 15 dB short at low frequencies. They built and tested six fixes: decoder rules for the postfilter on bursts and onsets, an encoder rule for burst contrast, a sharper attack. Some were even on a listening page. None helped the listener.

Then the listener gave the **time index**: 0.28 to 0.52 seconds. The problem was the "t" of "sunlight", in a completely different place. Every one of the six fixes had been aimed at a frame that was fine, and all of them were deleted.

**Lesson: when a listener reports a problem, get the exact time and quote it back ("you mean 0.28 to 0.52 seconds, the word 'sunlight'?") before analyzing anything.** Combined with the wrong "prism" word above, this happened twice in one day. It's now a rule of the project.

### 4. Knowing when to stop: the "t" of "sunlight"

At 0.28 to 0.52 seconds, the "t" of "sunlight" runs straight into the "s" of "strikes": one 80 ms cluster of hiss, with the voice buzz still underneath. Four of its five frames were coded mixed, so only one got a hiss index. The z fix sent the others as U U too, and afterwards the hiss matched the original within 2 to 3 dB every 5 ms. The listener still heard "sunlike".

So whatever is missing isn't the hiss. A "t" is a closure and a burst about 10 ms long, and sotto's frames are 20 ms, with the middle 10 ms filled in by interpolation. The frame is simply too coarse to place the burst. The team and the listener agreed to stop: some problems are limits of the design, and the right move is to write that down and move on. A future version might send a 1-bit "sharp attack" flag, but that needs a bit no frame has free.

### 5. The regression: 50 Hz spacing went missing

A **regression** in software is a bug that breaks something that used to work. After the cleanup that deleted the six wrong-place fixes, the main decoder file was rebuilt from the copy on the project's main branch. That copy was missing one line: the call that switches U U frames to 50 Hz noise spacing (and adds the 3 dB fricative boost). Nothing crashed and nothing printed an error. But U U frames were now played at whatever "pitch" their hiss index happened to spell: a different random pitch every 20 ms, under every "s". A whole listening page was rendered that way. Luckily it was caught, and the page re-rendered, before the listener heard it.

The fix put the line back and added a **test** that checks U U frames really decode at 50 Hz spacing, so the same mistake can't slip through again. **Lesson: after a cleanup, compare the output (scores, or the audio itself) with the output before the cleanup. "It runs without errors" doesn't mean "it does the same thing".** Notice that this is the same kind of mistake as the pitch-field leak: code that still treated the hiss bits as a pitch.

### 6. PESQ can't hear the hiss band

The usual PESQ (chapter 7) works at 8,000 samples per second, so it can't hear anything above 4,000 Hz at all. It also mildly dislikes the 50 Hz noise spacing on some voices (one voice lost 0.27). The wideband version of PESQ, which does go up to 8,000 Hz, scored the *exact true* hiss levels as worse, because it punishes noise added where the original has structure. So for this whole chapter, the deciding votes came from listening rounds. v3.8 scores about 2.84 on the 0 to 4,000 Hz PESQ against v3.7's 2.91, and in listening it won round after round. On the last page, ten fresh phrases, the verdict was "everything sounds great". As chapter 7 says, a computer score only *predicts* what people hear.

## What it costs

- **Bits:** none. Still 48 bits every 20 ms. It is a new stream version, though: a v3.7 decoder can play a v3.8 stream, but it reads the hiss index as a pitch and plays rougher noise, with no high band.
- **Delay:** the encoder rules (whisper, voicing smoothing and the rest) look at the frames around each frame, so the encoder now waits 10 frames: 320 ms instead of v3.7's 90 ms. The decoder adds 100 ms instead of 20. For push-to-talk radio that's acceptable; for a phone call it would be a lot.
- **The radio:** the C version of v3.8 exists in both floating point and fixed point (95% of frames identical between the two, hiss levels within 0.3 dB). Its speed on the radio's DSP chip hasn't been measured yet. And there's an open question: the hiss band only helps if the FTM-300D's microphone and speaker can pass 4,000 to 8,000 Hz. Nobody knows yet.

## Try it

1. To capture sounds up to 8,000 Hz, how many samples per second do you need? How many samples is one 20 ms frame at that rate?
2. Random-phase noise harmonics 100 Hz apart repeat every 10 ms. How often do they repeat at 50 Hz spacing? At 25 Hz? Why is 50 Hz enough for a 20 ms frame?
3. Use a calculator: what is 10 × log₁₀(2/3)? If the overlap-add loss were the only problem, how many dB would you add back?
4. A voice has 24 U U frames in a 2-second window. The side channel needs 16 of them for one message. On average, how far into the window does the message finish? Another voice has only 10 U U frames per window. What happens?
5. A friend says "your recording sounds weird near the start." Before you open any software, what should you ask them?
6. Whisper "sip", then whisper "zip". Can a listener tell which is which? What does that say about whether the buzz or the hiss tells you it's a "z"?

## Where this is in the code

- `sim/hiss_band.py`: `SottoV38`, the Python reference: the hiss index, the burst, whisper, voicing-smoothing and sibilant rules (`sib_db`, `sib_voiced` for the z fix), the vowel layer (`vowel_shape="fine"`) and the speaker side channel (`vowel="speaker"`). `UU_WO_GUARD` is the pitch-field leak fix.
- `sim/vocoder_lab.py`: `interp_wo()` (where the leak was) and the `_model_hook` call that applies the 50 Hz unvoiced spacing.
- `tests/test_vocoder.py`: `test_v38_unvoiced_frames_decode_at_50_hz_spacing`, the regression test.
- `tools/train_hiss_band.py`: trains the guess and the hiss codebook (`sim/data/hiss_band.npz`).
- `c/sotto_hiss.c` and `c/fixed/sotto_fx_hiss.c`: the floating-point and fixed-point C versions (`make -C c`, then `c/sotto_enc -16` and `c/sotto_dec -16`).
- `docs/unvoiced-hiss.md`: the full design notes, including every dead end, with measurements.
- `docs/c-port.md`: the C port, the look-ahead and the delay.

<details markdown="1"><summary>Answers</summary>

1. 16,000 samples per second (twice 8,000 Hz). 16,000 × 0.020 = 320 samples per frame.
2. Every 1/50 s = 20 ms at 50 Hz, and every 40 ms at 25 Hz. A 20 ms frame is exactly one repeat at 50 Hz, so inside a frame the noise never repeats and you don't hear a buzz.
3. About −1.76 dB, so you'd add back 1.76 dB. (The other 1.3 dB comes from averaging in decibels, which is why sotto adds 3 dB.)
4. 16 of 24 is two-thirds of the way, so about 1.3 seconds into the 2-second window (if the U U frames are spread evenly). With 10 U U frames, the window ends after 10 bits, the message is never finished, and the decoder keeps the old speaker shape. That's the voice whose shape took 8.6 seconds to arrive.
5. *Exactly* where: the time in seconds, or the word. Then repeat it back to them to make sure you mean the same spot.
6. Usually not. Whispering takes the buzz away from both, and they sound almost the same. So the buzz is the only difference between "s" and "z", and it's a weak clue. Losing the hiss of a "z" turns it into a different consonant ("th"), but losing its buzz mostly leaves it a "z" or an "s". That's why the z fix trades the buzz for the hiss.

</details>
