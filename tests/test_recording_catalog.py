import os
import tempfile
import unittest

from recording_catalog import (
    list_audio_exports,
    list_flac_exports,
    list_recordings,
    migrate_legacy_catalog,
    record_audio_export,
    record_flac_export,
    record_saved_recording,
    replace_recording_file,
)


class RecordingCatalogTests(unittest.TestCase):
    def test_tracks_archive_and_dj_exports_for_one_recording(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = os.path.join(directory, "recordings.sqlite3")
            wav_path = os.path.join(directory, "Track.wav")
            archive_path = os.path.join(directory, "Track.flac")
            dj_path = os.path.join(directory, "DJ 24-48", "Track.flac")
            os.makedirs(os.path.dirname(dj_path))
            for path in (wav_path, archive_path, dj_path):
                with open(path, "wb") as handle:
                    handle.write(b"fixture")
            analysis = {
                "duration_sec": 1.0,
                "sample_rate": 96000,
                "channels": 2,
                "warnings": [],
                "full_scale_ranges": [],
            }
            record_saved_recording(
                wav_path,
                {"name": "Track", "artist": "Artist", "album": "Album"},
                analysis,
                database_path=database_path,
            )
            record_audio_export(
                wav_path,
                archive_path,
                "complete_wav_retained",
                export_role="archive",
                source_sample_rate=96000,
                source_bit_depth=24,
                source_verified=True,
                output_sample_rate=96000,
                dither="NONE",
                database_path=database_path,
            )
            record_audio_export(
                wav_path,
                dj_path,
                "complete_wav_retained",
                export_role="dj",
                source_sample_rate=96000,
                source_bit_depth=24,
                source_verified=True,
                output_sample_rate=48000,
                src_engine="libsoxr",
                src_quality="SOXR_VHQ",
                src_phase="LINEAR",
                dither="TPDF",
                database_path=database_path,
            )
            exports = list_audio_exports(database_path=database_path)

        self.assertEqual({item["export_role"] for item in exports}, {"archive", "dj"})
        dj = next(item for item in exports if item["export_role"] == "dj")
        self.assertEqual(dj["output_sample_rate"], 48000)
        self.assertEqual(dj["src_quality"], "SOXR_VHQ")
        self.assertEqual(dj["src_phase"], "LINEAR")
        self.assertEqual(dj["dither"], "TPDF")

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

    def test_v3_flac_export_rows_migrate_to_archive_role(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as directory:
            database_path = os.path.join(directory, "recordings.sqlite3")
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    """
                    CREATE TABLE flac_exports (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        recording_id INTEGER,
                        source_wav_path TEXT NOT NULL UNIQUE,
                        flac_path TEXT,
                        status TEXT NOT NULL,
                        reason TEXT NOT NULL DEFAULT '',
                        bit_depth INTEGER NOT NULL DEFAULT 24,
                        dither TEXT NOT NULL DEFAULT 'TPDF',
                        sample_peak_dbfs REAL,
                        artwork_embedded INTEGER,
                        source_bytes INTEGER,
                        flac_bytes INTEGER,
                        wav_deleted INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO flac_exports (
                        source_wav_path, flac_path, status, bit_depth, dither,
                        wav_deleted, created_at, updated_at
                    ) VALUES (?, ?, 'complete', 24, 'TPDF', 1, ?, ?)
                    """,
                    (
                        os.path.join(directory, "old.wav"),
                        os.path.join(directory, "old.flac"),
                        "2026-01-01T00:00:00+00:00",
                        "2026-01-01T00:00:00+00:00",
                    ),
                )
            exports = list_audio_exports(database_path=database_path)

        self.assertEqual(len(exports), 1)
        self.assertEqual(exports[0]["export_role"], "archive")
        self.assertEqual(exports[0]["dither"], "TPDF")
        self.assertEqual(exports[0]["quantizer"], "LEGACY_TPDF_QUANTIZER")

    def test_tracks_flac_export_and_replaces_history_media_path(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = os.path.join(directory, "recordings.sqlite3")
            wav_path = os.path.join(directory, "Track.wav")
            flac_path = os.path.join(directory, "Track.flac")
            for path in (wav_path, flac_path):
                with open(path, "wb") as handle:
                    handle.write(b"fixture")
            analysis = {
                "duration_sec": 1.0,
                "sample_rate": 44100,
                "channels": 2,
                "integrated_lufs": -14.0,
                "sample_peak_dbfs": -1.0,
                "true_peak_dbtp": -0.8,
                "warnings": [],
                "full_scale_ranges": [],
            }
            record_saved_recording(
                wav_path,
                {"name": "Track", "artist": "Artist", "album": "Album"},
                analysis,
                database_path=database_path,
            )
            record_flac_export(
                wav_path,
                flac_path,
                "complete",
                sample_peak_dbfs=-1.0,
                artwork_embedded=True,
                source_bytes=100,
                flac_bytes=60,
                database_path=database_path,
            )
            replace_recording_file(wav_path, flac_path, database_path=database_path)
            exports = list_flac_exports("Artist", database_path=database_path)
            recordings = list_recordings(database_path=database_path)

        self.assertEqual(len(exports), 1)
        self.assertEqual(exports[0]["dither"], "TPDF")
        self.assertEqual(exports[0]["bit_depth"], 24)
        self.assertEqual(exports[0]["artwork_embedded"], 1)
        self.assertEqual(recordings[0]["file_path"], flac_path)


if __name__ == "__main__":
    unittest.main()
    record_audio_export,
