---
title: "7. How we measure \"better\""
---

# 7. How we measure "better"

Previous: [Across the radio](06-radio.md) · [Course home](README.md) · Next: [The hiss band](08-hiss-band.md)

## Big idea

"Does it sound better?" is a hard question to answer fairly. sotto's team uses computer scores that predict what listeners would say, and a kind of experiment called an **oracle** test, where part of the system is allowed to cheat. Stacking oracle tests one on top of another makes a **quantization ladder** that shows exactly where quality is lost. And in the end, real people listen.

## Scoring speech with a computer

The best test is to play clips to many people and ask them to rate them. That's slow, so during development sotto uses two scores that compare a decoded clip with the original:

- **PESQ** ("perceptual evaluation of speech quality") predicts how people would rate the *quality*, on a scale from about 1 (bad) to 4.5 (perfect). It's an international standard, built by fitting a model of human hearing to thousands of real listening tests. A clip compared with itself scores about 4.5; most low-bitrate vocoders land between 2.5 and 3.5.
- **ESTOI** predicts *intelligibility*: what fraction of the words a listener would understand, from 0 to 1.

These are predictions, not people. They can be fooled: later in this chapter you'll see a case where PESQ preferred one design and listeners preferred the other. That's why sotto is also tested by ear.

## What's an oracle?

In computer science, an **oracle** is a helper that magically knows the right answer. In vocoder experiments, an oracle test means: *let one part use the true, perfect values instead of its normal estimate or its rounded value, and see how much better the result gets.*

For example, a **pitch oracle** test gives the decoder the true pitch instead of the 7-bit rounded pitch (or a carefully checked pitch instead of the tracker's guess). If quality jumps up, pitch is worth working on. If it barely moves, spend your effort elsewhere. Oracle tests are cheap: you don't have to invent a better pitch tracker to find out whether one would help.

In the code, oracle experiments work by running Codec 2's analysis, saving its model (pitch, voicing and harmonic amplitudes for every 10 ms) to a file, editing that file in Python, and then synthesizing from the edited file (`c2sim --modelout` and `--modelin`).

## The quantization ladder

Now stack the oracles. Start at the top rung with everything perfect: the true harmonic amplitudes, the true phases, no rounding. Then go down one rung at a time, each time taking away one oracle, so one more piece becomes realistic. At the bottom you reach the real codec. The drop between two rungs is exactly what that one piece costs.

The team built this ladder for Codec 2 2400 (the code calls it the **oracle ladder**; "quantization ladder" means the same thing):

![The quantization ladder for Codec 2, with sotto v3.5 at the end](figures/ladder.svg)

Reading it from left to right:

| Rung | What became realistic | What it cost |
|---|---|---|
| Harmonic model, true phases | Nothing yet: perfect harmonics | (starting point) |
| + made-up phases | Phases are invented from voicing (chapter 5) | 0.3 to 0.7 |
| + postfilter | Codec 2's background-noise trick | about zero, sometimes a gain |
| **+ LPC-10 envelope** | The envelope is squeezed into 10 LPC numbers | **about 0.55 for the men** |
| + quantized LSPs | Those 10 numbers are rounded to fit the bits | about 0.1 |
| + quantized pitch and energy, 20 ms | Pitch and energy rounded, every other frame sent | 0 to 0.2 |
| Codec 2 2400 | The actual codec, start to finish | |

The tallest step down for both men is the LPC-10 envelope. That is the single finding that shaped sotto v0: replace the envelope first (chapter 3). The diamonds at the right are sotto v3.5, the actual 48-bit codec today. (They were scored later, with the corrected alignment described below, so compare them loosely.) It's well above Codec 2 for all three voices.

Two other things jump out:

- **The woman starts low.** Even at the top rung, with perfect harmonics, she scores only 3.08, against 3.8 to 4.0 for the men. Rounding can't be the reason, because nothing is rounded yet. The problem is earlier, in the *analysis*: pitch errors (chapter 2), or too few harmonics to read her envelope from (chapter 3). This finding led to sotto's pitch tracker (v1, then v2), which closed most of her gap.
- **Made-up phases are the second biggest loss.** That pointed at better voicing models, which led to mixed voicing in v3.2.

## A ladder for sotto v1

Later the team built a ladder for sotto v1 itself. It starts from the harmonic model with sotto's pitch tracker, and adds one step of the real codec at a time:

![The quantization ladder for sotto v1](figures/ladder-v1.svg)

| Step | What became realistic | What it cost |
|---|---|---|
| Harmonic model, sotto tracker | Made-up phases, nothing rounded | (starting point) |
| + 20-point envelope | The envelope is read at 20 mel points (chapter 3) | 0.15 to 0.4 |
| + envelope sent every 20 ms | Every other envelope is filled in by interpolation | 0.1 to 0.2 |
| + pitch sent every 20 ms | The same for pitch | 0 to 0.1 |
| + 7-bit pitch, 34-bit shape | Everything rounded to fit 48 bits (chapter 4) | **0.2 to 0.4** |
| + formant postfilter (= v1) | The decoder sharpens the envelope (chapter 5) | a *gain* of 0.14 to 0.24 |

This time the biggest single loss was the rounding step, the shape quantizer in particular. That pointed to a smarter quantizer, but training one needed far more speech than the team had (about 2 minutes, nearly all from one man). Once they found freely usable speech collections, they trained the vector quantizer in v3 (chapter 4). That's how the ladders steer the work: measure where the loss is, fix that, measure again.

## Sweeps: trying every setting

Some choices are a single number, like the formant postfilter's strength or the pitch tracker's look-ahead. For those, the team runs a **sweep**: try a range of values on many voices and plot the score for each. Two examples from v3.5, each over 31 voices:

| Postfilter strength | 1.0 | 1.2 | **1.35** | 1.5 | 1.7 | 2.0 |
|---|---|---|---|---|---|---|
| PESQ | 2.81 | 3.02 | **3.08** | 3.06 | 2.94 | 2.69 |

| Tracker look-ahead | 10 ms | 20 ms | 40 ms | **80 ms** | whole recording |
|---|---|---|---|---|---|
| PESQ | 3.011 | 3.035 | 3.041 | **3.052** | 3.055 |

The first shows a clear best value. The second shows that 80 ms of look-ahead is almost as good as seeing the whole recording, so real-time tracking costs almost nothing.

## Encoder against encoder

A ladder tells you where to work. A **race** tells you whether the result is better. In the encoder race, every codec sends 48 bits every 20 ms through the identical radio chain, with the same simulated fading, and the scores are averaged over three different random fading patterns. The results are the table at the end of chapter 6.

The team also scores codecs on more voices without the radio: 15 voices (the three clips plus 12 people counting from zero to nine), and 16 more studio and audiobook voices that were held out of training. On those 16, sotto beat Codec 2 on every one.

## Listening tests

Computer scores only predict what people hear, so sotto is now also tested by ear, by members of a radio club. Each test is a **blind A/B comparison**: the listener hears two versions of the same clip, labeled only A and B, and says which is better or that they're the same. Since round 5, the original recording plays next to every pair, so listeners judge "closer to the real voice", not just "nicer".

Each round also includes a **null pair**: the same clip twice. A careful listener should call it a tie. If they pick a winner, their other answers are suspect too.

Listening found things PESQ missed:

- A "glorp" where the woman's voice went above 400 Hz, which led to raising the pitch limit to 500 Hz.
- A faint "floor" of noise between words, which led to true silence for energy code 0.
- "five-anuh" in the "v" of "five", which led to mixed voicing and a lighter postfilter.
- Dropouts that sounded *worse* with a long fade, even though PESQ scored the long fade higher. That led to v3.5's short concealment (chapter 6).
- A voice at 390 Hz recorded on a poor microphone, where listeners twice preferred Codec 2. That one is still open.
- Dull "s" and "sh" sounds, a warble under whispers and a "z" heard as "th", which led to v3.8's hiss band (chapter 8). There PESQ was no help at all: it can't hear above 4,000 Hz.

## Being honest about the results

Good science says what it *didn't* test, and fixes its own mistakes:

- **A scoring bug.** PESQ and ESTOI compare the decoded clip with the original, so the two must be lined up in time. The scripts used to line them up by matching waveforms, but vocoders with made-up phases don't follow the original waveform, so the match picked delays anywhere from 50 to 180 samples when the right answer is about 160. That made scores wrong by up to ±0.1 (and ESTOI too low). Now every codec is scored with the same fixed delay of 160 samples. Old write-ups got their rankings right but some numbers wrong, and the course uses the corrected numbers.
- **Few listeners.** Most listening rounds had one listener. That's enough to find problems, not to prove small differences.
- **Training speech.** The codebook is trained on CC BY speech only, so it can be shipped. Another collection (EARS) is used only for testing because its license doesn't allow shipping.
- **Not on the radio yet.** sotto now has a C version (`c/`) that gives the same bits as the Python on 99.97% of frames, and a fixed-point version (`c/fixed/`) for the radio's chip. On TI's simulator of that chip, the fixed-point decoder needs about 37 MHz of the chip's 200 MHz, but the encoder needs about 188 MHz on average and 212 MHz on its worst frame: too much, so it still has to get faster. None of it has run on a real radio.

## What's next for sotto

From `docs/encoder-v1.md`:

1. Making the fixed-point encoder fast enough for the radio's DSP chip, a TI C5517 (the target is about 100 MHz), and measuring v3.8's hiss band on it.
2. Deciding whether to spread each frame's bits across *two* radio frames (200 ms). It's built, and PESQ says it helps from 30 km/h up, but in the first listening round it won 3, lost 2 and tied 1, so more tests come first.
3. Retraining the codebook on more speech when it's available.
4. Working out, by ear, what listeners want for very high voices.

## Try it

1. A ladder rung drops PESQ from 3.5 to 2.9. Another drops it from 2.9 to 2.8. If you had a month to improve the codec, which would you work on? Why?
2. Design an oracle test to find out whether the 2 voicing bits are good enough. What would you replace, and what would you compare?
3. Why do you think the team averaged the race over three different fading patterns instead of one?
4. In a listening round, a listener says the null pair's B sounds clearly better. What should the team conclude?

## Where this is in the code

- `sim/oracle_ladder.py`: builds the ladder. Results in `docs/results/oracle_ladder_15s.json`.
- `sim/encoder_race.py`: the encoder race (`--codecs` picks which codecs race). Results in `docs/results/encoder_race.json`; more voices in `docs/results/clean_15voices.json` and `docs/results/codebook_eval.json`.
- `sim/audio_race.py`: `CODEC_DELAY`, the fixed 160-sample alignment.
- `tools/sc_counting_clips.py`: builds the counting clips from Speech Commands.
- `sim/vocoder_lab.py`: the oracle lab (edit the model in Python, synthesize with `c2sim`).
- `tools/score_samples.py` and `sim/audio_race.py`: PESQ and ESTOI scoring.

<details markdown="1"><summary>Answers</summary>

1. The first one (a 0.6 drop). Fixing it could win back up to 0.6 points. The second could win back at most 0.1.
2. Replace the 2 transmitted voicing bits with an oracle: careful voicing decisions for every 10 ms (for example, marked by hand or by a better detector), or even per-band voicing. Keep everything else the same and compare PESQ. If the score rises a lot, voicing is worth improving.
3. Fading is random. One pattern might happen to put a deep fade on an important word for one codec and on a pause for the other. Averaging several patterns makes the comparison fairer.
4. The two clips were identical, so the listener heard a difference that wasn't there. Their other answers in that round may be guesses too, and should count for less.

</details>
