# whisper-dictate

Local Whisper transcription for Wayland — three pieces:

1. **Live CLI** (`mic_realtime.py`): streams mic audio to the terminal in real time.
2. **System dictation daemon** (`dictate.py`): persistent mic capture/transcribe worker for dictation.
3. **Global hotkey listener** (`kglobal_hotkey.py`): uses KWin's Wayland accessibility keyboard monitor to toggle dictation and always attempts to type the transcript into the current keyboard focus.

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

Recommended path on KDE/Wayland:

```bash
systemctl --user enable --now whisper-dictate
systemctl --user enable --now whisper-dictate-hotkey
```

The hotkey listener grabs `Ctrl+Space` directly through KWin's accessibility keyboard monitor. On each press it:

- starts dictation immediately
- stops dictation on the next press
- attempts to type the transcript into the current keyboard focus

There is no AT-SPI cursor/editability gate anymore. If the current target cannot accept typed text, the transcript is still saved in the daemon runtime files and `ydotool` is simply attempted against whatever currently has keyboard focus.

KWin currently restricts that keyboard-monitor interface to the screen-reader bus name `org.gnome.Orca.KeyboardMonitor`, so `whisper-dictate-hotkey.service` owns that name while it runs. If you use Orca, stop the hotkey service first or the listener will fail to start.

If the hotkey listener is not running, or you want to test without the global shortcut backend, use the terminal control path instead:

```bash
python dictatectl.py start
# speak
python dictatectl.py stop
```

`stop` waits for transcription to finish and prints the latest transcript from the daemon runtime files, so you can test dictation without depending on a global shortcut backend.

## Architecture

- `dictate.py`: long-lived daemon that keeps the Whisper model warm, owns microphone capture/transcription, and writes shared runtime files.
- `dictatectl.py`: stdlib control plane for `start`, `stop`, `toggle`, `status`, and `last-text`.
- `kglobal_hotkey.py`: system-Python KWin keyboard-monitor listener for the working `Ctrl+Space` toggle.
- `dictate_runtime.py`: shared runtime-path, daemon-state, and signaling helpers used by the daemon and control helpers.
- `desktop_actions.py`: shared `notify-send` and `ydotool` wrappers for desktop side effects.

### Runtime files

The daemon and helpers coordinate through two files under `XDG_RUNTIME_DIR`:

- `whisper-dictate-<uid>.state`: current daemon state (`idle`, `recording`, or `transcribing`)
- `whisper-dictate-<uid>.last.txt`: latest completed transcript

`dictate.py` owns writes to those files. `dictatectl.py` and `kglobal_hotkey.py` read them so control/status behavior stays consistent even though the hotkey listener runs under system Python.

### Helper scripts

`ptt-press.sh`, `ptt-release.sh`, and `toggle.sh` are now thin wrappers around `dictatectl.py`, so there is only one control-plane implementation to maintain.

## Tuning

- `--model-dir`: default is the English-only `distil-medium-en-ct2-int8`.
- `--cpu-threads N`: override thread count. Dictation-oriented defaults now use physical cores / short-form-friendly thread counts.
- `--compute-type int8|float16|float32`: precision/runtime tradeoff.
- `--language`: defaults to `en`.
- `--beam-size`: daemon and live CLI default to 1.
- `--state-file`: daemon runtime state file shared by `dictate.py`, `dictatectl.py`, and the helper scripts.
- `--last-text-file`: latest transcript file shared by `dictate.py`, `dictatectl.py`, and the hotkey listener.
- `--type-output/--no-type-output`: let the daemon type directly or leave typing to an external helper.
- `--vad-filter/--no-vad-filter`: daemon defaults to `vad_filter=False` for lower-latency short-form dictation.
- `--condition-on-previous-text/--no-condition-on-previous-text`: daemon defaults to `False` to reduce cascading hallucinations.
- `--no-speech-threshold`: Whisper-side non-speech rejection. The daemon defaults to `0.6`.
- `--energy-threshold`, `--start-speech-ms`, `--silence-ms`, `--max-utterance-s`: live CLI utterance-boundary controls.
- `--task transcribe|translate`: keep original language vs force English output (CLI mode only).
- `--decode-workers`, `--diag`, `--diag-interval-s`: parallelism and diagnostics (CLI mode only).

## Files

- `install.sh`: install dependencies and register the systemd service (Arch/Manjaro).
- `prepare_model.py`: download and convert the model.
- `mic_realtime.py`: live terminal transcription.
- `dictate.py`: system-wide dictation daemon.
- `dictate_runtime.py`: shared runtime-path, state-file, and daemon-signaling helpers.
- `desktop_actions.py`: shared desktop notification and typing helpers.
- `dictatectl.py`: terminal control helper for `start`, `stop`, `toggle`, `status`, and `last-text`.
- `kglobal_hotkey.py`: KWin accessibility hotkey listener that always attempts typing into the current keyboard focus.
- `ptt-press.sh`: push-to-talk press wrapper around `dictatectl.py start --no-wait`.
- `ptt-release.sh`: push-to-talk release wrapper around `dictatectl.py stop --no-wait`.
- `toggle.sh`: fallback toggle wrapper around `dictatectl.py toggle --no-wait`.
- `whisper-dictate-hotkey.service`: user service for the global hotkey listener.
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

List available sweep presets with:

```bash
.venv/bin/python eval/sweep.py --list-presets
```

Run the direct large-model accuracy bakeoff with:

```bash
.venv/bin/python eval/sweep.py --preset accuracy-bakeoff --samples 20 --tag accuracy_bakeoff
```

That preset compares:

- `whisper-large-v3`
- `whisper-large-v3-turbo`
- `distil-large-v3.5`

using the repo's current short-form dictation-oriented decode defaults.

If those models are not converted locally yet, prepare them with:

```bash
python prepare_model.py --model-id openai/whisper-large-v3 --output-dir models/whisper-large-v3-ct2
python prepare_model.py --model-id openai/whisper-large-v3-turbo --output-dir models/whisper-large-v3-turbo-ct2
python prepare_model.py --model-id distil-whisper/distil-large-v3.5 --output-dir models/distil-large-v3.5-ct2
```

If you specifically just need `distil-large-v3.5`, the command is:

```bash
python prepare_model.py --model-id distil-whisper/distil-large-v3.5 --output-dir models/distil-large-v3.5-ct2
```

For a very verbose real-time comparison that prints every emitted segment, per-sample WER/RTF, and a final leaderboard as it runs:

```bash
.venv/bin/python eval/verbose_benchmark.py --preset accuracy-bakeoff --samples 20 --tag watch_live
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
