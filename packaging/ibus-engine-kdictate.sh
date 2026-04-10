#!/usr/bin/env bash
set -euo pipefail

# IBus spawns this launcher from its own working directory (typically
# $HOME or /), so `python -m kdictate.ibus_engine` cannot rely on cwd to
# put the runtime-synced package on sys.path. The systemd unit for the
# core daemon covers the same concern via WorkingDirectory=; this script
# does it by setting PYTHONPATH explicitly.
exec env PYTHONPATH="@@REPO_DIR@@${PYTHONPATH:+:$PYTHONPATH}" \
    "@@REPO_DIR@@/.venv/bin/python" -m kdictate.ibus_engine "$@"
