#!/system/bin/sh
# Start the candidate bridge server in its own directory: its own mali.sock, control.sock,
# log and budget.conf. It never touches /opt/mb-sock. Run as root on the Android host, e.g.
#   adb shell su -c 'sh /data/local/Droidspaces/Containers/archniri/rootfs/opt/mb-ctl-<runid>/server/start-independent.sh'
# Usage: start-independent.sh [DIR] [SERVER_BINARY]
set -u
D=${1:-$(cd "$(dirname "$0")/.." && pwd)}
BIN=${2:-$D/candidate/vkbridge-server}
mkdir -p "$D"
chmod 0777 "$D"
rm -f "$D/mali.sock" "$D/control.sock"
VKBRIDGE_CONTROL_CONF="$D/budget.conf" VKBRIDGE_CONTROL_UIDS="${VKBRIDGE_CONTROL_UIDS:-1000}" \
  nohup sh -c "\"$BIN\" --socket \"$D/mali.sock\" --log \"$D/server.log\"" >"$D/stdout.log" 2>&1 &
echo $! > "$D/server.pid"
sleep 1
ls -l "$D"/mali.sock "$D"/control.sock 2>/dev/null
tail -3 "$D/server.log" 2>/dev/null
