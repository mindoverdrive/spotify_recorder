import os
import tempfile
import unittest

from recording_catalog import (
    list_recordings,
    migrate_legacy_catalog,
    record_saved_recording,
)


class RecordingCatalogTests(unittest.TestCase):
    def test_records_and_searches_saved_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = os.path.join(directory, "recordings.sqlite3")
            audio_path = os.path.join(directory, "Artist - Track.wav")
            with open(audio_path, "wb") as handle:
                handle.write(b"RIFF")

            analysis = {
                "duration_sec": 123.4,
                "sample_rate": 44100,
                "channels": 2,
                "integrated_lufs": -12.3,
                "sample_peak_dbfs": -0.5,
                "true_peak_dbtp": -0.2,
                "warnings": [],
                "full_scale_ranges": [],
                "sample_peak_time_sec": 5.0,
                "true_peak_time_sec": 5.1,
            }
            audit = {
                "quality_gate_pass": False,
                "assurance_label": "品質条件に要確認・実効Lossless未証明",
                "warnings": ["再生停止疑い"],
                "events": [
                    {
                        "type": "playback_stall",
                        "sample": 44100,
                        "time_sec": 1.0,
                        "duration_sec": 1.0,
                        "detail": "Spotify再生位置の停止疑い: 1.00秒",
                    }
                ],
            }

            record_saved_recording(
                audio_path,
                {"name": "Track", "artist": "Artist", "album": "Album"},
                analysis,
                audit,
                database_path,
            )
            rows = list_recordings("Artist", database_path=database_path)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Track")
        self.assertEqual(rows[0]["quality_gate_pass"], 0)
        self.assertTrue(rows[0]["file_exists"])
        self.assertEqual(rows[0]["suspect_events"]["capture"][0]["time_sec"], 1.0)

    def test_records_qobuz_source_evidence_and_filters_rerecords(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = os.path.join(directory, "recordings.sqlite3")
            audio_path = os.path.join(directory, "Qobuz.wav")
            with open(audio_path, "wb") as handle:
                handle.write(b"RIFF")
            analysis = {
                "duration_sec": 60.0,
                "sample_rate": 96000,
                "channels": 2,
                "integrated_lufs": -18.0,
                "sample_peak_dbfs": -1.0,
                "true_peak_dbtp": -0.8,
                "warnings": [],
                "full_scale_ranges": [],
            }
            audit = {
                "provider": "qobuz",
                "quality_gate_pass": False,
                "assurance_label": "品質条件に要確認・Qobuz bit一致未証明",
                "warnings": ["Qobuzバッファリング疑い"],
                "events": [
                    {
                        "type": "source_buffering",
                        "sample": 96000,
                        "time_sec": 1.0,
                        "duration_sec": 0.8,
                        "detail": "Qobuzバッファリング疑い: 0.80秒",
                    }
                ],
                "source_evaluation": {
                    "mode": "offline",
                    "source_verified": True,
                    "source_sample_rate": 96000,
                    "source_bit_depth": 24,
                    "source_channels": 2,
                    "evidence": {
                        "track_id": "123",
                        "format_id": 7,
                        "format_label": "Hi-Res 96",
                    },
                },
            }
            record_saved_recording(
                audio_path,
                {"name": "Track", "artist": "Artist", "album": "Album"},
                analysis,
                audit,
                database_path,
            )
            rows = list_recordings(
                database_path=database_path,
                provider="qobuz",
                requires_rerecord=True,
            )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_sample_rate"], 96000)
        self.assertEqual(rows[0]["source_bit_depth"], 24)
        self.assertEqual(rows[0]["source_track_id"], "123")
        self.assertEqual(rows[0]["requires_rerecord"], 1)

    def test_legacy_database_is_copied_without_deleting_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "old", "recordings.sqlite3")
            target = os.path.join(directory, "new", "recordings.sqlite3")
            os.makedirs(os.path.dirname(source))
            import sqlite3

            with sqlite3.connect(source) as connection:
                connection.execute("CREATE TABLE marker(value TEXT)")
                connection.execute("INSERT INTO marker VALUES('preserved')")

            migrated = migrate_legacy_catalog(source, target)
            with sqlite3.connect(target) as connection:
                value = connection.execute("SELECT value FROM marker").fetchone()[0]
            source_exists = os.path.isfile(source)

        self.assertTrue(migrated)
        self.assertEqual(value, "preserved")
        self.assertTrue(source_exists)


if __name__ == "__main__":
    unittest.main()
