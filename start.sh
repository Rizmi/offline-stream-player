#!/bin/bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT=8765

echo "Starting Offline Stream Player server on port ${PORT}..."
python3 "${DIR}/player/server.py" &
PID=$!

sleep 1

if which zen-browser >/dev/null 2>&1; then
    zen-browser "http://localhost:${PORT}" &
elif which xdg-open >/dev/null 2>&1; then
    xdg-open "http://localhost:${PORT}" &
elif which open >/dev/null 2>&1; then
    open "http://localhost:${PORT}" &
else
    echo "Open http://localhost:${PORT} in your web browser."
fi

trap "kill $PID 2>/dev/null; exit 0" SIGINT SIGTERM
wait $PID
