from __future__ import annotations

"""Runtime selection helpers shared by all CLI entrypoints.

Design constraints:
- Enforce CPU-only execution for portability and predictable behavior.
- Make device/precision choices explicit so scripts can print and verify runtime.
- Keep thread settings predictable by aligning BLAS/OpenMP thread vars.
"""

import os

try:
    import psutil
except ImportError:
    psutil = None


def recommended_cpu_threads() -> int:
    """Choose CPU thread count for local transcription.

    Default to all logical cores to maximize sustained decode throughput.

    Audio capture and terminal output run on lightweight threads; keeping one
    core reserved under-utilized the CPU on this laptop during real-time
    transcription.
    """
    if psutil is not None:
        physical = psutil.cpu_count(logical=False)
        logical = psutil.cpu_count(logical=True) or os.cpu_count() or 1
    else:
        physical = None
        logical = os.cpu_count() or 1
    if logical and logical > 0:
        return logical
    if physical and physical > 0:
        return physical
    return max(1, os.cpu_count() or 1)


def recommended_shortform_cpu_threads() -> int:
    """Choose CPU threads for single-utterance or dictation-style decode.

    Short clips benefit less from saturating all logical cores, and on this
    project's CPU benchmarks that increased scheduling overhead enough to hurt
    latency. Prefer physical cores when available, otherwise fall back to half
    the logical count.
    """
    if psutil is not None:
        physical = psutil.cpu_count(logical=False)
        if physical and physical > 0:
            return physical
        logical = psutil.cpu_count(logical=True) or os.cpu_count() or 1
    else:
        logical = os.cpu_count() or 1
    if logical <= 2:
        return max(1, logical)
    return max(1, logical // 2)


def resolve_runtime(device: str | None, compute_type: str | None, cpu_threads: int | None) -> dict:
    """Resolve effective runtime options under a CPU-only policy.

    Any `device` value currently resolves to CPU. We keep the argument for
    CLI compatibility and future extension without breaking call sites.
    """
    del device
    resolved_device = "cpu"

    if compute_type:
        resolved_compute_type = compute_type
    else:
        # Best speed/memory tradeoff on CPU in this project.
        resolved_compute_type = "int8"

    resolved_cpu_threads = cpu_threads if cpu_threads and cpu_threads > 0 else recommended_cpu_threads()
    return {
        "device": resolved_device,
        "compute_type": resolved_compute_type,
        "cpu_threads": resolved_cpu_threads,
    }


def set_thread_env(cpu_threads: int) -> None:
    """Set common math-runtime thread env vars to the selected thread count."""
    for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[var] = str(cpu_threads)
