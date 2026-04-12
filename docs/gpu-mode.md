# KDictate GPU Mode -- whisper.cpp Vulkan Backend

KDictate's CPU backend (faster-whisper, CTranslate2, int8) hits a ~5s
encoder floor per utterance on modern laptop hardware. The encoder pads
all audio to 30s regardless of actual length, and faster-whisper does not
expose the `audio_ctx` optimization that would let it skip the padding.

whisper.cpp has a Vulkan backend that works across GPU vendors (AMD,
NVIDIA, Intel) and has shown 3-12x speedups on integrated GPUs. Adding
it as an optional backend preserves the zero-dependency CPU default
while offering a real latency improvement where Vulkan is available.

## Design Constraints

- CPU mode remains the default. GPU mode is opt-in.
- The only new system dependency is `whisper-cpp` with Vulkan support
  (available as an AUR package, or buildable from source).
- No ROCm, no CUDA, no vendor-specific driver stack required.
- Model files: GGML format for whisper.cpp, alongside the existing
  CTranslate2 model. Both are downloaded on first use.
- The daemon selects the backend at startup. No hot-switching.
- If whisper.cpp or Vulkan is unavailable, fall back to CPU silently.

## Milestones

### M1 -- whisper.cpp subprocess integration

Wire whisper.cpp as an alternative transcription backend behind the
existing `TranscriptionBackend` protocol.

- Add a `WhisperCppBackend` that shells out to `whisper-cpp` CLI or
  links via `pywhispercpp` bindings
- Accept PCM audio, return text -- same contract as `FasterWhisperBackend`
- Detect whether `whisper-cpp` is available at daemon startup
- Add `--backend cpu|gpu|auto` flag (`auto` = try GPU, fall back to CPU)
- Keep the daemon, VAD, D-Bus, and IBus layers completely unchanged

### M2 -- GGML model management

Download and manage the GGML-format model alongside the CTranslate2 one.

- Download `ggml-large-v3-turbo.bin` (~1.6 GB FP16) from HuggingFace
  on first GPU-mode use
- Store under `~/.local/share/kdictate/ggml-large-v3-turbo/`
- Add model download to install.py as an optional GPU step
- Validate model integrity (file size or hash check)

### M3 -- Vulkan device selection and validation

Ensure the Vulkan backend picks the right GPU and handles failures.

- List available Vulkan devices at startup, log the selection
- Prefer discrete GPU over integrated if both are present
- Validate that the selected device can actually run inference
  (not all Vulkan drivers support the required features)
- Fall back to CPU with a clear log message on any Vulkan failure

### M4 -- Performance validation and tuning

Verify the GPU path is actually faster and tune parameters.

- Compare decode times: CPU (faster-whisper) vs GPU (whisper.cpp Vulkan)
  using the existing decode metrics logging
- Tune whisper.cpp parameters: threads, beam size, GPU layers
- Test with quantized models (Q8_0, Q5_0) if FP16 is too large for
  iGPU shared memory
- Document expected speedups for common hardware classes

### M5 -- Install and packaging

Make GPU mode installable without breaking the CPU-only default.

- Add optional GPU setup to install.py (download GGML model, check for
  whisper-cpp binary)
- Keep the CPU-only install path unchanged -- no new required deps
- Update README with GPU mode instructions
- Update systemd service to pass `--backend auto` if GPU mode is
  installed

## Out of Scope

- ROCm or CUDA backends (vendor-specific, heavy dependencies)
- Replacing faster-whisper as the CPU backend (it works fine)
- GPU acceleration for VAD (Silero is already fast on CPU)
- Building whisper.cpp from source as part of install.py
