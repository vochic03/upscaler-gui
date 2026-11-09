#!/bin/bash
# Start, stop or check the upscaler.
#
#   ./start.sh          start the GUI and the local worker, print the URL
#   ./start.sh stop     stop both
#   ./start.sh status   say what is running
#
# The GUI is the thing you open: http://127.0.0.1:8790/
# It binds to 127.0.0.1 only. Nothing leaves the machine unless you press
# a button that rents a GPU.

set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
PORT="${UPSCALER_PORT:-8790}"
WORKER_PORT="${UPSCALER_WORKER_PORT:-8791}"
LOG="${TMPDIR:-/tmp}/upscaler-gui.log"

start_one() {
    local name="$1" port="$2"; shift 2
    if curl -s --max-time 2 -o /dev/null "http://127.0.0.1:${port}/"; then
        echo "  ${name} is already up on ${port}"
        return 0
    fi
    ( cd "$HERE" && nohup "$@" >> "$LOG" 2>&1 & )
    for _ in $(seq 1 40); do
        sleep 0.5
        if curl -s --max-time 2 -o /dev/null "http://127.0.0.1:${port}/"; then
            echo "  ${name} is up on ${port}"
            return 0
        fi
    done
    echo "  ${name} did NOT come up on ${port} - look at $LOG"
    return 1
}

case "${1:-start}" in
stop)
    pkill -f "server.py --port ${PORT}" 2>/dev/null
    pkill -f "PORT=${WORKER_PORT} server.py" 2>/dev/null
    pkill -f "server.py --port ${WORKER_PORT}" 2>/dev/null
    echo "stopped"
    ;;
status)
    curl -s --max-time 2 -o /dev/null -w '  GUI    http://127.0.0.1:%{http_code}/\n' \
        "http://127.0.0.1:${PORT}/api/models"
    curl -s --max-time 2 -o /dev/null -w '  worker http://127.0.0.1:%{http_code}/ping\n' \
        "http://127.0.0.1:${WORKER_PORT}/ping"
    ;;
*)
    echo "starting the upscaler..."
    start_one "the GUI    " "${PORT}" python3 "$HERE/server.py" --port "${PORT}"
    start_one "the worker " "${WORKER_PORT}" env \
        "UPSCALER_BIN=$HERE/vendor/upscaler/bin/mac-amd64" \
        "UPSCALER_MODELS=$HERE/vendor/upscaler/models" \
        "PORT=${WORKER_PORT}" python3 "$HERE/worker/server.py"
    echo
    echo "  open  http://127.0.0.1:${PORT}/"
    echo "  stop  $HERE/start.sh stop"
    ;;
esac