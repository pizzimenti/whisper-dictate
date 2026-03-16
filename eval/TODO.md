# whisper-dictate evaluation TODO

## Baseline results (beam=5, no VAD)
- Avg WER: 7.2%
- Speed: 1.8x real-time (RTF 0.541)
- Notable: short clips (<4s) have RTF >1.0 (slower than real-time) due to fixed overhead

## Remaining benchmark runs
- [ ] beam=1, no VAD — measure speed gain vs accuracy loss
- [ ] beam=5, VAD on — should fix hallucination on silence
- [ ] beam=1, VAD on — best speed config
- [ ] beam=5, VAD on, condition_on_previous=True — check if coherence improves

## Accuracy fixes to apply to dictate.py
- [x] Add `vad_filter=True` — critical, strips silence, prevents hallucination garbage
- [x] Add `condition_on_previous_text=False` — prevents cascading hallucinations
- [x] Add `no_speech_threshold=0.6` — reject low-confidence segments
- [ ] Pick beam_size based on benchmark results (1 vs 5 tradeoff)

## Speed fixes to investigate
- [ ] Reduce beam_size (1 is ~2-3x faster, slight accuracy cost)
- [ ] ~4s minimum decode time even for short clips — investigate overhead
- [ ] Test with fewer cpu_threads (6 vs 12) to check if contention hurts

## UX improvements
- [ ] Persistent notification while recording (replace-mode with fixed ID)
- [ ] Fix stale venv shebangs from whisper-cli rename (recreate venv or sed fix)
- [ ] Commit all pending changes once tuning is done

## Open design questions
- How should dictation sessions work? (continuous vs toggle vs push-to-talk)
- How to clearly indicate recording state to the user?

## 2026-03-16 accuracy bakeoff handoff

### Latest verbose benchmark result
- Source: `eval/results/verbose_benchmarks/20260316_132242_watch_live/summary.json`
- `whisper-large-v3` (`models/whisper-large-v3-ct2`, 6 threads): avg WER `1.301%`, overall RTF `0.716`, mean decode `5.888s`, model load `6.218s`
- `whisper-large-v3-turbo` (`models/whisper-large-v3-turbo-ct2`, 12 threads): avg WER `1.614%`, overall RTF `0.545`, mean decode `4.485s`, model load `2.189s`
- `distil-large-v3.5` (`models/distil-large-v3.5-ct2`, 6 threads): avg WER `2.747%`, overall RTF `0.667`, mean decode `5.480s`, model load `0.946s`
- Current takeaway:
  - best accuracy: `whisper-large-v3`
  - best speed/accuracy compromise in the large-model group: `whisper-large-v3-turbo`
  - `distil-large-v3.5` does not currently justify adoption on this CPU-only machine

### Commands
- Convert `distil-large-v3.5`:
  - `.venv/bin/python prepare_model.py --model-id distil-whisper/distil-large-v3.5 --output-dir models/distil-large-v3.5-ct2`
- Run quiet bakeoff:
  - `.venv/bin/python eval/sweep.py --preset accuracy-bakeoff --samples 20 --tag accuracy_bakeoff`
- Run very verbose live bakeoff:
  - `.venv/bin/python eval/verbose_benchmark.py --preset accuracy-bakeoff --samples 20 --tag watch_live`

### Code added for this bakeoff
- `eval/sweep.py`
  - added preset support
  - added `accuracy-bakeoff` preset
  - supports `--list-presets`
- `eval/verbose_benchmark.py`
  - new real-time verbose benchmark runner
- `README.md`
  - documents model conversion commands and bakeoff commands

### Claude handoff prompt
```text
You are taking over work in the repo `/home/bradley/Code/whisper-dictate`.

Context:
- Goal: improve dictation accuracy and evaluate whether `distil-large-v3.5` is worth using versus other Whisper large-model options.
- The repo currently uses local CPU-only inference with `faster-whisper` / CTranslate2.
- Existing default runtime is centered on `distil-medium.en`.
- We wanted a direct bakeoff between:
  - `openai/whisper-large-v3`
  - `openai/whisper-large-v3-turbo`
  - `distil-whisper/distil-large-v3.5`

Important repo state:
- I already modified the repo to support this comparison.
- New/changed files:
  - `/home/bradley/Code/whisper-dictate/eval/sweep.py`
  - `/home/bradley/Code/whisper-dictate/eval/verbose_benchmark.py`
  - `/home/bradley/Code/whisper-dictate/README.md`
- `eval/sweep.py` now supports named presets, including `accuracy-bakeoff`.
- `eval/verbose_benchmark.py` is a new very verbose real-time benchmark runner that prints:
  - model load timing
  - per-sample progress
  - each emitted segment as decoding progresses
  - per-sample WER / RTF
  - running averages
  - final leaderboard
- README was updated with conversion commands and benchmark commands.

Known model directories:
- `/home/bradley/Code/whisper-dictate/models/whisper-large-v3-ct2`
- `/home/bradley/Code/whisper-dictate/models/whisper-large-v3-turbo-ct2`
- `/home/bradley/Code/whisper-dictate/models/distil-large-v3.5-ct2`
- Existing default:
  - `/home/bradley/Code/whisper-dictate/models/distil-medium-en-ct2-int8`

Commands that were intended for use:
- Convert v3.5:
  - `.venv/bin/python prepare_model.py --model-id distil-whisper/distil-large-v3.5 --output-dir models/distil-large-v3.5-ct2`
- Quiet bakeoff:
  - `.venv/bin/python eval/sweep.py --preset accuracy-bakeoff --samples 20 --tag accuracy_bakeoff`
- Verbose bakeoff:
  - `.venv/bin/python eval/verbose_benchmark.py --preset accuracy-bakeoff --samples 20 --tag watch_live`

Latest benchmark result:
- Source:
  - `/home/bradley/Code/whisper-dictate/eval/results/verbose_benchmarks/20260316_132242_watch_live/summary.json`
- Leaderboard:
  - `whisper_large_v3_t6`: avg_wer `0.01301`, overall_rtf `0.71613`, mean_decode_seconds `5.888`, model_load_seconds `6.218`
  - `whisper_large_v3_turbo_t12`: avg_wer `0.01614`, overall_rtf `0.54548`, mean_decode_seconds `4.485`, model_load_seconds `2.189`
  - `distil_large_v3_5_t6`: avg_wer `0.02747`, overall_rtf `0.66652`, mean_decode_seconds `5.48`, model_load_seconds `0.946`

What I need from you:
1. Inspect the current git diff and understand the new benchmark infrastructure.
2. Locate the most recent benchmark results produced by the user, especially under:
   - `/home/bradley/Code/whisper-dictate/eval/results/`
   - `/home/bradley/Code/whisper-dictate/eval/results/verbose_benchmarks/`
   - `/home/bradley/Code/whisper-dictate/eval/results/sweeps/`
3. Summarize the actual bakeoff results for:
   - `whisper-large-v3`
   - `whisper-large-v3-turbo`
   - `distil-large-v3.5`
4. Compare them on:
   - average WER
   - overall RTF
   - mean or short-form latency
   - practical dictation suitability on this CPU-only machine
5. Decide which model should become:
   - default balanced model
   - accuracy-first option
   - whether `distil-large-v3.5` should be kept or rejected
6. If the results clearly favor a new default or optional preset, implement the next logical repo change instead of stopping at analysis.
   Possible next changes:
   - promote a better model in runtime defaults
   - add a user-facing model selector / preset
   - update README recommendations
   - keep `distil-medium.en` as default but expose an accuracy mode
7. Verify whatever you change with the smallest reliable check.

Important constraints:
- Do not revert unrelated changes.
- Prefer evidence from the repo and produced benchmark output over generic claims.
- Treat this as a coding + evaluation handoff, not a greenfield redesign.
- If benchmark results are missing or incomplete, say exactly what exists and what is missing, then recommend the smallest next step.

Please start by:
- checking `git status`
- reading the modified files above
- finding the latest benchmark output
- then giving a concrete recommendation before making any further code changes.
```
