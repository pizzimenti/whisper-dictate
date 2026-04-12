# KDictate GPU Mode

Optional Vulkan-accelerated transcription via whisper.cpp.  Preserves
the zero-dependency CPU default while cutting decode latency roughly
in half on systems with a Vulkan-capable GPU.

## How it works

The daemon supports two transcription backends behind a common
`TranscriptionBackend` protocol:

- **CPU** (default): faster-whisper / CTranslate2, int8 quantization.
  No extra dependencies.  ~5 s decode floor on a Ryzen 5 8640HS.
- **GPU** (opt-in): whisper.cpp CLI with Vulkan, Q8_0 quantization,
  beam 3, flash attention.  ~2.5 s decode on the same hardware.

Both use the same large-v3-turbo model weights.  The GPU path uses
Q8_0 rather than FP16 because benchmarking showed it is 15 % faster
with no measurable accuracy loss, even under heavy background noise
(SNR 5 dB).  Beam 3 is free on the GPU (the encoder dominates) and
preserves capitalization and punctuation that beam 1 sometimes drops.

Backend selection is controlled by `--backend cpu|gpu|auto`:

- `cpu` — use faster-whisper (default, no GPU needed).
- `gpu` — require whisper.cpp + Vulkan; fail if unavailable.
- `auto` — try GPU, fall back to CPU silently.

The installer auto-detects GPU availability and prompts the user.
When GPU mode is selected, `--backend auto` is baked into the systemd
service so the daemon tries GPU first on every start.

## Requirements for GPU mode

- `whisper-cpp` with Vulkan support on `PATH` (Arch: `yay -S whisper.cpp-vulkan`)
- The GGML Q8_0 model (~874 MB), downloaded automatically by the installer
- A Vulkan-capable GPU with working drivers

## Architecture

Nothing outside `backend.py` knows which backend is active.  The VAD
segmenter, D-Bus service, IBus engine, and CLI are unchanged.

```text
daemon.py
  └─ TranscriptionBackend.transcribe(pcm_chunks, audio_seconds) -> str
       ├─ FasterWhisperBackend  (CPU, delegates to transcribe_pcm)
       └─ WhisperCppBackend     (GPU, subprocess to whisper-cli)
```

At startup the daemon probes the GPU backend by feeding 1 s of silence
to whisper.cpp.  If the probe fails (missing binary, missing model,
Vulkan driver error), the daemon falls back to CPU with a log message.
