#!/usr/bin/env bash
set -euxo pipefail

ENVFILE=/etc/default/syncsonic
[ -r "$ENVFILE" ] && source "$ENVFILE" || true

source /home/syncsonic/SyncSonicPi/backend/.venv/bin/activate
cd /home/syncsonic/SyncSonicPi/backend
export SYNCSONIC_ACTUATION_BACKEND="${SYNCSONIC_ACTUATION_BACKEND:-pipewire-node}"
export SYNCSONIC_AUDIO_RUNTIME="${SYNCSONIC_AUDIO_RUNTIME:-pipewire-headless}"

if [ -f tools/pw_delay_filter.c ] && { [ ! -x tools/pw_delay_filter ] || [ tools/pw_delay_filter.c -nt tools/pw_delay_filter ]; }; then
  # Slice 2: -pthread is required for the new control-thread / Unix-socket
  # surface in pw_delay_filter.c. Keep the rest of the invocation
  # identical to the pre-Slice-2 build line.
  gcc -O2 -Wall -Wextra -pthread -o tools/pw_delay_filter tools/pw_delay_filter.c $(/usr/bin/pkg-config --cflags --libs libpipewire-0.3)
fi

cleanup() {
  /usr/bin/pkill -u syncsonic -f /home/syncsonic/SyncSonicPi/backend/tools/pw_delay_filter 2>/dev/null || true
  if [ -n "${SYNCSONIC_MIC_CAPTURE_PID:-}" ]; then kill "$SYNCSONIC_MIC_CAPTURE_PID" 2>/dev/null || true; fi
  if [ -n "${SYNCSONIC_ACTUATION_DAEMON_PID:-}" ]; then kill "$SYNCSONIC_ACTUATION_DAEMON_PID" 2>/dev/null || true; fi
  if [ -n "${SYNCSONIC_PIPEWIRE_PULSE_PID:-}" ]; then kill "$SYNCSONIC_PIPEWIRE_PULSE_PID" 2>/dev/null || true; fi
  if [ -n "${SYNCSONIC_WIREPLUMBER_PID:-}" ]; then kill "$SYNCSONIC_WIREPLUMBER_PID" 2>/dev/null || true; fi
  if [ -n "${SYNCSONIC_PIPEWIRE_PID:-}" ]; then kill "$SYNCSONIC_PIPEWIRE_PID" 2>/dev/null || true; fi
  if [ -n "${SYNCSONIC_SESSION_DBUS_PID:-}" ]; then kill "$SYNCSONIC_SESSION_DBUS_PID" 2>/dev/null || true; fi
}

if [ "$SYNCSONIC_AUDIO_RUNTIME" = "pipewire-headless" ]; then
  export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/syncsonic}"
  export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"
  /usr/bin/pkill -u syncsonic -f /home/syncsonic/SyncSonicPi/backend/tools/pw_delay_filter 2>/dev/null || true
  rm -rf /tmp/syncsonic_pipewire
  mkdir -p "$XDG_RUNTIME_DIR/pulse"
  rm -f "$XDG_RUNTIME_DIR/bus" "$XDG_RUNTIME_DIR/pipewire-0" "$XDG_RUNTIME_DIR/pulse/native"

  /usr/bin/dbus-daemon --session --address="$DBUS_SESSION_BUS_ADDRESS" --fork --nopidfile --print-pid >"$XDG_RUNTIME_DIR/dbus-session.pid"
  SYNCSONIC_SESSION_DBUS_PID="$(tr -d '\n' < "$XDG_RUNTIME_DIR/dbus-session.pid")"

  /usr/bin/pipewire &
  SYNCSONIC_PIPEWIRE_PID=$!

  /usr/bin/wireplumber &
  SYNCSONIC_WIREPLUMBER_PID=$!

  /usr/bin/pipewire-pulse &
  SYNCSONIC_PIPEWIRE_PULSE_PID=$!

  python3 -u -m syncsonic_ble.helpers.pipewire_actuation_daemon &
  SYNCSONIC_ACTUATION_DAEMON_PID=$!

  # Slice 1 mic capture: continuous USB-mic recording into tmpfs
  # rolling segments (no SD card wear). Independent process so a crash
  # here cannot affect audio playback or the BLE control plane.
  mkdir -p "$XDG_RUNTIME_DIR/mic"
  python3 -u -m measurement.mic_capture &
  SYNCSONIC_MIC_CAPTURE_PID=$!

  trap cleanup EXIT

  for _ in $(seq 1 20); do
    [ -S "$XDG_RUNTIME_DIR/pulse/native" ] && break
    sleep 0.25
  done
elif [ "$SYNCSONIC_AUDIO_RUNTIME" = "pulseaudio-headless" ]; then
  /usr/bin/pulseaudio --daemonize=yes --exit-idle-time=-1 --log-target=journal -n -F /home/syncsonic/SyncSonicPi/backend/pulse-headless.pa
fi

python3 -u -m syncsonic_ble.main
EXIT_CODE=$?
cleanup
exit "$EXIT_CODE"
