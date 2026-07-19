import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from spotify_quality_audit import (
    CaptureQualityAudit,
    audit_for_audio_range,
    evaluate_spotify_quality_settings,
    parse_spotify_prefs,
    read_spotify_offline_mode,
    read_spotify_quality_settings,
)
from source_providers import SpotifySourceAdapter


GOOD_SETTINGS = {
    "available": True,
    "download_quality_raw": 5,
    "normalize": False,
    "automix": False,
    "offline_mode": {
        "available": True,
        "enabled": True,
        "evidence": "Spotify menu mark",
    },
}


class SpotifySettingsAuditTests(unittest.TestCase):
    def test_parses_and_evaluates_current_lossless_candidate_settings(self):
        prefs = parse_spotify_prefs(
            "\n".join(
                [
                    "audio.sync_bitrate_enumeration=5",
                    "audio.normalize_v2=false",
                    "audio.automix=false",
                ]
            )
        )
        settings = {
            "available": True,
            "download_quality_raw": prefs["audio.sync_bitrate_enumeration"],
            "normalize": prefs["audio.normalize_v2"],
            "automix": prefs["audio.automix"],
            "offline_mode": GOOD_SETTINGS["offline_mode"],
        }

        result = evaluate_spotify_quality_settings(settings)

        self.assertTrue(result["conditions_pass"])
        self.assertFalse(result["warnings"])
        self.assertIn("未証明", result["label"])

    def test_reads_latest_spotify_user_prefs(self):
        with tempfile.TemporaryDirectory() as directory:
            user_dir = os.path.join(directory, "Users", "test-user")
            os.makedirs(user_dir)
            with open(os.path.join(user_dir, "prefs"), "w", encoding="utf-8") as handle:
                handle.write(
                    "audio.sync_bitrate_enumeration=5\n"
                    "audio.normalize_v2=false\n"
                    "audio.automix=false\n"
                )

            settings = read_spotify_quality_settings(
                directory,
                offline_mode=GOOD_SETTINGS["offline_mode"],
            )

        self.assertEqual(settings, {**GOOD_SETTINGS, "source_note": "Spotify非公開prefsの観測値"})

    def test_offline_mode_off_fails_quality_gate(self):
        settings = {
            **GOOD_SETTINGS,
            "offline_mode": {"available": True, "enabled": False},
        }

        result = evaluate_spotify_quality_settings(settings)

        self.assertFalse(result["conditions_pass"])
        self.assertTrue(any("Offline ModeがOFF" in item for item in result["warnings"]))

    @patch("source_providers.read_spotify_quality_settings", return_value=GOOD_SETTINGS)
    def test_spotify_adapter_is_offline_only(self, _settings):
        result = SpotifySourceAdapter().preflight(
            {
                "name": "BlackHole 2ch",
                "nominal_sample_rate": 44100,
                "max_input_channels": 2,
            }
        )

        self.assertTrue(result["conditions_pass"])
        self.assertEqual(result["mode"], "offline")

    @patch("source_providers.read_spotify_quality_settings", return_value=GOOD_SETTINGS)
    def test_spotify_adapter_rejects_non_native_capture_rate(self, _settings):
        result = SpotifySourceAdapter().preflight(
            {
                "name": "BlackHole 2ch",
                "nominal_sample_rate": 48000,
                "max_input_channels": 2,
            }
        )

        self.assertFalse(result["conditions_pass"])
        self.assertTrue(any("レート不一致" in item for item in result["warnings"]))


class SpotifyOfflineModeTests(unittest.TestCase):
    @patch(
        "spotify_quality_audit.subprocess.run",
        return_value=SimpleNamespace(returncode=0, stdout="ON\n", stderr=""),
    )
    def test_reads_enabled_menu_mark(self, run):
        result = read_spotify_offline_mode()

        self.assertTrue(result["available"])
        self.assertTrue(result["enabled"])
        self.assertEqual(run.call_args.args[0][0], "/usr/bin/osascript")

    @patch(
        "spotify_quality_audit.subprocess.run",
        return_value=SimpleNamespace(returncode=1, stdout="", stderr="not authorized"),
    )
    def test_accessibility_failure_is_not_accepted(self, _run):
        result = read_spotify_offline_mode()

        self.assertFalse(result["available"])
        self.assertFalse(result["enabled"])
        self.assertIn("アクセシビリティ", result["error"])


class CaptureAuditTests(unittest.TestCase):
    def test_quality_gate_pass_does_not_claim_lossless_verification(self):
        with patch("spotify_quality_audit.time.time", side_effect=[1000.0, 1010.0]), patch(
            "spotify_quality_audit.time.monotonic", side_effect=[100.0, 110.0]
        ):
            audit = CaptureQualityAudit(44100, "Loopback", GOOD_SETTINGS)
            audit.record_audio_callback(441000)
            result = audit.finish()

        self.assertTrue(result["quality_gate_pass"])
        self.assertFalse(result["lossless_verified"])
        self.assertIn("未証明", result["assurance_label"])

    def test_audio_callback_problem_fails_quality_gate(self):
        with patch("spotify_quality_audit.time.time", side_effect=[1000.0, 1001.0]), patch(
            "spotify_quality_audit.time.monotonic", side_effect=[100.0, 101.0]
        ):
            audit = CaptureQualityAudit(44100, "Loopback", GOOD_SETTINGS)
            audit.record_audio_callback(44100, "input overflow")
            result = audit.finish()

        self.assertFalse(result["quality_gate_pass"])
        self.assertEqual(result["callback_status_count"], 1)
        self.assertTrue(any("コールバック異常" in warning for warning in result["warnings"]))

    def test_detects_playback_stall_and_timeline_slip(self):
        audio = np.full((44100, 2), 0.1, dtype=np.float32)
        with patch("spotify_quality_audit.time.time", side_effect=[1000.0, 1002.0]), patch(
            "spotify_quality_audit.time.monotonic", side_effect=[100.0, 102.0]
        ):
            audit = CaptureQualityAudit(44100, "Loopback", GOOD_SETTINGS)
            audit.record_audio_callback(44100, samples=audio)
            audit.observe_spotify_playback(("track",), "playing", 10.0, 100.5)
            audit.record_audio_callback(44100, samples=audio * 0.9)
            audit.observe_spotify_playback(("track",), "playing", 10.0, 101.5)
            result = audit.finish()

        self.assertEqual(result["playback_stall_count"], 1)
        self.assertEqual(result["timeline_slip_count"], 1)
        self.assertFalse(result["quality_gate_pass"])
        self.assertTrue(any(event["type"] == "playback_stall" for event in result["events"]))

        scoped = audit_for_audio_range(result, 44100, 88201)
        self.assertEqual(scoped["playback_stall_count"], 1)
        self.assertEqual(scoped["timeline_slip_count"], 1)

    def test_detects_long_digital_silence_and_boundary_discontinuity(self):
        silence = np.zeros((44100, 2), dtype=np.float32)
        discontinuous = np.ones((44100, 2), dtype=np.float32)
        with patch("spotify_quality_audit.time.time", side_effect=[1000.0, 1002.0]), patch(
            "spotify_quality_audit.time.monotonic", side_effect=[100.0, 102.0]
        ):
            audit = CaptureQualityAudit(44100, "Loopback", GOOD_SETTINGS)
            audit.record_audio_callback(44100, samples=silence)
            audit.record_audio_callback(44100, samples=discontinuous)
            result = audit.finish()

        self.assertEqual(result["digital_zero_run_count"], 1)
        self.assertEqual(result["boundary_discontinuities"], 1)
        self.assertFalse(result["quality_gate_pass"])

    def test_detects_adc_timeline_gap_at_callback_boundary(self):
        audio = np.full((4410, 2), 0.1, dtype=np.float32)
        with patch("spotify_quality_audit.time.time", side_effect=[1000.0, 1001.0]), patch(
            "spotify_quality_audit.time.monotonic", side_effect=[100.0, 101.0]
        ):
            audit = CaptureQualityAudit(44100, "BlackHole", GOOD_SETTINGS)
            audit.record_audio_callback(4410, samples=audio, adc_time=10.0)
            audit.record_audio_callback(4410, samples=audio * 0.9, adc_time=10.12)
            result = audit.finish()

        self.assertEqual(result["adc_timeline_gap_count"], 1)
        self.assertTrue(any(event["type"] == "adc_timeline_gap" for event in result["events"]))
        self.assertFalse(result["quality_gate_pass"])


if __name__ == "__main__":
    unittest.main()
