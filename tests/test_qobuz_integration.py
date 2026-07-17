import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from qobuz_integration import (
    diagnose_qobuz_integration,
    evaluate_qobuz_capture_gate,
    get_qobuz_snapshot,
    parse_qobuz_player_state,
)
from source_providers import QobuzSourceAdapter, source_is_playing


def create_qobuz_fixture(directory, completed=1, sample_rate=96000):
    player = {
        "player": {
            "currentTrack": {
                "track_id": 12345,
                "title": "Fixture Track",
                "artist": {"name": "Fixture Artist"},
                "album": {"title": "Fixture Album"},
                "duration": 123.4,
            },
            "playerState": "playing",
            "position": 12.5,
        },
        "audioOutputs": {
            "currentDeviceName": "BlackHole 2ch",
            "exclusiveMode": True,
            "volume": 100,
            "muted": False,
        },
    }
    with open(os.path.join(directory, "player-0.json"), "w", encoding="utf-8") as handle:
        json.dump(player, handle)
    database = os.path.join(directory, "qobuz.db")
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE L_Track (
                track_id INTEGER, data TEXT, status TEXT, format INTEGER,
                sampling_rate INTEGER, bit_depth INTEGER, duration REAL,
                is_completed INTEGER
            )
            """
        )
        connection.execute(
            "INSERT INTO L_Track VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                12345,
                json.dumps({"channels": 2}),
                "Import",
                7,
                sample_rate,
                24,
                123.4,
                completed,
            ),
        )


class QobuzStateTests(unittest.TestCase):
    def test_parses_player_and_track_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            create_qobuz_fixture(directory)
            snapshot = get_qobuz_snapshot(directory)

        self.assertEqual(snapshot["status"], "OK")
        self.assertEqual(snapshot["track_id"], "12345")
        self.assertEqual(snapshot["source_sample_rate"], 96000)
        self.assertEqual(snapshot["source_bit_depth"], 24)
        self.assertEqual(snapshot["format_label"], "Hi-Res 96")
        self.assertTrue(snapshot["is_completed"])
        self.assertTrue(snapshot["exclusive_mode"])
        self.assertEqual(snapshot["volume_percent"], 100.0)
        self.assertTrue(source_is_playing(snapshot))

        with tempfile.TemporaryDirectory() as directory:
            create_qobuz_fixture(directory)
            adapter_snapshot = QobuzSourceAdapter(directory).snapshot()
        self.assertEqual(adapter_snapshot["source_mode"], "offline")

    def test_parse_player_state_handles_nested_artist_and_album(self):
        result = parse_qobuz_player_state(
            {
                "current": {
                    "trackId": "99",
                    "title": "Title",
                    "artist": {"name": "Artist"},
                    "album": {"title": "Album"},
                },
                "playbackState": "paused",
                "exclusiveMode": True,
                "volume": 1.0,
            }
        )

        self.assertEqual(result["track_id"], "99")
        self.assertEqual(result["artist"], "Artist")
        self.assertEqual(result["album"], "Album")
        self.assertEqual(result["volume_percent"], 100.0)
        self.assertFalse(source_is_playing(result))

    def test_playback_automation_recognizes_qobuz_pause_and_stop(self):
        base = {"status": "OK", "provider": "qobuz"}

        self.assertTrue(source_is_playing({**base, "state": "playing"}))
        self.assertFalse(source_is_playing({**base, "state": "paused"}))
        self.assertFalse(source_is_playing({**base, "state": "stopped"}))
        self.assertFalse(source_is_playing({"status": "UNAVAILABLE", "state": "playing"}))


class QobuzGateTests(unittest.TestCase):
    def setUp(self):
        self.device = {
            "name": "BlackHole 2ch",
            "uid": "BlackHole2ch_UID",
            "nominal_sample_rate": 96000,
            "transport": "virt",
            "max_input_channels": 2,
            "is_aggregate": False,
        }
        self.snapshot = {
            "status": "OK",
            "track_id": "12345",
            "source_verified": True,
            "source_sample_rate": 96000,
            "source_bit_depth": 24,
            "source_channels": 2,
            "format_id": 7,
            "format_label": "Hi-Res 96",
            "is_completed": True,
            "volume_percent": 100.0,
            "muted": False,
            "exclusive_mode": True,
            "output_device_name": "BlackHole 2ch",
        }

    def test_accepts_complete_offline_capture_path(self):
        result = evaluate_qobuz_capture_gate(
            self.snapshot, self.device
        )

        self.assertTrue(result["conditions_pass"])
        self.assertTrue(result["source_verified"])
        self.assertIn("bit一致未証明", result["assurance_label"])

    def test_rejects_incomplete_mismatch_and_aggregate(self):
        snapshot = {
            **self.snapshot,
            "is_completed": False,
            "format_id": 5,
            "output_device_name": "Built-in Output",
        }
        device = {
            **self.device,
            "nominal_sample_rate": 44100,
            "is_aggregate": True,
        }
        result = evaluate_qobuz_capture_gate(snapshot, device)

        self.assertFalse(result["conditions_pass"])
        self.assertTrue(any("完全ダウンロード" in item for item in result["warnings"]))
        self.assertTrue(any("レート不一致" in item for item in result["warnings"]))
        self.assertTrue(any("Aggregate" in item for item in result["warnings"]))
        self.assertTrue(any("Lossless形式" in item for item in result["warnings"]))
        self.assertTrue(any("出力先" in item for item in result["warnings"]))

    def test_rejects_unverified_source_without_manual_fallback(self):
        result = evaluate_qobuz_capture_gate(
            {"source_verified": False},
            self.device,
        )

        self.assertFalse(result["conditions_pass"])
        self.assertFalse(result["source_verified"])
        self.assertEqual(result["mode"], "offline")
        self.assertTrue(any("Offlineソース" in item for item in result["warnings"]))
        self.assertTrue(any("完全ダウンロード" in item for item in result["warnings"]))

    def test_self_diagnosis_rejects_missing_app_but_validates_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            create_qobuz_fixture(directory)
            result = diagnose_qobuz_integration(
                directory, app_path=os.path.join(directory, "Missing.app")
            )

        self.assertFalse(result["available"])
        self.assertTrue(result["schema_ok"])
        self.assertTrue(any("アプリ" in item for item in result["warnings"]))

    def test_adapter_rejects_capture_when_integration_breaks(self):
        adapter = QobuzSourceAdapter()
        adapter.last_snapshot = dict(self.snapshot)
        diagnostic = {
            "available": False,
            "app_version": "future",
            "app_running": True,
            "warnings": ["Qobuz DBスキーマが未対応です"],
        }
        with patch("source_providers.diagnose_qobuz_integration", return_value=diagnostic):
            offline = adapter.preflight(self.device)

        self.assertFalse(offline["conditions_pass"])
        self.assertEqual(offline["mode"], "offline")
        self.assertIn("Offline証跡", offline["assurance_label"])

    def test_log_tailer_records_buffering_interval_after_priming(self):
        with tempfile.TemporaryDirectory() as directory:
            logs = os.path.join(directory, "logs")
            os.makedirs(logs)
            path = os.path.join(logs, "rapport_qobuz0.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("old log\n")
            adapter = QobuzSourceAdapter(directory)
            self.assertEqual(adapter.poll_events(), [])
            with open(path, "a", encoding="utf-8") as handle:
                handle.write("2026-07-14T12:00:00.000Z INFO Init buffer for track 123\n")
                handle.write("2026-07-14T12:00:01.000Z INFO Track 123 entirely buffered\n")
            events = adapter.poll_events()

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "source_buffering")
        self.assertEqual(events[0]["duration_sec"], 1.0)
        self.assertIsNotNone(events[0]["timestamp_epoch"])


if __name__ == "__main__":
    unittest.main()
