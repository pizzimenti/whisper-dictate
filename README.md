# whisper-cli (CPU-First, Laptop-Optimized)

This project runs **OpenAI Whisper Large V3 Turbo** from your **default system microphone** and prints transcription directly to the terminal in near real time.

No audio files are read for transcription input and no transcript files are written by default.

It uses:
- `openai/whisper-large-v3-turbo` as the source model
- CTranslate2 conversion + int8 quantization for faster local inference
- `faster-whisper` for efficient transcription runtime

## Why this setup

On this machine type (no NVIDIA CUDA GPU detected), the best practical local path is:
1. Convert the model to CTranslate2 format.
2. Run inference on CPU with `int8`.
3. Use physical-core thread count for stable throughput.
4. Keep runtime CPU-only for predictable local behavior.

## Quick start

```bash
cd whisper-cli
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

Convert the model once:

```bash
python prepare_model.py
```

Run live mic transcription:

```bash
python mic_realtime.py
```

Press `Enter` to stop.

## Files

- `prepare_model.py`: downloads/converts Whisper V3 Turbo to local CTranslate2 format.
- `mic_realtime.py`: captures default microphone input and prints live text to terminal.
- `transcribe.py`: transcribes a single file with laptop-tuned defaults.
- `benchmark.py`: measures latency and real-time factor across multiple runs.
- `runtime_profile.py`: hardware-aware runtime defaults.

## Tuning knobs

- `--cpu-threads N`: override thread count.
- `--compute-type int8|float16|float32`: precision/runtime tradeoff.
- `--energy-threshold`: raise this if room noise is triggering false speech.
- `--silence-ms`: lower for faster phrase commits, higher for longer phrases.
- `--beam-size`: higher can improve quality but slows down.

## Notes

- First conversion/download can take time and several GB of storage.
- This project intentionally runs CPU-only.
- Live mode does not create transcript files unless you explicitly redirect terminal output.
