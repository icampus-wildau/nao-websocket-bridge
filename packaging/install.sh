#!/bin/sh
# Install the audio bridge on a NAO from a laptop, without cloning this repo.
#
#   curl -LsSf https://raw.githubusercontent.com/icampus-wildau/nao-websocket-bridge/main/packaging/install.sh | sh
#
# Asks for the robot IP and the nao user's password when they are not already
# set. A password typed at the prompt is handed to ssh through SSH_ASKPASS and
# is not passed as an argument.
#
#   curl -LsSf .../install.sh | NAO_IP=192.168.2.57 sh
#   sh packaging/install.sh --ip 192.168.2.57
#   sh packaging/install.sh --ip 192.168.2.57 --user nao

set -eu

BRIDGE_REPO=https://github.com/icampus-wildau/nao-websocket-bridge
PROTOCOL_REPO=https://github.com/icampus-wildau/nao-websocket-bridge-protocol
BRANCH=main
NAO_USER=nao

usage() {
  cat <<EOF
usage: install.sh [--ip IP] [--user USER] [--password PASSWORD]

  NAO_IP        robot address, if --ip is omitted
  NAO_PASSWORD  SSH password, if --password is omitted
  NAO_USER      SSH user (default: nao)

With no IP or password, both are read from the terminal.
EOF
}

IP=${NAO_IP:-}
NAO_PASSWORD_SET=0
if [ -n "${NAO_PASSWORD+x}" ]; then
  NAO_PASSWORD_SET=1
fi

while [ $# -gt 0 ]; do
  case "$1" in
    --ip)
      IP=$2
      shift 2
      ;;
    --user)
      NAO_USER=$2
      shift 2
      ;;
    --password)
      NAO_PASSWORD=$2
      NAO_PASSWORD_SET=1
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      break
      ;;
    -*)
      echo "unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
    *@*)
      NAO_USER=${1%%@*}
      IP=${1#*@}
      shift
      ;;
    *)
      IP=$1
      shift
      ;;
  esac
done

need_tty=0
if [ -z "$IP" ] || [ "$NAO_PASSWORD_SET" -eq 0 ]; then
  need_tty=1
fi
if [ "$need_tty" -eq 1 ] && [ ! -r /dev/tty ]; then
  echo "No terminal. Set NAO_IP and NAO_PASSWORD, then run again." >&2
  exit 1
fi

if [ -z "$IP" ]; then
  printf 'NAO IP address: ' >/dev/tty
  IFS= read -r IP </dev/tty
fi
if [ -z "$IP" ]; then
  echo "NAO IP address is required." >&2
  exit 1
fi

restore_echo() {
  stty echo </dev/tty 2>/dev/null || true
}

if [ "$NAO_PASSWORD_SET" -eq 0 ]; then
  printf 'Password for %s@%s: ' "$NAO_USER" "$IP" >/dev/tty
  trap restore_echo INT TERM
  stty -echo </dev/tty
  IFS= read -r NAO_PASSWORD </dev/tty || NAO_PASSWORD=
  restore_echo
  trap - INT TERM
  printf '\n' >/dev/tty
  NAO_PASSWORD_SET=1
fi

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

WORKDIR=$(mktemp -d)
cleanup() {
  restore_echo
  rm -rf "$WORKDIR"
}
trap cleanup EXIT
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM

echo "Downloading $BRIDGE_REPO ($BRANCH)"
fetch "$BRIDGE_REPO/archive/refs/heads/$BRANCH.tar.gz" "$WORKDIR/bridge.tar.gz"
echo "Downloading $PROTOCOL_REPO ($BRANCH)"
fetch "$PROTOCOL_REPO/archive/refs/heads/$BRANCH.tar.gz" "$WORKDIR/protocol.tar.gz"

mkdir -p "$WORKDIR/src"
gzip -dc "$WORKDIR/bridge.tar.gz" | tar -xf - -C "$WORKDIR/src"
gzip -dc "$WORKDIR/protocol.tar.gz" | tar -xf - -C "$WORKDIR/src"

BRIDGE_ROOT=$WORKDIR/src/nao-websocket-bridge-$BRANCH
PROTOCOL_ROOT=$WORKDIR/src/nao-websocket-bridge-protocol-$BRANCH
if [ ! -f "$BRIDGE_ROOT/audio_bridge.py" ]; then
  echo "Download has no audio_bridge.py under $BRIDGE_ROOT" >&2
  exit 1
fi
if [ ! -f "$PROTOCOL_ROOT/nao_websocket_bridge_protocol/__init__.py" ]; then
  echo "Download has no protocol package under $PROTOCOL_ROOT" >&2
  exit 1
fi

ASKPASS=$WORKDIR/askpass
cat >"$ASKPASS" <<'EOF'
#!/bin/sh
printf '%s\n' "$NAO_PASSWORD"
EOF
chmod 700 "$ASKPASS"
export NAO_PASSWORD
export SSH_ASKPASS=$ASKPASS
export SSH_ASKPASS_REQUIRE=force
export DISPLAY="${DISPLAY:-:0}"

echo "Installing on $NAO_USER@$IP from the $BRANCH branch."
PROTOCOL_SRC=$PROTOCOL_ROOT sh "$BRIDGE_ROOT/packaging/install_on_nao.sh" "$NAO_USER@$IP"
