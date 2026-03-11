# whisper-dictate

Local Whisper transcription for Wayland — two modes:

1. **Live CLI** (`mic_realtime.py`): streams mic audio to the terminal in real time.
2. **System dictation daemon** (`dictate.py`): push-to-talk dictation with global hotkeys; types the result into any focused window via `ydotool`.

This project is standardized on `distil-whisper/distil-medium.en` converted to CTranslate2 int8 for local English dictation on CPU.

## Quick start

```bash
cd whisper-dictate
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

Convert the model once:

```bash
python prepare_model.py
```

The bundled/default model is English-only. Historical evaluation artifacts for other models are retained under `eval/results/`, but the active project runtime is `distil-medium-en`.

If `torch` is unavailable for your Python version, use a Python 3.12 venv for conversion only.

### Live CLI

```bash
python mic_realtime.py
```

Press `Enter` to stop.

### System dictation daemon

On Arch/Manjaro, `install.sh` handles everything automatically:

```bash
bash install.sh
```

It installs `ydotool`, enables the system service, adds you to the `input` group, and registers the `whisper-dictate` systemd user service. Log out and back in after running it for the group change to take effect.

To start the daemon manually instead:

```bash
source .venv/bin/activate
python dictate.py
```

**Hotkey (manual step):** Bind `ptt-press.sh` on key press and `ptt-release.sh` on key release in System Settings → Shortcuts → Custom Shortcuts. `Meta+Space` works well. If your shortcut tool cannot emit release events, `toggle.sh` remains available as a fallback.

Hold the hotkey while speaking and release it to transcribe. The transcribed text is typed at the cursor.

## Tuning

- `--model-dir`: default is the English-only `distil-medium-en-ct2-int8`.
- `--cpu-threads N`: override thread count. Dictation-oriented defaults now use physical cores / short-form-friendly thread counts.
- `--compute-type int8|float16|float32`: precision/runtime tradeoff.
- `--language`: defaults to `en`.
- `--beam-size`: daemon and live CLI default to 1.
- `--state-file`: daemon runtime state file used by `ptt-press.sh`, `ptt-release.sh`, and `toggle.sh`.
- `--vad-filter/--no-vad-filter`: daemon defaults to `vad_filter=False` for lower-latency short-form dictation.
- `--condition-on-previous-text/--no-condition-on-previous-text`: daemon defaults to `False` to reduce cascading hallucinations.
- `--no-speech-threshold`: daemon defaults to `0.6` to reject low-confidence speech.
- `--energy-threshold`, `--start-speech-ms`, `--silence-ms`, `--max-utterance-s`: live CLI utterance-boundary controls.
- `--no-speech-threshold`: Whisper-side non-speech rejection for live CLI and daemon decode.
- `--task transcribe|translate`: keep original language vs force English output (CLI mode only).
- `--decode-workers`, `--diag`, `--diag-interval-s`: parallelism and diagnostics (CLI mode only).

## Files

- `install.sh`: install dependencies and register the systemd service (Arch/Manjaro).
- `prepare_model.py`: download and convert the model.
- `mic_realtime.py`: live terminal transcription.
- `dictate.py`: system-wide dictation daemon.
- `ptt-press.sh`: start a push-to-talk recording.
- `ptt-release.sh`: stop recording and transcribe.
- `toggle.sh`: fallback toggle helper for shortcut tools that only support key press.
- `whisper-dictate.service`: systemd user unit.
- `transcribe.py`: transcribe an audio file.
- `benchmark.py`: latency and RTF benchmarking.
- `eval/sweep.py`: run the current `distil-medium-en` tuning matrix and save per-config transcripts, timings, and WER results.
- `runtime_profile.py`: shared CPU/runtime helpers.

## Evaluation

Run the curated sweep with:

```bash
.venv/bin/python eval/sweep.py --samples 20 --tag myrun
```

Each sweep writes `summary.json`, `leaderboard.csv`, `leaderboard.md`, and one JSON per config under `eval/results/sweeps/<timestamp>_<tag>/`. Those per-config JSON files include the model/settings used plus the reference and hypothesis for every audio file.

Local March 11, 2026 results on the bundled 20-sample LibriSpeech set:

- Best speed/latency tradeoff: `distil-medium-en`, beam 1, `without_timestamps=True`, `cpu_threads=6` → avg normalized WER `2.49%`, overall RTF `0.361`, short clips (`<=4s`) averaged `2.91s`.
- Best dictation defaults from the later exhaustive `distil-medium` sweep: `compute_type=int8`, `beam_size=1`, `cpu_threads=6`, `without_timestamps=True`, `vad_filter=False`, `condition_on_previous_text=False` for live cursor dictation.
- Historical cross-model comparisons are retained in `eval/results/`, but they are not part of the active runtime anymore.

## Notes

- First conversion can take time and several GB of storage.
- CPU-only; no CUDA or ROCm required.
- Live mode does not create transcript files unless you redirect terminal output.
