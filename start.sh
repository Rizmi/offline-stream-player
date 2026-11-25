#!/bin/bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${DIR}/.env"

if [ ! -f "${ENV_FILE}" ]; then
    echo "Error: Missing ${ENV_FILE}. Copy .env.example to .env and configure it." >&2
    exit 1
fi

set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a

for key in DOWNLOAD_DIR HOST PORT AUTO_OPEN_BROWSER; do
    if [ -z "${!key}" ]; then
        echo "Error: Required key '${key}' is missing or empty in .env" >&2
        exit 1
    fi
done

URL="http://${HOST}:${PORT}"

# Enable NVIDIA VA-API (NVDEC) when launching Firefox/Zen Browser
export LIBVA_DRIVER_NAME="${LIBVA_DRIVER_NAME:-nvidia}"
export NVD_BACKEND="${NVD_BACKEND:-direct}"
export MOZ_DISABLE_RDD_SANDBOX="${MOZ_DISABLE_RDD_SANDBOX:-1}"

open_browser() {
    if [ "${AUTO_OPEN_BROWSER}" != "true" ]; then
        return 0
    fi
    if [ -n "${BROWSER}" ]; then
        if ! command -v "${BROWSER}" >/dev/null 2>&1; then
            echo "Error: Configured BROWSER '${BROWSER}' not found in PATH" >&2
            return 1
        fi
        "${BROWSER}" "${URL}" >/dev/null 2>&1 &
    elif command -v xdg-open >/dev/null 2>&1; then
        xdg-open "${URL}" >/dev/null 2>&1 &
    else
        echo "Open ${URL} in your browser."
    fi
}

if python3 -c "import socket, sys; s=socket.socket(); s.settimeout(0.5); sys.exit(0 if s.connect_ex(('${HOST}', int('${PORT}')))==0 else 1)" 2>/dev/null; then
    echo "Offline Stream Player already running at ${URL}"
    open_browser
    exit 0
fi

echo "Starting Offline Stream Player on ${URL}..."
cd "${DIR}"
python3 "${DIR}/app.py" &
PID=$!

sleep 1
if ! kill -0 "$PID" 2>/dev/null; then
    wait "$PID"
fi
open_browser

trap "kill $PID 2>/dev/null; exit 0" SIGINT SIGTERM
wait $PID
