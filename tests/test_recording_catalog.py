import os
import tempfile
import unittest

from recording_catalog import list_recordings, record_saved_recording


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


if __name__ == "__main__":
    unittest.main()
