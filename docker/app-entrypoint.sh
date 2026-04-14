#!/bin/sh
set -e
set -u

mkdir -p "${APP_RUNTIME_DIR:-/runtime}"
cd "${APP_RUNTIME_DIR:-/runtime}"

if [ "${APP_DOCKER_BOOTSTRAP_OLLAMA:-1}" = "1" ]; then
  python /app/scripts/bootstrap_ollama.py
fi

exec python /app/app.py --host 0.0.0.0 --port 8000 --workspace "${APP_WORKSPACE:-/workspace}"
