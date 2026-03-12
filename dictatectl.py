#!/usr/bin/env python3

"""Terminal control plane for the whisper-dictate daemon."""

from __future__ import annotations

import argparse
import signal
import sys
from pathlib import Path

from dictate_runtime import (
    STATE_IDLE,
    STATE_RECORDING,
    STATE_TRANSCRIBING,
    DaemonControlError,
    RuntimePaths,
    default_runtime_paths,
    read_last_text,
    read_state,
    signal_daemon,
    wait_for_state,
)


DEFAULT_RUNTIME_PATHS = default_runtime_paths()


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments for daemon status and start/stop helpers."""

    parser = argparse.ArgumentParser(description="Terminal control for the whisper-dictate daemon.")
    parser.add_argument(
        "--state-file",
        default=str(DEFAULT_RUNTIME_PATHS.state_file),
        help="Path to the daemon runtime state file.",
    )
    parser.add_argument(
        "--last-text-file",
        default=str(DEFAULT_RUNTIME_PATHS.last_text_file),
        help="Path to the daemon latest-transcript file.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("status", help="Print the current daemon state.")
    subparsers.add_parser("last-text", help="Print the latest transcript.")

    start = subparsers.add_parser("start", help="Start recording.")
    start.add_argument(
        "--wait",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Wait for the daemon to enter recording/transcribing.",
    )
    start.add_argument("--timeout", type=float, default=5.0, help="Seconds to wait when --wait is enabled.")

    stop = subparsers.add_parser("stop", help="Stop recording and print the resulting transcript.")
    stop.add_argument(
        "--wait",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Wait for transcription to finish and the daemon to return to idle.",
    )
    stop.add_argument("--timeout", type=float, default=20.0, help="Seconds to wait when --wait is enabled.")

    toggle = subparsers.add_parser("toggle", help="Toggle recording state.")
    toggle.add_argument(
        "--wait",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Wait for the resulting state transition.",
    )
    toggle.add_argument("--timeout", type=float, default=20.0, help="Seconds to wait when --wait is enabled.")

    return parser.parse_args()


def _print_last_text(last_text_file: Path) -> int:
    """Print the latest transcript, preserving the existing empty-result behavior."""

    text = read_last_text(last_text_file)
    if text:
        print(text)
    else:
        print("(no transcript)", file=sys.stderr)
    return 0


def _handle_start(paths: RuntimePaths, timeout: float, wait: bool) -> int:
    """Start recording unless the daemon is already busy."""

    state = read_state(paths.state_file)
    if state == STATE_RECORDING:
        print(STATE_RECORDING)
        return 0
    if state == STATE_TRANSCRIBING:
        print(STATE_TRANSCRIBING, file=sys.stderr)
        return 1

    signal_daemon(signal.SIGUSR1)
    if not wait:
        print("starting")
        return 0

    new_state = wait_for_state(paths.state_file, {STATE_RECORDING, STATE_TRANSCRIBING}, timeout)
    if new_state is None:
        print("Timed out waiting for recording to start.", file=sys.stderr)
        return 1

    print(new_state)
    return 0


def _handle_stop(paths: RuntimePaths, timeout: float, wait: bool) -> int:
    """Stop recording and print the latest transcript when available."""

    state = read_state(paths.state_file)
    if state == STATE_IDLE:
        return _print_last_text(paths.last_text_file)
    if state == STATE_TRANSCRIBING:
        if not wait:
            print(STATE_TRANSCRIBING)
            return 0
        new_state = wait_for_state(paths.state_file, {STATE_IDLE}, timeout)
        if new_state is None:
            print("Timed out waiting for transcription to finish.", file=sys.stderr)
            return 1
        return _print_last_text(paths.last_text_file)
    if state != STATE_RECORDING:
        print(f"Cannot stop while state is {state}.", file=sys.stderr)
        return 1

    signal_daemon(signal.SIGUSR2)
    if not wait:
        print("stopping")
        return 0

    new_state = wait_for_state(paths.state_file, {STATE_IDLE}, timeout)
    if new_state is None:
        print("Timed out waiting for transcription to finish.", file=sys.stderr)
        return 1

    return _print_last_text(paths.last_text_file)


def _handle_toggle(paths: RuntimePaths, timeout: float, wait: bool) -> int:
    """Toggle between recording and idle states."""

    state = read_state(paths.state_file)
    if state == STATE_RECORDING:
        return _handle_stop(paths, timeout, wait)
    return _handle_start(paths, timeout, wait)


def main() -> int:
    """Run the whisper-dictate terminal control helper."""

    args = parse_args()
    paths = RuntimePaths(
        state_file=Path(args.state_file),
        last_text_file=Path(args.last_text_file),
    )

    try:
        if args.command == "status":
            print(read_state(paths.state_file))
            return 0
        if args.command == "last-text":
            return _print_last_text(paths.last_text_file)
        if args.command == "start":
            return _handle_start(paths, args.timeout, args.wait)
        if args.command == "stop":
            return _handle_stop(paths, args.timeout, args.wait)
        if args.command == "toggle":
            return _handle_toggle(paths, args.timeout, args.wait)
    except DaemonControlError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(f"Unknown command: {args.command}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
