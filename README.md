# whisper-dictate

Local Whisper transcription for Wayland — two modes:

1. **Live CLI** (`mic_realtime.py`): streams mic audio to the terminal in real time.
2. **System dictation daemon** (`dictate.py`): toggle recording with a global hotkey; types the result into any focused window via `wtype`.

Uses `distil-whisper/distil-large-v3` converted to CTranslate2 int8 for CPU-only inference.

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

If `torch` is unavailable for your Python version, use a Python 3.12 venv for this step only.

### Live CLI

```bash
python mic_realtime.py
```

Press `Enter` to stop.

### System dictation daemon

Install `wtype` for Wayland text injection:

```bash
sudo pacman -S wtype
```

Start the daemon manually:

```bash
source .venv/bin/activate
python dictate.py
```

Or enable as a systemd user service (auto-starts with your session):

```bash
cp whisper-dictate.service ~/.config/systemd/user/
systemctl --user enable --now whisper-dictate
```

Bind a global hotkey to `toggle.sh`. On KDE:

- Open **System Settings → Shortcuts → Custom Shortcuts**
- Add a new command shortcut pointing to `~/Code/whisper-dictate/toggle.sh`
- Assign a key (e.g. `Meta+Alt+Space`)

Press the hotkey once to start recording, again to stop — the transcribed text is typed at the cursor.

## Tuning

- `--cpu-threads N`: override thread count.
- `--compute-type int8|float16|float32`: precision/runtime tradeoff.
- `--language`: defaults to `en`.
- `--beam-size`: defaults to 5.
- `--energy-threshold`, `--silence-ms`, `--max-utterance-s`: VAD controls (CLI mode only).
- `--task transcribe|translate`: keep original language vs force English output (CLI mode only).
- `--decode-workers`, `--diag`, `--diag-interval-s`: parallelism and diagnostics (CLI mode only).

## Files

- `prepare_model.py`: download and convert the model.
- `mic_realtime.py`: live terminal transcription.
- `dictate.py`: system-wide dictation daemon.
- `toggle.sh`: send toggle signal to the daemon.
- `whisper-dictate.service`: systemd user unit.
- `transcribe.py`: transcribe an audio file.
- `benchmark.py`: latency and RTF benchmarking.
- `runtime_profile.py`: shared CPU/runtime helpers.

## Notes

- First conversion can take time and several GB of storage.
- CPU-only; no CUDA or ROCm required.
- Live mode does not create transcript files unless you redirect terminal output.
