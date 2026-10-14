#!/bin/sh
# Keep the audio bridge running across Python crashes.
# The first invocation returns immediately so NAOqi's [program] autoload
# does not wait on the restart loop.

BRIDGE_HOME=${NAO_AUDIO_BRIDGE_HOME:-/home/nao/nao-audio-bridge}
cd "$BRIDGE_HOME" || exit 1

PIDFILE=$BRIDGE_HOME/run_forever.pid
LOGFILE=$BRIDGE_HOME/run_forever.log

# PIDs for the restart loop and the interpreter.
#
# `ps` on the NAO prints the short command name (`python`, `sh`). The script
# name is only in /proc/PID/cmdline, which is NUL-separated and includes
# arguments. Matching `ps` killed the shell and left Python holding port 8765.
bridge_pids() {
  for dir in /proc/[0-9]*; do
    pid=${dir#/proc/}
    [ -r "$dir/cmdline" ] || continue
    cmd=$(tr '\0' ' ' < "$dir/cmdline" 2>/dev/null) || continue
    case "$cmd" in
      *audio_bridge.py*)
        printf '%s\n' "$pid"
        ;;
      *run_forever.sh*)
        # This stop invocation and its subshells contain " stop".
        case "$cmd" in
          *" stop"*) ;;
          *) printf '%s\n' "$pid" ;;
        esac
        ;;
    esac
  done
}

signal_bridge() {
  sig=$1
  bridge_pids | while read -r pid; do
    [ -n "$pid" ] || continue
    kill "$sig" "$pid" 2>/dev/null || true
  done
}

bridge_is_running() {
  bridge_pids | grep -q .
}

if [ "${1:-}" = "stop" ]; then
  # TERM the loop and the interpreter together so the loop cannot respawn
  # Python. A clean Python exit can take a couple of seconds (it unsubscribes
  # the microphone). Anything still listening after that is killed, and stop
  # does not return while a bridge process remains.
  signal_bridge -TERM
  i=0
  while bridge_is_running; do
    if [ "$i" -ge 4 ]; then
      break
    fi
    sleep 1
    i=$((i + 1))
  done
  if bridge_is_running; then
    signal_bridge -KILL
    i=0
    while bridge_is_running; do
      if [ "$i" -ge 3 ]; then
        echo "Audio bridge is still running" >&2
        exit 1
      fi
      sleep 1
      i=$((i + 1))
    done
  fi
  rm -f "$PIDFILE"
  exit 0
fi

if [ -z "${NAO_AUDIO_BRIDGE_CHILD:-}" ]; then
  if [ -f "$PIDFILE" ]; then
    old=$(cat "$PIDFILE")
    if kill -0 "$old" 2>/dev/null; then
      echo "Audio bridge restart loop already running ($old)."
      exit 0
    fi
  fi
  NAO_AUDIO_BRIDGE_CHILD=1 nohup "$0" >>"$LOGFILE" 2>&1 &
  echo $! > "$PIDFILE"
  exit 0
fi

echo $$ > "$PIDFILE"

# Autoload does not source a login shell, so qi is invisible unless these
# paths are set. An interactive ssh session usually has them already.
add_ld() {
  if [ -d "$1" ]; then
    case ":${LD_LIBRARY_PATH:-}:" in
      *":$1:"*) ;;
      *) LD_LIBRARY_PATH="$1${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" ;;
    esac
  fi
}
add_py() {
  if [ -d "$1/qi" ] || [ -f "$1/qi.so" ]; then
    case ":${PYTHONPATH:-}:" in
      *":$1:"*) ;;
      *) PYTHONPATH="$1${PYTHONPATH:+:$PYTHONPATH}" ;;
    esac
  fi
}
add_ld /opt/aldebaran/lib
add_ld /usr/lib
export LD_LIBRARY_PATH
add_py /opt/aldebaran/lib/python2.7/site-packages
add_py /usr/lib/python2.7/site-packages
export PYTHONPATH

PYTHON_BIN=""
for candidate in /usr/bin/python2.7 /usr/bin/python2 python2 python; do
  if [ -x "$candidate" ] || command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" -c "import qi" >/dev/null 2>&1; then
      PYTHON_BIN=$candidate
      break
    fi
  fi
done
if [ -z "$PYTHON_BIN" ]; then
  echo "No interpreter could import qi. LD_LIBRARY_PATH=${LD_LIBRARY_PATH:-} PYTHONPATH=${PYTHONPATH:-}" >&2
  if [ -x /usr/bin/python2.7 ]; then
    PYTHON_BIN=/usr/bin/python2.7
  else
    PYTHON_BIN=python
  fi
  "$PYTHON_BIN" -c "import qi" >&2 || true
fi
echo "Audio bridge Python: $PYTHON_BIN"
"$PYTHON_BIN" -c "import sys; print(sys.executable); print(sys.version)" || true

if [ -f ./bridge.env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./bridge.env
  set +a
fi

# Forward stop to the interpreter. Killing only this shell orphans Python,
# which keeps the listen socket.
child=0
stop_child() {
  if [ "$child" -ne 0 ]; then
    kill -TERM "$child" 2>/dev/null || true
  fi
  child=0
  exit 0
}
trap stop_child TERM INT

while true; do
  # One wait only: this sleep. In-process backoff lives in audio_bridge.main.
  if [ -n "${NAO_BRIDGE_TOKEN:-}" ]; then
    "$PYTHON_BIN" audio_bridge.py --token "$NAO_BRIDGE_TOKEN" &
  else
    "$PYTHON_BIN" audio_bridge.py &
  fi
  child=$!
  wait "$child"
  status=$?
  child=0
  echo "audio bridge exited ($status), restarting in 2s"
  sleep 2
done
