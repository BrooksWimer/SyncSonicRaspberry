# Get `pactl list cards` output from the Pi

Since copy-paste from the terminal isn't working, use one of these:

## Option 1: Save on Pi, then SCP to your PC (recommended)

**In your existing SSH session on the Pi**, run:

```bash
pactl list cards > /tmp/pactl_cards.txt
```

Then **on your Windows machine** (PowerShell, in any folder), run:

```powershell
scp syncsonic@10.0.0.89:/tmp/pactl_cards.txt "c:\Users\wimer\Desktop\SyncSonicPi\pactl_list_cards.txt"
```

Open `pactl_list_cards.txt` in the project—you can copy from there or share it.

## Option 2: Same, but with SyncSonic Pulse server

If you need cards for the SyncSonic Pulse instance:

```bash
PULSE_SERVER=unix:/run/syncsonic/pulse/native pactl list cards > /tmp/pactl_cards.txt
```

Then SCP as above.

## Option 3: Append to a file you can open in the project

On the Pi:

```bash
pactl list cards > /tmp/pactl_cards.txt
```

Then from Windows, open the file via SFTP in Cursor/VS Code (if you have an SFTP extension and the Pi mounted), or use `scp` as in Option 1.
