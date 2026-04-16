"""Whisper model loading, transcription, and VAD helpers for the daemon."""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("kdictate.daemon.vad")

VAD_QUEUE_POLL_TIMEOUT_S = 0.15
AUDIO_QUEUE_MAXSIZE = 512    # ~15s of 30ms blocks at 16kHz
UTTERANCE_QUEUE_MAXSIZE = 64  # max in-flight utterances

# Whisper models hallucinate these phrases on silence or near-silence.
# Comparison is case-insensitive with punctuation and extra whitespace
# collapsed.  The filter only fires when the utterance average RMS is
# below the configured VAD energy threshold so that legitimate short
# utterances ("okay", "bye") are not suppressed.
HALLUCINATION_PHRASES: frozenset[str] = frozenset({
    "thank you",
    "thanks for watching",
    "thank you for watching",
    "you",
    "bye",
    "goodbye",
    "the end",
    "thanks",
    "so",
    "okay",
})

#: Characters stripped before comparing against ``HALLUCINATION_PHRASES``.
_PUNCT_TABLE = str.maketrans("", "", ".,!?;:…\"'""''()[]{}")

def is_hallucination(text: str) -> bool:
    """Return True if *text* matches a known Whisper hallucination phrase."""
    normalized = " ".join(text.translate(_PUNCT_TABLE).strip().lower().split())
    return normalized in HALLUCINATION_PHRASES


def postprocess_transcript(
    text: str,
    pcm_chunks: list[Any],
    energy_threshold: float = 1500.0,
) -> str:
    """Normalize whitespace and suppress hallucinations on near-silent audio.

    Both backends should call this on raw transcript text before returning
    it to the daemon.  *energy_threshold* should match the configured VAD
    energy threshold so the suppression ceiling tracks user settings.
    """
    import numpy as np

    if not text:
        return ""
    text = " ".join(text.replace("\r", " ").replace("\n", " ").split())
    if is_hallucination(text) and pcm_chunks:
        samples = np.concatenate(pcm_chunks)
        if samples.size == 0:
            return text
        avg_rms = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))
        if avg_rms < energy_threshold:
            logger.info(
                "suppressed hallucination: %r (avg_rms=%.0f)", text, avg_rms,
            )
            return ""
    return text


def load_whisper_model(
    model_dir: str | Path,
    *,
    device: str = "cpu",
    compute_type: str = "int8",
    cpu_threads: int = 1,
    num_workers: int = 1,
) -> Any:
    """Load a faster-whisper CTranslate2 model and return it.

    Import is deferred so callers that only need other helpers don't pay the
    import cost.
    """
    from faster_whisper import WhisperModel

    return WhisperModel(
        str(model_dir),
        device=device,
        compute_type=compute_type,
        cpu_threads=cpu_threads,
        num_workers=num_workers,
    )


def transcribe_pcm(
    model: Any,
    pcm_chunks: list[Any],
    *,
    language: str = "en",
    task: str = "transcribe",
    beam_size: int = 1,
    no_speech_threshold: float = 0.6,
    condition_on_previous_text: bool = False,
    vad_filter: bool = True,
    energy_threshold: float = 1500.0,
) -> str:
    """Transcribe a list of int16 PCM chunks and return normalized text."""
    import numpy as np

    if not pcm_chunks:
        return ""

    audio = np.concatenate(pcm_chunks).astype(np.float32) / 32768.0
    audio = audio.clip(-1.0, 1.0)
    if audio.size == 0:
        return ""

    t0 = time.monotonic()
    segments, _ = model.transcribe(
        audio,
        language=language,
        task=task,
        beam_size=beam_size,
        best_of=1,
        temperature=0.0,
        condition_on_previous_text=condition_on_previous_text,
        vad_filter=vad_filter,
        no_speech_threshold=no_speech_threshold,
        without_timestamps=True,
    )
    seg_texts = [s.text.strip() for s in segments if s.text and s.text.strip()]
    elapsed = time.monotonic() - t0
    text = " ".join(seg_texts).strip()
    logger.info(
        "transcribe_pcm: %.1fs, %d segments, %d chars",
        elapsed, len(seg_texts), len(text),
    )
    if not text:
        return ""
    return postprocess_transcript(text, pcm_chunks, energy_threshold=energy_threshold)


@dataclass
class VADConfig:
    """Parameters for the energy-based VAD segmenter."""

    sample_rate: int = 16000
    block_ms: int = 30
    energy_threshold: float = 1500.0
    silence_ms: int = 300
    min_speech_ms: int = 180
    start_speech_ms: int = 150
    max_utterance_s: float = 10.0

    @property
    def silence_blocks(self) -> int:
        return max(1, int(self.silence_ms / self.block_ms))

    @property
    def min_speech_blocks(self) -> int:
        return max(1, int(self.min_speech_ms / self.block_ms))

    @property
    def start_speech_blocks(self) -> int:
        return max(1, int(self.start_speech_ms / self.block_ms))

    @property
    def max_utterance_blocks(self) -> int:
        return max(1, int((self.max_utterance_s * 1000.0) / self.block_ms))


class VADSegmenter:
    """Energy-based voice activity detector that segments audio into utterances.

    Reads int16 PCM chunks from ``audio_queue`` and posts completed utterance
    chunk lists to ``utterance_queue``. Runs until ``stop_event`` is set, then
    flushes any in-progress utterance before posting a ``None`` sentinel.

    The utterance queue items are ``(pcm_chunks, audio_seconds)`` tuples, or
    ``None`` as the stop sentinel.
    """

    def __init__(
        self,
        config: VADConfig,
        audio_queue: queue.Queue,
        utterance_queue: queue.Queue,
        stop_event: threading.Event,
    ) -> None:
        self.config = config
        self.audio_queue = audio_queue
        self.utterance_queue = utterance_queue
        self.stop_event = stop_event

    def run(self) -> None:
        """Block until stop_event is set, segmenting audio the whole time."""
        import numpy as np

        cfg = self.config
        silence_blocks = cfg.silence_blocks
        min_speech_blocks = cfg.min_speech_blocks
        start_speech_blocks = cfg.start_speech_blocks
        max_utterance_blocks = cfg.max_utterance_blocks

        utterance_pcm: list[Any] = []
        pending_speech_pcm: list[Any] = []
        pending_silence_pcm: list[Any] = []
        in_speech = False
        speech_block_count = 0
        pending_speech_block_count = 0
        trailing_silence_count = 0

        def commit() -> None:
            nonlocal in_speech, speech_block_count, pending_speech_block_count
            nonlocal trailing_silence_count, utterance_pcm, pending_speech_pcm, pending_silence_pcm
            if speech_block_count >= min_speech_blocks and utterance_pcm:
                audio_seconds = sum(len(c) for c in utterance_pcm) / float(cfg.sample_rate)
                avg_rms = float(np.sqrt(np.mean(
                    np.concatenate(utterance_pcm).astype(np.float32) ** 2
                )))
                pending = self.utterance_queue.qsize()
                try:
                    self.utterance_queue.put_nowait((list(utterance_pcm), audio_seconds))
                    logger.info(
                        "utterance committed: %.1fs audio, %d blocks, %d queued, avg_rms=%.0f",
                        audio_seconds, speech_block_count, pending, avg_rms,
                    )
                except queue.Full:
                    logger.warning("utterance dropped (queue full): %.1fs audio", audio_seconds)
            in_speech = False
            speech_block_count = 0
            pending_speech_block_count = 0
            trailing_silence_count = 0
            utterance_pcm.clear()
            pending_speech_pcm.clear()
            pending_silence_pcm.clear()

        try:
            while not self.stop_event.is_set():
                try:
                    chunk = self.audio_queue.get(timeout=VAD_QUEUE_POLL_TIMEOUT_S)
                except queue.Empty:
                    continue

                rms = float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))
                voiced = rms >= cfg.energy_threshold

                if voiced:
                    if not in_speech:
                        pending_speech_pcm.append(chunk)
                        pending_speech_block_count += 1
                        if pending_speech_block_count >= start_speech_blocks:
                            in_speech = True
                            logger.info("speech started (rms=%.0f)", rms)
                            utterance_pcm = list(pending_speech_pcm)
                            speech_block_count = len(utterance_pcm)
                            pending_speech_pcm = []
                            pending_speech_block_count = 0
                            pending_silence_pcm = []
                            trailing_silence_count = 0
                    else:
                        if pending_silence_pcm:
                            utterance_pcm.extend(pending_silence_pcm)
                            pending_silence_pcm = []
                        utterance_pcm.append(chunk)
                        speech_block_count += 1
                        trailing_silence_count = 0
                elif in_speech:
                    pending_silence_pcm.append(chunk)
                    trailing_silence_count += 1
                else:
                    pending_speech_pcm = []
                    pending_speech_block_count = 0

                if in_speech and speech_block_count >= max_utterance_blocks:
                    logger.info("force-commit (max utterance reached)")
                    commit()
                    continue

                if in_speech and trailing_silence_count >= silence_blocks:
                    commit()

            # Flush any in-progress utterance when recording stops
            if in_speech and speech_block_count >= min_speech_blocks and utterance_pcm:
                commit()
        finally:
            # Always post the stop sentinel — even if the loop above raised
            # — so the decode consumer can never wedge waiting for a sentinel
            # that never arrives. A short timeout guards against the unlikely
            # case of a fully-saturated utterance_queue.
            try:
                self.utterance_queue.put(None, timeout=1.0)
                logger.info("vad sentinel posted")
            except Exception:  # noqa: BLE001
                logger.warning("vad sentinel post failed (queue full or timeout)")
