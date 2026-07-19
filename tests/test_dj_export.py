import os
import tempfile
import unittest

import numpy as np
import soundfile as sf
from mutagen.flac import FLAC

from recording_catalog import list_audio_exports
from spotify_recorder_services import (
    DITHER_NONE,
    DITHER_TPDF,
    _resample_to_float64_wav,
    auto_export_variants,
    write_pcm24_flac,
)


def source_audit(bit_depth):
    return {
        "provider": "qobuz",
        "source_evaluation": {
            "source_verified": True,
            "source_bit_depth": bit_depth,
            "source_sample_rate": None,
        },
    }


def sine_audio(sample_rate, amplitude=0.4, seconds=0.5):
    frames = int(sample_rate * seconds)
    time = np.arange(frames, dtype=np.float64) / sample_rate
    mono = amplitude * np.sin(2.0 * np.pi * 997.0 * time)
    return np.column_stack((mono, mono)).astype(np.float32)


class QuantizationPolicyTests(unittest.TestCase):
    def test_16_bit_extension_is_deterministic_and_has_no_dither(self):
        values = np.array(
            [-32768, -24576, -1, 0, 1, 16384, 32767],
            dtype=np.int16,
        )
        audio = np.repeat(values[:, None], 2, axis=1).astype(np.float64) / 32768.0
        with tempfile.TemporaryDirectory() as directory:
            wav_path = os.path.join(directory, "source.wav")
            first_path = os.path.join(directory, "first.flac")
            second_path = os.path.join(directory, "second.flac")
            sf.write(wav_path, audio, 48000, format="WAV", subtype="FLOAT")
            processing = {
                "export_role": "archive",
                "source_sample_rate": 48000,
                "source_bit_depth": 16,
                "source_verified": True,
                "dither_reason": "Verified 16-bit source",
            }
            write_pcm24_flac(
                wav_path,
                first_path,
                processing=processing,
                dither=DITHER_NONE,
            )
            write_pcm24_flac(
                wav_path,
                second_path,
                processing=processing,
                dither=DITHER_NONE,
            )
            first, _ = sf.read(first_path, dtype="int32", always_2d=True)
            second, _ = sf.read(second_path, dtype="int32", always_2d=True)
            tags = FLAC(first_path)

        expected = np.repeat(values[:, None], 2, axis=1).astype(np.int32) << 16
        np.testing.assert_array_equal(first, expected)
        np.testing.assert_array_equal(first, second)
        self.assertEqual(tags["dither"], ["NONE"])
        self.assertEqual(tags["quantizer"], ["ROUND_TO_NEAREST_EVEN"])

    def test_verified_16_bit_src_stays_no_dither(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = os.path.join(directory, "recordings.sqlite3")
            wav_path = os.path.join(directory, "Track.wav")
            sf.write(
                wav_path,
                sine_audio(44100),
                44100,
                format="WAV",
                subtype="FLOAT",
            )
            result = auto_export_variants(
                wav_path,
                track_info={"name": "Track", "artist": "Artist", "album": "Album"},
                capture_audit=source_audit(16),
                catalog_path=database_path,
            )
            archive_tags = FLAC(result["archive"]["flac_path"])
            dj_tags = FLAC(result["dj"]["flac_path"])
            exports = list_audio_exports(database_path=database_path)
            archive_rate = sf.info(result["archive"]["flac_path"]).samplerate
            dj_rate = sf.info(result["dj"]["flac_path"]).samplerate

        self.assertEqual(result["status"], "complete")
        self.assertTrue(result["wav_deleted"])
        self.assertEqual(archive_rate, 44100)
        self.assertEqual(dj_rate, 48000)
        self.assertEqual(archive_tags["dither"], ["NONE"])
        self.assertEqual(dj_tags["dither"], ["NONE"])
        self.assertEqual(dj_tags["src_quality"], ["SOXR_VHQ"])
        self.assertEqual(dj_tags["src_phase"], ["LINEAR"])
        self.assertEqual({item["export_role"] for item in exports}, {"archive", "dj"})

    def test_verified_24_bit_src_uses_tpdf_only_for_dsp_output(self):
        with tempfile.TemporaryDirectory() as directory:
            wav_path = os.path.join(directory, "HiRes.wav")
            sf.write(
                wav_path,
                sine_audio(96000),
                96000,
                format="WAV",
                subtype="FLOAT",
            )
            result = auto_export_variants(
                wav_path,
                track_info={"name": "HiRes", "artist": "Artist", "album": "Album"},
                capture_audit=source_audit(24),
                random_seed=17,
            )
            archive_tags = FLAC(result["archive"]["flac_path"])
            dj_tags = FLAC(result["dj"]["flac_path"])

        self.assertEqual(result["status"], "complete")
        self.assertEqual(archive_tags["dither"], [DITHER_NONE])
        self.assertEqual(dj_tags["dither"], [DITHER_TPDF])
        self.assertEqual(dj_tags["src_engine"], ["libsoxr"])
        self.assertEqual(dj_tags["src_phase"], ["LINEAR"])

    def test_verified_24_bit_48khz_bypass_has_no_dither_without_gain(self):
        with tempfile.TemporaryDirectory() as directory:
            wav_path = os.path.join(directory, "Native48.wav")
            sf.write(
                wav_path,
                sine_audio(48000, amplitude=0.4),
                48000,
                format="WAV",
                subtype="FLOAT",
            )
            result = auto_export_variants(
                wav_path,
                capture_audit=source_audit(24),
            )
            dj_tags = FLAC(result["dj"]["flac_path"])

        self.assertEqual(dj_tags["dither"], [DITHER_NONE])
        self.assertEqual(dj_tags["src_engine"], ["BYPASS"])
        self.assertEqual(float(dj_tags["dj_safety_gain_db"][0]), 0.0)

    def test_unverified_spotify_depth_never_enables_dither(self):
        with tempfile.TemporaryDirectory() as directory:
            wav_path = os.path.join(directory, "Spotify.wav")
            sf.write(
                wav_path,
                sine_audio(44100),
                44100,
                format="WAV",
                subtype="FLOAT",
            )
            result = auto_export_variants(
                wav_path,
                capture_audit={
                    "provider": "spotify",
                    "source_evaluation": {
                        "source_verified": False,
                        "source_bit_depth": None,
                    },
                },
            )
            dj_tags = FLAC(result["dj"]["flac_path"])

        self.assertEqual(dj_tags["dither"], [DITHER_NONE])
        self.assertEqual(dj_tags["source_verified"], ["NO"])

    def test_16_bit_safety_gain_remains_no_dither_and_limits_true_peak(self):
        with tempfile.TemporaryDirectory() as directory:
            wav_path = os.path.join(directory, "Hot.wav")
            sf.write(
                wav_path,
                sine_audio(48000, amplitude=0.99),
                48000,
                format="WAV",
                subtype="FLOAT",
            )
            result = auto_export_variants(
                wav_path,
                capture_audit=source_audit(16),
            )
            dj_tags = FLAC(result["dj"]["flac_path"])

        self.assertEqual(dj_tags["dither"], [DITHER_NONE])
        self.assertLess(float(dj_tags["dj_safety_gain_db"][0]), 0.0)
        self.assertLessEqual(result["dj"]["output_analysis"]["true_peak_dbtp"], -0.99)


class SoxrLinearPhaseTests(unittest.TestCase):
    def test_all_qobuz_rates_produce_native_archive_and_48khz_dj_copy(self):
        for sample_rate in (44100, 48000, 88200, 96000, 176400, 192000):
            with self.subTest(sample_rate=sample_rate), tempfile.TemporaryDirectory() as directory:
                wav_path = os.path.join(directory, f"{sample_rate}.wav")
                sf.write(
                    wav_path,
                    sine_audio(sample_rate, seconds=0.05),
                    sample_rate,
                    format="WAV",
                    subtype="FLOAT",
                )
                result = auto_export_variants(
                    wav_path,
                    capture_audit=source_audit(24),
                    random_seed=sample_rate,
                )
                archive_rate = sf.info(result["archive"]["flac_path"]).samplerate
                dj_rate = sf.info(result["dj"]["flac_path"]).samplerate

                self.assertEqual(result["status"], "complete")
                self.assertEqual(archive_rate, sample_rate)
                self.assertEqual(dj_rate, 48000)

    def test_vhq_impulse_response_is_symmetric_and_stream_is_flushed(self):
        source = np.zeros((16384, 2), dtype=np.float64)
        source[8192, :] = 1.0
        with tempfile.TemporaryDirectory() as directory:
            input_path = os.path.join(directory, "impulse.wav")
            output_path = os.path.join(directory, "resampled.wav")
            sf.write(input_path, source, 96000, format="WAV", subtype="DOUBLE")
            result = _resample_to_float64_wav(input_path, output_path, 48000)
            output, output_rate = sf.read(output_path, dtype="float64", always_2d=True)

        peak = int(np.argmax(np.abs(output[:, 0])))
        left = output[peak - 30 : peak, 0]
        right = output[peak + 1 : peak + 31, 0][::-1]
        self.assertEqual(output_rate, 48000)
        self.assertEqual(len(output), 8192)
        self.assertLess(float(np.max(np.abs(left - right))), 1e-12)
        self.assertEqual(result["src_quality"], "SOXR_VHQ")
        self.assertEqual(result["src_phase"], "LINEAR")


if __name__ == "__main__":
    unittest.main()
