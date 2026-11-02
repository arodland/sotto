---
title: "2. Pitch and voicing"
---

# 2. Pitch and voicing

Previous: [Sound and bits](01-sound-and-bits.md) · [Course home](README.md) · Next: [The spectral envelope](03-envelope.md)

## Big idea

Your vocal cords open and close many times a second, like a very fast buzzer. How fast they buzz is your **pitch**. When they aren't buzzing (as in "s", "f" or a whisper), the sound is **unvoiced** hiss. sotto measures both every 10 milliseconds.

Hissing consonants like "f", "s", "sh" and "th" have a name: **fricatives** (say *FRIK-uh-tivs*). You make one by pushing air through a narrow gap, between your top teeth and lower lip for "f", or between your tongue and the ridge behind your top teeth for "s". The air rubs past and turns noisy, which is where the name comes from: *friction*.

## A quick review: sine waves and frequency

A **sine wave** is the smoothest possible wiggle. It has:

- a **frequency**: how many times it repeats per second, measured in hertz (Hz),
- an **amplitude**: how big it is,
- a **phase**: where in its cycle it starts.

A 100 Hz sine wave repeats every 1/100 of a second = 10 ms. In general:

> period (seconds) = 1 / frequency (Hz)

## Pitch is the repeat rate

Look back at the zoomed vowel in chapter 1. The pattern repeated every 10.5 ms. So the man's pitch at that moment was:

> 1 / 0.0105 s ≈ **95 Hz**

Typical speaking pitches are roughly 85–155 Hz for adult men and 165–255 Hz for adult women. Your pitch also moves all the time: it rises at the end of a question and falls at the end of a sentence.

## A buzz is made of harmonics

Here is the surprising part, and it is the most important idea in sotto. **Any sound that repeats can be built by adding up sine waves whose frequencies are whole-number multiples of the repeat rate.** These sine waves are called **harmonics**. (This is a famous result in math called *Fourier analysis*.)

So a 95 Hz buzz is really 95 Hz + 190 Hz + 285 Hz + 380 Hz + ... all playing at once, each with its own loudness.

![The spectrum of one vowel, showing harmonics](figures/harmonics.svg)

This graph is a **spectrum**: instead of time along the bottom, it has *frequency*. Each peak is one harmonic. They sit exactly 95 Hz apart, all the way up to 4,000 Hz: 41 harmonics in all.

In the code, the number of harmonics is called **L**, and their loudnesses are called **A** (for amplitude). So the vowel in this picture is "L = 41 harmonics with amplitudes A₁, A₂, ... A₄₁".

## Pitch tracking

A **pitch tracker** (or pitch estimator) is the part of the encoder that measures the pitch. sotto starts from Codec 2's tracker, called **NLP** ("non-linear pitch"). Roughly, it:

1. squares the signal, which makes the buzz rate stand out even when the lowest harmonic is weak (as it is on a phone line),
2. looks for the strongest repeating rate between 50 and 400 Hz,
3. checks nearby candidates and fine-tunes the answer.

![Spectrograms and pitch tracks for a man and a woman](figures/pitch.svg)

The top two pictures are **spectrograms**: time goes across, frequency goes up, and darker means louder. You can see the harmonics as thin parallel stripes. They're closer together for the man (low pitch) and further apart for the woman (higher pitch). When the stripes bend, the pitch is changing.

The third graph shows the pitch that sotto measured. For the man it is smooth, around 95 Hz.

### Octave errors

The bottom graph shows the woman's pitch two ways. Codec 2's tracker (gray) **jumps**: sometimes up near 350 Hz for a moment, sometimes down near 60 Hz. Her real pitch doesn't move like that. These are **octave errors**, where the tracker locks onto double or half the true pitch. Doubling and halving fit the harmonics almost as well as the truth: if the true pitch is 180 Hz, a "pitch" of 90 Hz also has a harmonic at 180, 360, 540 Hz and so on.

How common is it? Compared with a careful offline pitch tracker (pYIN), Codec 2's pitch was off by more than 20% on about 32% of the woman's voiced frames, against 14% and 6% for the two men. A wrong pitch puts the harmonics in the wrong places, and it was the main reason the first sotto versions sounded worse on her voice.

### sotto's tracker

sotto's tracker (orange) starts from Codec 2's answer and cleans it up. It decides **pitch and voicing together**, for the whole recording at once. For every 10 ms frame it lists the possibilities:

- **unvoiced**, or
- **voiced** at one of several candidate pitches: Codec 2's estimate for this frame or the frames just before and after, each also doubled and halved, and each fine-tuned.

Then it picks one possibility per frame, choosing the path through the whole recording with the lowest total "cost". The costs are:

1. **Bad fit**: how badly the harmonics at that pitch match the spectrum.
2. **A slight preference for the higher pitch**, because half the true pitch fits almost as well as the truth (the 90 Hz example above).
3. **Disagreeing with Codec 2** about whether the frame is voiced.
4. **Switching** between voiced and unvoiced.
5. **Jumping** in pitch between neighboring frames, with a much bigger penalty for jumps of more than a third of an octave, because real pitch glides smoothly.
6. **Missing odd harmonics** (added in v3.3): if a candidate's 3rd, 5th, 7th... harmonics are far weaker than its even ones, it's probably half the true pitch, so that candidate gets a penalty. This fixed a word where a breathy voice at 258 Hz was being tracked at 129 Hz and sounded "grumbly".

Finding the best path through millions of possible paths, without trying them all, is done with the **Viterbi algorithm**, the same method the radio receiver uses to fix bit errors (chapter 6). Two extra rules: doubled and halved candidates must stay above 70 Hz, so it doesn't halve a low man's voice; and the highest pitch allowed is **500 Hz**, raised from Codec 2's 400 Hz after listeners heard a "glorp" where the woman's voice went above 420 Hz.

It isn't perfect. Around 3.0–3.5 seconds both trackers put her near 330 Hz, and a simple check there can't tell whether that's right or an octave too high. But it helps a lot. Against Codec 2, the woman's score went up by 0.6 to 0.75 (PESQ, chapter 7), and she's now within 0.1 to 0.3 of the men, where she used to be 0.7 behind. So most of her gap was pitch and voicing errors.

### Looking ahead

The tracker as written looks at the whole recording before deciding anything. A radio can't wait for you to finish talking. So the team tested a **fixed look-ahead**: decide each frame after seeing only a short way into the future. With 80 ms of look-ahead, the score dropped by only 0.003 compared with seeing everything. That adds 80 ms of delay, which is fine for push-to-talk radio, so the real-time version will use 80 ms.

## Voicing

Not every sound is a buzz. To decide, Codec 2's **voicing detector** asks: *if I pretend this frame is a perfect set of harmonics at the measured pitch, how well does that match the real spectrum below 1,000 Hz?*

- A good match means a buzz: **voiced**.
- A bad match means noise: **unvoiced**.

It then double-checks with a simple rule: buzzes have most of their energy at low frequencies, and hiss has most of it at high frequencies. sotto uses this detector as a starting point, and its tracker makes the final call.

### Mixed voicing: buzz and hiss at once

Real speech isn't always one or the other. A "z" or a "v" is a buzz *and* a hiss at the same time: your vocal cords buzz while air hisses past your teeth. These are called **voiced fricatives**. Listeners heard the problem: the "v" at the end of "five" came out as a vowel, "five-anuh". The low harmonics of that "v" are clean, but above 1,000 Hz it's mostly hiss.

So sotto now has three kinds of frame:

- **U** (unvoiced): all noise.
- **V** (voiced): all harmonics.
- **M** (mixed): harmonics below 1,000 Hz, noise above. A frame becomes mixed when it's voiced but its harmonics fit poorly even below 1,000 Hz.

There's no room for more bits, so sotto reuses the 2 voicing bits. Before, they were one yes/no for each 10 ms half of the frame. Now they're **one code for the whole 20 ms frame**: U U, V V, M M, or an *edge* (one half unvoiced, the other mixed). Whether an edge is a start (U then M) or an end (M then U) is worked out from the frame before. The rare frames that don't fit these four codes are sent as the closest one.

One more rule handles the quiet ends of sounds: a voiced frame with a weak fit that is more than 20 dB quieter than the last 300 ms is sent as unvoiced (the *tail* rule, from v3.1). Without it, a fading "v" could be tracked down to a junk low pitch and buzz for 200 ms.

Chapter 8 adds a twist to all of this. In v3.8, a "z" whose hiss is nearly as loud as its buzz is sent as U U after all, because the hiss tells the listener far more about which consonant it is than the buzz does.

## How pitch is written in the code

The code doesn't store pitch in hertz. It uses **Wo** ("omega-zero"), the pitch written as an angle per sample:

> Wo = 2π × pitch ÷ 8000

So 95 Hz is Wo ≈ 0.075 radians per sample. The number of harmonics is L = ⌊π ÷ Wo⌋, which is the same as ⌊4000 ÷ pitch⌋. Chapter 4 shows how Wo is squeezed into 7 bits.

## Try it

1. A woman's pitch is 200 Hz. What is the period in milliseconds? How many harmonics fit below 4,000 Hz?
2. A tracker makes an octave error and reports 400 Hz instead of 200 Hz. Which of the true harmonics would the decoder be missing?
3. Whisper a sentence, then say it out loud. Which parts of the voice did you switch off when whispering?
4. Say "zzzz", then "ssss", then "aaaa". Which would sotto call U, V and M?

## Where this is in the code

- `codec/codec2/src/nlp.c`: the NLP pitch tracker.
- `codec/codec2/src/sine.c`: `est_voicing_mbe()`, the voicing detector.
- `sim/sinusoid.py`: `track_pitch_voicing()` (the joint tracker, with its `lag` look-ahead option), `odd_even_db()` (the missing-odd-harmonics check), `unvoice_tails()` (the tail rule), and a Python copy of Codec 2's analysis.
- `sim/vocoder_lab.py`: `voicing_classes()` (finds mixed frames) and `SottoV32` (packs U/V/M into 2 bits).
- `sim/vocoder_lab.py`: `analyse()` runs Codec 2's analysis (`c2sim --modelout`) and returns one record per 10 ms with `Wo`, `L`, `A`, `phi` and `voiced`. `analyse(x, "tracked3")` uses sotto's tracker instead.

<details markdown="1"><summary>Answers</summary>

1. 1/200 s = 5 ms. 4,000 ÷ 200 = 20 harmonics, the last one right at 4,000 Hz.
2. Every odd one: 200, 600, 1,000, 1,400 Hz and so on. Only the even ones (400, 800, ...) would be rebuilt, and the voice would sound an octave too high.
3. Whispering switches off voicing: every frame becomes unvoiced. You can hear this in chapter 5's "whisper" demo.
4. "ssss" is U (all hiss), "aaaa" is V (all buzz), and "zzzz" is M (a buzz underneath, hiss on top).

</details>
