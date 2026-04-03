#!/usr/bin/env bash
set -euo pipefail

# Active scope:
# - README.md
# - install.sh
# - ibus_engine.py
# - dictate.py
# - dictatectl.py
# - dictate_runtime.py
# - systemd/**
# - packaging/**

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
active_paths=(
  "$repo_dir/README.md"
  "$repo_dir/install.sh"
  "$repo_dir/ibus_engine.py"
  "$repo_dir/dictate.py"
  "$repo_dir/dictatectl.py"
  "$repo_dir/dictate_runtime.py"
  "$repo_dir/systemd"
  "$repo_dir/packaging"
)

echo "==> Checking active paths for forbidden injector or clipboard backends"
if rg -n -e 'ydotool|dotool|wtype|wl-copy|xdotool|type_text' "${active_paths[@]}"; then
  echo "Forbidden backend reference found in active paths." >&2
  exit 1
fi

echo "==> Checking required packaging assets"
for file in \
  "$repo_dir/packaging/ibus-engine-whisper-dictate" \
  "$repo_dir/systemd/io.github.pizzimenti.WhisperDictate.service" \
  "$repo_dir/packaging/io.github.pizzimenti.WhisperDictate.service" \
  "$repo_dir/packaging/io.github.pizzimenti.WhisperDictate.component.xml"; do
  if [[ ! -f "$file" ]]; then
    echo "Missing required file: $file" >&2
    exit 1
  fi
done

echo "OK"
