#!/usr/bin/env bash
set -euo pipefail

# Active scope:
# - README.md
# - install.sh
# - kdictate/**
# - systemd/**
# - packaging/**

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
active_paths=(
  "$repo_dir/README.md"
  "$repo_dir/install.sh"
  "$repo_dir/kdictate"
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
  "$repo_dir/packaging/ibus-engine-kdictate.sh" \
  "$repo_dir/systemd/io.github.pizzimenti.KDictate.service" \
  "$repo_dir/packaging/io.github.pizzimenti.KDictate.service" \
  "$repo_dir/packaging/io.github.pizzimenti.KDictate.component.xml"; do
  if [[ ! -f "$file" ]]; then
    echo "Missing required file: $file" >&2
    exit 1
  fi
done

echo "OK"
