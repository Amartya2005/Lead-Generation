#!/usr/bin/env bash
# Pull the latest code and re-apply setup (keeps .env, config/ and data/).
#   sudo bash /opt/leadgen/app/deploy/oracle/update.sh
set -euo pipefail
PREFIX=/opt/leadgen
[[ $EUID -eq 0 ]] || { echo "Run with sudo."; exit 1; }

echo "==> backing up the database first"
/usr/local/bin/leadgen backup || true

echo "==> pulling latest code"
sudo -u leadgen git -C "$PREFIX/app" pull --ff-only

echo "==> installing dependencies"
sudo -u leadgen "$PREFIX/venv/bin/pip" install --quiet -r "$PREFIX/app/requirements.txt"

echo "==> running the test suite before switching over"
if ! sudo -u leadgen env LEADGEN_ENV_FILE=/dev/null "$PREFIX/venv/bin/python" -m pytest -q "$PREFIX/app/tests" -p no:cacheprovider; then
  echo "!! tests failed on this server; not restarting services. Fix or roll back with:"
  echo "   sudo -u leadgen git -C $PREFIX/app reset --hard HEAD@{1}"
  exit 1
fi

exec bash "$PREFIX/app/deploy/oracle/setup.sh"
