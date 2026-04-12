"""Transcription backend abstraction.

Two backends share a common interface so the daemon can swap between
faster-whisper (CPU, default) and whisper.cpp (Vulkan GPU, optional)
without changing the VAD, D-Bus, or IBus layers.
"""

from __future__ import annotations

import io
import logging
import shutil
import subprocess
import wave
from pathlib import Path
from typing import Any, Protocol

from kdictate.app_metadata import GGML_MODEL_PATH

logger = logging.getLogger("kdictate.daemon.backend")


class TranscriptionBackend(Protocol):
    """Minimal contract every transcription backend must satisfy."""

    def transcribe(self, pcm_chunks: list[Any], audio_seconds: float) -> str:
        """Transcribe int16 PCM chunks and return normalized text."""
        ...  # pragma: no cover


# ------------------------------------------------------------------
# faster-whisper (CPU) backend
# ------------------------------------------------------------------


class FasterWhisperBackend:
    """Wraps the existing faster-whisper / CTranslate2 model."""

    def __init__(self, model: Any, *, language: str, beam_size: int,
                 no_speech_threshold: float, condition_on_previous_text: bool,
                 vad_filter: bool) -> None:
        self.model = model
        self.language = language
        self.beam_size = beam_size
        self.no_speech_threshold = no_speech_threshold
        self.condition_on_previous_text = condition_on_previous_text
        self.vad_filter = vad_filter

    def transcribe(self, pcm_chunks: list[Any], audio_seconds: float) -> str:
        import numpy as np

        if not pcm_chunks:
            return ""
        audio = np.concatenate(pcm_chunks).astype(np.float32) / 32768.0
        audio = audio.clip(-1.0, 1.0)
        if audio.size == 0:
            return ""

        segments, _ = self.model.transcribe(
            audio,
            language=self.language,
            task="transcribe",
            beam_size=self.beam_size,
            best_of=1,
            temperature=0.0,
            condition_on_previous_text=self.condition_on_previous_text,
            vad_filter=self.vad_filter,
            no_speech_threshold=self.no_speech_threshold,
            without_timestamps=True,
        )
        text = " ".join(
            s.text.strip() for s in segments if s.text and s.text.strip()
        ).strip()
        if not text:
            return ""
        return " ".join(text.replace("\r", " ").replace("\n", " ").split())


# ------------------------------------------------------------------
# whisper.cpp (Vulkan GPU) backend
# ------------------------------------------------------------------


def _pcm_to_wav_bytes(pcm_chunks: list[Any], sample_rate: int = 16000) -> bytes:
    """Encode int16 PCM chunks as an in-memory WAV file."""
    import numpy as np

    audio = np.concatenate(pcm_chunks)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # int16
        wf.setframerate(sample_rate)
        wf.writeframes(audio.tobytes())
    return buf.getvalue()


def find_whisper_cpp() -> str | None:
    """Return the path to a whisper.cpp binary, or None."""
    for name in ("whisper-cpp", "whisper-cli", "main"):
        path = shutil.which(name)
        if path is not None:
            return path
    return None


class WhisperCppBackend:
    """Transcribe via whisper.cpp CLI with Vulkan GPU acceleration."""

    def __init__(self, binary: str, model_path: str | Path, *,
                 language: str = "en", beam_size: int = 1,
                 n_threads: int = 4) -> None:
        self.binary = binary
        self.model_path = str(model_path)
        self.language = language
        self.beam_size = beam_size
        self.n_threads = n_threads

    def transcribe(self, pcm_chunks: list[Any], audio_seconds: float) -> str:
        if not pcm_chunks:
            return ""

        wav_bytes = _pcm_to_wav_bytes(pcm_chunks)

        cmd = [
            self.binary,
            "--model", self.model_path,
            "--language", self.language,
            "--beam-size", str(self.beam_size),
            "--threads", str(self.n_threads),
            "--no-timestamps",
            "--no-prints",
            "--file", "-",
        ]

        try:
            result = subprocess.run(
                cmd,
                input=wav_bytes,
                capture_output=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            logger.warning("whisper.cpp timed out after 30s")
            return ""
        except OSError as exc:
            logger.error("whisper.cpp exec failed: %s", exc)
            return ""

        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace").strip()
            logger.warning(
                "whisper.cpp exited %d: %s", result.returncode, stderr[:200],
            )
            return ""

        text = result.stdout.decode(errors="replace").strip()
        if not text:
            return ""
        # Normalize whitespace the same way as the CPU backend.
        return " ".join(text.replace("\r", " ").replace("\n", " ").split())


# ------------------------------------------------------------------
# Backend construction helpers
# ------------------------------------------------------------------


def create_cpu_backend(model: Any, config: Any) -> FasterWhisperBackend:
    """Build the default faster-whisper CPU backend from a DictationConfig."""
    return FasterWhisperBackend(
        model,
        language=config.language,
        beam_size=config.beam_size,
        no_speech_threshold=config.no_speech_threshold,
        condition_on_previous_text=config.condition_on_previous_text,
        vad_filter=config.vad_filter,
    )


def _probe_whisper_cpp(binary: str, model_path: str) -> bool:
    """Run a minimal whisper.cpp invocation to verify it starts correctly.

    Feeds a tiny silent WAV to whisper.cpp. If it exits 0, the binary,
    model, and GPU driver are all working. This catches Vulkan driver
    failures, missing shared libraries, and corrupt model files before
    real dictation starts.
    """
    import numpy as np

    silent_pcm = [np.zeros(16000, dtype=np.int16)]  # 1s silence
    wav_bytes = _pcm_to_wav_bytes(silent_pcm)

    try:
        result = subprocess.run(
            [
                binary,
                "--model", model_path,
                "--language", "en",
                "--no-timestamps",
                "--no-prints",
                "--file", "-",
            ],
            input=wav_bytes,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("whisper.cpp probe failed: %s", exc)
        return False

    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace").strip()
        logger.warning("whisper.cpp probe exited %d: %s", result.returncode, stderr[:300])
        return False

    logger.info("whisper.cpp probe succeeded")
    return True


def create_gpu_backend(config: Any, ggml_model: str | Path | None = None,
                       ) -> WhisperCppBackend | None:
    """Try to build a whisper.cpp GPU backend. Returns None on failure."""
    binary = find_whisper_cpp()
    if binary is None:
        logger.info("whisper.cpp not found on PATH; GPU backend unavailable")
        return None

    model_path = Path(ggml_model) if ggml_model else GGML_MODEL_PATH
    if not model_path.is_file():
        logger.info("GGML model not found at %s; GPU backend unavailable", model_path)
        return None

    logger.info("GPU backend: whisper.cpp=%s model=%s", binary, model_path)

    if not _probe_whisper_cpp(binary, str(model_path)):
        logger.warning("whisper.cpp probe failed; GPU backend unavailable")
        return None

    return WhisperCppBackend(
        binary,
        model_path,
        language=config.language,
        beam_size=config.beam_size,
        n_threads=config.cpu_threads,
    )
