import base64
import os
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf
from mutagen.flac import FLAC

from recording_catalog import list_flac_exports, list_recordings, record_saved_recording
from spotify_recorder_services import analyze_audio, auto_export_flac, write_tpdf_flac


SAMPLE_RATE = 44100
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "/w8AAusB9Wl2nXsAAAAASUVORK5CYII="
)


def write_float_wav(path, audio):
    sf.write(path, audio, SAMPLE_RATE, format="WAV", subtype="FLOAT")


def analysis_for(audio):
    result = analyze_audio(audio, SAMPLE_RATE)
    result["duration_sec"] = len(audio) / SAMPLE_RATE
    return result


class FlacExportTests(unittest.TestCase):
    def test_writes_tpdf_pcm24_and_embeds_cover(self):
        rng = np.random.default_rng(20260718)
        audio = rng.normal(0.0, 0.1, (SAMPLE_RATE, 2)).astype(np.float32)
        with tempfile.TemporaryDirectory() as directory:
            wav_path = os.path.join(directory, "source.wav")
            flac_path = os.path.join(directory, "source.flac")
            write_float_wav(wav_path, audio)

            result = write_tpdf_flac(
                wav_path,
                flac_path,
                track_info={"name": "Track", "artist": "Artist", "album": "Album"},
                artwork_bytes=PNG_1X1,
                analysis=analysis_for(audio),
                random_seed=7,
            )

            restored, restored_rate = sf.read(
                flac_path, dtype="float64", always_2d=True
            )
            info = sf.info(flac_path)
            tags = FLAC(flac_path)

        self.assertEqual(restored_rate, SAMPLE_RATE)
        self.assertEqual(info.format, "FLAC")
        self.assertEqual(info.subtype, "PCM_24")
        self.assertLessEqual(float(np.max(np.abs(restored - audio))), 2.0 / (1 << 23))
        self.assertEqual(tags["title"], ["Track"])
        self.assertEqual(
            tags["dither"],
            ["TPDF"],
        )
        self.assertEqual(tags.pictures[0].data, PNG_1X1)
        self.assertTrue(result["artwork_embedded"])

    def test_tpdf_dither_is_present_in_quantized_silence(self):
        audio = np.zeros((SAMPLE_RATE, 2), dtype=np.float32)
        with tempfile.TemporaryDirectory() as directory:
            wav_path = os.path.join(directory, "silence.wav")
            flac_path = os.path.join(directory, "silence.flac")
            write_float_wav(wav_path, audio)
            write_tpdf_flac(wav_path, flac_path, random_seed=19)
            restored, _rate = sf.read(flac_path, dtype="float64", always_2d=True)

        quantized = np.rint(restored * (1 << 23)).astype(np.int64)
        self.assertGreater(np.count_nonzero(quantized), 0)
        self.assertLessEqual(int(np.max(np.abs(quantized))), 1)
        self.assertAlmostEqual(float(np.mean(quantized)), 0.0, delta=0.01)

    def test_auto_export_deletes_verified_wav_and_switches_history_to_flac(self):
        time = np.arange(SAMPLE_RATE, dtype=np.float64) / SAMPLE_RATE
        mono = 0.25 * np.sin(2.0 * np.pi * 997.0 * time)
        audio = np.column_stack((mono, mono)).astype(np.float32)
        analysis = analysis_for(audio)
        track = {"name": "Track", "artist": "Artist", "album": "Album"}
        with tempfile.TemporaryDirectory() as directory:
            database_path = os.path.join(directory, "recordings.sqlite3")
            wav_path = os.path.join(directory, "Artist - Track.wav")
            write_float_wav(wav_path, audio)
            record_saved_recording(
                wav_path,
                track,
                analysis,
                database_path=database_path,
            )

            result = auto_export_flac(
                wav_path,
                track_info=track,
                artwork_bytes=PNG_1X1,
                analysis=analysis,
                catalog_path=database_path,
                random_seed=11,
            )
            exports = list_flac_exports(database_path=database_path)
            recordings = list_recordings(database_path=database_path)

            self.assertEqual(result["status"], "complete")
            self.assertFalse(os.path.exists(wav_path))
            self.assertTrue(os.path.isfile(result["flac_path"]))
            self.assertEqual(exports[0]["status"], "complete")
            self.assertEqual(exports[0]["wav_deleted"], 1)
            self.assertEqual(exports[0]["artwork_embedded"], 1)
            self.assertEqual(recordings[0]["file_path"], result["flac_path"])
            self.assertTrue(recordings[0]["file_exists"])

    def test_over_full_scale_rejects_flac_and_keeps_wav(self):
        audio = np.zeros((4096, 2), dtype=np.float32)
        audio[100, 0] = 1.0001
        analysis = analysis_for(audio)
        track = {"name": "Hot", "artist": "Artist", "album": "Album"}
        with tempfile.TemporaryDirectory() as directory:
            database_path = os.path.join(directory, "recordings.sqlite3")
            wav_path = os.path.join(directory, "hot.wav")
            write_float_wav(wav_path, audio)
            record_saved_recording(
                wav_path,
                track,
                analysis,
                database_path=database_path,
            )

            result = auto_export_flac(
                wav_path,
                track_info=track,
                analysis=analysis,
                catalog_path=database_path,
            )
            exports = list_flac_exports(database_path=database_path)
            recordings = list_recordings(database_path=database_path)

            self.assertEqual(result["status"], "partial")
            self.assertTrue(os.path.isfile(wav_path))
            self.assertFalse(any(name.endswith(".flac") for name in os.listdir(directory)))
            archive = next(item for item in exports if item["export_role"] == "archive")
            dj = next(item for item in exports if item["export_role"] == "dj")
            self.assertEqual(archive["status"], "rejected")
            self.assertIn("PCM24", archive["reason"])
            self.assertEqual(dj["status"], "complete_wav_retained")
            self.assertEqual(recordings[0]["file_path"], wav_path)

    def test_catalog_finalization_failure_keeps_verified_wav(self):
        audio = np.zeros((4096, 2), dtype=np.float32)
        analysis = analysis_for(audio)
        track = {"name": "Track", "artist": "Artist", "album": "Album"}
        with tempfile.TemporaryDirectory() as directory:
            database_path = os.path.join(directory, "recordings.sqlite3")
            wav_path = os.path.join(directory, "source.wav")
            write_float_wav(wav_path, audio)
            record_saved_recording(
                wav_path,
                track,
                analysis,
                database_path=database_path,
            )
            with patch(
                "spotify_recorder_services.replace_recording_file",
                side_effect=RuntimeError("database unavailable"),
            ):
                result = auto_export_flac(
                    wav_path,
                    track_info=track,
                    analysis=analysis,
                    catalog_path=database_path,
                    random_seed=3,
                )
            exports = list_flac_exports(database_path=database_path)
            recordings = list_recordings(database_path=database_path)

            self.assertEqual(result["status"], "complete_wav_retained")
            self.assertTrue(os.path.isfile(wav_path))
            self.assertTrue(os.path.isfile(result["flac_path"]))
            self.assertEqual(exports[0]["wav_deleted"], 0)
            self.assertEqual(recordings[0]["file_path"], wav_path)


if __name__ == "__main__":
    unittest.main()
