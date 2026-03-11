from __future__ import annotations

"""Live microphone transcription with immediate startup and ordered output.

Design:
- Start audio capture immediately.
- Load model in the background.
- Decode with one or more workers and print text in utterance order.
"""

import argparse
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from runtime_profile import recommended_shortform_cpu_threads, resolve_runtime, set_thread_env


@dataclass
class RuntimeStats:
    lock: threading.Lock = field(default_factory=threading.Lock)
    model_ready: bool = False
    model_load_seconds: float = 0.0
    audio_blocks_in: int = 0
    audio_blocks_dropped: int = 0
    utterances_committed: int = 0
    utterances_dropped: int = 0
    commit_by_silence: int = 0
    commit_by_maxlen: int = 0
    decode_started: int = 0
    decode_completed: int = 0
    decode_failed: int = 0
    decode_active_workers: int = 0
    decode_wait_seconds_total: float = 0.0
    decode_seconds_total: float = 0.0
    decode_audio_seconds_total: float = 0.0
    printed_chars_total: int = 0

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "model_ready": self.model_ready,
                "model_load_seconds": self.model_load_seconds,
                "audio_blocks_in": self.audio_blocks_in,
                "audio_blocks_dropped": self.audio_blocks_dropped,
                "utterances_committed": self.utterances_committed,
                "utterances_dropped": self.utterances_dropped,
                "commit_by_silence": self.commit_by_silence,
                "commit_by_maxlen": self.commit_by_maxlen,
                "decode_started": self.decode_started,
                "decode_completed": self.decode_completed,
                "decode_failed": self.decode_failed,
                "decode_active_workers": self.decode_active_workers,
                "decode_wait_seconds_total": self.decode_wait_seconds_total,
                "decode_seconds_total": self.decode_seconds_total,
                "decode_audio_seconds_total": self.decode_audio_seconds_total,
                "printed_chars_total": self.printed_chars_total,
            }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Real-time Whisper transcription from default microphone. Press Enter to stop."
    )
    parser.add_argument(
        "--model-dir",
        default="models/distil-medium-en-ct2-int8",
        help="Path to converted CTranslate2 model directory.",
    )
    parser.add_argument(
        "--device",
        default="cpu",
        choices=("auto", "cpu"),
        help="Inference device (CPU only in this project).",
    )
    parser.add_argument(
        "--compute-type",
        default=None,
        choices=("float32", "float16", "int8", "int8_float16"),
        help="faster-whisper compute type. If omitted, auto-selects.",
    )
    parser.add_argument(
        "--cpu-threads",
        type=int,
        default=recommended_shortform_cpu_threads(),
        help="Override CPU thread count. Defaults to a short-form latency-oriented value.",
    )
    parser.add_argument("--language", default="en", help="Language code, for example: en, es, fr.")
    parser.add_argument(
        "--task",
        default="transcribe",
        choices=("transcribe", "translate"),
        help="Set `transcribe` to keep original language, `translate` for English output.",
    )
    parser.add_argument("--sample-rate", type=int, default=16000, help="Microphone capture sample rate.")
    parser.add_argument("--block-ms", type=int, default=30, help="Capture block duration in milliseconds.")
    parser.add_argument(
        "--energy-threshold",
        type=float,
        default=300.0,
        help="RMS threshold for speech detection. Increase to ignore noise.",
    )
    parser.add_argument(
        "--silence-ms",
        type=int,
        default=220,
        help="Silence duration that finalizes and transcribes the current utterance.",
    )
    parser.add_argument(
        "--min-speech-ms",
        type=int,
        default=180,
        help="Minimum speech duration required to transcribe an utterance.",
    )
    parser.add_argument(
        "--start-speech-ms",
        type=int,
        default=90,
        help="Consecutive voiced duration required before an utterance starts.",
    )
    parser.add_argument(
        "--max-utterance-s",
        type=float,
        default=2.5,
        help="Force transcription when an utterance reaches this length.",
    )
    parser.add_argument("--beam-size", type=int, default=1, help="Whisper beam size.")
    parser.add_argument(
        "--no-speech-threshold",
        type=float,
        default=0.6,
        help="Reject low-confidence non-speech segments inside Whisper decode.",
    )
    parser.add_argument(
        "--decode-workers",
        type=int,
        default=4,
        help="Parallel decode workers. Increasing this can improve CPU utilization.",
    )
    parser.add_argument(
        "--diag",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Emit periodic performance diagnostics to stderr.",
    )
    parser.add_argument(
        "--diag-interval-s",
        type=float,
        default=2.0,
        help="Seconds between diagnostic log lines.",
    )
    return parser.parse_args()


def _wait_for_enter(stop_event: threading.Event) -> None:
    try:
        input()
    except EOFError:
        pass
    stop_event.set()


def _transcribe_utterance(
    model,
    utterance_pcm: list["np.ndarray"],
    language: str | None,
    task: str,
    beam_size: int,
    no_speech_threshold: float,
) -> str:
    import numpy as np

    if not utterance_pcm:
        return ""

    audio_pcm = np.concatenate(utterance_pcm, axis=0)
    if audio_pcm.size == 0:
        return ""

    audio_f32 = (audio_pcm.astype(np.float32) / 32768.0).clip(-1.0, 1.0)
    segments, _ = model.transcribe(
        audio_f32,
        language=language,
        task=task,
        beam_size=beam_size,
        best_of=1,
        temperature=0.0,
        condition_on_previous_text=False,
        vad_filter=False,
        no_speech_threshold=no_speech_threshold,
        without_timestamps=True,
    )
    text = " ".join(s.text.strip() for s in segments if s.text and s.text.strip()).strip()
    if not text:
        return ""
    return " ".join(text.replace("\r", " ").replace("\n", " ").split())


def _model_loader(
    model_box: dict,
    model_ready: threading.Event,
    stats: RuntimeStats,
    model_dir: str,
    device: str,
    compute_type: str,
    cpu_threads: int,
    num_workers: int,
) -> None:
    started = time.perf_counter()
    try:
        from faster_whisper import WhisperModel

        model_box["model"] = WhisperModel(
            model_dir,
            device=device,
            compute_type=compute_type,
            cpu_threads=cpu_threads,
            num_workers=num_workers,
        )
        load_seconds = time.perf_counter() - started
        with stats.lock:
            stats.model_ready = True
            stats.model_load_seconds = load_seconds
        print(f"Model ready ({load_seconds:.2f}s).", file=sys.stderr, flush=True)
    except Exception as exc:  # noqa: BLE001
        model_box["error"] = exc
        print(f"Model load failed: {exc}", file=sys.stderr, flush=True)
    finally:
        model_ready.set()


def _decode_worker(
    model_box: dict,
    model_ready: threading.Event,
    stats: RuntimeStats,
    utterance_queue: "queue.Queue[tuple[int, list[np.ndarray], float, float] | None]",
    result_queue: "queue.Queue[tuple[int, str] | None]",
    language: str | None,
    task: str,
    beam_size: int,
    no_speech_threshold: float,
) -> None:
    while True:
        item = utterance_queue.get()
        if item is None:
            break

        utterance_id, utterance_pcm, committed_at, audio_seconds = item
        model_ready.wait()
        queue_wait = max(0.0, time.perf_counter() - committed_at)

        if model_box.get("error") is not None:
            result_queue.put((utterance_id, ""))
            continue

        model = model_box.get("model")
        if model is None:
            result_queue.put((utterance_id, ""))
            continue

        with stats.lock:
            stats.decode_started += 1
            stats.decode_active_workers += 1
            stats.decode_wait_seconds_total += queue_wait
            stats.decode_audio_seconds_total += audio_seconds

        started = time.perf_counter()
        try:
            text = _transcribe_utterance(
                model,
                utterance_pcm,
                language,
                task,
                beam_size,
                no_speech_threshold,
            )
        except Exception:  # noqa: BLE001
            text = ""
            with stats.lock:
                stats.decode_failed += 1
        finally:
            elapsed = time.perf_counter() - started
            with stats.lock:
                stats.decode_seconds_total += elapsed
                stats.decode_completed += 1
                stats.decode_active_workers = max(0, stats.decode_active_workers - 1)

        result_queue.put((utterance_id, text))


def _print_worker(stats: RuntimeStats, result_queue: "queue.Queue[tuple[int, str] | None]") -> None:
    next_id = 0
    pending: dict[int, str] = {}
    while True:
        item = result_queue.get()
        if item is None:
            break
        utterance_id, text = item
        pending[utterance_id] = text
        while next_id in pending:
            out = pending.pop(next_id)
            if out:
                print(out, end=" ", flush=True)
                with stats.lock:
                    stats.printed_chars_total += len(out)
            next_id += 1


def _diagnostic_reporter(
    stop_event: threading.Event,
    stats: RuntimeStats,
    runtime_threads: int,
    decode_workers: int,
    audio_queue: "queue.Queue",
    utterance_queue: "queue.Queue",
    result_queue: "queue.Queue",
    interval_s: float,
) -> None:
    try:
        import psutil
    except ImportError:
        psutil = None

    process = psutil.Process() if psutil is not None else None
    if process is not None:
        process.cpu_percent(None)

    prev = stats.snapshot()
    while not stop_event.wait(max(0.2, interval_s)):
        snap = stats.snapshot()
        delta_decode_done = snap["decode_completed"] - prev["decode_completed"]
        delta_decode_started = snap["decode_started"] - prev["decode_started"]
        delta_decode_s = snap["decode_seconds_total"] - prev["decode_seconds_total"]
        delta_audio_s = snap["decode_audio_seconds_total"] - prev["decode_audio_seconds_total"]
        delta_wait_s = snap["decode_wait_seconds_total"] - prev["decode_wait_seconds_total"]
        delta_committed = snap["utterances_committed"] - prev["utterances_committed"]
        delta_dropped = snap["utterances_dropped"] - prev["utterances_dropped"]
        delta_commit_silence = snap["commit_by_silence"] - prev["commit_by_silence"]
        delta_commit_maxlen = snap["commit_by_maxlen"] - prev["commit_by_maxlen"]
        delta_audio_blocks = snap["audio_blocks_in"] - prev["audio_blocks_in"]
        delta_audio_drop = snap["audio_blocks_dropped"] - prev["audio_blocks_dropped"]

        avg_decode = (delta_decode_s / delta_decode_done) if delta_decode_done > 0 else 0.0
        avg_wait = (delta_wait_s / delta_decode_started) if delta_decode_started > 0 else 0.0
        avg_rtf = (delta_decode_s / delta_audio_s) if delta_audio_s > 1e-6 else 0.0

        proc_cpu = process.cpu_percent(None) if process is not None else -1.0
        sys_cpu = psutil.cpu_percent(None) if psutil is not None else -1.0
        target_util = (proc_cpu / (runtime_threads * 100.0) * 100.0) if proc_cpu >= 0 and runtime_threads > 0 else -1.0

        print(
            "[diag] "
            f"ready={int(snap['model_ready'])} load_s={snap['model_load_seconds']:.2f} "
            f"proc_cpu={proc_cpu:.1f}% target_util={target_util:.1f}% sys_cpu={sys_cpu:.1f}% "
            f"active_workers={snap['decode_active_workers']}/{decode_workers} "
            f"audio_q={audio_queue.qsize()} utt_q={utterance_queue.qsize()} res_q={result_queue.qsize()} "
            f"committed={delta_committed} (sil={delta_commit_silence},max={delta_commit_maxlen}) "
            f"dropped_utt={delta_dropped} blocks={delta_audio_blocks} dropped_blocks={delta_audio_drop} "
            f"avg_decode_s={avg_decode:.3f} avg_wait_s={avg_wait:.3f} avg_rtf={avg_rtf:.3f}",
            file=sys.stderr,
            flush=True,
        )
        prev = snap


def main() -> int:
    args = parse_args()
    runtime = resolve_runtime(args.device, args.compute_type, args.cpu_threads)
    set_thread_env(runtime["cpu_threads"])
    decode_workers = max(1, args.decode_workers)
    stats = RuntimeStats()

    model_dir = Path(args.model_dir)
    if not model_dir.exists():
        print(f"Model directory not found: {model_dir}")
        print("Create the default model once with:")
        print("  python prepare_model.py")
        return 1

    try:
        import numpy as np
        import sounddevice as sd
    except ImportError as exc:
        print(f"Missing dependency: {exc.name}")
        print("Install dependencies first: pip install -r requirements.txt")
        return 1

    block_size = max(1, int(args.sample_rate * (args.block_ms / 1000.0)))
    silence_blocks = max(1, int(args.silence_ms / args.block_ms))
    min_speech_blocks = max(1, int(args.min_speech_ms / args.block_ms))
    start_speech_blocks = max(1, int(args.start_speech_ms / args.block_ms))
    max_utterance_blocks = max(1, int((args.max_utterance_s * 1000.0) / args.block_ms))

    audio_queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=512)
    utterance_queue: queue.Queue[tuple[int, list[np.ndarray], float, float] | None] = queue.Queue(maxsize=64)
    result_queue: queue.Queue[tuple[int, str] | None] = queue.Queue(maxsize=64)

    stop_event = threading.Event()
    enter_thread = threading.Thread(target=_wait_for_enter, args=(stop_event,), daemon=True)
    enter_thread.start()

    model_box: dict = {}
    model_ready = threading.Event()
    loader_thread = threading.Thread(
        target=_model_loader,
        args=(
            model_box,
            model_ready,
            stats,
            str(model_dir),
            runtime["device"],
            runtime["compute_type"],
            runtime["cpu_threads"],
            decode_workers,
        ),
        daemon=True,
    )
    loader_thread.start()

    decoder_threads: list[threading.Thread] = []
    for _ in range(decode_workers):
        thread = threading.Thread(
            target=_decode_worker,
            args=(
                model_box,
                model_ready,
                stats,
                utterance_queue,
                result_queue,
                args.language,
                args.task,
                args.beam_size,
                args.no_speech_threshold,
            ),
            daemon=True,
        )
        thread.start()
        decoder_threads.append(thread)

    printer_thread = threading.Thread(target=_print_worker, args=(stats, result_queue), daemon=True)
    printer_thread.start()

    diag_stop_event = threading.Event()
    diag_thread = None
    if args.diag:
        diag_thread = threading.Thread(
            target=_diagnostic_reporter,
            args=(
                diag_stop_event,
                stats,
                runtime["cpu_threads"],
                decode_workers,
                audio_queue,
                utterance_queue,
                result_queue,
                args.diag_interval_s,
            ),
            daemon=True,
        )
        diag_thread.start()

    print(
        "Live transcription started immediately. Press Enter to stop.\n"
        "Model is loading in background; first transcript appears when ready.\n"
        f"device={runtime['device']} compute_type={runtime['compute_type']} cpu_threads={runtime['cpu_threads']} "
        f"decode_workers={decode_workers} beam_size={args.beam_size}",
        flush=True,
    )

    utterance_pcm: list[np.ndarray] = []
    pending_speech_pcm: list[np.ndarray] = []
    pending_silence_pcm: list[np.ndarray] = []
    in_speech = False
    speech_block_count = 0
    pending_speech_block_count = 0
    trailing_silence_count = 0
    next_utterance_id = 0

    def _commit_utterance() -> None:
        nonlocal in_speech, speech_block_count, pending_speech_block_count, trailing_silence_count
        nonlocal utterance_pcm, pending_speech_pcm, pending_silence_pcm, next_utterance_id
        if speech_block_count >= min_speech_blocks and utterance_pcm:
            audio_samples = sum(len(chunk) for chunk in utterance_pcm)
            audio_seconds = audio_samples / float(args.sample_rate)
            payload = (next_utterance_id, utterance_pcm, time.perf_counter(), audio_seconds)
            next_utterance_id += 1
            try:
                utterance_queue.put_nowait(payload)
                with stats.lock:
                    stats.utterances_committed += 1
            except queue.Full:
                with stats.lock:
                    stats.utterances_dropped += 1
        in_speech = False
        speech_block_count = 0
        pending_speech_block_count = 0
        trailing_silence_count = 0
        utterance_pcm = []
        pending_speech_pcm = []
        pending_silence_pcm = []

    def audio_callback(indata, frames, callback_time, status) -> None:
        del frames, callback_time
        if status or stop_event.is_set():
            return
        chunk = indata[:, 0].copy()
        with stats.lock:
            stats.audio_blocks_in += 1
        try:
            audio_queue.put_nowait(chunk)
        except queue.Full:
            with stats.lock:
                stats.audio_blocks_dropped += 1

    stream = sd.InputStream(
        samplerate=args.sample_rate,
        channels=1,
        dtype="int16",
        blocksize=block_size,
        callback=audio_callback,
    )

    with stream:
        while True:
            if stop_event.is_set() and audio_queue.empty():
                break
            try:
                chunk = audio_queue.get(timeout=0.05)
            except queue.Empty:
                continue

            rms = float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))
            voiced = rms >= args.energy_threshold

            if voiced:
                if not in_speech:
                    pending_speech_pcm.append(chunk)
                    pending_speech_block_count += 1
                    if pending_speech_block_count >= start_speech_blocks:
                        in_speech = True
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
                with stats.lock:
                    stats.commit_by_maxlen += 1
                _commit_utterance()
                continue

            if in_speech and trailing_silence_count >= silence_blocks:
                with stats.lock:
                    stats.commit_by_silence += 1
                _commit_utterance()

    if in_speech and speech_block_count >= min_speech_blocks and utterance_pcm:
        _commit_utterance()

    for _ in range(decode_workers):
        utterance_queue.put(None)
    for thread in decoder_threads:
        thread.join()

    result_queue.put(None)
    printer_thread.join()
    loader_thread.join()
    diag_stop_event.set()
    if diag_thread is not None:
        diag_thread.join()

    print("\nStopped.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
