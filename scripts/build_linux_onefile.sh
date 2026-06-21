#!/usr/bin/env bash
set -euo pipefail

output_name="${OUTPUT_NAME:-py-nic-manager}"
python_bin="${PYTHON:-python3}"
dist_dir="${DIST_DIR:-dist_exe}"
work_dir="${WORK_DIR:-build/pyinstaller-linux}"
clean="${CLEAN:-0}"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

if [[ "$clean" == "1" ]]; then
  rm -rf "$dist_dir" "$work_dir"
fi

venv_dir="$work_dir/.venv"
if [[ ! -d "$venv_dir" ]]; then
  "$python_bin" -m venv "$venv_dir"
fi

venv_python="$venv_dir/bin/python"
"$venv_python" -m pip install --upgrade pip
"$venv_python" -m pip install --upgrade pyinstaller
"$venv_python" -m pip install .

separator=":"
fonts_path="$repo_root/py_nic_manager/assets/fonts"
tap_path="$repo_root/py_nic_manager/assets/tap-windows6"
wintun_path="$repo_root/py_nic_manager/assets/wintun"
data_args=(
  --add-data "${fonts_path}${separator}py_nic_manager/assets/fonts"
  --add-data "${tap_path}${separator}py_nic_manager/assets/tap-windows6"
  --add-data "${wintun_path}${separator}py_nic_manager/assets/wintun"
)

hidden_imports=(
  --hidden-import py_admin_launch
  --hidden-import py_nic_manager.app
  --hidden-import py_nic_manager.ttl_exceeded
  --hidden-import py_nic_manager.nat_persistence
  --hidden-import py_nic_manager.global_forwarding
  --hidden-import py_nic_manager.macos_forwarding
)

"$venv_python" -m PyInstaller \
  --noconfirm \
  --clean \
  --onefile \
  --windowed \
  --name "$output_name" \
  --distpath "$dist_dir" \
  --workpath "$work_dir" \
  --specpath "$work_dir" \
  "${data_args[@]}" \
  "${hidden_imports[@]}" \
  py_nic_manager/frozen_entry.py

echo "Built $dist_dir/$output_name"
