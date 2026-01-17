"""latency_test.py - Speaker latency testing module for SyncSonic"""
from typing import Dict, List
import subprocess
import time
import numpy as np
from scipy.io import wavfile
import tempfile
import os
from syncsonic_ble.utils.logging_conf import get_logger
from syncsonic_ble.helpers.pulseaudio_helpers import create_loopback, remove_loopback_for_device

logger = get_logger(__name__)

class LatencyTester:
    def __init__(self):
        """Initialize the latency tester"""
        self.test_frequencies = [500, 1000, 1500, 2000]  # Hz
        self.timestamps: Dict[str, float] = {}
        self._temp_files: List[str] = []
        self._generate_test_tones()
        
    def _generate_test_tones(self):
        """Generate test tone WAV files in memory"""
        sample_rate = 44100  # Hz
        duration = 0.5  # seconds
        amplitude = 0.5  # Reduced volume for comfort
        
        t = np.linspace(0, duration, int(sample_rate * duration))
        self.temp_dir = tempfile.mkdtemp()
        
        for freq in self.test_frequencies:
            # Generate sine wave
            tone = amplitude * np.sin(2 * np.pi * freq * t)
            
            # Apply fade in/out
            fade_duration = 0.05  # 50ms fade
            fade_length = int(fade_duration * sample_rate)
            fade_in = np.linspace(0, 1, fade_length)
            fade_out = np.linspace(1, 0, fade_length)
            
            tone[:fade_length] *= fade_in
            tone[-fade_length:] *= fade_out
            
            # Convert to 16-bit PCM
            tone_int16 = (tone * 32767).astype(np.int16)
            
            # Save to temporary file
            temp_path = os.path.join(self.temp_dir, f"tone_{freq}Hz.wav")
            wavfile.write(temp_path, sample_rate, tone_int16)
            self._temp_files.append(temp_path)
            
        logger.info(f"Generated {len(self.test_frequencies)} test tones")
        
    def __del__(self):
        """Cleanup temporary files"""
        for file_path in self._temp_files:
            try:
                os.remove(file_path)
            except OSError:
                pass
        try:
            os.rmdir(self.temp_dir)
        except OSError:
            pass
            
    def _set_mute(self, mac: str, mute: bool) -> bool:
        """Mute or unmute a specific speaker"""
        try:
            sink = f"bluez_sink.{mac.replace(':', '_')}.a2dp_sink"
            subprocess.run(
                ["pactl", "set-sink-mute", sink, "1" if mute else "0"],
                check=True,
                capture_output=True
            )
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to {'mute' if mute else 'unmute'} {mac}: {e}")
            return False
            
    def _play_test_tone(self, freq: int) -> bool:
        """Play a test tone file"""
        tone_file = os.path.join(self.temp_dir, f"tone_{freq}Hz.wav")
        
        try:
            subprocess.run(["paplay", tone_file], check=True)
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to play test tone {freq}Hz: {e}")
            return False
            
    def test_speaker_latencies(self, connected_macs: List[str]) -> Dict[str, float]:
        """Test latency for all connected speakers"""
        self.timestamps.clear()
        
        # Mute all speakers initially
        for mac in connected_macs:
            if not self._set_mute(mac, True):
                logger.warning(f"Could not mute {mac}, skipping from test")
                continue
                
        # Test each speaker
        for i, mac in enumerate(connected_macs):
            freq = self.test_frequencies[i % len(self.test_frequencies)]
            logger.info(f"Testing speaker {mac} with {freq}Hz tone")
            
            if not self._set_mute(mac, False):
                continue
                
            time.sleep(0.5)
            
            if self._play_test_tone(freq):
                self.timestamps[mac] = time.time()
            
            time.sleep(1.5)
            self._set_mute(mac, True)
            
        return self.timestamps

    def calculate_latencies(self) -> Dict[str, float]:
        """Calculate relative latencies between speakers"""
        if not self.timestamps:
            return {}
            
        reference_time = min(self.timestamps.values())
        return {
            mac: (ts - reference_time) * 1000  # Convert to ms
            for mac, ts in self.timestamps.items()
        }