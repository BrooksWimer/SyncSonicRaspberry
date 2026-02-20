#!/usr/bin/env bash
set -euo pipefail

EXPECTED_ADAPTER_COUNT="${1:-4}"     # how many HCIs you expect
HUB_PATH="1-1"                        # your USB hub’s device path (for full hub cycle)
ENVFILE="/etc/default/syncsonic"      # where we record RESERVED_HCI (systemd EnvironmentFile format)

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') - $*"; }

detect_adapters() {
  # robust: lines like "hci0:" -> "hci0"
  hciconfig 2>/dev/null | awk -F: '/^hci[0-9]+:/{print $1}'
}

get_mac() {
  hciconfig "$1" 2>/dev/null | awk '/BD Address/ { print $3; exit }'
}

get_usb_device_for_hci() {
  local hci="$1" path
  path=$(readlink -f "/sys/class/bluetooth/$hci/device" 2>/dev/null || true)
  while [[ -n "${path:-}" && ! "$(basename "$path")" =~ ^[0-9]+-[0-9]+(\.[0-9]+)*$ ]]; do
    path=$(dirname "$path")
  done
  basename "${path:-}"
}

reset_usb_device() {
  local dev="$1"
  log "Unbinding USB device $dev…"
  echo "$dev" > /sys/bus/usb/drivers/usb/unbind
  sleep 1
  log "Rebinding USB device $dev…"
  echo "$dev" > /sys/bus/usb/drivers/usb/bind
  sleep 5
}

power_cycle_entire_hub() {
  log "🔌 Power-cycling USB hub $HUB_PATH…"
  echo "$HUB_PATH" > /sys/bus/usb/drivers/usb/unbind
  sleep 3
  echo "$HUB_PATH" > /sys/bus/usb/drivers/usb/bind
  log "✅ Hub cycle done."
  sleep 8
}

rfkill_unblock_bluetooth() {
  log "🔓 rfkill unblock bluetooth…"
  /usr/sbin/rfkill unblock bluetooth || true
  sleep 1
  /usr/sbin/rfkill list bluetooth || true
}

btmgmt_power_on_all() {
  # btmgmt indices match hci numbers in most setups; we’ll try each hci we see
  local hci idx
  for hci in $(detect_adapters); do
    idx="${hci#hci}"
    log "⚡ btmgmt power on ($hci)"
    /usr/bin/btmgmt --index "$idx" power on || true
  done
  sleep 1
}

ensure_all_adapters_up() {
  local hci i
  for hci in $(detect_adapters); do
    for i in {1..5}; do
      if hciconfig "$hci" up 2>/dev/null; then
        log "✅ $hci is UP."
        break
      else
        log "⚠️ $hci up failed, retry $i."
        sleep 2
      fi
    done
  done
}

all_adapters_present_and_have_macs() {
  local hci_list=() hci mac
  mapfile -t hci_list < <(detect_adapters)

  # count check
  if (( ${#hci_list[@]} < EXPECTED_ADAPTER_COUNT )); then
    return 1
  fi

  # MAC sanity check
  for hci in "${hci_list[@]}"; do
    mac="$(get_mac "$hci")"
    if [[ -z "${mac:-}" || "$mac" == "00:00:00:00:00:00" ]]; then
      return 1
    fi
  done

  return 0
}

write_reserved_hci_env() {
  local reserved="$1"

  # systemd EnvironmentFile format must be KEY=VALUE, no "export"
  # Make it idempotent: remove existing RESERVED_HCI= lines then append one.
  sed -i '/^RESERVED_HCI=/d' "$ENVFILE" 2>/dev/null || true
  echo "RESERVED_HCI=$reserved" >> "$ENVFILE"
  log "💾 Wrote RESERVED_HCI=$reserved to $ENVFILE"
}

name_and_choose_reserved_hci() {
  local -a all_hcis=()
  local hci bus_type reserved=""
  local count=1

  mapfile -t all_hcis < <(detect_adapters)

  for hci in "${all_hcis[@]}"; do
    # Example line: "Type: Primary  Bus: UART"
    bus_type="$(hciconfig "$hci" 2>/dev/null | awk '/Bus:/ {print $5; exit}')"

    if [[ "$bus_type" == "UART" ]]; then
      reserved="$hci"
      hciconfig "$hci" name "Sync-Sonic" || true
      log "📡 Reserved $hci for phone (UART bus)."
    else
      hciconfig "$hci" name "raspberrypi-$count" || true
      log "🔊 Named $hci → raspberrypi-$count"
      count=$((count+1))
    fi
  done

  if [[ -n "$reserved" ]]; then
    write_reserved_hci_env "$reserved"
  else
    log "⚠️ No UART adapter found; RESERVED_HCI left unset."
  fi
}

### 🔁 Main loop
log "🚀 Reset starting (expected HCIs: $EXPECTED_ADAPTER_COUNT)"

while true; do
  rfkill_unblock_bluetooth

  # If adapters are soft-blocked, power them on first.
  btmgmt_power_on_all

  # If some are missing or invalid, do the heavier resets.
  if all_adapters_present_and_have_macs; then
    log "✅ Adapters present and MACs look valid."
    break
  fi

  # Determine what’s wrong
  missing=false
  invalid=()
  hci_list=()
  mapfile -t hci_list < <(detect_adapters)

  (( ${#hci_list[@]} < EXPECTED_ADAPTER_COUNT )) && missing=true

  for hci in "${hci_list[@]}"; do
    mac="$(get_mac "$hci")"
    [[ -z "${mac:-}" || "$mac" == "00:00:00:00:00:00" ]] && invalid+=("$hci")
  done

  if $missing; then
    log "⚠️ Missing adapters (${#hci_list[@]}/$EXPECTED_ADAPTER_COUNT). Hub cycle."
    power_cycle_entire_hub
    continue
  fi

  if (( ${#invalid[@]} )); then
    log "🔁 Resetting invalid adapters: ${invalid[*]}"
    for hci in "${invalid[@]}"; do
      dev="$(get_usb_device_for_hci "$hci")"
      if [[ -n "${dev:-}" ]]; then
        reset_usb_device "$dev"
      else
        log "⚠️ Could not map $hci to a USB device path; doing hub cycle."
        power_cycle_entire_hub
      fi
    done
    continue
  fi

  # fallback if we didn’t hit missing/invalid but still failed the precheck
  log "⚠️ Unknown adapter issue; doing hub cycle as fallback."
  power_cycle_entire_hub
done

log "🔌 Bringing all adapters UP…"
ensure_all_adapters_up

log "🎉 All adapters healthy."

# Name adapters + persist reserved UART HCI (systemd-friendly)
name_and_choose_reserved_hci

exit 0
