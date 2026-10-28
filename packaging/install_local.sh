#!/bin/sh
# Install the audio bridge on this machine. Run it on the NAO, as the nao user.
# No robot IP or SSH password: the files are written into this home directory.
#
# Already logged in on the robot, with nothing cloned:
#
#   curl -LsSf https://raw.githubusercontent.com/icampus-wildau/nao-websocket-bridge/main/packaging/install_local.sh | sh
#
# Or, from a checkout of this repo on the robot:
#
#   sh packaging/install_local.sh
#
# A checkout is used as-is. Otherwise the script downloads the main branch.
# wget is used when curl is not installed. python is the NAO's Python 2.7.

set -eu

if [ "$(id -un 2>/dev/null || true)" != "nao" ]; then
  echo "Run this on the NAO as the nao user. Current user is $(id -un 2>/dev/null || echo unknown)." >&2
  exit 1
fi

BRIDGE_REPO=https://github.com/icampus-wildau/nao-websocket-bridge
PROTOCOL_REPO=https://github.com/icampus-wildau/nao-websocket-bridge-protocol
BRANCH=main

BRIDGE_HOME=${NAO_AUDIO_BRIDGE_HOME:-$HOME/nao-audio-bridge}
AUTOLOAD=$HOME/naoqi/preferences/autoload.ini
PKG_DIR=$HOME/.local/share/PackageManager/apps/nao-audio-bridge

fetch() {
  url=$1
  dest=$2
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$url" -o "$dest"
  elif command -v wget >/dev/null 2>&1; then
    wget -O "$dest" "$url"
  else
    echo "curl or wget is required to download the bridge." >&2
    return 1
  fi
}

download_main() {
  work=$1
  echo "Downloading $BRIDGE_REPO ($BRANCH)"
  fetch "$BRIDGE_REPO/archive/refs/heads/$BRANCH.tar.gz" "$work/bridge.tar.gz"
  echo "Downloading $PROTOCOL_REPO ($BRANCH)"
  fetch "$PROTOCOL_REPO/archive/refs/heads/$BRANCH.tar.gz" "$work/protocol.tar.gz"
  mkdir -p "$work/src"
  gzip -dc "$work/bridge.tar.gz" | tar -xf - -C "$work/src"
  gzip -dc "$work/protocol.tar.gz" | tar -xf - -C "$work/src"
  BRIDGE_ROOT=$work/src/nao-websocket-bridge-$BRANCH
  PROTOCOL_PKG=$work/src/nao-websocket-bridge-protocol-$BRANCH/nao_websocket_bridge_protocol
}

if [ -f "$0" ]; then
  SCRIPT_DIR=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
  MAYBE_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
  if [ -f "$MAYBE_ROOT/audio_bridge.py" ]; then
    BRIDGE_ROOT=$MAYBE_ROOT
  fi
fi

WORKDIR=
if [ -z "${BRIDGE_ROOT:-}" ]; then
  if command -v mktemp >/dev/null 2>&1; then
    WORKDIR=$(mktemp -d)
  else
    WORKDIR=/tmp/nao-audio-bridge-install.$$
    mkdir -p "$WORKDIR"
  fi
  trap 'rm -rf "$WORKDIR"' EXIT
  trap 'rm -rf "$WORKDIR"; exit 130' INT
  trap 'rm -rf "$WORKDIR"; exit 143' TERM
  download_main "$WORKDIR"
else
  PROTOCOL_PKG=
  sibling=$BRIDGE_ROOT/../nao-websocket-bridge-protocol/nao_websocket_bridge_protocol
  if [ -f "$sibling/__init__.py" ]; then
    PROTOCOL_PKG=$(CDPATH= cd -- "$sibling" && pwd)
    echo "Using local protocol checkout: $PROTOCOL_PKG"
  elif [ -n "${PROTOCOL_SRC:-}" ] && [ -f "$PROTOCOL_SRC/nao_websocket_bridge_protocol/__init__.py" ]; then
    PROTOCOL_PKG=$(CDPATH= cd -- "$PROTOCOL_SRC/nao_websocket_bridge_protocol" && pwd)
    echo "Using protocol checkout from PROTOCOL_SRC: $PROTOCOL_PKG"
  else
    if command -v mktemp >/dev/null 2>&1; then
      WORKDIR=$(mktemp -d)
    else
      WORKDIR=/tmp/nao-audio-bridge-protocol.$$
      mkdir -p "$WORKDIR"
    fi
    trap 'rm -rf "$WORKDIR"' EXIT
    trap 'rm -rf "$WORKDIR"; exit 130' INT
    trap 'rm -rf "$WORKDIR"; exit 143' TERM
    echo "Downloading $PROTOCOL_REPO ($BRANCH)"
    fetch "$PROTOCOL_REPO/archive/refs/heads/$BRANCH.tar.gz" "$WORKDIR/protocol.tar.gz"
    mkdir -p "$WORKDIR/src"
    gzip -dc "$WORKDIR/protocol.tar.gz" | tar -xf - -C "$WORKDIR/src"
    PROTOCOL_PKG=$WORKDIR/src/nao-websocket-bridge-protocol-$BRANCH/nao_websocket_bridge_protocol
  fi
fi

if [ ! -f "$BRIDGE_ROOT/audio_bridge.py" ]; then
  echo "No audio_bridge.py under $BRIDGE_ROOT" >&2
  exit 1
fi
if [ ! -f "$PROTOCOL_PKG/__init__.py" ]; then
  echo "No protocol package under $PROTOCOL_PKG" >&2
  exit 1
fi

if ! command -v python >/dev/null 2>&1; then
  echo "python is required to register the autoload entry." >&2
  exit 1
fi

echo "Installing audio bridge into $BRIDGE_HOME"
mkdir -p "$BRIDGE_HOME" "$HOME/naoqi/preferences"
cp "$BRIDGE_ROOT/audio_bridge.py" "$BRIDGE_HOME/audio_bridge.py"
rm -rf "$BRIDGE_HOME/SimpleWebSocketServer" "$BRIDGE_HOME/nao_websocket_bridge_protocol"
cp -R "$BRIDGE_ROOT/SimpleWebSocketServer" "$BRIDGE_HOME/SimpleWebSocketServer"
cp -R "$PROTOCOL_PKG" "$BRIDGE_HOME/nao_websocket_bridge_protocol"
cp "$BRIDGE_ROOT/packaging/run_forever.sh" "$BRIDGE_HOME/run_forever.sh"
cp "$BRIDGE_ROOT/packaging/register_autoload.py" "$BRIDGE_HOME/register_autoload.py"
chmod 755 "$BRIDGE_HOME/run_forever.sh" "$BRIDGE_HOME/audio_bridge.py"
rm -rf "$PKG_DIR"

python "$BRIDGE_HOME/register_autoload.py" "$AUTOLOAD" "$BRIDGE_HOME/run_forever.sh"
"$BRIDGE_HOME/run_forever.sh" stop || true
NAO_AUDIO_BRIDGE_HOME=$BRIDGE_HOME "$BRIDGE_HOME/run_forever.sh"

cat <<EOF
Installed into $BRIDGE_HOME.

NAOqi will start the bridge from [program] in
$AUTOLOAD the next time it boots.
The bridge was also started now.

Log:
  tail -f $BRIDGE_HOME/audio_bridge.log

Stop:
  $BRIDGE_HOME/run_forever.sh stop
EOF
