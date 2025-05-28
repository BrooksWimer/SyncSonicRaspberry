# SyncSonic BLE

_**Low-latency Bluetooth® Audio Orchestrator for Raspberry Pi**_

SyncSonic BLE is a small Python service that manages a fleet of Bluetooth audio
adapters and exposes a custom BLE interface so that a mobile phone can control
speaker selection, volume, muting and latency in real-time.  It is the glue
between the Linux audio stack (PulseAudio/ALSA/BlueZ) and a thin smartphone
application.

The project was primarily designed for Raspberry Pi-based installations where
multiple USB Bluetooth dongles are attached via a powered hub: one UART
controller (embedded or on a HAT) advertises the BLE service, while the
remaining adapters act as A2DP sinks that connect to speakers.

---

## Features

• **Custom BLE GATT service**
  – One characteristic handles all phone ↔︎ Pi messaging (binary protocol).

• **Multiple adapters**
  – Dynamically select audio endpoints and hot-swap faulty dongles.

• **Real-time audio control**
  – Set volume, latency, mute state and receive connection-status updates.

• **Self-healing**
  – Helper script (`reset_bt_adapters.sh`) that power-cycles USB ports, renames
    adapters and exports the *UART* controller via the `RESERVED_HCI` variable.

• **Pure-Python** implementation that relies only on BlueZ and GLib
  (through *dbus-python* & *PyGObject*).

---

## Directory layout

```
SyncSonicPi/
├── syncsonic_ble/          # Main Python package
│   ├── helpers/            # Low-level DBus & audio helpers
│   ├── infra/              # BLE GATT implementation & pairing agent
│   ├── state_management/   # High-level device / connection orchestration
│   ├── state_change/       # Action planners + handlers (phone requests)
│   └── utils/              # Constants & logging config
├── reset_bt_adapters.sh    # Optional startup script that prepares adapters
└── README.md              
```

---

## Installation

### 1. System packages

```
sudo apt update && sudo apt install -y \
    python3 python3-venv python3-pip \
    bluez pulseaudio pulseaudio-utils \
    libglib2.0-dev libdbus-1-dev libgirepository1.0-dev gir1.2-gtk-3.0
```

> **Note** `pulseaudio` can be substituted with `pipewire-pulse` if you prefer.

### 2. Python environment

```bash
cd /path/to/SyncSonicPi
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt   # create one or install manually:
# pip install dbus-python PyGObject
```

### 3. Reserve the advertising adapter (optional but recommended)

Run the helper script once on first boot; it will:

1. Verify that all dongles enumerate correctly.
2. Rename adapters for easier identification (`raspberrypi-1`, `-2`, …).
3. Export the *UART* controller's name (e.g. `hci0`) to
   `/etc/default/syncsonic` so that systemd services inherit
   `RESERVED_HCI`.

```bash
sudo ./reset_bt_adapters.sh         # defaults: expects 4 adapters & hub at 1-1
```

If you prefer manual configuration just export the variable yourself:

```bash
echo "export RESERVED_HCI=hci0" | sudo tee /etc/default/syncsonic
```

---

## Quick start

Activate the virtual-env (if not inside systemd) and launch the service:

```bash
source .venv/bin/activate
python -m syncsonic_ble     # or `python syncsonic_ble/main.py`
```

You should see log lines similar to:

```
INFO syncsonic_ble.main  Using primary adapter: /org/bluez/hci1
INFO syncsonic_ble.main  Pairing agent registered at /com/syncsonic/pair_agent
INFO syncsonic_ble.main  Advertisement active on adapter hci0
INFO syncsonic_ble.main  SyncSonic BLE server ready – service UUID 19b1…1214
```

The Raspberry Pi is now advertising a BLE service that your mobile app can
discover.

---

## Running with systemd

```ini
# /etc/systemd/system/syncsonic.service
[Unit]
Description=SyncSonic BLE service
After=bluetooth.target network.target sound.target

[Service]
Type=simple
EnvironmentFile=/etc/default/syncsonic   # contains RESERVED_HCI
WorkingDirectory=/home/pi/SyncSonicPi
ExecStart=/home/pi/SyncSonicPi/start_syncsonic.sh
Restart=always

[Install]
WantedBy=multi-user.target
```

Where `start_syncsonic.sh` is a tiny wrapper that activates your virtual-env
and executes `python -u -m syncsonic_ble.main` (see journal excerpt in the
issue above).

Enable & start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now syncsonic.service
```

Tail logs:

```bash
journalctl -u syncsonic.service -f
```

---

## Environment variables

| Variable         | Required | Default | Description                                       |
|------------------|:--------:|:-------:|---------------------------------------------------|
| `RESERVED_HCI`   |   ✅    | —       | Name of the adapter dedicated to BLE advertising. |
| `PYTHONUNBUFFERED`|   ⬜     | `1`     | Already set by `start_syncsonic.sh` for live logs.|

---

## BLE GATT schema

| Element            | UUID                                     | Notes                           |
|--------------------|-------------------------------------------|---------------------------------|
| Service            | `19b10000-e8f2-537e-4f6c-d104768a1214`   | Primary SyncSonic control svc    |
| Characteristic     | `19b10001-e8f2-537e-4f6c-d104768a1217`   | Supports **R/W/Notify**          |
| ClientConfigDescr. | `00002902-0000-1000-8000-00805f9b34fb`   | Standard CCCD for notifications  |

The binary protocol is defined in `syncsonic_ble/utils/constants.py#Msg`.

---

## Logging

Logs are emitted via `logging_conf.get_logger()` and by default go to
`stdout` (systemd captures them in the journal).  Adjust the configuration as
needed for file rotation or remote aggregation.

