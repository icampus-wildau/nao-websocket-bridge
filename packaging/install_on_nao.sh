#!/bin/sh
# Copy the audio bridge onto a NAO and register it with NAOqi autoload.
# Usage: ./install_on_nao.sh nao@ROBOT_IP
#
# The password is requested once. Later ssh and scp calls reuse that login
# through an SSH control socket, which is closed when this script exits.

set -eu

if [ $# -ne 1 ]; then
  echo "usage: install_on_nao.sh nao@ROBOT_IP" >&2
  exit 1
fi

ROBOT=$1
ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
PROTOCOL=$(CDPATH= cd -- "$ROOT/../nao_bridge_protocol/nao_bridge_protocol" && pwd)
REMOTE_DIR=/home/nao/nao-audio-bridge
PKG_DIR=/home/nao/.local/share/PackageManager/apps/nao-audio-bridge

CONTROL_DIR=$(mktemp -d)
CONTROL_PATH=$CONTROL_DIR/ssh.sock

ssh_nao() {
  ssh -o ControlMaster=auto -o ControlPath="$CONTROL_PATH" -o ControlPersist=120 "$@"
}

scp_nao() {
  scp -O -o ControlMaster=auto -o ControlPath="$CONTROL_PATH" -o ControlPersist=120 "$@"
}

cleanup() {
  ssh -o ControlPath="$CONTROL_PATH" -O exit "$ROBOT" >/dev/null 2>&1 || true
  rm -rf "$CONTROL_DIR"
}
trap cleanup EXIT
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM

echo "Installing audio bridge on $ROBOT"
echo "Enter the SSH password once. The rest of the install reuses that login."
ssh_nao "$ROBOT" "mkdir -p '$REMOTE_DIR' /home/nao/naoqi/preferences"
scp_nao "$ROOT/audio_bridge.py" "$ROBOT:$REMOTE_DIR/audio_bridge.py"
scp_nao -r "$PROTOCOL" "$ROBOT:$REMOTE_DIR/"
scp_nao -r "$ROOT/SimpleWebSocketServer" "$ROBOT:$REMOTE_DIR/"
scp_nao "$ROOT/packaging/run_forever.sh" "$ROBOT:$REMOTE_DIR/run_forever.sh"
scp_nao "$ROOT/packaging/register_autoload.py" "$ROBOT:$REMOTE_DIR/register_autoload.py"
ssh_nao "$ROBOT" "chmod 755 '$REMOTE_DIR/run_forever.sh' '$REMOTE_DIR/audio_bridge.py'"
# A copied manifest is not an installed package, so remove the earlier attempt.
ssh_nao "$ROBOT" "rm -rf '$PKG_DIR'"
ssh_nao "$ROBOT" "python '$REMOTE_DIR/register_autoload.py'"
ssh_nao "$ROBOT" "'$REMOTE_DIR/run_forever.sh' stop"
ssh_nao "$ROBOT" "'$REMOTE_DIR/run_forever.sh'"

cat <<EOF
Installed on $ROBOT.

NAOqi will start the bridge from [program] in
/home/nao/naoqi/preferences/autoload.ini the next time it boots.
The bridge was also started now.

Log:
  ssh $ROBOT tail -f $REMOTE_DIR/audio_bridge.log

Stop:
  ssh $ROBOT $REMOTE_DIR/run_forever.sh stop
EOF
