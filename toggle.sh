#!/usr/bin/env bash
set -euo pipefail

PID=$(pgrep -f "python.*dictate\.py" | head -1 || true)
STATE_FILE="${XDG_RUNTIME_DIR:-/tmp}/whisper-dictate-${UID}.state"

if [[ -z "$PID" ]]; then
    echo "whisper-kde daemon is not running." >&2
    exit 1
fi

STATE="idle"
if [[ -f "$STATE_FILE" ]]; then
    STATE="$(tr -d '[:space:]' < "$STATE_FILE")"
fi

if [[ "$STATE" == "recording" ]]; then
    kill -USR2 "$PID"
else
    kill -USR1 "$PID"
fi
