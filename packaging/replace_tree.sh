#!/bin/sh
# Swap a staged program tree into place and start one copy.
#
#   NAO_CANON        destination directory
#   NAO_STAGE        complete new tree
#   NAO_MARKER       interpreter token, such as audio_bridge.py
#   NAO_HOME_TOKEN   path token for this program, such as nao-audio-bridge
#   NAO_KEEP         comma-separated file names preserved from the old tree
#   NAO_LAUNCH       script inside the tree to start, usually run_forever.sh
#
# NAO_SKIP_STOP=1 skips the process scan. The install uses the scan.

set -eu

CANON=${NAO_CANON:?}
STAGE=${NAO_STAGE:?}
MARKER=${NAO_MARKER:?}
HOME_TOKEN=${NAO_HOME_TOKEN:?}
LAUNCH=${NAO_LAUNCH:-run_forever.sh}
PREV=$CANON.prev
KEEP=${NAO_KEEP:-}

if [ ! -d "$STAGE" ]; then
  echo "Staged tree is missing: $STAGE" >&2
  exit 1
fi

ours() {
  cmd=$1
  case "$cmd" in
    *"$MARKER"*)
      return 0
      ;;
  esac
  case "$cmd" in
    *run_forever.sh*)
      case "$cmd" in
        *" stop"*)
          return 1
          ;;
        *"$HOME_TOKEN"*)
          return 0
          ;;
      esac
      for word in $cmd; do
        case "$word" in
          *run_forever.sh)
            if [ -f "$word" ] && grep -q "$MARKER" "$word"; then
              return 0
            fi
            ;;
        esac
      done
      ;;
  esac
  return 1
}

signal_ours() {
  sig=$1
  for dir in /proc/[0-9]*; do
    pid=${dir#/proc/}
    [ -r "$dir/cmdline" ] || continue
    cmd=$(tr '\0' ' ' < "$dir/cmdline" 2>/dev/null) || continue
    if ours "$cmd"; then
      kill "$sig" "$pid" 2>/dev/null || true
    fi
  done
}

still_running() {
  for dir in /proc/[0-9]*; do
    [ -r "$dir/cmdline" ] || continue
    cmd=$(tr '\0' ' ' < "$dir/cmdline" 2>/dev/null) || continue
    if ours "$cmd"; then
      return 0
    fi
  done
  return 1
}

if [ "${NAO_SKIP_STOP:-}" != 1 ]; then
  signal_ours -TERM
  i=0
  while still_running; do
    if [ "$i" -ge 4 ]; then
      break
    fi
    sleep 1
    i=$((i + 1))
  done
  if still_running; then
    signal_ours -KILL
    i=0
    while still_running; do
      if [ "$i" -ge 3 ]; then
        echo "A copy of this program is still running" >&2
        rm -rf "$STAGE"
        exit 1
      fi
      sleep 1
      i=$((i + 1))
    done
  fi
fi

rollback() {
  rm -rf "$CANON"
  if [ -d "$PREV" ]; then
    mv "$PREV" "$CANON"
    if [ -x "$CANON/$LAUNCH" ]; then
      "$CANON/$LAUNCH" || true
    fi
  fi
}

rm -rf "$PREV"
if [ -d "$CANON" ]; then
  mv "$CANON" "$PREV"
fi
if ! mv "$STAGE" "$CANON"; then
  rollback
  exit 1
fi

if [ -n "$KEEP" ] && [ -d "$PREV" ]; then
  old_ifs=$IFS
  IFS=,
  for name in $KEEP; do
    if [ -f "$PREV/$name" ]; then
      if ! cp "$PREV/$name" "$CANON/$name"; then
        rollback
        exit 1
      fi
    fi
  done
  IFS=$old_ifs
fi

if [ "${NAO_REGISTER:-}" = 1 ] && [ -f "$CANON/register_autoload.py" ]; then
  python "$CANON/register_autoload.py" "${NAO_AUTOLOAD:-/home/nao/naoqi/preferences/autoload.ini}" "$CANON/$LAUNCH" || {
    rollback
    exit 1
  }
fi

if ! "$CANON/$LAUNCH"; then
  rollback
  exit 1
fi

rm -rf "$PREV"
