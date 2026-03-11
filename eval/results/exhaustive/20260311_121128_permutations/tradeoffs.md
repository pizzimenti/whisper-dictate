# Distil-Medium Exhaustive Permutation Summary

Date: 2026-03-11

Scope:
- Model: `models/distil-medium-en-ct2-int8`
- Dataset: `eval/audio/manifest.json` (`20` clips)
- Config axes:
  - `compute_type`: `int8`, `float32`
  - `beam_size`: `1`, `5`
  - `cpu_threads`: `6`, `12`
  - `without_timestamps`: `True`, `False`
  - `vad_filter`: `False`, `True`
  - `condition_on_previous_text`: `False`, `True`
- Total configs: `64`

## Winner

Best measured tradeoff and the only Pareto-frontier point:

- `dm_int8_b1_nots_novad_prev_t6`
- `avg_wer`: `0.02491`
- `overall_rtf`: `0.30891`
- `short_clip_mean_decode_seconds`: `2.355`
- `model_load_seconds`: `0.226`
- `first_short_result_seconds`: `2.581`

Close runner-up:

- `dm_int8_b1_nots_novad_noprev_t6`
- Same `avg_wer`: `0.02491`
- Slightly faster overall RTF: `0.29555`
- Slightly slower short clips: `2.401`
- Slightly slower first result: `2.645`

## Practical Conclusions

- `int8` remains the right compute type on this machine.
  - Best `float32` WER only matched the best `int8` WER.
  - In matched-pair comparisons, `float32` was slower on all `32/32` pairs for short-clip decode and first-result latency.
- `6` CPU threads beat `12` for the best configs.
  - Accuracy never improved with `12` threads.
  - The top `12`-thread config was slower than the top `6`-thread config on short clips (`2.651s` vs `2.355s`).
- `without_timestamps=True` is the better default.
  - On matched pairs, enabling timestamps never improved WER and usually made it worse.
  - The best timestamped int8 beam-1 runs landed at `0.0292` WER instead of `0.02491`.
- `vad_filter=True` did not help on this clean corpus.
  - It slightly hurt or left WER unchanged in matched pairs.
  - It did not produce a meaningful speed win.
- `beam_size=5` is not justified for interactive dictation here.
  - It never beat the winning beam-1 config on either WER or short-clip latency.
  - Its best WER plateau was `0.02607`, below the beam-1 best of `0.02491`.
- `condition_on_previous_text` is not a meaningful benchmark lever on this corpus.
  - WER was unchanged in all matched pairs.
  - Small timing differences favored `True`, but only slightly.
  - For real dictation behavior, keep using the product choice that avoids cross-utterance contamination unless live testing says otherwise.

## Recommendation

For local CPU dictation on this machine, stick with:

- `compute_type=int8`
- `beam_size=1`
- `cpu_threads=6`
- `without_timestamps=True`
- `vad_filter=False`

`condition_on_previous_text` is the only setting still worth treating as a behavioral choice rather than a benchmark choice.

On this dataset, `True` was the single fastest top-WER point.
For dictation UX, `False` may still be preferable if prior-text carryover causes hallucination or formatting problems in live use.
