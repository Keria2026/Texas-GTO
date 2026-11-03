#!/usr/bin/env bash
set -euo pipefail
umask 077
serve_bundle=$(cd "$(dirname "$0")" && pwd)
serve_prefix=$(realpath -m "${1:-$HOME/.local/share/texas-gto}")
if [[ ! "$serve_prefix" =~ ^/[a-zA-Z0-9_./-]+$ || "$serve_prefix" == / || $# -gt 2 ]]; then
  echo 'Usage: bash install.sh /absolute/install/path [exported-state-directory]' >&2
  exit 2
fi
python3 -c 'import platform,sys; assert sys.version_info[:2] == (3,12) and platform.system() == "Linux" and platform.machine() == "x86_64", "Requires Ubuntu x86_64 and Python 3.12"'
(cd "$serve_bundle" && sha256sum --check --quiet SHA256SUMS)
serve_unit="$HOME/.config/systemd/user/texas-gto-local.service"
if [[ ${TEXAS_INSTALL_NO_SERVICE:-0} != 1 ]]; then
  systemctl --user show-environment >/dev/null
  if [[ -f "$serve_unit" ]] && ! grep -Fqx "WorkingDirectory=$serve_prefix" "$serve_unit" && [[ $# -lt 2 ]]; then
    echo 'Existing service uses another data directory. Export its state and supply the bundle as the second argument.' >&2
    exit 2
  fi
fi
mkdir -p "$serve_prefix/releases"
exec 9>"$serve_prefix/install.lock"
flock -n 9 || { echo 'Another installation is running.' >&2; exit 1; }
serve_release=$(mktemp -d "$serve_prefix/releases/build-XXXXXX")
python3 -m venv "$serve_release"
"$serve_release/bin/python" -m pip install --disable-pip-version-check --no-index --find-links "$serve_bundle/wheels" \
  --require-hashes -r "$serve_bundle/requirements.txt"
"$serve_release/bin/python" -m pip install --disable-pip-version-check --no-index --no-deps "$serve_bundle"/wheels/texas_gto-*.whl
"$serve_release/bin/python" -m pip check
"$serve_release/bin/python" -c 'import texas_analysis.app, texas_strategy.mushroom_population'
if [[ $# == 2 ]]; then
  "$serve_release/bin/texas-state" restore --bundle "$2" --data-dir "$serve_prefix/data"
fi
mkdir -p "$serve_prefix/data"
ln -s "$serve_release" "$serve_prefix/.current-$(basename "$serve_release")"
mv -Tf "$serve_prefix/.current-$(basename "$serve_release")" "$serve_prefix/current"
cp "$serve_bundle/BUILD.json" "$serve_release/BUILD.json"
if [[ ${TEXAS_INSTALL_NO_SERVICE:-0} == 1 ]]; then
  echo "Installed at $serve_prefix; service registration was explicitly skipped."
  exit 0
fi
mkdir -p "$(dirname "$serve_unit")"
if [[ -f "$serve_unit" ]]; then cp "$serve_unit" "$serve_release/previous-service.conf"; fi
cat > "$serve_unit" <<UNIT
[Unit]
Description=Texas GTO portable algorithm service
StartLimitIntervalSec=0

[Service]
Type=exec
WorkingDirectory=$serve_prefix
ExecStart=$serve_prefix/current/bin/python -m texas_analysis
Environment=PYTHONUNBUFFERED=1
Environment=TEXAS_PORT=8765
Environment=TEXAS_AUTO_TRAIN=1
Environment=TEXAS_DATA_DIR=$serve_prefix/data
UnsetEnvironment=TEXAS_API_TOKEN TEXAS_ALLOWED_ORIGINS
MemoryAccounting=yes
MemoryMax=1G
Restart=always
RestartSec=3
TimeoutStopSec=30
UMask=0077

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable texas-gto-local.service
systemctl --user restart texas-gto-local.service
echo "Installed and started. Data remains at $serve_prefix/data; API listens on 127.0.0.1:8765."
