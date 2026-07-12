import os
import tempfile
import unittest

import numpy as np
import pyloudnorm as pyln
import soundfile as sf
from mutagen.wave import WAVE

from spotify_recorder_services import (
    analyze_audio,
    process_and_save_candidates,
)


SAMPLE_RATE = 44100


def stereo_sine(level_dbfs, duration=1.0, frequency=997.0):
    frames = int(SAMPLE_RATE * duration)
    time = np.arange(frames, dtype=np.float64) / SAMPLE_RATE
    amplitude = 10.0 ** (level_dbfs / 20.0)
    mono = amplitude * np.sin(2.0 * np.pi * frequency * time)
    return np.column_stack((mono, mono)).astype(np.float32)


class AudioAnalysisTests(unittest.TestCase):
    def test_reports_sample_peak_and_true_peak(self):
        audio = stereo_sine(-1.0)
        result = analyze_audio(audio, SAMPLE_RATE)

        self.assertAlmostEqual(result["sample_peak_dbfs"], -1.0, delta=0.02)
        self.assertAlmostEqual(result["true_peak_dbtp"], -1.0, delta=0.1)
        self.assertEqual(result["full_scale_sample_count"], 0)
        self.assertEqual(result["warnings"], [])

    def test_preserves_and_warns_about_over_full_scale_float(self):
        audio = stereo_sine(1.0)
        result = analyze_audio(audio, SAMPLE_RATE)

        self.assertGreater(result["sample_peak"], 1.0)
        self.assertGreater(result["full_scale_sample_count"], 0)
        self.assertIn("入力値が0 dBFSを超えています", result["warnings"])

    def test_rejects_non_finite_samples(self):
        for invalid in (np.nan, np.inf, -np.inf):
            audio = np.zeros((SAMPLE_RATE, 2), dtype=np.float32)
            audio[100, 0] = invalid
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    analyze_audio(audio, SAMPLE_RATE)

    def test_integrated_lufs_is_measurement_only(self):
        rng = np.random.default_rng(20260713)
        noise = rng.normal(0.0, 0.05, (SAMPLE_RATE * 3, 2)).astype(np.float32)
        meter = pyln.Meter(SAMPLE_RATE)
        current_lufs = meter.integrated_loudness(noise)
        reference = pyln.normalize.loudness(noise, current_lufs, -14.0).astype(np.float32)

        result = analyze_audio(reference, SAMPLE_RATE)

        self.assertIsNotNone(result["integrated_lufs"])
        self.assertAlmostEqual(result["integrated_lufs"], -14.0, delta=0.2)


class FloatWavSaveTests(unittest.TestCase):
    def save_candidate(self, audio, directory, legacy_options=None):
        analysis = analyze_audio(audio, SAMPLE_RATE)
        candidate = {
            "selected": True,
            "track": {
                "artist": "Test Artist",
                "name": "Unity Gain Test",
                "album": "Quality Tests",
                "artwork_url": None,
            },
            "segment_audio": audio,
            "trim_start": 0,
            "trim_end": len(audio),
            "analysis": analysis,
        }
        options = {
            "save_dir": directory,
            "sample_rate": SAMPLE_RATE,
            "normalize": True,
            "target_format": "M4A",
        }
        if legacy_options:
            options.update(legacy_options)

        logs = []
        finished = []
        process_and_save_candidates(
            [candidate],
            options,
            logs.append,
            lambda: finished.append(True),
        )
        files = [name for name in os.listdir(directory) if name.endswith(".wav")]
        self.assertEqual(len(files), 1, logs)
        self.assertEqual(finished, [True])
        return os.path.join(directory, files[0]), logs

    def test_writes_float_wav_without_gain_or_clipping(self):
        audio = stereo_sine(-6.0)
        audio[100, 0] = 1.25
        audio[101, 1] = -1.125

        with tempfile.TemporaryDirectory() as directory:
            path, logs = self.save_candidate(audio, directory)
            restored, sample_rate = sf.read(path, dtype="float32", always_2d=True)
            info = sf.info(path)

            self.assertEqual(sample_rate, SAMPLE_RATE)
            self.assertEqual(info.subtype, "FLOAT")
            np.testing.assert_array_equal(restored, audio)
            self.assertTrue(any("品質警告" in line for line in logs))

    def test_embeds_quality_metadata(self):
        audio = stereo_sine(-6.0)

        with tempfile.TemporaryDirectory() as directory:
            path, _ = self.save_candidate(audio, directory)
            tags = WAVE(path).tags

            self.assertIsNotNone(tags)
            self.assertEqual(tags.get("TXXX:Capture Gain").text, ["1.0 (Unity Gain)"])
            self.assertEqual(tags.get("TXXX:WAV Encoding").text, ["32-bit IEEE float"])
            self.assertIsNotNone(tags.get("TXXX:Integrated LUFS"))
            self.assertIsNotNone(tags.get("TXXX:True Peak dBTP"))


if __name__ == "__main__":
    unittest.main()
