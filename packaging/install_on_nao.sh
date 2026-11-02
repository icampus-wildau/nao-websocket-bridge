#!/bin/sh
# Copy the audio bridge onto a NAO and register it with NAOqi autoload.
# Usage: ./install_on_nao.sh nao@ROBOT_IP
#
# The protocol package is copied from a local checkout when one is already
# next to this repository (../nao-websocket-bridge-protocol). Otherwise the
# script clones https://github.com/icampus-wildau/nao-websocket-bridge-protocol
# and copies the package from that clone. Set PROTOCOL_SRC to a checkout
# root to use a different local copy.
#
# The password is requested once. Later ssh and scp calls reuse that login
# through an SSH control socket, which is closed when this script exits.

set -eu

PROTOCOL_REPO=https://github.com/icampus-wildau/nao-websocket-bridge-protocol.git

if [ $# -ne 1 ]; then
  echo "usage: install_on_nao.sh nao@ROBOT_IP" >&2
  exit 1
fi

ROBOT=$1

trim_field() {
  field=$(printf '%s' "$1" | tr -d '\r')
  field=${field#"${field%%[![:space:]]*}"}
  field=${field%"${field##*[![:space:]]}"}
  printf '%s' "$field"
}

ssh_name_ok() {
  case "$1" in
    ""|-*)
      return 1
      ;;
    *[!A-Za-z0-9._-]*)
      return 1
      ;;
  esac
}

raw=$(trim_field "$ROBOT")
case "$raw" in
  *@*)
    login=$(trim_field "${raw%@*}")
    host=$(trim_field "${raw##*@}")
    ;;
  *)
    login=nao
    host=$raw
    ;;
esac
case "$host" in
  *" ("*")")
    host=$(trim_field "${host%% (*}")
    ;;
esac
if ! ssh_name_ok "$login" || ! ssh_name_ok "$host"; then
  echo "Cannot connect to '$raw'." >&2
  echo "ssh rejects that address (hostname contains invalid characters)." >&2
  echo "Enter the robot IP on its own, for example 192.168.2.57." >&2
  exit 1
fi
ROBOT=$login@$host

ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
REMOTE_DIR=/home/nao/nao-audio-bridge
PKG_DIR=/home/nao/.local/share/PackageManager/apps/nao-audio-bridge

CONTROL_DIR=$(mktemp -d)
CONTROL_PATH=$CONTROL_DIR/ssh.sock

# Print the nao_websocket_bridge_protocol directory to copy onto the robot.
resolve_protocol() {
  src=${PROTOCOL_SRC:-}
  if [ -n "$src" ]; then
    if [ -f "$src/nao_websocket_bridge_protocol/__init__.py" ]; then
      echo "Using protocol checkout from PROTOCOL_SRC: $src" >&2
      CDPATH= cd -- "$src/nao_websocket_bridge_protocol" && pwd
      return
    fi
    echo "PROTOCOL_SRC is not a nao-websocket-bridge-protocol checkout: $src" >&2
    return 1
  fi

  sibling=$ROOT/../nao-websocket-bridge-protocol
  if [ -f "$sibling/nao_websocket_bridge_protocol/__init__.py" ]; then
    echo "Using local protocol checkout: $sibling" >&2
    CDPATH= cd -- "$sibling/nao_websocket_bridge_protocol" && pwd
    return
  fi

  version=$(awk '
    $0 ~ /^name = "nao-websocket-bridge-protocol"/ { found = 1 }
    found && $0 ~ /^version = / {
      gsub(/"/, "", $3)
      print $3
      exit
    }
  ' "$ROOT/uv.lock")
  if [ -z "$version" ]; then
    echo "The bridge release does not name a protocol version." >&2
    return 1
  fi
  echo "Downloading nao-websocket-bridge-protocol $version from PyPI" >&2
  py=python3
  command -v python3 >/dev/null 2>&1 || py=python
  "$py" - "$version" "$CONTROL_DIR" <<'PY'
import json, sys, tarfile, os
try:
    from urllib.request import urlopen
except ImportError:
    from urllib2 import urlopen
version, dest = sys.argv[1], sys.argv[2]
url = "https://pypi.org/pypi/nao-websocket-bridge-protocol/%s/json" % version
try:
    payload = json.load(urlopen(url))
except Exception as exc:
    sys.stderr.write("Protokollpaket %s ist auf PyPI nicht verfuegbar (%s).\n" % (version, exc))
    sys.exit(1)
sdists = [item["url"] for item in payload["urls"] if item["packagetype"] == "sdist"]
if not sdists:
    sys.stderr.write("Protokollpaket %s hat kein Quellarchiv.\n" % version)
    sys.exit(1)
archive_path = os.path.join(dest, "protocol.tar.gz")
handle = open(archive_path, "wb")
try:
    handle.write(urlopen(sdists[0]).read())
finally:
    handle.close()
archive = tarfile.open(archive_path)
try:
    archive.extractall(dest)
finally:
    archive.close()
for root, dirs, files in os.walk(dest):
    package = os.path.join(root, "nao_websocket_bridge_protocol", "__init__.py")
    if os.path.isfile(package):
        print(os.path.join(root, "nao_websocket_bridge_protocol"))
        sys.exit(0)
sys.stderr.write("The protocol archive has no package.\n")
sys.exit(1)
PY
}

ssh_base() {
  if [ -n "${NAO_KNOWN_HOSTS:-}" ]; then
    printf '%s\n' -o "UserKnownHostsFile=$NAO_KNOWN_HOSTS" -o StrictHostKeyChecking=yes
  fi
  printf '%s\n' -o ControlMaster=auto -o "ControlPath=$CONTROL_PATH" -o ControlPersist=120
}

ssh_nao() {
  # shellcheck disable=SC2046
  ssh $(ssh_base) "$@"
}

scp_nao() {
  # shellcheck disable=SC2046
  scp -O $(ssh_base) "$@"
}

cleanup() {
  ssh -o ControlPath="$CONTROL_PATH" -O exit "$ROBOT" >/dev/null 2>&1 || true
  rm -rf "$CONTROL_DIR"
}
trap cleanup EXIT
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM

PROTOCOL=$(resolve_protocol)

STAGE=$REMOTE_DIR.stage
echo "Installing audio bridge on $ROBOT"
if [ -z "${SSH_ASKPASS:-}" ]; then
  echo "Enter the SSH password once. The rest of the install reuses that login."
fi
ssh_nao "$ROBOT" "rm -rf '$STAGE' && mkdir -p '$STAGE' /home/nao/naoqi/preferences"
scp_nao "$ROOT/audio_bridge.py" "$ROBOT:$STAGE/audio_bridge.py"
scp_nao -r "$PROTOCOL" "$ROBOT:$STAGE/"
scp_nao -r "$ROOT/SimpleWebSocketServer" "$ROBOT:$STAGE/"
scp_nao "$ROOT/packaging/run_forever.sh" "$ROBOT:$STAGE/run_forever.sh"
scp_nao "$ROOT/packaging/register_autoload.py" "$ROBOT:$STAGE/register_autoload.py"
scp_nao "$ROOT/packaging/replace_tree.sh" "$ROBOT:$STAGE/replace_tree.sh"
if [ -n "${NAO_PROGRAM_VERSION:-}" ]; then
  printf '%s\n' "$NAO_PROGRAM_VERSION" > "$CONTROL_DIR/VERSION"
  scp_nao "$CONTROL_DIR/VERSION" "$ROBOT:$STAGE/VERSION"
fi
ssh_nao "$ROBOT" "chmod 755 '$STAGE/run_forever.sh' '$STAGE/audio_bridge.py' '$STAGE/replace_tree.sh'"
# A copied manifest is not an installed package, so remove the earlier attempt.
ssh_nao "$ROBOT" "rm -rf '$PKG_DIR'"
ssh_nao "$ROBOT" "NAO_CANON='$REMOTE_DIR' NAO_STAGE='$STAGE' NAO_MARKER='audio_bridge.py' NAO_HOME_TOKEN='nao-audio-bridge' NAO_KEEP='bridge.env' NAO_REGISTER=1 '$STAGE/replace_tree.sh'"

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
