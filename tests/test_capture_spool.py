import os
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

from capture_spool import CaptureSpool, capture_blocksize, check_capture_disk_space
from spotify_recorder_services import analyze_audio, process_and_save_candidates


class CaptureSpoolTests(unittest.TestCase):
    def test_adaptive_blocksize(self):
        self.assertEqual(capture_blocksize(44100), 2048)
        self.assertEqual(capture_blocksize(96000), 4096)
        self.assertEqual(capture_blocksize(192000), 8192)

    def test_spool_preserves_float_samples(self):
        rng = np.random.default_rng(20260714)
        first = rng.normal(0.0, 0.1, (8192, 2)).astype(np.float32)
        second = rng.normal(0.0, 0.1, (4096, 2)).astype(np.float32)
        with tempfile.TemporaryDirectory() as directory:
            spool = CaptureSpool(192000, blocksize=8192, cache_dir=directory)
            spool.start([first])
            self.assertTrue(spool.try_write(second))
            audio = spool.stop()
            restored = np.asarray(audio[:])
            np.testing.assert_array_equal(restored, np.concatenate((first, second)))
            raw_path = audio.raw_path
            audio.close(delete=True)
            self.assertFalse(os.path.exists(raw_path))

    def test_queue_overflow_is_a_hard_error(self):
        spool = CaptureSpool(44100, blocksize=44100, queue_seconds=0.01)
        spool._started = True
        block = np.zeros((44100, 2), dtype=np.float32)
        self.assertTrue(spool.try_write(block))
        self.assertTrue(spool.try_write(block))
        self.assertFalse(spool.try_write(block))
        self.assertTrue(spool.overflowed)
        self.assertIn("オーバーフロー", spool.error)

    def test_disk_preflight_accounts_for_spool_and_final_wav(self):
        with tempfile.TemporaryDirectory() as directory:
            result = check_capture_disk_space(
                directory,
                directory,
                192000,
                1,
                reserve_bytes=0,
            )
        self.assertTrue(result["ok"])
        self.assertTrue(result["same_volume"])
        self.assertEqual(result["required_bytes"], 192000 * 60 * 2 * 4 * 2)


class ChunkedAudioTests(unittest.TestCase):
    def test_all_supported_rates_round_trip_without_resampling(self):
        for sample_rate in (44100, 48000, 88200, 96000, 176400, 192000):
            with self.subTest(sample_rate=sample_rate), tempfile.TemporaryDirectory() as directory:
                frames = max(1024, sample_rate // 20)
                time = np.arange(frames, dtype=np.float64) / sample_rate
                mono = 0.25 * np.sin(2.0 * np.pi * 997.0 * time)
                audio = np.column_stack((mono, mono)).astype(np.float32)
                analysis = analyze_audio(audio, sample_rate)
                candidate = {
                    "selected": True,
                    "track": {
                        "artist": "Rates",
                        "name": str(sample_rate),
                        "album": "Tests",
                    },
                    "segment_audio": audio,
                    "trim_start": 0,
                    "trim_end": len(audio),
                    "analysis": analysis,
                }
                process_and_save_candidates(
                    [candidate],
                    {"save_dir": directory, "sample_rate": sample_rate},
                    lambda _message: None,
                    None,
                )
                path = os.path.join(directory, f"Rates - {sample_rate}.wav")
                restored, restored_rate = sf.read(
                    path, dtype="float32", always_2d=True
                )

                self.assertEqual(restored_rate, sample_rate)
                self.assertEqual(sf.info(path).subtype, "FLOAT")
                np.testing.assert_array_equal(restored, audio)

    def test_192khz_chunk_analysis_reports_unity_samples(self):
        sample_rate = 192000
        time = np.arange(sample_rate, dtype=np.float64) / sample_rate
        mono = 0.5 * np.sin(2.0 * np.pi * 997.0 * time)
        audio = np.column_stack((mono, mono)).astype(np.float32)
        result = analyze_audio(audio, sample_rate)

        self.assertAlmostEqual(result["sample_peak_dbfs"], -6.0206, delta=0.02)
        self.assertIsNotNone(result["integrated_lufs"])

    def test_rf64_branch_preserves_float_audio(self):
        audio = np.full((4410, 2), 0.125, dtype=np.float32)
        analysis = analyze_audio(audio, 44100)
        candidate = {
            "selected": True,
            "track": {"artist": "Artist", "name": "RF64", "album": "Tests"},
            "segment_audio": audio,
            "trim_start": 0,
            "trim_end": len(audio),
            "analysis": analysis,
        }
        with tempfile.TemporaryDirectory() as directory, patch(
            "spotify_recorder_services.RF64_DATA_THRESHOLD", 1
        ):
            process_and_save_candidates(
                [candidate],
                {"save_dir": directory, "sample_rate": 44100},
                lambda _message: None,
                None,
            )
            path = os.path.join(directory, "Artist - RF64.wav")
            restored, rate = sf.read(path, dtype="float32", always_2d=True)
            info = sf.info(path)

        self.assertEqual(rate, 44100)
        self.assertEqual(info.format, "RF64")
        np.testing.assert_array_equal(restored, audio)


if __name__ == "__main__":
    unittest.main()
