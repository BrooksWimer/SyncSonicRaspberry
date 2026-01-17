#!/usr/bin/env python3
"""Manual latency testing script for SyncSonic"""

import sys
import os
import time
import subprocess
import json
import numpy as np
import tempfile

# Load environment variables from the service config
env_file = "/etc/default/syncsonic"
if os.path.exists(env_file):
    with open(env_file, 'r') as f:
        for line in f:
            line = line.strip()
            if line.startswith('export '):
                key, value = line[7:].split('=', 1)
                os.environ[key] = value

def get_connected_speakers():
    """Get list of currently connected speakers using PulseAudio"""
    try:
        result = subprocess.run(
            ["pactl", "list", "sinks", "short"], 
            capture_output=True, 
            text=True,
            env={"PULSE_SERVER": "unix:/run/syncsonic/pulse/native"}
        )
        
        if result.returncode != 0:
            print("Failed to list sinks")
            return []
            
        connected_macs = []
        for line in result.stdout.splitlines():
            if "bluez_sink." in line and ".a2dp_sink" in line:
                # Extract MAC from sink name like "bluez_sink.XX_XX_XX_XX_XX_XX.a2dp_sink"
                parts = line.split()
                if len(parts) >= 2:
                    sink_name = parts[1]
                    mac_part = sink_name.split('.')[1]
                    mac = mac_part.replace('_', ':')
                    connected_macs.append(mac)
                    
        return connected_macs
    except Exception as e:
        print(f"Error getting connected speakers: {e}")
        return []

def generate_test_tone(freq=1000, duration=0.5, sample_rate=44100):
    """Generate a test tone WAV file"""
    # Generate time array
    t = np.linspace(0, duration, int(sample_rate * duration))
    
    # Generate sine wave with fade in/out
    amplitude = 0.3  # Reduced volume for comfort
    tone = amplitude * np.sin(2 * np.pi * freq * t)
    
    # Apply fade in/out to avoid clicks
    fade_duration = 0.05  # 50ms fade
    fade_length = int(fade_duration * sample_rate)
    fade_in = np.linspace(0, 1, fade_length)
    fade_out = np.linspace(1, 0, fade_length)
    
    tone[:fade_length] *= fade_in
    tone[-fade_length:] *= fade_out
    
    # Convert to 16-bit PCM
    tone_int16 = (tone * 32767).astype(np.int16)
    
    # Create temporary WAV file
    temp_file = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
    
    # Write WAV file header and data
    with open(temp_file.name, 'wb') as f:
        # WAV header
        f.write(b'RIFF')
        f.write((36 + len(tone_int16) * 2).to_bytes(4, 'little'))  # File size
        f.write(b'WAVE')
        f.write(b'fmt ')
        f.write((16).to_bytes(4, 'little'))  # Chunk size
        f.write((1).to_bytes(2, 'little'))   # Audio format (PCM)
        f.write((1).to_bytes(2, 'little'))   # Channels
        f.write(sample_rate.to_bytes(4, 'little'))  # Sample rate
        f.write((sample_rate * 2).to_bytes(4, 'little'))  # Byte rate
        f.write((2).to_bytes(2, 'little'))   # Block align
        f.write((16).to_bytes(2, 'little'))  # Bits per sample
        f.write(b'data')
        f.write((len(tone_int16) * 2).to_bytes(4, 'little'))  # Data size
        f.write(tone_int16.tobytes())
    
    return temp_file.name

def test_speaker_latency(mac, freq=1000, all_macs=[]):
    """Test latency for a single speaker by muting all and playing a test tone"""
    target_sink = f"bluez_sink.{mac.replace(':', '_')}.a2dp_sink"
    sinks_to_mute = [
        f"bluez_sink.{m.replace(':', '_')}.a2dp_sink"
        for m in all_macs if m != mac
    ]
    
    print(f"\n🔊 Testing speaker {mac} with {freq}Hz tone")
    
    # Generate test tone
    tone_file = generate_test_tone(freq)

    try:
        # 1. Mute all other speakers
        for sink in sinks_to_mute:
            subprocess.run([
                "pactl", "set-sink-mute", sink, "1"
            ], env={"PULSE_SERVER": "unix:/run/syncsonic/pulse/native"})

        # 2. Unmute current speaker
        subprocess.run([
            "pactl", "set-sink-mute", target_sink, "0"
        ], env={"PULSE_SERVER": "unix:/run/syncsonic/pulse/native"})

        time.sleep(0.5)  # Let mute/unmute settle

        # 3. Play tone
        timestamp = time.time()
        print(f"  → Playing tone at {timestamp}")
        subprocess.run([
            "paplay", tone_file
        ], env={"PULSE_SERVER": "unix:/run/syncsonic/pulse/native"})

        time.sleep(0.5)

    finally:
        try:
            os.unlink(tone_file)
        except:
            pass

    return timestamp

from typing import Dict

def compute_latency_offsets_from_play_and_heard_times(
    pi_play_times: Dict[str, float],
    mic_onsets: Dict[str, float]
) -> Dict[str, int]:
    """
    Compute latency offsets for speakers based on when tones were played
    (by the Pi) and when they were detected (by the mic).

    Parameters:
    - pi_play_times: dict of {mac: play_time_unix_seconds}
    - mic_onsets: dict of {mac: heard_time_relative_to_mic_start}

    Returns:
    - dict of {mac: latency_offset_in_ms} to align all speakers
    """
    if set(pi_play_times.keys()) != set(mic_onsets.keys()):
        raise ValueError("Mismatch between speakers in play_times and mic_onsets")

    # Compute latency per speaker
    latencies = {
        mac: mic_onsets[mac] - pi_play_times[mac]
        for mac in pi_play_times
    }

    # Find the slowest speaker (max latency)
    max_latency = max(latencies.values())

    # Compute how much to delay each speaker to match the slowest one
    offsets = {
        mac: round((max_latency - latency) * 1000)
        for mac, latency in latencies.items()
    }

    return offsets

def get_actual_sink_latency(mac: str) -> float:
    """
    Returns the actual sink latency (in ms) applied by PulseAudio for a given Bluetooth MAC address.
    """
    from re import search

    target_name = f"bluez_sink.{mac.replace(':', '_')}.a2dp_sink"

    result = subprocess.run(
        ["pactl", "list", "sinks"],
        capture_output=True,
        text=True,
        env={"PULSE_SERVER": "unix:/run/syncsonic/pulse/native"}
    )

    if result.returncode != 0:
        raise RuntimeError("Failed to list PulseAudio sinks")

    lines = result.stdout.splitlines()
    in_target = False
    for line in lines:
        if line.strip().startswith("Name:") and target_name in line:
            in_target = True
        elif in_target and "Latency:" in line:
            match = search(r"Latency:\s+([\d\.]+)\s*usec", line)
            if match:
                latency_usec = float(match.group(1))
                return latency_usec / 1000.0  # convert to ms
            break
        elif in_target and line.strip() == "":
            break  # end of current sink block

    raise ValueError(f"Could not find latency for sink {target_name}")




def get_current_loopback_latencies() -> Dict[str, int]:
    """
    Returns a dictionary mapping sink MAC addresses to their currently
    configured loopback latency in milliseconds.
    """
    result = subprocess.run(
        ["pactl", "list", "modules", "short"],
        capture_output=True,
        text=True,
        env={"PULSE_SERVER": "unix:/run/syncsonic/pulse/native"}
    )

    if result.returncode != 0:
        print("Failed to get module-loopback list")
        return {}

    latencies = {}
    for line in result.stdout.splitlines():
        if "module-loopback" in line:
            parts = line.split()
            args = parts[2:]
            sink_arg = next((a for a in args if a.startswith("sink=")), None)
            latency_arg = next((a for a in args if a.startswith("latency_msec=")), None)
            if sink_arg and latency_arg:
                sink_name = sink_arg.split("=")[1]
                latency_val = int(latency_arg.split("=")[1])
                if "bluez_sink." in sink_name:
                    mac = sink_name.split("bluez_sink.")[1].split(".")[0].replace("_", ":")
                    latencies[mac] = latency_val
    return latencies

def display_final_offsets_with_current_state(offsets: Dict[str, int]):
    """
    Print final latency offsets alongside actually applied sink latency values.
    """
    print("\nCalculated Latency Offsets (ms):")
    print("=================================")
    for mac, offset in offsets.items():
        try:
            actual = get_actual_sink_latency(mac)
            current_str = f"{actual:.2f} ms"
        except Exception as e:
            current_str = f"Error: {e}"
        print(f"{mac}: {offset} ms (actually applied: {current_str})")


def main():
    try:
        connected_macs = get_connected_speakers()
        if not connected_macs:
            print("No speakers connected! Please connect some speakers first.")
            sys.exit(1)

        print(f"Found {len(connected_macs)} connected speakers:")
        for mac in connected_macs:
            print(f"  • {mac}")

        input("\nPress Enter to start latency test (Ctrl+C to cancel)...")

        timestamps = {}
        test_frequencies = [500, 1000, 1500, 2000]

        for i, mac in enumerate(connected_macs):
            freq = test_frequencies[i % len(test_frequencies)]
            timestamps[mac] = test_speaker_latency(mac, freq, all_macs=connected_macs)

        print("\nPlease enter the mic detection time (in seconds) for each speaker:")
        mic_onsets = {}
        for i, mac in enumerate(connected_macs):
            freq = test_frequencies[i % len(test_frequencies)]
            prompt = f"  • {mac} (tone {freq}Hz): "
            value = float(input(prompt))
            mic_onsets[mac] = value

        offsets = compute_latency_offsets_from_play_and_heard_times(timestamps, mic_onsets)


        display_final_offsets_with_current_state(offsets)

    except KeyboardInterrupt:
        print("\nTest cancelled by user")
        sys.exit(0)
    except Exception as e:
        print(f"Error during latency test: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
