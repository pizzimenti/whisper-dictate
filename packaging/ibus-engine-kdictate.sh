#!/usr/bin/env bash
set -euo pipefail

exec "@@REPO_DIR@@/.venv/bin/python" -m kdictate.ibus_engine "$@"
