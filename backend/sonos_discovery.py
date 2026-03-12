from soco.discovery import discover
import time

devices = discover(timeout=8)
speaker = next((d for d in devices if d.player_name == "Living Room"), None) or list(devices)[0]
print("Using:", speaker.player_name, speaker.ip_address)

try:
    speaker.stop()
except Exception as e:
    print("Stop failed (ok):", e)

try:
    speaker.clear_queue()
except Exception as e:
    print("Clear queue failed (ok):", e)

speaker.volume = 10

uri = "http://stream.live.vc.bbcmedia.co.uk/bbc_radio_fourlw_online_nonuk"
print("Playing:", uri)
speaker.play_uri(uri)

time.sleep(5)
speaker.stop()
print("Done")