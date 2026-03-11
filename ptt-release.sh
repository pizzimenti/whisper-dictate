#!/usr/bin/env bash
set -euo pipefail

PID=$(pgrep -f "python.*dictate\.py" | head -1 || true)

if [[ -z "$PID" ]]; then
    echo "whisper-dictate daemon is not running." >&2
    exit 1
fi

kill -USR2 "$PID"
