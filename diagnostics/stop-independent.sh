#!/system/bin/sh
# Stop the independent server started by start-independent.sh and remove its sockets.
# Usage: stop-independent.sh [DIR]
set -u
D=${1:-$(cd "$(dirname "$0")/.." && pwd)}
if [ -f "$D/server.pid" ]; then
  P=$(cat "$D/server.pid")
  kill "$P" 2>/dev/null
  sleep 1
  kill -9 "$P" 2>/dev/null
fi
for i in 1 2 3 4 5; do
  pgrep -f "$D/mali.sock" >/dev/null 2>&1 || break
  sleep 0.5
done
pgrep -af "$D/mali.sock" || echo "no server process left for $D"
rm -f "$D/mali.sock" "$D/control.sock" "$D/server.pid"
