# whisper-dictate

Local Whisper transcription for Wayland, redesigned around IBus as the only text-placement path:

1. **Live CLI** (`mic_realtime.py`): streams mic audio to the terminal in real time.
2. **Core dictation daemon** (`dictate.py`): persistent mic capture/transcribe worker that publishes transcript/state events on session D-Bus.
3. **IBus frontend**: the only component allowed to place text into applications; it consumes daemon transcript events and maps them to IBus preedit and commit.

This project uses `openai/whisper-large-v3-turbo` converted to CTranslate2 int8 for local English dictation on CPU.

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
python prepare_model.py --model-id openai/whisper-large-v3-turbo --output-dir models/whisper-large-v3-turbo-ct2
```

Historical evaluation artifacts for other models are retained under `eval/results/`.

If `torch` is unavailable for your Python version, use a Python 3.12 venv for conversion only.

### Live CLI

```bash
python mic_realtime.py
```

Press `Enter` to stop.

### System dictation daemon

On Arch/Manjaro, `install.sh` handles the bootstrap path automatically:

```bash
bash install.sh
```

It installs `ibus`, sets up the Python environment, registers the `io.github.pizzimenti.WhisperDictate.service` systemd user unit, installs the D-Bus activation file, places the IBus component metadata under the current user's data directory, writes `~/.config/environment.d/60-whisper-dictate-ibus.conf` so IBus can scan that per-user component directory, keeps `XMODIFIERS=@im=ibus` for XWayland/X11 compatibility, installs `~/.config/plasma-workspace/env/whisper-dictate-plasma-wayland.sh` to unset `GTK_IM_MODULE` and `QT_IM_MODULE` during Plasma Wayland startup, installs the IBus engine launcher at `~/.local/bin/ibus-engine-whisper-dictate`, installs a hidden KDE launcher for `dictatectl.py toggle --no-wait`, configures Plasma's KWin Wayland input method in `~/.config/kwinrc` to use the installed `IBus Wayland` desktop file, refreshes the IBus cache, and restarts `ibus-daemon` for the current session.

To start the daemon manually instead:

```bash
source .venv/bin/activate
python dictate.py
```

The daemon publishes the reverse-DNS session bus name `io.github.pizzimenti.WhisperDictate1`, and the IBus frontend is expected to be selected by that engine name once the frontend package is installed.

The core service is user-level and idempotent:

```bash
systemctl --user enable --now io.github.pizzimenti.WhisperDictate.service
```

If you want to confirm the D-Bus API manually, use the terminal control path once the daemon-side service is available:

```bash
python dictatectl.py start
# speak
python dictatectl.py stop
```

`stop` waits for transcription to finish and prints the latest transcript from the daemon runtime files.

### IBus selection flow

After the IBus frontend is installed, enable it the same way you would any other IBus engine:

1. Open your IBus configuration tool.
2. Add `Whisper Dictate` or the reverse-DNS engine name `io.github.pizzimenti.WhisperDictate1`.
3. Select it in the input method switcher when you want dictation text to flow into the focused application.

The daemon never inserts text directly. Partial transcript should appear as preedit and final transcript should be committed by the IBus frontend only.
If the engine still does not appear immediately after install, or if text fields do not accept dictation commits, sign out and back in once so the desktop session reloads the updated input-method environment and KWin picks up the configured IBus Wayland input method. On Plasma Wayland, `GTK_IM_MODULE` and `QT_IM_MODULE` should remain unset in the desktop session; the compositor-backed `IBus Wayland` path handles native Wayland clients.
On KDE Plasma, `io.github.pizzimenti.WhisperDictateToggle.desktop` can be bound as a global shortcut to run `dictatectl.py toggle --no-wait`, which is the most reliable way to keep `Ctrl+Space` working as a dictation toggle.

## Architecture

- `dictate.py`: long-lived daemon that keeps the Whisper model warm, owns microphone capture/transcription, and publishes transcript/state events.
- `dictatectl.py`: stdlib control plane for `start`, `stop`, `toggle`, `status`, and `last-text`.
- `whisper_dictate/`: shared package for constants, exceptions, logging, and D-Bus contract scaffolding.
- `dictate_runtime.py`: shared runtime-path, daemon-state, and signaling helpers used by the daemon and control helpers.
- `desktop_actions.py`: shared notification helpers for desktop side effects.

### Runtime files

The daemon and helpers coordinate through two files under `XDG_RUNTIME_DIR`:

- `whisper-dictate-<uid>.state`: current daemon state (`idle`, `recording`, or `transcribing`)
- `whisper-dictate-<uid>.last.txt`: latest completed transcript

`dictate.py` owns writes to those files. `dictatectl.py` reads them so control/status behavior stays consistent across shells and user services.

### Helper scripts

`ptt-press.sh`, `ptt-release.sh`, and `toggle.sh` are now thin wrappers around `dictatectl.py`, so there is only one control-plane implementation to maintain.

## Tuning

- `--model-dir`: default is `whisper-large-v3-turbo-ct2`. For maximum accuracy use `whisper-large-v3-ct2` (1.3% vs 1.6% WER, ~1.4s slower startup).
- `--cpu-threads N`: override thread count. Dictation-oriented defaults now use physical cores / short-form-friendly thread counts.
- `--compute-type int8|float16|float32`: precision/runtime tradeoff.
- `--language`: defaults to `en`.
- `--beam-size`: daemon and live CLI default to 1.
- `--state-file`: daemon runtime state file shared by `dictate.py`, `dictatectl.py`, and the helper scripts.
- `--last-text-file`: latest transcript file shared by `dictate.py` and `dictatectl.py`.
- `--vad-filter/--no-vad-filter`: daemon defaults to `vad_filter=False` for lower-latency short-form dictation.
- `--condition-on-previous-text/--no-condition-on-previous-text`: daemon defaults to `False` to reduce cascading hallucinations.
- `--no-speech-threshold`: Whisper-side non-speech rejection. The daemon defaults to `0.6`.
- `--energy-threshold`, `--start-speech-ms`, `--silence-ms`, `--max-utterance-s`: live CLI utterance-boundary controls.
- `--task transcribe|translate`: keep original language vs force English output (CLI mode only).
- `--decode-workers`, `--diag`, `--diag-interval-s`: parallelism and diagnostics (CLI mode only).
- Runtime control/VAD polling now uses 150ms wait intervals to reduce idle wakeups without materially affecting dictation latency.

## Files

- `install.sh`: install dependencies, register the user service, and install the D-Bus and IBus metadata (Arch/Manjaro).
- `prepare_model.py`: download and convert the model.
- `mic_realtime.py`: live terminal transcription.
- `dictate.py`: system-wide dictation daemon.
- `dictate_runtime.py`: shared runtime-path, state-file, and daemon-signaling helpers.
- `desktop_actions.py`: shared desktop notification helpers.
- `dictatectl.py`: terminal control helper for `start`, `stop`, `toggle`, `status`, and `last-text`.
- `systemd/io.github.pizzimenti.WhisperDictate.service`: systemd user unit for the core daemon.
- `packaging/io.github.pizzimenti.WhisperDictate.service`: D-Bus activation file for the daemon.
- `packaging/io.github.pizzimenti.WhisperDictate.component.xml`: IBus component metadata for the engine frontend.
- `packaging/ibus-engine-whisper-dictate`: launcher template installed for IBus to execute the frontend.
- `ibus_engine.py`: top-level compatibility entrypoint for the IBus engine process.
- `scripts/check-ibus-only.sh`: smoke check for forbidden injector and clipboard backends.
- `ptt-press.sh`: push-to-talk press wrapper around `dictatectl.py start --no-wait`.
- `ptt-release.sh`: push-to-talk release wrapper around `dictatectl.py stop --no-wait`.
- `toggle.sh`: fallback toggle wrapper around `dictatectl.py toggle --no-wait`.
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

March 2026 bakeoff results on the bundled 20-sample LibriSpeech set (beam=1, int8, no VAD, condition_on_previous=False):

| Model | Threads | Avg WER | RTF | Mean decode | Model load |
|---|---|---|---|---|---|
| whisper-large-v3 | 6 | **1.301%** | 0.716 | 5.888s | 6.218s |
| whisper-large-v3-turbo | 12 | 1.614% | **0.545** | **4.485s** | 2.189s |
| distil-large-v3.5 | 6 | 2.747% | 0.667 | 5.480s | 0.946s |

- `whisper-large-v3-turbo` is the default: best overall speed on this 12-core machine, WER within 0.3pp of large-v3.
- `whisper-large-v3` is the accuracy-first option: 1.3% WER, use `--model-dir models/whisper-large-v3-ct2`.
- `distil-large-v3.5` was rejected: 2.1x worse WER than large-v3, no speed advantage over turbo, and consistent proper-noun truncation errors.

## Notes

- First conversion can take time and several GB of storage.
- CPU-only; no CUDA or ROCm required.
- Live mode does not create transcript files unless you redirect terminal output.
