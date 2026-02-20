# Viewing sync debug output from your computer

After a sync runs on the Pi, debug files are written to a folder on the Pi. You can pull them to your PC to inspect.

## Where the files are on the Pi

- **Path:** `/tmp/syncsonic_debug/` (Linux/Pi; `tempfile.gettempdir()` is usually `/tmp`)
- **Files:**
  - `last_recording.wav` – raw mic recording used for that sync
  - `spectrogram.png` – spectrogram with vertical lines at detected peak times (t1, t2)
  - `last_sync_meta.txt` – t1, t2, peak_spacing_sec, send_spacing_sec, delta_ms, mac_a, mac_b

## Using SFTP (same way you push code)

1. In your SFTP client, connect to the Pi (same host/user you use for code).
2. Go to **remote** path: `/tmp/syncsonic_debug`
3. Download the files to a folder on your computer:
   - `last_recording.wav` → open in any audio app (Audacity, VLC, etc.)
   - `spectrogram.png` → open in any image viewer
   - `last_sync_meta.txt` → open in any text editor

The folder is created the first time a sync runs; if the folder is empty or missing, run a sync once from the Pi (e.g. trigger from the app or `sync-once`), then refresh the SFTP view and download.

## Using command line (scp)

From your **Windows** machine (PowerShell or similar), with the Pi hostname/user you use for SSH:

```bash
scp syncsonic@raspberrypi4:/tmp/syncsonic_debug/spectrogram.png .
scp syncsonic@raspberrypi4:/tmp/syncsonic_debug/last_recording.wav .
scp syncsonic@raspberrypi4:/tmp/syncsonic_debug/last_sync_meta.txt .
```

Or pull the whole folder (if your scp supports it):

```bash
scp -r syncsonic@raspberrypi4:/tmp/syncsonic_debug ./syncsonic_debug
```

Replace `syncsonic@raspberrypi4` with your actual user@host.

## One-liner after each sync

After you run a sync on the Pi, from your PC you can run:

```bash
scp syncsonic@raspberrypi4:/tmp/syncsonic_debug/*.png syncsonic@raspberrypi4:/tmp/syncsonic_debug/*.wav syncsonic@raspberrypi4:/tmp/syncsonic_debug/*.txt .
```

(or use your SFTP client’s “download folder” once you’re in `/tmp/syncsonic_debug`).
